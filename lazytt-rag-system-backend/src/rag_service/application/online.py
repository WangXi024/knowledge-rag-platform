"""在线检索与 RAG 问答的核心业务编排。

完整流程包括：隔离范围校验、查询标准化、知识库语种统计、双语改写、多路线向量/BM25
检索、RRF 融合、去重与重排、上下文预算、LLM 生成、引用校验/修复、两级缓存、审计
落库和 SSE 事件。外部技术细节仍通过领域端口隔离在基础设施层。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
import unicodedata
from collections import Counter, defaultdict
from collections.abc import AsyncGenerator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from rag_service.application.access import (
    KnowledgeBaseAccessPolicy,
    SharedKnowledgeBaseAccessPolicy,
)
from rag_service.application.idempotency import IdempotencyService
from rag_service.application.profiles import ProfileCatalog
from rag_service.domain.cache import cache_id, knowledge_base_revisions_hash
from rag_service.domain.errors import LlmError, RagError, ResourceNotFoundError
from rag_service.domain.models import (
    AnswerOutcome,
    Citation,
    DocumentIndexNode,
    DocumentStatus,
    FeedbackRecord,
    IndexNodeType,
    KnowledgeBase,
    KnowledgeBaseScopeType,
    KnowledgeBaseStatus,
    QueryRecord,
    QueryStatus,
    RetrievalCandidate,
)
from rag_service.domain.ports import (
    AnswerCachePort,
    CacheBoundary,
    CacheEntry,
    DocumentRepository,
    EmbeddingPort,
    EngineChunk,
    EngineRetrievalQuery,
    EntityGraphRepository,
    IndexNodeRepository,
    KnowledgeBaseRepository,
    ListEngineChunksQuery,
    LlmMessage,
    LlmPort,
    LlmRequest,
    LlmUsage,
    QueryRepository,
    RagEnginePort,
    RerankPort,
)
from rag_service.security.context import SecurityContext
from rag_service.settings import AnswersSection, AppSection, LlmSection

_CITATION_LABEL = re.compile(r"\[C([1-9][0-9]*)\]")
_CJK_CHARACTER = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_ANSWER_PROMPT_PROFILE = "rag-answer-localized@1"
_CITATION_REPAIR_PROMPT_PROFILE = "citation-repair-localized@1"
_RESPONSE_LANGUAGE_NAMES = {
    "zh-CN": "Simplified Chinese (简体中文)",
    "en-US": "English",
}
_NO_EVIDENCE_ANSWERS = {
    "zh-CN": "当前知识库中没有足够证据确认该问题",
    "en-US": (
        "The current knowledge base does not contain enough evidence "
        "to answer this question."
    ),
}
_LANGUAGE_ZH = "zh-CN"
_LANGUAGE_EN = "en-US"
_SUPPORTED_RETRIEVAL_LANGUAGES = (_LANGUAGE_ZH, _LANGUAGE_EN)
_LEGACY_RRF_CONFIGURATION: dict[str, Any] = {
    "k": 60,
    "original_vector_weight": 1.0,
    "translated_vector_weight": 1.0,
    "primary_term_weight": 1.0,
    "secondary_term_weight": 1.0,
    "unknown_term_weight": 1.0,
}


def _elapsed_ms(started_at: float) -> float:
    """把单调性能计时器的差值转换为三位小数的毫秒数。"""
    return round((time.perf_counter() - started_at) * 1000, 3)


@dataclass(frozen=True, slots=True)
class HistoryMessage:
    """参与查询改写和回答的一条有限对话历史。"""
    role: str
    content: str


@dataclass(frozen=True, slots=True)
class RetrievalInput:
    """应用层检索输入；tenant_only 仅供纯检索接口限制当前租户资源。

    该标记由可信路由设置，不接受请求体覆盖；问答内部复用检索时保持现有共享库策略。
    """
    knowledge_base_ids: tuple[UUID, ...]
    query: str
    document_ids: tuple[UUID, ...] = ()
    history: tuple[HistoryMessage, ...] = ()
    metadata_filter: dict[str, Any] | None = None
    # v6 在 @5 的结构感知导航前增加独立的侧车余弦相似度门槛，避免知识库没有答案时
    # 仍从 Summary、Section 或 Parent 中强制选出 Top-N；旧版本仍可显式指定用于回滚。
    retrieval_profile: str = "hybrid-structure-aware@6"
    page_size: int = 10
    tenant_only: bool = False


@dataclass(frozen=True, slots=True)
class QueryPlan:
    """语言识别、问题类型、双语改写和针对知识语种选出的生效查询。"""
    original_query: str
    detected_language: str
    query_zh: str | None
    query_en: str | None
    knowledge_languages: tuple[str, ...]
    effective_query: str
    query_type: str = "fact"

    @property
    def variants(self) -> dict[str, str]:
        """只返回实际存在的中文/英文查询变体，供响应、缓存和审计使用。"""
        result: dict[str, str] = {}
        if self.query_zh:
            result[_LANGUAGE_ZH] = self.query_zh
        if self.query_en:
            result[_LANGUAGE_EN] = self.query_en
        return result


@dataclass(frozen=True, slots=True)
class KnowledgeLanguageProfile:
    """目标文档集合的已知语种、主语种和未知语种文档数量。"""
    languages: tuple[str, ...]
    primary_language: str | None
    unknown_documents: int = 0


@dataclass(frozen=True, slots=True)
class RetrievalRoute:
    """一条独立检索路线：查询文本、纯向量/纯关键词模式及 RRF 权重。"""
    name: str
    question: str
    vector_similarity_weight: float
    rrf_weight: float


@dataclass(frozen=True, slots=True)
class RetrievalOutput:
    """融合后的候选、问题分类、动态检索预算和完整查询规划轨迹。

    ``retrieval_top_k`` 是每条基础检索路线向 RAGFlow 请求的候选上限，
    ``rerank_top_n`` 是语义去重后最多送入 Cross-Encoder 的数量，
    ``context_top_n`` 是问答阶段最多交给 LLM 的证据块数。三者分别对应“先广泛
    召回、再精排、最后控制上下文”的不同阶段，不能用最终 ``total`` 反推。
    """
    original_query: str
    effective_query: str
    candidates: tuple[RetrievalCandidate, ...]
    total: int
    warnings: tuple[str, ...] = ()
    query_language: str = _LANGUAGE_EN
    query_variants: dict[str, str] = field(default_factory=dict)
    knowledge_languages: tuple[str, ...] = ()
    retrieval_routes: tuple[str, ...] = ()
    query_type: str = "fact"
    retrieval_top_k: int | None = None
    rerank_top_n: int | None = None
    context_top_n: int | None = None


@dataclass(frozen=True, slots=True)
class AnswerInput:
    """问答输入，额外包含生成 Profile、响应语言和稳定请求 ID。"""
    knowledge_base_ids: tuple[UUID, ...]
    query: str
    messages: tuple[HistoryMessage, ...] = ()
    metadata_filter: dict[str, Any] | None = None
    retrieval_profile: str = "hybrid-structure-aware@6"
    # 新请求使用供应商无关 Profile；实际模型来自 LLM_MODEL，历史 Profile 只服务
    # 已保存的请求和缓存边界，不再影响新调用方更换兼容端点。
    generation_profile: str = "answer-openai-compatible@4"
    response_language: str = "zh-CN"
    request_id: UUID = field(default_factory=uuid4)


@dataclass(frozen=True, slots=True)
class AnswerOutput:
    """答案、结果类型、实际引用、执行警告、用量、缓存与检索轨迹。

    动态检索字段随答案一起保存到缓存，因此命中缓存后仍能解释原答案当时使用的
    问题类型和三阶段预算。旧缓存没有这些字段时保持 ``None``，避免伪造运行参数。
    """
    answer: str
    outcome: AnswerOutcome
    citations: tuple[Citation, ...]
    warnings: tuple[str, ...]
    usage: LlmUsage | None
    response_language: str = "zh-CN"
    original_query: str = ""
    effective_query: str = ""
    cache_type: str = "none"
    timings: dict[str, float] = field(default_factory=dict)
    query_language: str = _LANGUAGE_EN
    query_variants: dict[str, str] = field(default_factory=dict)
    knowledge_languages: tuple[str, ...] = ()
    retrieval_routes: tuple[str, ...] = ()
    query_type: str | None = None
    retrieval_top_k: int | None = None
    rerank_top_n: int | None = None
    context_top_n: int | None = None


@dataclass(frozen=True, slots=True)
class StreamEvent:
    """应用层流式事件；API 层再把它编码成 SSE。"""
    event: str
    data: dict[str, Any] = field(default_factory=dict)


def normalize_query(value: str, max_chars: int) -> str:
    """对不可信查询做 Unicode、控制字符、空白和长度标准化。

    NFKC 会统一全角/兼容字符；控制与格式字符替换为空格可消除不可见指令；连续空白
    折叠后再判断空值和长度，使缓存哈希与检索输入都使用同一规范文本。
    """
    normalized = unicodedata.normalize("NFKC", value)
    normalized = "".join(
        " " if unicodedata.category(character) in {"Cc", "Cf"} else character
        for character in normalized
    )
    normalized = " ".join(normalized.split())
    if not normalized:
        raise RagError(
            "RAG-QUERY-001",
            "查询不能为空",
            http_status=400,
            retryable=False,
        )
    if len(normalized) > max_chars:
        raise RagError(
            "RAG-COMMON-006",
            "查询内容过长",
            http_status=413,
            retryable=False,
        )
    return normalized


class OnlineService:
    """编排低延迟检索、完整问答、流式问答与在线审计的应用服务。"""
    def __init__(
        self,
        knowledge_bases: KnowledgeBaseRepository,
        documents: DocumentRepository,
        rag_engine: RagEnginePort,
        llm: LlmPort,
        reranker: RerankPort,
        profiles: ProfileCatalog,
        app_settings: AppSection,
        answer_settings: AnswersSection,
        llm_settings: LlmSection,
        cache: AnswerCachePort | None = None,
        queries: QueryRepository | None = None,
        idempotency: IdempotencyService | None = None,
        embedding: EmbeddingPort | None = None,
        index_nodes: IndexNodeRepository | None = None,
        entity_graph: EntityGraphRepository | None = None,
        access_policy: KnowledgeBaseAccessPolicy | None = None,
    ) -> None:
        """注入资源仓库、模型端口、Profile、缓存和审计仓库。

        ``embedding`` 与答案语义缓存使用同一个版本化 Embedding 模型，但通过独立
        领域端口注入：在线检索会用它比较候选 Chunk 的语义相似度，删除内容近乎
        重复的候选。参数保持可选是为了让关闭优化的测试或降级运行仍可工作；生产
        组装会始终传入经过启动预检的真实适配器。``entity_graph`` 只提供源块导航，
        绝不会把合成实体或关系正文直接作为回答证据。
        """
        self._knowledge_bases = knowledge_bases
        self._documents = documents
        self._rag_engine = rag_engine
        self._llm = llm
        self._reranker = reranker
        self._profiles = profiles
        self._app_settings = app_settings
        self._answer_settings = answer_settings
        self._llm_settings = llm_settings
        self._cache = cache
        self._queries = queries
        self._idempotency = idempotency
        self._embedding = embedding
        self._index_nodes = index_nodes
        self._entity_graph = entity_graph
        self._access_policy = access_policy or SharedKnowledgeBaseAccessPolicy()

    async def execute_idempotent(
        self,
        *,
        security: SecurityContext,
        key: str,
        method: str,
        path: str,
        payload: dict[str, Any],
        response_status: int,
        operation: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        """为取消和反馈等在线写操作复用幂等协议。"""
        if self._idempotency is None:
            return await operation()
        return await self._idempotency.execute(
            security=security,
            key=key,
            method=method,
            path=path,
            payload=payload,
            response_status=response_status,
            operation=operation,
        )

    async def retrieve(
        self,
        security: SecurityContext,
        value: RetrievalInput,
    ) -> RetrievalOutput:
        """执行双语多路线检索、阈值回退、RRF 融合、去重和可选重排。

        每条路线只做纯向量或纯关键词检索，避免依赖引擎内部不可解释的混合权重；
        本地 RRF 再按 Profile 权重合并名次。首轮无候选时可用更低阈值再查一次。
        """
        knowledge_bases = await self._validate_scope(
            security,
            value.knowledge_base_ids,
            value.document_ids,
            tenant_only=value.tenant_only,
        )
        original_query = normalize_query(value.query, self._app_settings.max_query_chars)
        language_profile = await self._knowledge_language_profile(
            security,
            knowledge_bases,
            value.document_ids,
        )
        query_plan, rewrite_warnings = await self._rewrite_query(
            original_query,
            value.history,
            language_profile,
        )
        profile = self._profiles.get("RETRIEVAL", value.retrieval_profile)
        ragflow = _mapping(profile, "ragflow")
        postprocess = _mapping(profile, "postprocess")
        fallback = _mapping(profile, "fallback")
        rrf = _rrf_configuration(profile)
        adaptive = _adaptive_retrieval_strategy(profile, query_plan.query_type)
        routes = self._retrieval_routes(query_plan, language_profile, rrf)
        requested_page_size = min(
            value.page_size,
            _integer(ragflow, "page_size"),
        )
        route_top_k = int(adaptive.get("top_k", _integer(ragflow, "top_k")))
        rerank_top_n = int(
            adaptive.get("rerank_top_n", _integer(postprocess, "rerank_top_n"))
        )
        context_top_n = int(
            adaptive.get("context_top_n", _integer(postprocess, "context_top_n"))
        )
        # 多路线并发检索后以 RRF 合并，同一块在多路线排名靠前会获得更高综合分。
        candidates = await self._hybrid_rrf(
            security=security,
            knowledge_bases=knowledge_bases,
            document_ids=value.document_ids,
            routes=routes,
            page_size=max(requested_page_size, rerank_top_n),
            top_k=route_top_k,
            threshold=_number(ragflow, "similarity_threshold"),
            metadata_filter=value.metadata_filter,
            rrf_k=_integer(rrf, "k"),
        )
        warnings = list(rewrite_warnings)
        if language_profile.unknown_documents:
            warnings.append("knowledge_language_unknown_dual_route")
        if not candidates and bool(fallback.get("enabled", False)):
            candidates = await self._hybrid_rrf(
                security=security,
                knowledge_bases=knowledge_bases,
                document_ids=value.document_ids,
                routes=routes,
                page_size=max(requested_page_size, rerank_top_n),
                top_k=route_top_k,
                threshold=_number(fallback, "similarity_threshold"),
                metadata_filter=value.metadata_filter,
                rrf_k=_integer(rrf, "k"),
            )
            warnings.append("retrieval_threshold_fallback")

        # 多粒度索引只负责把概览、清单、比较和多跳问题导航到代表性原始 Chunk；
        # Parent-Child 再为已召回子块补回同组相邻证据。侧车没有复制 RAGFlow 的
        # 完整 Metadata Filter 执行器，因此请求显式过滤 Metadata 时必须整体绕过，
        # 不能为了增强召回而把过滤范围外的块补回来。Document IDs 继续逐节点过滤。
        sidecar_route_names: list[str] = []
        if value.metadata_filter is not None:
            warnings.append("sidecar_index_bypassed_metadata_filter")
        else:
            summary_index = profile.get("summary_index")
            # v5 的上下文 Parent 本身也是一条向量导航路线，它对“某个
            # 具体概念在哪一节”这类 detail 问题同样有价值；旧 Profile 没有
            # ``include_contextual_parents`` 时仍只在概览/清单/比较/多跳问题中
            # 启用摘要索引，保持历史行为不变。
            contextual_parent_enabled = isinstance(summary_index, dict) and bool(
                summary_index.get("include_contextual_parents", False)
            )
            if contextual_parent_enabled or query_plan.query_type in {
                "summary",
                "list",
                "comparison",
                "multi_hop",
            }:
                summary_candidates: tuple[RetrievalCandidate, ...] = ()
                for resource_scope_id, scoped_kbs in self._resource_groups(knowledge_bases):
                    scoped_candidates = await self._summary_index_candidates(
                        security.for_resource_scope(resource_scope_id),
                        query_plan.effective_query,
                        tuple(item.id for item in scoped_kbs),
                        value.document_ids,
                        profile,
                        rrf_k=_integer(rrf, "k"),
                        query_type=query_plan.query_type,
                    )
                    summary_candidates = _merge_candidate_routes(
                        summary_candidates,
                        scoped_candidates,
                    )
                if summary_candidates:
                    candidates = _merge_candidate_routes(candidates, summary_candidates)
                    sidecar_route_names.extend(
                        dict.fromkeys(
                            str(candidate.metadata.get("sidecar_route"))
                            for candidate in summary_candidates
                            if candidate.metadata.get("sidecar_route")
                        )
                    )
            # 实体路线使用原问题和双语改写变体的并集做精确名称匹配。它只导航到实体
            # 或显式关系所在的真实 Chunk；没有规则覆盖的问题会自然保留基础召回。
            entity_candidates: tuple[RetrievalCandidate, ...] = ()
            for resource_scope_id, scoped_kbs in self._resource_groups(knowledge_bases):
                scoped_candidates = await self._entity_graph_candidates(
                    security.for_resource_scope(resource_scope_id),
                    " ".join(
                        dict.fromkeys((original_query, *query_plan.variants.values()))
                    ),
                    tuple(item.id for item in scoped_kbs),
                    value.document_ids,
                    profile,
                    rrf_k=_integer(rrf, "k"),
                )
                entity_candidates = _merge_candidate_routes(
                    entity_candidates,
                    scoped_candidates,
                )
            if entity_candidates:
                candidates = _merge_candidate_routes(candidates, entity_candidates)
                sidecar_route_names.append("entity:graph")
            parent_candidates: tuple[RetrievalCandidate, ...] = ()
            for resource_scope_id, scoped_kbs in self._resource_groups(knowledge_bases):
                scoped_candidates = await self._parent_child_candidates(
                    security.for_resource_scope(resource_scope_id),
                    tuple(
                        item
                        for item in candidates
                        if item.resource_scope_id == resource_scope_id
                    ),
                    tuple(item.id for item in scoped_kbs),
                    profile,
                    rrf_k=_integer(rrf, "k"),
                )
                parent_candidates = _merge_candidate_routes(
                    parent_candidates,
                    scoped_candidates,
                )
            if parent_candidates:
                candidates = _merge_candidate_routes(candidates, parent_candidates)
                sidecar_route_names.append("parent:children")

        # 在昂贵的 Rerank 前先做内容去重和单文档配额，保证上下文来源多样性。
        candidates = self._postprocess(
            candidates,
            max_chunks_per_document=_integer(postprocess, "max_chunks_per_document"),
            limit=rerank_top_n,
        )
        # SHA-256 只能删除文本完全相同的候选；不同标点、标题前缀或少量 OCR 差异仍
        # 可能把同一语义重复塞入上下文。这里复用启动时已验证维度的 Embedding，
        # 按当前排序从高到低保留首个代表，失败时安全降级为原候选而不是让检索 500。
        candidates, semantic_dedup_warning = await self._semantic_deduplicate(
            candidates,
            threshold=_number(postprocess, "near_duplicate_threshold"),
        )
        if semantic_dedup_warning:
            warnings.append(semantic_dedup_warning)
        rerank_outcome = await self._reranker.rerank(
            query_plan.effective_query,
            tuple(item.content for item in candidates),
        )
        if rerank_outcome.warning:
            warnings.append(rerank_outcome.warning)
        if rerank_outcome.enabled:
            candidates = tuple(
                replace(item, rerank_score=score)
                for item, score in zip(
                    candidates,
                    rerank_outcome.scores,
                    strict=True,
                )
            )
            candidates = tuple(
                sorted(
                    candidates,
                    key=lambda item: (
                        item.rerank_score if item.rerank_score is not None else -math.inf
                    ),
                    reverse=True,
                )
            )
        return RetrievalOutput(
            original_query=original_query,
            effective_query=query_plan.effective_query,
            candidates=candidates[:requested_page_size],
            total=len(candidates),
            warnings=tuple(dict.fromkeys(warnings)),
            query_language=query_plan.detected_language,
            query_variants=query_plan.variants,
            knowledge_languages=query_plan.knowledge_languages,
            retrieval_routes=(*tuple(item.name for item in routes), *sidecar_route_names),
            query_type=query_plan.query_type,
            retrieval_top_k=route_top_k,
            rerank_top_n=rerank_top_n,
            context_top_n=context_top_n,
        )

    async def answer(
        self,
        security: SecurityContext,
        value: AnswerInput,
    ) -> AnswerOutput:
        """执行非流式问答，并在配置审计仓库时持久化完整状态变化。

        审计记录先落 ACCEPTED/RUNNING；无论业务错误还是未知异常都会变为 FAILED；
        成功后保存脱敏查询、答案、用量、耗时和实际使用的引用。
        """
        _response_language_name(value.response_language)
        if self._queries is None:
            return await self._answer_with_deadline(security, value)
        normalized = normalize_query(value.query, self._app_settings.max_query_chars)
        record = QueryRecord(
            request_id=value.request_id,
            security_scope_id=security.security_scope_id,
            query_sha256=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
            query_redacted=_redact_query(normalized),
            knowledge_base_ids=value.knowledge_base_ids,
            retrieval_profile_ref=value.retrieval_profile,
            prompt_profile_ref=_ANSWER_PROMPT_PROFILE,
            model_profile_ref=value.generation_profile,
            request_status=QueryStatus.ACCEPTED,
        )
        await self._queries.add(security.security_scope_id, record)
        record.request_status = QueryStatus.RUNNING
        await self._queries.update(security.security_scope_id, record)
        try:
            result = await self._answer_with_deadline(security, value)
        except RagError as error:
            record.request_status = QueryStatus.FAILED
            record.error_code = error.code
            record.completed_at = datetime.now(UTC)
            await self._queries.update(security.security_scope_id, record)
            raise
        except Exception:
            record.request_status = QueryStatus.FAILED
            record.error_code = "RAG-COMMON-011"
            record.completed_at = datetime.now(UTC)
            await self._queries.update(security.security_scope_id, record)
            raise
        record.request_status = QueryStatus.COMPLETED
        record.outcome = result.outcome
        record.answer_text = result.answer
        record.effective_query_redacted = _redact_query(
            result.effective_query or normalized
        )
        record.warnings = result.warnings
        record.timings = result.timings
        record.cache_status = {
            "none": "MISS",
            "exact": "EXACT_HIT",
            "semantic": "SEMANTIC_HIT",
        }[result.cache_type]
        if result.usage is not None:
            record.prompt_tokens = result.usage.prompt_tokens
            record.completion_tokens = result.usage.completion_tokens
            record.total_tokens = result.usage.total_tokens
        record.completed_at = datetime.now(UTC)
        await self._queries.update(security.security_scope_id, record)
        await self._queries.add_citations(
            security.security_scope_id,
            value.request_id,
            result.citations,
        )
        return result

    async def _answer_with_deadline(
        self,
        security: SecurityContext,
        value: AnswerInput,
    ) -> AnswerOutput:
        """给完整问答管线设置硬截止时间，并统一映射为 LLM 超时错误。"""
        try:
            async with asyncio.timeout(self._answer_settings.request_deadline_seconds):
                return await self._answer_impl(security, value)
        except TimeoutError as exc:
            raise LlmError.timeout() from exc

    async def _answer_impl(
        self,
        security: SecurityContext,
        value: AnswerInput,
    ) -> AnswerOutput:
        """依次尝试缓存、检索、生成、引用修复与写缓存，并累计阶段耗时。"""
        started_at = time.perf_counter()
        timings: dict[str, float] = {}

        def finished(result: AnswerOutput) -> AnswerOutput:
            """在任何提前返回路径上补齐总耗时，保持响应诊断字段一致。"""
            return replace(
                result,
                timings={
                    **timings,
                    "total_ms": _elapsed_ms(started_at),
                },
            )

        knowledge_bases = await self._validate_scope(
            security,
            value.knowledge_base_ids,
            (),
        )
        normalized_question = normalize_query(
            value.query,
            self._app_settings.max_query_chars,
        )
        boundary = self._cache_boundary(security, knowledge_bases, value)
        cache_warnings: tuple[str, ...] = ()
        if self._cache is not None:
            # 精确缓存成本最低，先查；未命中再做向量语义缓存。任何缓存故障都降级为
            # 正常检索，不应让“加速组件”变成问答可用性的单点故障。
            cache_started_at = time.perf_counter()
            try:
                exact = await self._cache.get_exact(boundary, normalized_question)
                if exact is not None and await self._cache_entry_valid(
                    security,
                    knowledge_bases,
                    boundary,
                    exact,
                ):
                    timings["cache_lookup_ms"] = _elapsed_ms(cache_started_at)
                    result = self._cached_answer(exact, "exact")
                    try:
                        await self._cache.mark_hit(exact)
                    except Exception:
                        result = replace(
                            result,
                            warnings=(*result.warnings, "cache_hit_mark_bypassed"),
                        )
                    return finished(result)
                semantic = await self._cache.get_semantic(
                    boundary,
                    normalized_question,
                )
                for candidate in semantic:
                    if await self._cache_entry_valid(
                        security,
                        knowledge_bases,
                        boundary,
                        candidate,
                    ):
                        timings["cache_lookup_ms"] = _elapsed_ms(cache_started_at)
                        result = self._cached_answer(candidate, "semantic")
                        try:
                            await self._cache.mark_hit(candidate)
                        except Exception:
                            result = replace(
                                result,
                                warnings=(*result.warnings, "cache_hit_mark_bypassed"),
                            )
                        return finished(result)
            except Exception:
                cache_warnings = ("cache_read_bypassed",)
            timings["cache_lookup_ms"] = _elapsed_ms(cache_started_at)

        retrieval_started_at = time.perf_counter()
        retrieval = await self.retrieve(
            security,
            RetrievalInput(
                knowledge_base_ids=value.knowledge_base_ids,
                query=value.query,
                history=value.messages,
                metadata_filter=value.metadata_filter,
                retrieval_profile=value.retrieval_profile,
                page_size=50,
            ),
        )
        timings["retrieval_ms"] = _elapsed_ms(retrieval_started_at)
        if not retrieval.candidates:
            # 没有证据时不调用 LLM，返回固定措辞，避免模型凭常识编造答案。
            return finished(
                AnswerOutput(
                    answer=_no_evidence_answer(value.response_language),
                    outcome=AnswerOutcome.NO_EVIDENCE,
                    citations=(),
                    warnings=(*retrieval.warnings, *cache_warnings),
                    usage=None,
                    response_language=value.response_language,
                    original_query=retrieval.original_query,
                    effective_query=retrieval.effective_query,
                    query_language=retrieval.query_language,
                    query_variants=retrieval.query_variants,
                    knowledge_languages=retrieval.knowledge_languages,
                    retrieval_routes=retrieval.retrieval_routes,
                    query_type=retrieval.query_type,
                    retrieval_top_k=retrieval.retrieval_top_k,
                    rerank_top_n=retrieval.rerank_top_n,
                    context_top_n=retrieval.context_top_n,
                )
            )
        context = self._context_candidates(
            retrieval.candidates,
            value.retrieval_profile,
            retrieval.query_type,
        )
        citations = self._citations(context)
        request = self._answer_request(value, retrieval.original_query, context)
        generation_started_at = time.perf_counter()
        response = await self._llm.complete(request)
        timings["generation_ms"] = _elapsed_ms(generation_started_at)
        citation_started_at = time.perf_counter()
        answer, valid = await self._validate_or_repair(
            response.content,
            request,
            citations,
            value.response_language,
        )
        timings["citation_validation_ms"] = _elapsed_ms(citation_started_at)
        if not valid:
            return finished(
                AnswerOutput(
                    answer=_no_evidence_answer(value.response_language),
                    outcome=AnswerOutcome.REFUSED,
                    citations=(),
                    warnings=(
                        *retrieval.warnings,
                        *cache_warnings,
                        "citation_validation_failed",
                    ),
                    usage=response.usage,
                    response_language=value.response_language,
                    original_query=retrieval.original_query,
                    effective_query=retrieval.effective_query,
                    query_language=retrieval.query_language,
                    query_variants=retrieval.query_variants,
                    knowledge_languages=retrieval.knowledge_languages,
                    retrieval_routes=retrieval.retrieval_routes,
                    query_type=retrieval.query_type,
                    retrieval_top_k=retrieval.retrieval_top_k,
                    rerank_top_n=retrieval.rerank_top_n,
                    context_top_n=retrieval.context_top_n,
                )
            )
        used = self._used_citations(answer, citations)
        result = AnswerOutput(
            answer=answer,
            outcome=AnswerOutcome.SUCCESS,
            citations=used,
            warnings=(*retrieval.warnings, *cache_warnings),
            usage=response.usage,
            response_language=value.response_language,
            original_query=retrieval.original_query,
            effective_query=retrieval.effective_query,
            query_language=retrieval.query_language,
            query_variants=retrieval.query_variants,
            knowledge_languages=retrieval.knowledge_languages,
            retrieval_routes=retrieval.retrieval_routes,
            query_type=retrieval.query_type,
            retrieval_top_k=retrieval.retrieval_top_k,
            rerank_top_n=retrieval.rerank_top_n,
            context_top_n=retrieval.context_top_n,
        )
        if self._cache is not None:
            cache_write_started_at = time.perf_counter()
            try:
                await self._cache.put(
                    CacheEntry(
                        cache_id=cache_id(boundary, normalized_question),
                        boundary=boundary,
                        normalized_question=normalized_question,
                        answer=result.answer,
                        citations=result.citations,
                        original_query=result.original_query,
                        effective_query=result.effective_query,
                        query_language=result.query_language,
                        query_variants=result.query_variants,
                        knowledge_languages=result.knowledge_languages,
                        retrieval_routes=result.retrieval_routes,
                        query_type=result.query_type,
                        retrieval_top_k=result.retrieval_top_k,
                        rerank_top_n=result.rerank_top_n,
                        context_top_n=result.context_top_n,
                        warnings=result.warnings,
                        usage=result.usage,
                    )
                )
            except Exception:
                result = replace(
                    result,
                    warnings=(*result.warnings, "cache_write_bypassed"),
                )
            timings["cache_write_ms"] = _elapsed_ms(cache_write_started_at)
        return finished(result)

    async def stream_answer(
        self,
        security: SecurityContext,
        value: AnswerInput,
    ) -> AsyncGenerator[StreamEvent, None]:
        """执行带审计的流式问答，并逐步产出状态/文本/引用/完成事件。"""
        _response_language_name(value.response_language)
        if self._queries is None:
            async for event in self._stream_with_deadline(security, value):
                yield event
            return
        normalized = normalize_query(value.query, self._app_settings.max_query_chars)
        record = QueryRecord(
            request_id=value.request_id,
            security_scope_id=security.security_scope_id,
            query_sha256=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
            query_redacted=_redact_query(normalized),
            knowledge_base_ids=value.knowledge_base_ids,
            retrieval_profile_ref=value.retrieval_profile,
            prompt_profile_ref=_ANSWER_PROMPT_PROFILE,
            model_profile_ref=value.generation_profile,
            request_status=QueryStatus.ACCEPTED,
        )
        await self._queries.add(security.security_scope_id, record)
        record.request_status = QueryStatus.RUNNING
        await self._queries.update(security.security_scope_id, record)
        answer_parts: list[str] = []
        citations: list[Citation] = []
        cache_type = "none"
        try:
            async for event in self._stream_with_deadline(security, value):
                current = await self._queries.get(
                    security.security_scope_id,
                    value.request_id,
                )
                if current is not None and current.request_status == QueryStatus.CANCELLED:
                    yield StreamEvent(
                        "completed",
                        {
                            "outcome": AnswerOutcome.CANCELLED.value,
                            "warnings": ["request_cancelled"],
                        },
                    )
                    return
                if event.event == "answer.delta":
                    answer_parts.append(str(event.data.get("delta", "")))
                elif event.event == "citation":
                    citation = event.data.get("citation")
                    if isinstance(citation, Citation):
                        citations.append(citation)
                elif event.event == "retrieval.completed":
                    record.candidate_count = int(event.data.get("candidate_count", 0))
                    record.context_count = int(event.data.get("context_count", 0))
                    effective_query = event.data.get("effective_query")
                    if isinstance(effective_query, str) and effective_query:
                        record.effective_query_redacted = _redact_query(effective_query)
                elif event.event == "usage":
                    usage = event.data.get("usage")
                    if isinstance(usage, LlmUsage):
                        record.prompt_tokens = usage.prompt_tokens
                        record.completion_tokens = usage.completion_tokens
                        record.total_tokens = usage.total_tokens
                elif event.event == "completed":
                    raw_outcome = str(event.data.get("outcome", "refused"))
                    record.outcome = AnswerOutcome(raw_outcome)
                    cache_type = str(event.data.get("cache_type", "none"))
                    warnings = event.data.get("warnings", [])
                    if isinstance(warnings, list):
                        record.warnings = tuple(str(item) for item in warnings)
                    timings = event.data.get("timings")
                    if isinstance(timings, dict):
                        record.timings = {
                            str(key): float(value)
                            for key, value in timings.items()
                            if isinstance(value, int | float)
                        }
                yield event
        except asyncio.CancelledError:
            record.request_status = QueryStatus.CANCELLED
            record.outcome = AnswerOutcome.CANCELLED
            record.completed_at = datetime.now(UTC)
            await self._queries.update(security.security_scope_id, record)
            raise
        except RagError as error:
            record.request_status = QueryStatus.FAILED
            record.error_code = error.code
            record.completed_at = datetime.now(UTC)
            await self._queries.update(security.security_scope_id, record)
            raise
        except Exception:
            record.request_status = QueryStatus.FAILED
            record.error_code = "RAG-COMMON-011"
            record.completed_at = datetime.now(UTC)
            await self._queries.update(security.security_scope_id, record)
            raise
        record.request_status = QueryStatus.COMPLETED
        record.cache_status = {
            "none": "MISS",
            "exact": "EXACT_HIT",
            "semantic": "SEMANTIC_HIT",
        }.get(cache_type, "BYPASS")
        record.answer_text = "".join(answer_parts) or (
            _no_evidence_answer(value.response_language)
            if record.outcome == AnswerOutcome.NO_EVIDENCE
            else None
        )
        record.completed_at = datetime.now(UTC)
        await self._queries.update(security.security_scope_id, record)
        await self._queries.add_citations(
            security.security_scope_id,
            value.request_id,
            citations,
        )

    async def _stream_with_deadline(
        self,
        security: SecurityContext,
        value: AnswerInput,
    ) -> AsyncGenerator[StreamEvent, None]:
        """为整个异步事件生成器施加总时限，并把超时转换为稳定错误。"""
        try:
            async with asyncio.timeout(self._answer_settings.request_deadline_seconds):
                async for event in self._stream_answer_impl(security, value):
                    yield event
        except TimeoutError as exc:
            raise LlmError.timeout() from exc

    async def _stream_answer_impl(
        self,
        security: SecurityContext,
        value: AnswerInput,
    ) -> AsyncGenerator[StreamEvent, None]:
        """流式版问答实现，复用缓存/检索规则并累积模型增量。

        模型 Token 到达时立即发 ``delta``；完整文本结束后才校验引用并发 ``citation``。
        因此客户端看到的最终 ``done`` 事件才代表答案和引用均已确认并可审计。
        """
        started_at = time.perf_counter()
        timings: dict[str, float] = {}
        yield StreamEvent("accepted")
        yield StreamEvent(
            "retrieval.started",
            {"retrieval_profile": value.retrieval_profile},
        )
        knowledge_bases = await self._validate_scope(
            security,
            value.knowledge_base_ids,
            (),
        )
        normalized_question = normalize_query(
            value.query,
            self._app_settings.max_query_chars,
        )
        boundary = self._cache_boundary(security, knowledge_bases, value)
        cache_warnings: list[str] = []
        if self._cache is not None:
            cache_started_at = time.perf_counter()
            try:
                cached = await self._cache.get_exact(boundary, normalized_question)
                cache_type = "exact"
                if cached is None or not await self._cache_entry_valid(
                    security,
                    knowledge_bases,
                    boundary,
                    cached,
                ):
                    cached = None
                    cache_type = "semantic"
                    for candidate in await self._cache.get_semantic(
                        boundary,
                        normalized_question,
                    ):
                        if await self._cache_entry_valid(
                            security,
                            knowledge_bases,
                            boundary,
                            candidate,
                        ):
                            cached = candidate
                            break
                timings["cache_lookup_ms"] = _elapsed_ms(cache_started_at)
                if cached is not None:
                    cached_answer = self._cached_answer(cached, cache_type)
                    try:
                        await self._cache.mark_hit(cached)
                    except Exception:
                        cached_answer = replace(
                            cached_answer,
                            warnings=(
                                *cached_answer.warnings,
                                "cache_hit_mark_bypassed",
                            ),
                        )
                    yield StreamEvent(
                        "retrieval.completed",
                        {
                            "candidate_count": 0,
                            "context_count": len(cached_answer.citations),
                            "original_query": cached_answer.original_query,
                            "effective_query": cached_answer.effective_query,
                            "query_rewritten": (
                                _query_was_rewritten(cached_answer)
                            ),
                            "query_plan": _query_plan_trace(cached_answer),
                        },
                    )
                    yield StreamEvent(
                        "generation.started",
                        {
                            "model_profile": value.generation_profile,
                            "response_language": value.response_language,
                            "source": "cache",
                        },
                    )
                    yield StreamEvent("answer.delta", {"delta": cached_answer.answer})
                    for citation in cached_answer.citations:
                        yield StreamEvent("citation", {"citation": citation})
                    if cached_answer.usage is not None:
                        yield StreamEvent("usage", {"usage": cached_answer.usage})
                    yield StreamEvent(
                        "completed",
                        {
                            "outcome": cached_answer.outcome.value,
                            "warnings": list(cached_answer.warnings),
                            "cache_type": cache_type,
                            "timings": {
                                **timings,
                                "total_ms": _elapsed_ms(started_at),
                            },
                        },
                    )
                    return
            except Exception:
                cache_warnings.append("cache_read_bypassed")
                timings["cache_lookup_ms"] = _elapsed_ms(cache_started_at)

        retrieval_started_at = time.perf_counter()
        retrieval = await self.retrieve(
            security,
            RetrievalInput(
                knowledge_base_ids=value.knowledge_base_ids,
                query=value.query,
                history=value.messages,
                metadata_filter=value.metadata_filter,
                retrieval_profile=value.retrieval_profile,
                page_size=50,
            ),
        )
        timings["retrieval_ms"] = _elapsed_ms(retrieval_started_at)
        context = self._context_candidates(
            retrieval.candidates,
            value.retrieval_profile,
            retrieval.query_type,
        )
        yield StreamEvent(
            "retrieval.completed",
            {
                "candidate_count": len(retrieval.candidates),
                "context_count": len(context),
                "original_query": retrieval.original_query,
                "effective_query": retrieval.effective_query,
                "query_rewritten": (
                    _query_was_rewritten(retrieval)
                ),
                "query_plan": _query_plan_trace(retrieval),
            },
        )
        if not context:
            yield StreamEvent(
                "answer.delta",
                {"delta": _no_evidence_answer(value.response_language)},
            )
            yield StreamEvent(
                "completed",
                {
                    "outcome": AnswerOutcome.NO_EVIDENCE.value,
                    "warnings": [*retrieval.warnings, *cache_warnings],
                    "cache_type": "none",
                    "timings": {
                        **timings,
                        "total_ms": _elapsed_ms(started_at),
                    },
                },
            )
            return

        citations = self._citations(context)
        request = self._answer_request(value, retrieval.original_query, context)
        yield StreamEvent(
            "generation.started",
            {
                "model_profile": value.generation_profile,
                "response_language": value.response_language,
            },
        )
        answer_parts: list[str] = []
        usage: LlmUsage | None = None
        generation_started_at = time.perf_counter()
        async for item in self._llm.stream(request):
            if item.delta:
                answer_parts.append(item.delta)
                yield StreamEvent("answer.delta", {"delta": item.delta})
            if item.usage is not None:
                usage = item.usage
        timings["generation_ms"] = _elapsed_ms(generation_started_at)
        answer = "".join(answer_parts)
        citation_started_at = time.perf_counter()
        used = self._used_citations(answer, citations)
        valid = bool(used) and self._all_labels_valid(answer, citations)
        timings["citation_validation_ms"] = _elapsed_ms(citation_started_at)
        if valid:
            for citation in used:
                yield StreamEvent("citation", {"citation": citation})
        if usage is not None:
            yield StreamEvent("usage", {"usage": usage})
        warnings = [*retrieval.warnings, *cache_warnings]
        outcome = AnswerOutcome.SUCCESS if valid else AnswerOutcome.REFUSED
        if not valid:
            warnings.append("citation_validation_failed_after_stream")
        elif self._cache is not None:
            cache_write_started_at = time.perf_counter()
            try:
                await self._cache.put(
                    CacheEntry(
                        cache_id=cache_id(boundary, normalized_question),
                        boundary=boundary,
                        normalized_question=normalized_question,
                        answer=answer,
                        citations=used,
                        original_query=retrieval.original_query,
                        effective_query=retrieval.effective_query,
                        query_language=retrieval.query_language,
                        query_variants=retrieval.query_variants,
                        knowledge_languages=retrieval.knowledge_languages,
                        retrieval_routes=retrieval.retrieval_routes,
                        query_type=retrieval.query_type,
                        retrieval_top_k=retrieval.retrieval_top_k,
                        rerank_top_n=retrieval.rerank_top_n,
                        context_top_n=retrieval.context_top_n,
                        warnings=tuple(warnings),
                        usage=usage,
                    )
                )
            except Exception:
                warnings.append("cache_write_bypassed")
            timings["cache_write_ms"] = _elapsed_ms(cache_write_started_at)
        yield StreamEvent(
            "completed",
            {
                "outcome": outcome.value,
                "warnings": warnings,
                "cache_type": "none",
                "timings": {
                    **timings,
                    "total_ms": _elapsed_ms(started_at),
                },
            },
        )

    async def get_request(
        self,
        security: SecurityContext,
        request_id: UUID,
    ) -> QueryRecord:
        """按安全范围读取在线请求审计记录。"""
        if self._queries is None:
            raise ResourceNotFoundError("RAG-QUERY-004", "问答请求不存在")
        value = await self._queries.get(security.security_scope_id, request_id)
        if value is None:
            raise ResourceNotFoundError("RAG-QUERY-004", "问答请求不存在")
        return value

    async def cancel_request(
        self,
        security: SecurityContext,
        request_id: UUID,
    ) -> QueryRecord:
        """把尚未结束的请求标记为取消；已结束请求保持幂等返回。"""
        value = await self.get_request(security, request_id)
        if value.request_status not in {QueryStatus.ACCEPTED, QueryStatus.RUNNING}:
            raise RagError(
                "RAG-COMMON-010",
                "问答请求已结束",
                http_status=409,
                retryable=False,
            )
        value.request_status = QueryStatus.CANCELLED
        value.outcome = AnswerOutcome.CANCELLED
        value.completed_at = datetime.now(UTC)
        assert self._queries is not None
        await self._queries.update(security.security_scope_id, value)
        return value

    async def list_request_citations(
        self,
        security: SecurityContext,
        request_id: UUID,
    ) -> tuple[Citation, ...]:
        """确认请求归属后列出其实际使用引用。"""
        await self.get_request(security, request_id)
        assert self._queries is not None
        return await self._queries.list_citations(
            security.security_scope_id,
            request_id,
        )

    async def get_citation(
        self,
        security: SecurityContext,
        citation_id: UUID,
    ) -> Citation:
        """按安全范围读取单条引用，隐藏跨租户引用是否存在。"""
        if self._queries is None:
            raise ResourceNotFoundError("RAG-CITATION-001", "引用不存在")
        value = await self._queries.get_citation(
            security.security_scope_id,
            citation_id,
        )
        if value is None:
            raise ResourceNotFoundError("RAG-CITATION-001", "引用不存在")
        return value

    async def submit_feedback(
        self,
        security: SecurityContext,
        *,
        request_id: UUID,
        feedback_type: str,
        rating: int | None,
        comment: str | None,
        corrected_answer: str | None,
    ) -> FeedbackRecord:
        """确认目标请求存在后保存结构化反馈。"""
        await self.get_request(security, request_id)
        assert self._queries is not None
        value = FeedbackRecord(
            security_scope_id=security.security_scope_id,
            request_id=request_id,
            feedback_type=feedback_type,
            rating=rating,
            comment=comment,
            corrected_answer=corrected_answer,
        )
        await self._queries.add_feedback(security.security_scope_id, value)
        return value

    async def _validate_scope(
        self,
        security: SecurityContext,
        knowledge_base_ids: Sequence[UUID],
        document_ids: Sequence[UUID],
        *,
        tenant_only: bool = False,
    ) -> tuple[KnowledgeBase, ...]:
        """验证知识库及文档范围；纯检索可进一步限定为当前租户的 TENANT 库。

        tenant_only 为真时，数据库候选范围先缩为认证租户的资源范围，再核验
        scope_type；显式系统库、其他租户库与不存在的 ID 统一返回 404，避免探测。
        不写入数据库或调用检索引擎；问答默认保留原有系统共享库可见规则。
        """
        if len(set(knowledge_base_ids)) != len(knowledge_base_ids):
            raise RagError(
                "RAG-COMMON-001",
                "知识库 ID 不可重复",
                http_status=400,
                retryable=False,
            )
        candidate_scopes = self._access_policy.candidate_resource_scopes(security)
        if tenant_only:
            # 只保留策略允许且由可信 tenant_id 派生的资源范围，不能从请求体扩大范围。
            candidate_scopes = tuple(
                scope for scope in candidate_scopes if scope == security.tenant_resource_scope_id
            )
        if knowledge_base_ids:
            knowledge_bases = await self._knowledge_bases.get_many_accessible(
                candidate_scopes,
                knowledge_base_ids,
            )
        else:
            knowledge_bases = await self._knowledge_bases.list_ready_accessible(
                candidate_scopes,
            )
        knowledge_bases = await self._access_policy.filter_readable(
            security,
            knowledge_bases,
        )
        if tenant_only:
            knowledge_bases = tuple(
                item
                for item in knowledge_bases
                if item.scope_type == KnowledgeBaseScopeType.TENANT
                and item.security_scope_id == security.tenant_resource_scope_id
            )
        if (knowledge_base_ids and len(knowledge_bases) != len(knowledge_base_ids)) or any(
            item.status != KnowledgeBaseStatus.READY for item in knowledge_bases
        ):
            raise ResourceNotFoundError("RAG-KB-001", "知识库不存在")
        if not knowledge_bases:
            raise ResourceNotFoundError("RAG-KB-001", "没有可用知识库")
        embedding_profiles = {item.embedding_profile_ref for item in knowledge_bases}
        if len(embedding_profiles) != 1:
            raise RagError(
                "RAG-RETRIEVAL-006",
                "知识库 Embedding 配置不兼容",
                http_status=409,
                retryable=False,
            )
        if document_ids:
            documents = await self._documents.get_many_accessible(
                candidate_scopes,
                document_ids,
            )
            allowed_kb_ids = {item.id for item in knowledge_bases}
            if len(documents) != len(set(document_ids)) or any(
                item.knowledge_base_id not in allowed_kb_ids or item.status != DocumentStatus.READY
                for item in documents
            ):
                raise ResourceNotFoundError("RAG-DOC-001", "文档不存在")
        return knowledge_bases

    @staticmethod
    def _resource_groups(
        knowledge_bases: Sequence[KnowledgeBase],
    ) -> tuple[tuple[str, tuple[KnowledgeBase, ...]], ...]:
        """按资源范围分组，确保租户库和系统库分别调用底层隔离接口。"""
        grouped: dict[str, list[KnowledgeBase]] = {}
        for knowledge_base in knowledge_bases:
            grouped.setdefault(knowledge_base.security_scope_id, []).append(knowledge_base)
        return tuple(
            (resource_scope_id, tuple(items))
            for resource_scope_id, items in grouped.items()
        )

    @staticmethod
    def _cache_boundary(
        security: SecurityContext,
        knowledge_bases: tuple[KnowledgeBase, ...],
        value: AnswerInput,
    ) -> CacheBoundary:
        """用租户、知识库修订、Profile、语言和对话历史构造答案缓存边界。"""
        history = json.dumps(
            [{"role": item.role, "content": item.content} for item in value.messages],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return CacheBoundary(
            security_scope_id=security.security_scope_id,
            knowledge_base_revisions_hash=knowledge_base_revisions_hash(
                [(item.id, item.revision) for item in knowledge_bases]
            ),
            retrieval_profile=value.retrieval_profile,
            prompt_profile=_ANSWER_PROMPT_PROFILE,
            model_profile=value.generation_profile,
            language=value.response_language,
            history_hash=hashlib.sha256(history.encode("utf-8")).hexdigest(),
        )

    async def _cache_entry_valid(
        self,
        security: SecurityContext,
        knowledge_bases: tuple[KnowledgeBase, ...],
        expected_boundary: CacheBoundary,
        value: CacheEntry,
    ) -> bool:
        """在返回缓存前重新验证边界和每条引用仍指向 READY 文档。"""
        if value.boundary != expected_boundary or not value.citations:
            return False
        engine_health = await self._rag_engine.health()
        if engine_health.status != "ok" or any(
            status != "ok" for status in engine_health.dependencies.values()
        ):
            return False
        candidate_scopes = self._access_policy.candidate_resource_scopes(security)
        documents = await self._documents.get_many_accessible(
            candidate_scopes,
            tuple(item.document_id for item in value.citations),
        )
        documents_by_id = {item.id: item for item in documents}
        allowed_kbs = {
            item.id: item.security_scope_id
            for item in knowledge_bases
        }
        return all(
            bool(citation.resource_scope_id)
            and allowed_kbs.get(citation.knowledge_base_id) == citation.resource_scope_id
            and (document := documents_by_id.get(citation.document_id)) is not None
            and document.status == DocumentStatus.READY
            and document.knowledge_base_id == citation.knowledge_base_id
            and document.security_scope_id == citation.resource_scope_id
            for citation in value.citations
        )

    @staticmethod
    def _cached_answer(value: CacheEntry, cache_type: str) -> AnswerOutput:
        """将缓存记录还原为答案，并保留原请求的问题分类与动态预算。

        缓存兼容旧版本：历史条目没有动态字段时返回 ``None``，客户端可以显示
        “历史缓存未记录”，而不是错误地把当前 Profile 参数冒充为当时参数。
        """
        citations = tuple(replace(item, citation_id=uuid4()) for item in value.citations)
        return AnswerOutput(
            answer=value.answer,
            outcome=AnswerOutcome.SUCCESS,
            citations=citations,
            warnings=value.warnings,
            usage=value.usage,
            response_language=value.boundary.language,
            original_query=value.original_query or value.normalized_question,
            effective_query=value.effective_query or value.normalized_question,
            cache_type=cache_type,
            query_language=value.query_language or _query_language(value.normalized_question),
            query_variants=dict(value.query_variants),
            knowledge_languages=value.knowledge_languages,
            retrieval_routes=value.retrieval_routes,
            query_type=value.query_type,
            retrieval_top_k=value.retrieval_top_k,
            rerank_top_n=value.rerank_top_n,
            context_top_n=value.context_top_n,
        )

    async def _rewrite_query(
        self,
        query: str,
        history: tuple[HistoryMessage, ...],
        language_profile: KnowledgeLanguageProfile,
    ) -> tuple[QueryPlan, tuple[str, ...]]:
        """按查询与知识语种决定是否调用 LLM 生成中英文检索变体。

        单语且同语种知识可直接使用原问题；跨语种/混合语种知识请求结构化双语改写。
        改写失败时保留原查询并返回降级警告，检索仍可继续。
        """
        detected_language = _query_language(query)
        base_query_zh = query if detected_language == _LANGUAGE_ZH else None
        base_query_en = query if detected_language == _LANGUAGE_EN else None
        missing_language = any(
            (language == _LANGUAGE_ZH and base_query_zh is None)
            or (language == _LANGUAGE_EN and base_query_en is None)
            for language in language_profile.languages
        )
        if not history and not missing_language:
            return (
                _query_plan(
                    original_query=query,
                    detected_language=detected_language,
                    query_zh=base_query_zh,
                    query_en=base_query_en,
                    language_profile=language_profile,
                ),
                (),
            )
        profile = self._profiles.get("PROMPT", "retrieval-query-bilingual@1")
        # 双语 QueryPlan 与最终答案共享通用 LLM 连接；协议差异由 Bootstrap 探测，
        # 此处只从供应商无关 Profile 读取业务超时和输出预算。
        model_profile = self._profiles.get("MODEL", "rewrite-openai-compatible@4")
        conversation = (
            "\n".join(f"{item.role}: {item.content}" for item in history)
            if history
            else "(none)"
        )
        prompt = _template(
            _string(profile, "template"),
            conversation_text=conversation,
            query=query,
            query_language=detected_language,
            knowledge_languages=", ".join(language_profile.languages),
        )
        try:
            async with asyncio.timeout(_number(model_profile, "total_timeout_seconds")):
                max_attempts = _integer(model_profile, "max_attempts")
                for attempt in range(max_attempts):
                    try:
                        response = await self._llm.complete(
                            LlmRequest(
                                messages=(
                                    LlmMessage("system", _string(profile, "system")),
                                    LlmMessage("user", prompt),
                                ),
                                model=self._llm_settings.rewrite_model,
                                max_output_tokens=_integer(
                                    model_profile,
                                    "max_output_tokens",
                                ),
                                temperature=_number(model_profile, "temperature"),
                                top_p=_number(model_profile, "top_p"),
                            )
                        )
                        query_zh, query_en = _parse_bilingual_query_response(
                            response.content,
                            original_query=query,
                            detected_language=detected_language,
                            max_chars=self._app_settings.max_query_chars,
                        )
                        if any(
                            language == _LANGUAGE_ZH and not query_zh
                            or language == _LANGUAGE_EN and not query_en
                            for language in language_profile.languages
                        ):
                            raise ValueError("query rewrite omitted a required language")
                        return (
                            _query_plan(
                                original_query=query,
                                detected_language=detected_language,
                                query_zh=query_zh,
                                query_en=query_en,
                                language_profile=language_profile,
                            ),
                            (),
                        )
                    except Exception:
                        if attempt + 1 >= max_attempts:
                            raise
                raise RuntimeError("query rewrite attempts exhausted")
        except Exception:
            return (
                _query_plan(
                    original_query=query,
                    detected_language=detected_language,
                    query_zh=base_query_zh,
                    query_en=base_query_en,
                    language_profile=language_profile,
                ),
                ("query_rewrite_fallback",),
            )

    async def _knowledge_language_profile(
        self,
        security: SecurityContext,
        knowledge_bases: tuple[KnowledgeBase, ...],
        document_ids: tuple[UUID, ...],
    ) -> KnowledgeLanguageProfile:
        """统计目标文档的中文、英文和未知语种，推导主语种。"""
        zh_count = 0
        en_count = 0
        unknown_count = 0
        if document_ids:
            documents = await self._documents.get_many_accessible(
                tuple(item.security_scope_id for item in knowledge_bases),
                document_ids,
            )
            counts = _document_language_counts(documents)
            zh_count, en_count, unknown_count = counts
        else:
            for resource_scope_id, scoped_kbs in self._resource_groups(knowledge_bases):
                counts_by_kb = await self._documents.language_counts(
                    resource_scope_id,
                    tuple(item.id for item in scoped_kbs),
                )
                for raw_counts in counts_by_kb.values():
                    for raw_language, count in raw_counts.items():
                        language = _normalize_document_language(raw_language)
                        if language == _LANGUAGE_ZH:
                            zh_count += count
                        elif language == _LANGUAGE_EN:
                            en_count += count
                        else:
                            unknown_count += count

        if zh_count == 0 and en_count == 0:
            unknown_count = max(1, unknown_count)
        languages: tuple[str, ...]
        if unknown_count:
            languages = _SUPPORTED_RETRIEVAL_LANGUAGES
        elif zh_count and en_count:
            languages = _SUPPORTED_RETRIEVAL_LANGUAGES
        elif zh_count:
            languages = (_LANGUAGE_ZH,)
        else:
            languages = (_LANGUAGE_EN,)
        primary_language: str | None = None
        if zh_count > en_count:
            primary_language = _LANGUAGE_ZH
        elif en_count > zh_count:
            primary_language = _LANGUAGE_EN
        return KnowledgeLanguageProfile(
            languages=languages,
            primary_language=primary_language,
            unknown_documents=unknown_count,
        )

    @staticmethod
    def _retrieval_routes(
        query_plan: QueryPlan,
        language_profile: KnowledgeLanguageProfile,
        rrf: dict[str, Any],
    ) -> tuple[RetrievalRoute, ...]:
        """根据 QueryPlan 生成去重后的原文/译文向量路线与各语种关键词路线。"""
        routes: list[RetrievalRoute] = [
            RetrievalRoute(
                name=f"vector:original:{query_plan.detected_language}",
                question=query_plan.original_query,
                vector_similarity_weight=1.0,
                rrf_weight=_number(rrf, "original_vector_weight"),
            )
        ]
        variants = query_plan.variants
        for language in language_profile.languages:
            question = variants.get(language)
            if not question:
                continue
            if question != query_plan.original_query:
                routes.append(
                    RetrievalRoute(
                        name=f"vector:translated:{language}",
                        question=question,
                        vector_similarity_weight=1.0,
                        rrf_weight=_number(rrf, "translated_vector_weight"),
                    )
                )
            if language_profile.primary_language == language:
                term_weight = _number(rrf, "primary_term_weight")
            elif language_profile.primary_language is None:
                term_weight = _number(rrf, "unknown_term_weight")
            else:
                term_weight = _number(rrf, "secondary_term_weight")
            routes.append(
                RetrievalRoute(
                    name=f"term:{language}",
                    question=question,
                    vector_similarity_weight=0.0,
                    rrf_weight=term_weight,
                )
            )
        unique: list[RetrievalRoute] = []
        seen: set[tuple[str, str]] = set()
        for route in routes:
            route_type = route.name.split(":", maxsplit=1)[0]
            key = (route_type, route.question)
            if key in seen:
                continue
            seen.add(key)
            unique.append(route)
        return tuple(unique)

    async def _hybrid_rrf(
        self,
        *,
        security: SecurityContext,
        knowledge_bases: tuple[KnowledgeBase, ...],
        document_ids: tuple[UUID, ...],
        routes: tuple[RetrievalRoute, ...],
        page_size: int,
        top_k: int,
        threshold: float,
        metadata_filter: dict[str, Any] | None,
        rrf_k: int,
    ) -> tuple[RetrievalCandidate, ...]:
        """并发执行所有检索路线并用加权倒数排名融合候选。

        RRF 分数公式为 ``路线权重 / (k + 名次)``；它只依赖相对名次，不要求向量分
        与 BM25 分处于同一数值尺度。候选还保留各路线观察到的最佳原始分数。
        """
        async def execute(
            route: RetrievalRoute,
            resource_scope_id: str,
            scoped_kbs: tuple[KnowledgeBase, ...],
        ) -> tuple[RetrievalCandidate, ...]:
            """把单条路线转换为引擎查询并仅返回候选序列。"""
            result = await self._rag_engine.retrieve(
                EngineRetrievalQuery(
                    security=security.for_resource_scope(resource_scope_id),
                    knowledge_base_ids=tuple(item.id for item in scoped_kbs),
                    document_ids=document_ids,
                    question=route.question,
                    page_size=page_size,
                    top_k=top_k,
                    similarity_threshold=threshold,
                    vector_similarity_weight=route.vector_similarity_weight,
                    metadata_filter=metadata_filter,
                )
            )
            return result.candidates

        executions = tuple(
            (route, resource_scope_id, scoped_kbs)
            for resource_scope_id, scoped_kbs in self._resource_groups(knowledge_bases)
            for route in routes
        )
        results = await asyncio.gather(
            *(
                execute(route, resource_scope_id, scoped_kbs)
                for route, resource_scope_id, scoped_kbs in executions
            )
        )
        route_results = tuple(
            (route, candidates)
            for (route, _resource_scope_id, _scoped_kbs), candidates in zip(
                executions,
                results,
                strict=True,
            )
        )

        merged: dict[str, RetrievalCandidate] = {}
        scores: defaultdict[str, float] = defaultdict(float)
        vector_scores: dict[str, float | None] = {}
        term_scores: dict[str, float | None] = {}
        route_names: defaultdict[str, list[str]] = defaultdict(list)
        for route, candidates in route_results:
            for rank, candidate in enumerate(candidates, start=1):
                if not _valid_candidate(candidate):
                    continue
                merged.setdefault(candidate.chunk_id, candidate)
                scores[candidate.chunk_id] += route.rrf_weight / (rrf_k + rank)
                # 一个真实 Chunk 可能同时被原问题向量、翻译问题向量和 BM25 命中。
                # 旧实现只累计 RRF 分数，没有保留这些路线名称，开发人员只能看到
                # 最后附加的侧车路线，容易误以为基础召回没有执行。这里按首次出现
                # 顺序记录完整路线，后续再与 Summary/Parent/实体路线无损合并。
                if route.name not in route_names[candidate.chunk_id]:
                    route_names[candidate.chunk_id].append(route.name)
                if route.vector_similarity_weight == 1.0:
                    vector_score = (
                        candidate.vector_score
                        if candidate.vector_score is not None
                        else candidate.score
                    )
                    previous = vector_scores.get(candidate.chunk_id)
                    if previous is None or vector_score > previous:
                        vector_scores[candidate.chunk_id] = vector_score
                else:
                    term_score = (
                        candidate.term_score
                        if candidate.term_score is not None
                        else candidate.score
                    )
                    previous = term_scores.get(candidate.chunk_id)
                    if previous is None or term_score > previous:
                        term_scores[candidate.chunk_id] = term_score
        return tuple(
            replace(
                merged[chunk_id],
                score=score,
                vector_score=vector_scores.get(chunk_id),
                term_score=term_scores.get(chunk_id),
                metadata=_metadata_with_retrieval_routes(
                    merged[chunk_id].metadata,
                    route_names[chunk_id],
                ),
            )
            for chunk_id, score in sorted(
                scores.items(),
                key=lambda item: item[1],
                reverse=True,
            )
        )

    async def _summary_index_candidates(
        self,
        security: SecurityContext,
        query: str,
        knowledge_base_ids: tuple[UUID, ...],
        document_ids: tuple[UUID, ...],
        profile: dict[str, Any],
        *,
        rrf_k: int,
        query_type: str,
    ) -> tuple[RetrievalCandidate, ...]:
        """用 Summary、Section 和可选上下文 Parent 定位真实 Chunk。

        侧车正文绝不直接进入答案候选。命中的 Summary/Section/Parent 节点只提供真实源
        Chunk ID；方法随后从 RAGFlow 回读原文并按节点级别赋予独立 RRF 权重。新Profile
        可以在取 Top-N 前声明最低余弦相似度；旧Profile没有该字段时保持原行为。概览
        问题只会从已经通过门槛的节点中为文档级 Summary 保留导航名额，不能用强制保留
        绕过相关性门槛。最终引用文本、页码和 Chunk ID 始终由 RAGFlow 原始块提供。
        """
        raw = profile.get("summary_index")
        if (
            self._index_nodes is None
            or self._embedding is None
            or not isinstance(raw, dict)
            or not bool(raw.get("enabled", False))
        ):
            return ()
        node_types: list[IndexNodeType] = [IndexNodeType.SUMMARY]
        if bool(raw.get("include_sections", False)):
            node_types.append(IndexNodeType.SECTION)
        if bool(raw.get("include_contextual_parents", False)):
            node_types.append(IndexNodeType.PARENT)
        nodes = await self._index_nodes.list_for_knowledge_bases(
            security.security_scope_id,
            knowledge_base_ids,
            node_types=tuple(node_types),
            limit=int(raw.get("candidate_nodes", 500)),
        )
        if document_ids:
            allowed_documents = set(document_ids)
            nodes = tuple(node for node in nodes if node.document_id in allowed_documents)
        nodes = tuple(node for node in nodes if node.embedding and node.source_chunk_ids)
        if not nodes:
            return ()
        try:
            query_vectors = await self._embedding.embed((query,))
            query_vector = query_vectors[0]
            ranked = sorted(
                (
                    (_cosine_similarity(query_vector, node.embedding), node)
                    for node in nodes
                ),
                key=lambda item: item[0],
                reverse=True,
            )
        except Exception:
            return ()
        if "similarity_threshold" in raw:
            threshold = _number(raw, "similarity_threshold")
            if not 0.0 <= threshold <= 1.0:
                raise RagError(
                    "RAG-CONFIG-002",
                    "配置 Profile Schema 无效",
                    http_status=422,
                    retryable=False,
                )
            # 侧车索引必须先判断绝对相关性，再取相对排名。否则当所有节点都与问题
            # 无关时，Top-N 仍会强行选出“最不差”的节点并通过 Parent-Child 扩展正文。
            ranked = [item for item in ranked if item[0] >= threshold]
            if not ranked:
                return ()
        top_nodes = int(raw.get("top_nodes", 4))
        selected_ranked = ranked[:top_nodes]
        if query_type == "summary":
            # 概览型问题优先保留一个已经通过门槛的文档级摘要。把合格 Summary 放在
            # 首位还能让同一源 Chunk 同时属于 Summary/Section 时优先保留 document
            # 路线，但绝不能从已过滤节点中重新捞回低相关摘要。
            best_document_summary = next(
                (
                    item
                    for item in ranked
                    if item[1].node_type == IndexNodeType.SUMMARY
                ),
                None,
            )
            if best_document_summary is not None:
                selected_ranked = [
                    best_document_summary,
                    *(
                        item
                        for item in ranked
                        if item[1] is not best_document_summary[1]
                    ),
                ][:top_nodes]
        selected_nodes = tuple(item[1] for item in selected_ranked)
        chunks = await self._source_candidates(
            security,
            selected_nodes,
            max_chunks_per_node=int(raw.get("source_chunks_per_node", 4)),
        )
        document_weight = float(raw.get("rrf_weight", 0.85))
        section_weight = float(raw.get("section_rrf_weight", document_weight))
        parent_weight = float(raw.get("contextual_parent_rrf_weight", section_weight))
        table_parent_weight = float(raw.get("table_parent_rrf_weight", parent_weight))

        def route_and_weight(candidate: RetrievalCandidate) -> tuple[str, float]:
            """把侧车节点类型转换为稳定路线名称和 Profile 权重。

            电子表格 Parent 可使用独立权重，因为其侧车内容带有重复表头；
            返回的候选正文仍是 RAGFlow Chunk，不会把这段前缀展示给用户。
            """
            node_type = candidate.metadata.get("sidecar_node_type")
            if node_type == IndexNodeType.SECTION.value:
                return "summary:section", section_weight
            if node_type == IndexNodeType.PARENT.value:
                if candidate.metadata.get("sidecar_node_kind") == "table":
                    return "context:table", table_parent_weight
                return "context:parent", parent_weight
            return "summary:document", document_weight

        return tuple(
            replace(
                candidate,
                score=route_and_weight(candidate)[1] / (rrf_k + rank),
                metadata=_metadata_with_retrieval_routes(
                    candidate.metadata,
                    (route_and_weight(candidate)[0],),
                    sidecar_route=route_and_weight(candidate)[0],
                ),
            )
            for rank, candidate in enumerate(chunks, start=1)
        )

    async def _parent_child_candidates(
        self,
        security: SecurityContext,
        candidates: tuple[RetrievalCandidate, ...],
        knowledge_base_ids: tuple[UUID, ...],
        profile: dict[str, Any],
        *,
        rrf_k: int,
    ) -> tuple[RetrievalCandidate, ...]:
        """根据已召回子块查找 Parent，并补回同组相邻原始 Chunk。

        Parent 节点本身不参与引用；它只是“哪些原始块属于同一局部章节”的映射。
        每次扩展的 Parent 数和兄弟块数都由 Profile 限制，避免一个命中块把整篇文档
        拉入上下文。已有候选会在路线合并时叠加 RRF 分，新兄弟块则接受后续统一
        去重、单文档配额和真实 Cross-Encoder Rerank。
        """
        raw = profile.get("parent_child")
        if (
            self._index_nodes is None
            or not candidates
            or not isinstance(raw, dict)
            or not bool(raw.get("enabled", False))
        ):
            return ()
        parents = await self._index_nodes.find_parents_for_chunks(
            security.security_scope_id,
            knowledge_base_ids,
            tuple(item.engine_chunk_id for item in candidates),
            limit=int(raw.get("max_parents", 3)),
        )
        if not parents:
            return ()
        chunks = await self._source_candidates(
            security,
            parents,
            max_chunks_per_node=int(raw.get("max_siblings_per_parent", 4)),
        )
        weight = float(raw.get("rrf_weight", 0.70))
        return tuple(
            replace(
                candidate,
                score=weight / (rrf_k + rank),
                metadata=_metadata_with_retrieval_routes(
                    candidate.metadata,
                    ("parent:children",),
                    sidecar_route="parent:children",
                ),
            )
            for rank, candidate in enumerate(chunks, start=1)
        )

    async def _entity_graph_candidates(
        self,
        security: SecurityContext,
        query: str,
        knowledge_base_ids: tuple[UUID, ...],
        document_ids: tuple[UUID, ...],
        profile: dict[str, Any],
        *,
        rrf_k: int,
    ) -> tuple[RetrievalCandidate, ...]:
        """用实体/显式关系命中源 ID 回读原始 Chunk，形成一条可解释 RRF 路线。

        Repository 已按租户、知识库和 READY 文档过滤。这里构造的临时节点只用于
        复用统一的源 Chunk 回读逻辑，节点正文不会进入答案；最终候选仍携带 RAGFlow
        原文、页码和稳定块 ID，并继续经过统一去重、配额、Rerank 与引用校验。
        """
        raw = profile.get("entity_graph")
        if (
            self._entity_graph is None
            or not isinstance(raw, dict)
            or not bool(raw.get("enabled", False))
        ):
            return ()
        evidence = await self._entity_graph.search_evidence(
            security.security_scope_id,
            knowledge_base_ids,
            query,
            limit=int(raw.get("max_evidence", 12)),
        )
        if document_ids:
            allowed_documents = set(document_ids)
            evidence = tuple(
                item for item in evidence if item.document_id in allowed_documents
            )
        if not evidence:
            return ()
        nodes = tuple(
            DocumentIndexNode(
                security_scope_id=security.security_scope_id,
                knowledge_base_id=item.knowledge_base_id,
                document_id=item.document_id,
                node_type=IndexNodeType.PARENT,
                ordinal=rank,
                content=item.matched_text,
                source_chunk_ids=item.source_chunk_ids,
                metadata={"evidence_type": item.evidence_type},
            )
            for rank, item in enumerate(evidence)
        )
        chunks = await self._source_candidates(
            security,
            nodes,
            max_chunks_per_node=int(raw.get("source_chunks_per_evidence", 2)),
        )
        weight = float(raw.get("rrf_weight", 0.75))
        return tuple(
            replace(
                candidate,
                score=weight / (rrf_k + rank),
                metadata=_metadata_with_retrieval_routes(
                    candidate.metadata,
                    ("entity:graph",),
                    sidecar_route="entity:graph",
                ),
            )
            for rank, candidate in enumerate(chunks, start=1)
        )

    async def _source_candidates(
        self,
        security: SecurityContext,
        nodes: tuple[DocumentIndexNode, ...],
        *,
        max_chunks_per_node: int,
    ) -> tuple[RetrievalCandidate, ...]:
        """按层级节点源 ID 回读 RAGFlow Chunk，并映射为标准可引用候选。

        RAGFlow 当前公开 API 没有“按一批 Chunk ID”读取接口，因此按文档分页读取，
        一旦找到所有需要的 ID 就停止。稳定 ``chunk_id`` 使用与 RAGFlow Adapter
        完全相同的哈希公式，使同一块在基础路线和侧车路线中能够正确合并分数。
        """
        if not nodes or max_chunks_per_node <= 0:
            return ()
        document_ids = tuple(dict.fromkeys(node.document_id for node in nodes))
        documents = await self._documents.get_many(
            security.security_scope_id,
            document_ids,
        )
        document_map = {document.id: document for document in documents}
        requested_by_document: defaultdict[UUID, list[str]] = defaultdict(list)
        source_node_by_chunk: dict[tuple[UUID, str], DocumentIndexNode] = {}
        for node in nodes:
            for source_chunk_id in node.source_chunk_ids[:max_chunks_per_node]:
                requested_by_document[node.document_id].append(source_chunk_id)
                # ``nodes`` 已按向量相似度排序。一个真实块同时属于文档摘要和章节时，
                # 保留最先命中的节点作为本次路线来源，避免同一块重复进入候选集合。
                source_node_by_chunk.setdefault((node.document_id, source_chunk_id), node)
        result: list[RetrievalCandidate] = []
        for document_id, requested_values in requested_by_document.items():
            document = document_map.get(document_id)
            if document is None or document.status != DocumentStatus.READY:
                continue
            requested = set(requested_values)
            page_number = 1
            while requested:
                page = await self._rag_engine.list_chunks(
                    ListEngineChunksQuery(
                        security=security,
                        knowledge_base_id=document.knowledge_base_id,
                        document_id=document.id,
                        page=page_number,
                        page_size=100,
                    )
                )
                current = tuple(item for item in page.items if isinstance(item, EngineChunk))
                for chunk in current:
                    if chunk.engine_chunk_id not in requested:
                        continue
                    requested.remove(chunk.engine_chunk_id)
                    stable_chunk_id = hashlib.sha256(
                        f"{document.id}\x1f{chunk.engine_chunk_id}".encode()
                    ).hexdigest()
                    result.append(
                        RetrievalCandidate(
                            chunk_id=stable_chunk_id,
                            engine_chunk_id=chunk.engine_chunk_id,
                            knowledge_base_id=document.knowledge_base_id,
                            document_id=document.id,
                            document_name=document.filename,
                            content=chunk.content,
                            score=0.0,
                            resource_scope_id=security.security_scope_id,
                            positions=chunk.positions,
                            metadata={
                                **dict(chunk.metadata),
                                "sidecar_node_type": source_node_by_chunk[
                                    (document.id, chunk.engine_chunk_id)
                                ].node_type.value,
                                "sidecar_node_ordinal": source_node_by_chunk[
                                    (document.id, chunk.engine_chunk_id)
                                ].ordinal,
                                # 节点正文不进入候选，只传递粗粒度类型和前缀
                                # 版本供路由、日志和验收使用。这两个字段不包含正文，
                                # 也不会绕过知识库/文档权限边界。
                                "sidecar_node_kind": source_node_by_chunk[
                                    (document.id, chunk.engine_chunk_id)
                                ].metadata.get("segment_kind"),
                                "sidecar_context_prefix_version": source_node_by_chunk[
                                    (document.id, chunk.engine_chunk_id)
                                ].metadata.get("context_prefix_version"),
                            },
                        )
                    )
                if not current or page_number * page.page_size >= page.total:
                    break
                page_number += 1
        return tuple(result)

    @staticmethod
    def _postprocess(
        candidates: tuple[RetrievalCandidate, ...],
        *,
        max_chunks_per_document: int,
        limit: int,
    ) -> tuple[RetrievalCandidate, ...]:
        """按规范化内容摘要去重，限制单文档块数，并截取候选上限。"""
        result: list[RetrievalCandidate] = []
        content_hashes: set[str] = set()
        per_document: Counter[UUID] = Counter()
        for candidate in candidates:
            normalized_content = " ".join(candidate.content.split())
            digest = hashlib.sha256(normalized_content.encode("utf-8")).hexdigest()
            if digest in content_hashes:
                continue
            if per_document[candidate.document_id] >= max_chunks_per_document:
                continue
            content_hashes.add(digest)
            per_document[candidate.document_id] += 1
            result.append(candidate)
            if len(result) >= limit:
                break
        return tuple(result)

    async def _semantic_deduplicate(
        self,
        candidates: tuple[RetrievalCandidate, ...],
        *,
        threshold: float,
    ) -> tuple[tuple[RetrievalCandidate, ...], str | None]:
        """用候选文本向量删除近义重复块，并保持原始相关性排序。

        算法按 RRF 排名顺序逐个处理：当前候选与所有已保留候选的余弦相似度只要
        达到阈值，就视为同一证据的近似副本。保留靠前者意味着不会让去重改变“更
        相关候选优先”的基础排序。任何模型、网络、数量或零向量异常都会返回原始
        候选和可观测警告，确保这个质量优化永远不会破坏基础检索可用性。
        """
        if self._embedding is None or len(candidates) < 2:
            return candidates, None
        if not 0.0 < threshold <= 1.0:
            return candidates, "semantic_candidate_dedup_invalid_threshold"
        try:
            vectors = await self._embedding.embed(tuple(item.content for item in candidates))
            if len(vectors) != len(candidates):
                raise ValueError("embedding result count does not match candidates")
            kept_candidates: list[RetrievalCandidate] = []
            kept_vectors: list[tuple[float, ...]] = []
            for candidate, vector in zip(candidates, vectors, strict=True):
                if not vector or any(not math.isfinite(value) for value in vector):
                    raise ValueError("embedding contains empty or non-finite vector")
                if any(
                    _cosine_similarity(vector, existing_vector) >= threshold
                    for existing_vector in kept_vectors
                ):
                    continue
                kept_candidates.append(candidate)
                kept_vectors.append(vector)
            return tuple(kept_candidates), None
        except Exception:
            return candidates, "semantic_candidate_dedup_bypassed"

    def _context_candidates(
        self,
        candidates: tuple[RetrievalCandidate, ...],
        retrieval_profile: str,
        query_type: str = "fact",
    ) -> tuple[RetrievalCandidate, ...]:
        """按问题类型、粗略 Token 预算和块数上限选择最终证据。

        事实问答通常只需少量精确块，概览、比较和多跳问题则需要覆盖更多来源；新
        Profile 可分别声明 ``context_top_n``。旧 Profile 没有自适应配置时完全沿用
        原固定值，因此历史缓存边界和回滚行为不会被悄悄改变。
        """
        profile = self._profiles.get("RETRIEVAL", retrieval_profile)
        postprocess = _mapping(profile, "postprocess")
        adaptive = _adaptive_retrieval_strategy(profile, query_type)
        maximum = int(adaptive.get("context_top_n", _integer(postprocess, "context_top_n")))
        token_budget = _integer(postprocess, "max_context_tokens")
        selected: list[RetrievalCandidate] = []
        estimated_tokens = 0
        for candidate in candidates:
            candidate_tokens = max(1, len(candidate.content) // 4)
            if estimated_tokens + candidate_tokens > token_budget:
                continue
            selected.append(candidate)
            estimated_tokens += candidate_tokens
            if len(selected) >= maximum:
                break
        return tuple(selected)

    def _answer_request(
        self,
        value: AnswerInput,
        query: str,
        context: tuple[RetrievalCandidate, ...],
    ) -> LlmRequest:
        """把编号证据、历史和问题填入版本化 Prompt，构造可复现 LLM 请求。"""
        prompt_profile = self._profiles.get("PROMPT", _ANSWER_PROMPT_PROFILE)
        model_profile = self._profiles.get("MODEL", value.generation_profile)
        response_language = _response_language_name(value.response_language)
        postprocess = _mapping(
            self._profiles.get("RETRIEVAL", value.retrieval_profile),
            "postprocess",
        )
        context_text = self._context_text(
            context,
            merge_adjacent=bool(postprocess.get("merge_adjacent_chunks", False)),
        )
        conversation = "\n".join(f"{item.role}: {item.content}" for item in value.messages)
        user_prompt = _template(
            _string(prompt_profile, "template"),
            context_text=context_text,
            conversation_text=conversation,
            query=query,
            response_language=response_language,
        )
        return LlmRequest(
            messages=(
                LlmMessage(
                    "system",
                    f"{_string(prompt_profile, 'system')}\n{_string(prompt_profile, 'rules')}",
                ),
                LlmMessage("user", user_prompt),
            ),
            model=self._llm_settings.model,
            max_output_tokens=_integer(model_profile, "max_output_tokens"),
            temperature=_number(model_profile, "temperature"),
            top_p=float(model_profile.get("top_p", 0.8)),
        )

    @staticmethod
    def _context_text(
        context: tuple[RetrievalCandidate, ...],
        *,
        merge_adjacent: bool,
    ) -> str:
        """生成带引用标签的上下文，并按文本重叠合并真正相邻的候选块。

        RAGFlow 的 10% Chunk overlap 会让相邻块共享一段完全相同的尾首文本。开启
        合并后，本方法寻找“同一文档、页码相邻且尾首文字重叠”的候选，删除重复
        区域后只向 LLM 发送一次。被合并块的标签全部保留，例如 ``[C1][C3]``，所以
        LLM 仍能引用每个原始块，引用详情也继续指向真实 RAGFlow Chunk，而不是伪造
        一个无法回查的合成块。
        """
        if not merge_adjacent:
            return "\n\n".join(
                f"[C{index}] 文档：{item.document_name}\n{item.content}"
                for index, item in enumerate(context, start=1)
            )

        indexed = list(enumerate(context, start=1))
        consumed: set[int] = set()
        blocks: list[str] = []
        for base_index, base in indexed:
            if base_index in consumed:
                continue
            consumed.add(base_index)
            labels = [base_index]
            merged_content = base.content.strip()
            # 一个上下文最多只有 Profile 限制的少量候选，逐对比较比额外访问下游
            # 读取全量文档更稳定，也不会为了合并把未召回证据绕过检索边界加入回答。
            changed = True
            while changed:
                changed = False
                for candidate_index, candidate in indexed:
                    if candidate_index in consumed or candidate.document_id != base.document_id:
                        continue
                    if not _pages_are_adjacent(base, candidate):
                        continue
                    forward = _merge_overlapping_text(merged_content, candidate.content)
                    if forward is not None:
                        merged_content = forward
                        labels.append(candidate_index)
                    else:
                        backward = _merge_overlapping_text(candidate.content, merged_content)
                        if backward is None:
                            continue
                        merged_content = backward
                        labels.insert(0, candidate_index)
                    consumed.add(candidate_index)
                    changed = True
                    break
            label_text = "".join(f"[C{label}]" for label in labels)
            blocks.append(f"{label_text} 文档：{base.document_name}\n{merged_content}")
        return "\n\n".join(blocks)

    @staticmethod
    def _citations(
        context: tuple[RetrievalCandidate, ...],
    ) -> tuple[Citation, ...]:
        """按上下文顺序生成 C1、C2… 引用，并计算页码范围。"""
        result: list[Citation] = []
        for index, item in enumerate(context, start=1):
            pages = [position.page for position in item.positions if position.page is not None]
            result.append(
                Citation(
                    citation_id=uuid4(),
                    label=f"C{index}",
                    knowledge_base_id=item.knowledge_base_id,
                    resource_scope_id=item.resource_scope_id,
                    document_id=item.document_id,
                    document_name=item.document_name,
                    engine_chunk_id=item.engine_chunk_id,
                    quote=item.content,
                    page_start=min(pages) if pages else None,
                    page_end=max(pages) if pages else None,
                    positions=item.positions,
                    score=item.score,
                    rerank_score=item.rerank_score,
                )
            )
        return tuple(result)

    async def _validate_or_repair(
        self,
        answer: str,
        original_request: LlmRequest,
        citations: tuple[Citation, ...],
        response_language: str,
    ) -> tuple[str, bool]:
        """检查答案引用标签；缺失/越界时最多调用一次确定性修复 Prompt。"""
        if self._all_labels_valid(answer, citations) and _CITATION_LABEL.search(answer):
            return answer, True
        if (
            not self._answer_settings.retry_generation_when_missing_citations
            or self._answer_settings.max_citation_repair_attempts < 1
        ):
            return answer, False
        profile = self._profiles.get("PROMPT", _CITATION_REPAIR_PROMPT_PROFILE)
        response_language_name = _response_language_name(response_language)
        repaired = await self._llm.complete(
            LlmRequest(
                messages=(
                    LlmMessage("system", _string(profile, "system")),
                    LlmMessage(
                        "user",
                        _template(
                            _string(profile, "template"),
                            allowed_citation_labels=", ".join(item.label for item in citations),
                            answer=answer,
                            response_language=response_language_name,
                        ),
                    ),
                ),
                model=original_request.model,
                max_output_tokens=original_request.max_output_tokens,
                temperature=0.0,
            )
        )
        valid = self._all_labels_valid(repaired.content, citations) and bool(
            _CITATION_LABEL.search(repaired.content)
        )
        return repaired.content, valid

    @staticmethod
    def _all_labels_valid(
        answer: str,
        citations: tuple[Citation, ...],
    ) -> bool:
        """确认答案出现的每个 ``[Cn]`` 都存在于允许引用集合。"""
        available = {item.label for item in citations}
        return all(f"C{match}" in available for match in _CITATION_LABEL.findall(answer))

    @staticmethod
    def _used_citations(
        answer: str,
        citations: tuple[Citation, ...],
    ) -> tuple[Citation, ...]:
        """只保留答案文本实际提及的引用，避免返回未使用证据。"""
        labels = {f"C{match}" for match in _CITATION_LABEL.findall(answer)}
        return tuple(item for item in citations if item.label in labels)


def _query_language(query: str) -> str:
    """以是否包含中日韩统一表意文字快速区分中文或英文查询。"""
    return _LANGUAGE_ZH if _CJK_CHARACTER.search(query) else _LANGUAGE_EN


def _query_plan_trace(value: RetrievalOutput | AnswerOutput) -> dict[str, Any]:
    """抽取可 JSON 化的查询规划与三阶段预算，供 REST 和 SSE 统一展示。

    这里输出的是本次请求真正采用的配置，不是最终候选数量。旧缓存可能没有新增
    字段，此时保留 ``None``，让客户端明确区分“历史数据未知”和“预算为零”。
    """
    return {
        "detected_language": value.query_language,
        "variants": dict(value.query_variants),
        "knowledge_languages": list(value.knowledge_languages),
        "retrieval_routes": list(value.retrieval_routes),
        "query_type": value.query_type,
        "retrieval_top_k": value.retrieval_top_k,
        "rerank_top_n": value.rerank_top_n,
        "context_top_n": value.context_top_n,
    }


def _query_was_rewritten(value: RetrievalOutput | AnswerOutput) -> bool:
    """判断任一实际查询变体是否不同于客户端原问题。"""
    if value.query_variants:
        return any(query != value.original_query for query in value.query_variants.values())
    return value.original_query != value.effective_query


def _query_plan(
    *,
    original_query: str,
    detected_language: str,
    query_zh: str | None,
    query_en: str | None,
    language_profile: KnowledgeLanguageProfile,
) -> QueryPlan:
    """组合查询变体、识别查询类型，并选择知识库主语种对应的生效查询。"""
    if language_profile.primary_language == _LANGUAGE_ZH and query_zh:
        effective_query = query_zh
    elif language_profile.primary_language == _LANGUAGE_EN and query_en:
        effective_query = query_en
    elif detected_language == _LANGUAGE_ZH and query_en:
        effective_query = query_en
    else:
        effective_query = original_query
    return QueryPlan(
        original_query=original_query,
        detected_language=detected_language,
        query_zh=query_zh,
        query_en=query_en,
        knowledge_languages=language_profile.languages,
        effective_query=effective_query,
        query_type=_classify_query(
            " ".join(item for item in (original_query, query_zh, query_en) if item)
        ),
    )


def _classify_query(value: str) -> str:
    """用透明、可回滚的中英文规则把问题分为五类检索需求。

    分类只决定召回数量，不决定答案内容，也不会绕过知识库权限或无依据拒答。规则
    优先识别需要广覆盖的概览、比较和多跳问题，再识别列表型问题，其余归为事实型。
    同时检查原问题和双语改写文本，使中文问英文库、英文问中文库时得到一致策略。
    """
    normalized = value.casefold()
    keyword_groups: tuple[tuple[str, tuple[str, ...]], ...] = (
        (
            "summary",
            (
                "总结",
                "概述",
                "摘要",
                "主要内容",
                "整体介绍",
                "summarize",
                "summary",
                "overview",
                "main points",
            ),
        ),
        (
            "comparison",
            (
                "比较",
                "区别",
                "差异",
                "异同",
                "优缺点",
                "compare",
                "comparison",
                "difference",
                "versus",
                " vs ",
            ),
        ),
        (
            "multi_hop",
            (
                "为什么",
                "原因和影响",
                "关系是什么",
                "如何影响",
                "结合",
                "综合",
                "完整流程",
                "串联",
                "why",
                "relationship between",
                "how does",
                "cause and effect",
                "combine ",
                "combined ",
                "together",
                "end-to-end",
                "full process",
            ),
        ),
        (
            "list",
            (
                "列出",
                "有哪些",
                "哪几",
                "多少种",
                "分别是什么",
                "list ",
                "what are the",
                "which ",
                "enumerate",
            ),
        ),
    )
    for query_type, keywords in keyword_groups:
        if any(keyword in normalized for keyword in keywords):
            return query_type
    return "fact"


def _parse_bilingual_query_response(
    content: str,
    *,
    original_query: str,
    detected_language: str,
    max_chars: int,
) -> tuple[str | None, str | None]:
    """宽容解析 LLM 的双语 JSON，同时兼容代码围栏和旧版纯文本输出。

    无论 LLM 是否完整返回两种语言，原查询所在语种的版本始终保留；解析到的文本会
    再次走 ``normalize_query``，防止模型输出控制字符或超长内容。
    """
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", stripped, flags=re.I)
    start = stripped.find("{")
    end = stripped.rfind("}")
    payload: dict[str, Any] | None = None
    if start >= 0 and end > start:
        try:
            decoded = json.loads(stripped[start : end + 1])
            if isinstance(decoded, dict):
                payload = decoded
        except json.JSONDecodeError:
            payload = None

    query_zh: str | None = original_query if detected_language == _LANGUAGE_ZH else None
    query_en: str | None = original_query if detected_language == _LANGUAGE_EN else None
    if payload is not None:
        raw_zh = payload.get("query_zh")
        raw_en = payload.get("query_en")
        if isinstance(raw_zh, str) and raw_zh.strip():
            query_zh = normalize_query(raw_zh, max_chars)
        if isinstance(raw_en, str) and raw_en.strip():
            query_en = normalize_query(raw_en, max_chars)
    elif stripped:
        legacy = normalize_query(stripped, max_chars)
        if _query_language(legacy) == _LANGUAGE_ZH:
            query_zh = legacy
        else:
            query_en = legacy
    return query_zh, query_en


def _document_language_counts(documents: Sequence[Any]) -> tuple[int, int, int]:
    """从文档 metadata 的 language/lang 字段统计中文、英文和未知数量。"""
    zh_count = 0
    en_count = 0
    unknown_count = 0
    for document in documents:
        metadata = getattr(document, "metadata", None)
        raw_language = None
        if isinstance(metadata, dict):
            raw_language = metadata.get("language", metadata.get("lang"))
        language = _normalize_document_language(raw_language)
        if language == _LANGUAGE_ZH:
            zh_count += 1
        elif language == _LANGUAGE_EN:
            en_count += 1
        else:
            unknown_count += 1
    return zh_count, en_count, unknown_count


def _normalize_document_language(value: Any) -> str | None:
    """把常见中英文语种别名统一成 ``zh-CN``/``en-US``，其他值视为未知。"""
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower().replace("_", "-")
    if normalized in {"zh", "zh-cn", "zh-hans", "cn", "chinese", "中文", "zho"}:
        return _LANGUAGE_ZH
    if normalized in {"en", "en-us", "en-gb", "english", "eng"}:
        return _LANGUAGE_EN
    return None


def _valid_candidate(candidate: RetrievalCandidate) -> bool:
    """过滤缺少 ID/内容或综合得分为 NaN、无穷大的损坏候选。"""
    return bool(candidate.chunk_id and candidate.content.strip()) and math.isfinite(candidate.score)


def _merge_candidate_routes(
    primary: tuple[RetrievalCandidate, ...],
    sidecar: tuple[RetrievalCandidate, ...],
) -> tuple[RetrievalCandidate, ...]:
    """把侧车索引路线分数叠加到基础 RRF 候选，并按新综合分重新排序。

    两条路线指向同一真实 Chunk 时使用稳定 ``chunk_id`` 合并，不产生重复引用；仅
    由侧车路线发现的块则作为普通候选加入，随后仍经过统一去重、配额与 Rerank。
    向量/BM25 原始分保留在基础候选中，所有基础与侧车路线都会写入
    ``gateway_retrieval_routes``。兼容字段 ``sidecar_route`` 保留最早命中的侧车
    路线，不再被稍后执行的 Parent 扩展覆盖。
    """
    merged = {candidate.chunk_id: candidate for candidate in primary}
    for candidate in sidecar:
        existing = merged.get(candidate.chunk_id)
        if existing is None:
            merged[candidate.chunk_id] = candidate
            continue
        merged[candidate.chunk_id] = replace(
            existing,
            score=existing.score + candidate.score,
            metadata=_merge_candidate_metadata(existing.metadata, candidate.metadata),
        )
    return tuple(sorted(merged.values(), key=lambda item: item.score, reverse=True))


def _metadata_with_retrieval_routes(
    metadata: dict[str, Any],
    route_names: Sequence[str],
    *,
    sidecar_route: str | None = None,
) -> dict[str, Any]:
    """返回附带完整 Gateway 命中路线的新 Metadata，且不修改输入字典。

    ``gateway_retrieval_routes`` 是应用层诊断字段，按首次命中顺序列出向量、BM25、
    Summary、Section、实体和 Parent 路线。RAGFlow 原始 Metadata 仍原样保留；即使
    外部数据意外提供同名但格式错误的字段，也只忽略损坏值，不影响正文检索。
    ``sidecar_route`` 是旧客户端使用的单值兼容字段，只在候选第一次通过侧车导航时
    写入，避免后执行的 Parent 路线掩盖真正把候选带入结果集的 Summary 或实体路线。
    """
    result = dict(metadata)
    existing_routes = result.get("gateway_retrieval_routes")
    normalized_routes: list[str] = []
    if isinstance(existing_routes, (list, tuple)):
        normalized_routes.extend(
            item for item in existing_routes if isinstance(item, str) and item
        )
    elif isinstance(existing_routes, str) and existing_routes:
        normalized_routes.append(existing_routes)
    for route_name in route_names:
        if route_name and route_name not in normalized_routes:
            normalized_routes.append(route_name)
    # 显式传入的路线已经按真实执行先后排好；旧单值字段只作为缺失信息的补充，
    # 必须最后追加，否则字典合并残留的“最后路线”会再次跑到数组首位。
    legacy_sidecar_route = result.get("sidecar_route")
    if (
        isinstance(legacy_sidecar_route, str)
        and legacy_sidecar_route
        and legacy_sidecar_route not in normalized_routes
    ):
        normalized_routes.append(legacy_sidecar_route)
    result["gateway_retrieval_routes"] = normalized_routes
    if sidecar_route is not None and not result.get("sidecar_route"):
        result["sidecar_route"] = sidecar_route
    return result


def _merge_candidate_metadata(
    primary: dict[str, Any],
    additional: dict[str, Any],
) -> dict[str, Any]:
    """合并同一 Chunk 的两份 Metadata，并保留全部检索路线来源。

    一条候选常先由基础向量/BM25 找到，再被 Summary、实体和 Parent 路线重复命中。
    普通字典覆盖会只剩最后一条路线。本函数先合并普通字段，再把两边的路线集合按
    稳定顺序去重；若主候选已有 ``sidecar_route``，则继续保留它作为最早侧车来源。
    """
    merged = {**primary, **additional}
    # 普通字典展开后，这个数组暂时等于 additional 的值；如果直接在它上面追加，
    # 后执行路线就会错误排到最前。先移除临时覆盖值，再严格按 primary → additional
    # 重建，确保“首次命中路线”在接口响应和兼容字段中保持稳定。
    merged.pop("gateway_retrieval_routes", None)
    primary_routes = _metadata_with_retrieval_routes(primary, ()).get(
        "gateway_retrieval_routes",
        [],
    )
    additional_routes = _metadata_with_retrieval_routes(additional, ()).get(
        "gateway_retrieval_routes",
        [],
    )
    merged = _metadata_with_retrieval_routes(
        merged,
        (*primary_routes, *additional_routes),
    )
    if primary.get("sidecar_route"):
        merged["sidecar_route"] = primary["sidecar_route"]
    return merged


def _cosine_similarity(
    left: Sequence[float],
    right: Sequence[float],
) -> float:
    """计算两个同维向量的余弦相似度，并拒绝无法比较的输入。

    余弦值只反映向量方向是否接近，不受文本长度导致的向量模长影响。Embedding
    服务通常会返回归一化向量，但这里仍显式计算两个模长，避免把供应商实现细节
    当作应用层契约。维度不一致或零向量说明上游结果损坏，抛出的异常会由调用方
    转换为“语义去重已绕过”警告并继续返回原始候选。
    """
    if len(left) != len(right) or not left:
        raise ValueError("cosine vectors must be non-empty and have equal dimensions")
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm == 0.0 or right_norm == 0.0:
        raise ValueError("cosine vectors must not be zero vectors")
    return dot / (left_norm * right_norm)


def _pages_are_adjacent(
    left: RetrievalCandidate,
    right: RetrievalCandidate,
) -> bool:
    """判断两个同文档候选的已知页码是否相邻。

    没有页码时不能证明不相邻，后续仍会用严格的尾首文本重叠作最终判断；两边都
    有页码且相隔超过一页时则直接拒绝，避免恰好重复的页眉页脚把远距离内容合并。
    """
    left_pages = [item.page for item in left.positions if item.page is not None]
    right_pages = [item.page for item in right.positions if item.page is not None]
    if not left_pages or not right_pages:
        return True
    return min(
        abs(left_page - right_page)
        for left_page in left_pages
        for right_page in right_pages
    ) <= 1


def _merge_overlapping_text(left: str, right: str, *, minimum_overlap: int = 16) -> str | None:
    """在右文本前缀与左文本后缀完全重叠时返回去重后的拼接文本。

    只接受至少 16 个字符的逐字相同重叠，短词、标题或页眉不会触发合并。搜索从
    最大可能重叠向下进行，确保 10% 分块重叠中的全部重复文字只保留一次；没有
    可靠证据证明相邻时返回 ``None``，调用方继续把两个 Chunk 独立交给模型。
    """
    normalized_left = left.strip()
    normalized_right = right.strip()
    maximum = min(len(normalized_left), len(normalized_right))
    for overlap in range(maximum, minimum_overlap - 1, -1):
        if normalized_left[-overlap:] == normalized_right[:overlap]:
            return normalized_left + normalized_right[overlap:]
    return None


def _response_language_name(value: str) -> str:
    """把 API 语言码转换为 Prompt 中的清晰名称，并拒绝未支持语言。"""
    language = _RESPONSE_LANGUAGE_NAMES.get(value)
    if language is None:
        raise RagError(
            "RAG-QUERY-002",
            "不支持的响应语言",
            http_status=422,
            retryable=False,
        )
    return language


def _no_evidence_answer(response_language: str) -> str:
    """返回目标语言的固定“证据不足”答复。"""
    _response_language_name(response_language)
    return _NO_EVIDENCE_ANSWERS[response_language]


def _redact_query(value: str) -> str:
    """在审计落库前替换电子邮箱和六位以上数字，降低个人信息泄漏风险。"""
    value = re.sub(
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
        "[email]",
        value,
    )
    return re.sub(r"\b[0-9]{6,}\b", "[number]", value)


def _mapping(value: dict[str, Any], key: str) -> dict[str, Any]:
    """从 Profile 读取必需对象字段，类型不符时报告统一配置错误。"""
    result = value.get(key)
    if not isinstance(result, dict):
        raise RagError(
            "RAG-CONFIG-002",
            "配置 Profile Schema 无效",
            http_status=422,
            retryable=False,
        )
    return result


def _rrf_configuration(profile: dict[str, Any]) -> dict[str, Any]:
    """读取 RRF 段；旧 Profile 未定义时返回与历史行为一致的默认权重副本。"""
    result = profile.get("rrf")
    if result is None:
        return dict(_LEGACY_RRF_CONFIGURATION)
    if not isinstance(result, dict):
        raise RagError(
            "RAG-CONFIG-002",
            "配置 Profile Schema 无效",
            http_status=422,
            retryable=False,
        )
    return result


def _adaptive_retrieval_strategy(
    profile: dict[str, Any],
    query_type: str,
) -> dict[str, Any]:
    """从新 Profile 读取某类问题的动态召回参数，旧 Profile 返回空配置。

    返回空字典代表调用方继续使用 ``ragflow.top_k`` 和 ``postprocess`` 固定值。
    新 Profile 必须显式 ``enabled: true`` 且对应策略是对象才会生效；错误类型会以
    配置错误失败，而不是在运行中悄悄使用意外参数。这样旧版 ``@1/@2`` 保持原样，
    从 ``@3`` 起才启用按 fact/list/comparison/summary/multi_hop 调整召回深度的
    行为；后续多粒度 Profile 继续复用同一配置结构。
    """
    raw = profile.get("adaptive_top_k")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise RagError(
            "RAG-CONFIG-002",
            "配置 Profile Schema 无效",
            http_status=422,
            retryable=False,
        )
    if not bool(raw.get("enabled", False)):
        return {}
    strategies = raw.get("strategies")
    if not isinstance(strategies, dict):
        raise RagError(
            "RAG-CONFIG-002",
            "配置 Profile Schema 无效",
            http_status=422,
            retryable=False,
        )
    selected = strategies.get(query_type, strategies.get("fact"))
    if not isinstance(selected, dict):
        raise RagError(
            "RAG-CONFIG-002",
            "配置 Profile Schema 无效",
            http_status=422,
            retryable=False,
        )
    return selected


def _integer(value: dict[str, Any], key: str) -> int:
    """读取严格整数配置；Python 的 ``bool`` 虽是 int 子类也明确拒绝。"""
    result = value.get(key)
    if not isinstance(result, int) or isinstance(result, bool):
        raise RagError(
            "RAG-CONFIG-002",
            "配置 Profile Schema 无效",
            http_status=422,
            retryable=False,
        )
    return result


def _number(value: dict[str, Any], key: str) -> float:
    """读取整数或浮点配置并统一为 float，同时拒绝布尔值。"""
    result = value.get(key)
    if not isinstance(result, int | float) or isinstance(result, bool):
        raise RagError(
            "RAG-CONFIG-002",
            "配置 Profile Schema 无效",
            http_status=422,
            retryable=False,
        )
    return float(result)


def _string(value: dict[str, Any], key: str) -> str:
    """读取必需字符串配置。"""
    result = value.get(key)
    if not isinstance(result, str):
        raise RagError(
            "RAG-CONFIG-002",
            "配置 Profile Schema 无效",
            http_status=422,
            retryable=False,
        )
    return result


def _template(template: str, **values: str) -> str:
    """替换 Profile 模板中带空格或不带空格的双花括号占位符。"""
    result = template
    for key, value in values.items():
        result = result.replace("{{ " + key + " }}", value)
        result = result.replace("{{" + key + "}}", value)
    return result


def stream_event_json(
    *,
    request_id: UUID,
    sequence: int,
    event: StreamEvent,
) -> str:
    """把应用事件编码为包含事件名、序号、时间和 JSON 数据的 SSE 帧。"""
    data: dict[str, Any] = {
        "request_id": str(request_id),
        "sequence": sequence,
        "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }
    for key, value in event.data.items():
        if isinstance(value, Citation):
            data[key] = _citation_json(value)
        elif isinstance(value, LlmUsage):
            data.update(
                {
                    "prompt_tokens": value.prompt_tokens,
                    "completion_tokens": value.completion_tokens,
                    "total_tokens": value.total_tokens,
                }
            )
        else:
            data[key] = value
    return (
        f"event: {event.event}\n"
        f"id: {sequence}\n"
        f"data: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"
    )


def _citation_json(value: Citation) -> dict[str, Any]:
    """将引用转换成 SSE 可直接 JSON 编码的字典。"""
    return {
        "citation_id": str(value.citation_id),
        "label": value.label,
        "knowledge_base_id": str(value.knowledge_base_id),
        "document_id": str(value.document_id),
        "document_name": value.document_name,
        "page_start": value.page_start,
        "page_end": value.page_end,
        "positions": [{"page": item.page, "bbox": item.bbox} for item in value.positions],
        "quote": value.quote,
        "score": value.score,
    }
