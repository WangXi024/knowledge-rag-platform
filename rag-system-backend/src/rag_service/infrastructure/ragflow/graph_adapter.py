"""RAGFlow v0.26.4 原生 GraphRAG 的独立公共 HTTP 适配器。

本模块先验证本地范围映射，并在构图、导航前比较引擎全部物理文档与本地活动文档。
该版本上游图谱忽略文档过滤，因此出现候选、退役或未就绪文档时必须保守拒绝。
生成式 KG 聚合只拆成有限导航词，绝不转换为可引用的原始文档 Chunk。
"""

from __future__ import annotations

import csv
import json
import math
import re
import unicodedata
from dataclasses import dataclass
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
_GRAPH_NODE_TRUNCATION_LIMIT = 256
_GRAPH_EDGE_TRUNCATION_LIMIT = 128
_MAX_STRUCTURAL_TERMS = 8
_MAX_GRAPH_RESPONSE_BYTES = 4 * 1024 * 1024
_GRAPH_SECTIONS = re.compile(r"^---- (Entities|Relations|Community Report) ----\s*$", re.M)


def _object(value: Any) -> dict[str, Any]:
    """要求上游 JSON 为对象；错误只暴露稳定协议码，不回显响应或模型凭据。"""
    if not isinstance(value, dict):
        raise RagFlowError.invalid_response()
    return value


def _response_data(value: Any) -> Any:
    """统一成功 Envelope 的业务码校验，不回显上游失败响应或诊断正文。"""
    envelope = _object(value)
    if isinstance(envelope.get("code"), bool) or envelope.get("code") != 0:
        raise RagFlowError.downstream()
    return envelope.get("data")


def _successful_response(response: httpx.Response) -> None:
    """在读响应体前映射 HTTP 错误；大错误页不能绕过只读图谱的资源上限。"""
    if response.status_code == 401:
        raise RagFlowError.unauthorized()
    if not response.is_success:
        raise RagFlowError.downstream(retryable=response.status_code in {429, 502, 503, 504})


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


def _match_text(value: str) -> str:
    """统一实体匹配的兼容字符、大小写及空白；不解释图谱中的任何描述指令。"""
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(
        "".join(
            " " if unicodedata.category(character) in {"Cc", "Cf"} else character
            for character in normalized
        ).split()
    )


def _entity_label(value: Any) -> tuple[str, str]:
    """校验原生实体名称并返回显示值与规范匹配值，不从描述补造实体名。"""
    label = _term(value)
    if label is None or any(unicodedata.category(character) in {"Cc", "Cf"} for character in label):
        raise RagFlowError.invalid_response()
    normalized = _match_text(label)
    if not normalized or len(normalized) > 160 or not any(char.isalnum() for char in normalized):
        raise RagFlowError.invalid_response()
    return label, normalized


def _entity_matches(question: str, entity: str) -> bool:
    """匹配完整实体短语；ASCII 使用词边界，中文名称允许自然句中的连续子串。

    词边界只约束 ASCII 字母、数字和下划线，使 ATLAS 不会命中 ATLASES，同时
    “Atlas项目”这样的中英文混排仍能正确命中；正则元字符一律作为名称字面量。
    """
    ascii_word = "abcdefghijklmnopqrstuvwxyz0123456789_"
    prefix = r"(?<![A-Za-z0-9_])" if entity[0] in ascii_word else ""
    suffix = r"(?![A-Za-z0-9_])" if entity[-1] in ascii_word else ""
    return re.search(f"{prefix}{re.escape(entity)}{suffix}", question) is not None


@dataclass(frozen=True, slots=True)
class _NativeGraphNode:
    """已校验的原生实体；只保留名称及活动来源，不保存生成描述。"""

    label: str
    normalized_name: str
    source_ids: frozenset[str]


