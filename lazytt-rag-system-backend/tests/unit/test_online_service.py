"""在线应用服务测试：双语路线、RRF、生成/引用、缓存、SSE、截止时间与审计隔离。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from rag_service.application.online import (
    AnswerInput,
    HistoryMessage,
    OnlineService,
    RetrievalInput,
)
from rag_service.application.profiles import ProfileCatalog
from rag_service.domain.errors import LlmError, ResourceNotFoundError
from rag_service.domain.models import (
    AnswerOutcome,
    Citation,
    DocumentIndexNode,
    DocumentRecord,
    DocumentStatus,
    FeedbackRecord,
    GraphEvidence,
    IndexNodeType,
    KnowledgeBase,
    KnowledgeBaseScopeType,
    KnowledgeBaseStatus,
    QueryRecord,
    QueryStatus,
    RetrievalCandidate,
    SourcePosition,
)
from rag_service.domain.ports import (
    CacheBoundary,
    CacheEntry,
    EngineChunk,
    EngineHealth,
    EngineRetrievalQuery,
    EngineRetrievalResult,
    ListEngineChunksQuery,
    LlmEvent,
    LlmRequest,
    LlmResponse,
    LlmUsage,
    Page,
    RerankOutcome,
    RerankPort,
)
from rag_service.infrastructure.models.rerank import DisabledRerankAdapter
from rag_service.security.context import SecurityContext, derive_system_resource_scope_id
from rag_service.settings import AnswersSection, ServiceSettings


class MemoryKnowledgeBases:
    def __init__(self, values: tuple[KnowledgeBase, ...]) -> None:
        self.values = values

    async def get_many(
        self,
        security_scope_id: str,
        knowledge_base_ids: Sequence[UUID],
    ) -> tuple[KnowledgeBase, ...]:
        requested = set(knowledge_base_ids)
        return tuple(
            item
            for item in self.values
            if item.security_scope_id == security_scope_id and item.id in requested
        )

    async def get_many_accessible(
        self,
        security_scope_ids: Sequence[str],
        knowledge_base_ids: Sequence[UUID],
    ) -> tuple[KnowledgeBase, ...]:
        requested = set(knowledge_base_ids)
        return tuple(
            item
            for item in self.values
            if item.security_scope_id in security_scope_ids and item.id in requested
        )

    async def list_ready_accessible(
        self,
        security_scope_ids: Sequence[str],
    ) -> tuple[KnowledgeBase, ...]:
        return tuple(
            item
            for item in self.values
            if item.security_scope_id in security_scope_ids
            and item.status == KnowledgeBaseStatus.READY
        )


class FakeEnabledReranker:
    """用固定真实分数模拟已启用 Cross-Encoder，验证应用层写回和排序分支。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    @property
    def enabled(self) -> bool:
        """说明该测试替身代表已经通过启动验证的服务。"""
        return True

    async def rerank(self, query: str, documents: Sequence[str]) -> RerankOutcome:
        """记录有效问题与候选正文，并返回与输入严格等长的合成分数。"""
        values = tuple(documents)
        self.calls.append((query, values))
        return RerankOutcome(
            scores=tuple(0.75 for _ in values),
            enabled=True,
        )


class FakeEmbedding:
    """返回预置候选向量，验证语义去重而不访问真实云端模型。"""

    def __init__(self, vectors: tuple[tuple[float, ...], ...]) -> None:
        """保存与候选文本顺序一一对应的合成向量。"""
        self.vectors = vectors
        self.requests: list[tuple[str, ...]] = []

    async def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        """记录批量输入并返回固定向量，数量错误会由生产逻辑安全降级。"""
        self.requests.append(tuple(texts))
        if len(self.vectors) == 1 and len(texts) > 1:
            return self.vectors * len(texts)
        return self.vectors


class FakeIndexNodes:
    """按测试场景返回预置 Summary 或 Parent 节点。"""

    def __init__(self, nodes: tuple[DocumentIndexNode, ...]) -> None:
        """保存当前测试可见的侧车节点集合。"""
        self.nodes = nodes

    async def list_for_knowledge_bases(
        self,
        security_scope_id: str,
        knowledge_base_ids: Sequence[UUID],
        *,
        node_types: Sequence[IndexNodeType],
        limit: int,
    ) -> tuple[DocumentIndexNode, ...]:
        """按租户、知识库和类型模拟摘要节点查询。"""
        allowed_kbs = set(knowledge_base_ids)
        allowed_types = set(node_types)
        return tuple(
            node
            for node in self.nodes
            if node.security_scope_id == security_scope_id
            and node.knowledge_base_id in allowed_kbs
            and node.node_type in allowed_types
        )[:limit]

    async def find_parents_for_chunks(
        self,
        security_scope_id: str,
        knowledge_base_ids: Sequence[UUID],
        source_chunk_ids: Sequence[str],
        *,
        limit: int,
    ) -> tuple[DocumentIndexNode, ...]:
        """返回包含任一当前召回 Chunk 的 Parent 节点。"""
        allowed_kbs = set(knowledge_base_ids)
        requested = set(source_chunk_ids)
        return tuple(
            node
            for node in self.nodes
            if node.security_scope_id == security_scope_id
            and node.knowledge_base_id in allowed_kbs
            and node.node_type == IndexNodeType.PARENT
            and requested.intersection(node.source_chunk_ids)
        )[:limit]


class FakeEntityGraph:
    """返回预置实体证据，验证在线路线只回读真实 RAGFlow Chunk。"""

    def __init__(self, evidence: tuple[GraphEvidence, ...]) -> None:
        """保存当前租户测试可见的实体/关系命中。"""
        self.evidence = evidence
        self.queries: list[str] = []

    async def search_evidence(
        self,
        security_scope_id: str,
        knowledge_base_ids: Sequence[UUID],
        query: str,
        *,
        limit: int,
    ) -> tuple[GraphEvidence, ...]:
        """记录双语合并问题，并按安全范围和知识库过滤预置证据。"""
        self.queries.append(query)
        allowed = set(knowledge_base_ids)
        assert len(security_scope_id) == 64
        return tuple(item for item in self.evidence if item.knowledge_base_id in allowed)[:limit]


class MemoryDocuments:
    def __init__(self, values: tuple[DocumentRecord, ...]) -> None:
        self.values = values

    async def get_many(
        self,
        security_scope_id: str,
        document_ids: Sequence[UUID],
    ) -> tuple[DocumentRecord, ...]:
        requested = set(document_ids)
        return tuple(
            item
            for item in self.values
            if item.security_scope_id == security_scope_id and item.id in requested
        )

    async def get_many_accessible(
        self,
        security_scope_ids: Sequence[str],
        document_ids: Sequence[UUID],
    ) -> tuple[DocumentRecord, ...]:
        requested = set(document_ids)
        return tuple(
            item
            for item in self.values
            if item.security_scope_id in security_scope_ids and item.id in requested
        )

    async def language_counts(
        self,
        security_scope_id: str,
        knowledge_base_ids: Sequence[UUID],
    ) -> dict[UUID, dict[str | None, int]]:
        requested = set(knowledge_base_ids)
        result: dict[UUID, dict[str | None, int]] = {}
        for item in self.values:
            if (
                item.security_scope_id != security_scope_id
                or item.knowledge_base_id not in requested
            ):
                continue
            raw_language = item.metadata.get("language", item.metadata.get("lang"))
            language = str(raw_language) if raw_language is not None else None
            counts = result.setdefault(item.knowledge_base_id, {})
            counts[language] = counts.get(language, 0) + 1
        return result

    async def list(
        self,
        security_scope_id: str,
        knowledge_base_id: UUID,
        *,
        page: int,
        page_size: int,
        status: str | None = None,
        filename: str | None = None,
        external_id: str | None = None,
    ) -> tuple[tuple[DocumentRecord, ...], int]:
        values = tuple(
            item
            for item in self.values
            if item.security_scope_id == security_scope_id
            and item.knowledge_base_id == knowledge_base_id
            and (status is None or item.status.value == status)
            and (filename is None or filename.lower() in item.filename.lower())
            and (external_id is None or item.external_id == external_id)
        )
        offset = (page - 1) * page_size
        return values[offset : offset + page_size], len(values)


