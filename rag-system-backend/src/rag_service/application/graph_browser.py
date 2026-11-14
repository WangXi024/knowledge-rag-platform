"""有界只读图谱浏览及聚合报告的安全桥接。

管理端浏览和在线社区问答共用当前图版本、活动文档及原文复验；所有原生 ID 均在
Adapter 完成本地映射后使用。读取不会调用模型、调度构图或修复旧报告。
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, replace
from uuid import UUID

from rag_service.application.access import (
    KnowledgeBaseAccessPolicy,
    ManagementAccessPolicy,
    SharedKnowledgeBaseAccessPolicy,
    TrustedNetworkManagementAccessPolicy,
)
from rag_service.domain.aggregate import AggregateEvidence, AggregateSource
from rag_service.domain.errors import RagError, ResourceNotFoundError
from rag_service.domain.graph import GraphRepository, GraphState, GraphStatus
from rag_service.domain.graph_read import GraphReadPort, NativeGraphData
from rag_service.domain.models import KnowledgeBase, KnowledgeBaseStatus
from rag_service.domain.ports import KnowledgeBaseRepository
from rag_service.security.context import SecurityContext

SUMMARY_GRAPH_PROFILE = "native-graph-with-summaries@1"


def public_graph_id(kb_id: UUID, generation: int, kind: str, identifier: str) -> str:
    """生成仅在本知识库/图代数内稳定的公开 ID，不接受客户端直接提交引擎 ID。"""
    return hashlib.sha256(
        f"{kb_id}\x1f{generation}\x1f{kind}\x1f{identifier}".encode()
    ).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class GraphViewNode:
    """实体的公开名称与生成描述；来源独立保留，描述不充当原文 quote。"""

    id: str
    name: str
    entity_type: str
    description: str
    sources: tuple[AggregateSource, ...] = ()


@dataclass(frozen=True, slots=True)
class GraphViewEdge:
    """连接已返回节点的有限关系；端点 ID 与同次返回的实体 ID 一致。"""

    id: str
    source: str
    target: str
    description: str
    sources: tuple[AggregateSource, ...] = ()


@dataclass(frozen=True, slots=True)
class GraphView:
    """一次版本固定的浏览结果；未就绪时只返回状态和空结构。"""

    knowledge_base_id: UUID
    status: GraphStatus
    graph_generation: int
    content_revision: int
    profile_ref: str
    nodes: tuple[GraphViewNode, ...] = ()
    edges: tuple[GraphViewEdge, ...] = ()
    truncated: bool = False
    warnings: tuple[str, ...] = ()


class GraphBrowserService:
    """请求级无状态服务，复用访问策略与有界原生读取，不跨请求保存敏感图正文。"""

    def __init__(
        self,
        knowledge_bases: KnowledgeBaseRepository,
        graphs: GraphRepository,
        reader: GraphReadPort,
        *,
        enabled: bool,
        access_policy: KnowledgeBaseAccessPolicy | None = None,
        management_access: ManagementAccessPolicy | None = None,
    ) -> None:
        """注入同一请求事务的仓库；在线桥接不经过管理员入口但仍执行资源授权。"""
        self._kbs = knowledge_bases
        self._graphs = graphs
        self._reader = reader
        self._enabled = enabled
        self._access = access_policy or SharedKnowledgeBaseAccessPolicy()
        self._management = management_access or TrustedNetworkManagementAccessPolicy()

    async def _resource(
        self,
        security: SecurityContext,
        kb_id: UUID,
    ) -> tuple[KnowledgeBase, GraphState]:
        """先完成可读资源判断，跨范围/删除与不存在均使用相同 404。"""
        kb = await self._kbs.get_accessible(self._access.candidate_resource_scopes(security), kb_id)
        if (
            kb is None
            or kb.status != KnowledgeBaseStatus.READY
            or not await self._access.filter_readable(security, (kb,))
        ):
            raise ResourceNotFoundError("RAG-KB-001", "知识库不存在")
        state = await self._graphs.get(kb.security_scope_id, kb.id)
        if state is None:
            state = GraphState(kb.security_scope_id, kb.id)
        if state.security_scope_id != kb.security_scope_id or state.knowledge_base_id != kb.id:
            raise ResourceNotFoundError("RAG-KB-001", "知识库不存在")
        if not self._enabled or not state.configuration.enabled:
            state = replace(state, status=GraphStatus.DISABLED)
        elif state.status == GraphStatus.READY and state.built_for_revision != kb.revision:
            state = replace(state, status=GraphStatus.STALE)
        return kb, state

    @staticmethod
    def _generation(state: GraphState, expected: int | None) -> None:
        """浏览器展开必须沿同一图代数；重建后拒绝拼接旧邻域。"""
        if expected is not None and expected != state.graph_generation:
            raise RagError("RAG-GRAPH-020", "图谱版本已变化，请重新加载", 409, False)

    async def _unchanged(
        self, security: SecurityContext, kb: KnowledgeBase, state: GraphState
    ) -> bool:
        """外部读取后再次检查内容与行版本，配置/内容中途变化时不返回旧正文。"""
        fresh_kb, fresh = await self._resource(security, kb.id)
        return (
            fresh_kb.revision == kb.revision
            and fresh.status == GraphStatus.READY
            and fresh.row_version == state.row_version
            and fresh.graph_generation == state.graph_generation
        )

    async def view(
        self,
        security: SecurityContext,
        kb_id: UUID,
        *,
        query: str = "",
        node_id: str | None = None,
        depth: int = 1,
        limit: int = 40,
        generation: int | None = None,
    ) -> GraphView:
        """在总计八秒预算内搜索或展开最多两跳、四十节点的邻域；读取失败无旧图兜底。"""
        await self._management.require_access(security, "knowledge_base.graph.read")
        if not 0 <= depth <= 2 or not 1 <= limit <= 40 or len(query) > 100:
            raise RagError("RAG-COMMON-001", "图谱浏览参数无效", 400, False)
        async with asyncio.timeout(8):
            kb, state = await self._resource(security, kb_id)
            self._generation(state, generation)
            base = GraphView(
                kb.id,
                state.status,
                state.graph_generation,
                kb.revision,
                state.configuration.profile_ref,
            )
            if state.status != GraphStatus.READY:
                return base
            graph = await self._reader.read_graph(
                kb.security_scope_id, kb.id, state.graph_generation
            )
            self._graph_identity(graph, kb, state)
            result = await self._select(
                base, graph, query, node_id, depth, limit, kb.security_scope_id
            )
            if not await self._unchanged(security, kb, state):
                raise RagError("RAG-GRAPH-020", "图谱或来源已变化，请重新加载", 409, False)
            return result

    @staticmethod
    def _graph_identity(graph: NativeGraphData, kb: KnowledgeBase, state: GraphState) -> None:
        """即使端口返回类型合法，范围或版本不一致也不得公开其中的生成描述。"""
        if (
            graph.security_scope_id != kb.security_scope_id
            or graph.knowledge_base_id != kb.id
            or graph.graph_generation != state.graph_generation
        ):
            raise RagError("RAG-GRAPH-004", "图谱范围或版本不一致", 409, False)

    async def _select(
        self,
        base: GraphView,
        graph: NativeGraphData,
        query: str,
        node_id: str | None,
        depth: int,
        limit: int,
        scope: str,
    ) -> GraphView:
        """只对选定结构回读有限原文；来源超预算时明确标记，绝不伪造来源摘录。"""
        ids = {
            item.id: public_graph_id(base.knowledge_base_id, base.graph_generation, "node", item.id)
            for item in graph.entities
        }
        entities = {item.id: item for item in graph.entities}
        if node_id:
            seeds = {key for key, value in ids.items() if value == node_id}
            if not seeds:
                raise ResourceNotFoundError("RAG-GRAPH-021", "图谱实体不存在")
        else:
            seeds = {item.id for item in graph.entities if query.casefold() in item.name.casefold()}
        selected = set(sorted(seeds)[:limit])
        truncated = len(seeds) > limit
        for _ in range(depth if node_id else 0):
            neighbors = {edge.target for edge in graph.relations if edge.source in selected}
            neighbors |= {edge.source for edge in graph.relations if edge.target in selected}
            additions = sorted(neighbors - selected)
            remaining = max(0, limit - len(selected))
            truncated |= len(additions) > remaining
            selected |= set(additions[:remaining])
        selected_edges = tuple(
            edge for edge in graph.relations if edge.source in selected and edge.target in selected
        )
        truncated |= len(selected_edges) > 80
        edges = selected_edges[:80]
        doc_ids = set().union(*(set(entities[key].source_document_ids) for key in selected))
        doc_ids |= set().union(*(set(edge.source_document_ids) for edge in edges))
        warnings: tuple[str, ...] = ()
        sources: tuple[AggregateSource, ...] = ()
        try:
            if len(doc_ids) > 8:
                raise RagError("RAG-GRAPH-022", "来源回读超过单次预算", 409, False)
            if doc_ids:
                sources = await self._reader.read_source_chunks(
                    scope, base.knowledge_base_id, tuple(sorted(doc_ids))
                )
        except RagError:
            warnings = ("graph_sources_unavailable",)

        def matching(
            allowed: tuple[str, ...], names: tuple[str, ...]
        ) -> tuple[AggregateSource, ...]:
            """来源只从对应节点/关系声明的活动文档中选，不借其他节点原文补数。"""
            nonlocal warnings
            # 每个文档最多一个含实体名的短原文预览，不能让首个文档多块挤掉其他来源。
            previews = []
            for document_id in allowed[:8]:
                source = next(
                    (
                        item
                        for item in sources
                        if item.engine_document_id == document_id
                        and any(name.casefold() in item.quote.casefold() for name in names)
                    ),
                    None,
                )
                if source is None:
                    if "graph_sources_unavailable" not in warnings:
                        warnings = tuple(dict.fromkeys((*warnings, "graph_sources_partial")))
                    continue
                # 引擎文档来源不保证定位到单一Chunk；仅把确实包含实体名的原文片段展示为预览。
                offsets = [source.quote.casefold().find(name.casefold()) for name in names]
                offset = max(0, min(value for value in offsets if value >= 0) - 100)
                quote = source.quote[offset : offset + 400]
                previews.append(
                    replace(
                        source, quote=quote, quote_sha256=hashlib.sha256(quote.encode()).hexdigest()
                    )
                )
            return tuple(previews)

        nodes = tuple(
            GraphViewNode(
                ids[key],
                entities[key].name,
                entities[key].entity_type,
                entities[key].description[:600],
                matching(entities[key].source_document_ids, (entities[key].name,)),
            )
            for key in sorted(selected)
        )
        public_edges = tuple(
            GraphViewEdge(
                public_graph_id(base.knowledge_base_id, base.graph_generation, "edge", edge.id),
                ids[edge.source],
                ids[edge.target],
                edge.description[:600],
                matching(
                    edge.source_document_ids,
                    (entities[edge.source].name, entities[edge.target].name),
                ),
            )
            for edge in edges
        )
        return replace(
            base, nodes=nodes, edges=public_edges, truncated=truncated, warnings=warnings
        )

    async def list_available(
        self,
        security: SecurityContext,
        knowledge_base_ids: tuple[UUID, ...],
    ) -> tuple[AggregateEvidence, ...]:
        """在线桥接仅返回可读、就绪且全部原文复核成功的多文档报告，不触发模型。"""
        result: list[AggregateEvidence] = []
        async with asyncio.timeout(8):
            for kb_id in knowledge_base_ids[:2]:
                kb, state = await self._resource(security, kb_id)
                if (
                    state.status != GraphStatus.READY
                    or not state.configuration.retrieval_enabled
                    or state.configuration.profile_ref != SUMMARY_GRAPH_PROFILE
                ):
                    continue
                # 即使本地版本仍就绪，也须验证原生物理来源没有外部变化或残留版本。
                graph = await self._reader.read_graph(
                    kb.security_scope_id, kb.id, state.graph_generation
                )
                self._graph_identity(graph, kb, state)
                for evidence in state.community_reports[:4]:
                    if await self._evidence_valid(kb, state, evidence):
                        result.append(
                            replace(evidence, configuration_version=state.configuration_version)
                        )
                if not await self._unchanged(security, kb, state):
                    raise RagError("RAG-GRAPH-020", "社区来源已变化", 409, False)
        return tuple(result[:4])

    async def _evidence_valid(
        self,
        kb: KnowledgeBase,
        state: GraphState,
        evidence: AggregateEvidence,
    ) -> bool:
        """复验报告身份和每条逐字原文；任何来源失效都不发布残缺摘要。"""
        if (
            evidence.knowledge_base_id != kb.id
            or evidence.resource_scope_id != kb.security_scope_id
            or evidence.graph_generation != state.graph_generation
            or evidence.content_revision != kb.revision
            or evidence.graph_profile_ref != state.configuration.profile_ref
            or evidence.report_sha256 != hashlib.sha256(evidence.summary.encode()).hexdigest()
            or len({source.document_id for source in evidence.sources}) < 2
        ):
            return False
        fresh = await self._reader.read_source_chunks(
            kb.security_scope_id,
            kb.id,
            tuple(sorted({source.engine_document_id for source in evidence.sources})),
        )
        for source in evidence.sources:
            if not any(
                item.resource_scope_id == source.resource_scope_id == kb.security_scope_id
                and item.knowledge_base_id == source.knowledge_base_id == kb.id
                and item.document_id == source.document_id
                and item.engine_document_id == source.engine_document_id
                and item.engine_chunk_id == source.engine_chunk_id
                and item.document_revision == source.document_revision
                and item.active_version_id == source.active_version_id
                and source.quote in item.quote
                and hashlib.sha256(source.quote.encode()).hexdigest() == source.quote_sha256
                for item in fresh
            ):
                return False
        return True

    async def revalidate(
        self,
        security: SecurityContext,
        evidences: tuple[AggregateEvidence, ...],
    ) -> bool:
        """生成后/缓存命中后沿同一公开桥接再验证；报告删除或版本切换时失败封闭。"""
        try:
            available = await self.list_available(
                security, tuple(dict.fromkeys(item.knowledge_base_id for item in evidences))
            )
            return all(
                any(
                    # ID/标签属于单次回答；摘要、生成归属、模型/策略及全部来源必须完全一致。
                    replace(item, aggregate_id=old.aggregate_id, label=old.label) == old
                    for item in available
                )
                for old in evidences
            )
        except (RagError, TimeoutError):
            return False

    async def communities(
        self,
        security: SecurityContext,
        kb_id: UUID,
        *,
        generation: int | None = None,
    ) -> tuple[GraphView, tuple[AggregateEvidence, ...]]:
        """管理只读社区列表；空库/旧策略/失效来源提供空结果及明确状态。"""
        await self._management.require_access(security, "knowledge_base.graph.read")
        kb, state = await self._resource(security, kb_id)
        self._generation(state, generation)
        base = GraphView(
            kb.id,
            state.status,
            state.graph_generation,
            kb.revision,
            state.configuration.profile_ref,
        )
        if state.status != GraphStatus.READY:
            return base, ()
        evidence = await self.list_available(security, (kb_id,))
        # 内层复读不能绕过外层浏览器请求的generation，避免旧页版本拼入新代报告。
        if not await self._unchanged(security, kb, state):
            raise RagError("RAG-GRAPH-020", "社区图版本已变化，请重新加载", 409, False)
        warnings = () if evidence else ("community_reports_unavailable",)
        return replace(base, warnings=warnings), evidence