@dataclass(frozen=True, slots=True)
class _NativeGraphStructure:
    """完整验证之后的有限图，供发布快照和在线导航共享相同安全边界。"""

    nodes: dict[str, _NativeGraphNode]
    adjacent: dict[str, set[str]]
    relation_count: int


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
            if method == "GET" and path.endswith("/graph"):
                return _response_data(await self._read_graph_response(path, **kwargs))
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
        _successful_response(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise RagFlowError.invalid_response() from exc
        return _response_data(payload)

    async def _read_graph_response(self, path: str, **kwargs: Any) -> Any:
        """只读图谱采用流式读取，JSON 解析前最多接收 4 MiB，不全量 aread。

        已声明的大 Content-Length 在迭代之前拒绝；未知长度或分块传输按解码后字节
        累计限额，越限立即关闭响应。只读图谱显式请求 identity 编码，正常小响应与
        其他 API 的 HTTP、超时和 Envelope 错误契约保持一致。
        """
        async with self._client.stream(
            "GET",
            path,
            headers={
                "Authorization": f"Bearer {self._settings.api_key}",
                "Accept-Encoding": "identity",
            },
            timeout=self._settings.read_timeout_seconds,
            **kwargs,
        ) as response:
            _successful_response(response)
            content_length = response.headers.get("Content-Length")
            if content_length is not None:
                try:
                    declared_length = int(content_length)
                except ValueError as exc:
                    raise RagFlowError.invalid_response() from exc
                if not 0 <= declared_length <= _MAX_GRAPH_RESPONSE_BYTES:
                    raise RagFlowError.invalid_response()
            body = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=65_536):
                if len(body) + len(chunk) > _MAX_GRAPH_RESPONSE_BYTES:
                    raise RagFlowError.invalid_response()
                body.extend(chunk)
            try:
                return json.loads(body)
            except ValueError as exc:
                raise RagFlowError.invalid_response() from exc

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
        """生成不泄漏其他版本 ID 的稳定来源冲突，不能把旧图产物当成当前活动图。"""
        return RagError(
            "RAG-GRAPH-004",
            "图谱来源必须全部属于当前已就绪活动文档，且物理文档集合必须一致，请完成摄取或清理旧图后重建",
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

    async def _read_validated_graph(self, knowledge_base: KnowledgeBase) -> _NativeGraphStructure:
        """核验物理集合并读取完整受限图；快照发布和导航不能采用不同的来源规则。

        v0.26.4 的重建可能复用旧合并检查点：当前物理文档已删干净、任务进度为 1，
        并不证明图节点、边或全图元信息没有旧来源。必须逐项验证 source_id，任何
        删除/跨范围来源都明确拒绝，不能只统计节点后让 Worker 发布 READY。
        """
        active_ids = await self._active_document_ids(knowledge_base)
        # 官方 v0.26.4 路由没有读取 max_nodes 查询参数，不发送未经支持的假限额。
        data = _object(await self._request("GET", f"{self._dataset_path(knowledge_base)}/graph"))
        graph = _object(data.get("graph", {}))
        nodes, edges = graph.get("nodes", []), graph.get("edges", [])
        if (
            not isinstance(nodes, list)
            or not isinstance(edges, list)
            or len(nodes) >= _GRAPH_NODE_TRUNCATION_LIMIT
            or len(edges) >= _GRAPH_EDGE_TRUNCATION_LIMIT
        ):
            # 官方服务已裁剪为 nodes[:256]、edges[:128]，且没有完整性标志。达到
            # 裁剪界限就无法证明尾部不存在旧来源；不能把恰好饱和的响应当作完整图。
            raise RagFlowError.invalid_response()

        def source_ids(value: Any, *, allow_empty: bool = False) -> frozenset[str]:
            """验证原生 source_id 文档数组；只有可选全图元信息允许空来源集合。"""
            if not isinstance(value, list) or (not value and not allow_empty):
                raise RagFlowError.invalid_response()
            identifiers = frozenset(_identifier(identifier) for identifier in value)
            if not identifiers <= active_ids:
                raise self._boundary_error()
            return identifiers

        if "graph" in graph:
            attributes = _object(graph["graph"])
            if "source_id" in attributes:
                source_ids(attributes["source_id"], allow_empty=True)

        validated: dict[str, _NativeGraphNode] = {}
        normalized_names: set[str] = set()
        for value in nodes:
            node = _object(value)
            identifier, normalized_id = _entity_label(node.get("id"))
            label, normalized_name = _entity_label(node.get("entity_name"))
            if (
                normalized_id != normalized_name
                or identifier in validated
                or normalized_name in normalized_names
            ):
                raise RagFlowError.invalid_response()
            validated[identifier] = _NativeGraphNode(
                label, normalized_name, source_ids(node.get("source_id"))
            )
            normalized_names.add(normalized_name)

        adjacent: dict[str, set[str]] = {identifier: set() for identifier in validated}
        for value in edges:
            edge = _object(value)
            source, target = _identifier(edge.get("source")), _identifier(edge.get("target"))
            if source not in validated or target not in validated:
                raise RagFlowError.invalid_response()
            source_ids(edge.get("source_id"))
            # 连接仅用于找相邻实体，不把边描述或方向解释为事实声明。
            adjacent[source].add(target)
            adjacent[target].add(source)
        return _NativeGraphStructure(validated, adjacent, len(edges))

    async def configure(
        self,
        security_scope_id: str,
        knowledge_base_id: UUID,
        graph_generation: int,
        configuration: GraphConfiguration,
        *,
        previous_task_id: str | None,
    ) -> None:
        """按已持久准备意图清理旧终态图，再合并受控配置，不触发收费构图。

        previous_task_id 来自 Worker 清理之前已经提交的终态观察；已记录旧终态
        而任务现在为空时，可以幂等重做清理。None 则要求首次无任务且原生图为空。
        只调用官方图谱索引删除，保留 Dataset 文档、原始 Chunk 和通用模型缓存。
        """
        knowledge_base = await self._knowledge_base(
            security_scope_id, knowledge_base_id, graph_generation
        )
        if configuration.enabled:
            self._require_enabled()
        GraphProfile(configuration.profile_ref)
        if previous_task_id is not None:
            _identifier(previous_task_id)

        def require_prepared_task(observed: GraphTask | None) -> None:
            """确认任务身份；只有已记录终态意图的旧 ID 才允许在任务缺失时继续清理。"""
            if previous_task_id is None:
                if observed is not None:
                    raise RagError(
                        "RAG-GRAPH-007", "原生任务已变化，不能按首次构图准备继续", 409, False
                    )
            elif observed is not None:
                if observed.engine_task_id != previous_task_id:
                    raise RagError("RAG-GRAPH-007", "原生任务已被其他构建替换", 409, False)
                if observed.status not in {GraphStatus.READY, GraphStatus.FAILED}:
                    raise RagError("RAG-GRAPH-006", "旧原生任务仍在运行，不能清理", 409, False)

        observed = await self._observe_index_task(
            knowledge_base,
            graph_generation,
            verify_sources=False,
        )
        require_prepared_task(observed)
        if previous_task_id is None:
            existing_graph = await self._read_validated_graph(knowledge_base)
            if existing_graph.nodes or existing_graph.relation_count:
                raise RagError(
                    "RAG-GRAPH-006", "存在无可核验终态任务的旧图，不能按首次构图清理", 409, False
                )
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
        # 删除前紧邻再次观察身份/终态。上游没有任务条件删除或 CAS 参数，因此仍依赖
        # 本地唯一在途任务和有效租约；不能把任意新任务的终态借来确认旧任务停止。
        latest = await self._observe_index_task(
            knowledge_base,
            graph_generation,
            verify_sources=False,
        )
        require_prepared_task(latest)
        if previous_task_id is not None:
            await self._request(
                "DELETE",
                f"{self._dataset_path(knowledge_base)}/index",
                params={"type": "graph", "wipe": "true"},
            )
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
        return await self._observe_index_task(knowledge_base, graph_generation, verify_sources=True)

    async def _observe_index_task(
        self,
        knowledge_base: KnowledgeBase,
        graph_generation: int,
        *,
        verify_sources: bool,
    ) -> GraphTask | None:
        """统一读取原生任务；清理前只核终态身份，公开任务读取仍另核活动物理集合。

        清理的最后核对不能在 GET index 与 DELETE 之间插入文档请求。此处的终态只
        说明原生执行已结束，不替代 snapshot 对派生图来源的发布验收。
        """
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
            if verify_sources:
                await self._active_document_ids(knowledge_base)
            status = GraphStatus.READY
        elif progress == 0:
            status = GraphStatus.QUEUED
        return GraphTask(
            knowledge_base.security_scope_id,
            knowledge_base.id,
            graph_generation,
            _identifier(data.get("id", data.get("task_id"))),
            status,
            float(progress),
            "图谱构建失败" if status == GraphStatus.FAILED else None,
        )

    async def snapshot(
        self, security_scope_id: str, knowledge_base_id: UUID, graph_generation: int
    ) -> GraphSnapshot:
        """完整验证原生图所有来源后返回有限统计；删除来源或无效结构禁止发布。"""
        knowledge_base = await self._knowledge_base(
            security_scope_id, knowledge_base_id, graph_generation
        )
        graph = await self._read_validated_graph(knowledge_base)
        return GraphSnapshot(
            security_scope_id,
            knowledge_base_id,
            graph_generation,
            tuple(node.label for node in graph.nodes.values()),
            len(graph.nodes),
            graph.relation_count,
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
        """读取真实原生图结构，以精确实体及一跳邻居导航到普通原文检索。

        v0.26.4 的原生 use_kg 检索已实测不能可靠返回 KG 聚合，在线默认使用
        官方只读 graph 接口；这不代表 use_kg 成功，也不调用额外图谱查询模型。
        所有节点和边先验证活动来源，再对实体名做确定性匹配。描述、实体类型和
        关系摘要均不作为回答证据或扩展词，最终引用仍须重新检索原始文档块。
        """
        knowledge_base = await self._knowledge_base(
            security_scope_id, knowledge_base_id, graph_generation
        )
        self._require_enabled()
        if (
            not question.strip()
            or isinstance(limit, bool)
            or not 1 <= limit <= _MAX_STRUCTURAL_TERMS
        ):
            raise ValueError("question must be non-empty and structural limit must be within 1..8")
        graph = await self._read_validated_graph(knowledge_base)
        validated = graph.nodes
        normalized_question = _match_text(question)
        seeds = sorted(
            (
                identifier
                for identifier, node in validated.items()
                if _entity_matches(normalized_question, node.normalized_name)
            ),
            key=lambda identifier: (
                -len(validated[identifier].normalized_name),
                validated[identifier].normalized_name,
            ),
        )
        # 只从原问题真正命中的实体出发；无命中不能用高 Pagerank 节点伪装语义命中。
        seed_set = set(seeds)
        neighbors = sorted(
            {neighbor for seed in seeds for neighbor in graph.adjacent[seed]} - seed_set,
            key=lambda identifier: validated[identifier].normalized_name,
        )
        selected = (*seeds, *neighbors)[:limit]
        entities = tuple(validated[identifier].label for identifier in selected)
        document_ids = sorted(
            {
                document_id
                for identifier in selected
                for document_id in validated[identifier].source_ids
            }
        )[:limit]
        return GraphNavigation(
            security_scope_id,
            knowledge_base_id,
            graph_generation,
            entities,
            entities,
            tuple(GraphSourceReference(document_id) for document_id in document_ids),
        )

    async def probe_kg_navigation(
        self,
        security_scope_id: str,
        knowledge_base_id: UUID,
        graph_generation: int,
        question: str,
        *,
        limit: int = 8,
    ) -> GraphNavigation:
        """仅供诊断官方 use_kg 路径；在线默认导航不调用本方法或等待查询模型。

        保留真实 KG 聚合的 CSV 协议验证，便于独立定位上游修复；没有聚合时返回空
        导航，不伪装 use_kg 已经成功，也不把无文档 ID 的摘要映射为原始 Chunk。
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