class FakeRagEngine:
    def __init__(
        self,
        vector: tuple[RetrievalCandidate, ...],
        term: tuple[RetrievalCandidate, ...],
    ) -> None:
        self.vector = vector
        self.term = term
        self.queries: list[EngineRetrievalQuery] = []
        self.healthy = True
        self.chunks: tuple[EngineChunk, ...] = ()

    async def retrieve(self, query: EngineRetrievalQuery) -> EngineRetrievalResult:
        self.queries.append(query)
        values = self.vector if query.vector_similarity_weight == 1.0 else self.term
        return EngineRetrievalResult(candidates=values, total=len(values))

    async def health(self) -> EngineHealth:
        status = "ok" if self.healthy else "unavailable"
        return EngineHealth(status=status, dependencies={"db": status, "doc_engine": status})

    async def list_chunks(self, query: ListEngineChunksQuery) -> Page:
        """分页返回预置真实 Chunk，供侧车索引回读引用来源。"""
        offset = (query.page - 1) * query.page_size
        values = self.chunks[offset : offset + query.page_size]
        return Page(
            items=values,
            page=query.page,
            page_size=query.page_size,
            total=len(self.chunks),
        )


class FakeLlm:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.requests: list[LlmRequest] = []

    async def complete(self, request: LlmRequest) -> LlmResponse:
        self.requests.append(request)
        return LlmResponse(
            content=self.responses.pop(0),
            usage=LlmUsage(10, 5, 15),
        )

    def stream(self, request: LlmRequest) -> AsyncIterator[LlmEvent]:
        self.requests.append(request)

        async def events() -> AsyncIterator[LlmEvent]:
            yield LlmEvent(delta="合成事实")
            yield LlmEvent(delta=" [C1]")
            yield LlmEvent(usage=LlmUsage(10, 5, 15))

        return events()


class CancellingSlowLlm(FakeLlm):
    def __init__(self) -> None:
        super().__init__([])
        self.cancelled = False

    async def complete(self, request: LlmRequest) -> LlmResponse:
        self.requests.append(request)
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("slow LLM should have been cancelled")


class FakeCache:
    def __init__(self) -> None:
        self.entry: CacheEntry | None = None
        self.semantic_entry: CacheEntry | None = None
        self.puts: list[CacheEntry] = []
        self.hits: list[CacheEntry] = []
        self.fail = False

    async def get_exact(
        self,
        boundary: CacheBoundary,
        normalized_question: str,
    ) -> CacheEntry | None:
        if self.fail:
            raise RuntimeError("cache unavailable")
        if (
            self.entry is not None
            and self.entry.boundary == boundary
            and self.entry.normalized_question == normalized_question
        ):
            return self.entry
        return None

    async def get_semantic(
        self,
        boundary: CacheBoundary,
        normalized_question: str,
    ) -> tuple[CacheEntry, ...]:
        del normalized_question
        if self.fail:
            raise RuntimeError("cache unavailable")
        if self.semantic_entry is not None:
            return (self.semantic_entry,)
        return ()

    async def put(self, value: CacheEntry) -> None:
        if self.fail:
            raise RuntimeError("cache unavailable")
        self.entry = value
        self.puts.append(value)

    async def mark_hit(self, value: CacheEntry) -> None:
        if self.fail:
            raise RuntimeError("cache unavailable")
        self.hits.append(value)


class MemoryQueries:
    def __init__(self) -> None:
        self.records: dict[UUID, QueryRecord] = {}
        self.citations: dict[UUID, tuple[Citation, ...]] = {}
        self.feedback: list[FeedbackRecord] = []

    async def add(self, security_scope_id: str, value: QueryRecord) -> None:
        assert value.security_scope_id == security_scope_id
        self.records[value.request_id] = value

    async def get(
        self,
        security_scope_id: str,
        request_id: UUID,
    ) -> QueryRecord | None:
        value = self.records.get(request_id)
        return value if value is not None and value.security_scope_id == security_scope_id else None

    async def update(self, security_scope_id: str, value: QueryRecord) -> bool:
        if value.security_scope_id != security_scope_id:
            return False
        self.records[value.request_id] = value
        return True

    async def add_citations(
        self,
        security_scope_id: str,
        request_id: UUID,
        citations: Sequence[Citation],
    ) -> None:
        assert self.records[request_id].security_scope_id == security_scope_id
        self.citations[request_id] = tuple(citations)

    async def list_citations(
        self,
        security_scope_id: str,
        request_id: UUID,
    ) -> tuple[Citation, ...]:
        if await self.get(security_scope_id, request_id) is None:
            return ()
        return self.citations.get(request_id, ())

    async def get_citation(
        self,
        security_scope_id: str,
        citation_id: UUID,
    ) -> Citation | None:
        for request_id, values in self.citations.items():
            if await self.get(security_scope_id, request_id) is not None:
                for value in values:
                    if value.citation_id == citation_id:
                        return value
        return None

    async def add_feedback(
        self,
        security_scope_id: str,
        value: FeedbackRecord,
    ) -> None:
        assert value.security_scope_id == security_scope_id
        self.feedback.append(value)


def _security(tenant: str = "tenant") -> SecurityContext:
    return SecurityContext.create(
        subject_id="subject",
        tenant_id=tenant,
        rag_scope_id="scope",
        token_id=f"token-{tenant}",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )


def _resources(
    security: SecurityContext,
) -> tuple[KnowledgeBase, DocumentRecord, RetrievalCandidate]:
    knowledge_base = KnowledgeBase(
        security_scope_id=security.tenant_resource_scope_id,
        name="Synthetic KB",
        parser_profile_ref="deepdoc-default@1",
        chunk_profile_ref="general-512@1",
        embedding_profile_ref="embedding-openai-compatible@3",
        retrieval_profile_ref="hybrid-default@1",
        status=KnowledgeBaseStatus.READY,
        ragflow_dataset_id="engine-kb",
    )
    document = DocumentRecord(
        security_scope_id=security.tenant_resource_scope_id,
        knowledge_base_id=knowledge_base.id,
        filename="fixture.txt",
        safe_filename="fixture.txt",
        mime_type="text/plain",
        file_size_bytes=7,
        content_sha256="a" * 64,
        parser_profile_ref="deepdoc-default@1",
        chunk_profile_ref="general-512@1",
        metadata={"language": "en"},
        status=DocumentStatus.READY,
        ragflow_document_id="engine-document",
    )
    candidate = RetrievalCandidate(
        chunk_id="stable-chunk",
        engine_chunk_id="engine-chunk",
        knowledge_base_id=knowledge_base.id,
        document_id=document.id,
        document_name=document.filename,
        content="这是最小非业务合成证据。",
        score=0.9,
        resource_scope_id=security.tenant_resource_scope_id,
        vector_score=0.9,
        positions=(SourcePosition(page=1, bbox=(0, 0, 1, 1)),),
    )
    return knowledge_base, document, candidate


def _service(
    settings: ServiceSettings,
    *,
    llm_responses: list[str],
    candidates: bool = True,
    cache: FakeCache | None = None,
    queries: MemoryQueries | None = None,
    document_languages: tuple[str | None, ...] = ("en",),
    reranker: RerankPort | None = None,
    embedding: FakeEmbedding | None = None,
    index_nodes: FakeIndexNodes | None = None,
    entity_graph: FakeEntityGraph | None = None,
) -> tuple[OnlineService, FakeRagEngine, FakeLlm, KnowledgeBase, DocumentRecord]:
    security = _security()
    knowledge_base, document, candidate = _resources(security)
    document.metadata = (
        {"language": document_languages[0]} if document_languages[0] is not None else {}
    )
    documents = [document]
    for index, language in enumerate(document_languages[1:], start=2):
        documents.append(
            replace(
                document,
                id=uuid4(),
                filename=f"fixture-{index}.txt",
                safe_filename=f"fixture-{index}.txt",
                ragflow_document_id=f"engine-document-{index}",
                metadata={"language": language} if language is not None else {},
            )
        )
    values = (candidate,) if candidates else ()
    engine = FakeRagEngine(values, values)
    llm = FakeLlm(llm_responses)
    service = OnlineService(
        MemoryKnowledgeBases((knowledge_base,)),  # type: ignore[arg-type]
        MemoryDocuments(tuple(documents)),  # type: ignore[arg-type]
        engine,  # type: ignore[arg-type]
        llm,
        reranker or DisabledRerankAdapter(),
        ProfileCatalog.load(settings.profiles),
        settings.app,
        settings.answers,
        settings.models.llm,
        cache,
        queries,
        embedding=embedding,
        index_nodes=index_nodes,
        entity_graph=entity_graph,
    )
    return service, engine, llm, knowledge_base, document


