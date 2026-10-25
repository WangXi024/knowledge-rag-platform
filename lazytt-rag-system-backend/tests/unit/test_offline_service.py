"""离线应用服务测试：Profile、范围、上传补偿、状态守卫、重试、删除和任务查询。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from rag_service.application.offline import (
    CreateKnowledgeBaseInput,
    OfflineService,
    UpdateKnowledgeBaseInput,
    UploadDocumentInput,
    UploadDocumentVersionInput,
)
from rag_service.application.profiles import ProfileCatalog
from rag_service.domain.errors import RagError, ResourceNotFoundError
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
)
from rag_service.domain.ports import (
    CreateDatasetCommand,
    EngineChunk,
    EngineDataset,
    ListEngineChunksQuery,
    Page,
    StagedFile,
    UpdateEngineDatasetCommand,
)
from rag_service.security.context import SecurityContext, derive_system_resource_scope_id
from rag_service.settings import ServiceSettings


class MemoryKnowledgeBases:
    def __init__(self) -> None:
        self.items: dict[UUID, KnowledgeBase] = {}
        self.fail_update = False

    async def add(self, security_scope_id: str, value: KnowledgeBase) -> None:
        assert value.security_scope_id == security_scope_id
        self.items[value.id] = value

    async def get(self, security_scope_id: str, knowledge_base_id: UUID) -> KnowledgeBase | None:
        item = self.items.get(knowledge_base_id)
        return item if item and item.security_scope_id == security_scope_id else None

    async def get_many(
        self, security_scope_id: str, knowledge_base_ids: Any
    ) -> tuple[KnowledgeBase, ...]:
        return tuple(
            item
            for identifier in knowledge_base_ids
            if (item := await self.get(security_scope_id, identifier)) is not None
        )

    async def get_accessible(
        self,
        security_scope_ids: Any,
        knowledge_base_id: UUID,
    ) -> KnowledgeBase | None:
        item = self.items.get(knowledge_base_id)
        return (
            item
            if item
            and item.security_scope_id in security_scope_ids
            and item.status != KnowledgeBaseStatus.DELETED
            else None
        )

    async def get_deleted(
        self, security_scope_id: str, knowledge_base_id: UUID
    ) -> KnowledgeBase | None:
        """模拟数据库仅按原资源范围返回软删除记录。"""
        item = self.items.get(knowledge_base_id)
        return (
            item
            if item
            and item.security_scope_id == security_scope_id
            and item.status == KnowledgeBaseStatus.DELETED
            else None
        )

    async def get_many_accessible(
        self,
        security_scope_ids: Any,
        knowledge_base_ids: Any,
    ) -> tuple[KnowledgeBase, ...]:
        return tuple(
            item
            for identifier in knowledge_base_ids
            if (item := await self.get_accessible(security_scope_ids, identifier)) is not None
        )

    async def list(
        self,
        security_scope_id: str,
        *,
        page: int,
        page_size: int,
        name: str | None = None,
        status: str | None = None,
        scope_type: str | None = None,
    ) -> tuple[tuple[KnowledgeBase, ...], int]:
        items = tuple(
            item
            for item in self.items.values()
            if item.security_scope_id == security_scope_id
            and (not name or name in item.name)
            and (not status or status == item.status.value)
            and (not scope_type or scope_type == item.scope_type.value)
        )
        return items[(page - 1) * page_size : page * page_size], len(items)

    async def list_accessible(
        self,
        security_scope_ids: Any,
        *,
        page: int,
        page_size: int,
        name: str | None = None,
        status: str | None = None,
        scope_type: str | None = None,
    ) -> tuple[tuple[KnowledgeBase, ...], int]:
        items = tuple(
            item
            for item in self.items.values()
            if item.security_scope_id in security_scope_ids
            and (not name or name in item.name)
            and (not status or status == item.status.value)
            and (not scope_type or scope_type == item.scope_type.value)
        )
        return items[(page - 1) * page_size : page * page_size], len(items)

    async def list_ready_accessible(
        self,
        security_scope_ids: Any,
    ) -> tuple[KnowledgeBase, ...]:
        return tuple(
            item
            for item in self.items.values()
            if item.security_scope_id in security_scope_ids
            and item.status == KnowledgeBaseStatus.READY
        )

    async def update(
        self,
        security_scope_id: str,
        value: KnowledgeBase,
        expected_row_version: int,
    ) -> bool:
        assert value.security_scope_id == security_scope_id
        if self.fail_update or value.row_version != expected_row_version:
            return False
        value.row_version += 1
        return True


class MemoryDocuments:
    def __init__(self) -> None:
        self.items: dict[UUID, DocumentRecord] = {}
        self.fail_add = False
        self.fail_update = False

    async def add(self, security_scope_id: str, value: DocumentRecord) -> None:
        assert value.security_scope_id == security_scope_id
        if self.fail_add:
            raise RuntimeError("synthetic database failure")
        self.items[value.id] = value

    async def get(self, security_scope_id: str, document_id: UUID) -> DocumentRecord | None:
        item = self.items.get(document_id)
        return item if item and item.security_scope_id == security_scope_id else None

    async def get_accessible(
        self,
        security_scope_ids: Any,
        document_id: UUID,
    ) -> DocumentRecord | None:
        item = self.items.get(document_id)
        return item if item and item.security_scope_id in security_scope_ids else None

    async def get_many_accessible(
        self,
        security_scope_ids: Any,
        document_ids: Any,
    ) -> tuple[DocumentRecord, ...]:
        return tuple(
            item
            for identifier in document_ids
            if (item := await self.get_accessible(security_scope_ids, identifier)) is not None
        )

    async def find_active_by_content_sha256(
        self,
        security_scope_id: str,
        knowledge_base_id: UUID,
        content_sha256: str,
    ) -> DocumentRecord | None:
        """模拟生产仓库的精确文件去重边界，供上传用例验证。

        测试仓库同样同时校验租户、知识库、摘要和软删除状态，避免单元测试因为使用
        简化内存实现而漏掉生产环境最重要的隔离条件。
        """
        return next(
            (
                item
                for item in self.items.values()
                if item.security_scope_id == security_scope_id
                and item.knowledge_base_id == knowledge_base_id
                and item.content_sha256 == content_sha256
                and item.status != DocumentStatus.DELETED
            ),
            None,
        )

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
        items = tuple(
            item
            for item in self.items.values()
            if item.security_scope_id == security_scope_id
            and item.knowledge_base_id == knowledge_base_id
            and item.status != DocumentStatus.DELETED
            and (not status or item.status.value == status)
            and (not filename or filename in item.filename)
            and (not external_id or item.external_id == external_id)
        )
        return items[(page - 1) * page_size : page * page_size], len(items)

    async def update(
        self,
        security_scope_id: str,
        value: DocumentRecord,
        expected_row_version: int,
    ) -> bool:
        assert value.security_scope_id == security_scope_id
        if self.fail_update or value.row_version != expected_row_version:
            return False
        value.row_version += 1
        return True


class MemoryJobs:
    def __init__(self) -> None:
        self.items: dict[UUID, IngestionJob] = {}

    async def add(self, security_scope_id: str, value: IngestionJob) -> None:
        assert value.security_scope_id == security_scope_id
        self.items[value.id] = value

    async def get(self, security_scope_id: str, job_id: UUID) -> IngestionJob | None:
        item = self.items.get(job_id)
        return item if item and item.security_scope_id == security_scope_id else None

    async def update(self, security_scope_id: str, value: IngestionJob) -> bool:
        assert value.security_scope_id == security_scope_id
        self.items[value.id] = value
        return True

    async def find_active_for_document(
        self, security_scope_id: str, document_id: UUID
    ) -> IngestionJob | None:
        return next(
            (
                job
                for job in self.items.values()
                if job.security_scope_id == security_scope_id
                and job.document_id == document_id
                and job.status
                in {
                    JobStatus.PENDING,
                    JobStatus.RUNNING,
                    JobStatus.RETRY_WAIT,
                    JobStatus.CANCEL_REQUESTED,
                }
            ),
            None,
        )

    async def latest_for_document(
        self, security_scope_id: str, document_id: UUID
    ) -> IngestionJob | None:
        matches = [
            job
            for job in self.items.values()
            if job.security_scope_id == security_scope_id
            and job.document_id == document_id
        ]
        return max(matches, key=lambda item: item.created_at) if matches else None


class MemoryDocumentVersions:
    """模拟文档版本仓库，覆盖编号、列表和活动版本查询的安全边界。"""

    def __init__(self) -> None:
        """初始化空版本集合，并记录编号锁被调用次数用于并发守卫断言。"""
        self.items: dict[UUID, DocumentVersion] = {}
        self.number_requests = 0

    async def add(self, security_scope_id: str, value: DocumentVersion) -> None:
        """只允许把属于当前 Scope 的版本加入内存集合。"""
        assert value.security_scope_id == security_scope_id
        self.items[value.id] = value

    async def get(
        self, security_scope_id: str, version_id: UUID
    ) -> DocumentVersion | None:
        """按 Scope 返回单一版本，模拟跨租户不可见。"""
        value = self.items.get(version_id)
        return value if value and value.security_scope_id == security_scope_id else None

    async def get_active(
        self, security_scope_id: str, document_id: UUID
    ) -> DocumentVersion | None:
        """返回一个逻辑文档当前活动版本。"""
        return next(
            (
                value
                for value in self.items.values()
                if value.security_scope_id == security_scope_id
                and value.document_id == document_id
                and value.status == DocumentVersionStatus.ACTIVE
            ),
            None,
        )

    async def next_version_number(self, security_scope_id: str, document_id: UUID) -> int:
        """按现有最大编号返回下一值；生产实现会在此持有事务级 advisory lock。"""
        self.number_requests += 1
        numbers = [
            value.version_number
            for value in self.items.values()
            if value.security_scope_id == security_scope_id
            and value.document_id == document_id
        ]
        return max(numbers, default=0) + 1

    async def list(
        self,
        security_scope_id: str,
        document_id: UUID,
        *,
        page: int,
        page_size: int,
    ) -> tuple[tuple[DocumentVersion, ...], int]:
        """按版本号倒序分页，保持与 PostgreSQL 仓库对外语义一致。"""
        values = sorted(
            (
                value
                for value in self.items.values()
                if value.security_scope_id == security_scope_id
                and value.document_id == document_id
            ),
            key=lambda value: value.version_number,
            reverse=True,
        )
        return tuple(values[(page - 1) * page_size : page * page_size]), len(values)

    async def update(self, security_scope_id: str, value: DocumentVersion) -> bool:
        """更新版本快照；单元测试不模拟数据库并发冲突。"""
        assert value.security_scope_id == security_scope_id
        self.items[value.id] = value
        return True

    async def activate(
        self,
        security_scope_id: str,
        *,
        document_id: UUID,
        version_id: UUID,
        activated_at: datetime,
    ) -> bool:
        """退役旧活动版本并激活目标，供后续 Worker 测试替身复用。"""
        target = await self.get(security_scope_id, version_id)
        if target is None or target.document_id != document_id:
            return False
        for value in self.items.values():
            if (
                value.security_scope_id == security_scope_id
                and value.document_id == document_id
                and value.status == DocumentVersionStatus.ACTIVE
            ):
                value.status = DocumentVersionStatus.RETIRED
                value.retired_at = activated_at
        target.status = DocumentVersionStatus.ACTIVE
        target.activated_at = activated_at
        target.retired_at = None
        return True


class MemoryStaging:
    def __init__(self) -> None:
        self.saved = 0
        self.deleted: list[UUID] = []

    async def save(self, stream: AsyncIterator[bytes], _metadata: Any) -> StagedFile:
        content = b"".join([chunk async for chunk in stream])
        self.saved += 1
        return StagedFile(
            staged_file_id=uuid4(),
            safe_filename="fixture.txt",
            size_bytes=len(content),
            content_sha256="a" * 64,
            ready_path=Path("/not-persisted-in-job/fixture.ready"),
        )

    async def delete(self, staged_file_id: UUID) -> None:
        self.deleted.append(staged_file_id)


class FakeEngine:
    def __init__(self) -> None:
        self.create_commands: list[CreateDatasetCommand] = []
        self.update_commands: list[UpdateEngineDatasetCommand] = []
        self.chunk_queries: list[ListEngineChunksQuery] = []
        self.fail_create = False

    async def create_dataset(self, command: CreateDatasetCommand) -> EngineDataset:
        self.create_commands.append(command)
        if self.fail_create:
            raise RuntimeError("synthetic RAGFlow failure")
        return EngineDataset(
            engine_id="engine-dataset-1",
            name=command.name,
            chunk_method="naive",
        )

    async def update_dataset(self, command: UpdateEngineDatasetCommand) -> EngineDataset:
        self.update_commands.append(command)
        return EngineDataset(
            engine_id="engine-dataset-1",
            name=command.name or "Synthetic KB",
            chunk_method="naive",
        )

    async def list_chunks(self, query: ListEngineChunksQuery) -> Page:
        self.chunk_queries.append(query)
        return Page(
            items=(
                EngineChunk(
                    engine_chunk_id="chunk-1",
                    document_id=query.document_id,
                    content="synthetic",
                ),
            ),
            page=query.page,
            page_size=query.page_size,
            total=1,
        )


def _security(tenant: str = "tenant") -> SecurityContext:
    return SecurityContext.create(
        subject_id="subject",
        tenant_id=tenant,
        rag_scope_id="scope",
        token_id=f"token-{tenant}",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )


async def _stream() -> AsyncIterator[bytes]:
    yield b"fixture"


def _service(
    settings: ServiceSettings,
) -> tuple[
    OfflineService,
    MemoryKnowledgeBases,
    MemoryDocuments,
    MemoryJobs,
    MemoryStaging,
    FakeEngine,
]:
    knowledge_bases = MemoryKnowledgeBases()
    documents = MemoryDocuments()
    jobs = MemoryJobs()
    staging = MemoryStaging()
    engine = FakeEngine()
    service = OfflineService(
        knowledge_bases,
        documents,
        jobs,
        staging,
        engine,
        ProfileCatalog.load(settings.profiles),
        settings.jobs,
        settings.cache.semantic.embedding_model,
    )
    return service, knowledge_bases, documents, jobs, staging, engine


@pytest.mark.asyncio
async def test_create_knowledge_base_resolves_profiles_and_enforces_scope(
    service_settings: ServiceSettings,
) -> None:
    service, _, _, _, _, engine = _service(service_settings)
    security = _security()

    knowledge_base = await service.create_knowledge_base(
        security,
        CreateKnowledgeBaseInput(
            name="Synthetic KB",
            parser_profile="deepdoc-default@1",
            chunk_profile="general-256@2",
            embedding_profile="embedding-openai-compatible@3",
            retrieval_profile="hybrid-default@1",
        ),
    )

    assert knowledge_base.ragflow_dataset_id == "engine-dataset-1"
    assert knowledge_base.status.value == "READY"
    assert knowledge_base.security_scope_id == security.tenant_resource_scope_id
    assert engine.create_commands[0].security.principal_scope_id == security.security_scope_id
    command = engine.create_commands[0]
    # 这一断言覆盖从产品默认 Profile 到外部引擎命令的完整应用层链路，保证长度和
    # 重叠百分比会一起传给 RAGFlow，而不是只在 YAML 中看起来已经配置。
    assert command.parser_config["chunk_token_num"] == 256
    assert command.parser_config["overlapped_percent"] == 10
    assert command.parser_config["layout_recognize"] == "DeepDOC"
    with pytest.raises(ResourceNotFoundError):
        await service.get_knowledge_base(_security("other"), knowledge_base.id)


@pytest.mark.asyncio
async def test_create_system_knowledge_base_uses_one_global_resource_scope(
    service_settings: ServiceSettings,
) -> None:
    service, _, _, _, _, engine = _service(service_settings)
    security = _security()

    knowledge_base = await service.create_knowledge_base(
        security,
        CreateKnowledgeBaseInput(
            name="System KB",
            parser_profile="office-default@1",
            chunk_profile="general-512@1",
            embedding_profile="embedding-openai-compatible@3",
            retrieval_profile="hybrid-default@1",
            scope_type=KnowledgeBaseScopeType.SYSTEM,
        ),
    )

    assert knowledge_base.scope_type == KnowledgeBaseScopeType.SYSTEM
    assert knowledge_base.security_scope_id == derive_system_resource_scope_id()
    assert engine.create_commands[0].security.security_scope_id == derive_system_resource_scope_id()
    assert (await service.get_knowledge_base(_security("other"), knowledge_base.id)).id == (
        knowledge_base.id
    )


@pytest.mark.asyncio
async def test_shared_list_can_filter_tenant_and_system_scopes_for_pagination(
    service_settings: ServiceSettings,
) -> None:
    """普通用户分页筛选归属时，总数和当前页都只能包含所选 scope_type。"""
    service, _, _, _, _, _ = _service(service_settings)
    security = _security()
    common = {
        "parser_profile": "office-default@1",
        "chunk_profile": "general-512@1",
        "embedding_profile": "embedding-openai-compatible@3",
        "retrieval_profile": "hybrid-default@1",
    }
    await service.create_knowledge_base(
        security,
        CreateKnowledgeBaseInput(name="Tenant KB", **common),
    )
    await service.create_knowledge_base(
        security,
        CreateKnowledgeBaseInput(
            name="System KB",
            scope_type=KnowledgeBaseScopeType.SYSTEM,
            **common,
        ),
    )

    tenant_page = await service.list_shared_knowledge_bases(
        security,
        page=1,
        page_size=20,
        scope_type="TENANT",
    )
    system_page = await service.list_shared_knowledge_bases(
        security,
        page=1,
        page_size=20,
        scope_type="SYSTEM",
    )

    assert tenant_page.total == 1
    assert tenant_page.items[0].scope_type == KnowledgeBaseScopeType.TENANT
    assert system_page.total == 1
    assert system_page.items[0].scope_type == KnowledgeBaseScopeType.SYSTEM


@pytest.mark.asyncio
async def test_upload_creates_document_and_minimal_job_payload(
    service_settings: ServiceSettings,
) -> None:
    service, _, _, jobs, staging, _ = _service(service_settings)
    security = _security()
    knowledge_base = await service.create_knowledge_base(
        security,
        CreateKnowledgeBaseInput(
            name="Synthetic KB",
            parser_profile="office-default@1",
            chunk_profile="general-512@1",
            embedding_profile="embedding-openai-compatible@3",
            retrieval_profile="hybrid-default@1",
        ),
    )

    accepted = await service.upload_document(
        security,
        knowledge_base.id,
        UploadDocumentInput(
            filename="fixture.txt",
            mime_type="text/plain",
            stream=_stream(),
            metadata={"product": "synthetic"},
        ),
    )

    assert staging.saved == 1
    assert accepted.document.status == DocumentStatus.RECEIVED
    assert accepted.document.chunk_profile_ref == "prose-384@1"
    assert jobs.items[accepted.job.id] is accepted.job
    assert set(accepted.job.payload) == {
        "schema_version",
        "staged_file_id",
        "safe_filename",
        "size_bytes",
        "content_sha256",
        "auto_parse",
    }
    assert not any("/" in str(value) for value in accepted.job.payload.values())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "mime_type", "expected_parser", "expected_chunk"),
    (
        ("fixture.pdf", "application/pdf", "deepdoc-default@1", "prose-384@1"),
        (
            "fixture.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "office-default@1",
            "table-192@1",
        ),
        (
            "fixture.pptx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            "office-default@1",
            "visual-192@1",
        ),
        ("fixture.json", "application/json", "json-default@1", "prose-384@1"),
        ("fixture.png", "image/png", "picture-default@1", "visual-192@1"),
        ("fixture.mp3", "audio/mpeg", "media-default@1", "media-320@1"),
    ),
)
async def test_upload_without_profiles_routes_each_supported_file_family(
    service_settings: ServiceSettings,
    filename: str,
    mime_type: str,
    expected_parser: str,
    expected_chunk: str,
) -> None:
    """同一个 Office 默认知识库应接收多种文件，并保存后端选出的有效 Profile。

    请求不传 Parser/Chunk，模拟 Apifox 和管理端的自动模式。断言保存在文档记录中的
    是最终有效版本，保证后续 Worker、重试和重解析不会再次路由产生漂移。
    """
    service, _, _, _, _, _ = _service(service_settings)
    security = _security()
    knowledge_base = await service.create_knowledge_base(
        security,
        CreateKnowledgeBaseInput(
            name="Mixed file KB",
            parser_profile="office-default@1",
            chunk_profile="general-256@2",
            embedding_profile="embedding-openai-compatible@3",
            retrieval_profile="hybrid-default@1",
        ),
    )

    accepted = await service.upload_document(
        security,
        knowledge_base.id,
        UploadDocumentInput(
            filename=filename,
            mime_type=mime_type,
            stream=_stream(),
        ),
    )

    assert accepted.document.parser_profile_ref == expected_parser
    assert accepted.document.chunk_profile_ref == expected_chunk


@pytest.mark.asyncio
async def test_upload_keeps_explicit_incompatible_parser_error(
    service_settings: ServiceSettings,
) -> None:
    """管理员显式给 PDF 选择 Office 时必须在暂存前失败，不能静默自动修正。"""
    service, _, _, _, staging, _ = _service(service_settings)
    security = _security()
    knowledge_base = await service.create_knowledge_base(
        security,
        CreateKnowledgeBaseInput(
            name="Explicit parser KB",
            parser_profile="office-default@1",
            chunk_profile="general-256@2",
            embedding_profile="embedding-openai-compatible@3",
            retrieval_profile="hybrid-default@1",
        ),
    )

    with pytest.raises(RagError) as incompatible:
        await service.upload_document(
            security,
            knowledge_base.id,
            UploadDocumentInput(
                filename="fixture.pdf",
                mime_type="application/pdf",
                stream=_stream(),
                parser_profile="office-default@1",
            ),
        )

    assert incompatible.value.code == "RAG-KB-004"
    assert staging.saved == 0


@pytest.mark.asyncio
async def test_upload_rejects_exact_duplicate_and_cleans_new_staged_copy(
    service_settings: ServiceSettings,
) -> None:
    """同一用户同一知识库的相同字节只能创建一条文档和一个上传任务。"""
    service, _, documents, jobs, staging, _ = _service(service_settings)
    security = _security()
    knowledge_base = await service.create_knowledge_base(
        security,
        CreateKnowledgeBaseInput(
            name="Synthetic KB",
            parser_profile="office-default@1",
            chunk_profile="general-512@1",
            embedding_profile="embedding-openai-compatible@3",
            retrieval_profile="hybrid-default@1",
        ),
    )
    accepted = await service.upload_document(
        security,
        knowledge_base.id,
        UploadDocumentInput(
            filename="original.txt",
            mime_type="text/plain",
            stream=_stream(),
        ),
    )

    with pytest.raises(RagError) as conflict:
        await service.upload_document(
            security,
            knowledge_base.id,
            UploadDocumentInput(
                # 文件名不同也不能绕过内容去重，因为判断依据是完整文件 SHA-256。
                filename="renamed-copy.txt",
                mime_type="text/plain",
                stream=_stream(),
            ),
        )

    assert conflict.value.code == "RAG-DOC-012"
    assert str(accepted.document.id) in conflict.value.message
    assert conflict.value.details[0].field == "existing_document_id"
    assert conflict.value.details[0].reason == str(accepted.document.id)
    assert len(documents.items) == 1
    assert len(jobs.items) == 1
    assert staging.saved == 2
    assert len(staging.deleted) == 1


@pytest.mark.asyncio
async def test_document_version_upload_keeps_active_document_and_creates_safe_candidate(
    service_settings: ServiceSettings,
) -> None:
    """新版本 HTTP 前置流程不得提前改写当前 READY 文档或伪报增量执行。"""
    security = _security()
    knowledge_bases = MemoryKnowledgeBases()
    documents = MemoryDocuments()
    jobs = MemoryJobs()
    staging = MemoryStaging()
    engine = FakeEngine()
    versions = MemoryDocumentVersions()
    knowledge_base = KnowledgeBase(
        security_scope_id=security.tenant_resource_scope_id,
        name="Versioned KB",
        parser_profile_ref="office-default@1",
        chunk_profile_ref="general-256@2",
        embedding_profile_ref="embedding-openai-compatible@3",
        retrieval_profile_ref="hybrid-default@1",
        status=KnowledgeBaseStatus.READY,
        ragflow_dataset_id="engine-dataset-1",
    )
    document = DocumentRecord(
        security_scope_id=security.tenant_resource_scope_id,
        knowledge_base_id=knowledge_base.id,
        filename="policy-v1.txt",
        safe_filename="policy-v1.txt",
        mime_type="text/plain",
        file_size_bytes=3,
        content_sha256="b" * 64,
        parser_profile_ref="office-default@1",
        chunk_profile_ref="prose-384@1",
        status=DocumentStatus.READY,
        ragflow_document_id="engine-v1",
    )
    initial = DocumentVersion(
        security_scope_id=security.tenant_resource_scope_id,
        knowledge_base_id=knowledge_base.id,
        document_id=document.id,
        version_number=1,
        filename=document.filename,
        safe_filename=document.safe_filename,
        mime_type=document.mime_type,
        file_size_bytes=document.file_size_bytes,
        content_sha256=document.content_sha256,
        parser_profile_ref=document.parser_profile_ref,
        chunk_profile_ref=document.chunk_profile_ref,
        ragflow_document_id="engine-v1",
        status=DocumentVersionStatus.ACTIVE,
        requested_mode=DocumentUpdateMode.SAFE_REPLACE,
    )
    document.active_version_id = initial.id
    knowledge_bases.items[knowledge_base.id] = knowledge_base
    documents.items[document.id] = document
    versions.items[initial.id] = initial
    service = OfflineService(
        knowledge_bases,
        documents,
        jobs,
        staging,
        engine,
        ProfileCatalog.load(service_settings.profiles),
        service_settings.jobs,
        service_settings.cache.semantic.embedding_model,
        versions=versions,
    )

    accepted = await service.upload_document_version(
        security,
        document.id,
        UploadDocumentVersionInput(
            # 新版本从文本变为 PDF，旧 Office Parser 不兼容；未显式选择时应自动
            # 路由到配置中的 PDF Parser，同时保持旧活动版本不变。
            filename="policy-v2.pdf",
            mime_type="application/pdf",
            stream=_stream(),
            mode=DocumentUpdateMode.AUTO,
        ),
    )

    assert document.filename == "policy-v1.txt"
    assert document.ragflow_document_id == "engine-v1"
    assert document.active_version_id == initial.id
    assert accepted.version.version_number == 2
    assert accepted.version.status == DocumentVersionStatus.PENDING
    assert accepted.version.parser_profile_ref == "deepdoc-default@1"
    assert accepted.version.chunk_profile_ref == "prose-384@1"
    assert accepted.version.requested_mode == DocumentUpdateMode.AUTO
    assert accepted.version.effective_mode == DocumentUpdateMode.SAFE_REPLACE
    assert accepted.job.job_type == JobType.UPDATE_DOCUMENT
    assert versions.number_requests == 1
    assert staging.deleted == []


@pytest.mark.asyncio
async def test_invalid_metadata_is_rejected_before_staging(
    service_settings: ServiceSettings,
) -> None:
    service, _, _, _, staging, _ = _service(service_settings)
    security = _security()
    knowledge_base = await service.create_knowledge_base(
        security,
        CreateKnowledgeBaseInput(
            name="Synthetic KB",
            parser_profile="office-default@1",
            chunk_profile="general-512@1",
            embedding_profile="embedding-openai-compatible@3",
            retrieval_profile="hybrid-default@1",
        ),
    )

    with pytest.raises(RagError):
        await service.upload_document(
            security,
            knowledge_base.id,
            UploadDocumentInput(
                filename="fixture.txt",
                mime_type="text/plain",
                stream=_stream(),
                metadata={"Invalid-Key": "value"},
            ),
        )
    assert staging.saved == 0


@pytest.mark.asyncio
async def test_upload_external_id_conflicts_only_with_active_documents(
    service_settings: ServiceSettings,
) -> None:
    service, _, documents, _, staging, _ = _service(service_settings)
    security = _security()
    knowledge_base = await service.create_knowledge_base(
        security,
        CreateKnowledgeBaseInput(
            name="Synthetic KB",
            parser_profile="office-default@1",
            chunk_profile="general-512@1",
            embedding_profile="embedding-openai-compatible@3",
            retrieval_profile="hybrid-default@1",
        ),
    )
    value = UploadDocumentInput(
        filename="fixture.txt",
        mime_type="text/plain",
        stream=_stream(),
        external_id="reusable-fixture",
    )
    accepted = await service.upload_document(security, knowledge_base.id, value)

    with pytest.raises(RagError) as conflict:
        await service.upload_document(
            security,
            knowledge_base.id,
            UploadDocumentInput(
                filename="fixture.txt",
                mime_type="text/plain",
                stream=_stream(),
                external_id="reusable-fixture",
            ),
        )
    assert conflict.value.code == "RAG-DOC-009"
    assert conflict.value.http_status == 409
    assert staging.saved == 1

    accepted.document.status = DocumentStatus.DELETED
    accepted.document.deleted_at = datetime.now(UTC)
    replacement = await service.upload_document(
        security,
        knowledge_base.id,
        UploadDocumentInput(
            filename="fixture.txt",
            mime_type="text/plain",
            stream=_stream(),
            external_id="reusable-fixture",
        ),
    )
    assert replacement.document.id != accepted.document.id
    assert documents.items[replacement.document.id].external_id == "reusable-fixture"
    assert staging.saved == 2


@pytest.mark.asyncio
async def test_parse_job_and_chunk_listing_keep_local_ids(
    service_settings: ServiceSettings,
) -> None:
    service, knowledge_bases, documents, _, _, engine = _service(service_settings)
    security = _security()
    knowledge_base = KnowledgeBase(
        security_scope_id=security.tenant_resource_scope_id,
        name="Synthetic KB",
        parser_profile_ref="office-default@1",
        chunk_profile_ref="general-512@1",
        embedding_profile_ref="embedding-openai-compatible@3",
        retrieval_profile_ref="hybrid-default@1",
        ragflow_dataset_id="engine-dataset-1",
        status=KnowledgeBaseStatus.READY,
    )
    knowledge_bases.items[knowledge_base.id] = knowledge_base
    document = DocumentRecord(
        security_scope_id=security.tenant_resource_scope_id,
        knowledge_base_id=knowledge_base.id,
        filename="fixture.txt",
        safe_filename="fixture.txt",
        mime_type="text/plain",
        file_size_bytes=7,
        content_sha256="a" * 64,
        parser_profile_ref="office-default@1",
        chunk_profile_ref="general-512@1",
        ragflow_document_id="engine-document-1",
        status=DocumentStatus.UPLOADED,
    )
    documents.items[document.id] = document

    job = await service.create_parse_job(security, document.id)
    chunks = await service.list_chunks(
        security,
        document.id,
        page=1,
        page_size=20,
    )

    assert job.job_type.value == "PARSE"
    assert document.status == DocumentStatus.QUEUED
    assert chunks.items[0].document_id == document.id
    assert engine.chunk_queries[0].security.security_scope_id == security.tenant_resource_scope_id


@pytest.mark.asyncio
async def test_create_failure_and_mapping_conflict_are_explicit(
    service_settings: ServiceSettings,
) -> None:
    service, knowledge_bases, _, _, _, engine = _service(service_settings)
    security = _security()
    value = CreateKnowledgeBaseInput(
        name="Synthetic KB",
        parser_profile="office-default@1",
        chunk_profile="general-512@1",
        embedding_profile="embedding-openai-compatible@3",
        retrieval_profile="hybrid-default@1",
    )
    engine.fail_create = True

    with pytest.raises(RuntimeError, match="synthetic RAGFlow failure"):
        await service.create_knowledge_base(security, value)

    failed = next(iter(knowledge_bases.items.values()))
    assert failed.status == KnowledgeBaseStatus.CREATE_FAILED

    service, knowledge_bases, _, _, _, _ = _service(service_settings)
    knowledge_bases.fail_update = True
    with pytest.raises(RagError, match="本地映射未完成") as conflict:
        await service.create_knowledge_base(security, value)
    assert conflict.value.code == "RAG-RF-007"


@pytest.mark.asyncio
async def test_update_and_list_knowledge_base_are_profile_versioned(
    service_settings: ServiceSettings,
) -> None:
    service, _, _, _, _, engine = _service(service_settings)
    security = _security()
    knowledge_base = await service.create_knowledge_base(
        security,
        CreateKnowledgeBaseInput(
            name="Synthetic KB",
            parser_profile="office-default@1",
            chunk_profile="general-512@1",
            embedding_profile="embedding-openai-compatible@3",
            retrieval_profile="hybrid-default@1",
        ),
    )

    updated = await service.update_knowledge_base(
        security,
        knowledge_base.id,
        UpdateKnowledgeBaseInput(
            name="Renamed Synthetic KB",
            parser_profile="deepdoc-default@1",
        ),
    )
    page = await service.list_knowledge_bases(
        security,
        page=1,
        page_size=20,
        name="Renamed",
        status="READY",
    )

    assert updated.name == "Renamed Synthetic KB"
    assert updated.parser_profile_ref == "deepdoc-default@1"
    assert updated.revision == 1
    assert page.total == 1
    assert engine.update_commands[0].parser_config is not None

    updated.status = KnowledgeBaseStatus.DELETING
    with pytest.raises(RagError) as blocked:
        await service.update_knowledge_base(
            security,
            updated.id,
            UpdateKnowledgeBaseInput(description="blocked"),
        )
    assert blocked.value.code == "RAG-KB-003"


@pytest.mark.asyncio
async def test_upload_database_failure_cleans_staged_file(
    service_settings: ServiceSettings,
) -> None:
    service, _, documents, _, staging, _ = _service(service_settings)
    security = _security()
    knowledge_base = await service.create_knowledge_base(
        security,
        CreateKnowledgeBaseInput(
            name="Synthetic KB",
            parser_profile="office-default@1",
            chunk_profile="general-512@1",
            embedding_profile="embedding-openai-compatible@3",
            retrieval_profile="hybrid-default@1",
        ),
    )
    documents.fail_add = True

    with pytest.raises(RuntimeError, match="synthetic database failure"):
        await service.upload_document(
            security,
            knowledge_base.id,
            UploadDocumentInput(
                filename="fixture.txt",
                mime_type="text/plain",
                stream=_stream(),
            ),
        )

    assert len(staging.deleted) == 1


@pytest.mark.asyncio
async def test_document_parse_retry_delete_and_cancel_guards(
    service_settings: ServiceSettings,
) -> None:
    service, knowledge_bases, documents, jobs, _, _ = _service(service_settings)
    security = _security()
    knowledge_base = KnowledgeBase(
        security_scope_id=security.tenant_resource_scope_id,
        name="Synthetic KB",
        parser_profile_ref="office-default@1",
        chunk_profile_ref="general-512@1",
        embedding_profile_ref="embedding-openai-compatible@3",
        retrieval_profile_ref="hybrid-default@1",
        ragflow_dataset_id="engine-dataset-1",
        status=KnowledgeBaseStatus.READY,
    )
    knowledge_bases.items[knowledge_base.id] = knowledge_base
    document = DocumentRecord(
        security_scope_id=security.tenant_resource_scope_id,
        knowledge_base_id=knowledge_base.id,
        filename="fixture.txt",
        safe_filename="fixture.txt",
        mime_type="text/plain",
        file_size_bytes=7,
        content_sha256="a" * 64,
        parser_profile_ref="office-default@1",
        chunk_profile_ref="general-512@1",
        ragflow_document_id="engine-document-1",
        status=DocumentStatus.READY,
    )
    documents.items[document.id] = document

    with pytest.raises(RagError) as parse_wrong_state:
        await service.create_parse_job(security, document.id)
    assert parse_wrong_state.value.code == "RAG-DOC-006"

    reparse = await service.create_parse_job(
        security,
        document.id,
        reparse=True,
        parser_profile="office-default@1",
    )
    assert reparse.job_type == JobType.REPARSE
    assert document.status == DocumentStatus.QUEUED

    with pytest.raises(RagError) as active_job:
        await service.create_parse_job(security, document.id, reparse=True)
    assert active_job.value.code == "RAG-JOB-003"

    reparse.status = JobStatus.FAILED
    # 模拟旧 Worker 留下的悬挂记录：任务已经失败，但文档仍停在 PARSING。服务应先
    # 根据最近失败任务安全收敛状态，再创建可执行的重试任务。
    document.status = DocumentStatus.PARSING
    reparse.attempt = 2
    reparse.max_attempts = 2
    retry = await service.retry_document(security, document.id)
    assert retry.job_type == JobType.REPARSE
    assert retry.attempt == 2
    assert retry.max_attempts == 4
    assert document.status == DocumentStatus.RETRYING

    retry.status = JobStatus.RETRY_WAIT
    document.status = DocumentStatus.FAILED
    active_retry = await service.retry_document(security, document.id)
    assert active_retry is retry
    assert document.status == DocumentStatus.RETRYING

    retry.status = JobStatus.FAILED
    document.status = DocumentStatus.READY
    delete_job = await service.delete_document(security, document.id)
    assert delete_job.job_type == JobType.DELETE_DOCUMENT
    assert document.status == DocumentStatus.DELETING

    assert (await service.cancel_job(security, delete_job.id)).status == JobStatus.CANCELLED
    with pytest.raises(RagError) as terminal:
        await service.cancel_job(security, delete_job.id)
    assert terminal.value.code == "RAG-JOB-006"


@pytest.mark.asyncio
async def test_delete_document_can_cancel_active_parse_and_queue_delete(
    service_settings: ServiceSettings,
) -> None:
    service, knowledge_bases, documents, jobs, _, _ = _service(service_settings)
    security = _security()
    knowledge_base = KnowledgeBase(
        security_scope_id=security.tenant_resource_scope_id,
        name="Synthetic KB",
        parser_profile_ref="office-default@1",
        chunk_profile_ref="general-512@1",
        embedding_profile_ref="embedding-openai-compatible@3",
        retrieval_profile_ref="hybrid-default@1",
        ragflow_dataset_id="engine-dataset-1",
        status=KnowledgeBaseStatus.READY,
    )
    knowledge_bases.items[knowledge_base.id] = knowledge_base
    document = DocumentRecord(
        security_scope_id=security.tenant_resource_scope_id,
        knowledge_base_id=knowledge_base.id,
        filename="fixture.txt",
        safe_filename="fixture.txt",
        mime_type="text/plain",
        file_size_bytes=7,
        content_sha256="a" * 64,
        parser_profile_ref="office-default@1",
        chunk_profile_ref="general-512@1",
        ragflow_document_id="engine-document-1",
        status=DocumentStatus.PARSING,
    )
    documents.items[document.id] = document
    parse_job = IngestionJob(
        security_scope_id=security.tenant_resource_scope_id,
        job_type=JobType.PARSE,
        status=JobStatus.RUNNING,
        knowledge_base_id=knowledge_base.id,
        document_id=document.id,
    )
    jobs.items[parse_job.id] = parse_job

    delete_job = await service.delete_document(
        security,
        document.id,
        cancel_active=True,
    )

    assert parse_job.status == JobStatus.CANCEL_REQUESTED
    assert parse_job.payload["delete_after_cancel"] is True
    assert document.status == DocumentStatus.DELETING
    assert delete_job.job_type == JobType.DELETE_DOCUMENT
    assert delete_job.status == JobStatus.PENDING


@pytest.mark.asyncio
async def test_knowledge_base_delete_requires_force_and_job_lookups_are_scoped(
    service_settings: ServiceSettings,
) -> None:
    service, knowledge_bases, documents, _, _, _ = _service(service_settings)
    security = _security()
    knowledge_base = KnowledgeBase(
        security_scope_id=security.tenant_resource_scope_id,
        name="Synthetic KB",
        parser_profile_ref="office-default@1",
        chunk_profile_ref="general-512@1",
        embedding_profile_ref="embedding-openai-compatible@3",
        retrieval_profile_ref="hybrid-default@1",
        ragflow_dataset_id="engine-dataset-1",
        status=KnowledgeBaseStatus.READY,
    )
    knowledge_bases.items[knowledge_base.id] = knowledge_base
    document = DocumentRecord(
        security_scope_id=security.tenant_resource_scope_id,
        knowledge_base_id=knowledge_base.id,
        filename="fixture.txt",
        safe_filename="fixture.txt",
        mime_type="text/plain",
        file_size_bytes=7,
        content_sha256="a" * 64,
        parser_profile_ref="office-default@1",
        chunk_profile_ref="general-512@1",
    )
    documents.items[document.id] = document

    listed = await service.list_documents(
        security,
        knowledge_base.id,
        page=1,
        page_size=20,
        filename="fixture",
    )
    assert listed.total == 1

    with pytest.raises(RagError) as force_required:
        await service.delete_knowledge_base(security, knowledge_base.id, force=False)
    assert force_required.value.code == "RAG-KB-005"

    deletion = await service.delete_knowledge_base(security, knowledge_base.id, force=True)
    assert deletion.job_type == JobType.DELETE_KB
    assert knowledge_base.status == KnowledgeBaseStatus.DELETING
    with pytest.raises(ResourceNotFoundError):
        await service.get_job(_security("other"), deletion.id)


@pytest.mark.asyncio
async def test_deleted_knowledge_base_job_remains_readable_only_in_its_scope(
    service_settings: ServiceSettings,
) -> None:
    """软删除后仍可轮询删除任务，但不能借此读取其他任务或其他租户。"""
    service, knowledge_bases, _, jobs, _, _ = _service(service_settings)
    owner = _security()
    knowledge_base = KnowledgeBase(
        security_scope_id=owner.tenant_resource_scope_id,
        name="Deleted fixture",
        parser_profile_ref="office-default@1",
        chunk_profile_ref="general-512@1",
        embedding_profile_ref="embedding-openai-compatible@3",
        retrieval_profile_ref="hybrid-default@1",
        status=KnowledgeBaseStatus.DELETED,
    )
    knowledge_bases.items[knowledge_base.id] = knowledge_base
    delete_job = IngestionJob(
        security_scope_id=knowledge_base.security_scope_id,
        job_type=JobType.DELETE_KB,
        knowledge_base_id=knowledge_base.id,
        status=JobStatus.SUCCEEDED,
    )
    other_job = IngestionJob(
        security_scope_id=knowledge_base.security_scope_id,
        job_type=JobType.PARSE,
        knowledge_base_id=knowledge_base.id,
        status=JobStatus.SUCCEEDED,
    )
    jobs.items[delete_job.id] = delete_job
    jobs.items[other_job.id] = other_job
    missing_job = IngestionJob(
        security_scope_id=knowledge_base.security_scope_id,
        job_type=JobType.DELETE_KB,
        knowledge_base_id=uuid4(),
        status=JobStatus.SUCCEEDED,
    )
    jobs.items[missing_job.id] = missing_job
    knowledge_base.security_scope_id = _security("other").tenant_resource_scope_id

    with pytest.raises(ResourceNotFoundError, match="Job 不存在"):
        await service.get_job(owner, delete_job.id)
    knowledge_base.security_scope_id = owner.tenant_resource_scope_id

    assert await service.get_job(owner, delete_job.id) == delete_job
    with pytest.raises(ResourceNotFoundError, match="Job 不存在"):
        await service.get_job(owner, other_job.id)
    with pytest.raises(ResourceNotFoundError, match="Job 不存在"):
        await service.get_job(owner, missing_job.id)
    with pytest.raises(ResourceNotFoundError, match="Job 不存在"):
        await service.get_job(_security("other"), delete_job.id)


@pytest.mark.asyncio
@pytest.mark.parametrize('old,new', [
    ('picture-default@1', 'mineru-image@1'), ('mineru-image@1', 'picture-default@1'),
])
async def test_reparse_cannot_change_image_storage_format(
    service_settings: ServiceSettings, old: str, new: str,
) -> None:
    """已上传文件不能靠重解析切换图片/PDF 格式，拒绝时记录与任务保持不变。"""
    service, knowledge_bases, documents, jobs, _, _ = _service(service_settings)
    security = _security()
    kb = KnowledgeBase(
        security_scope_id=security.tenant_resource_scope_id, name='Image fixture',
        parser_profile_ref=old, chunk_profile_ref='visual-192@1',
        embedding_profile_ref='embedding-openai-compatible@3',
        retrieval_profile_ref='hybrid-default@1', ragflow_dataset_id='dataset',
        status=KnowledgeBaseStatus.READY,
    )
    knowledge_bases.items[kb.id] = kb
    document = DocumentRecord(
        security_scope_id=security.tenant_resource_scope_id, knowledge_base_id=kb.id,
        filename='fixture.png', safe_filename='fixture.png', mime_type='image/png',
        file_size_bytes=7, content_sha256='a' * 64, parser_profile_ref=old,
        chunk_profile_ref='visual-192@1', ragflow_document_id='image',
        status=DocumentStatus.READY,
    )
    documents.items[document.id] = document
    with pytest.raises(RagError, match='重新上传'):
        await service.create_parse_job(security, document.id, reparse=True, parser_profile=new)
    assert document.parser_profile_ref == old
    assert document.status == DocumentStatus.READY
    assert not jobs.items
