"""摄取 Worker 测试：上传/解析/删除、轮询、取消、重试、租约检查点和修订号变化。"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from rag_service.application.ingestion_worker import IngestionWorker
from rag_service.application.profiles import ProfileCatalog
from rag_service.domain.errors import RagError, RagFlowError
from rag_service.domain.models import (
    DocumentEntity,
    DocumentIndexNode,
    DocumentRecord,
    DocumentStatus,
    DocumentUpdateMode,
    DocumentVersion,
    DocumentVersionStatus,
    EntityRelation,
    IngestionJob,
    JobStatus,
    JobType,
    KnowledgeBase,
    KnowledgeBaseStatus,
)
from rag_service.domain.ports import (
    DeleteEngineDocumentCommand,
    EngineChunk,
    EngineDocument,
    EngineDocumentStatus,
    GetEngineDocumentQuery,
    GetEngineDocumentVersionQuery,
    ListEngineChunksQuery,
    ListEngineVersionChunksQuery,
    Page,
    ParseDocumentCommand,
    PreparedMediaDocument,
    SetEngineDocumentVersionAvailabilityCommand,
    SetEngineVersionChunksAvailabilityCommand,
    StagedFile,
    StagedFileMetadata,
    StopParseCommand,
    UpdateEngineDocumentCommand,
    UpdateEngineDocumentVersionCommand,
    UploadDocumentCommand,
    UploadDocumentVersionCommand,
)
from rag_service.settings import ServiceSettings


class FakeDocuments:
    def __init__(self, document: DocumentRecord) -> None:
        self.document = document

    async def get(self, security_scope_id: str, document_id: UUID) -> DocumentRecord | None:
        if self.document.security_scope_id == security_scope_id and self.document.id == document_id:
            return self.document
        return None

    async def update(
        self,
        security_scope_id: str,
        value: DocumentRecord,
        expected_row_version: int,
    ) -> bool:
        assert value.security_scope_id == security_scope_id
        if value.row_version != expected_row_version:
            return False
        value.row_version += 1
        return True

    async def list(
        self,
        security_scope_id: str,
        knowledge_base_id: UUID,
        *,
        page: int,
        page_size: int,
        **_filters: Any,
    ) -> tuple[tuple[DocumentRecord, ...], int]:
        if (
            self.document.security_scope_id == security_scope_id
            and self.document.knowledge_base_id == knowledge_base_id
            and self.document.status != DocumentStatus.DELETED
        ):
            return (self.document,), 1
        return (), 0


class FakeJobs:
    def __init__(self, job: IngestionJob) -> None:
        self.job = job
        self.items: dict[UUID, IngestionJob] = {job.id: job}

    async def get(self, security_scope_id: str, job_id: UUID) -> IngestionJob | None:
        job = self.items.get(job_id)
        if job is not None and job.security_scope_id == security_scope_id:
            return job
        return None

    async def add(self, security_scope_id: str, value: IngestionJob) -> None:
        assert value.security_scope_id == security_scope_id
        self.items[value.id] = value

    async def update(self, security_scope_id: str, value: IngestionJob) -> bool:
        assert value.security_scope_id == security_scope_id
        self.items[value.id] = value
        return True


class FakeStaging:
    def __init__(self, staged_file_id: UUID) -> None:
        self.staged_file_id = staged_file_id
        self.deleted: list[UUID] = []

    @asynccontextmanager
    async def open(
        self,
        staged_file_id: UUID,
        *,
        expected_size: int,
        expected_sha256: str,
    ) -> Any:
        assert staged_file_id == self.staged_file_id
        assert expected_size == 7
        assert expected_sha256 == "a" * 64
        yield BytesIO(b"fixture")

    async def delete(self, staged_file_id: UUID) -> None:
        self.deleted.append(staged_file_id)


class FakeMediaStaging:
    """保存原媒体和派生 Markdown，模拟跨 Worker 重试仍可读取的共享暂存卷。"""

    def __init__(self, staged_file_id: UUID, content: bytes) -> None:
        """用调用方给定 UUID 保存合成原媒体，并初始化删除审计。"""
        self.files: dict[UUID, bytes] = {staged_file_id: content}
        self.filenames: dict[UUID, str] = {staged_file_id: "fixture.wav"}
        self.deleted: list[UUID] = []

    @asynccontextmanager
    async def open(
        self,
        staged_file_id: UUID,
        *,
        expected_size: int,
        expected_sha256: str,
    ) -> Any:
        """按真实端口规则复验大小和摘要，再返回独立可读句柄。"""
        content = self.files[staged_file_id]
        assert len(content) == expected_size
        assert hashlib.sha256(content).hexdigest() == expected_sha256
        yield BytesIO(content)

    async def save(
        self,
        stream: AsyncIterator[bytes],
        metadata: StagedFileMetadata,
    ) -> StagedFile:
        """收集媒体处理器生成的 Markdown，并返回带真实摘要的暂存记录。"""
        content = b"".join([chunk async for chunk in stream])
        staged_file_id = uuid4()
        self.files[staged_file_id] = content
        self.filenames[staged_file_id] = metadata.original_filename
        return StagedFile(
            staged_file_id=staged_file_id,
            safe_filename=metadata.original_filename,
            size_bytes=len(content),
            content_sha256=hashlib.sha256(content).hexdigest(),
            ready_path=Path(f"/synthetic/{staged_file_id}.ready"),
        )

    async def delete(self, staged_file_id: UUID) -> None:
        """记录并幂等删除，便于断言原媒体与最终派生文件的生命周期。"""
        self.deleted.append(staged_file_id)
        self.files.pop(staged_file_id, None)


class FakeMediaProcessor:
    """把任意 WAV 转换为固定 Markdown，并记录昂贵云端处理被调用的次数。"""

    def __init__(self) -> None:
        """初始化调用计数为零。"""
        self.calls = 0

    def supports(self, filename: str) -> bool:
        """测试替身只处理 WAV，避免误伤现有普通文档用例。"""
        return filename.endswith(".wav")

    async def prepare(
        self,
        content: Any,
        *,
        filename: str,
        mime_type: str,
    ) -> PreparedMediaDocument:
        """读取合成原媒体后返回带时间轴的确定性 Markdown。"""
        self.calls += 1
        assert content.read() == b"synthetic wav"
        assert filename == "fixture.wav"
        assert mime_type == "audio/wav"
        return PreparedMediaDocument(
            filename="fixture.media-transcript.md",
            content=b"# fixture\n\n## [00:00:00-00:00:01]\nSynthetic transcript.\n",
            mime_type="text/markdown",
            metadata={
                "media_source_type": "audio",
                "media_duration_seconds": 1.0,
                "lazytt_media_processing_status": "completed",
            },
        )


class FakeIndexNodes:
    """记录 Worker 原子替换的层级索引节点。"""

    def __init__(self) -> None:
        """初始化为空的替换记录。"""
        self.document_id: UUID | None = None
        self.nodes: tuple[DocumentIndexNode, ...] = ()

    async def replace_document_nodes(
        self,
        security_scope_id: str,
        document_id: UUID,
        nodes: tuple[DocumentIndexNode, ...],
    ) -> None:
        """验证节点安全范围并保存最后一次完整替换结果。"""
        assert all(node.security_scope_id == security_scope_id for node in nodes)
        self.document_id = document_id
        self.nodes = nodes


class FakeEntityGraph:
    """记录 Worker 替换的实体和关系，验证重解析不会只追加旧图。"""

    def __init__(self) -> None:
        """初始化为空结果，等待一次解析完成回调。"""
        self.document_id: UUID | None = None
        self.entities: tuple[DocumentEntity, ...] = ()
        self.relations: tuple[EntityRelation, ...] = ()

    async def replace_document_graph(
        self,
        security_scope_id: str,
        document_id: UUID,
        entities: tuple[DocumentEntity, ...],
        relations: tuple[EntityRelation, ...],
    ) -> None:
        """验证租户边界并保存完整替换集合，模拟同事务 PostgreSQL Repository。"""
        assert all(entity.security_scope_id == security_scope_id for entity in entities)
        assert all(relation.security_scope_id == security_scope_id for relation in relations)
        self.document_id = document_id
        self.entities = entities
        self.relations = relations


class FakeEmbedding:
    """为每个层级节点生成两个维度的确定性向量。"""

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        """按输入数量返回向量，便于断言所有节点都完成向量化。"""
        return tuple((1.0, float(index)) for index, _ in enumerate(texts))


class FakeEngine:
    def __init__(
        self,
        *,
        fail: bool = False,
        fail_parse: bool = False,
        parse_status: EngineDocumentStatus | None = None,
        parse_statuses: tuple[EngineDocumentStatus, ...] = (),
        cancel_job: IngestionJob | None = None,
    ) -> None:
        self.fail = fail
        self.fail_parse = fail_parse
        self.command: UploadDocumentCommand | None = None
        self.parse_command: ParseDocumentCommand | None = None
        self.stop_command: StopParseCommand | None = None
        self.delete_command: DeleteEngineDocumentCommand | None = None
        self.update_document_command: UpdateEngineDocumentCommand | None = None
        self.chunk_queries: list[ListEngineChunksQuery] = []
        self.parse_status = parse_status
        self.parse_statuses = list(parse_statuses)
        self.cancel_job = cancel_job

    async def upload_document(self, command: UploadDocumentCommand) -> EngineDocument:
        self.command = command
        if self.fail:
            raise RagFlowError.connection()
        return EngineDocument(
            engine_id="engine-document-1",
            dataset_engine_id="engine-dataset-1",
            name=command.filename,
            run_state="UNSTART",
            size_bytes=7,
        )

    async def start_parse(self, command: ParseDocumentCommand) -> None:
        self.parse_command = command
        if self.fail_parse:
            raise RagFlowError.downstream()
        if self.cancel_job is not None:
            self.cancel_job.status = JobStatus.CANCEL_REQUESTED

    async def stop_parse(self, command: StopParseCommand) -> None:
        self.stop_command = command

    async def get_document(self, _query: GetEngineDocumentQuery) -> EngineDocumentStatus:
        if self.parse_statuses:
            return self.parse_statuses.pop(0)
        assert self.parse_status is not None
        return self.parse_status

    async def delete_document(self, command: DeleteEngineDocumentCommand) -> None:
        self.delete_command = command

    async def list_chunks(self, query: ListEngineChunksQuery) -> Page:
        """返回含中英文与表格标记的真实形态块，供 Metadata 补全用例观察。"""
        self.chunk_queries.append(query)
        return Page(
            items=(
                EngineChunk(
                    engine_chunk_id="engine-chunk-1",
                    document_id=query.document_id,
                    content="RAGFlow 使用 Elasticsearch 进行索引。这是中文合成正文。",
                    metadata={"type": "text"},
                ),
            ),
            page=query.page,
            page_size=query.page_size,
            total=1,
        )

    async def update_document(
        self,
        command: UpdateEngineDocumentCommand,
    ) -> EngineDocument:
        """记录同步给 RAGFlow 的自动 Metadata，并返回标准文档摘要。"""
        self.update_document_command = command
        return EngineDocument(
            engine_id="engine-document-1",
            dataset_engine_id="engine-dataset-1",
            name="fixture.txt",
            run_state="DONE",
            size_bytes=7,
        )


class FakeKnowledgeBases:
    def __init__(self, value: KnowledgeBase) -> None:
        self.value = value

    async def get(self, security_scope_id: str, knowledge_base_id: UUID) -> KnowledgeBase | None:
        if self.value.security_scope_id == security_scope_id and self.value.id == knowledge_base_id:
            return self.value
        return None

    async def increment_revision(self, security_scope_id: str, knowledge_base_id: UUID) -> bool:
        """模拟独立的版本递增，不通过整条知识库更新。"""
        value = await self.get(security_scope_id, knowledge_base_id)
        if value is None or value.status == KnowledgeBaseStatus.DELETED:
            return False
        value.revision += 1
        value.row_version += 1
        return True

    async def update(
        self,
        security_scope_id: str,
        value: KnowledgeBase,
        expected_row_version: int,
    ) -> bool:
        assert value.security_scope_id == security_scope_id
        if value.row_version != expected_row_version:
            return False
        value.row_version += 1
        return True


class FakeDocumentVersions:
    """保存活动、候选与退役版本，模拟一次原子激活所需的仓库语义。"""

    def __init__(
        self,
        *values: DocumentVersion,
        fail_activate: bool = False,
    ) -> None:
        """用测试给定版本初始化内存集合，并可注入数据库激活冲突。"""
        self.items = {value.id: value for value in values}
        self.fail_activate = fail_activate

    async def get(self, security_scope_id: str, version_id: UUID) -> DocumentVersion | None:
        """按安全 Scope 查询版本。"""
        value = self.items.get(version_id)
        return value if value and value.security_scope_id == security_scope_id else None

    async def get_active(self, security_scope_id: str, document_id: UUID) -> DocumentVersion | None:
        """返回当前唯一活动版本。"""
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

    async def update(self, security_scope_id: str, value: DocumentVersion) -> bool:
        """保存状态、引擎映射和解析统计。"""
        assert value.security_scope_id == security_scope_id
        self.items[value.id] = value
        return True

    async def activate(
        self,
        security_scope_id: str,
        *,
        document_id: UUID,
        version_id: UUID,
        activated_at: Any,
    ) -> bool:
        """把旧活动版退役并激活目标版本；故障开关模拟提交前并发冲突。"""
        if self.fail_activate:
            return False
        target = await self.get(security_scope_id, version_id)
        if (
            target is None
            or target.document_id != document_id
            or target.status != DocumentVersionStatus.INDEXING
        ):
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

    async def list(
        self,
        security_scope_id: str,
        document_id: UUID,
        *,
        page: int,
        page_size: int,
    ) -> tuple[tuple[DocumentVersion, ...], int]:
        """分页列出同一 Scope 和逻辑文档的全部版本。"""
        values = tuple(
            value
            for value in self.items.values()
            if value.security_scope_id == security_scope_id and value.document_id == document_id
        )
        return values[(page - 1) * page_size : page * page_size], len(values)


class FakeVersionEngine:
    """模拟候选解析、完整物理文档开关、Chunk 修复及 Metadata 同步。"""

    def __init__(
        self,
        document_id: UUID,
        *,
        candidate_has_chunks: bool = True,
        expected_upload_content: bytes = b"fixture",
    ) -> None:
        """建立新旧版本 Chunk，并允许测试候选空索引或媒体派生正文。"""
        self.document_id = document_id
        self.candidate_has_chunks = candidate_has_chunks
        self.expected_upload_content = expected_upload_content
        self.availability_commands: list[SetEngineDocumentVersionAvailabilityCommand] = []
        self.chunk_availability_commands: list[SetEngineVersionChunksAvailabilityCommand] = []
        self.upload_command: UploadDocumentVersionCommand | None = None
        self.metadata_command: UpdateEngineDocumentVersionCommand | None = None

    async def upload_document_version(
        self, command: UploadDocumentVersionCommand
    ) -> EngineDocument:
        """校验 Worker 二次读取到候选字节，并返回独立物理 ID。"""
        assert command.content.read() == self.expected_upload_content
        self.upload_command = command
        return EngineDocument(
            engine_id="engine-v2",
            dataset_engine_id="engine-dataset-1",
            name=command.filename,
            run_state="DONE",
            size_bytes=7,
        )

    async def get_document_version(
        self, query: GetEngineDocumentVersionQuery
    ) -> EngineDocumentStatus:
        """新旧物理文档都已解析完成，既支持首次发布也支持回滚。"""
        assert query.engine_document_id in {"engine-v1", "engine-v2"}
        return EngineDocumentStatus(
            engine_id=query.engine_document_id,
            dataset_engine_id="engine-dataset-1",
            name="policy-v2.txt" if query.engine_document_id == "engine-v2" else "policy-v1.txt",
            run_state="DONE",
            progress=1.0,
            chunk_count=1,
            token_count=12,
        )

    async def list_document_version_chunks(self, query: ListEngineVersionChunksQuery) -> Page:
        """分别返回活动版和候选版真实 Chunk，支持发布时双向可用性切换。"""
        chunk_id = "chunk-v2" if query.engine_document_id == "engine-v2" else "chunk-v1"
        if chunk_id == "chunk-v2" and not self.candidate_has_chunks:
            return Page(items=(), page=query.page, page_size=query.page_size, total=0)
        content = "新版本新增了安全发布说明。" if chunk_id == "chunk-v2" else "旧版本正文。"
        return Page(
            items=(
                EngineChunk(
                    engine_chunk_id=chunk_id,
                    document_id=self.document_id,
                    content=content,
                ),
            ),
            page=query.page,
            page_size=query.page_size,
            total=1,
        )

    async def update_document_version(
        self, command: UpdateEngineDocumentVersionCommand
    ) -> EngineDocument:
        """记录只写入候选物理文档的自动 Metadata。"""
        self.metadata_command = command
        return EngineDocument(
            engine_id=command.engine_document_id,
            dataset_engine_id="engine-dataset-1",
            name="policy-v2.txt",
            run_state="DONE",
            size_bytes=7,
        )

    async def set_document_version_chunks_available(
        self, command: SetEngineVersionChunksAvailabilityCommand
    ) -> None:
        """保留细粒度 Chunk 修复能力，并证明主版本切换没有误用该入口。"""
        self.chunk_availability_commands.append(command)

    async def set_document_version_available(
        self, command: SetEngineDocumentVersionAvailabilityCommand
    ) -> None:
        """记录整份候选/活动物理文档关闭和打开的严格顺序。"""
        self.availability_commands.append(command)


async def _no_sleep(_seconds: float) -> None:
    return None


def _records() -> tuple[DocumentRecord, IngestionJob, UUID]:
    scope = "a" * 64
    knowledge_base_id = uuid4()
    staged_file_id = uuid4()
    document = DocumentRecord(
        security_scope_id=scope,
        knowledge_base_id=knowledge_base_id,
        filename="fixture.txt",
        safe_filename="fixture.txt",
        mime_type="text/plain",
        file_size_bytes=7,
        content_sha256="a" * 64,
        parser_profile_ref="deepdoc-default@1",
        chunk_profile_ref="general-512@1",
    )
    job = IngestionJob(
        security_scope_id=scope,
        job_type=JobType.UPLOAD,
        knowledge_base_id=knowledge_base_id,
        document_id=document.id,
        payload={
            "schema_version": 1,
            "staged_file_id": str(staged_file_id),
            "safe_filename": "fixture.txt",
            "size_bytes": 7,
            "content_sha256": "a" * 64,
        },
    )
    return document, job, staged_file_id


@pytest.mark.asyncio
async def test_upload_worker_maps_document_and_deletes_staging() -> None:
    document, job, staged_file_id = _records()
    # 模拟同一个文档首次上传被旧版适配器误判失败后再重试。成功映射 RAGFlow 文档时，
    # Worker 不仅要推进状态，也必须移除旧错误，避免 READY 文档继续携带失败提示。
    document.last_error_code = "RAG-RF-004"
    document.last_error_message = "RAGFlow 响应协议无效"
    staging = FakeStaging(staged_file_id)
    engine = FakeEngine()
    jobs = FakeJobs(job)
    worker = IngestionWorker(
        FakeDocuments(document),
        jobs,
        staging,
        engine,
    )

    result = await worker.execute(job.security_scope_id, job.id)

    assert result.status == JobStatus.SUCCEEDED
    assert result.progress == 1.0
    assert document.status == DocumentStatus.UPLOADED
    assert document.ragflow_document_id == "engine-document-1"
    assert document.last_error_code is None
    assert document.last_error_message is None
    assert staging.deleted == [staged_file_id]
    assert engine.command is not None
    assert engine.command.security.security_scope_id == job.security_scope_id


@pytest.mark.asyncio
async def test_upload_worker_preserves_staging_and_marks_failures() -> None:
    document, job, staged_file_id = _records()
    initial_version = DocumentVersion(
        security_scope_id=document.security_scope_id,
        knowledge_base_id=document.knowledge_base_id,
        document_id=document.id,
        version_number=1,
        filename=document.filename,
        safe_filename=document.safe_filename,
        mime_type=document.mime_type,
        file_size_bytes=document.file_size_bytes,
        content_sha256=document.content_sha256,
        parser_profile_ref=document.parser_profile_ref,
        chunk_profile_ref=document.chunk_profile_ref,
    )
    document.active_version_id = initial_version.id
    staging = FakeStaging(staged_file_id)
    worker = IngestionWorker(
        FakeDocuments(document),
        FakeJobs(job),
        staging,
        FakeEngine(fail=True),
        versions=FakeDocumentVersions(initial_version),
    )

    with pytest.raises(RagFlowError):
        await worker.execute(job.security_scope_id, job.id)

    assert job.status == JobStatus.RETRY_WAIT
    assert job.next_retry_at is not None
    assert document.status == DocumentStatus.FAILED
    assert initial_version.status == DocumentVersionStatus.FAILED
    assert initial_version.last_error_code == "RAG-RF-001"
    assert staging.deleted == []


@pytest.mark.asyncio
async def test_media_upload_checkpoint_prevents_duplicate_cloud_processing_on_retry() -> None:
    """RAGFlow 首次上传失败后复用派生 Markdown，不再次调用 ASR 或删除其恢复点。"""
    scope = "a" * 64
    knowledge_base_id = uuid4()
    staged_file_id = uuid4()
    original = b"synthetic wav"
    original_sha256 = hashlib.sha256(original).hexdigest()
    document = DocumentRecord(
        security_scope_id=scope,
        knowledge_base_id=knowledge_base_id,
        filename="fixture.wav",
        safe_filename="fixture.wav",
        mime_type="audio/wav",
        file_size_bytes=len(original),
        content_sha256=original_sha256,
        parser_profile_ref="media-default@1",
        chunk_profile_ref="general-256@2",
    )
    job = IngestionJob(
        security_scope_id=scope,
        job_type=JobType.UPLOAD,
        knowledge_base_id=knowledge_base_id,
        document_id=document.id,
        max_attempts=2,
        payload={
            "schema_version": 1,
            "staged_file_id": str(staged_file_id),
            "safe_filename": "fixture.wav",
            "size_bytes": len(original),
            "content_sha256": original_sha256,
        },
    )
    documents = FakeDocuments(document)
    jobs = FakeJobs(job)
    staging = FakeMediaStaging(staged_file_id, original)
    engine = FakeEngine(fail=True)
    processor = FakeMediaProcessor()
    worker = IngestionWorker(
        documents,
        jobs,
        staging,
        engine,
        media_processor=processor,
    )

    # 第一次执行已经完成 ASR 检查点，但模拟 RAGFlow 上传失败。派生文件必须保留，
    # 这样调度器领取重试任务时不会重复调用按量计费的云模型。
    with pytest.raises(RagFlowError):
        await worker.execute(scope, job.id)
    derived_staged_file_id = UUID(str(job.payload["staged_file_id"]))
    assert processor.calls == 1
    assert job.payload["media_preprocessed"] is True
    assert staged_file_id in staging.deleted
    assert derived_staged_file_id in staging.files
    assert document.content_sha256 == original_sha256
    assert document.metadata["media_source_type"] == "audio"

    # 模拟有限重试重新领取任务。只切换任务/引擎状态，不修改已经持久化的 Payload。
    engine.fail = False
    job.status = JobStatus.PENDING
    job.next_retry_at = None
    result = await worker.execute(scope, job.id)

    assert result.status == JobStatus.SUCCEEDED
    assert processor.calls == 1
    assert derived_staged_file_id in staging.deleted
    assert engine.command is not None
    assert engine.command.filename.endswith("fixture.media-transcript.md")
    assert engine.command.content_media_type == "text/markdown"
    assert engine.command.expected_size_bytes == len(
        b"# fixture\n\n## [00:00:00-00:00:01]\nSynthetic transcript.\n"
    )
    assert (
        engine.command.expected_sha256
        == hashlib.sha256(
            b"# fixture\n\n## [00:00:00-00:00:01]\nSynthetic transcript.\n"
        ).hexdigest()
    )


@pytest.mark.asyncio
async def test_media_upload_fails_explicitly_when_processor_is_not_enabled() -> None:
    """媒体配置漏装处理器时返回稳定错误，绝不把 WAV 二进制交给 RAGFlow。"""
    document, job, staged_file_id = _records()
    document.filename = "fixture.wav"
    document.safe_filename = "fixture.wav"
    document.mime_type = "audio/wav"
    job.payload["safe_filename"] = "fixture.wav"
    engine = FakeEngine()
    worker = IngestionWorker(
        FakeDocuments(document),
        FakeJobs(job),
        FakeStaging(staged_file_id),
        engine,
    )

    with pytest.raises(RagError) as captured:
        await worker.execute(job.security_scope_id, job.id)

    assert captured.value.code == "RAG-DOC-013"
    assert captured.value.retryable is True
    assert engine.command is None


@pytest.mark.asyncio
async def test_upload_worker_enqueues_parse_when_requested() -> None:
    document, job, staged_file_id = _records()
    job.payload["auto_parse"] = True
    jobs = FakeJobs(job)
    worker = IngestionWorker(
        FakeDocuments(document),
        jobs,
        FakeStaging(staged_file_id),
        FakeEngine(),
    )

    result = await worker.execute(job.security_scope_id, job.id)

    assert document.status == DocumentStatus.QUEUED
    parse_jobs = [item for item in jobs.items.values() if item.job_type == JobType.PARSE]
    assert len(parse_jobs) == 1
    assert result.result["parse_job_id"] == str(parse_jobs[0].id)


@pytest.mark.asyncio
@pytest.mark.parametrize("chunk_count", [0, 2])
async def test_parse_worker_marks_ready_and_increments_revision(chunk_count: int) -> None:
    document, _, staged_file_id = _records()
    document.status = DocumentStatus.QUEUED
    document.ragflow_document_id = "engine-document-1"
    job = IngestionJob(
        security_scope_id=document.security_scope_id,
        job_type=JobType.PARSE,
        knowledge_base_id=document.knowledge_base_id,
        document_id=document.id,
        payload={"schema_version": 1, "delete_existing": False},
    )
    knowledge_base = KnowledgeBase(
        security_scope_id=document.security_scope_id,
        id=document.knowledge_base_id,
        name="Synthetic",
        parser_profile_ref="deepdoc-default@1",
        chunk_profile_ref="general-512@1",
        embedding_profile_ref="embedding-openai-compatible@3",
        retrieval_profile_ref="hybrid-default@1",
        status=KnowledgeBaseStatus.READY,
        ragflow_dataset_id="engine-dataset-1",
    )
    engine = FakeEngine(
        parse_status=EngineDocumentStatus(
            engine_id="engine-document-1",
            dataset_engine_id="engine-dataset-1",
            name="fixture.txt",
            run_state="DONE",
            progress=1.0,
            chunk_count=chunk_count,
            token_count=11,
        )
    )
    worker = IngestionWorker(
        FakeDocuments(document),
        FakeJobs(job),
        FakeStaging(staged_file_id),
        engine,
        FakeKnowledgeBases(knowledge_base),
    )

    # 复现引擎 DONE 但零切块：应失败且不可增加知识库版本或写入 READY。
    if chunk_count == 0:
        with pytest.raises(RagError) as error:
            await worker.execute(job.security_scope_id, job.id)
        assert error.value.code == "RAG-DOC-008"
        assert document.status == DocumentStatus.FAILED
        assert job.status == JobStatus.FAILED
        assert document.indexed_at is None
        assert knowledge_base.revision == 0
        return

    # 模拟 Runner 已领取重试任务：不会经过 PENDING 分支清理上次错误。
    job.status = JobStatus.RUNNING
    job.locked_by = "retry-worker"
    job.error_code = "RAG-DB-002"
    job.error_message = "知识库 Revision 并发更新冲突"
    result = await worker.execute(job.security_scope_id, job.id, worker_id="retry-worker")

    assert result.error_code is None
    assert result.error_message is None
    assert result.status == JobStatus.SUCCEEDED
    assert result.result == {"chunk_count": 2, "token_count": 11}
    assert document.status == DocumentStatus.READY
    assert document.indexed_at is not None
    assert document.metadata["language"] == "zh-CN"
    assert document.metadata["title"] == "fixture"
    assert document.metadata["file_type"] == "txt"
    assert document.metadata["lazytt_metadata_status"] == "completed"
    assert document.metadata["lazytt_has_table"] is False
    assert knowledge_base.revision == 1
    assert engine.parse_command is None
    assert engine.update_document_command is not None
    assert engine.update_document_command.metadata == document.metadata


@pytest.mark.asyncio
async def test_parse_worker_marks_document_failed_when_start_parse_fails() -> None:
    """RAGFlow 拒绝启动解析时，Job 和文档必须一起进入可重试失败状态。

    若只失败 Job 而把文档留在 PARSING，标准重试接口会拒绝该文档，用户只能看到
    永久停在 25% 的记录。本用例锁定失败收敛，避免再次产生不可恢复的悬挂状态。
    """
    document, _, staged_file_id = _records()
    document.status = DocumentStatus.QUEUED
    document.ragflow_document_id = "engine-document-1"
    job = IngestionJob(
        security_scope_id=document.security_scope_id,
        job_type=JobType.PARSE,
        knowledge_base_id=document.knowledge_base_id,
        document_id=document.id,
        payload={"schema_version": 1, "delete_existing": False},
    )
    worker = IngestionWorker(
        FakeDocuments(document),
        FakeJobs(job),
        FakeStaging(staged_file_id),
        FakeEngine(
            fail_parse=True,
            parse_status=EngineDocumentStatus(
                engine_id="engine-document-1",
                dataset_engine_id="engine-dataset-1",
                name="fixture.txt",
                run_state="UNSTART",
                progress=0.0,
                chunk_count=0,
                token_count=0,
            ),
        ),
    )

    with pytest.raises(RagFlowError):
        await worker.execute(job.security_scope_id, job.id)

    assert job.status == JobStatus.FAILED
    assert document.status == DocumentStatus.FAILED
    assert document.last_error_code == "RAG-RF-002"


@pytest.mark.asyncio
async def test_parse_worker_builds_multigranular_sidecar_index() -> None:
    """解析完成会构建 Parent/Section/Summary 节点，但不改变 RAGFlow 原始块。"""
    document, _, staged_file_id = _records()
    document.status = DocumentStatus.QUEUED
    document.ragflow_document_id = "engine-document-1"
    job = IngestionJob(
        security_scope_id=document.security_scope_id,
        job_type=JobType.PARSE,
        knowledge_base_id=document.knowledge_base_id,
        document_id=document.id,
    )
    engine = FakeEngine(
        parse_status=EngineDocumentStatus(
            engine_id="engine-document-1",
            dataset_engine_id="engine-dataset-1",
            name="fixture.txt",
            run_state="DONE",
            progress=1.0,
            chunk_count=1,
            token_count=11,
        )
    )
    index_nodes = FakeIndexNodes()
    entity_graph = FakeEntityGraph()

    await IngestionWorker(
        FakeDocuments(document),
        FakeJobs(job),
        FakeStaging(staged_file_id),
        engine,
        index_nodes=index_nodes,
        entity_graph=entity_graph,
        embedding=FakeEmbedding(),
    ).execute(job.security_scope_id, job.id)

    assert index_nodes.document_id == document.id
    assert [node.node_type.value for node in index_nodes.nodes] == [
        "PARENT",
        "SECTION",
        "SUMMARY",
    ]
    assert all(node.source_chunk_ids == ("engine-chunk-1",) for node in index_nodes.nodes)
    assert all(len(node.embedding) == 2 for node in index_nodes.nodes)
    assert document.metadata["lazytt_hierarchical_index"] == "ready"
    assert document.metadata["lazytt_hierarchy_version"] == 3
    assert document.metadata["lazytt_context_prefix_version"] == 1
    assert document.metadata["lazytt_parent_strategy"] == "heading-length-v3"
    assert document.metadata["lazytt_parent_count"] == 1
    assert document.metadata["lazytt_section_count"] == 1
    assert document.metadata["lazytt_summary_count"] == 1
    assert entity_graph.document_id == document.id
    assert {entity.normalized_name for entity in entity_graph.entities} >= {
        "ragflow",
        "elasticsearch",
    }
    assert entity_graph.relations[0].source_chunk_ids == ("engine-chunk-1",)
    assert document.metadata["lazytt_entity_graph"] == "ready"


@pytest.mark.asyncio
async def test_parse_worker_applies_document_level_chunk_profile_before_parse(
    service_settings: ServiceSettings,
) -> None:
    """Worker 必须把文档记录中的自动 Profile 转成真实解析命令。

    只断言 DocumentRecord 里的名称不能证明优化生效。本用例使用
    电子表格 Profile，检查发给 Adapter 的命令已经是 192 Token、5% 重叠。
    """
    document, _, staged_file_id = _records()
    document.status = DocumentStatus.QUEUED
    document.ragflow_document_id = "engine-document-1"
    document.filename = "fixture.xlsx"
    document.safe_filename = "fixture.xlsx"
    document.mime_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    document.parser_profile_ref = "office-default@1"
    document.chunk_profile_ref = "table-192@1"
    job = IngestionJob(
        security_scope_id=document.security_scope_id,
        job_type=JobType.PARSE,
        knowledge_base_id=document.knowledge_base_id,
        document_id=document.id,
    )
    engine = FakeEngine(
        parse_statuses=(
            EngineDocumentStatus(
                engine_id="engine-document-1",
                dataset_engine_id="engine-dataset-1",
                name="fixture.xlsx",
                run_state="0",
                progress=0,
                chunk_count=0,
                token_count=0,
            ),
            EngineDocumentStatus(
                engine_id="engine-document-1",
                dataset_engine_id="engine-dataset-1",
                name="fixture.xlsx",
                run_state="3",
                progress=1,
                chunk_count=1,
                token_count=10,
            ),
        )
    )

    await IngestionWorker(
        FakeDocuments(document),
        FakeJobs(job),
        FakeStaging(staged_file_id),
        engine,
        profiles=ProfileCatalog.load(service_settings.profiles),
        sleep=_no_sleep,
    ).execute(job.security_scope_id, job.id)

    assert engine.parse_command is not None
    assert engine.parse_command.chunk_method == "naive"
    assert engine.parse_command.parser_config is not None
    assert engine.parse_command.parser_config["chunk_token_num"] == 192
    assert engine.parse_command.parser_config["overlapped_percent"] == 5


@pytest.mark.asyncio
async def test_initial_parse_starts_only_when_ragflow_document_is_unstarted() -> None:
    document, _, staged_file_id = _records()
    document.status = DocumentStatus.QUEUED
    document.ragflow_document_id = "engine-document-1"
    job = IngestionJob(
        security_scope_id=document.security_scope_id,
        job_type=JobType.PARSE,
        knowledge_base_id=document.knowledge_base_id,
        document_id=document.id,
        payload={"schema_version": 1, "delete_existing": False},
    )
    unstarted = EngineDocumentStatus(
        engine_id="engine-document-1",
        dataset_engine_id="engine-dataset-1",
        name="fixture.txt",
        run_state="0",
        progress=0,
        chunk_count=0,
        token_count=0,
    )
    done = EngineDocumentStatus(
        engine_id="engine-document-1",
        dataset_engine_id="engine-dataset-1",
        name="fixture.txt",
        run_state="3",
        progress=1,
        chunk_count=2,
        token_count=11,
    )
    engine = FakeEngine(parse_statuses=(unstarted, done))

    result = await IngestionWorker(
        FakeDocuments(document),
        FakeJobs(job),
        FakeStaging(staged_file_id),
        engine,
        sleep=_no_sleep,
    ).execute(job.security_scope_id, job.id)

    assert result.status == JobStatus.SUCCEEDED
    assert engine.parse_command is not None
    assert engine.parse_command.delete_existing is False


@pytest.mark.asyncio
async def test_initial_parse_adopts_running_ragflow_job_without_restarting_it() -> None:
    document, _, staged_file_id = _records()
    document.status = DocumentStatus.QUEUED
    document.ragflow_document_id = "engine-document-1"
    job = IngestionJob(
        security_scope_id=document.security_scope_id,
        job_type=JobType.PARSE,
        knowledge_base_id=document.knowledge_base_id,
        document_id=document.id,
    )
    running = EngineDocumentStatus(
        engine_id="engine-document-1",
        dataset_engine_id="engine-dataset-1",
        name="fixture.txt",
        run_state="RUNNING",
        progress=0.5,
        chunk_count=1,
        token_count=5,
    )
    done = EngineDocumentStatus(
        engine_id="engine-document-1",
        dataset_engine_id="engine-dataset-1",
        name="fixture.txt",
        run_state="DONE",
        progress=1,
        chunk_count=2,
        token_count=11,
    )
    engine = FakeEngine(parse_statuses=(running, done))

    result = await IngestionWorker(
        FakeDocuments(document),
        FakeJobs(job),
        FakeStaging(staged_file_id),
        engine,
        sleep=_no_sleep,
    ).execute(job.security_scope_id, job.id)

    assert result.status == JobStatus.SUCCEEDED
    assert engine.parse_command is None
    assert document.status == DocumentStatus.READY


@pytest.mark.asyncio
async def test_explicit_reparse_restarts_even_when_ragflow_document_is_done() -> None:
    document, _, staged_file_id = _records()
    document.status = DocumentStatus.QUEUED
    document.ragflow_document_id = "engine-document-1"
    job = IngestionJob(
        security_scope_id=document.security_scope_id,
        job_type=JobType.REPARSE,
        knowledge_base_id=document.knowledge_base_id,
        document_id=document.id,
        payload={"schema_version": 1, "delete_existing": True},
    )
    done = EngineDocumentStatus(
        engine_id="engine-document-1",
        dataset_engine_id="engine-dataset-1",
        name="fixture.txt",
        run_state="DONE",
        progress=1,
        chunk_count=2,
        token_count=11,
    )
    engine = FakeEngine(parse_statuses=(done, done))

    result = await IngestionWorker(
        FakeDocuments(document),
        FakeJobs(job),
        FakeStaging(staged_file_id),
        engine,
    ).execute(job.security_scope_id, job.id)

    assert result.status == JobStatus.SUCCEEDED
    assert engine.parse_command is not None
    assert engine.parse_command.delete_existing is True


@pytest.mark.asyncio
async def test_parse_worker_checkpoints_visible_progress_before_completion() -> None:
    """RAGFlow 中间百分比必须同时对文档列表和 Job 查询可见。

    这个回归场景模拟引擎依次返回 40%、70% 和完成。检查点相当于生产环境中的
    数据库 commit；如果 Worker 只更新 DocumentRecord 而遗漏 IngestionJob，客户端
    轮询 ``GET /v1/jobs/{id}`` 就会长期看到 0，直到任务结束后突然跳到 100%。
    """
    document, _, staged_file_id = _records()
    document.status = DocumentStatus.QUEUED
    document.ragflow_document_id = "engine-document-1"
    job = IngestionJob(
        security_scope_id=document.security_scope_id,
        job_type=JobType.PARSE,
        knowledge_base_id=document.knowledge_base_id,
        document_id=document.id,
    )
    engine = FakeEngine(
        parse_statuses=(
            EngineDocumentStatus(
                engine_id="engine-document-1",
                dataset_engine_id="engine-dataset-1",
                name="fixture.txt",
                run_state="RUNNING",
                progress=0.4,
                chunk_count=0,
                token_count=0,
            ),
            EngineDocumentStatus(
                engine_id="engine-document-1",
                dataset_engine_id="engine-dataset-1",
                name="fixture.txt",
                run_state="RUNNING",
                progress=0.7,
                chunk_count=1,
                token_count=5,
            ),
            EngineDocumentStatus(
                engine_id="engine-document-1",
                dataset_engine_id="engine-dataset-1",
                name="fixture.txt",
                run_state="DONE",
                progress=1.0,
                chunk_count=2,
                token_count=11,
            ),
        )
    )
    snapshots: list[tuple[DocumentStatus, float, JobStatus, float]] = []

    async def checkpoint() -> None:
        """保存每次事务提交时两个资源的进度，模拟 API 在该时刻读取数据库。"""
        snapshots.append((document.status, document.progress, job.status, job.progress))

    worker = IngestionWorker(
        FakeDocuments(document),
        FakeJobs(job),
        FakeStaging(staged_file_id),
        engine,
        checkpoint=checkpoint,
        sleep=_no_sleep,
    )

    await worker.execute(job.security_scope_id, job.id)

    assert any(status == DocumentStatus.PARSING for status, _, _, _ in snapshots)
    assert any(
        document_progress == pytest.approx(0.4)
        and job_status == JobStatus.RUNNING
        and job_progress == pytest.approx(0.4)
        for _, document_progress, job_status, job_progress in snapshots
    )
    assert any(
        document_progress == pytest.approx(0.7)
        and job_status == JobStatus.RUNNING
        and job_progress == pytest.approx(0.7)
        for _, document_progress, job_status, job_progress in snapshots
    )
    assert any(
        status == DocumentStatus.READY and progress == 1.0
        for status, progress, _, _ in snapshots
    )
    assert snapshots[-1][2] == JobStatus.SUCCEEDED
    assert snapshots[-1][3] == 1.0


@pytest.mark.asyncio
async def test_parse_worker_propagates_cancellation_to_ragflow() -> None:
    document, _, staged_file_id = _records()
    document.status = DocumentStatus.QUEUED
    document.ragflow_document_id = "engine-document-1"
    job = IngestionJob(
        security_scope_id=document.security_scope_id,
        job_type=JobType.PARSE,
        knowledge_base_id=document.knowledge_base_id,
        document_id=document.id,
    )
    engine = FakeEngine(
        cancel_job=job,
        parse_status=EngineDocumentStatus(
            engine_id="engine-document-1",
            dataset_engine_id="engine-dataset-1",
            name="fixture.txt",
            run_state="0",
            progress=0,
            chunk_count=0,
            token_count=0,
        ),
    )
    worker = IngestionWorker(
        FakeDocuments(document),
        FakeJobs(job),
        FakeStaging(staged_file_id),
        engine,
    )

    result = await worker.execute(job.security_scope_id, job.id)

    assert result.status == JobStatus.CANCELLED
    assert document.status == DocumentStatus.FAILED
    assert engine.stop_command is not None


@pytest.mark.asyncio
async def test_version_update_publishes_only_after_candidate_is_ready(
    service_settings: ServiceSettings,
) -> None:
    """候选就绪后按顺序关闭候选、打开候选、关闭旧物理文档。"""
    scope = "a" * 64
    knowledge_base_id = uuid4()
    staged_file_id = uuid4()
    document = DocumentRecord(
        security_scope_id=scope,
        knowledge_base_id=knowledge_base_id,
        filename="policy-v1.txt",
        safe_filename="policy-v1.txt",
        mime_type="text/plain",
        file_size_bytes=6,
        content_sha256="b" * 64,
        parser_profile_ref="office-default@1",
        chunk_profile_ref="prose-384@1",
        status=DocumentStatus.READY,
        progress=1.0,
        chunk_count=1,
        token_count=5,
        ragflow_document_id="engine-v1",
    )
    active = DocumentVersion(
        security_scope_id=scope,
        knowledge_base_id=knowledge_base_id,
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
    candidate = DocumentVersion(
        security_scope_id=scope,
        knowledge_base_id=knowledge_base_id,
        document_id=document.id,
        version_number=2,
        filename="policy-v2.txt",
        safe_filename="policy-v2.txt",
        mime_type="text/plain",
        file_size_bytes=7,
        content_sha256="a" * 64,
        parser_profile_ref="office-default@1",
        chunk_profile_ref="prose-384@1",
        requested_mode=DocumentUpdateMode.AUTO,
        effective_mode=DocumentUpdateMode.SAFE_REPLACE,
    )
    document.active_version_id = active.id
    job = IngestionJob(
        security_scope_id=scope,
        job_type=JobType.UPDATE_DOCUMENT,
        knowledge_base_id=knowledge_base_id,
        document_id=document.id,
        payload={
            "schema_version": 1,
            "operation": "publish",
            "version_id": str(candidate.id),
            "staged_file_id": str(staged_file_id),
            "size_bytes": 7,
            "content_sha256": "a" * 64,
        },
    )
    knowledge_base = KnowledgeBase(
        security_scope_id=scope,
        id=knowledge_base_id,
        name="Versioned KB",
        parser_profile_ref="office-default@1",
        chunk_profile_ref="general-256@2",
        embedding_profile_ref="embedding-openai-compatible@3",
        retrieval_profile_ref="hybrid-default@1",
        status=KnowledgeBaseStatus.READY,
        ragflow_dataset_id="engine-dataset-1",
    )
    versions = FakeDocumentVersions(active, candidate)
    staging = FakeStaging(staged_file_id)
    engine = FakeVersionEngine(document.id)
    jobs = FakeJobs(job)
    worker = IngestionWorker(
        FakeDocuments(document),
        jobs,
        staging,
        engine,
        knowledge_bases=FakeKnowledgeBases(knowledge_base),
        settings=service_settings.jobs,
        sleep=_no_sleep,
        profiles=ProfileCatalog.load(service_settings.profiles),
        versions=versions,
    )

    result = await worker.execute(scope, job.id)

    assert result.status == JobStatus.SUCCEEDED
    assert candidate.status == DocumentVersionStatus.ACTIVE
    assert active.status == DocumentVersionStatus.RETIRED
    assert document.filename == "policy-v2.txt"
    assert document.ragflow_document_id == "engine-v2"
    assert document.active_version_id == candidate.id
    assert document.active_version_number == 2
    assert document.document_revision == 2
    assert document.status == DocumentStatus.READY
    assert knowledge_base.revision == 1
    assert staging.deleted == [staged_file_id]
    assert engine.metadata_command is not None
    assert [
        (command.engine_document_id, command.available) for command in engine.availability_commands
    ] == [
        ("engine-v2", False),
        ("engine-v2", True),
        ("engine-v1", False),
    ]
    assert engine.chunk_availability_commands == []
    assert result.result["effective_mode"] == "SAFE_REPLACE"

    # 回滚不是修改状态字符串，而是走同一套候选停用、启用目标、停用当前活动版和
    # Revision 提交链路。回滚完成后 v2 仍保留为 RETIRED，可再次切换回来。
    rollback_job = IngestionJob(
        security_scope_id=scope,
        job_type=JobType.UPDATE_DOCUMENT,
        knowledge_base_id=knowledge_base_id,
        document_id=document.id,
        payload={
            "schema_version": 1,
            "operation": "rollback",
            "version_id": str(active.id),
        },
    )
    await jobs.add(scope, rollback_job)
    rollback_result = await worker.execute(scope, rollback_job.id)

    assert rollback_result.status == JobStatus.SUCCEEDED
    assert active.status == DocumentVersionStatus.ACTIVE
    assert candidate.status == DocumentVersionStatus.RETIRED
    assert document.filename == "policy-v1.txt"
    assert document.ragflow_document_id == "engine-v1"
    assert document.active_version_id == active.id
    assert document.active_version_number == 1
    assert document.document_revision == 3
    assert knowledge_base.revision == 2
    assert [
        (command.engine_document_id, command.available)
        for command in engine.availability_commands[-3:]
    ] == [
        ("engine-v1", False),
        ("engine-v1", True),
        ("engine-v2", False),
    ]
    assert engine.chunk_availability_commands == []


@pytest.mark.asyncio
async def test_version_document_switch_is_compensated_when_activation_fails(
    service_settings: ServiceSettings,
) -> None:
    """PostgreSQL 激活冲突后必须重新打开旧文档并关闭候选文档。"""
    scope = "a" * 64
    knowledge_base_id = uuid4()
    staged_file_id = uuid4()
    document = DocumentRecord(
        security_scope_id=scope,
        knowledge_base_id=knowledge_base_id,
        filename="policy-v1.txt",
        safe_filename="policy-v1.txt",
        mime_type="text/plain",
        file_size_bytes=6,
        content_sha256="b" * 64,
        parser_profile_ref="office-default@1",
        chunk_profile_ref="prose-384@1",
        status=DocumentStatus.READY,
        progress=1.0,
        chunk_count=1,
        token_count=5,
        ragflow_document_id="engine-v1",
    )
    active = DocumentVersion(
        security_scope_id=scope,
        knowledge_base_id=knowledge_base_id,
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
    )
    candidate = DocumentVersion(
        security_scope_id=scope,
        knowledge_base_id=knowledge_base_id,
        document_id=document.id,
        version_number=2,
        filename="policy-v2.txt",
        safe_filename="policy-v2.txt",
        mime_type="text/plain",
        file_size_bytes=7,
        content_sha256="a" * 64,
        parser_profile_ref="office-default@1",
        chunk_profile_ref="prose-384@1",
        requested_mode=DocumentUpdateMode.AUTO,
        effective_mode=DocumentUpdateMode.SAFE_REPLACE,
    )
    document.active_version_id = active.id
    job = IngestionJob(
        security_scope_id=scope,
        job_type=JobType.UPDATE_DOCUMENT,
        knowledge_base_id=knowledge_base_id,
        document_id=document.id,
        max_attempts=1,
        payload={
            "schema_version": 1,
            "operation": "publish",
            "version_id": str(candidate.id),
            "staged_file_id": str(staged_file_id),
            "size_bytes": 7,
            "content_sha256": "a" * 64,
        },
    )
    engine = FakeVersionEngine(document.id)
    worker = IngestionWorker(
        FakeDocuments(document),
        FakeJobs(job),
        FakeStaging(staged_file_id),
        engine,
        settings=service_settings.jobs,
        sleep=_no_sleep,
        profiles=ProfileCatalog.load(service_settings.profiles),
        versions=FakeDocumentVersions(active, candidate, fail_activate=True),
    )

    with pytest.raises(RagError, match="候选版本并发激活失败"):
        await worker.execute(scope, job.id)

    # 前三项是发布尝试，最后两项是 Saga 补偿。补偿后 RAGFlow 与仍指向 v1 的
    # DocumentRecord 再次一致，候选 v2 不会污染 Dataset Top-K。
    assert [
        (command.engine_document_id, command.available) for command in engine.availability_commands
    ] == [
        ("engine-v2", False),
        ("engine-v2", True),
        ("engine-v1", False),
        ("engine-v1", True),
        ("engine-v2", False),
    ]
    assert engine.chunk_availability_commands == []
    assert document.ragflow_document_id == "engine-v1"
    assert document.active_version_id == active.id
    assert active.status == DocumentVersionStatus.ACTIVE


@pytest.mark.asyncio
async def test_version_update_failure_keeps_old_document_active(
    service_settings: ServiceSettings,
) -> None:
    """候选没有生成 Chunk 时只失败候选，稳定文档和旧活动版本保持不变。"""
    scope = "a" * 64
    knowledge_base_id = uuid4()
    staged_file_id = uuid4()
    document = DocumentRecord(
        security_scope_id=scope,
        knowledge_base_id=knowledge_base_id,
        filename="policy-v1.txt",
        safe_filename="policy-v1.txt",
        mime_type="text/plain",
        file_size_bytes=6,
        content_sha256="b" * 64,
        parser_profile_ref="office-default@1",
        chunk_profile_ref="prose-384@1",
        status=DocumentStatus.READY,
        progress=1.0,
        chunk_count=1,
        token_count=5,
        ragflow_document_id="engine-v1",
    )
    active = DocumentVersion(
        security_scope_id=scope,
        knowledge_base_id=knowledge_base_id,
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
    )
    candidate = DocumentVersion(
        security_scope_id=scope,
        knowledge_base_id=knowledge_base_id,
        document_id=document.id,
        version_number=2,
        filename="policy-v2.txt",
        safe_filename="policy-v2.txt",
        mime_type="text/plain",
        file_size_bytes=7,
        content_sha256="a" * 64,
        parser_profile_ref="office-default@1",
        chunk_profile_ref="prose-384@1",
        requested_mode=DocumentUpdateMode.AUTO,
        effective_mode=DocumentUpdateMode.SAFE_REPLACE,
    )
    document.active_version_id = active.id
    job = IngestionJob(
        security_scope_id=scope,
        job_type=JobType.UPDATE_DOCUMENT,
        knowledge_base_id=knowledge_base_id,
        document_id=document.id,
        max_attempts=1,
        payload={
            "schema_version": 1,
            "operation": "publish",
            "version_id": str(candidate.id),
            "staged_file_id": str(staged_file_id),
            "safe_filename": candidate.safe_filename,
            "size_bytes": 7,
            "content_sha256": "a" * 64,
        },
    )
    versions = FakeDocumentVersions(active, candidate)
    worker = IngestionWorker(
        FakeDocuments(document),
        FakeJobs(job),
        FakeStaging(staged_file_id),
        FakeVersionEngine(document.id, candidate_has_chunks=False),
        settings=service_settings.jobs,
        sleep=_no_sleep,
        profiles=ProfileCatalog.load(service_settings.profiles),
        versions=versions,
    )

    with pytest.raises(RagError, match="候选版本没有生成可发布的 Chunk"):
        await worker.execute(scope, job.id)

    assert job.status == JobStatus.FAILED
    assert candidate.status == DocumentVersionStatus.FAILED
    assert active.status == DocumentVersionStatus.ACTIVE
    assert document.status == DocumentStatus.READY
    assert document.filename == "policy-v1.txt"
    assert document.ragflow_document_id == "engine-v1"
    assert document.active_version_id == active.id
    assert document.document_revision == 1


@pytest.mark.asyncio
async def test_delete_document_worker_updates_local_tombstone() -> None:
    document, _, staged_file_id = _records()
    document.status = DocumentStatus.DELETING
    document.ragflow_document_id = "engine-document-1"
    job = IngestionJob(
        security_scope_id=document.security_scope_id,
        job_type=JobType.DELETE_DOCUMENT,
        knowledge_base_id=document.knowledge_base_id,
        document_id=document.id,
    )
    knowledge_base = KnowledgeBase(
        security_scope_id=document.security_scope_id,
        id=document.knowledge_base_id,
        name="Synthetic",
        parser_profile_ref="deepdoc-default@1",
        chunk_profile_ref="general-512@1",
        embedding_profile_ref="embedding-openai-compatible@3",
        retrieval_profile_ref="hybrid-default@1",
        status=KnowledgeBaseStatus.READY,
        ragflow_dataset_id="engine-dataset-1",
    )
    engine = FakeEngine()
    worker = IngestionWorker(
        FakeDocuments(document),
        FakeJobs(job),
        FakeStaging(staged_file_id),
        engine,
        FakeKnowledgeBases(knowledge_base),
    )

    result = await worker.execute(job.security_scope_id, job.id)

    assert result.status == JobStatus.SUCCEEDED
    assert document.status == DocumentStatus.DELETED
    assert document.deleted_at is not None
    assert knowledge_base.revision == 1
    assert engine.delete_command is not None
