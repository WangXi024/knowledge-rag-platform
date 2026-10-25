"""在线 API 边界测试：检索/问答 DTO、SSE 事件顺序、断线、引用、取消和反馈。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from rag_service.api.app import create_app
from rag_service.api.online_routes import _stream_sse_events
from rag_service.application.online import (
    AnswerInput,
    AnswerOutput,
    RetrievalInput,
    RetrievalOutput,
    StreamEvent,
)
from rag_service.domain.errors import RagError
from rag_service.domain.models import (
    AnswerOutcome,
    Citation,
    FeedbackRecord,
    QueryRecord,
    QueryStatus,
    RetrievalCandidate,
    SourcePosition,
)
from rag_service.domain.ports import LlmUsage
from rag_service.security.context import SecurityContext
from rag_service.settings import ServiceSettings


class FakeOnline:
    def __init__(self) -> None:
        self.knowledge_base_id = uuid4()
        self.document_id = uuid4()
        self.candidate = RetrievalCandidate(
            chunk_id="stable-chunk",
            engine_chunk_id="engine-chunk",
            knowledge_base_id=self.knowledge_base_id,
            document_id=self.document_id,
            document_name="fixture.txt",
            content="synthetic evidence",
            score=0.03,
            vector_score=0.9,
            term_score=0.8,
            positions=(SourcePosition(page=1),),
        )
        self.scope: str | None = None
        self.retrieval_input: RetrievalInput | None = None
        self.records: dict[UUID, QueryRecord] = {}
        self.citations: dict[UUID, tuple[Citation, ...]] = {}

    async def retrieve(
        self,
        security: SecurityContext,
        value: RetrievalInput,
    ) -> RetrievalOutput:
        self.scope = security.security_scope_id
        self.retrieval_input = value
        return RetrievalOutput(
            original_query=value.query,
            effective_query=value.query,
            candidates=(self.candidate,),
            total=1,
            warnings=("rerank_disabled_vector_bm25_rrf_fallback",),
            query_type="multi_hop",
            retrieval_top_k=80,
            rerank_top_n=16,
            context_top_n=8,
        )

    async def answer(
        self,
        security: SecurityContext,
        value: AnswerInput,
    ) -> AnswerOutput:
        self.scope = security.security_scope_id
        citation = Citation(
            citation_id=uuid4(),
            label="C1",
            knowledge_base_id=self.knowledge_base_id,
            document_id=self.document_id,
            document_name="fixture.txt",
            engine_chunk_id="engine-chunk",
            quote="synthetic evidence",
            page_start=1,
            page_end=1,
            positions=(SourcePosition(page=1),),
            score=0.03,
        )
        self.records[value.request_id] = QueryRecord(
            security_scope_id=security.security_scope_id,
            request_id=value.request_id,
            query_sha256="a" * 64,
            knowledge_base_ids=value.knowledge_base_ids,
            retrieval_profile_ref=value.retrieval_profile,
            request_status=QueryStatus.COMPLETED,
            outcome=AnswerOutcome.SUCCESS,
            answer_text=f"{value.query} [C1]",
        )
        self.citations[value.request_id] = (citation,)
        return AnswerOutput(
            answer=f"{value.query} [C1]",
            outcome=AnswerOutcome.SUCCESS,
            citations=(citation,),
            warnings=(),
            usage=LlmUsage(10, 5, 15),
            response_language=value.response_language,
            original_query=value.query,
            effective_query=f"独立检索：{value.query}" if value.messages else value.query,
            query_type="fact",
            retrieval_top_k=30,
            rerank_top_n=8,
            context_top_n=5,
        )

    async def stream_answer(
        self,
        security: SecurityContext,
        value: AnswerInput,
    ) -> AsyncIterator[StreamEvent]:
        self.scope = security.security_scope_id
        yield StreamEvent("accepted")
        yield StreamEvent(
            "retrieval.completed",
            {
                "candidate_count": 1,
                "context_count": 1,
                "original_query": value.query,
                "effective_query": (
                    f"独立检索：{value.query}" if value.messages else value.query
                ),
                "query_rewritten": bool(value.messages),
            },
        )
        yield StreamEvent(
            "generation.started",
            {"response_language": value.response_language},
        )
        yield StreamEvent("answer.delta", {"delta": value.query})
        yield StreamEvent("completed", {"outcome": "success", "warnings": []})

    async def get_request(
        self,
        security: SecurityContext,
        request_id: UUID,
    ) -> QueryRecord:
        value = self.records[request_id]
        assert value.security_scope_id == security.security_scope_id
        return value

    async def list_request_citations(
        self,
        security: SecurityContext,
        request_id: UUID,
    ) -> tuple[Citation, ...]:
        await self.get_request(security, request_id)
        return self.citations[request_id]

    async def get_citation(
        self,
        security: SecurityContext,
        citation_id: UUID,
    ) -> Citation:
        for request_id, values in self.citations.items():
            await self.get_request(security, request_id)
            for value in values:
                if value.citation_id == citation_id:
                    return value
        raise KeyError(citation_id)

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
        await self.get_request(security, request_id)
        return FeedbackRecord(
            security_scope_id=security.security_scope_id,
            request_id=request_id,
            feedback_type=feedback_type,
            rating=rating,
            comment=comment,
            corrected_answer=corrected_answer,
        )


def _client(settings: ServiceSettings, service: FakeOnline) -> TestClient:
    client = TestClient(
        create_app(
            settings,
            offline_service=object(),  # type: ignore[arg-type]
            online_service=service,  # type: ignore[arg-type]
        )
    )
    # 在线 API 的每个业务用例都必须位于明确租户；具体 User ID 仍由各测试自行设置。
    client.headers["X-Tenant-ID"] = "test-tenant"
    return client


def test_online_rest_routes_use_verified_scope(
    service_settings: ServiceSettings,
) -> None:
    service = FakeOnline()
    body = {
        "knowledge_base_ids": [str(service.knowledge_base_id)],
        "query": "合成问题",
    }
    with _client(service_settings, service) as client:
        retrieval = client.post(
            "/v1/retrieval",
            headers={"X-User-ID": "test-user"},
            json=body,
        )
        answer = client.post(
            "/v1/answers",
            headers={"X-User-ID": "test-user"},
            json=body,
        )

    assert retrieval.status_code == 200
    # 路由必须在可信服务输入中打开租户限定；问答请求体不能覆盖这一内部标记。
    assert service.retrieval_input is not None and service.retrieval_input.tenant_only
    assert retrieval.json()["data"]["candidates"][0]["chunk_id"] == "stable-chunk"
    # 公开轨迹必须展示真实分类与三阶段预算，人工验收不再根据最终 total 猜测。
    assert retrieval.json()["data"]["query_plan"] == {
        "detected_language": "en-US",
        "variants": {},
        "knowledge_languages": [],
        "retrieval_routes": [],
        "query_type": "multi_hop",
        "retrieval_top_k": 80,
        "rerank_top_n": 16,
        "context_top_n": 8,
    }
    assert answer.status_code == 200
    assert answer.json()["data"]["outcome"] == "success"
    assert answer.json()["data"]["response_language"] == "zh-CN"
    assert answer.json()["data"]["original_query"] == "合成问题"
    assert answer.json()["data"]["effective_query"] == "合成问题"
    assert answer.json()["data"]["query_rewritten"] is False
    assert answer.json()["data"]["query_plan"]["query_type"] == "fact"
    assert answer.json()["data"]["query_plan"]["retrieval_top_k"] == 30
    assert answer.json()["data"]["query_plan"]["rerank_top_n"] == 8
    assert answer.json()["data"]["query_plan"]["context_top_n"] == 5
    assert answer.json()["data"]["citations"][0]["label"] == "C1"
    assert service.scope is not None and len(service.scope) == 64


def test_answer_rejects_an_unsupported_response_language(
    service_settings: ServiceSettings,
) -> None:
    service = FakeOnline()
    with _client(service_settings, service) as client:
        response = client.post(
            "/v1/answers",
            headers={"X-User-ID": "test-user"},
            json={
                "knowledge_base_ids": [str(service.knowledge_base_id)],
                "query": "fixture",
                "response_language": "fr-FR",
            },
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "RAG-COMMON-001"
    assert service.scope is None


def test_sse_route_emits_framed_ordered_events(
    service_settings: ServiceSettings,
) -> None:
    service = FakeOnline()
    with _client(service_settings, service) as client:
        response = client.post(
            "/v1/answers/stream",
            headers={"X-User-ID": "test-user"},
            json={
                "knowledge_base_ids": [str(service.knowledge_base_id)],
                "query": "流式",
                "messages": [
                    {"role": "user", "content": "上一轮"},
                    {"role": "assistant", "content": "上一轮回答"},
                ],
            },
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.index("event: accepted") < response.text.index("event: answer.delta")
    assert '"original_query":"流式"' in response.text
    assert '"effective_query":"独立检索：流式"' in response.text
    assert '"query_rewritten":true' in response.text
    assert response.text.rstrip().endswith('"outcome":"success","warnings":[]}')


@pytest.mark.asyncio
async def test_sse_stream_emits_heartbeat_without_cancelling_source() -> None:
    source_completed = False

    async def source() -> AsyncGenerator[StreamEvent, None]:
        nonlocal source_completed
        yield StreamEvent("accepted")
        await asyncio.sleep(0.03)
        source_completed = True
        yield StreamEvent("completed", {"outcome": "success", "warnings": []})

    async def connected() -> bool:
        return False

    stream = source()
    events = [
        event
        async for event in _stream_sse_events(
            stream=stream,
            request_id=uuid4(),
            heartbeat_seconds=0.005,
            disconnected=connected,
        )
    ]

    event_names = [
        line.removeprefix("event: ")
        for event in events
        for line in event.splitlines()
        if line.startswith("event: ")
    ]
    event_ids = [
        int(line.removeprefix("id: "))
        for event in events
        for line in event.splitlines()
        if line.startswith("id: ")
    ]
    assert event_names[0] == "accepted"
    assert "heartbeat" in event_names
    assert event_names[-1] == "completed"
    assert event_ids == list(range(1, len(events) + 1))
    assert source_completed is True


@pytest.mark.asyncio
async def test_sse_disconnect_cancels_pending_source() -> None:
    source_closed = asyncio.Event()

    async def source() -> AsyncGenerator[StreamEvent, None]:
        try:
            await asyncio.sleep(60)
            yield StreamEvent("completed")
        finally:
            source_closed.set()

    async def disconnected() -> bool:
        return True

    stream = source()
    events = [
        event
        async for event in _stream_sse_events(
            stream=stream,
            request_id=uuid4(),
            heartbeat_seconds=0.005,
            disconnected=disconnected,
        )
    ]

    assert events == []
    assert source_closed.is_set()


@pytest.mark.asyncio
async def test_sse_error_is_terminal_and_keeps_sequence() -> None:
    async def source() -> AsyncGenerator[StreamEvent, None]:
        yield StreamEvent("accepted")
        raise RagError(
            "RAG-LLM-001",
            "模型服务暂时不可用",
            http_status=502,
            retryable=True,
        )

    async def connected() -> bool:
        return False

    events = [
        event
        async for event in _stream_sse_events(
            stream=source(),
            request_id=uuid4(),
            heartbeat_seconds=1,
            disconnected=connected,
        )
    ]

    assert len(events) == 2
    assert "event: accepted\nid: 1\n" in events[0]
    assert "event: error\nid: 2\n" in events[1]
    assert '"code":"RAG-LLM-001"' in events[1]
    assert '"retryable":true' in events[1]


def test_online_route_rejects_forged_scope_before_service(
    service_settings: ServiceSettings,
) -> None:
    service = FakeOnline()
    with _client(service_settings, service) as client:
        response = client.post(
            "/v1/answers",
            headers={"X-User-ID": "test-user"},
            json={
                "knowledge_base_ids": [str(service.knowledge_base_id)],
                "query": "forged",
                "security_scope_id": "attacker",
            },
        )

    assert response.status_code == 400
    assert service.scope is None


def test_request_citation_and_feedback_routes(
    service_settings: ServiceSettings,
) -> None:
    service = FakeOnline()
    headers = {"X-User-ID": "test-user"}
    with _client(service_settings, service) as client:
        answer = client.post(
            "/v1/answers",
            headers=headers,
            json={
                "knowledge_base_ids": [str(service.knowledge_base_id)],
                "query": "可追踪",
            },
        )
        request_id = answer.json()["request_id"]
        citation_id = answer.json()["data"]["citations"][0]["citation_id"]
        record = client.get(f"/v1/requests/{request_id}", headers=headers)
        citations = client.get(
            f"/v1/requests/{request_id}/citations",
            headers=headers,
        )
        citation = client.get(f"/v1/citations/{citation_id}", headers=headers)
        feedback = client.post(
            "/v1/feedback",
            headers={**headers, "Idempotency-Key": "feedback-1"},
            json={
                "request_id": request_id,
                "feedback_type": "helpful",
                "rating": 1,
            },
        )

    assert record.status_code == 200
    assert record.json()["data"]["status"] == "COMPLETED"
    assert citations.status_code == 200
    assert citations.json()["data"][0]["citation_id"] == citation_id
    assert citation.status_code == 200
    assert feedback.status_code == 201
    assert feedback.json()["data"]["feedback_type"] == "helpful"