@pytest.mark.asyncio
async def test_empty_selection_automatically_matches_tenant_and_system_knowledge_bases(
    service_settings: ServiceSettings,
) -> None:
    principal = _security()
    tenant_kb, tenant_document, _candidate = _resources(principal)
    system_kb = replace(
        tenant_kb,
        id=uuid4(),
        security_scope_id=derive_system_resource_scope_id(),
        scope_type=KnowledgeBaseScopeType.SYSTEM,
        name="System KB",
    )
    system_document = replace(
        tenant_document,
        id=uuid4(),
        security_scope_id=derive_system_resource_scope_id(),
        knowledge_base_id=system_kb.id,
    )
    service = OnlineService(
        MemoryKnowledgeBases((tenant_kb, system_kb)),  # type: ignore[arg-type]
        MemoryDocuments((tenant_document, system_document)),  # type: ignore[arg-type]
        FakeRagEngine((), ()),  # type: ignore[arg-type]
        FakeLlm([]),
        DisabledRerankAdapter(),
        ProfileCatalog.load(service_settings.profiles),
        service_settings.app,
        service_settings.answers,
        service_settings.models.llm,
    )

    current_tenant = await service._validate_scope(principal, (), ())
    another_tenant = await service._validate_scope(_security("other"), (), ())

    assert {item.id for item in current_tenant} == {tenant_kb.id, system_kb.id}
    assert tuple(item.id for item in another_tenant) == (system_kb.id,)


@pytest.mark.asyncio
async def test_pure_retrieval_accepts_only_current_tenant_knowledge_bases(
    service_settings: ServiceSettings,
) -> None:
    """纯检索的空 ID 只选当前租户；显式系统、跨租户及混合 ID 在引擎调用前拒绝。"""
    principal = _security()
    tenant_kb, tenant_document, candidate = _resources(principal)
    system_kb = replace(
        tenant_kb,
        id=uuid4(),
        security_scope_id=derive_system_resource_scope_id(),
        scope_type=KnowledgeBaseScopeType.SYSTEM,
    )
    other_kb = replace(
        tenant_kb,
        id=uuid4(),
        security_scope_id=_security("other").tenant_resource_scope_id,
    )
    mislabeled_system_kb = replace(
        tenant_kb,
        id=uuid4(),
        scope_type=KnowledgeBaseScopeType.SYSTEM,
    )
    engine = FakeRagEngine((candidate,), (candidate,))
    knowledge_bases = (tenant_kb, system_kb, other_kb, mislabeled_system_kb)
    service = OnlineService(
        MemoryKnowledgeBases(knowledge_bases),  # type: ignore[arg-type]
        MemoryDocuments((tenant_document,)),  # type: ignore[arg-type]
        engine,  # type: ignore[arg-type]
        FakeLlm([]),
        DisabledRerankAdapter(),
        ProfileCatalog.load(service_settings.profiles),
        service_settings.app,
        service_settings.answers,
        service_settings.models.llm,
    )

    selected = await service._validate_scope(principal, (), (), tenant_only=True)
    assert tuple(item.id for item in selected) == (tenant_kb.id,)
    result = await service.retrieve(
        principal,
        RetrievalInput(knowledge_base_ids=(), query="synthetic question", tenant_only=True),
    )
    assert result.candidates and result.candidates[0].knowledge_base_id == tenant_kb.id
    assert engine.queries
    assert all(query.knowledge_base_ids == (tenant_kb.id,) for query in engine.queries)
    explicit = await service._validate_scope(
        principal,
        (tenant_kb.id,),
        (),
        tenant_only=True,
    )
    assert tuple(item.id for item in explicit) == (tenant_kb.id,)

    # 系统库即使对该租户可读，也不得由纯检索接口进入 RAGFlow；跨租户同样不可见。
    query_count = len(engine.queries)
    for requested in (
        (system_kb.id,),
        (other_kb.id,),
        (mislabeled_system_kb.id,),
        (tenant_kb.id, system_kb.id),
    ):
        with pytest.raises(ResourceNotFoundError) as error:
            await service.retrieve(
                principal,
                RetrievalInput(
                    knowledge_base_ids=requested,
                    query="synthetic question",
                    tenant_only=True,
                ),
            )
        assert error.value.code == "RAG-KB-001"
        assert len(engine.queries) == query_count

    # 第三个租户没有任何知识库，空 ID 不能退回系统共享库。
    with pytest.raises(ResourceNotFoundError) as error:
        await service.retrieve(
            _security("unrelated"),
            RetrievalInput(knowledge_base_ids=(), query="synthetic question", tenant_only=True),
        )
    assert error.value.code == "RAG-KB-001"
    assert len(engine.queries) == query_count


@pytest.mark.asyncio
async def test_retrieval_runs_vector_bm25_and_rrf_without_rerank(
    service_settings: ServiceSettings,
) -> None:
    service, engine, _, knowledge_base, document = _service(
        service_settings,
        llm_responses=[],
    )

    result = await service.retrieve(
        _security(),
        RetrievalInput(
            knowledge_base_ids=(knowledge_base.id,),
            document_ids=(document.id,),
            query="  synthetic\u0000question  ",
        ),
    )

    assert result.effective_query == "synthetic question"
    assert len(engine.queries) == 2
    assert {item.vector_similarity_weight for item in engine.queries} == {0.0, 1.0}
    assert result.candidates[0].vector_score == 0.9
    assert result.candidates[0].term_score == 0.9
    assert "rerank_disabled_vector_bm25_rrf_fallback" in result.warnings


@pytest.mark.asyncio
async def test_retrieval_applies_enabled_cross_encoder_scores(
    service_settings: ServiceSettings,
) -> None:
    """RRF 后的候选必须调用真实 Port，写入分数且不再报告关闭降级。"""
    reranker = FakeEnabledReranker()
    service, engine, _, knowledge_base, document = _service(
        service_settings,
        llm_responses=[],
        reranker=reranker,
    )

    result = await service.retrieve(
        _security(),
        RetrievalInput(
            knowledge_base_ids=(knowledge_base.id,),
            document_ids=(document.id,),
            query="synthetic question",
        ),
    )

    assert reranker.calls == [("synthetic question", ("这是最小非业务合成证据。",))]
    assert result.candidates[0].rerank_score == pytest.approx(0.75)
    assert "rerank_disabled_vector_bm25_rrf_fallback" not in result.warnings


@pytest.mark.asyncio
async def test_retrieval_semantically_deduplicates_near_identical_candidates(
    service_settings: ServiceSettings,
) -> None:
    """标点或 OCR 差异造成的近义重复块只保留 RRF 排名更高的一条。"""
    embedding = FakeEmbedding(((1.0, 0.0), (0.999, 0.001)))
    service, engine, _, knowledge_base, document = _service(
        service_settings,
        llm_responses=[],
        embedding=embedding,
    )
    first = engine.vector[0]
    duplicate = replace(
        first,
        chunk_id="near-duplicate-chunk",
        engine_chunk_id="near-duplicate-engine-chunk",
        content="这是最小非业务合成证据!",
        score=0.85,
    )
    engine.vector = (first, duplicate)
    engine.term = (first, duplicate)

    result = await service.retrieve(
        _security(),
        RetrievalInput(
            knowledge_base_ids=(knowledge_base.id,),
            document_ids=(document.id,),
            query="synthetic question",
        ),
    )

    assert [item.chunk_id for item in result.candidates] == [first.chunk_id]
    assert embedding.requests == [
        ("这是最小非业务合成证据。", "这是最小非业务合成证据!")
    ]
    assert "semantic_candidate_dedup_bypassed" not in result.warnings


@pytest.mark.asyncio
async def test_summary_query_uses_adaptive_wider_top_k(
    service_settings: ServiceSettings,
) -> None:
    """概览问题应扩大底层召回和重排候选，而普通事实问题保持较小开销。"""
    service, engine, _, knowledge_base, document = _service(
        service_settings,
        llm_responses=[],
    )

    result = await service.retrieve(
        _security(),
        RetrievalInput(
            knowledge_base_ids=(knowledge_base.id,),
            document_ids=(document.id,),
            query="Summarize the main points of this document.",
        ),
    )

    assert result.query_type == "summary"
    assert result.retrieval_top_k == 80
    assert result.rerank_top_n == 15
    assert result.context_top_n == 8
    assert {query.top_k for query in engine.queries} == {80}
    assert {query.page_size for query in engine.queries} == {15}


@pytest.mark.asyncio
async def test_combined_full_process_query_uses_multi_hop_budget(
    service_settings: ServiceSettings,
) -> None:
    """“结合多个主题说明完整流程”必须使用多跳预算，防止被缩成事实型检索。

    该表达来自真实人工验收。它没有“为什么”等旧关键词，但答案需要同时跨越安装
    组件与操作步骤，因此必须扩大 RAGFlow 召回、Rerank 和最终上下文三个阶段。
    """
    service, engine, _, knowledge_base, document = _service(
        service_settings,
        llm_responses=[],
    )

    result = await service.retrieve(
        _security(),
        RetrievalInput(
            knowledge_base_ids=(knowledge_base.id,),
            document_ids=(document.id,),
            query="结合安装组件和操作步骤说明完整流程。",
        ),
    )

    assert result.query_type == "multi_hop"
    assert result.retrieval_top_k == 80
    assert result.rerank_top_n == 16
    assert result.context_top_n == 8
    assert {query.top_k for query in engine.queries} == {80}
    assert {query.page_size for query in engine.queries} == {16}


