"""RAGFlow v0.26.4 原生 GraphRAG 的独立公共 HTTP 适配器。

本模块先验证本地范围映射，并在构图、导航前比较引擎全部物理文档与本地活动文档。
该版本上游图谱忽略文档过滤，因此出现候选、退役或未就绪文档时必须保守拒绝。
生成式 KG 聚合只拆成有限导航词，绝不转换为可引用的原始文档 Chunk。
"""

from __future__ import annotations

import csv
import math
import re
from http import HTTPStatus
from io import StringIO
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

from rag_service.domain.errors import RagError, RagFlowError, ResourceNotFoundError
from rag_service.domain.graph import (
    GraphConfiguration,
    GraphNavigation,
    GraphProfile,
    GraphSnapshot,
    GraphSourceReference,
    GraphStatus,
    GraphTask,
    native_graph_parser_config,
)
from rag_service.domain.models import DocumentStatus, KnowledgeBase
from rag_service.domain.ports import DocumentRepository, KnowledgeBaseRepository
from rag_service.settings import RagFlowSection

_GRAPH_TITLE = "Related content in Knowledge Graph"
_PAGE_SIZE = 100
_MAX_DOCUMENTS = 100_000
_MAX_NAVIGATION_TERMS = 32
_GRAPH_SECTIONS = re.compile(r"^---- (Entities|Relations|Community Report) ----\s*$", re.M)


def _object(value: Any) -> dict[str, Any]:
    """要求上游 JSON 为对象；错误只暴露稳定协议码，不回显响应或模型凭据。"""
    if not isinstance(value, dict):
        raise RagFlowError.invalid_response()
    return value


def _identifier(value: Any) -> str:
    """读取有限非空引擎标识；拒绝控制字符，路径使用前还会单独编码。"""
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 512
        or any(ord(character) < 32 for character in value)
    ):
        raise RagFlowError.invalid_response()
    return value


def _total(value: Any) -> int:
    """读取分页总数并设置资源上限，避免异常协议造成无限循环或无界内存。"""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_DOCUMENTS:
        raise RagFlowError.invalid_response()
    return int(value)


def _term(value: Any) -> str | None:
    """从不可信图谱内容读取单个短导航词，丢弃多行及控制字符。"""
    if not isinstance(value, str):
        return None
    result = value.strip()
    if not result or len(result) > 160 or any(ord(character) < 32 for character in result):
        return None
    return result


def _graph_terms(content: Any, limit: int) -> tuple[str, ...]:
    """只解析官方 KG 聚合中的实体/关系 CSV 名称；描述与社区报告不参与扩展。"""
    if not isinstance(content, str) or len(content) > 262_144:
        return ()
    parts = _GRAPH_SECTIONS.split(content)
    terms: dict[str, None] = {}
    for index in range(1, len(parts) - 1, 2):
        section, body = parts[index], parts[index + 1]
        if section == "Community Report":
            continue
        columns = ("Entity",) if section == "Entities" else ("From Entity", "To Entity")
        try:
            rows = csv.DictReader(StringIO(body.strip()))
            for row_index, row in enumerate(rows):
                if row_index >= 256:
                    break
                for column in columns:
                    value = _term(row.get(column))
                    if value is not None:
                        terms.setdefault(value, None)
                        if len(terms) >= limit:
                            return tuple(terms)
        except csv.Error:
            # CSV 异常只意味着不能可靠提取导航，不能把整段摘要当作替代关键词。
            continue
    return tuple(terms)


