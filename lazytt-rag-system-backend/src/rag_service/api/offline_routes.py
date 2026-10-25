"""知识库、文档、解析任务和文本块等离线资源的 HTTP 路由。

路由只做协议工作：读取 Header/Query/Form，转换 Pydantic 与领域对象，设置 HTTP 状态
和封装幂等执行。实际状态机、权限和外部引擎编排全部委托给 ``OfflineService``。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated, Any, Literal
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, File, Form, Header, Query, Request, UploadFile, status
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from rag_service.api.schemas import (
    ApiErrorSchema,
    ApiModel,
    ChunkData,
    ChunkPageResponse,
    CreateKnowledgeBaseRequest,
    DocumentAcceptedData,
    DocumentAcceptedResponse,
    DocumentData,
    DocumentPageResponse,
    DocumentResponse,
    DocumentVersionAcceptedData,
    DocumentVersionAcceptedResponse,
    DocumentVersionData,
    DocumentVersionPageResponse,
    JobAcceptedData,
    JobAcceptedResponse,
    JobData,
    JobResponse,
    KnowledgeBaseData,
    KnowledgeBasePageResponse,
    KnowledgeBaseResponse,
    PageMeta,
    ReparseDocumentRequest,
    SourcePositionData,
    UpdateKnowledgeBaseRequest,
)
from rag_service.application.offline import (
    CreateKnowledgeBaseInput,
    OfflineService,
    UpdateKnowledgeBaseInput,
    UploadDocumentInput,
    UploadDocumentVersionInput,
)
from rag_service.domain.errors import RagError
from rag_service.domain.models import (
    DocumentRecord,
    DocumentUpdateMode,
    DocumentVersion,
    IngestionJob,
    KnowledgeBase,
    KnowledgeBaseScopeType,
)
from rag_service.domain.ports import EngineChunk, Page
from rag_service.security.context import SecurityContext

OfflineDependency = Callable[[], AsyncIterator[OfflineService]]
AuthenticateDependency = Callable[..., Any]


def _request_id(request: Request) -> UUID:
    """取得请求中间件生成的 ID，供响应体与日志关联。"""
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, UUID) else uuid4()


def _stored_error(code: str | None, message: str | None) -> ApiErrorSchema | None:
    """把资源记录中的最近错误字段转换为公开错误结构；无错误码表示成功。"""
    if not code:
        return None
    return ApiErrorSchema(
        code=code,
        message=message or "执行失败",
        retryable=False,
        details=[],
    )


def _knowledge_base(value: KnowledgeBase) -> KnowledgeBaseData:
    """将内部知识库对象投影为不含隔离键和引擎 ID 的 API 视图。"""
    return KnowledgeBaseData(
        id=value.id,
        scope_type=value.scope_type.value,
        external_id=value.external_id,
        name=value.name,
        description=value.description,
        status=value.status.value,
        revision=value.revision,
        parser_profile=value.parser_profile_ref,
        chunk_profile=value.chunk_profile_ref,
        embedding_profile=value.embedding_profile_ref,
        retrieval_profile=value.retrieval_profile_ref,
        document_count=value.document_count,
        created_at=value.created_at,
        updated_at=value.updated_at,
    )


def _document(value: DocumentRecord) -> DocumentData:
    """将内部文档记录投影为包含解析进度与最近错误的 API 视图。"""
    return DocumentData(
        id=value.id,
        knowledge_base_id=value.knowledge_base_id,
        external_id=value.external_id,
        filename=value.filename,
        mime_type=value.mime_type,
        file_size_bytes=value.file_size_bytes,
        content_sha256=value.content_sha256,
        active_version_id=value.active_version_id,
        active_version_number=value.active_version_number,
        document_revision=value.document_revision,
        metadata=value.metadata,
        parser_profile=value.parser_profile_ref,
        chunk_profile=value.chunk_profile_ref,
        status=value.status.value,
        progress=value.progress,
        chunk_count=value.chunk_count,
        token_count=value.token_count,
        last_error=_stored_error(value.last_error_code, value.last_error_message),
        created_at=value.created_at,
        updated_at=value.updated_at,
        indexed_at=value.indexed_at,
    )


def _document_version(value: DocumentVersion) -> DocumentVersionData:
    """把版本领域对象投影为不含物理 RAGFlow ID 和 Scope 的安全 API 视图。"""
    return DocumentVersionData(
        id=value.id,
        document_id=value.document_id,
        version_number=value.version_number,
        filename=value.filename,
        mime_type=value.mime_type,
        file_size_bytes=value.file_size_bytes,
        content_sha256=value.content_sha256,
        metadata=value.metadata,
        parser_profile=value.parser_profile_ref,
        chunk_profile=value.chunk_profile_ref,
        status=value.status.value,
        requested_mode=value.requested_mode.value,
        effective_mode=value.effective_mode.value,
        chunk_count=value.chunk_count,
        token_count=value.token_count,
        last_error=_stored_error(value.last_error_code, value.last_error_message),
        created_at=value.created_at,
        updated_at=value.updated_at,
        activated_at=value.activated_at,
        retired_at=value.retired_at,
    )


def _job(value: IngestionJob) -> JobData:
    """将 Worker 任务记录转换为客户端可轮询的任务视图。"""
    return JobData(
        id=value.id,
        type=value.job_type.value,
        status=value.status.value,
        progress=value.progress,
        attempt=value.attempt,
        max_attempts=value.max_attempts,
        knowledge_base_id=value.knowledge_base_id,
        document_id=value.document_id,
        error=_stored_error(value.error_code, value.error_message),
        created_at=value.created_at,
        started_at=value.started_at,
        finished_at=value.finished_at,
    )


def _chunk(value: EngineChunk) -> ChunkData:
    """将引擎文本块转换为稳定 API 字段，并逐项转换原文位置。"""
    return ChunkData(
        id=value.engine_chunk_id,
        document_id=value.document_id,
        content=value.content,
        positions=[
            SourcePositionData(page=position.page, bbox=position.bbox)
            for position in value.positions
        ],
        metadata=value.metadata,
    )


def _page_meta(value: Page) -> PageMeta:
    """从领域分页结果提取通用分页元信息。"""
    return PageMeta(page=value.page, page_size=value.page_size, total=value.total)


def _metadata(value: str | None) -> dict[str, Any]:
    """解析 multipart 表单中的元数据 JSON，并强制最外层为 Object。"""
    if value is None or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise RagError(
            "RAG-DOC-003",
            "文档元数据不是有效 JSON",
            http_status=400,
            retryable=False,
            details=[],
        ) from exc
    if not isinstance(parsed, dict):
        raise RagError(
            "RAG-DOC-003",
            "文档元数据必须是 JSON Object",
            http_status=400,
            retryable=False,
            details=[],
        )
    return parsed


async def _idempotent[ResponseModel: ApiModel](
    *,
    service: OfflineService,
    security: SecurityContext,
    key: str,
    method: str,
    path: str,
    payload: dict[str, Any],
    response_status: int,
    response_model: type[ResponseModel],
    operation: Callable[[], Awaitable[ResponseModel]],
) -> ResponseModel:
    """以 JSON 形式包装写操作，使重复请求能复用原始的强类型响应。

    测试替身可能不实现幂等接口，此时直接执行；真实服务会保存响应 JSON，取回后再
    用目标 Pydantic 类型验证，保证首次与重放响应结构完全一致。
    """
    executor = getattr(service, "execute_idempotent", None)
    if executor is None:
        return await operation()

    async def encoded_operation() -> dict[str, Any]:
        """运行真实路由操作，并转换为可持久化的 JSON 数据。"""
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


async def _upload_sha256(file: UploadFile) -> str:
    """以 1 MiB 分块计算上传内容摘要，再把文件指针复位供后续保存。"""
    digest = hashlib.sha256()
    while chunk := await file.read(1024 * 1024):
        digest.update(chunk)
    await file.seek(0)
    return digest.hexdigest()


def install_offline_routes(
    app: FastAPI,
    *,
    authenticate: AuthenticateDependency,
    get_offline_service: OfflineDependency,
) -> None:
    """向应用注册离线资源路由，并绑定 Direct Tenant/User 上下文与 Service 依赖。"""
    security_dependency = Depends(authenticate)
    service_dependency = Depends(get_offline_service)

    @app.post(
        "/v1/management/knowledge-bases",
        response_model=KnowledgeBaseResponse,
        status_code=status.HTTP_201_CREATED,
        tags=["KnowledgeBases"],
    )
    async def create_knowledge_base(
        request: Request,
        body: CreateKnowledgeBaseRequest,
        idempotency_key: Annotated[
            str,
            Header(
                alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[\x21-\x7E]+$"
            ),
        ],
        security: SecurityContext = security_dependency,
        service: OfflineService = service_dependency,
    ) -> KnowledgeBaseResponse:
        """幂等创建知识库；外部引擎创建完成后返回 201。"""
        async def operation() -> KnowledgeBaseResponse:
            value = await service.create_knowledge_base(
                security,
                CreateKnowledgeBaseInput(
                    scope_type=KnowledgeBaseScopeType(body.scope_type),
                    external_id=body.external_id,
                    name=body.name,
                    description=body.description,
                    parser_profile=body.parser_profile,
                    chunk_profile=body.chunk_profile,
                    embedding_profile=body.embedding_profile,
                    retrieval_profile=body.retrieval_profile,
                ),
            )
            return KnowledgeBaseResponse(
                request_id=_request_id(request),
                data=_knowledge_base(value),
            )

        return await _idempotent(
            service=service,
            security=security,
            key=idempotency_key,
            method="POST",
            path="/v1/management/knowledge-bases",
            payload=body.model_dump(mode="json"),
            response_status=status.HTTP_201_CREATED,
            response_model=KnowledgeBaseResponse,
            operation=operation,
        )

    @app.get(
        "/v1/knowledge-bases",
        response_model=KnowledgeBasePageResponse,
        tags=["KnowledgeBases"],
    )
    async def list_shared_knowledge_bases(
        request: Request,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
        name: Annotated[str | None, Query(max_length=128)] = None,
        status_filter: Annotated[
            str | None,
            Query(alias="status", max_length=32),
        ] = None,
        scope_type: Annotated[Literal["TENANT", "SYSTEM"] | None, Query()] = None,
        security: SecurityContext = security_dependency,
        service: OfflineService = service_dependency,
    ) -> KnowledgeBasePageResponse:
        """只读列出当前 Tenant/User 可访问的租户共享库和系统共享库。

        路由不经过管理授权，也不提供创建、修改或删除能力；知识库成员 ACL 上线后由
        应用层访问策略统一过滤。响应保留 ``scope_type``，供用户界面标明资源归属。
        """
        result = await service.list_shared_knowledge_bases(
            security,
            page=page,
            page_size=page_size,
            name=name,
            status=status_filter,
            scope_type=scope_type,
        )
        return KnowledgeBasePageResponse(
            request_id=_request_id(request),
            data=[
                _knowledge_base(item) for item in result.items if isinstance(item, KnowledgeBase)
            ],
            meta=_page_meta(result),
        )

    @app.get(
        "/v1/management/knowledge-bases",
        response_model=KnowledgeBasePageResponse,
        tags=["KnowledgeBases"],
    )
    async def list_knowledge_bases(
        request: Request,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
        name: Annotated[str | None, Query(max_length=128)] = None,
        status_filter: Annotated[
            str | None,
            Query(alias="status", max_length=32),
        ] = None,
        scope_type: Annotated[Literal["TENANT", "SYSTEM"] | None, Query()] = None,
        security: SecurityContext = security_dependency,
        service: OfflineService = service_dependency,
    ) -> KnowledgeBasePageResponse:
        """按名称/状态过滤并分页列出当前安全范围的知识库。"""
        result = await service.list_knowledge_bases(
            security,
            page=page,
            page_size=page_size,
            name=name,
            status=status_filter,
            scope_type=scope_type,
        )
        return KnowledgeBasePageResponse(
            request_id=_request_id(request),
            data=[
                _knowledge_base(item) for item in result.items if isinstance(item, KnowledgeBase)
            ],
            meta=_page_meta(result),
        )

    @app.get(
        "/v1/management/knowledge-bases/{knowledge_base_id}",
        response_model=KnowledgeBaseResponse,
        tags=["KnowledgeBases"],
    )
    async def get_knowledge_base(
        request: Request,
        knowledge_base_id: UUID,
        security: SecurityContext = security_dependency,
        service: OfflineService = service_dependency,
    ) -> KnowledgeBaseResponse:
        """读取当前安全范围内的单个知识库。"""
        value = await service.get_knowledge_base(security, knowledge_base_id)
        return KnowledgeBaseResponse(
            request_id=_request_id(request),
            data=_knowledge_base(value),
        )

    @app.patch(
        "/v1/management/knowledge-bases/{knowledge_base_id}",
        response_model=KnowledgeBaseResponse,
        tags=["KnowledgeBases"],
    )
    async def update_knowledge_base(
        request: Request,
        knowledge_base_id: UUID,
        body: UpdateKnowledgeBaseRequest,
        security: SecurityContext = security_dependency,
        service: OfflineService = service_dependency,
    ) -> KnowledgeBaseResponse:
        """更新知识库可编辑字段与 Profile，并同步外部引擎。"""
        value = await service.update_knowledge_base(
            security,
            knowledge_base_id,
            UpdateKnowledgeBaseInput(
                name=body.name,
                description=body.description,
                parser_profile=body.parser_profile,
                chunk_profile=body.chunk_profile,
                retrieval_profile=body.retrieval_profile,
            ),
        )
        return KnowledgeBaseResponse(
            request_id=_request_id(request),
            data=_knowledge_base(value),
        )

    @app.delete(
        "/v1/management/knowledge-bases/{knowledge_base_id}",
        response_model=JobAcceptedResponse,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["KnowledgeBases"],
    )
    async def delete_knowledge_base(
        request: Request,
        knowledge_base_id: UUID,
        idempotency_key: Annotated[
            str,
            Header(
                alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[\x21-\x7E]+$"
            ),
        ],
        force: bool = False,
        security: SecurityContext = security_dependency,
        service: OfflineService = service_dependency,
    ) -> JobAcceptedResponse:
        """创建知识库异步删除任务，立即返回可轮询任务 ID。"""
        async def operation() -> JobAcceptedResponse:
            job = await service.delete_knowledge_base(
                security,
                knowledge_base_id,
                force=force,
            )
            return JobAcceptedResponse(
                request_id=_request_id(request),
                data=JobAcceptedData(job_id=job.id, status=job.status.value),
            )

        return await _idempotent(
            service=service,
            security=security,
            key=idempotency_key,
            method="DELETE",
            path=f"/v1/management/knowledge-bases/{knowledge_base_id}",
            payload={"force": force},
            response_status=status.HTTP_202_ACCEPTED,
            response_model=JobAcceptedResponse,
            operation=operation,
        )

    @app.post(
        "/v1/management/knowledge-bases/{knowledge_base_id}/documents",
        response_model=DocumentAcceptedResponse,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["Documents"],
    )
    async def upload_document(
        request: Request,
        knowledge_base_id: UUID,
        file: Annotated[UploadFile, File()],
        idempotency_key: Annotated[
            str,
            Header(
                alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[\x21-\x7E]+$"
            ),
        ],
        external_id: Annotated[str | None, Form(max_length=128)] = None,
        metadata: Annotated[str | None, Form()] = None,
        parser_profile: Annotated[
            str | None,
            Form(
                max_length=128,
                description=(
                    "可选显式 Parser；留空时先保留兼容的知识库默认值，"
                    "不兼容时按配置规则自动选择"
                ),
            ),
        ] = None,
        chunk_profile: Annotated[
            str | None,
            Form(
                max_length=128,
                description="可选显式 Chunk；留空时按文件族自动选择",
            ),
        ] = None,
        auto_parse: Annotated[bool, Form()] = True,
        security: SecurityContext = security_dependency,
        service: OfflineService = service_dependency,
    ) -> DocumentAcceptedResponse:
        """校验 multipart 文件与摘要、流式暂存，并创建异步上传任务。"""
        async def chunks() -> AsyncIterator[bytes]:
            """按固定大小读取上传流，避免把整个文件一次性装入内存。"""
            while chunk := await file.read(1024 * 1024):
                yield chunk

        parsed_metadata = _metadata(metadata)
        filename_value = file.filename or "upload"
        mime_type = file.content_type or "application/octet-stream"
        content_sha256 = await _upload_sha256(file)

        async def operation() -> DocumentAcceptedResponse:
            accepted = await service.upload_document(
                security,
                knowledge_base_id,
                UploadDocumentInput(
                    filename=filename_value,
                    mime_type=mime_type,
                    stream=chunks(),
                    external_id=external_id,
                    metadata=parsed_metadata,
                    parser_profile=parser_profile,
                    chunk_profile=chunk_profile,
                    auto_parse=auto_parse,
                ),
            )
            return DocumentAcceptedResponse(
                request_id=_request_id(request),
                data=DocumentAcceptedData(
                    document_id=accepted.document.id,
                    job_id=accepted.job.id,
                    status=accepted.document.status.value,
                ),
            )

        try:
            return await _idempotent(
                service=service,
                security=security,
                key=idempotency_key,
                method="POST",
                path=f"/v1/management/knowledge-bases/{knowledge_base_id}/documents",
                payload={
                    "filename": filename_value,
                    "mime_type": mime_type,
                    "external_id": external_id,
                    "metadata": parsed_metadata,
                    "parser_profile": parser_profile,
                    "chunk_profile": chunk_profile,
                    "auto_parse": auto_parse,
                    "file_sha256": content_sha256,
                },
                response_status=status.HTTP_202_ACCEPTED,
                response_model=DocumentAcceptedResponse,
                operation=operation,
            )
        finally:
            await file.close()

    @app.get(
        "/v1/knowledge-bases/{knowledge_base_id}/documents",
        response_model=DocumentPageResponse,
        tags=["Documents"],
    )
    async def list_shared_documents(
        request: Request,
        knowledge_base_id: UUID,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
        status_filter: Annotated[
            str | None,
            Query(alias="status", max_length=32),
        ] = None,
        filename: Annotated[str | None, Query(max_length=255)] = None,
        external_id: Annotated[str | None, Query(max_length=128)] = None,
        security: SecurityContext = security_dependency,
        service: OfflineService = service_dependency,
    ) -> DocumentPageResponse:
        """只读分页列出一个可访问共享知识库中的文档。

        路径知识库 ID 必须通过 TENANT/SYSTEM 访问策略；该接口不会上传、解析、重试
        或修改文档，跨租户和未来 ACL 不可见资源仍统一返回 404。
        """
        result = await service.list_shared_documents(
            security,
            knowledge_base_id,
            page=page,
            page_size=page_size,
            status=status_filter,
            filename=filename,
            external_id=external_id,
        )
        return DocumentPageResponse(
            request_id=_request_id(request),
            data=[_document(item) for item in result.items if isinstance(item, DocumentRecord)],
            meta=_page_meta(result),
        )

    @app.get(
        "/v1/management/knowledge-bases/{knowledge_base_id}/documents",
        response_model=DocumentPageResponse,
        tags=["Documents"],
    )
    async def list_documents(
        request: Request,
        knowledge_base_id: UUID,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
        status_filter: Annotated[
            str | None,
            Query(alias="status", max_length=32),
        ] = None,
        filename: Annotated[str | None, Query(max_length=255)] = None,
        external_id: Annotated[str | None, Query(max_length=128)] = None,
        security: SecurityContext = security_dependency,
        service: OfflineService = service_dependency,
    ) -> DocumentPageResponse:
        """在指定知识库内按状态、文件名或外部 ID 分页筛选文档。"""
        result = await service.list_documents(
            security,
            knowledge_base_id,
            page=page,
            page_size=page_size,
            status=status_filter,
            filename=filename,
            external_id=external_id,
        )
        return DocumentPageResponse(
            request_id=_request_id(request),
            data=[_document(item) for item in result.items if isinstance(item, DocumentRecord)],
            meta=_page_meta(result),
        )

    @app.get(
        "/v1/documents/{document_id}",
        response_model=DocumentResponse,
        tags=["Documents"],
    )
    async def get_shared_document(
        request: Request,
        document_id: UUID,
        security: SecurityContext = security_dependency,
        service: OfflineService = service_dependency,
    ) -> DocumentResponse:
        """只读返回可访问共享知识库中的单个文档详情。"""
        value = await service.get_shared_document(security, document_id)
        return DocumentResponse(
            request_id=_request_id(request),
            data=_document(value),
        )

    @app.get(
        "/v1/management/documents/{document_id}",
        response_model=DocumentResponse,
        tags=["Documents"],
    )
    async def get_document(
        request: Request,
        document_id: UUID,
        security: SecurityContext = security_dependency,
        service: OfflineService = service_dependency,
    ) -> DocumentResponse:
        """读取单个文档及其最新解析状态和错误。"""
        value = await service.get_document(security, document_id)
        return DocumentResponse(
            request_id=_request_id(request),
            data=_document(value),
        )

    @app.post(
        "/v1/management/documents/{document_id}/versions",
        response_model=DocumentVersionAcceptedResponse,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["Documents"],
    )
    async def upload_document_version(
        request: Request,
        document_id: UUID,
        file: Annotated[UploadFile, File()],
        idempotency_key: Annotated[
            str,
            Header(
                alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[\x21-\x7E]+$"
            ),
        ],
        metadata: Annotated[str | None, Form()] = None,
        parser_profile: Annotated[
            str | None,
            Form(
                max_length=128,
                description=(
                    "可选显式 Parser；留空时先沿用兼容的当前文档 Parser，"
                    "不兼容时按配置规则自动选择"
                ),
            ),
        ] = None,
        chunk_profile: Annotated[
            str | None,
            Form(
                max_length=128,
                description="可选显式 Chunk；留空时按新文件类型自动选择",
            ),
        ] = None,
        mode: Annotated[Literal["AUTO", "SAFE_REPLACE"], Form()] = "AUTO",
        security: SecurityContext = security_dependency,
        service: OfflineService = service_dependency,
    ) -> DocumentVersionAcceptedResponse:
        """接收逻辑文档新文件版本，旧版本继续服务直到异步发布成功。

        与首次上传相同，幂等摘要包含文件 SHA-256 而不包含正文。重复使用同一个
        ``Idempotency-Key`` 和相同文件会返回原始版本/Job；同键不同文件由统一幂等
        服务拒绝，避免并发创建两个版本号。
        """

        async def chunks() -> AsyncIterator[bytes]:
            """按 1 MiB 流式读取候选文件，控制 Gateway 内存占用。"""
            while chunk := await file.read(1024 * 1024):
                yield chunk

        parsed_metadata = _metadata(metadata)
        filename_value = file.filename or "update"
        mime_type = file.content_type or "application/octet-stream"
        content_sha256 = await _upload_sha256(file)

        async def operation() -> DocumentVersionAcceptedResponse:
            """创建候选版本和 UPDATE_DOCUMENT Job，并返回稳定公开字段。"""
            accepted = await service.upload_document_version(
                security,
                document_id,
                UploadDocumentVersionInput(
                    filename=filename_value,
                    mime_type=mime_type,
                    stream=chunks(),
                    metadata=parsed_metadata,
                    parser_profile=parser_profile,
                    chunk_profile=chunk_profile,
                    mode=DocumentUpdateMode(mode),
                ),
            )
            return DocumentVersionAcceptedResponse(
                request_id=_request_id(request),
                data=DocumentVersionAcceptedData(
                    document_id=document_id,
                    version_id=accepted.version.id,
                    version_number=accepted.version.version_number,
                    job_id=accepted.job.id,
                    status=accepted.version.status.value,
                    effective_mode=accepted.version.effective_mode.value,
                ),
            )

        try:
            return await _idempotent(
                service=service,
                security=security,
                key=idempotency_key,
                method="POST",
                path=f"/v1/management/documents/{document_id}/versions",
                payload={
                    "filename": filename_value,
                    "mime_type": mime_type,
                    "metadata": parsed_metadata,
                    "parser_profile": parser_profile,
                    "chunk_profile": chunk_profile,
                    "mode": mode,
                    "file_sha256": content_sha256,
                },
                response_status=status.HTTP_202_ACCEPTED,
                response_model=DocumentVersionAcceptedResponse,
                operation=operation,
            )
        finally:
            await file.close()

    @app.get(
        "/v1/management/documents/{document_id}/versions",
        response_model=DocumentVersionPageResponse,
        tags=["Documents"],
    )
    async def list_document_versions(
        request: Request,
        document_id: UUID,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
        security: SecurityContext = security_dependency,
        service: OfflineService = service_dependency,
    ) -> DocumentVersionPageResponse:
        """分页读取当前 Scope 下一个逻辑文档的版本历史。"""
        result = await service.list_document_versions(
            security,
            document_id,
            page=page,
            page_size=page_size,
        )
        return DocumentVersionPageResponse(
            request_id=_request_id(request),
            data=[
                _document_version(item)
                for item in result.items
                if isinstance(item, DocumentVersion)
            ],
            meta=_page_meta(result),
        )

    @app.post(
        "/v1/management/documents/{document_id}/versions/{version_id}/rollback",
        response_model=JobAcceptedResponse,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["Documents"],
    )
    async def rollback_document_version(
        request: Request,
        document_id: UUID,
        version_id: UUID,
        idempotency_key: Annotated[
            str,
            Header(
                alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[\x21-\x7E]+$"
            ),
        ],
        security: SecurityContext = security_dependency,
        service: OfflineService = service_dependency,
    ) -> JobAcceptedResponse:
        """创建异步回滚任务，仍通过完整激活流程切换 Chunk 和缓存 Revision。"""

        async def operation() -> JobAcceptedResponse:
            """验证目标退役版本并保存可轮询的 UPDATE_DOCUMENT Job。"""
            job = await service.rollback_document_version(
                security,
                document_id,
                version_id,
            )
            return JobAcceptedResponse(
                request_id=_request_id(request),
                data=JobAcceptedData(job_id=job.id, status=job.status.value),
            )

        return await _idempotent(
            service=service,
            security=security,
            key=idempotency_key,
            method="POST",
            path=f"/v1/management/documents/{document_id}/versions/{version_id}/rollback",
            payload={},
            response_status=status.HTTP_202_ACCEPTED,
            response_model=JobAcceptedResponse,
            operation=operation,
        )

    @app.delete(
        "/v1/management/documents/{document_id}",
        response_model=JobAcceptedResponse,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["Documents"],
    )
    async def delete_document(
        request: Request,
        document_id: UUID,
        idempotency_key: Annotated[
            str,
            Header(
                alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[\x21-\x7E]+$"
            ),
        ],
        cancel_active: bool = False,
        security: SecurityContext = security_dependency,
        service: OfflineService = service_dependency,
    ) -> JobAcceptedResponse:
        """创建文档异步删除任务，不在 HTTP 请求中等待外部索引删除。"""
        async def operation() -> JobAcceptedResponse:
            job = await service.delete_document(
                security,
                document_id,
                cancel_active=cancel_active,
            )
            return JobAcceptedResponse(
                request_id=_request_id(request),
                data=JobAcceptedData(job_id=job.id, status=job.status.value),
            )

        return await _idempotent(
            service=service,
            security=security,
            key=idempotency_key,
            method="DELETE",
            path=f"/v1/management/documents/{document_id}",
            payload={"cancel_active": cancel_active},
            response_status=status.HTTP_202_ACCEPTED,
            response_model=JobAcceptedResponse,
            operation=operation,
        )

    @app.post(
        "/v1/management/documents/{document_id}/parse",
        response_model=JobAcceptedResponse,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["Documents"],
    )
    async def parse_document(
        request: Request,
        document_id: UUID,
        idempotency_key: Annotated[
            str,
            Header(
                alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[\x21-\x7E]+$"
            ),
        ],
        security: SecurityContext = security_dependency,
        service: OfflineService = service_dependency,
    ) -> JobAcceptedResponse:
        """为已上传文档创建首次解析任务。"""
        async def operation() -> JobAcceptedResponse:
            job = await service.create_parse_job(security, document_id)
            return JobAcceptedResponse(
                request_id=_request_id(request),
                data=JobAcceptedData(job_id=job.id, status=job.status.value),
            )

        return await _idempotent(
            service=service,
            security=security,
            key=idempotency_key,
            method="POST",
            path=f"/v1/management/documents/{document_id}/parse",
            payload={},
            response_status=status.HTTP_202_ACCEPTED,
            response_model=JobAcceptedResponse,
            operation=operation,
        )

    @app.post(
        "/v1/management/documents/{document_id}/reparse",
        response_model=JobAcceptedResponse,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["Documents"],
    )
    async def reparse_document(
        request: Request,
        document_id: UUID,
        idempotency_key: Annotated[
            str,
            Header(
                alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[\x21-\x7E]+$"
            ),
        ],
        body: ReparseDocumentRequest | None = None,
        security: SecurityContext = security_dependency,
        service: OfflineService = service_dependency,
    ) -> JobAcceptedResponse:
        """使用可选新 Profile 创建重解析任务，并替换已有文本块。"""
        value = body or ReparseDocumentRequest()

        async def operation() -> JobAcceptedResponse:
            job = await service.create_parse_job(
                security,
                document_id,
                reparse=True,
                parser_profile=value.parser_profile,
                chunk_profile=value.chunk_profile,
            )
            return JobAcceptedResponse(
                request_id=_request_id(request),
                data=JobAcceptedData(job_id=job.id, status=job.status.value),
            )

        return await _idempotent(
            service=service,
            security=security,
            key=idempotency_key,
            method="POST",
            path=f"/v1/management/documents/{document_id}/reparse",
            payload=value.model_dump(mode="json"),
            response_status=status.HTTP_202_ACCEPTED,
            response_model=JobAcceptedResponse,
            operation=operation,
        )

    @app.post(
        "/v1/management/documents/{document_id}/retry",
        response_model=JobAcceptedResponse,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["Documents"],
    )
    async def retry_document(
        request: Request,
        document_id: UUID,
        idempotency_key: Annotated[
            str,
            Header(
                alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[\x21-\x7E]+$"
            ),
        ],
        security: SecurityContext = security_dependency,
        service: OfflineService = service_dependency,
    ) -> JobAcceptedResponse:
        """根据文档最近失败阶段创建合适的重试任务。"""
        async def operation() -> JobAcceptedResponse:
            job = await service.retry_document(security, document_id)
            return JobAcceptedResponse(
                request_id=_request_id(request),
                data=JobAcceptedData(job_id=job.id, status=job.status.value),
            )

        return await _idempotent(
            service=service,
            security=security,
            key=idempotency_key,
            method="POST",
            path=f"/v1/management/documents/{document_id}/retry",
            payload={},
            response_status=status.HTTP_202_ACCEPTED,
            response_model=JobAcceptedResponse,
            operation=operation,
        )

    @app.get(
        "/v1/documents/{document_id}/chunks",
        response_model=ChunkPageResponse,
        tags=["Documents"],
    )
    async def list_shared_chunks(
        request: Request,
        document_id: UUID,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
        security: SecurityContext = security_dependency,
        service: OfflineService = service_dependency,
    ) -> ChunkPageResponse:
        """只读分页返回可访问共享文档的解析文本块与来源位置。"""
        result = await service.list_shared_chunks(
            security,
            document_id,
            page=page,
            page_size=page_size,
        )
        return ChunkPageResponse(
            request_id=_request_id(request),
            data=[_chunk(item) for item in result.items if isinstance(item, EngineChunk)],
            meta=_page_meta(result),
        )

    @app.get(
        "/v1/management/documents/{document_id}/source",
        response_class=StreamingResponse,
        responses={200: {"content": {"application/pdf": {
            "schema": {"type": "string", "format": "binary"}
        }}}},
        tags=["Documents"],
    )
    async def get_document_source(
        document_id: UUID,
        document_revision: Annotated[int, Query(ge=1)],
        security: SecurityContext = security_dependency,
        service: OfflineService = service_dependency,
    ) -> StreamingResponse:
        """只读转发固定修订的 PDF；不暴露存储地址，禁止缓存，完成/取消均关闭下游流。"""
        source = await service.get_document_source(
            security, document_id, document_revision=document_revision
        )
        return StreamingResponse(
            source.content,
            media_type="application/pdf",
            headers={
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": (
                    f"inline; filename*=UTF-8''{quote(source.filename, safe='')}"
                ),
            },
            background=BackgroundTask(source.close),
        )

    @app.get(
        "/v1/management/documents/{document_id}/chunks",
        response_model=ChunkPageResponse,
        tags=["Documents"],
    )
    async def list_chunks(
        request: Request,
        document_id: UUID,
        page: Annotated[int, Query(ge=1)] = 1,
        page_size: Annotated[int, Query(ge=1, le=100)] = 20,
        document_revision: Annotated[int | None, Query(ge=1)] = None,
        security: SecurityContext = security_dependency,
        service: OfflineService = service_dependency,
    ) -> ChunkPageResponse:
        """分页读取原文位置；预览可附修订号，拒绝与原文件版本不一致的切块。"""
        result = await service.list_chunks(
            security,
            document_id,
            page=page,
            page_size=page_size,
            document_revision=document_revision,
        )
        return ChunkPageResponse(
            request_id=_request_id(request),
            data=[_chunk(item) for item in result.items if isinstance(item, EngineChunk)],
            meta=_page_meta(result),
        )

    @app.get(
        "/v1/management/jobs/{job_id}",
        response_model=JobResponse,
        tags=["Jobs"],
    )
    async def get_job(
        request: Request,
        job_id: UUID,
        security: SecurityContext = security_dependency,
        service: OfflineService = service_dependency,
    ) -> JobResponse:
        """读取异步任务状态，供客户端轮询进度。"""
        value = await service.get_job(security, job_id)
        return JobResponse(request_id=_request_id(request), data=_job(value))

    @app.post(
        "/v1/management/jobs/{job_id}/cancel",
        response_model=JobAcceptedResponse,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["Jobs"],
    )
    async def cancel_job(
        request: Request,
        job_id: UUID,
        idempotency_key: Annotated[
            str,
            Header(
                alias="Idempotency-Key", min_length=1, max_length=128, pattern=r"^[\x21-\x7E]+$"
            ),
        ],
        security: SecurityContext = security_dependency,
        service: OfflineService = service_dependency,
    ) -> JobAcceptedResponse:
        """请求取消尚未进入不可中断阶段的任务，并返回任务新状态。"""
        async def operation() -> JobAcceptedResponse:
            job = await service.cancel_job(security, job_id)
            return JobAcceptedResponse(
                request_id=_request_id(request),
                data=JobAcceptedData(job_id=job.id, status=job.status.value),
            )

        return await _idempotent(
            service=service,
            security=security,
            key=idempotency_key,
            method="POST",
            path=f"/v1/management/jobs/{job_id}/cancel",
            payload={},
            response_status=status.HTTP_202_ACCEPTED,
            response_model=JobAcceptedResponse,
            operation=operation,
        )