@pytest.mark.asyncio
async def test_summary_query_reserves_document_summary_before_higher_section(
    service_settings: ServiceSettings,
) -> None:
    """概览问题即使章节更相似，也必须保留一个文档级 Summary 导航名额。

    测试把 ``top_nodes`` 压到一，并让 Section 相似度高于 Summary。旧实现会只选
    Section；修复后应优先文档 Summary，再回读它指向的真实 RAGFlow Chunk。
    """
    index_nodes = FakeIndexNodes(())
    embedding = FakeEmbedding(((1.0, 0.0),))
    service, engine, _, knowledge_base, document = _service(
        service_settings,
        llm_responses=[],
        embedding=embedding,
        index_nodes=index_nodes,
    )
    engine.chunks = (
        EngineChunk(
            engine_chunk_id="engine-summary-source",
            document_id=document.id,
            content="文档级摘要导航回读的真实原始块。",
            positions=(SourcePosition(page=1),),
        ),
        EngineChunk(
            engine_chunk_id="engine-section-source",
            document_id=document.id,
            content="章节摘要导航回读的真实原始块。",
            positions=(SourcePosition(page=2),),
        ),
    )
    common = {
        "security_scope_id": document.security_scope_id,
        "knowledge_base_id": knowledge_base.id,
        "document_id": document.id,
    }
    index_nodes.nodes = (
        DocumentIndexNode(
            **common,
            node_type=IndexNodeType.SECTION,
            ordinal=0,
            content="与查询完全同向的章节摘要。",
            source_chunk_ids=("engine-section-source",),
            embedding=(1.0, 0.0),
        ),
        DocumentIndexNode(
            **common,
            node_type=IndexNodeType.SUMMARY,
            ordinal=0,
            content="相似度稍低的文档摘要。",
            source_chunk_ids=("engine-summary-source",),
            embedding=(0.6, 0.8),
        ),
    )

    candidates = await service._summary_index_candidates(
        _security().for_resource_scope(document.security_scope_id),
        "总结整份文档",
        (knowledge_base.id,),
        (document.id,),
        {
            "summary_index": {
                "enabled": True,
                "include_sections": True,
                "candidate_nodes": 20,
                "top_nodes": 1,
                "source_chunks_per_node": 1,
                "rrf_weight": 0.85,
                "section_rrf_weight": 0.80,
            }
        },
        rrf_k=60,
        query_type="summary",
    )

    assert [item.engine_chunk_id for item in candidates] == ["engine-summary-source"]
    assert candidates[0].metadata["sidecar_route"] == "summary:document"


@pytest.mark.asyncio
async def test_summary_query_does_not_restore_nodes_below_similarity_threshold(
    service_settings: ServiceSettings,
) -> None:
    """概览保留规则不能绕过 v6 的侧车最低相似度。

    测试只提供一个余弦相似度约为 0.30 的文档 Summary，并把门槛设为 0.40。
    预期侧车路线直接返回空集合且不回读 RAGFlow Chunk，从而防止知识库没有答案时
    因“概览必须保留 Summary”而重新引入已经被过滤的无关节点。
    """
    index_nodes = FakeIndexNodes(())
    embedding = FakeEmbedding(((1.0, 0.0),))
    service, engine, _, knowledge_base, document = _service(
        service_settings,
        llm_responses=[],
        embedding=embedding,
        index_nodes=index_nodes,
    )
    engine.chunks = (
        EngineChunk(
            engine_chunk_id="engine-low-similarity-source",
            document_id=document.id,
            content="低于侧车门槛时不得回读的真实原始块。",
            positions=(SourcePosition(page=1),),
        ),
    )
    index_nodes.nodes = (
        DocumentIndexNode(
            security_scope_id=document.security_scope_id,
            knowledge_base_id=knowledge_base.id,
            document_id=document.id,
            node_type=IndexNodeType.SUMMARY,
            ordinal=0,
            content="仅用于阈值回归测试的合成摘要。",
            source_chunk_ids=("engine-low-similarity-source",),
            embedding=(0.3, 0.9539392014),
        ),
    )

    candidates = await service._summary_index_candidates(
        _security(),
        "总结整份文档",
        (knowledge_base.id,),
        (document.id,),
        {
            "summary_index": {
                "enabled": True,
                "candidate_nodes": 20,
                "similarity_threshold": 0.40,
                "top_nodes": 1,
                "source_chunks_per_node": 1,
                "rrf_weight": 0.85,
            }
        },
        rrf_k=60,
        query_type="summary",
    )

    assert candidates == ()


@pytest.mark.asyncio
async def test_contextual_table_parent_routes_back_to_original_ragflow_chunk(
    service_settings: ServiceSettings,
) -> None:
    """表头/文档前缀只帮助 Parent 向量命中，不得成为公开引用正文。"""
    index_nodes = FakeIndexNodes(())
    embedding = FakeEmbedding(((1.0, 0.0),))
    service, engine, _, knowledge_base, document = _service(
        service_settings,
        llm_responses=[],
        embedding=embedding,
        index_nodes=index_nodes,
    )
    engine.chunks = (
        EngineChunk(
            engine_chunk_id="engine-table-source",
            document_id=document.id,
            content="A7\t106\t31",
            positions=(SourcePosition(page=7),),
        ),
    )
    index_nodes.nodes = (
        DocumentIndexNode(
            security_scope_id=document.security_scope_id,
            knowledge_base_id=knowledge_base.id,
            document_id=document.id,
            node_type=IndexNodeType.PARENT,
            ordinal=2,
            content=(
                "文档：battery\n内容类型：spreadsheet\n索引层级：parent\n"
                "表头：型号\t容量\t温度\n正文：\nA7\t106\t31"
            ),
            source_chunk_ids=("engine-table-source",),
            metadata={"segment_kind": "table", "context_prefix_version": 1},
            embedding=(1.0, 0.0),
            index_version=3,
        ),
    )

    candidates = await service._summary_index_candidates(
        _security().for_resource_scope(document.security_scope_id),
        "A7 的温度是多少",
        (knowledge_base.id,),
        (document.id,),
        {
            "summary_index": {
                "enabled": True,
                "include_sections": True,
                "include_contextual_parents": True,
                "candidate_nodes": 20,
                "top_nodes": 4,
                "source_chunks_per_node": 2,
                "rrf_weight": 0.85,
                "section_rrf_weight": 0.80,
                "contextual_parent_rrf_weight": 0.72,
                "table_parent_rrf_weight": 0.78,
            }
        },
        rrf_k=60,
        query_type="fact",
    )

    assert len(candidates) == 1
    assert candidates[0].content == "A7\t106\t31"
    assert "文档：battery" not in candidates[0].content
    assert candidates[0].metadata["sidecar_route"] == "context:table"
    assert candidates[0].metadata["sidecar_node_kind"] == "table"
    assert candidates[0].metadata["sidecar_context_prefix_version"] == 1
    assert candidates[0].score == pytest.approx(0.78 / 61)


@pytest.mark.asyncio
async def test_summary_and_parent_routes_return_only_original_ragflow_chunks(
    service_settings: ServiceSettings,
) -> None:
    """侧车节点只做导航，公开候选必须来自其 source_chunk_ids 对应原始块。"""
    index_nodes = FakeIndexNodes(())
    # 第一次单条调用使用首个向量计算摘要相似度；语义去重时两条原文使用互相正交
    # 的向量，确保测试能同时观察 Summary 与 Parent 对同一候选的路线归因。
    embedding = FakeEmbedding(((1.0, 0.0), (0.0, 1.0)))
    service, engine, _, knowledge_base, document = _service(
        service_settings,
        llm_responses=[],
        embedding=embedding,
        index_nodes=index_nodes,
    )
    sibling = EngineChunk(
        engine_chunk_id="engine-sibling",
        document_id=document.id,
        content="来自同一 Parent 的原始相邻证据。",
        positions=(SourcePosition(page=2),),
    )
    engine.chunks = (
        EngineChunk(
            engine_chunk_id="engine-chunk",
            document_id=document.id,
            content="这是最小非业务合成证据。",
            positions=(SourcePosition(page=1),),
        ),
        sibling,
    )
    common = {
        "security_scope_id": document.security_scope_id,
        "knowledge_base_id": knowledge_base.id,
        "document_id": document.id,
    }
    index_nodes.nodes = (
        DocumentIndexNode(
            **common,
            node_type=IndexNodeType.PARENT,
            ordinal=0,
            content="局部父节点正文。",
            source_chunk_ids=("engine-chunk", "engine-sibling"),
            embedding=(1.0, 0.0),
        ),
        DocumentIndexNode(
            **common,
            node_type=IndexNodeType.SUMMARY,
            ordinal=0,
            content="文档抽取式摘要。",
            source_chunk_ids=("engine-sibling",),
            embedding=(1.0, 0.0),
        ),
    )

    result = await service.retrieve(
        _security(),
        RetrievalInput(
            knowledge_base_ids=(knowledge_base.id,),
            document_ids=(document.id,),
            query="Summarize the main points of this document.",
        ),
    )

    assert "summary:document" in result.retrieval_routes
    assert "parent:children" in result.retrieval_routes
    assert all(
        candidate.engine_chunk_id in {"engine-chunk", "engine-sibling"}
        for candidate in result.candidates
    )
    assert all("文档抽取式摘要" not in candidate.content for candidate in result.candidates)
    sibling_candidate = next(
        candidate
        for candidate in result.candidates
        if candidate.engine_chunk_id == "engine-sibling"
    )
    # 同一原始块先由文档摘要定位、随后又被 Parent 扩展命中。旧实现会把单值路线
    # 覆盖成 parent:children；修复后兼容字段保留最早侧车来源，完整数组记录两条路线。
    assert sibling_candidate.metadata["sidecar_route"] == "summary:document"
    assert sibling_candidate.metadata["gateway_retrieval_routes"] == [
        "summary:document",
        "parent:children",
    ]


