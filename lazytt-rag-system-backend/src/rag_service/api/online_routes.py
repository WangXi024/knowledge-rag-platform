"""检索、问答、SSE 流、请求记录、引用和反馈等在线 HTTP 路由。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from contextlib import suppress
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, Path, Request, status
from fastapi.responses import StreamingResponse

from rag_service.api.schemas import (
    AnswerData,
    AnswerMeta,
    AnswerRequest,
    AnswerResponse,
    ApiModel,
    CitationData,
    CitationListResponse,
    CitationResponse,
    CreateFeedbackRequest,
    FeedbackData,
    FeedbackResponse,
    QueryPlanData,
    RequestRecordData,
    RequestRecordResponse,
    RetrievalCandidateData,
    RetrievalData,
    RetrievalRequest,
    RetrievalResponse,
    RetrievalScores,
    SourcePositionData,
    UsageData,
)
from rag_service.application.online import (
    AnswerInput,
    AnswerOutput,
    HistoryMessage,
    OnlineService,
    RetrievalInput,
    RetrievalOutput,
    StreamEvent,
    stream_event_json,
)
from rag_service.domain.errors import RagError
from rag_service.domain.models import Citation, RetrievalCandidate
from rag_service.security.context import SecurityContext

OnlineDependency = Callable[[], AsyncIterator[OnlineService]]
AuthenticateDependency = Callable[..., Any]
DisconnectCheck = Callable[[], Awaitable[bool]]


def _request_id(request: Request) -> UUID:
    """读取贯穿一次在线请求的稳定 UUID。"""
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, UUID) else uuid4()


def _positions(value: RetrievalCandidate | Citation) -> list[SourcePositionData]:
    """把领域原文位置列表转换为 API Schema。"""
    return [SourcePositionData(page=item.page, bbox=item.bbox) for item in value.positions]


def _candidate(
    value: RetrievalCandidate, include_content: bool, include_positions: bool
) -> RetrievalCandidateData:
    """按客户端返回选项转换候选；可省略大文本和坐标以减少响应体。"""
    return RetrievalCandidateData(
        chunk_id=value.chunk_id,
        knowledge_base_id=value.knowledge_base_id,
        document_id=value.document_id,
        document_name=value.document_name,
        content=value.content if include_content else "",
        positions=_positions(value) if include_positions else [],
        scores=RetrievalScores(
            score=value.score,
            vector_score=value.vector_score,
            term_score=value.term_score,
            rerank_score=value.rerank_score,
        ),
        metadata=value.metadata,
    )


def _retrieval(
    value: RetrievalOutput,
    *,
    include_content: bool,
    include_positions: bool,
) -> RetrievalData:
    """把候选、问题分类和真实动态预算组成可直接验收的公开检索响应。"""
    return RetrievalData(
        original_query=value.original_query,
        effective_query=value.effective_query,
        query_plan=QueryPlanData(
            detected_language=value.query_language,
            variants=value.query_variants,
            knowledge_languages=list(value.knowledge_languages),
            retrieval_routes=list(value.retrieval_routes),
            query_type=value.query_type,
            retrieval_top_k=value.retrieval_top_k,
            rerank_top_n=value.rerank_top_n,
            context_top_n=value.context_top_n,
        ),
        candidates=[
            _candidate(item, include_content, include_positions) for item in value.candidates
        ],
        total=value.total,
        warnings=list(value.warnings),
    )


def _citation(value: Citation) -> CitationData:
    """转换引用；排序分优先展示重排分，否则退回综合检索分。"""
    return CitationData(
        citation_id=value.citation_id,
        label=value.label,
        knowledge_base_id=value.knowledge_base_id,
        document_id=value.document_id,
        document_name=value.document_name,
        page_start=value.page_start,
        page_end=value.page_end,
        positions=_positions(value),
        quote=value.quote,
        score=value.rerank_score if value.rerank_score is not None else value.score,
    )


def _answer(value: AnswerOutput) -> AnswerData:
    """转换答案并保留生成该答案时的问题分类、动态预算和改写轨迹。"""
    usage = value.usage
    return AnswerData(
        answer=value.answer,
        outcome=value.outcome.value,
        response_language=value.response_language,
        original_query=value.original_query,
        effective_query=value.effective_query,
        query_rewritten=(
            any(query != value.original_query for query in value.query_variants.values())
            if value.query_variants
            else value.original_query != value.effective_query
        ),
        query_plan=QueryPlanData(
            detected_language=value.query_language,
            variants=value.query_variants,
            knowledge_languages=list(value.knowledge_languages),
            retrieval_routes=list(value.retrieval_routes),
            query_type=value.query_type,
            retrieval_top_k=value.retrieval_top_k,
            rerank_top_n=value.rerank_top_n,
            context_top_n=value.context_top_n,
        ),
        citations=[_citation(item) for item in value.citations],
        warnings=list(value.warnings),
        usage=(
            UsageData(
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                total_tokens=usage.total_tokens,
            )
            if usage is not None
            else None
        ),
        meta=AnswerMeta(cache_type=value.cache_type),
    )


def _history(body: AnswerRequest) -> tuple[HistoryMessage, ...]:
    """将已验证的 API 历史消息转换成不可变应用层消息。"""
    return tuple(HistoryMessage(role=item.role, content=item.content) for item in body.messages)


def _answer_input(body: AnswerRequest, request_id: UUID) -> AnswerInput:
    """把问答请求体完整转换为应用层输入对象。"""
    return AnswerInput(
        knowledge_base_ids=tuple(body.knowledge_base_ids),
        query=body.query,
        messages=_history(body),
        metadata_filter=(
            body.metadata_filter.model_dump(mode="json")
            if body.metadata_filter is not None
            else None
        ),
        retrieval_profile=body.retrieval_profile,
        generation_profile=body.generation_profile,
        response_language=body.response_language,
        request_id=request_id,
    )


def _record(value: Any) -> RequestRecordData:
    """把持久化查询记录投影为脱敏审计响应。"""
    return RequestRecordData(
        request_id=value.request_id,
        status=value.request_status.value,
        outcome=value.outcome.value if value.outcome is not None else None,
        original_query=value.query_redacted,
        effective_query=value.effective_query_redacted,
        answer=value.answer_text,
        warnings=list(value.warnings),
        created_at=value.created_at,
        completed_at=value.completed_at,
    )


async def _idempotent[ResponseModel: ApiModel](
    *,
    service: OnlineService,
    security: SecurityContext,
    key: str,
    method: str,
    path: str,
    payload: dict[str, Any],
    response_status: int,
    response_model: type[ResponseModel],
    operation: Callable[[], Awaitable[ResponseModel]],
) -> ResponseModel:
    """使用 OnlineService 的幂等执行器运行 JSON 响应型写操作。"""
    executor = getattr(service, "execute_idempotent", None)
    if executor is None:
        return await operation()

    async def encoded_operation() -> dict[str, Any]:
        """执行强类型操作并编码为可持久化 JSON。"""
        response = await operation()
        return response.model_dump(mode="json")

    encoded = await executor(
        security=security,
        key=key,
        method=method,
        path=path,
        payload=payload,
        response_status=response_status,
        operation=encoded_operation,
    )
    return response_model.model_validate(encoded)


async def _stream_sse_events(
    *,
    stream: AsyncGenerator[StreamEvent, None],
    request_id: UUID,
    heartbeat_seconds: float,
    disconnected: DisconnectCheck,
) -> AsyncIterator[str]:
    """将应用层事件流包装成可靠的 SSE 文本流。

    函数给事件分配递增序号，在模型暂时无输出时发送心跳，并定期检查客户端是否
    断开；结束或异常时关闭后台取数任务，避免连接消失后模型仍继续消耗资源。
    """
    sequence = 0
    pending_next: asyncio.Task[StreamEvent] | None = None
    try:
        pending_next = asyncio.create_task(anext(stream))
        while True:
            completed, _ = await asyncio.wait(
                {pending_next},
                timeout=heartbeat_seconds,
            )
            if not completed:
                if await disconnected():
                    return
                sequence += 1
                yield stream_event_json(
                    request_id=request_id,
                    sequence=sequence,
                    event=StreamEvent("heartbeat"),
                )
                continue

            current = pending_next
            pending_next = None
            try:
                event = current.result()
            except StopAsyncIteration:
                return

            if await disconnected():
                return
            sequence += 1
            yield stream_event_json(
                request_id=request_id,
                sequence=sequence,
                event=event,
            )
            pending_next = asyncio.create_task(anext(stream))
    except RagError as error:
        sequence += 1
        payload = {
            "request_id": str(request_id),
            "sequence": sequence,
            "error": {
                "code": error.code,
                "message": error.message,
                "retryable": error.retryable,
            },
        }
        yield (
            f"event: error\nid: {sequence}\n"
            f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"
        )
    finally:
        if pending_next is not None:
            pending_next.cancel()
            with suppress(asyncio.CancelledError, StopAsyncIteration, RagError):
                await pending_next
        await stream.aclose()


def install_online_routes(
    app: FastAPI,
    *,
    authenticate: AuthenticateDependency,
    get_online_service: OnlineDependency,
    sse_heartbeat_seconds: float,
) -> None:
    """注册在线路由，并绑定 Direct Tenant/User 上下文、在线 Service 与 SSE 心跳。"""
    security_dependency = Depends(authenticate)
    service_dependency = Depends(get_online_service)

    @app.post(
        "/v1/retrieval",
        response_model=RetrievalResponse,
        tags=["Retrieval"],
    )
    async def retrieve(
        request: Request,
        body: RetrievalRequest,
        security: SecurityContext = security_dependency,
        service: OnlineService = service_dependency,
    ) -> RetrievalResponse:
        """仅在认证租户的知识库中执行纯检索并返回候选文本块。

        租户身份来自认证上下文；请求体只能缩小知识库范围，不能指定系统共享库。
        本路由不写库；未知、系统或跨租户知识库由应用层统一返回 404。
        """
        result = await service.retrieve(
            security,
            RetrievalInput(
                knowledge_base_ids=tuple(body.knowledge_base_ids),
                document_ids=tuple(body.document_ids),
                query=body.query,
                metadata_filter=(
                    body.metadata_filter.model_dump(mode="json")
                    if body.metadata_filter is not None
                    else None
                ),
                retrieval_profile=body.retrieval_profile,
                page_size=body.options.page_size,
                tenant_only=True,
            ),
        )
        return RetrievalResponse(
            request_id=_request_id(request),
            data=_retrieval(
                result,
                include_content=body.options.include_content,
                include_positions=body.options.include_positions,
            ),
        )

    @app.post(
        "/v1/answers",
        response_model=AnswerResponse,
        tags=["Answers"],
    )
    async def answer(
        request: Request,
        body: AnswerRequest,
        security: SecurityContext = security_dependency,
        service: OnlineService = service_dependency,
    ) -> AnswerResponse:
        """执行非流式 RAG 问答，等待完整答案与引用后一次返回。"""
        result = await service.answer(
            security,
            _answer_input(body, _request_id(request)),
        )
        return AnswerResponse(
            request_id=_request_id(request),
            data=_answer(result),
        )

    @app.post(
        "/v1/answers/stream",
        response_class=StreamingResponse,
        tags=["Answers"],
    )
    async def stream_answer(
        request: Request,
        body: AnswerRequest,
        security: SecurityContext = security_dependency,
        service: OnlineService = service_dependency,
    ) -> StreamingResponse:
        """启动 RAG 问答并以 SSE 增量返回阶段、文本、引用和完成事件。"""
        request_id = _request_id(request)

        async def events() -> AsyncIterator[str]:
            """把 Service 事件桥接到带心跳和断线检测的 SSE 包装器。"""
            stream = service.stream_answer(
                security,
                _answer_input(body, request_id),
            )
            async for event in _stream_sse_events(
                stream=stream,
                request_id=request_id,
                heartbeat_seconds=sse_heartbeat_seconds,
                disconnected=request.is_disconnected,
            ):
                yield event

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get(
        "/v1/requests/{request_id}",
        response_model=RequestRecordResponse,
        tags=["Answers"],
    )
    async def get_request(
        request: Request,
        target_request_id: Annotated[UUID, Path(alias="request_id")],
        security: SecurityContext = security_dependency,
        service: OnlineService = service_dependency,
    ) -> RequestRecordResponse:
        """按请求 ID 查询脱敏状态、结果与警告。"""
        value = await service.get_request(security, target_request_id)
        return RequestRecordResponse(
            request_id=_request_id(request),
            data=_record(value),
        )

    @app.post(
        "/v1/requests/{request_id}/cancel",
        response_model=RequestRecordResponse,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["Answers"],
    )
    async def cancel_request(
        request: Request,
        target_request_id: Annotated[UUID, Path(alias="request_id")],
        idempotency_key: Annotated[
            str,
            Header(
                alias="Idempotency-Key",
                min_length=1,
                max_length=128,
                pattern=r"^[\x21-\x7E]+$",
            ),
        ],
        security: SecurityContext = security_dependency,
        service: OnlineService = service_dependency,
    ) -> RequestRecordResponse:
        """幂等请求取消尚未结束的在线生成，并返回更新后的审计记录。"""
        async def operation() -> RequestRecordResponse:
            value = await service.cancel_request(security, target_request_id)
            return RequestRecordResponse(
                request_id=_request_id(request),
                data=_record(value),
            )

        return await _idempotent(
            service=service,
            security=security,
            key=idempotency_key,
            method="POST",
            path=f"/v1/requests/{target_request_id}/cancel",
            payload={},
            response_status=status.HTTP_202_ACCEPTED,
            response_model=RequestRecordResponse,
            operation=operation,
        )

    @app.get(
        "/v1/requests/{request_id}/citations",
        response_model=CitationListResponse,
        tags=["Citations"],
    )
    async def list_request_citations(
        request: Request,
        target_request_id: Annotated[UUID, Path(alias="request_id")],
        security: SecurityContext = security_dependency,
        service: OnlineService = service_dependency,
    ) -> CitationListResponse:
        """列出指定问答请求最终实际使用的全部引用。"""
        values = await service.list_request_citations(
            security,
            target_request_id,
        )
        return CitationListResponse(
            request_id=_request_id(request),
            data=[_citation(value) for value in values],
        )

    @app.get(
        "/v1/citations/{citation_id}",
        response_model=CitationResponse,
        tags=["Citations"],
    )
    async def get_citation(
        request: Request,
        citation_id: UUID,
        security: SecurityContext = security_dependency,
        service: OnlineService = service_dependency,
    ) -> CitationResponse:
        """读取单条引用及其原文位置和摘录。"""
        value = await service.get_citation(security, citation_id)
        return CitationResponse(
            request_id=_request_id(request),
            data=_citation(value),
        )

    @app.post(
        "/v1/feedback",
        response_model=FeedbackResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["Feedback"],
    )
    async def create_feedback(
        request: Request,
        body: CreateFeedbackRequest,
        idempotency_key: Annotated[
            str,
            Header(
                alias="Idempotency-Key",
                min_length=1,
                max_length=128,
                pattern=r"^[\x21-\x7E]+$",
            ),
        ],
        security: SecurityContext = security_dependency,
        service: OnlineService = service_dependency,
    ) -> FeedbackResponse:
        """幂等提交对指定请求的用户反馈。"""
        async def operation() -> FeedbackResponse:
            value = await service.submit_feedback(
                security,
                request_id=body.request_id,
                feedback_type=body.feedback_type,
                rating=body.rating,
                comment=body.comment,
                corrected_answer=body.corrected_answer,
            )
            return FeedbackResponse(
                request_id=_request_id(request),
                data=FeedbackData(
                    id=value.id,
                    request_id=value.request_id,
                    rating=value.rating,
                    feedback_type=value.feedback_type,
                    created_at=value.created_at,
                ),
            )

        return await _idempotent(
            service=service,
            security=security,
            key=idempotency_key,
            method="POST",
            path="/v1/feedback",
            payload=body.model_dump(mode="json"),
            response_status=status.HTTP_201_CREATED,
            response_model=FeedbackResponse,
            operation=operation,
        )
