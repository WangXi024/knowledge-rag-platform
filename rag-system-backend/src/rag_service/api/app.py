"""FastAPI 应用的组装根和系统级 HTTP 行为。

本模块创建数据库、认证、RAGFlow、模型、缓存和应用服务，把它们按请求生命周期注入
路由；同时负责请求 ID、访问日志、统一异常响应、健康/版本/能力接口以及连接关闭。
测试可传入替身服务和探针，避免启动真实外部依赖。
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Protocol
from uuid import UUID, uuid4

from fastapi import FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

from rag_service.api.offline_routes import install_offline_routes
from rag_service.api.online_routes import install_online_routes
from rag_service.api.schemas import (
    ApiErrorSchema,
    CapabilitiesData,
    CapabilitiesResponse,
    DependencyHealth,
    ErrorDetailSchema,
    ErrorResponse,
    HealthData,
    HealthResponse,
    HealthStatus,
    VersionData,
    VersionResponse,
)
from rag_service.application.idempotency import IdempotencyService
from rag_service.application.offline import OfflineService
from rag_service.application.online import OnlineService
from rag_service.application.profiles import ProfileCatalog
from rag_service.bootstrap.profiles import load_profile_seeds
from rag_service.domain.errors import ErrorDetail, RagError
from rag_service.infrastructure.cache.redis_cache import RedisAnswerCache
from rag_service.infrastructure.health import RuntimeReadinessProbe
from rag_service.infrastructure.models.openai_compatible import (
    OpenAICompatibleEmbeddingAdapter,
    OpenAICompatibleLlmAdapter,
)
from rag_service.infrastructure.models.openai_compatible_rerank import (
    OpenAICompatibleRerankAdapter,
)
from rag_service.infrastructure.models.rerank import DisabledRerankAdapter
from rag_service.infrastructure.postgres.query_store import (
    TransactionalQueryRepository,
)
from rag_service.infrastructure.postgres.repositories import (
    SqlAlchemyDocumentRepository,
    SqlAlchemyDocumentVersionRepository,
    SqlAlchemyEntityGraphRepository,
    SqlAlchemyIdempotencyRepository,
    SqlAlchemyIndexNodeRepository,
    SqlAlchemyJobRepository,
    SqlAlchemyKnowledgeBaseRepository,
)
from rag_service.infrastructure.postgres.session import (
    create_engine as create_database_engine,
)
from rag_service.infrastructure.postgres.session import create_session_factory
from rag_service.infrastructure.ragflow.adapter import RagFlowAdapter
from rag_service.infrastructure.staging.filesystem import FileSystemUploadStaging
from rag_service.observability.logging import configure_structured_logging
from rag_service.security.context import SecurityContext
from rag_service.security.direct_user import DirectIdentityValueError, DirectUserResolver
from rag_service.settings import ServiceSettings, get_settings
from rag_service.workers.dispatch import notify_committed

REQUEST_ID_HEADER = "X-Request-ID"
logger = logging.getLogger(__name__)


class ReadinessProbe(Protocol):
    """就绪接口所需的最小依赖探测接口。"""

    async def check(self) -> HealthData:
        """检查数据库、缓存和 RAG 引擎等依赖，汇总成统一的就绪状态。"""
        ...


class NotConfiguredReadinessProbe:
    """未启用真实运行时探测时使用的保守探针，明确报告不可用而非伪装健康。"""
    async def check(self) -> HealthData:
        """返回数据库、Redis 和 RAGFlow 均未配置的不可用快照。"""
        return HealthData(
            status=HealthStatus.UNAVAILABLE,
            dependencies={
                "application_database": DependencyHealth(
                    status=HealthStatus.UNAVAILABLE,
                    message="probe_not_configured",
                ),
                "redis": DependencyHealth(
                    status=HealthStatus.UNAVAILABLE,
                    message="probe_not_configured",
                ),
                "ragflow": DependencyHealth(
                    status=HealthStatus.UNAVAILABLE,
                    message="probe_not_configured",
                ),
            },
        )


def _request_id(request: Request) -> UUID:
    """读取中间件设置的请求 ID；异常路径缺失时生成兜底 ID。"""
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, UUID) else uuid4()


def _error_payload(request_id: UUID, error: RagError) -> dict[str, object]:
    """把内部领域错误转换为符合 OpenAPI 的、可 JSON 序列化的响应字典。"""
    response = ErrorResponse(
        request_id=request_id,
        error=ApiErrorSchema(
            code=error.code,
            message=error.message,
            retryable=error.retryable,
            details=[
                ErrorDetailSchema(field=detail.field, reason=detail.reason)
                for detail in error.details
            ],
        ),
    )
    return response.model_dump(mode="json")


def _invalid_request_id_response() -> JSONResponse:
    """构造客户端传入非法 ``X-Request-ID`` 时的独立 400 响应。"""
    request_id = uuid4()
    error = RagError(
        code="RAG-COMMON-001",
        message="请求参数无效",
        http_status=400,
        retryable=False,
        details=[ErrorDetail(reason="invalid_uuid", field="X-Request-ID")],
    )
    return JSONResponse(
        status_code=400,
        content=_error_payload(request_id, error),
        headers={REQUEST_ID_HEADER: str(request_id)},
    )


def create_app(
    settings: ServiceSettings,
    *,
    readiness_probe: ReadinessProbe | None = None,
    offline_service: OfflineService | None = None,
    online_service: OnlineService | None = None,
    runtime_readiness: bool = False,
) -> FastAPI:
    """创建配置完整的 FastAPI 应用，并按需组装真实或注入的依赖。

    若调用方未注入 Offline/Online Service，函数才创建数据库及相关适配器；这让单元
    测试只测试 HTTP 边界。``runtime_readiness`` 仅在真实进程入口启用，防止普通应用
    构造就产生额外网络探测。
    """
    configure_structured_logging(settings.observability.log_level)
    # 只要任一业务服务需要现场组装，就必须有共享数据库引擎和会话工厂。
    database_engine = (
        create_database_engine(settings.database)
        if offline_service is None or online_service is None
        else None
    )
    session_factory = (
        create_session_factory(database_engine) if database_engine is not None else None
    )
    staging = FileSystemUploadStaging(settings.uploads) if offline_service is None else None
    runtime_probe = (
        RuntimeReadinessProbe(settings, database_engine)
        if runtime_readiness and readiness_probe is None and database_engine is not None
        else None
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """应用停止时依次关闭探针 HTTP 客户端和数据库连接池。"""
        try:
            yield
        finally:
            if runtime_probe is not None:
                await runtime_probe.close()
            if database_engine is not None:
                await database_engine.dispose()

    app = FastAPI(
        title="RAG Service API",
        version="1.0.0",
        description="Scope-isolated RAG Gateway",
        lifespan=lifespan,
    )
    # 直连身份解析器同时接受 X-Tenant-ID 与 X-User-ID，并由两者派生隔离范围。
    # 它不做登录或签名校验，生产环境已由 Settings 阻止启动，等待后续
    # Identity 通过可信内部链路或已验签 Claims 注入这两个字段。
    user_resolver = DirectUserResolver(settings.identity)
    probe = readiness_probe or runtime_probe or NotConfiguredReadinessProbe()
    profile_seeds = load_profile_seeds(settings.profiles)
    profile_refs: dict[str, list[str]] = {
        "PARSER": [],
        "CHUNK": [],
        "RETRIEVAL": [],
    }
    for seed in profile_seeds:
        if seed.enabled and seed.profile_type in profile_refs:
            profile_refs[seed.profile_type].append(seed.ref)

    @app.middleware("http")
    async def request_id_middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[JSONResponse]],
    ) -> JSONResponse:
        """验证/生成请求 ID，记录耗时，并把同一 ID 写入响应头和访问日志。"""
        started_at = time.perf_counter()
        supplied = request.headers.get(REQUEST_ID_HEADER)
        if supplied:
            try:
                request_id = UUID(supplied)
            except ValueError:
                return _invalid_request_id_response()
        else:
            request_id = uuid4()
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = str(request_id)
        logger.info(
            "http_request_completed",
            extra={
                "request_id": str(request_id),
                "method": request.method,
                "path": request.url.path,
                "status_code": response.status_code,
                "duration_ms": round((time.perf_counter() - started_at) * 1000, 3),
            },
        )
        return response

    @app.exception_handler(RagError)
    async def handle_rag_error(request: Request, error: RagError) -> JSONResponse:
        """把预期业务错误转换为其指定 HTTP 状态，并记录无敏感内容的告警日志。"""
        request_id = _request_id(request)
        logger.warning(
            "http_request_failed",
            extra={
                "request_id": str(request_id),
                "path": request.url.path,
                "status_code": error.http_status,
                "error_code": error.code,
            },
        )
        return JSONResponse(
            status_code=error.http_status,
            content=_error_payload(request_id, error),
            headers={REQUEST_ID_HEADER: str(request_id)},
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        """把 Pydantic/FastAPI 参数错误统一映射为字段级 ``RAG-COMMON-001``。"""
        details = [
            ErrorDetailSchema(
                field=".".join(str(part) for part in item["loc"]),
                reason=str(item["type"]),
            )
            for item in error.errors()
        ]
        rag_error = RagError(
            code="RAG-COMMON-001",
            message="请求参数无效",
            http_status=400,
            retryable=False,
            details=[ErrorDetail(field=item.field, reason=item.reason) for item in details],
        )
        request_id = _request_id(request)
        return JSONResponse(
            status_code=400,
            content=_error_payload(request_id, rag_error),
            headers={REQUEST_ID_HEADER: str(request_id)},
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, _error: Exception) -> JSONResponse:
        """隐藏未知异常内部细节，对外返回固定 500，同时在服务端保留堆栈。"""
        request_id = _request_id(request)
        logger.exception(
            "http_request_unexpected_failure",
            extra={
                "request_id": str(request_id),
                "path": request.url.path,
                "status_code": 500,
                "error_code": "RAG-COMMON-011",
            },
        )
        error = RagError(
            code="RAG-COMMON-011",
            message="服务内部错误",
            http_status=500,
            retryable=True,
        )
        return JSONResponse(
            status_code=500,
            content=_error_payload(request_id, error),
            headers={REQUEST_ID_HEADER: str(request_id)},
        )

    async def authenticate(
        tenant_id: str = Header(
            alias=settings.identity.tenant_header_name,
        ),
        user_id: str = Header(
            alias=settings.identity.user_header_name,
        ),
    ) -> SecurityContext:
        """把必填 Tenant/User 请求头转成隔离上下文，不记录或回显原始身份。

        缺少请求头由统一 FastAPI 参数校验处理；格式错误则精确指向对应 Header。
        所有校验都发生在业务 Service 运行之前，因此非法身份不会访问数据库、缓存、
        RAGFlow 或模型服务。
        """
        try:
            return user_resolver.resolve(tenant_id, user_id)
        except DirectIdentityValueError as error:
            header_name = (
                "X-Tenant-ID" if error.field == "tenant_id" else "X-User-ID"
            )
            raise RagError(
                code="RAG-COMMON-001",
                message="请求参数无效",
                http_status=400,
                retryable=False,
                details=[ErrorDetail(field=f"header.{header_name}", reason=error.reason)],
            ) from error

    async def get_offline_service() -> AsyncIterator[OfflineService]:
        """为一次离线 API 请求创建事务内 Service，并在结束后关闭 RAGFlow 客户端。"""
        if offline_service is not None:
            yield offline_service
            return
        if session_factory is None or staging is None:
            raise RuntimeError("offline service dependencies are unavailable")
        async with session_factory() as session, session.begin():
            knowledge_bases = SqlAlchemyKnowledgeBaseRepository(session)
            documents = SqlAlchemyDocumentRepository(session)
            versions = SqlAlchemyDocumentVersionRepository(session)
            jobs = SqlAlchemyJobRepository(session)
            idempotency = IdempotencyService(
                SqlAlchemyIdempotencyRepository(session),
                settings.idempotency,
            )
            catalog = await ProfileCatalog.from_database(session, settings.profiles)
            rag_engine = RagFlowAdapter(
                settings.ragflow,
                knowledge_bases,
                documents,
            )
            service = OfflineService(
                knowledge_bases,
                documents,
                jobs,
                staging,
                rag_engine,
                catalog,
                settings.jobs,
                settings.ragflow.embedding_model,
                idempotency,
                versions,
            )
            try:
                yield service
            finally:
                await rag_engine.close()
        # 退出 session.begin 后事务已成功提交；回滚不会执行这里的通知。
        await notify_committed(session, settings)

    async def get_online_service() -> AsyncIterator[OnlineService]:
        """为一次在线 API 请求组装检索/模型/缓存 Service，并可靠关闭全部客户端。

        查询审计使用独立的事务型 Repository，以便 SSE 长连接期间可分阶段提交状态，
        而知识库和幂等写操作仍位于当前请求事务中。
        """
        if online_service is not None:
            yield online_service
            return
        if session_factory is None:
            raise RuntimeError("online service dependencies are unavailable")
        async with session_factory() as session, session.begin():
            knowledge_bases = SqlAlchemyKnowledgeBaseRepository(session)
            documents = SqlAlchemyDocumentRepository(session)
            index_nodes = SqlAlchemyIndexNodeRepository(session)
            entity_graph = SqlAlchemyEntityGraphRepository(session)
            catalog = await ProfileCatalog.from_database(session, settings.profiles)
            rag_engine = RagFlowAdapter(
                settings.ragflow,
                knowledge_bases,
                documents,
            )
            llm = OpenAICompatibleLlmAdapter(settings.models.llm)
            embedding = OpenAICompatibleEmbeddingAdapter(settings.models.embedding)
            # Rerank 只在四项配置完整且启动预检已经通过时创建真实云端适配器；关闭
            # Profile 继续注入显式降级实现，避免用向量相似度伪装 Cross-Encoder。
            reranker = (
                OpenAICompatibleRerankAdapter(settings.models.rerank)
                if settings.models.rerank.enabled
                else DisabledRerankAdapter()
            )
            query_repository = TransactionalQueryRepository(session_factory)
            cache_redis = Redis.from_url(
                settings.cache.url,
                decode_responses=False,
                socket_connect_timeout=settings.redis.socket_connect_timeout_seconds,
                socket_timeout=settings.redis.socket_timeout_seconds,
            )
            service = OnlineService(
                knowledge_bases,
                documents,
                rag_engine,
                llm,
                reranker,
                catalog,
                settings.app,
                settings.answers,
                settings.models.llm,
                RedisAnswerCache(cache_redis, embedding, settings.cache),
                query_repository,
                IdempotencyService(
                    SqlAlchemyIdempotencyRepository(session),
                    settings.idempotency,
                ),
                embedding=embedding,
                index_nodes=index_nodes,
                entity_graph=entity_graph,
            )
            try:
                yield service
            finally:
                await cache_redis.aclose()
                if isinstance(reranker, OpenAICompatibleRerankAdapter):
                    await reranker.close()
                await embedding.close()
                await llm.close()
                await rag_engine.close()

    @app.get("/health/live", response_model=HealthResponse, tags=["System"])
    async def liveness(request: Request) -> HealthResponse:
        """只证明 Python 进程能响应；不访问任何下游依赖。"""
        return HealthResponse(
            request_id=_request_id(request),
            data=HealthData(status=HealthStatus.OK),
        )

    @app.get("/health/ready", response_model=HealthResponse, tags=["System"])
    async def readiness(request: Request) -> HealthResponse:
        """聚合真实依赖状态；关键依赖不可用时返回可重试的 503。"""
        snapshot = await probe.check()
        if snapshot.status == HealthStatus.UNAVAILABLE:
            raise RagError(
                code="RAG-COMMON-012",
                message="服务未就绪",
                http_status=503,
                retryable=True,
                details=[
                    ErrorDetail(field=name, reason=dependency.message or dependency.status.value)
                    for name, dependency in snapshot.dependencies.items()
                    if dependency.status == HealthStatus.UNAVAILABLE
                ],
            )
        return HealthResponse(request_id=_request_id(request), data=snapshot)

    @app.get(
        "/v1/version",
        response_model=VersionResponse,
        tags=["System"],
    )
    async def version(
        request: Request,
    ) -> VersionResponse:
        """返回 API、构建和外部契约版本，供部署排障和兼容性判断。"""
        return VersionResponse(
            request_id=_request_id(request),
            data=VersionData(
                api_version=settings.app.api_version,
                build_commit=settings.build_commit,
                build_time=settings.build_time,
                ragflow_contract_version=settings.ragflow.contract_version,
            ),
        )

    @app.get(
        "/v1/capabilities",
        response_model=CapabilitiesResponse,
        tags=["System"],
    )
    async def capabilities(
        request: Request,
    ) -> CapabilitiesResponse:
        """返回当前配置实际开放的格式、Profile 与功能，供前端动态展示。"""
        return CapabilitiesResponse(
            request_id=_request_id(request),
            data=CapabilitiesData(
                file_extensions=list(settings.uploads.allowed_extensions),
                max_file_bytes=settings.uploads.max_file_bytes,
                parser_profiles=sorted(profile_refs["PARSER"]),
                chunk_profiles=sorted(profile_refs["CHUNK"]),
                retrieval_profiles=sorted(profile_refs["RETRIEVAL"]),
                features={
                    "answer_streaming": True,
                    "exact_cache": settings.cache.exact_enabled,
                    "semantic_cache": settings.cache.semantic_enabled,
                    "rerank": settings.models.rerank.enabled,
                    "retrieval_vector_bm25_rrf": True,
                    "adaptive_top_k": True,
                    "parent_child_index": True,
                    "summary_index": True,
                    "entity_index": True,
                    "lightweight_knowledge_graph": True,
                    "tenant_shared_knowledge_bases": True,
                    "system_shared_knowledge_bases": True,
                    "automatic_shared_knowledge_retrieval": True,
                    "user_managed_knowledge_bases": False,
                    # 当前联调身份边界同时依赖 Tenant ID 与 User ID。能力名称明确包含
                    # 两者，避免调用方误以为仅传 User ID 就能形成完整隔离范围。
                    "identity_mode_direct_tenant_user": True,
                    "authentication_enabled": False,
                    "production_safe_identity": False,
                },
            ),
        )

    install_offline_routes(
        app,
        authenticate=authenticate,
        get_offline_service=get_offline_service,
    )
    install_online_routes(
        app,
        authenticate=authenticate,
        get_online_service=get_online_service,
        sse_heartbeat_seconds=settings.answers.sse_heartbeat_seconds,
    )

    return app


def get_application() -> FastAPI:
    """生产 ASGI 入口：读取缓存配置并启用真实运行时就绪探针。"""
    return create_app(get_settings(), runtime_readiness=True)