@pytest.mark.asyncio
async def test_candidate_metadata_preserves_all_base_retrieval_routes(
    service_settings: ServiceSettings,
) -> None:
    """同一 Chunk 被向量和 BM25 同时命中时，响应必须保留两条基础路线。

    该诊断字段不参与排名和权限判断，只帮助开发人员解释 RRF 分数来源。测试防止
    后续重构再次只保留最后执行路线，造成“BM25 或向量路线没有运行”的错误判断。
    """
    service, _, _, knowledge_base, document = _service(
        service_settings,
        llm_responses=[],
    )

    result = await service.retrieve(
        _security(),
        RetrievalInput(
            knowledge_base_ids=(knowledge_base.id,),
            document_ids=(document.id,),
            query="synthetic question",
        ),
    )

    route_names = result.candidates[0].metadata["gateway_retrieval_routes"]
    assert any(route.startswith("vector:") for route in route_names)
    assert any(route.startswith("term:") for route in route_names)


@pytest.mark.asyncio
async def test_list_query_uses_section_summary_but_returns_original_chunk(
    service_settings: ServiceSettings,
) -> None:
    """清单问题应命中章节摘要路线，公开内容仍是该章节对应的 RAGFlow 原始块。"""
    index_nodes = FakeIndexNodes(())
    embedding = FakeEmbedding(((1.0, 0.0),))
    service, engine, _, knowledge_base, document = _service(
        service_settings,
        llm_responses=[],
        embedding=embedding,
        index_nodes=index_nodes,
    )
    engine.chunks = (
        EngineChunk(
            engine_chunk_id="engine-section-source",
            document_id=document.id,
            content="第一步准备环境；第二步启动服务。",
            positions=(SourcePosition(page=3),),
        ),
    )
    # 清空基础路线，使断言只观察 Section 导航回读的真实 Chunk，避免测试夹具原有
    # 候选先占满单文档配额而把本节候选截掉。
    engine.vector = ()
    engine.term = ()
    index_nodes.nodes = (
        DocumentIndexNode(
            security_scope_id=document.security_scope_id,
            knowledge_base_id=knowledge_base.id,
            document_id=document.id,
            node_type=IndexNodeType.SECTION,
            ordinal=2,
            content="安装步骤章节摘要。",
            source_chunk_ids=("engine-section-source",),
            embedding=(1.0, 0.0),
        ),
    )

    result = await service.retrieve(
        _security(),
        RetrievalInput(
            knowledge_base_ids=(knowledge_base.id,),
            document_ids=(document.id,),
            query="列出本文有哪些安装步骤",
        ),
    )

    assert result.query_type == "list"
    assert "summary:section" in result.retrieval_routes
    assert any(
        candidate.engine_chunk_id == "engine-section-source"
        and candidate.content == "第一步准备环境；第二步启动服务。"
        for candidate in result.candidates
    )


@pytest.mark.asyncio
async def test_entity_graph_route_returns_original_chunk_instead_of_graph_text(
    service_settings: ServiceSettings,
) -> None:
    """实体关系只负责定位来源，响应正文必须是 RAGFlow 原始块而非合成三元组。"""
    security = _security()
    entity_graph = FakeEntityGraph(())
    service, engine, _, knowledge_base, document = _service(
        service_settings,
        llm_responses=[],
        entity_graph=entity_graph,
    )
    entity_graph.evidence = (
        GraphEvidence(
            knowledge_base_id=knowledge_base.id,
            document_id=document.id,
            source_chunk_ids=("engine-entity-source",),
            score=1.2,
            matched_text="RAGFlow 使用 Elasticsearch",
            evidence_type="RELATION",
        ),
    )
    engine.chunks = (
        EngineChunk(
            engine_chunk_id="engine-entity-source",
            document_id=document.id,
            content="原文明确说明 RAGFlow 使用 Elasticsearch 完成索引。",
            positions=(SourcePosition(page=3),),
        ),
    )

    result = await service.retrieve(
        security,
        RetrievalInput(
            knowledge_base_ids=(knowledge_base.id,),
            document_ids=(document.id,),
            query="RAGFlow 使用什么建立索引？",
        ),
    )

    assert "entity:graph" in result.retrieval_routes
    assert any(
        candidate.engine_chunk_id == "engine-entity-source" for candidate in result.candidates
    )
    assert all(candidate.content != "RAGFlow 使用 Elasticsearch" for candidate in result.candidates)


@pytest.mark.asyncio
async def test_metadata_filter_bypasses_sidecar_routes_to_preserve_scope(
    service_settings: ServiceSettings,
) -> None:
    """显式 Metadata Filter 必须只走能执行同一过滤条件的基础检索路线。

    Parent/Summary/实体侧车没有复制 RAGFlow 的完整 Metadata Filter 语义。这里同时
    准备三类可命中的侧车数据，确认请求一旦携带过滤器，Gateway 不会查询实体图、
    不会补回 Parent/Summary 候选，并通过 warning 明确告诉调用方发生了安全降级。
    """
    security = _security()
    index_nodes = FakeIndexNodes(())
    entity_graph = FakeEntityGraph(())
    embedding = FakeEmbedding(((1.0, 0.0),))
    service, _, _, knowledge_base, document = _service(
        service_settings,
        llm_responses=[],
        embedding=embedding,
        index_nodes=index_nodes,
        entity_graph=entity_graph,
    )
    common = {
        "security_scope_id": document.security_scope_id,
        "knowledge_base_id": knowledge_base.id,
        "document_id": document.id,
    }
    index_nodes.nodes = (
        DocumentIndexNode(
            **common,
            node_type=IndexNodeType.PARENT,
            ordinal=0,
            content="局部父节点正文。",
            source_chunk_ids=("engine-chunk",),
            embedding=(1.0, 0.0),
        ),
        DocumentIndexNode(
            **common,
            node_type=IndexNodeType.SUMMARY,
            ordinal=0,
            content="文档抽取式摘要。",
            source_chunk_ids=("engine-chunk",),
            embedding=(1.0, 0.0),
        ),
    )
    entity_graph.evidence = (
        GraphEvidence(
            knowledge_base_id=knowledge_base.id,
            document_id=document.id,
            source_chunk_ids=("engine-chunk",),
            score=1.0,
            matched_text="RAGFlow 使用 Elasticsearch",
            evidence_type="RELATION",
        ),
    )

    result = await service.retrieve(
        security,
        RetrievalInput(
            knowledge_base_ids=(knowledge_base.id,),
            query="Summarize the main points of this document.",
            metadata_filter={
                "logic": "and",
                "conditions": [
                    {"field": "language", "operator": "eq", "value": "en"}
                ],
            },
        ),
    )

    assert "sidecar_index_bypassed_metadata_filter" in result.warnings
    assert "summary:document" not in result.retrieval_routes
    assert "summary:section" not in result.retrieval_routes
    assert "parent:children" not in result.retrieval_routes
    assert "entity:graph" not in result.retrieval_routes
    assert entity_graph.queries == []


def test_adaptive_context_budget_depends_on_query_type(
    service_settings: ServiceSettings,
) -> None:
    """同一候选集在概览问题中可保留更多证据，事实问题仍限制为五块。"""
    service, _, _, _, _ = _service(service_settings, llm_responses=[])
    security = _security()
    _, _, candidate = _resources(security)
    candidates = tuple(
        replace(
            candidate,
            chunk_id=f"chunk-{index}",
            engine_chunk_id=f"engine-chunk-{index}",
            content=f"第 {index} 个彼此不同的短证据块。",
        )
        for index in range(1, 9)
    )

    fact = service._context_candidates(candidates, "hybrid-adaptive@3", "fact")
    summary = service._context_candidates(candidates, "hybrid-adaptive@3", "summary")

    assert len(fact) == 5
    assert len(summary) == 8


