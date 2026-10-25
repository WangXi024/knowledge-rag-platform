"""离线资源用例：知识库、上传文档、解析/删除任务和文本块查询。

本服务位于 HTTP 与基础设施之间，负责权限范围、Profile、状态机、乐观锁和补偿清理。
需要较长时间的上传到引擎、解析和删除不会占住 HTTP 请求，而是写入 ``IngestionJob``
交给 Worker 执行。
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from rag_service.application.access import (
    KnowledgeBaseAccessPolicy,
    ManagementAccessPolicy,
    SharedKnowledgeBaseAccessPolicy,
    TrustedNetworkManagementAccessPolicy,
)
from rag_service.application.idempotency import IdempotencyService
from rag_service.application.profiles import ProfileCatalog
from rag_service.domain.errors import ErrorDetail, RagError, ResourceNotFoundError
from rag_service.domain.models import (
    DocumentRecord,
    DocumentStatus,
    DocumentUpdateMode,
    DocumentVersion,
    DocumentVersionStatus,
    IngestionJob,
    JobStatus,
    JobType,
    KnowledgeBase,
    KnowledgeBaseScopeType,
    KnowledgeBaseStatus,
    utc_now,
)
from rag_service.domain.ports import (
    CreateDatasetCommand,
    DocumentRepository,
    DocumentSource,
    DocumentVersionRepository,
    EngineChunk,
    GetDocumentSourceQuery,
    JobRepository,
    KnowledgeBaseRepository,
    ListEngineChunksQuery,
    Page,
    RagEnginePort,
    StagedFileMetadata,
    UpdateEngineDatasetCommand,
    UploadStagingPort,
)
from rag_service.domain.state import (
    DOCUMENT_TRANSITIONS,
    JOB_TRANSITIONS,
    KNOWLEDGE_BASE_TRANSITIONS,
    require_transition,
)
from rag_service.security.context import SecurityContext
from rag_service.settings import JobsSection

_METADATA_KEY = re.compile(r"^[a-z0-9_]{1,64}$")


@dataclass(frozen=True, slots=True)
class CreateKnowledgeBaseInput:
    """创建知识库的应用层输入，不包含由认证上下文提供的租户信息。"""
    name: str
    parser_profile: str
    chunk_profile: str
    embedding_profile: str
    retrieval_profile: str
    scope_type: KnowledgeBaseScopeType = KnowledgeBaseScopeType.TENANT
    external_id: str | None = None
    description: str | None = None


@dataclass(frozen=True, slots=True)
class UploadDocumentInput:
    """上传文件流、展示信息、元数据、可选 Profile 和是否自动解析。"""
    filename: str
    mime_type: str
    stream: AsyncIterator[bytes]
    external_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    parser_profile: str | None = None
    chunk_profile: str | None = None
    auto_parse: bool = True


@dataclass(frozen=True, slots=True)
class DocumentAccepted:
    """文档已安全暂存并创建任务后的组合结果。"""
    document: DocumentRecord
    job: IngestionJob


@dataclass(frozen=True, slots=True)
class UploadDocumentVersionInput:
    """替换逻辑文档内容时提交的新文件、Metadata、Profile 和执行模式。

    文件名和 MIME 描述候选版本；``metadata`` 只覆盖用户拥有的旧字段，系统生成的
    ``lazytt_*`` 字段会在新版本解析完成后重新计算，避免把旧索引状态带到新正文。
    """

    filename: str
    mime_type: str
    stream: AsyncIterator[bytes]
    metadata: dict[str, Any] = field(default_factory=dict)
    parser_profile: str | None = None
    chunk_profile: str | None = None
    mode: DocumentUpdateMode = DocumentUpdateMode.AUTO


@dataclass(frozen=True, slots=True)
class DocumentVersionAccepted:
    """候选版本已安全暂存并创建异步更新任务后的组合结果。"""

    version: DocumentVersion
    job: IngestionJob


@dataclass(frozen=True, slots=True)
class UpdateKnowledgeBaseInput:
    """知识库局部更新；``None`` 表示保留原值。"""
    name: str | None = None
    description: str | None = None
    parser_profile: str | None = None
    chunk_profile: str | None = None
    embedding_profile: str | None = None
    retrieval_profile: str | None = None


class OfflineService:
    """编排所有非实时生成类业务用例的应用服务。"""
    def __init__(
        self,
        knowledge_bases: KnowledgeBaseRepository,
        documents: DocumentRepository,
        jobs: JobRepository,
        staging: UploadStagingPort,
        rag_engine: RagEnginePort,
        profiles: ProfileCatalog,
        job_settings: JobsSection,
        embedding_model: str,
        idempotency: IdempotencyService | None = None,
        versions: DocumentVersionRepository | None = None,
        access_policy: KnowledgeBaseAccessPolicy | None = None,
        management_access: ManagementAccessPolicy | None = None,
    ) -> None:
        """注入持久化、暂存、RAG 引擎、Profile、任务配置和可选版本仓库。

        ``versions`` 允许旧单元测试继续构造不支持更新的 Service；真实应用始终注入
        PostgreSQL 实现。调用版本 API 时若部署未完成迁移，会返回明确能力不可用，
        而不是绕过版本表直接覆盖当前 RAGFlow 文档。
        """
        self._knowledge_bases = knowledge_bases
        self._documents = documents
        self._jobs = jobs
        self._staging = staging
        self._rag_engine = rag_engine
        self._profiles = profiles
        self._job_settings = job_settings
        self._embedding_model = embedding_model
        self._idempotency = idempotency
        self._versions = versions
        self._access_policy = access_policy or SharedKnowledgeBaseAccessPolicy()
        self._management_access = management_access or TrustedNetworkManagementAccessPolicy()

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
        """让 API 层复用统一幂等协议；测试未注入时直接执行操作。"""
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

    async def create_knowledge_base(
        self,
        security: SecurityContext,
        value: CreateKnowledgeBaseInput,
    ) -> KnowledgeBase:
        """校验 Profile，在本地创建记录，再同步创建 RAGFlow 数据集。

        本地先写 ``CREATING`` 使过程可观察；外部失败则标记 ``CREATE_FAILED``。外部
        成功但本地映射因并发冲突未保存时返回可重试错误，避免错误宣称创建完成。
        """
        await self._management_access.require_access(security, "knowledge_base.create")
        resource_scope_id = self._access_policy.resource_scope_for_creation(
            security,
            value.scope_type,
        )
        resource_security = security.for_resource_scope(resource_scope_id)
        configuration = self._profiles.dataset_configuration(
            parser_ref=value.parser_profile,
            chunk_ref=value.chunk_profile,
            embedding_ref=value.embedding_profile,
            embedding_model=self._embedding_model,
        )
        self._profiles.get("RETRIEVAL", value.retrieval_profile)
        knowledge_base = KnowledgeBase(
            security_scope_id=resource_scope_id,
            scope_type=value.scope_type,
            external_id=value.external_id,
            name=value.name,
            description=value.description,
            parser_profile_ref=value.parser_profile,
            chunk_profile_ref=value.chunk_profile,
            embedding_profile_ref=value.embedding_profile,
            retrieval_profile_ref=value.retrieval_profile,
        )
        await self._knowledge_bases.add(
            resource_scope_id,
            knowledge_base,
        )
        try:
            engine_dataset = await self._rag_engine.create_dataset(
                CreateDatasetCommand(
                    security=resource_security,
                    knowledge_base_id=knowledge_base.id,
                    name=knowledge_base.name,
                    description=knowledge_base.description,
                    chunk_method=configuration.chunk_method,
                    parser_config=configuration.parser_config,
                    embedding_model=configuration.embedding_model,
                )
            )
        except Exception:
            knowledge_base.status = require_transition(
                knowledge_base.status,
                KnowledgeBaseStatus.CREATE_FAILED,
                KNOWLEDGE_BASE_TRANSITIONS,
            )
            await self._knowledge_bases.update(
                resource_scope_id,
                knowledge_base,
                knowledge_base.row_version,
            )
            raise
        knowledge_base.ragflow_dataset_id = engine_dataset.engine_id
        knowledge_base.status = require_transition(
            knowledge_base.status,
            KnowledgeBaseStatus.READY,
            KNOWLEDGE_BASE_TRANSITIONS,
        )
        if not await self._knowledge_bases.update(
            resource_scope_id,
            knowledge_base,
            knowledge_base.row_version,
        ):
            raise RagError(
                "RAG-RF-007",
                "RAGFlow 创建成功但本地映射未完成",
                http_status=409,
                retryable=True,
            )
        return knowledge_base

    async def get_knowledge_base(
        self,
        security: SecurityContext,
        knowledge_base_id: UUID,
    ) -> KnowledgeBase:
        """管理用例读取知识库；先走可替换管理授权，再复用普通共享读取规则。"""
        await self._management_access.require_access(security, "knowledge_base.read")
        return await self.get_shared_knowledge_base(security, knowledge_base_id)

    async def get_shared_knowledge_base(
        self,
        security: SecurityContext,
        knowledge_base_id: UUID,
    ) -> KnowledgeBase:
        """按当前共享范围读取知识库，跨租户与真正不存在统一表现为 404。

        该方法不要求管理员身份，只执行 ``KnowledgeBaseAccessPolicy``。未来接入成员
        ACL 后，用户端只读 API 会在同一策略入口收窄结果，不需要调用管理接口。
        """
        candidate_scopes = self._access_policy.candidate_resource_scopes(security)
        value = await self._knowledge_bases.get_accessible(
            candidate_scopes,
            knowledge_base_id,
        )
        if value is None:
            raise ResourceNotFoundError("RAG-KB-001", "知识库不存在")
        readable = await self._access_policy.filter_readable(security, (value,))
        if not readable:
            raise ResourceNotFoundError("RAG-KB-001", "知识库不存在")
        return readable[0]

    async def list_knowledge_bases(
        self,
        security: SecurityContext,
        *,
        page: int,
        page_size: int,
        name: str | None = None,
        status: str | None = None,
        scope_type: str | None = None,
    ) -> Page:
        """管理用例分页列出知识库；管理授权与共享资源可见性分别判断。"""
        await self._management_access.require_access(security, "knowledge_base.list")
        return await self.list_shared_knowledge_bases(
            security,
            page=page,
            page_size=page_size,
            name=name,
            status=status,
            scope_type=scope_type,
        )

    async def list_shared_knowledge_bases(
        self,
        security: SecurityContext,
        *,
        page: int,
        page_size: int,
        name: str | None = None,
        status: str | None = None,
        scope_type: str | None = None,
    ) -> Page:
        """分页列出当前主体可读的 TENANT/SYSTEM 共享知识库。

        当前策略返回同租户全部租户库和全部系统库；未来 ACL 可以过滤成员不可读库。
        方法只读 PostgreSQL，不调用 RAGFlow，也不会创建、更新或删除任何资源。
        """
        candidate_scopes = self._access_policy.candidate_resource_scopes(security)
        items, total = await self._knowledge_bases.list_accessible(
            candidate_scopes,
            page=page,
            page_size=page_size,
            name=name,
            status=status,
            scope_type=scope_type,
        )
        readable = await self._access_policy.filter_readable(security, items)
        return Page(items=readable, page=page, page_size=page_size, total=total)

    async def update_knowledge_base(
        self,
        security: SecurityContext,
        knowledge_base_id: UUID,
        value: UpdateKnowledgeBaseInput,
    ) -> KnowledgeBase:
        """更新可编辑字段和 Profile，并在配置变化时递增缓存修订号。

        先更新外部数据集，成功后再保存本地映射；Parser/Chunk 变化会重新生成引擎
        parser_config，名称/描述变化则只更新对应字段。
        """
        knowledge_base = await self.get_knowledge_base(security, knowledge_base_id)
        await self._management_access.require_access(security, "knowledge_base.update")
        security = security.for_resource_scope(knowledge_base.security_scope_id)
        if knowledge_base.status != KnowledgeBaseStatus.READY:
            raise RagError(
                "RAG-KB-003",
                "知识库当前状态不可修改",
                http_status=409,
                retryable=False,
            )
        parser_ref = value.parser_profile or knowledge_base.parser_profile_ref
        chunk_ref = value.chunk_profile or knowledge_base.chunk_profile_ref
        embedding_ref = value.embedding_profile or knowledge_base.embedding_profile_ref
        retrieval_ref = value.retrieval_profile or knowledge_base.retrieval_profile_ref
        configuration = self._profiles.dataset_configuration(
            parser_ref=parser_ref,
            chunk_ref=chunk_ref,
            embedding_ref=embedding_ref,
            embedding_model=self._embedding_model,
        )
        self._profiles.get("RETRIEVAL", retrieval_ref)
        profile_changed = any(
            (
                parser_ref != knowledge_base.parser_profile_ref,
                chunk_ref != knowledge_base.chunk_profile_ref,
                embedding_ref != knowledge_base.embedding_profile_ref,
                retrieval_ref != knowledge_base.retrieval_profile_ref,
            )
        )
        await self._rag_engine.update_dataset(
            UpdateEngineDatasetCommand(
                security=security,
                knowledge_base_id=knowledge_base.id,
                name=value.name,
                description=value.description,
                parser_config=(configuration.parser_config if profile_changed else None),
            )
        )
        if value.name is not None:
            knowledge_base.name = value.name
        if value.description is not None:
            knowledge_base.description = value.description
        knowledge_base.parser_profile_ref = parser_ref
        knowledge_base.chunk_profile_ref = chunk_ref
        knowledge_base.embedding_profile_ref = embedding_ref
        knowledge_base.retrieval_profile_ref = retrieval_ref
        if profile_changed:
            knowledge_base.revision += 1
        expected_version = knowledge_base.row_version
        if not await self._knowledge_bases.update(
            security.security_scope_id,
            knowledge_base,
            expected_version,
        ):
            raise RagError(
                "RAG-RF-007",
                "RAGFlow 修改成功但本地映射未完成",
                http_status=409,
                retryable=True,
            )
        return knowledge_base

    @staticmethod
    def _validate_metadata(metadata: dict[str, Any]) -> None:
        """限制文档元数据总大小、键格式和值类型，保持过滤语义和索引安全。"""
        encoded = json.dumps(
            metadata,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        if len(encoded) > 16 * 1024:
            raise RagError(
                "RAG-DOC-003",
                "文档元数据过大",
                http_status=400,
                retryable=False,
            )
        for key, value in metadata.items():
            valid_value = isinstance(value, str | int | float | bool) or (
                isinstance(value, list) and all(isinstance(item, str) for item in value)
            )
            if not _METADATA_KEY.fullmatch(key) or not valid_value:
                raise RagError(
                    "RAG-DOC-003",
                    "文档元数据无效",
                    http_status=400,
                    retryable=False,
                )

    async def upload_document(
        self,
        security: SecurityContext,
        knowledge_base_id: UUID,
        value: UploadDocumentInput,
    ) -> DocumentAccepted:
        """流式暂存文件，原子创建文档记录与上传任务。

        在写磁盘前验证知识库/Profile/元数据/外部 ID；数据库写入失败时删除暂存文件
        作为补偿，避免永远无人引用的大文件占用空间。
        """
        knowledge_base = await self.get_knowledge_base(
            security,
            knowledge_base_id,
        )
        await self._management_access.require_access(security, "document.upload")
        security = security.for_resource_scope(knowledge_base.security_scope_id)
        if knowledge_base.status != KnowledgeBaseStatus.READY:
            raise RagError(
                "RAG-KB-003",
                "知识库当前状态不可上传",
                http_status=409,
                retryable=False,
            )
        extension = Path(value.filename).suffix.lower()
        # 显式 Parser 始终优先且稍后严格校验；自动模式先应用环境的 PDF 默认覆盖，
        # 未配置覆盖时保留兼容的知识库默认值，
        # 不兼容时再按配置文件中的有序规则选择。这样 MinerU PDF 默认值不会被覆盖，
        # Office 默认知识库仍能接收 PDF、JSON、图片和音视频，未知格式则在暂存前
        # 明确失败。
        parser_profile = value.parser_profile or (
            self._profiles.recommended_document_parser_profile(
                extension=extension,
                preferred_ref=knowledge_base.parser_profile_ref,
            )
        )
        # 调用方没有显式指定 Chunk Profile 时，按文件族选择不可变版本：正文、表格、
        # 视觉内容和媒体转写分别使用适合自身边界的长度与重叠比例。选择结果写入
        # DocumentRecord，Worker 会在解析前通过 RAGFlow 官方文档 PATCH 真正应用，
        # 不能只更新本地字段制造“看起来已经自适应”的假象。开发控制台显式传入的
        # Profile 仍具有最高优先级，便于对照实验和历史回滚。
        chunk_profile = value.chunk_profile or self._profiles.recommended_document_chunk_profile(
            extension=extension,
            fallback_ref=knowledge_base.chunk_profile_ref,
        )
        self._profiles.validate_document_profiles(
            parser_ref=parser_profile,
            chunk_ref=chunk_profile,
            extension=extension,
        )
        self._validate_metadata(value.metadata)
        if value.external_id:
            _, duplicate_count = await self._documents.list(
                security.security_scope_id,
                knowledge_base.id,
                page=1,
                page_size=1,
                external_id=value.external_id,
            )
            if duplicate_count:
                raise RagError(
                    "RAG-DOC-009",
                    "文档 external_id 已存在",
                    http_status=409,
                    retryable=False,
                )
        staged = await self._staging.save(
            value.stream,
            StagedFileMetadata(
                original_filename=value.filename,
                media_type=value.mime_type,
            ),
        )
        try:
            # 文件必须先安全暂存才能得到对完整字节流计算的 SHA-256。摘要得到后立即
            # 在“当前共享知识库”边界内检查重复；命中时删除这次临时副本并把
            # 已有文档 ID 放进稳定错误详情。这样前端可直接定位原记录，且不会创建
            # 第二个 Document、Upload Job 或 RAGFlow 索引任务。
            existing_document = await self._documents.find_active_by_content_sha256(
                security.security_scope_id,
                knowledge_base.id,
                staged.content_sha256,
            )
            if existing_document is not None:
                raise RagError(
                    "RAG-DOC-012",
                    (
                        "该文件内容已经上传；"
                        f"existing_document_id={existing_document.id}"
                    ),
                    http_status=409,
                    retryable=False,
                    details=[
                        ErrorDetail(
                            field="existing_document_id",
                            reason=str(existing_document.id),
                        )
                    ],
                )
            document = DocumentRecord(
                security_scope_id=security.security_scope_id,
                knowledge_base_id=knowledge_base.id,
                external_id=value.external_id,
                filename=value.filename,
                safe_filename=staged.safe_filename,
                mime_type=value.mime_type,
                file_size_bytes=staged.size_bytes,
                content_sha256=staged.content_sha256,
                metadata=value.metadata,
                parser_profile_ref=parser_profile,
                chunk_profile_ref=chunk_profile,
            )
            initial_version = DocumentVersion(
                security_scope_id=security.security_scope_id,
                knowledge_base_id=knowledge_base.id,
                document_id=document.id,
                version_number=1,
                filename=value.filename,
                safe_filename=staged.safe_filename,
                mime_type=value.mime_type,
                file_size_bytes=staged.size_bytes,
                content_sha256=staged.content_sha256,
                metadata=value.metadata,
                parser_profile_ref=parser_profile,
                chunk_profile_ref=chunk_profile,
                requested_mode=DocumentUpdateMode.SAFE_REPLACE,
                effective_mode=DocumentUpdateMode.SAFE_REPLACE,
            )
            document.active_version_id = initial_version.id
            job = IngestionJob(
                security_scope_id=security.security_scope_id,
                job_type=JobType.UPLOAD,
                knowledge_base_id=knowledge_base.id,
                document_id=document.id,
                max_attempts=self._job_settings.default_max_attempts,
                payload={
                    "schema_version": 1,
                    "staged_file_id": str(staged.staged_file_id),
                    "safe_filename": staged.safe_filename,
                    "size_bytes": staged.size_bytes,
                    "content_sha256": staged.content_sha256,
                    "auto_parse": value.auto_parse,
                },
            )
            await self._documents.add(security.security_scope_id, document)
            if self._versions is not None:
                await self._versions.add(security.security_scope_id, initial_version)
            await self._jobs.add(security.security_scope_id, job)
        except Exception:
            await self._staging.delete(staged.staged_file_id)
            raise
        return DocumentAccepted(document=document, job=job)

    def _require_version_repository(self) -> DocumentVersionRepository:
        """返回已配置版本仓库；缺失时阻止不安全的原地覆盖回退路径。"""
        if self._versions is None:
            raise RagError(
                "RAG-DOC-015",
                "文档版本更新能力尚未完成数据库迁移",
                http_status=503,
                retryable=True,
            )
        return self._versions

    async def upload_document_version(
        self,
        security: SecurityContext,
        document_id: UUID,
        value: UploadDocumentVersionInput,
    ) -> DocumentVersionAccepted:
        """安全暂存新文件并创建候选版本，旧活动版本继续提供检索。

        方法只负责短事务：验证 Scope、READY 状态、文件类型/Profile、幂等并保存
        ``DocumentVersion`` 与 ``UPDATE_DOCUMENT`` Job。耗时的 RAGFlow 上传、解析、
        侧车索引和原子发布全部由 Worker 完成。任何数据库失败都会删除本次暂存文件，
        不改变主文档内容、Revision 或缓存边界。
        """
        versions = self._require_version_repository()
        document = await self.get_document(security, document_id)
        await self._management_access.require_access(security, "document.version.upload")
        security = security.for_resource_scope(document.security_scope_id)
        if document.status != DocumentStatus.READY:
            raise RagError(
                "RAG-DOC-016",
                "只有可用文档可以上传新版本",
                http_status=409,
                retryable=False,
            )
        if await self._jobs.find_active_for_document(
            security.security_scope_id,
            document_id,
        ):
            raise RagError(
                "RAG-JOB-003",
                "文档已有执行中的 Job",
                http_status=409,
                retryable=False,
            )
        extension = Path(value.filename).suffix.lower()
        # 新版本沿用与首次上传相同的决策顺序，但“兼容默认值”取当前活动文档实际使用
        # 的 Parser；环境显式配置 PDF 默认覆盖时优先采用该覆盖。旧 Parser 不兼容时，
        # 才按配置规则切换，并把结果固化到候选 DocumentVersion。
        parser_profile = value.parser_profile or (
            self._profiles.recommended_document_parser_profile(
                extension=extension,
                preferred_ref=document.parser_profile_ref,
            )
        )
        chunk_profile = value.chunk_profile or self._profiles.recommended_document_chunk_profile(
            extension=extension,
            fallback_ref=document.chunk_profile_ref,
        )
        self._profiles.validate_document_profiles(
            parser_ref=parser_profile,
            chunk_ref=chunk_profile,
            extension=extension,
        )
        self._validate_metadata(value.metadata)
        staged = await self._staging.save(
            value.stream,
            StagedFileMetadata(
                original_filename=value.filename,
                media_type=value.mime_type,
            ),
        )
        try:
            if staged.content_sha256 == document.content_sha256:
                raise RagError(
                    "RAG-DOC-014",
                    "新版本与当前版本内容完全相同，无需更新",
                    http_status=409,
                    retryable=False,
                    details=[
                        ErrorDetail(
                            field="existing_document_id",
                            reason=str(document.id),
                        )
                    ],
                )
            # 只继承用户字段；解析统计、层级索引和实体图等 lazytt_* 字段必须依据
            # 新正文重算。调用方显式 Metadata 最后覆盖，支持修改语言或业务标签。
            inherited_metadata = {
                key: item
                for key, item in document.metadata.items()
                if not key.startswith("lazytt_")
            }
            metadata = {**inherited_metadata, **value.metadata}
            version_number = await versions.next_version_number(
                security.security_scope_id,
                document.id,
            )
            # ``next_version_number`` 会持有文档级 PostgreSQL advisory lock 直到当前
            # 请求事务结束。并发请求可能都在暂存前通过第一次 Job 快速检查，因此拿到
            # 锁后必须重新检查一次；后到请求会在这里看见先到请求已经提交的活动 Job，
            # 从而删除自己的暂存副本并返回 409，而不是偷偷排出 v2、v3 两个并行任务。
            if await self._jobs.find_active_for_document(
                security.security_scope_id,
                document_id,
            ):
                raise RagError(
                    "RAG-JOB-003",
                    "文档已有执行中的 Job",
                    http_status=409,
                    retryable=False,
                )
            version = DocumentVersion(
                security_scope_id=security.security_scope_id,
                knowledge_base_id=document.knowledge_base_id,
                document_id=document.id,
                version_number=version_number,
                filename=value.filename,
                safe_filename=staged.safe_filename,
                mime_type=value.mime_type,
                file_size_bytes=staged.size_bytes,
                content_sha256=staged.content_sha256,
                metadata=metadata,
                parser_profile_ref=parser_profile,
                chunk_profile_ref=chunk_profile,
                requested_mode=value.mode,
                # 当前版本明确执行安全替换；AUTO 只是未来能力协商入口，不能把完整
                # 重建记录成增量复用。
                effective_mode=DocumentUpdateMode.SAFE_REPLACE,
            )
            job = IngestionJob(
                security_scope_id=security.security_scope_id,
                job_type=JobType.UPDATE_DOCUMENT,
                knowledge_base_id=document.knowledge_base_id,
                document_id=document.id,
                max_attempts=self._job_settings.default_max_attempts,
                payload={
                    "schema_version": 1,
                    "operation": "publish",
                    "version_id": str(version.id),
                    "staged_file_id": str(staged.staged_file_id),
                    "safe_filename": staged.safe_filename,
                    "size_bytes": staged.size_bytes,
                    "content_sha256": staged.content_sha256,
                },
            )
            await versions.add(security.security_scope_id, version)
            await self._jobs.add(security.security_scope_id, job)
        except Exception:
            await self._staging.delete(staged.staged_file_id)
            raise
        return DocumentVersionAccepted(version=version, job=job)

    async def list_document_versions(
        self,
        security: SecurityContext,
        document_id: UUID,
        *,
        page: int,
        page_size: int,
    ) -> Page:
        """验证逻辑文档可见后，分页读取同一 Scope 的版本历史。"""
        versions = self._require_version_repository()
        document = await self.get_document(security, document_id)
        await self._management_access.require_access(security, "document.version.list")
        security = security.for_resource_scope(document.security_scope_id)
        items, total = await versions.list(
            security.security_scope_id,
            document_id,
            page=page,
            page_size=page_size,
        )
        return Page(items=items, page=page, page_size=page_size, total=total)

    async def rollback_document_version(
        self,
        security: SecurityContext,
        document_id: UUID,
        version_id: UUID,
    ) -> IngestionJob:
        """创建异步回滚任务；目标物理版本仍存在时才允许发布。

        回滚同样要重读真实 Chunk、重建当前侧车索引并推进知识库 Revision，因此不在
        HTTP 请求内直接改指针。已被后台保留策略清理的物理版本未来可通过持久化源
        文件重新上传；首版尚未提供源文件归档，故此时返回明确冲突。
        """
        versions = self._require_version_repository()
        document = await self.get_document(security, document_id)
        await self._management_access.require_access(security, "document.version.rollback")
        security = security.for_resource_scope(document.security_scope_id)
        if document.status != DocumentStatus.READY:
            raise RagError(
                "RAG-DOC-016",
                "当前文档状态不可回滚",
                http_status=409,
                retryable=False,
            )
        if await self._jobs.find_active_for_document(
            security.security_scope_id,
            document.id,
        ):
            raise RagError(
                "RAG-JOB-003",
                "文档已有执行中的 Job",
                http_status=409,
                retryable=False,
            )
        target = await versions.get(security.security_scope_id, version_id)
        if target is None or target.document_id != document.id:
            raise ResourceNotFoundError("RAG-DOC-017", "文档版本不存在")
        if target.status != DocumentVersionStatus.RETIRED or not target.ragflow_document_id:
            raise RagError(
                "RAG-DOC-018",
                "目标版本当前不可回滚",
                http_status=409,
                retryable=False,
            )
        job = IngestionJob(
            security_scope_id=security.security_scope_id,
            job_type=JobType.UPDATE_DOCUMENT,
            knowledge_base_id=document.knowledge_base_id,
            document_id=document.id,
            max_attempts=self._job_settings.default_max_attempts,
            payload={
                "schema_version": 1,
                "operation": "rollback",
                "version_id": str(target.id),
            },
        )
        await self._jobs.add(security.security_scope_id, job)
        return job

    async def get_document(
        self,
        security: SecurityContext,
        document_id: UUID,
    ) -> DocumentRecord:
        """管理用例读取文档；先检查管理授权，再复用共享文档读取边界。"""
        await self._management_access.require_access(security, "document.read")
        return await self.get_shared_document(security, document_id)

    async def get_shared_document(
        self,
        security: SecurityContext,
        document_id: UUID,
    ) -> DocumentRecord:
        """读取可访问共享知识库中的文档，不泄漏其他租户资源是否存在。

        文档自身和所属知识库必须落在同一个资源 Scope，并通过知识库访问策略。该方法
        只读 PostgreSQL，供普通用户详情和切块接口复用，不要求管理权限。
        """
        candidate_scopes = self._access_policy.candidate_resource_scopes(security)
        document = await self._documents.get_accessible(
            candidate_scopes,
            document_id,
        )
        if document is None:
            raise ResourceNotFoundError("RAG-DOC-001", "文档不存在")
        knowledge_base = await self._knowledge_bases.get_accessible(
            candidate_scopes,
            document.knowledge_base_id,
        )
        if knowledge_base is None:
            raise ResourceNotFoundError("RAG-DOC-001", "文档不存在")
        readable = await self._access_policy.filter_readable(security, (knowledge_base,))
        if not readable or knowledge_base.security_scope_id != document.security_scope_id:
            raise ResourceNotFoundError("RAG-DOC-001", "文档不存在")
        return document

    async def list_documents(
        self,
        security: SecurityContext,
        knowledge_base_id: UUID,
        *,
        page: int,
        page_size: int,
        status: str | None = None,
        filename: str | None = None,
        external_id: str | None = None,
    ) -> Page:
        """管理用例分页列出文档；管理授权与共享知识库可见性分别判断。"""
        await self._management_access.require_access(security, "document.list")
        return await self.list_shared_documents(
            security,
            knowledge_base_id,
            page=page,
            page_size=page_size,
            status=status,
            filename=filename,
            external_id=external_id,
        )

    async def list_shared_documents(
        self,
        security: SecurityContext,
        knowledge_base_id: UUID,
        *,
        page: int,
        page_size: int,
        status: str | None = None,
        filename: str | None = None,
        external_id: str | None = None,
    ) -> Page:
        """确认共享知识库可读后，分页列出其中的文档。

        普通用户只能读取访问策略允许的库；过滤和分页仍由 PostgreSQL 完成。该方法
        不触发上传、解析或其他写操作，未来成员 ACL 会由知识库读取策略统一收窄。
        """
        knowledge_base = await self.get_shared_knowledge_base(security, knowledge_base_id)
        security = security.for_resource_scope(knowledge_base.security_scope_id)
        items, total = await self._documents.list(
            security.security_scope_id,
            knowledge_base_id,
            page=page,
            page_size=page_size,
            status=status,
            filename=filename,
            external_id=external_id,
        )
        return Page(items=items, page=page, page_size=page_size, total=total)

    async def create_parse_job(
        self,
        security: SecurityContext,
        document_id: UUID,
        *,
        reparse: bool = False,
        parser_profile: str | None = None,
        chunk_profile: str | None = None,
    ) -> IngestionJob:
        """校验当前文档状态并创建首次解析或重解析任务。"""
        document = await self.get_document(security, document_id)
        await self._management_access.require_access(security, "document.parse")
        security = security.for_resource_scope(document.security_scope_id)
        if await self._jobs.find_active_for_document(security.security_scope_id, document_id):
            raise RagError(
                "RAG-JOB-003",
                "文档已有执行中的 Job",
                http_status=409,
                retryable=False,
            )
        allowed = {DocumentStatus.READY} if reparse else {DocumentStatus.UPLOADED}
        if document.status not in allowed:
            raise RagError(
                "RAG-DOC-006",
                "当前文档状态不可解析",
                http_status=409,
                retryable=False,
            )
        if reparse and (parser_profile or chunk_profile):
            next_parser = parser_profile or document.parser_profile_ref
            next_chunk = chunk_profile or document.chunk_profile_ref
            self._profiles.validate_document_profiles(
                parser_ref=next_parser,
                chunk_ref=next_chunk,
                extension=Path(document.filename).suffix,
            )
            # 图片包装发生在上传阶段；已有物理文件不能仅靠重解析切换格式。
            # 允许原策略重解析/改分块；跨包装边界请重新上传，避免引擎误走 picture。
            if (
                next_parser != document.parser_profile_ref
                and "mineru-image@1" in {next_parser, document.parser_profile_ref}
            ):
                raise RagError(
                    "RAG-DOC-006", "切换 MinerU 图片策略需要重新上传文件", 409,
                    retryable=False,
                )
            document.parser_profile_ref = next_parser
            document.chunk_profile_ref = next_chunk
        document.status = require_transition(
            document.status,
            DocumentStatus.QUEUED,
            DOCUMENT_TRANSITIONS,
        )
        if not await self._documents.update(
            security.security_scope_id,
            document,
            document.row_version,
        ):
            raise RagError(
                "RAG-DB-002",
                "文档并发更新冲突",
                http_status=409,
                retryable=True,
            )
        job = IngestionJob(
            security_scope_id=security.security_scope_id,
            job_type=JobType.REPARSE if reparse else JobType.PARSE,
            knowledge_base_id=document.knowledge_base_id,
            document_id=document.id,
            max_attempts=self._job_settings.default_max_attempts,
            payload={"schema_version": 1, "delete_existing": reparse},
        )
        await self._jobs.add(security.security_scope_id, job)
        return job

    async def retry_document(
        self,
        security: SecurityContext,
        document_id: UUID,
    ) -> IngestionJob:
        """根据失败文档是否已有引擎映射，选择重试上传或重试解析。"""
        document = await self.get_document(security, document_id)
        await self._management_access.require_access(security, "document.retry")
        security = security.for_resource_scope(document.security_scope_id)
        active_job = await self._jobs.find_active_for_document(
            security.security_scope_id,
            document_id,
        )
        if active_job is not None:
            if active_job.job_type not in {JobType.UPLOAD, JobType.PARSE, JobType.REPARSE}:
                raise RagError(
                    "RAG-JOB-003",
                    "文档已有执行中的 Job",
                    http_status=409,
                    retryable=False,
                )
            if document.status == DocumentStatus.FAILED:
                document.status = require_transition(
                    document.status,
                    DocumentStatus.RETRYING,
                    DOCUMENT_TRANSITIONS,
                )
                if not await self._documents.update(
                    security.security_scope_id,
                    document,
                    document.row_version,
                ):
                    raise RagError(
                        "RAG-DB-002",
                        "文档并发更新冲突",
                        http_status=409,
                        retryable=True,
                    )
            return active_job
        previous = await self._jobs.latest_for_document(security.security_scope_id, document_id)
        if (
            document.status in {DocumentStatus.PARSING, DocumentStatus.INDEXING}
            and previous is not None
            and previous.status == JobStatus.FAILED
        ):
            # 兼容修复前已存在的悬挂记录：旧 Worker 可能已经失败 Parse Job，却没有
            # 把文档从 PARSING/INDEXING 收敛为 FAILED。最近任务明确属于该文档且已经
            # 失败时，先走领域状态机修正一次，随后复用标准 FAILED -> RETRYING 路径；
            # READY、UPLOADED 等其他状态仍会被拒绝，不能借重试接口越过正常流程。
            expected_version = document.row_version
            document.status = require_transition(
                document.status,
                DocumentStatus.FAILED,
                DOCUMENT_TRANSITIONS,
            )
            if not await self._documents.update(
                security.security_scope_id,
                document,
                expected_version,
            ):
                raise RagError(
                    "RAG-DB-002",
                    "文档并发更新冲突",
                    http_status=409,
                    retryable=True,
                )
        if document.status != DocumentStatus.FAILED:
            raise RagError(
                "RAG-DOC-007",
                "只有失败文档可以重试",
                http_status=409,
                retryable=False,
            )
        if previous is None or previous.status != JobStatus.FAILED:
            raise RagError(
                "RAG-JOB-004",
                "缺少可重试的失败 Job",
                http_status=409,
                retryable=False,
            )
        document.status = require_transition(
            document.status,
            DocumentStatus.RETRYING,
            DOCUMENT_TRANSITIONS,
        )
        if not await self._documents.update(
            security.security_scope_id,
            document,
            document.row_version,
        ):
            raise RagError(
                "RAG-DB-002",
                "文档并发更新冲突",
                http_status=409,
                retryable=True,
            )
        job = IngestionJob(
            security_scope_id=security.security_scope_id,
            job_type=previous.job_type,
            knowledge_base_id=document.knowledge_base_id,
            document_id=document.id,
            attempt=previous.attempt,
            max_attempts=max(
                previous.max_attempts,
                previous.attempt + self._job_settings.default_max_attempts,
            ),
            payload=dict(previous.payload),
        )
        await self._jobs.add(security.security_scope_id, job)
        return job

    async def delete_document(
        self,
        security: SecurityContext,
        document_id: UUID,
        *,
        cancel_active: bool = False,
    ) -> IngestionJob:
        """把文档置为删除中并创建删除任务，可按要求先取消活动任务。"""
        document = await self.get_document(security, document_id)
        await self._management_access.require_access(security, "document.delete")
        security = security.for_resource_scope(document.security_scope_id)
        active_job = await self._jobs.find_active_for_document(
            security.security_scope_id,
            document_id,
        )
        if active_job is not None and not cancel_active:
            raise RagError(
                "RAG-JOB-003",
                "文档已有执行中的 Job",
                http_status=409,
                retryable=False,
            )
        if active_job is not None:
            if active_job.job_type == JobType.UPLOAD and document.status in {
                DocumentStatus.RECEIVED,
                DocumentStatus.UPLOADING,
            }:
                raise RagError(
                    "RAG-JOB-003",
                    "文件仍在上传，请稍后再删除",
                    http_status=409,
                    retryable=True,
                )
            active_job.payload["delete_after_cancel"] = True
            if active_job.status in {JobStatus.PENDING, JobStatus.RETRY_WAIT}:
                active_job.status = require_transition(
                    active_job.status,
                    JobStatus.CANCELLED,
                    JOB_TRANSITIONS,
                )
                active_job.finished_at = utc_now()
                active_job.locked_by = None
                active_job.locked_until = None
            elif active_job.status == JobStatus.RUNNING:
                active_job.status = require_transition(
                    active_job.status,
                    JobStatus.CANCEL_REQUESTED,
                    JOB_TRANSITIONS,
                )
            if not await self._jobs.update(security.security_scope_id, active_job):
                raise RagError(
                    "RAG-DB-002",
                    "Job 并发更新冲突",
                    http_status=409,
                    retryable=True,
                )
        document.status = require_transition(
            document.status,
            DocumentStatus.DELETING,
            DOCUMENT_TRANSITIONS,
        )
        if not await self._documents.update(
            security.security_scope_id,
            document,
            document.row_version,
        ):
            raise RagError(
                "RAG-DB-002",
                "文档并发更新冲突",
                http_status=409,
                retryable=True,
            )
        job = IngestionJob(
            security_scope_id=security.security_scope_id,
            job_type=JobType.DELETE_DOCUMENT,
            knowledge_base_id=document.knowledge_base_id,
            document_id=document.id,
            max_attempts=self._job_settings.default_max_attempts,
            payload={"schema_version": 1},
        )
        await self._jobs.add(security.security_scope_id, job)
        return job

    async def delete_knowledge_base(
        self,
        security: SecurityContext,
        knowledge_base_id: UUID,
        *,
        force: bool,
    ) -> IngestionJob:
        """把知识库置为删除中并创建后台删除任务；非空库需显式 ``force``。"""
        knowledge_base = await self.get_knowledge_base(security, knowledge_base_id)
        await self._management_access.require_access(security, "knowledge_base.delete")
        security = security.for_resource_scope(knowledge_base.security_scope_id)
        documents, total = await self._documents.list(
            security.security_scope_id,
            knowledge_base_id,
            page=1,
            page_size=1,
        )
        del documents
        if total and not force:
            raise RagError(
                "RAG-KB-005",
                "知识库非空，删除需要 force=true",
                http_status=409,
                retryable=False,
            )
        knowledge_base.status = require_transition(
            knowledge_base.status,
            KnowledgeBaseStatus.DELETING,
            KNOWLEDGE_BASE_TRANSITIONS,
        )
        if not await self._knowledge_bases.update(
            security.security_scope_id,
            knowledge_base,
            knowledge_base.row_version,
        ):
            raise RagError(
                "RAG-DB-002",
                "知识库并发更新冲突",
                http_status=409,
                retryable=True,
            )
        job = IngestionJob(
            security_scope_id=security.security_scope_id,
            job_type=JobType.DELETE_KB,
            knowledge_base_id=knowledge_base.id,
            max_attempts=self._job_settings.default_max_attempts,
            payload={"schema_version": 1, "force": force},
        )
        await self._jobs.add(security.security_scope_id, job)
        return job

    async def cancel_job(
        self,
        security: SecurityContext,
        job_id: UUID,
    ) -> IngestionJob:
        """请求取消待执行/运行/重试中的任务；终态任务保持幂等返回。"""
        job = await self.get_job(security, job_id)
        await self._management_access.require_access(security, "job.cancel")
        security = security.for_resource_scope(job.security_scope_id)
        if job.status in {
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }:
            raise RagError(
                "RAG-JOB-006",
                "已结束 Job 不可取消",
                http_status=409,
                retryable=False,
            )
        target = (
            JobStatus.CANCELLED
            if job.status in {JobStatus.PENDING, JobStatus.RETRY_WAIT}
            else JobStatus.CANCEL_REQUESTED
        )
        job.status = require_transition(job.status, target, JOB_TRANSITIONS)
        if target == JobStatus.CANCELLED:
            job.finished_at = utc_now()
        if not await self._jobs.update(security.security_scope_id, job):
            raise RagError(
                "RAG-DB-002",
                "Job 并发更新冲突",
                http_status=409,
                retryable=True,
            )
        return job

    async def get_document_source(
        self, security: SecurityContext, document_id: UUID, *, document_revision: int
    ) -> DocumentSource:
        """校验管理入口与共享资源边界后，读取同一修订的原 PDF，不解析、不写数据库。

        跨租户和不存在统一 404；修订变化拒绝预览，避免旧切块落到新文件上。
        原文件只从该资源映射的 RAGFlow 官方下载接口读取，连接由 HTTP 响应关闭。
        """
        await self._management_access.require_access(security, "document.source.read")
        document = await self.get_shared_document(security, document_id)
        return await self._rag_engine.get_document_source(
            GetDocumentSourceQuery(
                security=security.for_resource_scope(document.security_scope_id),
                knowledge_base_id=document.knowledge_base_id,
                document_id=document.id,
                document_revision=document_revision,
            )
        )

    async def list_chunks(
        self,
        security: SecurityContext,
        document_id: UUID,
        *,
        page: int,
        page_size: int,
        document_revision: int | None = None,
    ) -> Page:
        """管理用例分页读取切块；可固定修订号，与原文件预览保持同版本。"""
        await self._management_access.require_access(security, "document.chunk.list")
        return await self.list_shared_chunks(
            security,
            document_id,
            page=page,
            page_size=page_size,
            document_revision=document_revision,
        )

    async def list_shared_chunks(
        self,
        security: SecurityContext,
        document_id: UUID,
        *,
        page: int,
        page_size: int,
        document_revision: int | None = None,
    ) -> Page:
        """分页读取可访问共享文档的原始文本块与位置。

        先通过 PostgreSQL 验证文档及知识库 Scope，再用该资源 Scope 调用 RAGFlow；
        可选 document_revision 固定预览版本，由适配器拒绝过期修订或未就绪文档。
        不允许浏览器凭文档 UUID 越过租户或未来 ACL，也不会修改解析状态。
        """
        document = await self.get_shared_document(security, document_id)
        security = security.for_resource_scope(document.security_scope_id)
        result = await self._rag_engine.list_chunks(
            ListEngineChunksQuery(
                security=security,
                knowledge_base_id=document.knowledge_base_id,
                document_id=document.id,
                page=page,
                page_size=page_size,
                document_revision=document_revision,
            )
        )
        items = tuple(item for item in result.items if isinstance(item, EngineChunk))
        return Page(
            items=items,
            page=result.page,
            page_size=result.page_size,
            total=result.total,
        )

    async def get_job(
        self,
        security: SecurityContext,
        job_id: UUID,
    ) -> IngestionJob:
        """按安全范围读取任务；找不到时返回稳定任务 404。"""
        await self._management_access.require_access(security, "job.read")
        candidate_scopes = self._access_policy.candidate_resource_scopes(security)
        job = None
        for resource_scope_id in candidate_scopes:
            job = await self._jobs.get(resource_scope_id, job_id)
            if job is not None:
                break
        if job is None:
            raise ResourceNotFoundError("RAG-JOB-001", "Job 不存在")
        if job.knowledge_base_id is not None:
            knowledge_base = await self._knowledge_bases.get_accessible(
                candidate_scopes,
                job.knowledge_base_id,
            )
            # 删除任务完成后，普通查询会隐藏软删除知识库；只针对该任务按其原资源范围
            # 核验已删除记录，避免轮询误报任务不存在，也不放宽其他任务的可见性。
            if knowledge_base is None and job.job_type == JobType.DELETE_KB:
                knowledge_base = await self._knowledge_bases.get_deleted(
                    job.security_scope_id,
                    job.knowledge_base_id,
                )
            if knowledge_base is None:
                raise ResourceNotFoundError("RAG-JOB-001", "Job 不存在")
            readable = await self._access_policy.filter_readable(security, (knowledge_base,))
            if not readable or knowledge_base.security_scope_id != job.security_scope_id:
                raise ResourceNotFoundError("RAG-JOB-001", "Job 不存在")
        return job
