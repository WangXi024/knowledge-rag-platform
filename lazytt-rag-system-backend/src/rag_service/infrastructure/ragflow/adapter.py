"""RAGFlow v0.26.4 公共 HTTP API 到内部 ``RagEnginePort`` 的适配器。

本地 UUID 从不直接当作 RAGFlow ID：每次操作先在当前 ``security_scope_id`` 下读取
本地映射，再调用外部 API；返回的 dataset/document ID 也必须反查本地记录后才能形成
候选，从而阻止外部脏数据或 ID 猜测越过租户边界。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx

from rag_service.domain.errors import RagError, RagFlowError, ResourceNotFoundError
from rag_service.domain.models import (
    DocumentRecord,
    DocumentStatus,
    KnowledgeBase,
    RetrievalCandidate,
    SourcePosition,
)
from rag_service.domain.ports import (
    CreateDatasetCommand,
    DeleteEngineDatasetCommand,
    DeleteEngineDocumentCommand,
    DeleteEngineDocumentVersionCommand,
    DocumentRepository,
    DocumentSource,
    EngineChunk,
    EngineDataset,
    EngineDocument,
    EngineDocumentStatus,
    EngineHealth,
    EngineRetrievalQuery,
    EngineRetrievalResult,
    GetDocumentSourceQuery,
    GetEngineDatasetQuery,
    GetEngineDocumentQuery,
    GetEngineDocumentVersionQuery,
    KnowledgeBaseRepository,
    ListEngineChunksQuery,
    ListEngineDocumentsQuery,
    ListEngineVersionChunksQuery,
    Page,
    ParseDocumentCommand,
    ParseEngineDocumentVersionCommand,
    SetEngineDocumentVersionAvailabilityCommand,
    SetEngineVersionChunksAvailabilityCommand,
    StopEngineDocumentVersionCommand,
    StopParseCommand,
    UpdateEngineDatasetCommand,
    UpdateEngineDocumentCommand,
    UpdateEngineDocumentVersionCommand,
    UploadDocumentCommand,
    UploadDocumentVersionCommand,
)
from rag_service.infrastructure.ragflow.image_pdf import IMAGE_PDF_PROFILE, image_to_pdf
from rag_service.settings import RagFlowSection

_RETRY_STATUSES = frozenset({429, 502, 503, 504})
_UPLOAD_INTEGRITY_ATTEMPTS = 2
_SAFE_NAME = re.compile(r"[^\w\u4e00-\u9fff.-]+", re.UNICODE)
_LOGGER = logging.getLogger(__name__)
# RAGFlow v0.26.4 会按这些扩展名强制使用内置 ``presentation`` 解析器，并明确拒绝
# 文档 PATCH 请求中的 ``chunk_method``。Gateway 仍可写入 ``parser_config``，因此只需
# 保留原生解析器并下发版本化 Chunk Profile，无需修改或 Fork RAGFlow 源码。
_PRESENTATION_EXTENSIONS = frozenset({".ppt", ".pptx", ".pages"})
# RAGFlow 同样会按图片扩展名固定使用 ``picture`` 解析器，并拒绝文档 PATCH 请求中的
# ``chunk_method``。图片仍需在配置更新后显式 POST /chunks，不复用演示文稿防重分支。
_VISUAL_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".webp"})
# RAGFlow 公共 API 同时存在文字状态和数字状态。这里仅用于判断“解析已经启动或完成”，
# 防止 PATCH Presentation 配置触发内部排队后，Gateway 又重复创建第二个解析任务。
_PARSE_STARTED_STATES = frozenset({"RUNNING", "PARSING", "DONE", "1", "3"})
_METADATA_OPERATOR_MAP = {
    "eq": "is",
    "ne": "not is",
    "contains": "contains",
    "not_contains": "not contains",
    "starts_with": "start with",
    "empty": "empty",
    "not_empty": "not empty",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
}


def _dict(value: Any) -> dict[str, Any]:
    """要求下游值为 JSON Object，否则报告协议无效。"""
    if not isinstance(value, dict):
        raise RagFlowError.invalid_response()
    return value


def _list(value: Any) -> list[Any]:
    """要求下游值为 JSON Array。"""
    if not isinstance(value, list):
        raise RagFlowError.invalid_response()
    return value


def _string(value: Any) -> str:
    """要求下游值为非空字符串。"""
    if not isinstance(value, str) or not value:
        raise RagFlowError.invalid_response()
    return value


def _number(value: Any, default: float = 0.0) -> float:
    """读取有限数值；值缺失时使用调用方默认值。"""
    if value is None:
        return default
    if not isinstance(value, int | float) or not math.isfinite(float(value)):
        raise RagFlowError.invalid_response()
    return float(value)


def _integer(value: Any, default: int = 0) -> int:
    """读取数学意义上的整数，拒绝带小数的下游数值。"""
    number = _number(value, float(default))
    if not number.is_integer():
        raise RagFlowError.invalid_response()
    return int(number)


def _ragflow_duplicate_name_index(expected_name: str, actual_name: str) -> int | None:
    """识别 RAGFlow 为避免重名而生成的 ``file(1).ext`` 名称。

    Gateway 发送给 RAGFlow 的文件名以本地文档 UUID 开头，因此“原名”以及由
    RAGFlow 在最后一个扩展名前增加的正整数后缀都仍然只属于同一条本地文档记录。
    返回 ``0`` 表示名称未变化，正整数表示 RAGFlow 的重名序号；其他名称返回
    ``None``，调用方必须拒绝把无关文档映射到当前租户文档。
    """
    if actual_name == expected_name:
        return 0
    stem, separator, suffix = expected_name.rpartition(".")
    if not separator:
        stem = expected_name
    extension = f"{separator}{suffix}" if separator else ""
    matched = re.fullmatch(
        rf"{re.escape(stem)}\(([1-9][0-9]*)\){re.escape(extension)}",
        actual_name,
    )
    return int(matched.group(1)) if matched else None


def _positions(value: Any) -> tuple[SourcePosition, ...]:
    """兼容 RAGFlow 的对象/数组位置格式，规范化页码与四元坐标。"""
    if not isinstance(value, list):
        return ()
    positions: list[SourcePosition] = []
    for item in value:
        if isinstance(item, dict):
            page = item.get("page")
            bbox = item.get("bbox")
        elif isinstance(item, list | tuple) and item:
            page = item[0]
            bbox = item[1:5] if len(item) >= 5 else None
        else:
            continue
        normalized_page = max(1, int(page)) if isinstance(page, int | float) else None
        normalized_bbox: tuple[float, float, float, float] | None = None
        if (
            isinstance(bbox, list | tuple)
            and len(bbox) == 4
            and all(isinstance(part, int | float) for part in bbox)
        ):
            normalized_bbox = (
                float(bbox[0]),
                float(bbox[1]),
                float(bbox[2]),
                float(bbox[3]),
            )
        positions.append(
            SourcePosition(
                page=normalized_page,
                bbox=normalized_bbox,
            )
        )
    return tuple(positions)


def _ragflow_metadata_condition(value: dict[str, Any] | None) -> dict[str, Any] | None:
    """把本服务字段/operator/value 条件翻译为 RAGFlow 的名称与比较操作符。"""
    if value is None:
        return None
    conditions = value.get("conditions")
    if not isinstance(conditions, list):
        return value
    translated: list[dict[str, Any]] = []
    for raw in conditions:
        if not isinstance(raw, dict):
            translated.append(raw)
            continue
        field = raw.get("field")
        operator = raw.get("operator")
        translated.append(
            {
                "name": field,
                "comparison_operator": _METADATA_OPERATOR_MAP.get(str(operator), operator),
                "value": raw.get("value"),
            }
        )
    return {
        "logic": value.get("logic", "and"),
        "conditions": translated,
    }


class RagFlowAdapter:
    """实现数据集、文档、解析、块检索和健康检查的 RAG 引擎端口。"""

    def __init__(
        self,
        settings: RagFlowSection,
        knowledge_bases: KnowledgeBaseRepository,
        documents: DocumentRepository,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """注入配置和本地映射仓库，创建或接收复用的异步 HTTP 客户端。"""
        self._settings = settings
        self._knowledge_bases = knowledge_bases
        self._documents = documents
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=settings.base_url.rstrip("/"),
            timeout=httpx.Timeout(
                connect=settings.connect_timeout_seconds,
                read=settings.read_timeout_seconds,
                write=settings.upload_timeout_seconds,
                pool=settings.connect_timeout_seconds,
            ),
        )
        self._client.headers["Authorization"] = f"Bearer {settings.api_key}"

    async def close(self) -> None:
        """仅关闭适配器自行创建的客户端。"""
        if self._owns_client:
            await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        retry: bool = True,
        timeout_seconds: int | None = None,
        **kwargs: Any,
    ) -> Any:
        """统一执行 RAGFlow 请求、有限重试、Envelope 校验和错误映射。

        非幂等创建/上传调用会传 ``retry=False``，避免网络响应丢失后重复创建资源；
        401 单独映射为凭据错误，429/网关临时故障才允许有限退避重试。
        """
        attempts = self._settings.max_attempts if retry else 1
        for attempt in range(attempts):
            try:
                response = await self._client.request(
                    method,
                    path,
                    timeout=timeout_seconds,
                    **kwargs,
                )
            except httpx.TimeoutException as exc:
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.25 * (attempt + 1))
                    continue
                raise RagFlowError.timeout() from exc
            except httpx.RequestError as exc:
                if attempt + 1 < attempts:
                    await asyncio.sleep(0.25 * (attempt + 1))
                    continue
                raise RagFlowError.connection() from exc

            if response.status_code == 401:
                raise RagFlowError.unauthorized()
            if response.status_code in _RETRY_STATUSES and attempt + 1 < attempts:
                await asyncio.sleep(0.25 * (attempt + 1))
                continue
            if not response.is_success:
                raise RagFlowError.downstream(retryable=response.status_code in _RETRY_STATUSES)
            try:
                payload = response.json()
            except ValueError as exc:
                raise RagFlowError.invalid_response() from exc
            envelope = _dict(payload)
            if envelope.get("code") != 0:
                raise RagFlowError.downstream()
            return envelope.get("data")
        raise RagFlowError.connection()

    async def _knowledge_base(
        self, security_scope_id: str, knowledge_base_id: UUID
    ) -> KnowledgeBase:
        """在给定隔离范围读取本地知识库映射。"""
        knowledge_base = await self._knowledge_bases.get(security_scope_id, knowledge_base_id)
        if knowledge_base is None:
            raise ResourceNotFoundError("RAG-KB-001", "知识库不存在")
        return knowledge_base

    async def _mapped_knowledge_base(
        self, security_scope_id: str, knowledge_base_id: UUID
    ) -> KnowledgeBase:
        """读取本地知识库并要求它已有 RAGFlow dataset ID。"""
        knowledge_base = await self._knowledge_base(security_scope_id, knowledge_base_id)
        if not knowledge_base.ragflow_dataset_id:
            raise RagFlowError.invalid_response()
        return knowledge_base

    async def _document(
        self,
        security_scope_id: str,
        knowledge_base_id: UUID,
        document_id: UUID,
    ) -> DocumentRecord:
        """读取本地文档，并额外确认它确实属于命令中的知识库。"""
        document = await self._documents.get(security_scope_id, document_id)
        if document is None or document.knowledge_base_id != knowledge_base_id:
            raise ResourceNotFoundError("RAG-DOC-001", "文档不存在")
        return document

    @staticmethod
    def _dataset(data: Any) -> EngineDataset:
        """把 RAGFlow 数据集响应严格转换为标准引擎视图。"""
        item = _dict(data)
        return EngineDataset(
            engine_id=_string(item.get("id")),
            name=_string(item.get("name")),
            chunk_method=_string(item.get("chunk_method")),
        )

    @staticmethod
    def _document_status(data: Any, dataset_id: str) -> EngineDocumentStatus:
        """转换文档状态，并验证响应中的数据集 ID 未串库。"""
        item = _dict(data)
        item_dataset_id = item.get("dataset_id") or item.get("kb_id") or dataset_id
        if _string(item_dataset_id) != dataset_id:
            raise RagFlowError.invalid_response()
        return EngineDocumentStatus(
            engine_id=_string(item.get("id")),
            dataset_engine_id=dataset_id,
            name=_string(item.get("name")),
            run_state=_string(item.get("run")),
            progress=_number(item.get("progress")),
            chunk_count=_integer(item.get("chunk_count")),
            token_count=_integer(item.get("token_count")),
        )

    @staticmethod
    def _safe_dataset_name(knowledge_base_id: UUID, name: str) -> str:
        """清洗用户名称并加入 UUID 前缀，生成 RAGFlow 内唯一且安全的数据集名。"""
        cleaned = _SAFE_NAME.sub("-", name.strip()).strip(".-") or "dataset"
        return f"{str(knowledge_base_id)[:8]}-{cleaned[:100]}"

    def _resolve_parser_model(self, parser_config: dict[str, Any]) -> dict[str, Any]:
        """将视觉 PDF Profile 的固定占位符绑定到已配置的 RAGFlow 模型引用。

        只识别这一项受信占位符，不展开任意环境变量；复制配置以保留 Profile 的
        不可变内容。Dataset 与文档接口共用此转换，避免上传时回退到本地 DeepDOC。
        """
        resolved = dict(parser_config)
        if resolved.get("layout_recognize") == "${RAGFLOW_VISION_MODEL}":
            resolved["layout_recognize"] = self._settings.vision_model
        return resolved

    def _dataset_parser_request_fields(self, parser_config: dict[str, Any]) -> dict[str, Any]:
        """把引擎解析配置封装成 RAGFlow v0.26.4 Dataset API 可接受的请求字段。

        该版本的解析内核已经支持 ``overlapped_percent``，但公开 API 的
        ``ParserConfig`` 模型尚未声明它，直接发送会返回 code=101。公开模型同时提供
        顶层 ``ext`` 扩展入口，Dataset 服务会在校验标准字段后用其中的完整
        ``parser_config`` 覆盖请求，再保存到数据库。因此这里发送两份受信配置：

        - ``parser_config`` 去掉暂未进入公开 Schema 的重叠字段，用于正常类型校验；
        - ``ext.parser_config`` 保留完整配置，让解析任务真正收到重叠百分比。

        这个兼容层只处理传输形状，不修改调用方字典，也不直接访问 RAGFlow 数据库。
        等后续固定版本把该字段加入公开 Schema，只需在这里移除兼容封装。
        """
        expanded_config = self._resolve_parser_model(parser_config)
        nested_extension = expanded_config.pop("ext", {})
        if isinstance(nested_extension, dict):
            expanded_config.update(nested_extension)
        public_config = dict(expanded_config)
        public_config.pop("overlapped_percent", None)
        return {
            "parser_config": public_config,
            "ext": {"parser_config": expanded_config},
        }

    def _document_parser_config(self, parser_config: dict[str, Any]) -> dict[str, Any]:
        """把可信 Profile 转成 RAGFlow 文档 PATCH 接受的 ParserConfig。

        RAGFlow v0.26.4 的文档更新 Schema 会保留 ``parser_config.ext`` 并在服务层
        展开，但顶层模型尚未声明 ``overlapped_percent``。因此公开字段照常发送，
        重叠百分比放入同一对象的 ``ext``；这与 Dataset 创建时的兼容传输目的相同，
        只是文档接口的扩展字段嵌套位置不同。输入来自 ProfileCatalog，不接受浏览器
        任意 JSON，返回新字典也不会污染任务重试使用的共享配置。
        """
        expanded_config = self._resolve_parser_model(parser_config)
        existing_ext = expanded_config.pop("ext", {})
        public_config = dict(expanded_config)
        overlap = public_config.pop("overlapped_percent", None)
        ext = dict(existing_ext) if isinstance(existing_ext, dict) else {}
        if overlap is not None:
            ext["overlapped_percent"] = overlap
        if ext:
            public_config["ext"] = ext
        return public_config

    async def create_dataset(self, command: CreateDatasetCommand) -> EngineDataset:
        """确认本地知识库存在后，以禁重试请求创建外部数据集。"""
        await self._knowledge_base(
            command.security.security_scope_id,
            command.knowledge_base_id,
        )
        data = await self._request(
            "POST",
            "/api/v1/datasets",
            retry=False,
            json={
                "name": self._safe_dataset_name(command.knowledge_base_id, command.name),
                "description": command.description,
                "embedding_model": command.embedding_model,
                "permission": "me",
                "chunk_method": command.chunk_method,
                **self._dataset_parser_request_fields(command.parser_config),
            },
        )
        return self._dataset(data)

    async def get_dataset(self, query: GetEngineDatasetQuery) -> EngineDataset:
        """按本地映射查询唯一外部数据集，并核对返回 ID。"""
        knowledge_base = await self._mapped_knowledge_base(
            query.security.security_scope_id,
            query.knowledge_base_id,
        )
        data = await self._request(
            "GET",
            "/api/v1/datasets",
            params={"id": knowledge_base.ragflow_dataset_id},
        )
        items = data if isinstance(data, list) else _dict(data).get("docs")
        datasets = _list(items)
        if len(datasets) != 1:
            raise RagFlowError.invalid_response()
        dataset = self._dataset(datasets[0])
        if dataset.engine_id != knowledge_base.ragflow_dataset_id:
            raise RagFlowError.invalid_response()
        return dataset

    async def update_dataset(self, command: UpdateEngineDatasetCommand) -> EngineDataset:
        """只发送非 None 字段更新数据集，再回读得到标准化最新状态。"""
        knowledge_base = await self._mapped_knowledge_base(
            command.security.security_scope_id,
            command.knowledge_base_id,
        )
        body: dict[str, Any] = {
            name: value
            for name, value in {
                "name": command.name,
                "description": command.description,
            }.items()
            if value is not None
        }
        if command.parser_config is not None:
            body.update(self._dataset_parser_request_fields(command.parser_config))
        await self._request(
            "PUT",
            f"/api/v1/datasets/{knowledge_base.ragflow_dataset_id}",
            json=body,
        )
        return await self.get_dataset(
            GetEngineDatasetQuery(
                security=command.security,
                knowledge_base_id=command.knowledge_base_id,
            )
        )

    async def delete_dataset(self, command: DeleteEngineDatasetCommand) -> None:
        """按本地映射删除唯一外部数据集，不使用危险的 delete_all。"""
        knowledge_base = await self._mapped_knowledge_base(
            command.security.security_scope_id,
            command.knowledge_base_id,
        )
        await self._request(
            "DELETE",
            "/api/v1/datasets",
            json={"ids": [knowledge_base.ragflow_dataset_id], "delete_all": False},
        )

    async def _set_document_metadata(
        self,
        dataset_id: str,
        document_id: str,
        metadata: dict[str, Any],
    ) -> None:
        """文档有自定义元数据时单独更新 RAGFlow meta_fields。"""
        if not metadata:
            return
        await self._request(
            "PUT",
            f"/api/v1/datasets/{dataset_id}/documents/{document_id}",
            json={"meta_fields": metadata},
        )

    async def _delete_remote_documents(
        self,
        dataset_id: str,
        document_ids: tuple[str, ...],
    ) -> None:
        """通过 RAGFlow 官方 API 删除已确认损坏的外部文档。

        该方法只接收已经同时通过 Dataset、UUID 文件名族和文档 ID 校验的记录，避免
        完整性恢复误删其他租户或其他本地文档。删除是幂等恢复的一部分：损坏副本必须
        在重传前清理，否则 RAGFlow 会不断生成 ``(1)``、``(2)`` 重名文件。
        """
        if not document_ids:
            return
        await self._request(
            "DELETE",
            f"/api/v1/datasets/{dataset_id}/documents",
            json={"ids": list(document_ids)},
        )

    async def _remote_document_matches(
        self,
        *,
        dataset_id: str,
        engine_document_id: str,
        local_document: DocumentRecord,
        expected_size: int,
        expected_sha256: str,
    ) -> bool:
        """回读 RAGFlow 文件并与 Gateway 已验证的大小、SHA-256 逐字节核对。

        RAGFlow 的 JSON 响应只能证明它创建了文档记录，不能证明 multipart 正文没有
        被增加前导 CRLF。这里使用官方文档下载接口回读二进制并计算摘要；只返回布尔
        结果，不记录文件名或正文。网络、认证和非成功状态仍转换为稳定 RAGFlow 错误，
        交给 Job 的有限重试处理，不能把暂时无法回读误判为内容损坏并删除文档。
        """
        try:
            response = await self._client.get(
                f"/api/v1/datasets/{dataset_id}/documents/{engine_document_id}",
                timeout=self._settings.upload_timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise RagFlowError.timeout() from exc
        except httpx.RequestError as exc:
            raise RagFlowError.connection() from exc
        if response.status_code == 401:
            raise RagFlowError.unauthorized()
        if not response.is_success:
            raise RagFlowError.downstream(
                retryable=response.status_code in _RETRY_STATUSES,
            )

        remote_content = response.content
        remote_sha256 = hashlib.sha256(remote_content).hexdigest()
        matches = len(remote_content) == expected_size and remote_sha256 == expected_sha256.lower()
        if not matches:
            # 摘要只记录前 12 位，足以关联同一次故障，又不会把完整内容指纹长期暴露
            # 在日志中。日志不包含用户邮箱、文件名或任何正文。
            _LOGGER.warning(
                "ragflow_upload_integrity_mismatch "
                "document_id=%s engine_document_id=%s expected_size=%d actual_size=%d "
                "expected_sha256_prefix=%s actual_sha256_prefix=%s",
                local_document.id,
                engine_document_id,
                expected_size,
                len(remote_content),
                expected_sha256[:12],
                remote_sha256[:12],
            )
        return matches

    @staticmethod
    def _uploaded_document(
        data: Any,
        *,
        dataset_id: str,
        expected_name: str,
        expected_size: int,
    ) -> tuple[EngineDocument, int, int]:
        """校验一个上传结果的 Dataset、名称族和基础字段。

        RAGFlow 的 ``(1)`` 重名后缀仍可归属于同一个本地 UUID 文件名族，但响应中的
        大小不能再被视为内容完整性证据。方法先完成身份边界校验并保留原始大小，随后
        由回读校验方法比较真实二进制的大小与 SHA-256；跨 Dataset、无关文件名、缺失
        字段和负数大小会在回读前立即拒绝。

        返回值依次为标准文档、RAGFlow 原始大小和重名序号，供恢复逻辑选择最接近
        本地原文件的候选。此方法只做协议转换，不写数据库或外部服务。
        """
        item = _dict(data)
        item_dataset_id = _string(item.get("dataset_id") or item.get("kb_id"))
        if item_dataset_id != dataset_id:
            raise RagFlowError.invalid_response()
        actual_name = _string(item.get("name"))
        duplicate_index = _ragflow_duplicate_name_index(expected_name, actual_name)
        if duplicate_index is None:
            raise RagFlowError.invalid_response()
        actual_size = _integer(item.get("size"))
        if actual_size < 0:
            raise RagFlowError.invalid_response()
        return (
            EngineDocument(
                engine_id=_string(item.get("id")),
                dataset_engine_id=item_dataset_id,
                name=actual_name,
                run_state=_string(item.get("run")),
                size_bytes=expected_size,
            ),
            actual_size,
            duplicate_index,
        )

    async def upload_document(self, command: UploadDocumentCommand) -> EngineDocument:
        """复用完整的 UUID 名称族文档，否则上传、回读校验并有限重传。

        Worker 崩溃后重跑可能遇到“外部已上传、本地未记录”的窗口，故先查同名同大小
        文档。每个候选和新上传文档都通过官方接口回读，大小及 SHA-256 必须与 Gateway
        暂存阶段一致；确认损坏的同名副本会先删除，再以确定长度的 bytes 最多重传一次。
        无关名称或跨 Dataset 响应不会进入删除集合，从而保持租户和文档身份边界。
        """
        knowledge_base = await self._mapped_knowledge_base(
            command.security.security_scope_id,
            command.knowledge_base_id,
        )
        local_document = await self._document(
            command.security.security_scope_id,
            command.knowledge_base_id,
            command.document_id,
        )
        expected_size = command.expected_size_bytes or local_document.file_size_bytes
        expected_sha256 = command.expected_sha256 or local_document.content_sha256
        content_media_type = command.content_media_type or local_document.mime_type
        # 把已经由暂存层二次校验的文件读成确定长度 bytes。这样同一份正文可安全用于
        # 两次有限上传，不依赖文件句柄在 multipart 失败后的游标状态；100 MB 上传上限
        # 同时约束了单任务内存占用。
        try:
            command.content.seek(0)
            upload_content = command.content.read()
        except (OSError, ValueError) as exc:
            raise RagFlowError.invalid_response() from exc
        if (
            not isinstance(upload_content, bytes)
            or len(upload_content) != expected_size
            or hashlib.sha256(upload_content).hexdigest() != expected_sha256.lower()
        ):
            raise RagFlowError.invalid_response()

        # 图片策略先验证原图，再在线程中生成确定性 PDF。派生副本使用自己的大小和
        # 摘要参与重试校验；原图保存在 PDF 附件内，本地文档名称、摘要不变。
        engine_filename = command.filename
        if command.parser_profile_ref == IMAGE_PDF_PROFILE:
            upload_content = await asyncio.to_thread(image_to_pdf, upload_content)
            # 给新增扩展名预留四字节，保留开头 UUID，避免超长中文文件名超出引擎上限。
            engine_filename = (
                command.filename.encode("utf-8")[:251].decode("utf-8", "ignore") + ".pdf"
            )
            expected_size = len(upload_content)
            expected_sha256 = hashlib.sha256(upload_content).hexdigest()
            content_media_type = "application/pdf"

        dataset_id = _string(knowledge_base.ragflow_dataset_id)
        existing_data = _dict(
            await self._request(
                "GET",
                f"/api/v1/datasets/{dataset_id}/documents",
                params={"keywords": engine_filename, "page": 1, "page_size": 10},
            )
        )
        # ``keywords`` 可能返回相似名称。只有严格属于本地 UUID 文件名族且回读摘要
        # 完全一致的文档才能接管；大小漂移或摘要不一致的候选会进入受控删除集合。
        existing: list[tuple[tuple[int, str], EngineDocument]] = []
        corrupted_document_ids: list[str] = []
        for raw_item in _list(existing_data.get("docs")):
            item = _dict(raw_item)
            actual_name = _string(item.get("name"))
            duplicate_index = _ragflow_duplicate_name_index(engine_filename, actual_name)
            if duplicate_index is None:
                continue
            engine_document, actual_size, validated_duplicate_index = self._uploaded_document(
                item,
                dataset_id=dataset_id,
                expected_name=engine_filename,
                expected_size=expected_size,
            )
            size_matches = actual_size == expected_size
            content_matches = size_matches and await self._remote_document_matches(
                dataset_id=dataset_id,
                engine_document_id=engine_document.engine_id,
                local_document=local_document,
                expected_size=expected_size,
                expected_sha256=expected_sha256,
            )
            if not content_matches:
                corrupted_document_ids.append(engine_document.engine_id)
                continue
            existing.append(
                ((validated_duplicate_index, engine_document.engine_id), engine_document)
            )

        await self._delete_remote_documents(dataset_id, tuple(corrupted_document_ids))
        if existing:
            _, engine_document = min(
                existing,
                key=lambda candidate: candidate[0],
            )
            await self._set_document_metadata(
                engine_document.dataset_engine_id,
                engine_document.engine_id,
                command.metadata,
            )
            return engine_document

        # 非幂等 POST 不由通用 HTTP 重试器自动重放。这里每次都能拿到明确的外部文档
        # ID，并且仅在回读确认损坏后先删除该 ID，才进行第二次有界重传。
        for attempt in range(_UPLOAD_INTEGRITY_ATTEMPTS):
            data = await self._request(
                "POST",
                f"/api/v1/datasets/{dataset_id}/documents",
                retry=False,
                timeout_seconds=self._settings.upload_timeout_seconds,
                params={"type": "local"},
                files={
                    "file": (
                        engine_filename,
                        upload_content,
                        content_media_type,
                    )
                },
            )
            items = _list(data)
            if len(items) != 1:
                raise RagFlowError.invalid_response()
            engine_document, actual_size, _ = self._uploaded_document(
                items[0],
                dataset_id=dataset_id,
                expected_name=engine_filename,
                expected_size=expected_size,
            )
            integrity_ok = actual_size == expected_size and await self._remote_document_matches(
                dataset_id=dataset_id,
                engine_document_id=engine_document.engine_id,
                local_document=local_document,
                expected_size=expected_size,
                expected_sha256=expected_sha256,
            )
            if integrity_ok:
                await self._set_document_metadata(
                    engine_document.dataset_engine_id,
                    engine_document.engine_id,
                    command.metadata,
                )
                return engine_document

            await self._delete_remote_documents(
                dataset_id,
                (engine_document.engine_id,),
            )
            _LOGGER.warning(
                "ragflow_upload_integrity_retry document_id=%s attempt=%d max_attempts=%d",
                local_document.id,
                attempt + 1,
                _UPLOAD_INTEGRITY_ATTEMPTS,
            )

        raise RagFlowError.invalid_response()

    async def upload_document_version(
        self, command: UploadDocumentVersionCommand
    ) -> EngineDocument:
        """复用现有完整性上传协议，把候选版本保存为独立 UUID 文件名族。

        主文档在发布前仍保存旧文件大小和摘要，因此这里必须把候选版本已经由暂存层
        验证的大小、SHA-256 和 MIME 显式传给通用上传实现。通用实现仍会按 Scope
        读取逻辑文档确认归属，并回读 RAGFlow 二进制验证正文；唯一差别是文件名由
        ``version_id`` 开头，不会命中或删除旧版本的名称族。
        """
        return await self.upload_document(
            UploadDocumentCommand(
                security=command.security,
                knowledge_base_id=command.knowledge_base_id,
                document_id=command.document_id,
                filename=command.filename,
                parser_profile_ref=command.parser_profile_ref,
                content=command.content,
                metadata=command.metadata,
                content_media_type=command.media_type,
                expected_size_bytes=command.expected_size_bytes,
                expected_sha256=command.expected_sha256,
            )
        )

    async def _version_context(
        self,
        *,
        security_scope_id: str,
        knowledge_base_id: UUID,
        document_id: UUID,
    ) -> tuple[KnowledgeBase, DocumentRecord]:
        """验证候选物理 ID 操作仍属于当前 Scope、知识库和逻辑文档。

        物理 ID 来自受 Scope 保护的 ``DocumentVersionRepository``，但 Adapter 仍做
        第二道本地映射校验。这样未来调用方即使误传其他 Dataset 的 ID，也无法借用
        当前租户凭据绕过逻辑文档边界。
        """
        knowledge_base = await self._mapped_knowledge_base(
            security_scope_id,
            knowledge_base_id,
        )
        document = await self._document(
            security_scope_id,
            knowledge_base_id,
            document_id,
        )
        return knowledge_base, document

    async def get_document_version(
        self, query: GetEngineDocumentVersionQuery
    ) -> EngineDocumentStatus:
        """按明确物理 ID 查询候选状态，并核对响应仍属于目标 Dataset。"""
        knowledge_base, _ = await self._version_context(
            security_scope_id=query.security.security_scope_id,
            knowledge_base_id=query.knowledge_base_id,
            document_id=query.document_id,
        )
        dataset_id = _string(knowledge_base.ragflow_dataset_id)
        data = await self._request(
            "GET",
            f"/api/v1/datasets/{dataset_id}/documents",
            params={"id": query.engine_document_id, "page": 1, "page_size": 2},
        )
        docs = _list(_dict(data).get("docs"))
        if len(docs) != 1:
            raise RagFlowError.invalid_response()
        result = self._document_status(docs[0], dataset_id)
        if result.engine_id != query.engine_document_id:
            raise RagFlowError.invalid_response()
        return result

    async def update_document_version(
        self, command: UpdateEngineDocumentVersionCommand
    ) -> EngineDocument:
        """通过官方文档接口更新候选 Metadata，不修改当前活动映射。"""
        knowledge_base, _ = await self._version_context(
            security_scope_id=command.security.security_scope_id,
            knowledge_base_id=command.knowledge_base_id,
            document_id=command.document_id,
        )
        dataset_id = _string(knowledge_base.ragflow_dataset_id)
        await self._request(
            "PUT",
            f"/api/v1/datasets/{dataset_id}/documents/{command.engine_document_id}",
            json={"meta_fields": command.metadata},
        )
        status = await self.get_document_version(
            GetEngineDocumentVersionQuery(
                security=command.security,
                knowledge_base_id=command.knowledge_base_id,
                document_id=command.document_id,
                engine_document_id=command.engine_document_id,
            )
        )
        return EngineDocument(
            engine_id=status.engine_id,
            dataset_engine_id=status.dataset_engine_id,
            name=status.name,
            run_state=status.run_state,
            size_bytes=0,
        )

    async def delete_document_version(self, command: DeleteEngineDocumentVersionCommand) -> None:
        """删除失败候选或明确清理的退役物理文档，调用可安全重复。"""
        knowledge_base, _ = await self._version_context(
            security_scope_id=command.security.security_scope_id,
            knowledge_base_id=command.knowledge_base_id,
            document_id=command.document_id,
        )
        await self._delete_remote_documents(
            _string(knowledge_base.ragflow_dataset_id),
            (command.engine_document_id,),
        )

    async def start_document_version_parse(
        self, command: ParseEngineDocumentVersionCommand
    ) -> None:
        """给候选物理文档写入文档级 Profile 后启动 RAGFlow 解析。"""
        knowledge_base, _ = await self._version_context(
            security_scope_id=command.security.security_scope_id,
            knowledge_base_id=command.knowledge_base_id,
            document_id=command.document_id,
        )
        dataset_id = _string(knowledge_base.ragflow_dataset_id)
        await self._request(
            "PATCH",
            f"/api/v1/datasets/{dataset_id}/documents/{command.engine_document_id}",
            json={
                "chunk_method": command.chunk_method,
                "parser_config": self._document_parser_config(command.parser_config),
            },
        )
        await self._request(
            "POST",
            f"/api/v1/datasets/{dataset_id}/chunks",
            json={"document_ids": [command.engine_document_id]},
        )

    async def stop_document_version_parse(self, command: StopEngineDocumentVersionCommand) -> None:
        """通过官方 chunks 删除入口停止候选解析并清理其未完成 Chunk。"""
        knowledge_base, _ = await self._version_context(
            security_scope_id=command.security.security_scope_id,
            knowledge_base_id=command.knowledge_base_id,
            document_id=command.document_id,
        )
        await self._request(
            "DELETE",
            f"/api/v1/datasets/{knowledge_base.ragflow_dataset_id}/chunks",
            json={"document_ids": [command.engine_document_id]},
        )

    async def list_document_version_chunks(self, query: ListEngineVersionChunksQuery) -> Page:
        """读取候选物理 Chunk，并统一归属到稳定逻辑文档 UUID。"""
        knowledge_base, _ = await self._version_context(
            security_scope_id=query.security.security_scope_id,
            knowledge_base_id=query.knowledge_base_id,
            document_id=query.document_id,
        )
        dataset_id = _string(knowledge_base.ragflow_dataset_id)
        data = _dict(
            await self._request(
                "GET",
                f"/api/v1/datasets/{dataset_id}/documents/{query.engine_document_id}/chunks",
                params={"page": query.page, "page_size": query.page_size},
            )
        )
        items = tuple(
            EngineChunk(
                engine_chunk_id=_string(item.get("id")),
                document_id=query.document_id,
                content=_string(item.get("content")),
                positions=_positions(item.get("positions")),
                metadata=_dict(item.get("metadata")) if item.get("metadata") is not None else {},
            )
            for item in (_dict(raw) for raw in _list(data.get("chunks")))
        )
        return Page(
            items=items,
            page=query.page,
            page_size=query.page_size,
            total=_integer(data.get("total"), len(items)),
        )

    async def set_document_version_chunks_available(
        self, command: SetEngineVersionChunksAvailabilityCommand
    ) -> None:
        """分批切换候选/退役版本 Chunk 可用性。

        官方接口要求显式 Chunk ID，空集合表示无需操作而不是“全部”。每批 500 个
        控制请求体大小；任一批失败会抛稳定下游错误，由 Worker 执行反向补偿并保持
        旧 ``DocumentRecord`` 映射不变。
        """
        if not command.chunk_ids:
            return
        knowledge_base, _ = await self._version_context(
            security_scope_id=command.security.security_scope_id,
            knowledge_base_id=command.knowledge_base_id,
            document_id=command.document_id,
        )
        dataset_id = _string(knowledge_base.ragflow_dataset_id)
        for offset in range(0, len(command.chunk_ids), 500):
            await self._request(
                "PATCH",
                f"/api/v1/datasets/{dataset_id}/documents/{command.engine_document_id}/chunks",
                json={
                    "chunk_ids": list(command.chunk_ids[offset : offset + 500]),
                    "available": command.available,
                },
            )

    async def set_document_version_available(
        self, command: SetEngineDocumentVersionAvailabilityCommand
    ) -> None:
        """通过 RAGFlow 官方文档状态接口启用或停用完整物理版本。

        这个接口正是 RAGFlow 管理界面“启用”开关使用的入口。它会在 RAGFlow 内部
        同步文档 ``status`` 与该文档全部 Chunk 的 ``available_int``，所以退役旧版
        不只是不参与检索，管理界面也会明确显示为关闭。响应中的任一局部失败都会由
        ``_request`` 映射成下游错误，交给 Worker 的 Saga 补偿逻辑处理。
        """
        knowledge_base, _ = await self._version_context(
            security_scope_id=command.security.security_scope_id,
            knowledge_base_id=command.knowledge_base_id,
            document_id=command.document_id,
        )
        dataset_id = _string(knowledge_base.ragflow_dataset_id)
        await self._request(
            "POST",
            f"/api/v1/datasets/{dataset_id}/documents/batch-update-status",
            json={
                "doc_ids": [command.engine_document_id],
                "status": 1 if command.available else 0,
            },
        )

    async def get_document(self, query: GetEngineDocumentQuery) -> EngineDocumentStatus:
        """按本地映射查询唯一文档状态，并核对文档与数据集 ID。"""
        knowledge_base = await self._mapped_knowledge_base(
            query.security.security_scope_id,
            query.knowledge_base_id,
        )
        document = await self._document(
            query.security.security_scope_id,
            query.knowledge_base_id,
            query.document_id,
        )
        if not document.ragflow_document_id:
            raise RagFlowError.invalid_response()
        dataset_id = _string(knowledge_base.ragflow_dataset_id)
        data = await self._request(
            "GET",
            f"/api/v1/datasets/{dataset_id}/documents",
            params={"id": document.ragflow_document_id, "page": 1, "page_size": 2},
        )
        docs = _list(_dict(data).get("docs"))
        if len(docs) != 1:
            raise RagFlowError.invalid_response()
        status = self._document_status(docs[0], dataset_id)
        if status.engine_id != document.ragflow_document_id:
            raise RagFlowError.invalid_response()
        return status

    async def list_documents(self, query: ListEngineDocumentsQuery) -> Page:
        """分页读取外部数据集文档并转换为标准状态页。"""
        knowledge_base = await self._mapped_knowledge_base(
            query.security.security_scope_id,
            query.knowledge_base_id,
        )
        dataset_id = _string(knowledge_base.ragflow_dataset_id)
        data = _dict(
            await self._request(
                "GET",
                f"/api/v1/datasets/{dataset_id}/documents",
                params={"page": query.page, "page_size": query.page_size},
            )
        )
        docs = tuple(self._document_status(item, dataset_id) for item in _list(data.get("docs")))
        return Page(
            items=docs,
            page=query.page,
            page_size=query.page_size,
            total=_integer(data.get("total"), len(docs)),
        )

    async def update_document(self, command: UpdateEngineDocumentCommand) -> EngineDocument:
        """更新文档元数据后回读状态，组合成标准文档视图。"""
        knowledge_base = await self._mapped_knowledge_base(
            command.security.security_scope_id,
            command.knowledge_base_id,
        )
        document = await self._document(
            command.security.security_scope_id,
            command.knowledge_base_id,
            command.document_id,
        )
        if not document.ragflow_document_id:
            raise RagFlowError.invalid_response()
        await self._request(
            "PUT",
            f"/api/v1/datasets/{knowledge_base.ragflow_dataset_id}/documents/"
            f"{document.ragflow_document_id}",
            json={"meta_fields": command.metadata},
        )
        status = await self.get_document(
            GetEngineDocumentQuery(
                security=command.security,
                knowledge_base_id=command.knowledge_base_id,
                document_id=command.document_id,
            )
        )
        return EngineDocument(
            engine_id=status.engine_id,
            dataset_engine_id=status.dataset_engine_id,
            name=status.name,
            run_state=status.run_state,
            size_bytes=document.file_size_bytes,
        )

    async def delete_document(self, command: DeleteEngineDocumentCommand) -> None:
        """使用本地映射删除单个外部文档。"""
        knowledge_base = await self._mapped_knowledge_base(
            command.security.security_scope_id,
            command.knowledge_base_id,
        )
        document = await self._document(
            command.security.security_scope_id,
            command.knowledge_base_id,
            command.document_id,
        )
        if not document.ragflow_document_id:
            raise RagFlowError.invalid_response()
        await self._request(
            "DELETE",
            f"/api/v1/datasets/{knowledge_base.ragflow_dataset_id}/documents",
            json={"ids": [document.ragflow_document_id]},
        )

    async def start_parse(self, command: ParseDocumentCommand) -> None:
        """应用文档级 Profile 后，请求 RAGFlow 创建或重建文本块。

        PATCH 与启动解析都使用已经完成 Scope 映射的 Dataset/Document ID。配置写入
        成功后才发起解析；写入失败会让当前 Job 按统一失败策略处理，不能静默沿用
        错误的 Dataset 默认值。旧任务未携带配置时跳过 PATCH，保持向后兼容。

        PPT/PPTX/Pages 和图片是 RAGFlow v0.26.4 的特殊文件族：引擎会固定使用原生
        ``presentation`` 或 ``picture`` 解析器，并拒绝文档级 ``chunk_method`` 更新。
        因此这些文件只 PATCH ``parser_config``；演示文稿首次解析还会回读一次状态，
        避免引擎已经自动排队后重复 POST。图片仍正常 POST，用户明确发起的演示文稿
        重解析也不走防重复分支。
        """
        knowledge_base = await self._mapped_knowledge_base(
            command.security.security_scope_id,
            command.knowledge_base_id,
        )
        document = await self._document(
            command.security.security_scope_id,
            command.knowledge_base_id,
            command.document_id,
        )
        if not document.ragflow_document_id:
            raise RagFlowError.invalid_response()
        extension = Path(document.filename).suffix.lower()
        is_presentation = extension in _PRESENTATION_EXTENSIONS
        # 图片 MinerU 策略上传的是 PDF，必须设置 naive，不能按原图走 picture 分支。
        is_visual = (
            extension in _VISUAL_EXTENSIONS
            and document.parser_profile_ref != IMAGE_PDF_PROFILE
        )
        if command.chunk_method is not None or command.parser_config is not None:
            if command.chunk_method is None or command.parser_config is None:
                raise RagFlowError.invalid_response()
            profile_payload: dict[str, Any] = {
                "parser_config": self._document_parser_config(command.parser_config),
            }
            if not is_presentation and not is_visual:
                profile_payload["chunk_method"] = command.chunk_method
            await self._request(
                "PATCH",
                (
                    f"/api/v1/datasets/{knowledge_base.ragflow_dataset_id}"
                    f"/documents/{document.ragflow_document_id}"
                ),
                json=profile_payload,
            )
        if is_presentation and not command.delete_existing:
            current = await self.get_document(
                GetEngineDocumentQuery(
                    security=command.security,
                    knowledge_base_id=command.knowledge_base_id,
                    document_id=command.document_id,
                )
            )
            if current.run_state.strip().upper() in _PARSE_STARTED_STATES:
                return
        try:
            await self._request(
                "POST",
                f"/api/v1/datasets/{knowledge_base.ragflow_dataset_id}/chunks",
                json={"document_ids": [document.ragflow_document_id]},
            )
        except RagFlowError:
            if not is_presentation or command.delete_existing:
                raise
            # PATCH 与 POST 之间存在很短的并发窗口：RAGFlow 可能刚完成内部排队，
            # 导致 POST 返回“任务已存在”。只有回读确认目标文档确实正在解析或已经完成
            # 时才把本次调用视为成功；连接故障或其他业务失败仍按原异常向上抛出。
            current = await self.get_document(
                GetEngineDocumentQuery(
                    security=command.security,
                    knowledge_base_id=command.knowledge_base_id,
                    document_id=command.document_id,
                )
            )
            if current.run_state.strip().upper() not in _PARSE_STARTED_STATES:
                raise
            _LOGGER.info(
                "presentation_parse_already_started document_id=%s run_state=%s",
                document.id,
                current.run_state,
            )

    async def stop_parse(self, command: StopParseCommand) -> None:
        """请求 RAGFlow 停止文档解析。"""
        knowledge_base = await self._mapped_knowledge_base(
            command.security.security_scope_id,
            command.knowledge_base_id,
        )
        document = await self._document(
            command.security.security_scope_id,
            command.knowledge_base_id,
            command.document_id,
        )
        if not document.ragflow_document_id:
            raise RagFlowError.invalid_response()
        await self._request(
            "DELETE",
            f"/api/v1/datasets/{knowledge_base.ragflow_dataset_id}/chunks",
            json={"document_ids": [document.ragflow_document_id]},
        )

    @staticmethod
    def _require_preview_revision(document: DocumentRecord, revision: int | None) -> None:
        """固定修订的查看必须使用 READY 文档；更新/重解析时拒绝把位置套在变化的文件上。"""
        if revision is not None and (
            document.document_revision != revision or document.status != DocumentStatus.READY
        ):
            raise RagError(
                "RAG-DOC-019", "文档版本或解析状态已变化，请刷新文档列表后重新打开", 409
            )

    async def get_document_source(self, query: GetDocumentSourceQuery) -> DocumentSource:
        """从官方下载接口流式读取当前 PDF，不落盘、不转换文件、不向浏览器泄漏密钥。

        先验证 Scope/映射/修订与类型；禁止重定向，检查 MIME、长度和 PDF 文件头。
        一次请求不自动重试；正文按 64 KiB 转发并限于已登记大小，关闭或取消释放连接。
        """
        knowledge_base = await self._mapped_knowledge_base(
            query.security.security_scope_id, query.knowledge_base_id
        )
        document = await self._document(
            query.security.security_scope_id, query.knowledge_base_id, query.document_id
        )
        self._require_preview_revision(document, query.document_revision)
        if not document.filename.lower().endswith(".pdf"):
            raise RagError("RAG-DOC-020", "目前仅支持 PDF 原文件预览", 415)
        if not document.ragflow_document_id:
            raise ResourceNotFoundError("RAG-DOC-021", "原文件不可用")
        # 预览沿用管理端单文件 100 MiB 上限，不允许下游无限流占用连接或浏览器内存。
        if not 0 < document.file_size_bytes <= 100 * 1024 * 1024:
            raise RagError("RAG-DOC-022", "原文件大小不在预览范围内（上限 100 MiB）", 413)
        response: httpx.Response | None = None
        try:
            request = self._client.build_request(
                "GET",
                f"/api/v1/datasets/{knowledge_base.ragflow_dataset_id}/documents/"
                f"{document.ragflow_document_id}",
                timeout=self._settings.read_timeout_seconds,
            )
            response = await self._client.send(request, stream=True, follow_redirects=False)
            if response.status_code == 404:
                raise ResourceNotFoundError("RAG-DOC-021", "原文件不可用")
            if response.status_code == 401:
                raise RagFlowError.unauthorized()
            if response.status_code != 200:
                raise RagFlowError.downstream(retryable=response.status_code in _RETRY_STATUSES)
            media_type = response.headers.get("content-type", "").split(";")[0].strip()
            length = response.headers.get("content-length")
            if media_type not in {"application/pdf", "application/octet-stream"} or (
                length is not None and length != str(document.file_size_bytes)
            ):
                raise RagFlowError.invalid_response()
            chunks = response.aiter_bytes(chunk_size=64 * 1024)
            first = await anext(chunks, b"")
            if not first.startswith(b"%PDF-") or len(first) > document.file_size_bytes:
                raise RagFlowError.invalid_response()
        except BaseException as exc:
            # 包含取消路径，确保尚未交给响应层的连接不会遗留；不记录下游响应正文。
            if response is not None:
                await response.aclose()
            if isinstance(exc, httpx.TimeoutException):
                raise RagFlowError.timeout() from exc
            if isinstance(exc, httpx.RequestError):
                raise RagFlowError.connection() from exc
            raise

        assert response is not None

        async def content() -> AsyncIterator[bytes]:
            """消费一次原文件流；内容长度/摘要错误中断响应，不能当作有效 PDF 缓存。"""
            received = len(first)
            digest = hashlib.sha256(first)
            try:
                yield first
                async for part in chunks:
                    received += len(part)
                    if received > document.file_size_bytes:
                        raise RagFlowError.invalid_response()
                    digest.update(part)
                    yield part
                if (
                    received != document.file_size_bytes
                    or digest.hexdigest() != document.content_sha256
                ):
                    raise RagFlowError.invalid_response()
            finally:
                await response.aclose()

        return DocumentSource(
            document.filename, document.file_size_bytes, content(), response.aclose
        )

    async def list_chunks(self, query: ListEngineChunksQuery) -> Page:
        """分页读取文档块，并把外部位置/元数据转换为领域结构。"""
        knowledge_base = await self._mapped_knowledge_base(
            query.security.security_scope_id,
            query.knowledge_base_id,
        )
        document = await self._document(
            query.security.security_scope_id,
            query.knowledge_base_id,
            query.document_id,
        )
        if not document.ragflow_document_id:
            raise RagFlowError.invalid_response()
        self._require_preview_revision(document, query.document_revision)
        data = _dict(
            await self._request(
                "GET",
                f"/api/v1/datasets/{knowledge_base.ragflow_dataset_id}/documents/"
                f"{document.ragflow_document_id}/chunks",
                params={"page": query.page, "page_size": query.page_size},
            )
        )
        items = tuple(
            EngineChunk(
                engine_chunk_id=_string(item.get("id")),
                document_id=document.id,
                content=_string(item.get("content")),
                positions=_positions(item.get("positions")),
                metadata=_dict(item.get("metadata")) if item.get("metadata") is not None else {},
            )
            for item in (_dict(raw) for raw in _list(data.get("chunks")))
        )
        return Page(
            items=items,
            page=query.page,
            page_size=query.page_size,
            total=_integer(data.get("total"), len(items)),
        )

    async def retrieve(self, query: EngineRetrievalQuery) -> EngineRetrievalResult:
        """执行 RAGFlow 检索，并通过本地映射过滤所有返回候选。

        调用前确认请求的每个知识库/文档都属于当前隔离范围；调用后再按外部 ID 批量
        反查本地文档。任何无法映射、知识库与文档不一致的块都丢弃，稳定 chunk_id 由
        本地文档 UUID 与引擎块 ID 共同计算。
        """
        knowledge_bases = await self._knowledge_bases.get_many(
            query.security.security_scope_id,
            query.knowledge_base_ids,
        )
        if len(knowledge_bases) != len(set(query.knowledge_base_ids)) or any(
            not item.ragflow_dataset_id for item in knowledge_bases
        ):
            raise ResourceNotFoundError("RAG-KB-001", "知识库不存在")
        dataset_map = {
            item.ragflow_dataset_id: item
            for item in knowledge_bases
            if item.ragflow_dataset_id is not None
        }
        local_documents = await self._documents.get_many(
            query.security.security_scope_id,
            query.document_ids,
        )
        if len(local_documents) != len(set(query.document_ids)):
            raise ResourceNotFoundError("RAG-DOC-001", "文档不存在")
        engine_document_ids = [
            item.ragflow_document_id
            for item in local_documents
            if item.ragflow_document_id is not None
        ]
        data = _dict(
            await self._request(
                "POST",
                "/api/v1/retrieval",
                timeout_seconds=self._settings.retrieval_timeout_seconds,
                json={
                    "question": query.question,
                    "dataset_ids": list(dataset_map),
                    "document_ids": engine_document_ids,
                    "page": 1,
                    "page_size": query.page_size,
                    "similarity_threshold": query.similarity_threshold,
                    "vector_similarity_weight": query.vector_similarity_weight,
                    "top_k": query.top_k,
                    "keyword": False,
                    "highlight": False,
                    "metadata_condition": _ragflow_metadata_condition(query.metadata_filter),
                    "use_kg": False,
                    "toc_enhance": False,
                },
            )
        )
        raw_chunks = [_dict(item) for item in _list(data.get("chunks"))]
        candidate_engine_document_ids = [_string(item.get("document_id")) for item in raw_chunks]
        mapped_documents = await self._documents.get_by_engine_ids(
            query.security.security_scope_id,
            candidate_engine_document_ids,
        )
        document_map = {
            item.ragflow_document_id: item
            for item in mapped_documents
            if item.ragflow_document_id is not None
        }
        candidates: list[RetrievalCandidate] = []
        for item in raw_chunks:
            engine_chunk_id = _string(item.get("id"))
            engine_dataset_id = _string(item.get("dataset_id") or item.get("kb_id"))
            engine_document_id = _string(item.get("document_id"))
            knowledge_base = dataset_map.get(engine_dataset_id)
            document = document_map.get(engine_document_id)
            if (
                knowledge_base is None
                or document is None
                or document.knowledge_base_id != knowledge_base.id
            ):
                continue
            stable_chunk_id = hashlib.sha256(
                f"{document.id}\x1f{engine_chunk_id}".encode()
            ).hexdigest()
            candidates.append(
                RetrievalCandidate(
                    chunk_id=stable_chunk_id,
                    engine_chunk_id=engine_chunk_id,
                    knowledge_base_id=knowledge_base.id,
                    document_id=document.id,
                    # RAGFlow 为避免内部文件重名，可能把 ``document_keyword`` 改成
                    # “本地 UUID-原文件名”。这个引擎内部名称不能直接暴露给用户，
                    # 否则同一 Chunk 经基础路线和 Gateway 侧车路线返回时会显示成两个
                    # 不同文件。当前 Scope 下反查得到的 DocumentRecord 才是稳定事实，
                    # 因此始终使用上传时保存的原始文件名。
                    document_name=document.filename,
                    content=_string(item.get("content")),
                    score=_number(item.get("similarity")),
                    resource_scope_id=query.security.security_scope_id,
                    vector_score=(
                        _number(item.get("vector_similarity"))
                        if item.get("vector_similarity") is not None
                        else None
                    ),
                    term_score=(
                        _number(item.get("term_similarity"))
                        if item.get("term_similarity") is not None
                        else None
                    ),
                    positions=_positions(item.get("positions")),
                    metadata=(
                        _dict(item.get("metadata")) if item.get("metadata") is not None else {}
                    ),
                )
            )
        return EngineRetrievalResult(
            candidates=tuple(candidates),
            total=_integer(data.get("total"), len(candidates)),
        )

    async def health(self) -> EngineHealth:
        """直接调用无 Envelope 的健康接口，并标准化子依赖状态。"""
        try:
            response = await self._client.get(
                "/api/v1/system/healthz",
                timeout=self._settings.read_timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise RagFlowError.timeout() from exc
        except httpx.RequestError as exc:
            raise RagFlowError.connection() from exc
        if response.status_code == 401:
            raise RagFlowError.unauthorized()
        if not response.is_success:
            raise RagFlowError.downstream(retryable=response.status_code in _RETRY_STATUSES)
        try:
            data = _dict(response.json())
        except ValueError as exc:
            raise RagFlowError.invalid_response() from exc
        status = _string(data.get("status"))
        dependencies = {key: _string(value) for key, value in data.items() if key != "status"}
        return EngineHealth(status=status, dependencies=dependencies)