def test_context_text_merges_overlap_but_preserves_all_citation_labels() -> None:
    """相邻块共享的 10% 文本只进入 Prompt 一次，两个真实引用标签仍同时存在。"""
    security = _security()
    knowledge_base, document, candidate = _resources(security)
    overlap = "这是需要在相邻块之间保留的共同上下文文字。"
    left = replace(candidate, content=f"第一段正文。{overlap}")
    right = replace(
        candidate,
        chunk_id="adjacent-chunk",
        engine_chunk_id="adjacent-engine-chunk",
        content=f"{overlap}第二段正文。",
        positions=(SourcePosition(page=2),),
    )

    context_text = OnlineService._context_text((left, right), merge_adjacent=True)

    assert context_text.startswith("[C1][C2]")
    assert context_text.count(overlap) == 1
    assert "第一段正文" in context_text
    assert "第二段正文" in context_text


@pytest.mark.asyncio
async def test_legacy_retrieval_profile_remains_callable_for_rollback(
    service_settings: ServiceSettings,
) -> None:
    service, engine, _, knowledge_base, _ = _service(
        service_settings,
        llm_responses=[],
    )

    result = await service.retrieve(
        _security(),
        RetrievalInput(
            knowledge_base_ids=(knowledge_base.id,),
            query="synthetic question",
            retrieval_profile="hybrid-default@1",
        ),
    )

    assert len(engine.queries) == 2
    assert result.candidates[0].score == pytest.approx(2 / 61)


@pytest.mark.asyncio
async def test_chinese_query_over_english_kb_uses_bilingual_vector_and_english_bm25(
    service_settings: ServiceSettings,
) -> None:
    translated = "How many players from each team are allowed on the court?"
    service, engine, llm, knowledge_base, document = _service(
        service_settings,
        llm_responses=[translated],
    )

    result = await service.retrieve(
        _security(),
        RetrievalInput(
            knowledge_base_ids=(knowledge_base.id,),
            document_ids=(document.id,),
            query="每支球队可以同时有多少名球员在场上？",
        ),
    )

    assert result.original_query == "每支球队可以同时有多少名球员在场上?"
    assert result.effective_query == translated
    assert [
        (item.question, item.vector_similarity_weight) for item in engine.queries
    ] == [
        (result.original_query, 1.0),
        (translated, 1.0),
        (translated, 0.0),
    ]
    assert result.query_language == "zh-CN"
    assert result.query_variants == {"zh-CN": result.original_query, "en-US": translated}
    assert result.knowledge_languages == ("en-US",)
    assert result.retrieval_routes == (
        "vector:original:zh-CN",
        "vector:translated:en-US",
        "term:en-US",
    )
    assert result.candidates[0].score == pytest.approx(2.9 / 61)
    assert len(llm.requests) == 1
    assert llm.requests[0].max_output_tokens == 2048


@pytest.mark.asyncio
async def test_english_query_over_chinese_kb_adds_chinese_vector_and_bm25(
    service_settings: ServiceSettings,
) -> None:
    translated = "篮球运动员累计五次犯规后会怎样?"
    service, engine, llm, knowledge_base, _ = _service(
        service_settings,
        llm_responses=[translated],
        document_languages=("zh-CN",),
    )

    result = await service.retrieve(
        _security(),
        RetrievalInput(
            knowledge_base_ids=(knowledge_base.id,),
            query="What happens after a basketball player commits five fouls?",
        ),
    )

    assert result.effective_query == translated
    assert result.query_variants == {"zh-CN": translated, "en-US": result.original_query}
    assert result.knowledge_languages == ("zh-CN",)
    assert [
        (item.question, item.vector_similarity_weight) for item in engine.queries
    ] == [
        (result.original_query, 1.0),
        (translated, 1.0),
        (translated, 0.0),
    ]
    assert result.retrieval_routes == (
        "vector:original:en-US",
        "vector:translated:zh-CN",
        "term:zh-CN",
    )
    assert result.candidates[0].score == pytest.approx(2.9 / 61)
    assert len(llm.requests) == 1


@pytest.mark.asyncio
async def test_chinese_query_over_chinese_kb_uses_native_vector_and_bm25(
    service_settings: ServiceSettings,
) -> None:
    service, engine, llm, knowledge_base, _ = _service(
        service_settings,
        llm_responses=[],
        document_languages=("zh-CN",),
    )

    result = await service.retrieve(
        _security(),
        RetrievalInput(
            knowledge_base_ids=(knowledge_base.id,),
            query="篮球运动员累计五次犯规后会怎样？",
        ),
    )

    assert result.effective_query == result.original_query
    assert result.query_variants == {"zh-CN": result.original_query}
    assert result.retrieval_routes == ("vector:original:zh-CN", "term:zh-CN")
    assert [item.vector_similarity_weight for item in engine.queries] == [1.0, 0.0]
    assert llm.requests == []


@pytest.mark.asyncio
async def test_mixed_language_kb_uses_both_bm25_routes_and_weighted_rrf(
    service_settings: ServiceSettings,
) -> None:
    translated = "What happens after a player commits five fouls?"
    service, engine, _, knowledge_base, _ = _service(
        service_settings,
        llm_responses=[translated],
        document_languages=("zh-CN", "en-US"),
    )

    result = await service.retrieve(
        _security(),
        RetrievalInput(
            knowledge_base_ids=(knowledge_base.id,),
            query="球员累计五次犯规后会怎样？",
        ),
    )

    assert result.knowledge_languages == ("zh-CN", "en-US")
    assert [
        (item.question, item.vector_similarity_weight) for item in engine.queries
    ] == [
        (result.original_query, 1.0),
        (result.original_query, 0.0),
        (translated, 1.0),
        (translated, 0.0),
    ]
    assert result.retrieval_routes == (
        "vector:original:zh-CN",
        "term:zh-CN",
        "vector:translated:en-US",
        "term:en-US",
    )
    assert result.candidates[0].score == pytest.approx(3.7 / 61)


@pytest.mark.asyncio
async def test_english_query_over_mixed_kb_uses_chinese_and_english_routes(
    service_settings: ServiceSettings,
) -> None:
    translated = "球员累计五次犯规后会怎样?"
    service, engine, _, knowledge_base, _ = _service(
        service_settings,
        llm_responses=[translated],
        document_languages=("zh-CN", "en-US"),
    )

    result = await service.retrieve(
        _security(),
        RetrievalInput(
            knowledge_base_ids=(knowledge_base.id,),
            query="What happens after a player commits five fouls?",
        ),
    )

    assert result.effective_query == result.original_query
    assert result.query_variants == {"zh-CN": translated, "en-US": result.original_query}
    assert [
        (item.question, item.vector_similarity_weight) for item in engine.queries
    ] == [
        (result.original_query, 1.0),
        (translated, 1.0),
        (translated, 0.0),
        (result.original_query, 0.0),
    ]
    assert result.retrieval_routes == (
        "vector:original:en-US",
        "vector:translated:zh-CN",
        "term:zh-CN",
        "term:en-US",
    )


@pytest.mark.asyncio
async def test_unknown_document_language_safely_uses_bilingual_routes(
    service_settings: ServiceSettings,
) -> None:
    service, _, _, knowledge_base, _ = _service(
        service_settings,
        llm_responses=["球员累计五次犯规后会怎样?"],
        document_languages=(None,),
    )

    result = await service.retrieve(
        _security(),
        RetrievalInput(
            knowledge_base_ids=(knowledge_base.id,),
            query="What happens after a player commits five fouls?",
        ),
    )

    assert result.knowledge_languages == ("zh-CN", "en-US")
    assert "knowledge_language_unknown_dual_route" in result.warnings
    assert {route.split(":", maxsplit=1)[0] for route in result.retrieval_routes} == {
        "vector",
        "term",
    }


@pytest.mark.asyncio
async def test_query_translation_retries_an_empty_qwen3_completion_once(
    service_settings: ServiceSettings,
) -> None:
    translated = "What happens after a player commits five fouls?"
    service, engine, llm, knowledge_base, _ = _service(
        service_settings,
        llm_responses=["", translated],
    )

    result = await service.retrieve(
        _security(),
        RetrievalInput(
            knowledge_base_ids=(knowledge_base.id,),
            query="球员累计五次犯规后会怎样？",
        ),
    )

    assert result.effective_query == translated
    assert "query_rewrite_fallback" not in result.warnings
    assert len(llm.requests) == 2
    assert len(engine.queries) == 3