class RagFlowGraphAdapter:
    """实现隔离的原生图谱端口；客户端支持注入 MockTransport 做无网络契约验收。"""

    def __init__(
        self,
        settings: RagFlowSection,
        knowledge_bases: KnowledgeBaseRepository,
        documents: DocumentRepository,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """接收本地映射仓库和非敏感模型配置；只关闭自行创建的 HTTP 客户端。"""
        self._settings = settings
        self._knowledge_bases = knowledge_bases
        self._documents = documents
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=settings.base_url.rstrip("/"),
            timeout=httpx.Timeout(
                connect=settings.connect_timeout_seconds,
                read=settings.read_timeout_seconds,
                write=settings.read_timeout_seconds,
                pool=settings.connect_timeout_seconds,
            ),
        )

    async def close(self) -> None:
        """释放自有连接池；借入的客户端生命周期仍由调用方负责。"""
        if self._owns_client:
            await self._client.aclose()

    def _require_enabled(self) -> None:
        """全局开关关闭时明确拒绝构图或导航，禁止隐式触发云端模型调用。"""
        if not self._settings.graph_enabled:
            raise RagError(
                "RAG-GRAPH-001", "原生图谱功能未启用", HTTPStatus.SERVICE_UNAVAILABLE, False
            )

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """统一 Envelope、超时和网络错误；不自动重试，避免重复提交构图任务。

        每次请求显式提供本适配器的授权与时限，不修改借入客户端的全局 Header。
        Python 的取消异常保持传播，以便 Worker 安全停止轮询。
        """
        try:
            response = await self._client.request(
                method,
                path,
                headers={"Authorization": f"Bearer {self._settings.api_key}"},
                timeout=(
                    self._settings.retrieval_timeout_seconds
                    if path == "/api/v1/retrieval"
                    else self._settings.read_timeout_seconds
                ),
                **kwargs,
            )
        except httpx.TimeoutException as exc:
            raise RagFlowError.timeout() from exc
        except httpx.RequestError as exc:
            raise RagFlowError.connection() from exc
        if response.status_code == 401:
            raise RagFlowError.unauthorized()
        if not response.is_success:
            raise RagFlowError.downstream(retryable=response.status_code in {429, 502, 503, 504})
        try:
            envelope = _object(response.json())
        except ValueError as exc:
            raise RagFlowError.invalid_response() from exc
        if isinstance(envelope.get("code"), bool) or envelope.get("code") != 0:
            raise RagFlowError.downstream()
        return envelope.get("data")

    async def _knowledge_base(
        self, security_scope_id: str, knowledge_base_id: UUID, graph_generation: int
    ) -> KnowledgeBase:
        """按范围查询映射并防御仓库错误返回；不接受调用方直接传外部 Dataset ID。"""
        knowledge_base = await self._knowledge_bases.get(security_scope_id, knowledge_base_id)
        if (
            knowledge_base is None
            or knowledge_base.security_scope_id != security_scope_id
            or knowledge_base.id != knowledge_base_id
        ):
            raise ResourceNotFoundError("RAG-KB-001", "知识库不存在")
        if (
            isinstance(graph_generation, bool)
            or not isinstance(graph_generation, int)
            or graph_generation < 0
        ):
            raise ValueError("graph generation must be a non-negative integer")
        _identifier(knowledge_base.ragflow_dataset_id)
        return knowledge_base

    @staticmethod
    def _dataset_path(knowledge_base: KnowledgeBase) -> str:
        """对已校验的引擎映射进行路径编码，避免路径字符改变操作目标。"""
        return f"/api/v1/datasets/{quote(_identifier(knowledge_base.ragflow_dataset_id), safe='')}"

    @staticmethod
    def _boundary_error() -> RagError:
        """生成不泄漏其他版本 ID 的稳定冲突，调用方须清理物理文档后重建。"""
        return RagError(
            "RAG-GRAPH-004",
            "图谱要求全部物理文档与当前已就绪活动文档严格一致，请完成摄取或清理旧版本后重建",
            HTTPStatus.CONFLICT,
            False,
        )

    async def _active_document_ids(self, knowledge_base: KnowledgeBase) -> frozenset[str]:
        """比较本地活动主记录与全部上游物理文档；任何候选/旧版/缺失都阻断图谱。

        不按 READY 预过滤本地查询，否则未完成的新文档会被隐藏，造成旧图继续开放。
        两端分页均校验总数、重复项和归属，不能只验证第一页或 disabled 标志。
        """
        local_ids: set[str] = set()
        local_count = 0
        local_total: int | None = None
        for page in range(1, _MAX_DOCUMENTS // _PAGE_SIZE + 2):
            items, count = await self._documents.list(
                knowledge_base.security_scope_id,
                knowledge_base.id,
                page=page,
                page_size=_PAGE_SIZE,
            )
            count = _total(count)
            if local_total is not None and count != local_total:
                raise self._boundary_error()
            local_total = count
            for document in items:
                if (
                    document.security_scope_id != knowledge_base.security_scope_id
                    or document.knowledge_base_id != knowledge_base.id
                    or document.status != DocumentStatus.READY
                    or not document.ragflow_document_id
                    or document.ragflow_document_id in local_ids
                ):
                    raise self._boundary_error()
                local_ids.add(_identifier(document.ragflow_document_id))
            local_count += len(items)
            if local_count == count:
                break
            if not items or local_count > count:
                raise self._boundary_error()
        else:
            raise self._boundary_error()

        engine_ids: set[str] = set()
        engine_total: int | None = None
        for page in range(1, _MAX_DOCUMENTS // _PAGE_SIZE + 2):
            data = _object(
                await self._request(
                    "GET",
                    f"{self._dataset_path(knowledge_base)}/documents",
                    params={"page": page, "page_size": _PAGE_SIZE},
                )
            )
            docs = data.get("docs")
            if not isinstance(docs, list):
                raise RagFlowError.invalid_response()
            count = _total(data.get("total"))
            if engine_total is not None and count != engine_total:
                raise self._boundary_error()
            engine_total = count
            for value in docs:
                engine_document = _object(value)
                engine_id = _identifier(engine_document.get("id"))
                dataset_id = engine_document.get("dataset_id", engine_document.get("kb_id"))
                if engine_id in engine_ids or (
                    dataset_id is not None and dataset_id != knowledge_base.ragflow_dataset_id
                ):
                    raise self._boundary_error()
                engine_ids.add(engine_id)
            if len(engine_ids) == count:
                break
            if not docs or len(engine_ids) > count:
                raise self._boundary_error()
        else:
            raise self._boundary_error()
        if local_ids != engine_ids:
            raise self._boundary_error()
        return frozenset(local_ids)

    async def configure(
        self,
        security_scope_id: str,
        knowledge_base_id: UUID,
        graph_generation: int,
        configuration: GraphConfiguration,
    ) -> None:
        """读取现有 Dataset 并仅替换受控图谱子配置；不触发索引，不覆盖解析字段。"""
        knowledge_base = await self._knowledge_base(
            security_scope_id, knowledge_base_id, graph_generation
        )
        if configuration.enabled:
            self._require_enabled()
        GraphProfile(configuration.profile_ref)
        data = await self._request(
            "GET", "/api/v1/datasets", params={"id": knowledge_base.ragflow_dataset_id}
        )
        values = data if isinstance(data, list) else _object(data).get("docs")
        if not isinstance(values, list) or len(values) != 1:
            raise RagFlowError.invalid_response()
        dataset = _object(values[0])
        if dataset.get("id") != knowledge_base.ragflow_dataset_id:
            raise RagFlowError.invalid_response()
        parser_config = dict(_object(dataset.get("parser_config")))
        # light@1 的语义固定；不继承外部遗留的 general/community 或任意上游参数。
        parser_config["graphrag"] = native_graph_parser_config(configuration)
        # 沿用官方顶层 ext 扩展：公开 Schema 只验证 graph 子配置，完整回读配置由
        # 服务层在校验后合并保存。这样 overlapped_percent 等已有字段不会被拒绝或丢失。
        await self._request(
            "PUT",
            self._dataset_path(knowledge_base),
            json={
                "parser_config": {"graphrag": parser_config["graphrag"]},
                "ext": {"parser_config": parser_config},
            },
        )

    async def build(
        self, security_scope_id: str, knowledge_base_id: UUID, graph_generation: int
    ) -> GraphTask:
        """核验物理版本边界后提交一次构图；只有真实返回任务 ID 才报告排队成功。"""
        knowledge_base = await self._knowledge_base(
            security_scope_id, knowledge_base_id, graph_generation
        )
        self._require_enabled()
        if not await self._active_document_ids(knowledge_base):
            raise self._boundary_error()
        data = _object(
            await self._request(
                "POST", f"{self._dataset_path(knowledge_base)}/index", params={"type": "graph"}
            )
        )
        return GraphTask(
            security_scope_id,
            knowledge_base_id,
            graph_generation,
            _identifier(data.get("task_id")),
            GraphStatus.QUEUED,
        )

    async def task(
        self, security_scope_id: str, knowledge_base_id: UUID, graph_generation: int
    ) -> GraphTask | None:
        """读取原生任务进度；空对象表示尚无任务，异常数值不能转换成 READY。"""
        knowledge_base = await self._knowledge_base(
            security_scope_id, knowledge_base_id, graph_generation
        )
        data = _object(
            await self._request(
                "GET", f"{self._dataset_path(knowledge_base)}/index", params={"type": "graph"}
            )
        )
        if not data:
            return None
        progress = data.get("progress")
        if (
            isinstance(progress, bool)
            or not isinstance(progress, int | float)
            or not math.isfinite(progress)
            or (progress != -1 and not 0 <= progress <= 1)
        ):
            raise RagFlowError.invalid_response()
        status = GraphStatus.BUILDING
        if progress == -1:
            status = GraphStatus.FAILED
        elif progress == 1:
            # 完成后再次核验，避免候选上传或旧版残留期间把派生索引公开为 READY。
            await self._active_document_ids(knowledge_base)
            status = GraphStatus.READY
        elif progress == 0:
            status = GraphStatus.QUEUED
        return GraphTask(
            security_scope_id,
            knowledge_base_id,
            graph_generation,
            _identifier(data.get("id", data.get("task_id"))),
            status,
            float(progress),
            "图谱构建失败" if status == GraphStatus.FAILED else None,
        )

    async def snapshot(
        self, security_scope_id: str, knowledge_base_id: UUID, graph_generation: int
    ) -> GraphSnapshot:
        """读取官方有限图结构并只返回统计与实体名称；空图不算协议异常。"""
        knowledge_base = await self._knowledge_base(
            security_scope_id, knowledge_base_id, graph_generation
        )
        await self._active_document_ids(knowledge_base)
        data = _object(await self._request("GET", f"{self._dataset_path(knowledge_base)}/graph"))
        graph = _object(data.get("graph", {}))
        nodes, edges = graph.get("nodes", []), graph.get("edges", [])
        if not isinstance(nodes, list) or not isinstance(edges, list):
            raise RagFlowError.invalid_response()
        names = tuple(
            dict.fromkeys(
                name
                for node in nodes[:256]
                if isinstance(node, dict) and (name := _term(node.get("id"))) is not None
            )
        )
        return GraphSnapshot(
            security_scope_id,
            knowledge_base_id,
            graph_generation,
            names,
            len(nodes),
            len(edges),
        )

    async def cancel(
        self, security_scope_id: str, knowledge_base_id: UUID, graph_generation: int
    ) -> None:
        """仅取消运行任务并保留图谱进度；显式 wipe=false，绝不调用删除图谱接口。"""
        knowledge_base = await self._knowledge_base(
            security_scope_id, knowledge_base_id, graph_generation
        )
        await self._request(
            "DELETE",
            f"{self._dataset_path(knowledge_base)}/index",
            params={"type": "graph", "wipe": "false"},
        )

    async def navigate(
        self,
        security_scope_id: str,
        knowledge_base_id: UUID,
        graph_generation: int,
        question: str,
        *,
        limit: int = 8,
    ) -> GraphNavigation:
        """把真实 KG 聚合分流为导航词；普通块只留已活动文档的可核验来源指针。

        本端口故意没有文档或元数据过滤参数，上层遇到此类限制必须跳过图谱。
        无文档 ID 的未知块直接忽略，不触发普通候选映射或生成虚假引用。
        """
        knowledge_base = await self._knowledge_base(
            security_scope_id, knowledge_base_id, graph_generation
        )
        self._require_enabled()
        if not question.strip() or not 1 <= limit <= _MAX_NAVIGATION_TERMS:
            raise ValueError("question must be non-empty and navigation limit must be within 1..32")
        active_ids = await self._active_document_ids(knowledge_base)
        data = _object(
            await self._request(
                "POST",
                "/api/v1/retrieval",
                json={
                    "question": question,
                    "dataset_ids": [knowledge_base.ragflow_dataset_id],
                    "use_kg": True,
                    "page": 1,
                    "page_size": limit,
                    "top_k": limit,
                    "highlight": False,
                },
            )
        )
        chunks = data.get("chunks")
        if not isinstance(chunks, list):
            raise RagFlowError.invalid_response()
        entities: dict[str, None] = {}
        sources: dict[GraphSourceReference, None] = {}
        for value in chunks[:256]:
            chunk = _object(value)
            datasets = chunk.get("dataset_id", chunk.get("kb_id"))
            if isinstance(datasets, str):
                datasets = [datasets]
            # KG 的 dataset_id 是列表；必须恰好属于本次唯一目标，拒绝跨库聚合。
            if datasets != [knowledge_base.ragflow_dataset_id]:
                continue
            document_id = chunk.get("document_id", chunk.get("doc_id"))
            if not document_id:
                title = chunk.get("document_keyword", chunk.get("docnm_kwd"))
                if title == _GRAPH_TITLE:
                    content = chunk.get("content", chunk.get("content_with_weight"))
                    for entity in _graph_terms(content, limit):
                        if len(entities) < limit:
                            entities.setdefault(entity, None)
                continue
            if not isinstance(document_id, str) or document_id not in active_ids:
                continue
            chunk_id = chunk.get("id", chunk.get("chunk_id"))
            if chunk_id is not None:
                chunk_id = _identifier(chunk_id)
            if len(sources) < limit:
                sources.setdefault(GraphSourceReference(document_id, chunk_id), None)
        return GraphNavigation(
            security_scope_id,
            knowledge_base_id,
            graph_generation,
            tuple(entities),
            tuple(entities),
            tuple(sources),
        )