@pytest.mark.asyncio
async def test_answer_uses_context_and_returns_only_valid_citations(
    service_settings: ServiceSettings,
) -> None:
    service, _, llm, knowledge_base, _ = _service(
        service_settings,
        llm_responses=["合成答案 [C1]"],
    )

    result = await service.answer(
        _security(),
        AnswerInput(
            knowledge_base_ids=(knowledge_base.id,),
            query="What is the evidence?",
        ),
    )

    assert result.outcome == AnswerOutcome.SUCCESS
    assert result.answer == "合成答案 [C1]"
    assert result.original_query == "What is the evidence?"
    assert result.effective_query == "What is the evidence?"
    assert [item.label for item in result.citations] == ["C1"]
    assert "<context>" in llm.requests[0].messages[1].content
    assert result.usage == LlmUsage(10, 5, 15)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response_language", "llm_answer", "language_instruction"),
    [
        ("zh-CN", "球员必须离场 [C1]", "Simplified Chinese (简体中文)"),
        ("en-US", "The player must leave the game [C1]", "English"),
    ],
)
async def test_answer_prompt_uses_the_requested_response_language(
    service_settings: ServiceSettings,
    response_language: str,
    llm_answer: str,
    language_instruction: str,
) -> None:
    service, _, llm, knowledge_base, _ = _service(
        service_settings,
        llm_responses=[llm_answer],
    )

    result = await service.answer(
        _security(),
        AnswerInput(
            knowledge_base_ids=(knowledge_base.id,),
            query="What happens after five fouls?",
            response_language=response_language,
        ),
    )

    assert result.answer == llm_answer
    assert result.response_language == response_language
    assert language_instruction in llm.requests[0].messages[1].content
    assert "rag-answer-localized" not in llm.requests[0].messages[1].content


@pytest.mark.asyncio
async def test_answer_exposes_rewritten_query_and_preserves_it_in_cache(
    service_settings: ServiceSettings,
) -> None:
    cache = FakeCache()
    service, engine, llm, knowledge_base, _ = _service(
        service_settings,
        llm_responses=[
            "Can a player continue playing after committing five fouls?",
            "不能继续比赛 [C1]",
        ],
        cache=cache,
    )
    value = AnswerInput(
        knowledge_base_ids=(knowledge_base.id,),
        query="他还能继续比赛吗？",
        messages=(
            HistoryMessage("user", "球员累计五次犯规后会怎样？"),
            HistoryMessage("assistant", "该球员必须离场。"),
        ),
    )

    first = await service.answer(_security(), value)
    second = await service.answer(_security(), value)

    assert first.original_query == "他还能继续比赛吗?"
    assert first.effective_query == "Can a player continue playing after committing five fouls?"
    assert second.original_query == first.original_query
    assert second.effective_query == first.effective_query
    assert second.cache_type == "exact"
    assert [
        (item.question, item.vector_similarity_weight) for item in engine.queries
    ] == [
        (first.original_query, 1.0),
        (first.effective_query, 1.0),
        (first.effective_query, 0.0),
    ]
    assert len(llm.requests) == 2
    assert llm.requests[0].max_output_tokens == 2048
    assert first.original_query in llm.requests[1].messages[1].content


@pytest.mark.asyncio
async def test_no_evidence_does_not_call_llm(
    service_settings: ServiceSettings,
) -> None:
    service, _, llm, knowledge_base, _ = _service(
        service_settings,
        llm_responses=[],
        candidates=False,
    )

    result = await service.answer(
        _security(),
        AnswerInput(
            knowledge_base_ids=(knowledge_base.id,),
            query="unknown question",
        ),
    )

    assert result.outcome == AnswerOutcome.NO_EVIDENCE
    assert result.answer == "当前知识库中没有足够证据确认该问题"
    assert llm.requests == []


@pytest.mark.asyncio
async def test_no_evidence_is_localized_in_english(
    service_settings: ServiceSettings,
) -> None:
    service, _, llm, knowledge_base, _ = _service(
        service_settings,
        llm_responses=[],
        candidates=False,
    )

    result = await service.answer(
        _security(),
        AnswerInput(
            knowledge_base_ids=(knowledge_base.id,),
            query="unknown question",
            response_language="en-US",
        ),
    )

    assert result.outcome == AnswerOutcome.NO_EVIDENCE
    assert result.response_language == "en-US"
    assert result.answer.startswith("The current knowledge base")
    assert llm.requests == []


@pytest.mark.asyncio
async def test_invalid_citation_is_repaired_once(
    service_settings: ServiceSettings,
) -> None:
    service, _, llm, knowledge_base, _ = _service(
        service_settings,
        llm_responses=["错误引用 [C99]", "修复答案 [C1]"],
    )

    result = await service.answer(
        _security(),
        AnswerInput(
            knowledge_base_ids=(knowledge_base.id,),
            query="citation repair fixture",
        ),
    )

    assert result.outcome == AnswerOutcome.SUCCESS
    assert result.answer == "修复答案 [C1]"
    assert len(llm.requests) == 2


@pytest.mark.asyncio
async def test_english_citation_repair_preserves_the_requested_language(
    service_settings: ServiceSettings,
) -> None:
    service, _, llm, knowledge_base, _ = _service(
        service_settings,
        llm_responses=["Unsupported citation [C99]", "Repaired answer [C1]"],
    )

    result = await service.answer(
        _security(),
        AnswerInput(
            knowledge_base_ids=(knowledge_base.id,),
            query="citation repair fixture",
            response_language="en-US",
        ),
    )

    assert result.answer == "Repaired answer [C1]"
    assert "Required response language: English" in llm.requests[1].messages[1].content


@pytest.mark.asyncio
async def test_cache_isolated_by_response_language(
    service_settings: ServiceSettings,
) -> None:
    cache = FakeCache()
    service, engine, llm, knowledge_base, _ = _service(
        service_settings,
        llm_responses=["中文答案 [C1]", "English answer [C1]"],
        cache=cache,
    )
    common = {
        "knowledge_base_ids": (knowledge_base.id,),
        "query": "What is the evidence?",
    }

    chinese = await service.answer(
        _security(),
        AnswerInput(**common, response_language="zh-CN"),
    )
    english = await service.answer(
        _security(),
        AnswerInput(**common, response_language="en-US"),
    )

    assert chinese.cache_type == "none"
    assert english.cache_type == "none"
    assert [item.boundary.language for item in cache.puts] == ["zh-CN", "en-US"]
    assert len(engine.queries) == 4
    assert len(llm.requests) == 2


@pytest.mark.asyncio
async def test_cross_scope_kb_stops_before_ragflow_and_llm(
    service_settings: ServiceSettings,
) -> None:
    service, engine, llm, knowledge_base, _ = _service(
        service_settings,
        llm_responses=[],
    )

    with pytest.raises(ResourceNotFoundError):
        await service.answer(
            _security("other"),
            AnswerInput(
                knowledge_base_ids=(knowledge_base.id,),
                query="越权",
            ),
        )

    assert engine.queries == []
    assert llm.requests == []


@pytest.mark.asyncio
async def test_sse_event_order_and_streaming_usage(
    service_settings: ServiceSettings,
) -> None:
    service, _, _, knowledge_base, _ = _service(
        service_settings,
        llm_responses=[],
    )

    events = [
        event
        async for event in service.stream_answer(
            _security(),
            AnswerInput(
                knowledge_base_ids=(knowledge_base.id,),
                query="streaming question",
            ),
        )
    ]

    assert [item.event for item in events] == [
        "accepted",
        "retrieval.started",
        "retrieval.completed",
        "generation.started",
        "answer.delta",
        "answer.delta",
        "citation",
        "usage",
        "completed",
    ]
    retrieval_event = next(item for item in events if item.event == "retrieval.completed")
    generation_event = next(item for item in events if item.event == "generation.started")
    assert retrieval_event.data["original_query"] == "streaming question"
    assert retrieval_event.data["effective_query"] == "streaming question"
    assert retrieval_event.data["query_rewritten"] is False
    # SSE 与 REST 共用同一查询规划轨迹，前端可以直接展示分类和真实三阶段预算。
    assert retrieval_event.data["query_plan"]["query_type"] == "fact"
    assert retrieval_event.data["query_plan"]["retrieval_top_k"] == 30
    assert retrieval_event.data["query_plan"]["rerank_top_n"] == 8
    assert retrieval_event.data["query_plan"]["context_top_n"] == 5
    assert generation_event.data["response_language"] == "zh-CN"
    assert events[-1].data["outcome"] == "success"


@pytest.mark.asyncio
async def test_sse_translates_chinese_for_retrieval_but_answers_original_question(
    service_settings: ServiceSettings,
) -> None:
    translated = "What happens after a player commits five fouls?"
    service, engine, llm, knowledge_base, _ = _service(
        service_settings,
        llm_responses=[translated],
    )

    events = [
        event
        async for event in service.stream_answer(
            _security(),
            AnswerInput(
                knowledge_base_ids=(knowledge_base.id,),
                query="球员累计五次犯规后会怎样？",
            ),
        )
    ]

    retrieval_event = next(item for item in events if item.event == "retrieval.completed")
    assert retrieval_event.data["original_query"] == "球员累计五次犯规后会怎样?"
    assert retrieval_event.data["effective_query"] == translated
    assert retrieval_event.data["query_rewritten"] is True
    assert [
        (item.question, item.vector_similarity_weight) for item in engine.queries
    ] == [
        (retrieval_event.data["original_query"], 1.0),
        (translated, 1.0),
        (translated, 0.0),
    ]
    assert len(llm.requests) == 2
    assert retrieval_event.data["original_query"] in llm.requests[1].messages[1].content
    assert events[-1].data["outcome"] == "success"


@pytest.mark.asyncio
async def test_exact_cache_hit_revalidates_citation_and_skips_downstream(
    service_settings: ServiceSettings,
) -> None:
    cache = FakeCache()
    service, engine, llm, knowledge_base, _ = _service(
        service_settings,
        llm_responses=["第一次答案 [C1]"],
        cache=cache,
    )
    value = AnswerInput(
        knowledge_base_ids=(knowledge_base.id,),
        query="cache question",
    )

    first = await service.answer(_security(), value)
    second = await service.answer(_security(), value)

    assert first.cache_type == "none"
    assert second.cache_type == "exact"
    assert second.answer == first.answer
    assert len(llm.requests) == 1
    assert len(engine.queries) == 2
    assert second.citations[0].citation_id != first.citations[0].citation_id


@pytest.mark.asyncio
async def test_cache_hit_is_not_served_when_ragflow_health_is_unavailable(
    service_settings: ServiceSettings,
) -> None:
    cache = FakeCache()
    service, engine, llm, knowledge_base, _ = _service(
        service_settings,
        llm_responses=["seeded answer [C1]", "fresh answer [C1]"],
        cache=cache,
    )
    value = AnswerInput(
        knowledge_base_ids=(knowledge_base.id,),
        query="health-gated cache",
    )
    await service.answer(_security(), value)
    engine.healthy = False

    result = await service.answer(_security(), value)

    assert result.cache_type == "none"
    assert result.answer == "fresh answer [C1]"
    assert len(llm.requests) == 2


@pytest.mark.asyncio
async def test_stream_exact_cache_hit_skips_retrieval_and_generation(
    service_settings: ServiceSettings,
) -> None:
    cache = FakeCache()
    service, engine, llm, knowledge_base, _ = _service(
        service_settings,
        llm_responses=["缓存来源答案 [C1]"],
        cache=cache,
    )
    value = AnswerInput(
        knowledge_base_ids=(knowledge_base.id,),
        query="streaming cache question",
    )
    first = await service.answer(_security(), value)

    events = [event async for event in service.stream_answer(_security(), value)]

    assert first.outcome == AnswerOutcome.SUCCESS
    assert len(engine.queries) == 2
    assert len(llm.requests) == 1
    assert (
        "".join(str(item.data["delta"]) for item in events if item.event == "answer.delta")
        == first.answer
    )
    assert events[-1].data["cache_type"] == "exact"
    assert events[-1].data["outcome"] == "success"


@pytest.mark.asyncio
async def test_semantic_cache_hit_requires_the_complete_cache_boundary(
    service_settings: ServiceSettings,
) -> None:
    cache = FakeCache()
    service, engine, llm, knowledge_base, _ = _service(
        service_settings,
        llm_responses=["语义缓存答案 [C1]", "边界不符时重新生成 [C1]"],
        cache=cache,
    )
    await service.answer(
        _security(),
        AnswerInput(
            knowledge_base_ids=(knowledge_base.id,),
            query="original cache question",
        ),
    )
    assert cache.entry is not None
    cache.semantic_entry = cache.entry

    semantic_hit = await service.answer(
        _security(),
        AnswerInput(
            knowledge_base_ids=(knowledge_base.id,),
            query="similar cache question",
        ),
    )

    assert semantic_hit.cache_type == "semantic"
    assert len(engine.queries) == 2
    assert len(llm.requests) == 1

    cache.semantic_entry = replace(
        cache.entry,
        boundary=replace(
            cache.entry.boundary,
            security_scope_id=_security("other").security_scope_id,
        ),
    )
    rejected = await service.answer(
        _security(),
        AnswerInput(
            knowledge_base_ids=(knowledge_base.id,),
            query="boundary mismatch question",
        ),
    )

    assert rejected.cache_type == "none"
    assert rejected.answer == "边界不符时重新生成 [C1]"
    assert len(engine.queries) == 4
    assert len(llm.requests) == 2


@pytest.mark.asyncio
async def test_cache_failure_is_bypassed_without_failing_answer(
    service_settings: ServiceSettings,
) -> None:
    cache = FakeCache()
    cache.fail = True
    service, _, _, knowledge_base, _ = _service(
        service_settings,
        llm_responses=["正常答案 [C1]"],
        cache=cache,
    )

    result = await service.answer(
        _security(),
        AnswerInput(
            knowledge_base_ids=(knowledge_base.id,),
            query="cache failure",
        ),
    )

    assert result.outcome == AnswerOutcome.SUCCESS
    assert "cache_read_bypassed" in result.warnings
    assert "cache_write_bypassed" in result.warnings


@pytest.mark.asyncio
async def test_total_answer_deadline_cancels_the_inflight_llm(
    service_settings: ServiceSettings,
) -> None:
    service, _, _, knowledge_base, _ = _service(
        service_settings,
        llm_responses=[],
    )
    slow_llm = CancellingSlowLlm()
    service._llm = slow_llm  # type: ignore[assignment]
    service._answer_settings = AnswersSection(request_deadline_seconds=1)

    with pytest.raises(LlmError) as timeout:
        await service.answer(
            _security(),
            AnswerInput(
                knowledge_base_ids=(knowledge_base.id,),
                query="deadline fixture",
            ),
        )

    assert timeout.value.code == "RAG-LLM-002"
    assert slow_llm.cancelled is True


@pytest.mark.asyncio
async def test_query_citations_and_feedback_are_persisted_and_scoped(
    service_settings: ServiceSettings,
) -> None:
    queries = MemoryQueries()
    service, _, _, knowledge_base, _ = _service(
        service_settings,
        llm_responses=["持久化答案 [C1]"],
        queries=queries,
    )
    request_id = uuid4()

    result = await service.answer(
        _security(),
        AnswerInput(
            request_id=request_id,
            knowledge_base_ids=(knowledge_base.id,),
            query="persist request",
        ),
    )
    record = await service.get_request(_security(), request_id)
    citations = await service.list_request_citations(_security(), request_id)
    feedback = await service.submit_feedback(
        _security(),
        request_id=request_id,
        feedback_type="helpful",
        rating=1,
        comment="synthetic",
        corrected_answer=None,
    )

    assert record.request_status.value == "COMPLETED"
    assert record.answer_text == result.answer
    assert citations[0].label == "C1"
    assert feedback.request_id == request_id
    with pytest.raises(ResourceNotFoundError):
        await service.get_request(_security("other"), request_id)


@pytest.mark.asyncio
async def test_stream_query_record_and_cancel_are_scope_qualified(
    service_settings: ServiceSettings,
) -> None:
    queries = MemoryQueries()
    service, _, _, knowledge_base, _ = _service(
        service_settings,
        llm_responses=[],
        queries=queries,
    )
    request_id = uuid4()

    events = [
        event
        async for event in service.stream_answer(
            _security(),
            AnswerInput(
                request_id=request_id,
                knowledge_base_ids=(knowledge_base.id,),
                query="persist streaming request",
            ),
        )
    ]
    record = await service.get_request(_security(), request_id)

    assert events[-1].data["outcome"] == "success"
    assert record.request_status == QueryStatus.COMPLETED
    assert record.answer_text == "合成事实 [C1]"
    assert record.timings["total_ms"] >= 0
    assert len(await service.list_request_citations(_security(), request_id)) == 1

    cancel_id = uuid4()
    await queries.add(
        _security().security_scope_id,
        QueryRecord(
            request_id=cancel_id,
            security_scope_id=_security().security_scope_id,
            query_sha256="a" * 64,
            knowledge_base_ids=(knowledge_base.id,),
            retrieval_profile_ref="hybrid-default@1",
            request_status=QueryStatus.RUNNING,
        ),
    )
    cancelled = await service.cancel_request(_security(), cancel_id)

    assert cancelled.request_status == QueryStatus.CANCELLED
    with pytest.raises(ResourceNotFoundError):
        await service.cancel_request(_security("other"), cancel_id)
