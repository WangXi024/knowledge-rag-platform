"""异步摄取 Worker 对单个持久化任务的执行编排。

任务可能因进程崩溃、租约超时或网络故障被再次执行，因此每条路径都尽量可重入：
先检查当前资源状态，已完成则直接回填结果；真正的外部调用前后设置检查点，让事务
提交与租约续期有机会发生。失败会分别落到任务和文档状态，重试次数由 Job 控制。
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from rag_service.application.indexing import (
    attach_embeddings,
    build_hierarchical_nodes,
    build_lightweight_graph,
    extract_structured_metadata,
)
from rag_service.application.profiles import ProfileCatalog
from rag_service.domain.errors import (
    JobCancellationRequested,
    JobLeaseLostError,
    RagError,
    ResourceNotFoundError,
)
from rag_service.domain.models import (
    DocumentRecord,
    DocumentStatus,
    DocumentVersion,
    DocumentVersionStatus,
    IngestionJob,
    JobStatus,
    JobType,
    KnowledgeBaseStatus,
    utc_now,
)
from rag_service.domain.ports import (
    DeleteEngineDatasetCommand,
    DeleteEngineDocumentCommand,
    DeleteEngineDocumentVersionCommand,
    DocumentRepository,
    DocumentVersionRepository,
    EmbeddingPort,
    EngineChunk,
    EngineDocumentStatus,
    EntityGraphRepository,
    GetEngineDocumentQuery,
    GetEngineDocumentVersionQuery,
    IndexNodeRepository,
    JobRepository,
    KnowledgeBaseRepository,
    ListEngineChunksQuery,
    ListEngineVersionChunksQuery,
    MediaProcessingPort,
    ParseDocumentCommand,
    ParseEngineDocumentVersionCommand,
    RagEnginePort,
    SetEngineDocumentVersionAvailabilityCommand,
    StagedFileMetadata,
    StopEngineDocumentVersionCommand,
    StopParseCommand,
    UpdateEngineDocumentCommand,
    UpdateEngineDocumentVersionCommand,
    UploadDocumentCommand,
    UploadDocumentVersionCommand,
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

_MEDIA_EXTENSIONS = frozenset(
    {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".mp4", ".mov", ".mkv", ".webm", ".avi"}
)


class _JobCancelled(Exception):
    """内部控制流信号：任务已按请求取消，不应再走普通失败/重试逻辑。"""

    pass


async def _noop_checkpoint() -> None:
    """未注入事务提交或回滚回调时的默认空操作。"""
    return None


async def _single_bytes_stream(content: bytes) -> AsyncIterator[bytes]:
    """把已经由媒体适配器生成的小型 Markdown 包装成暂存端口需要的异步流。"""
    yield content


def _engine_filename(document_id: UUID, safe_filename: str) -> str:
    """在文件名中加入本地文档 UUID，使 RAGFlow 返回结果可稳定映射回本地记录。"""
    available = 255 - len(str(document_id).encode("ascii")) - 1
    encoded = safe_filename.encode("utf-8")[:available]
    while encoded:
        try:
            suffix = encoded.decode("utf-8")
            break
        except UnicodeDecodeError:
            encoded = encoded[:-1]
    else:
        suffix = "document"
    return f"{document_id}-{suffix}"


def _worker_security_context(security_scope_id: str, job_id: UUID) -> SecurityContext:
    """为后台任务构造只用于适配器映射的内部安全上下文。

    Worker 没有原始 User ID，但任务记录已带服务端派生的 ``security_scope_id``。其他占位字段不会
    参与授权，外部适配器真正用于 Repository 查询的是这个隔离摘要。
    """
    return SecurityContext(
        subject_id="internal-worker",
        tenant_id="internal",
        rag_scope_id="internal",
        security_scope_id=security_scope_id,
        token_id=str(job_id),
        expires_at=datetime.max.replace(tzinfo=UTC),
    )


def _detect_document_language(sample: str) -> str | None:
    """按正文字符比例推断中文或英文，无法判断时不制造伪 Metadata。

    这是用于检索路线选择的轻量预分类，不替代专业语言识别模型。中文字符达到英文
    字母数量的四分之一即归为中文，可覆盖中英混排技术文档；没有任何中英文字符时
    返回 ``None``，让在线层继续采用安全的中英文双路线，而不是错误地只查一种语言。
    """
    cjk_count = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff]", sample))
    latin_count = len(re.findall(r"[A-Za-z]", sample))
    if cjk_count == 0 and latin_count == 0:
        return None
    return "zh-CN" if cjk_count * 4 >= latin_count else "en-US"


def _sample_contains_table(sample: str, chunks: tuple[EngineChunk, ...]) -> bool:
    """根据 Chunk 元数据和常见文本结构判断样本是否包含表格。

    优先相信 RAGFlow 返回的结构化类型字段；没有该字段时，再识别 Markdown 表格
    分隔线或一行三个以上制表符的电子表格文本。结果仅作为查询路由/过滤提示，不
    会改变原文、Chunk 或引用，因此宁可返回 False 也不做激进猜测。
    """
    for chunk in chunks:
        raw_type = chunk.metadata.get("type", chunk.metadata.get("content_type"))
        if isinstance(raw_type, str) and "table" in raw_type.casefold():
            return True
    if re.search(r"(?m)^\s*\|?.+:?-{3,}:?\s*\|", sample):
        return True
    return any(line.count("\t") >= 3 for line in sample.splitlines())


class IngestionWorker:
    """执行上传、解析、文档删除和知识库删除任务的状态机驱动器。"""

    def __init__(
        self,
        documents: DocumentRepository,
        jobs: JobRepository,
        staging: UploadStagingPort,
        rag_engine: RagEnginePort,
        knowledge_bases: KnowledgeBaseRepository | None = None,
        settings: JobsSection | None = None,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        checkpoint: Callable[[], Awaitable[None]] = _noop_checkpoint,
        rollback: Callable[[], Awaitable[None]] = _noop_checkpoint,
        index_nodes: IndexNodeRepository | None = None,
        entity_graph: EntityGraphRepository | None = None,
        embedding: EmbeddingPort | None = None,
        media_processor: MediaProcessingPort | None = None,
        profiles: ProfileCatalog | None = None,
        versions: DocumentVersionRepository | None = None,
    ) -> None:
        """注入摄取依赖，以及可选的 Gateway 侧车索引组件。

        ``profiles`` 存在时，Worker 会按 DocumentRecord 中已保存的 Parser/Chunk
        不可变引用生成文档级配置，并在开始解析前交给 RAGFlow Adapter。测试或旧版
        Runner 未注入时保持数据集配置，兼容已经排队的历史任务。

        ``checkpoint`` 提交恢复点，``rollback`` 丢弃尚未发布的侧车/指针写入；两者
        在真实 Runner 中分别绑定同一 SQLAlchemy Session 的 commit/rollback。

        ``versions`` 存在时启用安全文档更新：候选物理文档在旧版之外完成解析，
        激活时才切换主记录并推进 Revision。没有版本仓库的旧测试仍可执行历史任务，
        但若收到 ``UPDATE_DOCUMENT`` 会明确失败，绝不会降级为原地覆盖。

        ``index_nodes`` 和 ``embedding`` 同时存在时，解析完成会构建
        Document Summary/Section/Parent 多粒度索引；``entity_graph`` 存在时继续
        维护已有的确定性实体/显式关系索引。任何组件缺失都代表明确关闭对应增强
        路线，核心 RAGFlow 摄取路径仍保持可用。
        """
        self._documents = documents
        self._jobs = jobs
        self._staging = staging
        self._rag_engine = rag_engine
        self._knowledge_bases = knowledge_bases
        self._settings = settings or JobsSection()
        self._sleep = sleep
        self._monotonic = monotonic
        self._checkpoint = checkpoint
        self._rollback = rollback
        self._index_nodes = index_nodes
        self._entity_graph = entity_graph
        self._embedding = embedding
        self._media_processor = media_processor
        self._profiles = profiles
        self._versions = versions

    async def execute(
        self,
        security_scope_id: str,
        job_id: UUID,
        *,
        worker_id: str | None = None,
    ) -> IngestionJob:
        """执行一个已认领任务，并统一处理取消、成功、重试和最终失败。

        开始时将任务置为 RUNNING 并增加尝试次数；按任务类型分派。可重试异常且未到
        上限时进入 ``RETRY_WAIT`` 并计算下次时间，否则进入 ``FAILED``；取消信号进入
        ``CANCELLED``。每个状态变化后调用检查点，让外层 Runner 提交事务。
        """
        job = await self._jobs.get(security_scope_id, job_id)
        if job is None:
            raise ResourceNotFoundError("RAG-JOB-001", "Job 不存在")
        if job.status == JobStatus.PENDING:
            job.status = require_transition(
                job.status,
                JobStatus.RUNNING,
                JOB_TRANSITIONS,
            )
            job.attempt += 1
            job.started_at = utc_now()
            job.locked_by = worker_id
            if worker_id is not None:
                job.locked_until = utc_now() + timedelta(seconds=self._settings.lease_seconds)
            job.error_code = None
            job.error_message = None
            if not await self._jobs.update(security_scope_id, job):
                raise RagError(
                    "RAG-DB-002",
                    "Job 并发更新冲突",
                    http_status=409,
                    retryable=True,
                )
            await self._checkpoint()
        elif not (
            job.status == JobStatus.RUNNING and worker_id is not None and job.locked_by == worker_id
        ):
            raise RagError(
                "RAG-JOB-002",
                "Job 当前状态不可执行",
                http_status=409,
                retryable=False,
            )
        try:
            if job.job_type == JobType.UPLOAD:
                await self._execute_upload(job)
            elif job.job_type in {JobType.PARSE, JobType.REPARSE}:
                await self._execute_parse(job)
            elif job.job_type == JobType.UPDATE_DOCUMENT:
                await self._execute_update_document(job)
            elif job.job_type == JobType.DELETE_DOCUMENT:
                await self._execute_delete_document(job)
            elif job.job_type == JobType.DELETE_KB:
                await self._execute_delete_knowledge_base(job)
            else:
                raise RagError(
                    "RAG-JOB-002",
                    "Job 类型尚不可执行",
                    http_status=409,
                    retryable=False,
                )
            job.status = require_transition(
                job.status,
                JobStatus.SUCCEEDED,
                JOB_TRANSITIONS,
            )
            # 成功状态只保留本次结果；先前失败原因仍可在历史日志中追查。
            job.error_code = None
            job.error_message = None
            job.progress = 1.0
            job.finished_at = utc_now()
            job.locked_by = None
            job.locked_until = None
            await self._jobs.update(security_scope_id, job)
            await self._checkpoint()
            return job
        except JobLeaseLostError:
            # 所有权丢失不属于业务失败，交给 Runner 回滚，不覆盖新 Worker 的状态。
            raise
        except (_JobCancelled, JobCancellationRequested):
            refreshed = await self._jobs.get(security_scope_id, job.id)
            if refreshed is not None:
                job = refreshed
            if job.status == JobStatus.RUNNING:
                job.status = require_transition(
                    job.status,
                    JobStatus.CANCELLED,
                    JOB_TRANSITIONS,
                )
            elif job.status == JobStatus.CANCEL_REQUESTED:
                job.status = require_transition(
                    job.status,
                    JobStatus.CANCELLED,
                    JOB_TRANSITIONS,
                )
            job.finished_at = utc_now()
            job.locked_by = None
            job.locked_until = None
            await self._jobs.update(security_scope_id, job)
            await self._checkpoint()
            return job
        except Exception as exc:
            refreshed = await self._jobs.get(security_scope_id, job.id)
            if refreshed is not None:
                job = refreshed
            if job.status == JobStatus.CANCEL_REQUESTED:
                job.status = require_transition(
                    job.status,
                    JobStatus.CANCELLED,
                    JOB_TRANSITIONS,
                )
            elif isinstance(exc, RagError) and exc.retryable and job.attempt < job.max_attempts:
                job.status = require_transition(
                    job.status,
                    JobStatus.RETRY_WAIT,
                    JOB_TRANSITIONS,
                )
                job.next_retry_at = utc_now() + timedelta(seconds=min(60, 2 ** max(job.attempt, 1)))
            else:
                job.status = require_transition(
                    job.status,
                    JobStatus.FAILED,
                    JOB_TRANSITIONS,
                )
            if isinstance(exc, RagError):
                job.error_code = exc.code
                job.error_message = exc.message[:1024]
            else:
                job.error_code = "RAG-COMMON-011"
                job.error_message = "Worker 执行失败"
            job.finished_at = utc_now()
            job.locked_by = None
            job.locked_until = None
            await self._jobs.update(security_scope_id, job)
            await self._checkpoint()
            raise

    async def _document_for_job(self, job: IngestionJob) -> DocumentRecord:
        """读取任务关联文档，并把缺少 document_id/记录转换为明确领域错误。"""
        if job.document_id is None or job.knowledge_base_id is None:
            raise RagError(
                "RAG-DOC-011",
                "Job 缺少文档资源标识",
                http_status=409,
                retryable=True,
            )
        document = await self._documents.get(
            job.security_scope_id,
            job.document_id,
        )
        if document is None or document.knowledge_base_id != job.knowledge_base_id:
            raise ResourceNotFoundError("RAG-DOC-001", "文档不存在")
        return document

    async def _update_document(
        self,
        document: DocumentRecord,
        expected_version: int,
    ) -> None:
        """以乐观锁保存文档；版本不匹配说明并发操作，返回可重试冲突。"""
        if not await self._documents.update(
            document.security_scope_id,
            document,
            expected_version,
        ):
            raise RagError(
                "RAG-DB-002",
                "文档并发更新冲突",
                http_status=409,
                retryable=True,
            )
        await self._checkpoint()

    async def _tracked_primary_version(
        self,
        document: DocumentRecord,
    ) -> DocumentVersion | None:
        """读取主记录当前指向的版本；未启用版本仓库时兼容旧部署返回 None。

        初次上传在 HTTP 事务中已经创建 v1 并把 ``active_version_id`` 写入主记录，
        即使该版本尚未解析成 ACTIVE，也可以通过这个指针持续同步上传/解析状态。
        """
        if self._versions is None or document.active_version_id is None:
            return None
        version = await self._versions.get(
            document.security_scope_id,
            document.active_version_id,
        )
        if version is None or version.document_id != document.id:
            raise RagError(
                "RAG-DOC-015",
                "文档活动版本映射无效",
                http_status=409,
                retryable=True,
            )
        return version

    async def _execute_upload(self, job: IngestionJob) -> None:
        """重新校验暂存文件并上传 RAGFlow，按需串联一个解析任务。

        文件通过大小与 SHA-256 二次校验后才交给外部引擎。上传成功立即删除暂存文件；
        若 ``auto_parse`` 为真，则把文档置为 QUEUED 并新建 PARSE Job，而不是在当前
        Job 内继续长时间轮询。
        """
        document = await self._document_for_job(job)
        try:
            staged_file_id = UUID(str(job.payload["staged_file_id"]))
            safe_filename = str(job.payload["safe_filename"])
            size_bytes = int(job.payload["size_bytes"])
            content_sha256 = str(job.payload["content_sha256"])
            auto_parse = bool(job.payload.get("auto_parse", False))
        except (KeyError, TypeError, ValueError) as exc:
            raise RagError(
                "RAG-DOC-011",
                "上传 Job Payload 无效",
                http_status=409,
                retryable=True,
            ) from exc
        expected_version = document.row_version
        if document.status == DocumentStatus.FAILED:
            document.status = require_transition(
                document.status,
                DocumentStatus.RETRYING,
                DOCUMENT_TRANSITIONS,
            )
        if document.status in {DocumentStatus.RECEIVED, DocumentStatus.RETRYING}:
            document.status = require_transition(
                document.status,
                DocumentStatus.UPLOADING,
                DOCUMENT_TRANSITIONS,
            )
        if document.status != DocumentStatus.UPLOADING:
            raise RagError(
                "RAG-DOC-005",
                "文档当前状态不可上传",
                http_status=409,
                retryable=False,
            )
        await self._update_document(document, expected_version)
        try:
            (
                staged_file_id,
                safe_filename,
                size_bytes,
                content_sha256,
            ) = await self._prepare_media_if_needed(
                job,
                document,
                staged_file_id=staged_file_id,
                safe_filename=safe_filename,
                size_bytes=size_bytes,
                content_sha256=content_sha256,
            )
            async with self._staging.open(
                staged_file_id,
                expected_size=size_bytes,
                expected_sha256=content_sha256,
            ) as content:
                engine_document = await self._rag_engine.upload_document(
                    UploadDocumentCommand(
                        security=_worker_security_context(
                            job.security_scope_id,
                            job.id,
                        ),
                        knowledge_base_id=document.knowledge_base_id,
                        document_id=document.id,
                        # 转换只影响引擎副本，原图身份和摘要保持在本地文档记录中。
                        parser_profile_ref=document.parser_profile_ref,
                        filename=_engine_filename(document.id, safe_filename),
                        content=content,
                        metadata=document.metadata,
                        content_media_type=(
                            str(job.payload["content_media_type"])
                            if job.payload.get("media_preprocessed")
                            else None
                        ),
                        expected_size_bytes=(
                            size_bytes if job.payload.get("media_preprocessed") else None
                        ),
                        expected_sha256=(
                            content_sha256 if job.payload.get("media_preprocessed") else None
                        ),
                    )
                )
        except Exception as exc:
            await self._mark_document_failure(document, exc)
            raise
        expected_version = document.row_version
        document.ragflow_document_id = engine_document.engine_id
        document.status = require_transition(
            document.status,
            DocumentStatus.UPLOADED,
            DOCUMENT_TRANSITIONS,
        )
        document.progress = 0.25
        # 文档重试成功后必须清除上一次失败留下的错误快照。否则记录虽然已经进入
        # UPLOADED/READY，API 仍会把旧的 ``RAG-RF-004`` 返回给前端，造成“解析成功
        # 但仍显示协议无效”的矛盾状态。这里只在外部上传已经获得有效文档映射后清理，
        # 因而不会掩盖本次上传过程中真正发生的新错误。
        document.last_error_code = None
        document.last_error_message = None
        primary_version = await self._tracked_primary_version(document)
        if primary_version is not None and self._versions is not None:
            primary_version.ragflow_document_id = engine_document.engine_id
            primary_version.status = (
                DocumentVersionStatus.PARSING if auto_parse else DocumentVersionStatus.PENDING
            )
            primary_version.last_error_code = None
            primary_version.last_error_message = None
            if not await self._versions.update(document.security_scope_id, primary_version):
                raise RagError(
                    "RAG-DB-002",
                    "文档初始版本映射保存失败",
                    http_status=409,
                    retryable=True,
                )
        await self._update_document(document, expected_version)
        await self._staging.delete(staged_file_id)

        if auto_parse:
            expected_version = document.row_version
            document.status = require_transition(
                document.status,
                DocumentStatus.QUEUED,
                DOCUMENT_TRANSITIONS,
            )
            await self._update_document(document, expected_version)
            parse_job = IngestionJob(
                security_scope_id=job.security_scope_id,
                job_type=JobType.PARSE,
                knowledge_base_id=document.knowledge_base_id,
                document_id=document.id,
                max_attempts=job.max_attempts,
                payload={"schema_version": 1, "delete_existing": False},
            )
            await self._jobs.add(job.security_scope_id, parse_job)
            await self._checkpoint()
            job.result["parse_job_id"] = str(parse_job.id)

    async def _prepare_media_if_needed(
        self,
        job: IngestionJob,
        document: DocumentRecord,
        *,
        staged_file_id: UUID,
        safe_filename: str,
        size_bytes: int,
        content_sha256: str,
    ) -> tuple[UUID, str, int, str]:
        """把首次音视频上传转换为 Markdown，并把恢复点原子写入 Job Payload。

        云端 ASR/视觉调用只发生一次。派生文件先写成新的 ``.ready``，随后同时保存
        Job Payload 和文档处理 Metadata，提交成功后才删除原媒体暂存副本。Worker
        在这一检查点后崩溃或重试时会直接复用派生文件，不会重复计费或生成不同文本。
        """
        already_preprocessed = bool(job.payload.get("media_preprocessed", False))
        if already_preprocessed:
            return staged_file_id, safe_filename, size_bytes, content_sha256
        if self._media_processor is None:
            # 配置允许上传音视频但 Worker 未装配处理器时，不能把二进制媒体误交给
            # RAGFlow 的 Markdown Parser。明确失败可以让健康检查和运维立即发现部署
            # 漏配，同时普通 PDF、Office 和图片仍沿用原来的摄取路径。
            if Path(safe_filename).suffix.lower() in _MEDIA_EXTENSIONS:
                raise RagError(
                    "RAG-DOC-013",
                    "音视频处理能力未启用",
                    http_status=503,
                    retryable=True,
                )
            return staged_file_id, safe_filename, size_bytes, content_sha256
        if not self._media_processor.supports(safe_filename):
            return staged_file_id, safe_filename, size_bytes, content_sha256

        async with self._staging.open(
            staged_file_id,
            expected_size=size_bytes,
            expected_sha256=content_sha256,
        ) as content:
            prepared = await self._media_processor.prepare(
                content,
                filename=document.filename,
                mime_type=document.mime_type,
            )
        derived = await self._staging.save(
            _single_bytes_stream(prepared.content),
            StagedFileMetadata(
                original_filename=prepared.filename,
                media_type=prepared.mime_type,
                expected_size=len(prepared.content),
            ),
        )

        original_staged_file_id = staged_file_id
        job.payload.update(
            {
                "staged_file_id": str(derived.staged_file_id),
                "safe_filename": derived.safe_filename,
                "size_bytes": derived.size_bytes,
                "content_sha256": derived.content_sha256,
                "content_media_type": prepared.mime_type,
                "media_preprocessed": True,
                # 原始文件身份仍保留在 DocumentRecord；这里只有恢复/审计所需摘要，
                # 不保存正文、临时路径或云端响应原文。
                "original_content_sha256": document.content_sha256,
            }
        )
        expected_version = document.row_version
        document.metadata = {**document.metadata, **prepared.metadata}
        await self._update_document(document, expected_version)
        if not await self._jobs.update(job.security_scope_id, job):
            raise RagError(
                "RAG-DB-002",
                "音视频处理检查点保存失败",
                http_status=409,
                retryable=True,
            )
        await self._checkpoint()
        await self._staging.delete(original_staged_file_id)
        return (
            derived.staged_file_id,
            derived.safe_filename,
            derived.size_bytes,
            derived.content_sha256,
        )

    async def _prepare_version_media_if_needed(
        self,
        job: IngestionJob,
        version: DocumentVersion,
        *,
        staged_file_id: UUID,
        safe_filename: str,
        size_bytes: int,
        content_sha256: str,
    ) -> tuple[UUID, str, int, str]:
        """把候选音视频版本转换为可交给 RAGFlow 的 Markdown 恢复点。

        ``DocumentVersion`` 始终记录用户上传的原文件身份，便于版本历史准确展示和
        完整文件去重；RAGFlow 物理候选则接收 ASR/视觉服务产生的 Markdown。派生
        文件的名称、大小、摘要和媒体类型只保存在 Job Payload 中，Worker 重试时
        可以复用同一份结果，避免重复调用按量计费模型。只有 Payload、候选 Metadata
        和派生文件恢复点一起提交成功后，才删除原媒体暂存副本。
        """
        if bool(job.payload.get("media_preprocessed", False)):
            return staged_file_id, safe_filename, size_bytes, content_sha256
        if self._media_processor is None:
            if Path(safe_filename).suffix.lower() in _MEDIA_EXTENSIONS:
                raise RagError(
                    "RAG-DOC-013",
                    "音视频处理能力未启用",
                    http_status=503,
                    retryable=True,
                )
            return staged_file_id, safe_filename, size_bytes, content_sha256
        if not self._media_processor.supports(safe_filename):
            return staged_file_id, safe_filename, size_bytes, content_sha256

        async with self._staging.open(
            staged_file_id,
            expected_size=size_bytes,
            expected_sha256=content_sha256,
        ) as content:
            prepared = await self._media_processor.prepare(
                content,
                filename=version.filename,
                mime_type=version.mime_type,
            )
        derived = await self._staging.save(
            _single_bytes_stream(prepared.content),
            StagedFileMetadata(
                original_filename=prepared.filename,
                media_type=prepared.mime_type,
                expected_size=len(prepared.content),
            ),
        )

        original_staged_file_id = staged_file_id
        job.payload.update(
            {
                "staged_file_id": str(derived.staged_file_id),
                "safe_filename": derived.safe_filename,
                "size_bytes": derived.size_bytes,
                "content_sha256": derived.content_sha256,
                "content_media_type": prepared.mime_type,
                "media_preprocessed": True,
                "original_content_sha256": version.content_sha256,
            }
        )
        version.metadata = {**version.metadata, **prepared.metadata}
        if self._versions is None or not await self._versions.update(
            job.security_scope_id,
            version,
        ):
            await self._staging.delete(derived.staged_file_id)
            raise RagError(
                "RAG-DB-002",
                "候选音视频版本检查点保存失败",
                http_status=409,
                retryable=True,
            )
        if not await self._jobs.update(job.security_scope_id, job):
            await self._staging.delete(derived.staged_file_id)
            raise RagError(
                "RAG-DB-002",
                "候选音视频任务检查点保存失败",
                http_status=409,
                retryable=True,
            )
        await self._checkpoint()
        await self._staging.delete(original_staged_file_id)
        return (
            derived.staged_file_id,
            derived.safe_filename,
            derived.size_bytes,
            derived.content_sha256,
        )

    async def _execute_parse(self, job: IngestionJob) -> None:
        """启动或接管 RAGFlow 解析，指数退避轮询直至完成、失败、取消或超时。

        进程重启后会先读取引擎状态，若解析已在运行则不重复 start；每轮同步进度、
        块数和 Token 数。文档与 Job 进度在同一检查点提交，使文档列表和任务查询
        都能在解析结束前看到 RAGFlow 的真实阶段进度；完成时增加知识库 revision
        使旧答案缓存失效。
        """
        document = await self._document_for_job(job)
        if document.status == DocumentStatus.READY:
            job.result.update(
                {
                    "chunk_count": document.chunk_count,
                    "token_count": document.token_count,
                }
            )
            return
        if not document.ragflow_document_id:
            raise RagError(
                "RAG-DOC-011",
                "文档缺少 RAGFlow 映射",
                http_status=409,
                retryable=True,
            )
        expected_version = document.row_version
        if document.status == DocumentStatus.FAILED:
            document.status = require_transition(
                document.status,
                DocumentStatus.RETRYING,
                DOCUMENT_TRANSITIONS,
            )
        if document.status in {DocumentStatus.QUEUED, DocumentStatus.RETRYING}:
            document.status = require_transition(
                document.status,
                DocumentStatus.PARSING,
                DOCUMENT_TRANSITIONS,
            )
        if document.status not in {DocumentStatus.PARSING, DocumentStatus.INDEXING}:
            raise RagError(
                "RAG-DOC-005",
                "文档当前状态不可解析",
                http_status=409,
                retryable=False,
            )
        # 上传阶段占整个文档流水线的前 25%。进入解析后先同时保存文档和 Job 的
        # 起始进度；Job 更新放在文档检查点之前，使外层 SQLAlchemy Session 一次
        # commit 两条记录，避免任务接口仍显示 0 而文档接口已经显示解析中。
        document.progress = max(document.progress, 0.25)
        job.progress = max(job.progress, document.progress)
        if not await self._jobs.update(job.security_scope_id, job):
            raise RagError(
                "RAG-DB-002",
                "Job 解析进度保存失败",
                http_status=409,
                retryable=True,
            )
        await self._update_document(document, expected_version)
        security = _worker_security_context(job.security_scope_id, job.id)
        current_engine_status = await self._rag_engine.get_document(
            GetEngineDocumentQuery(
                security=security,
                knowledge_base_id=document.knowledge_base_id,
                document_id=document.id,
            )
        )
        engine_status: EngineDocumentStatus | None = current_engine_status
        engine_run_state = current_engine_status.run_state.upper()
        parse_already_started = engine_run_state in {"RUNNING", "PARSING", "DONE", "1", "3"}
        if job.job_type == JobType.REPARSE or not parse_already_started:
            # 自动选择的 Chunk Profile 只有通过文档级官方配置接口写入 RAGFlow 后才算
            # 真正生效。配置目录中的版本不可变，所以任务重试可以安全重新发送相同值；
            # Adapter 会先 PATCH 文档再启动解析。未注入目录表示兼容旧 Worker，此时
            # 两个可选字段保持空值并沿用数据集配置，不会猜测或伪造参数。
            document_parser_configuration = (
                self._profiles.parser_configuration(
                    parser_ref=document.parser_profile_ref,
                    chunk_ref=document.chunk_profile_ref,
                )
                if self._profiles is not None
                else None
            )
            try:
                await self._rag_engine.start_parse(
                    ParseDocumentCommand(
                        security=security,
                        knowledge_base_id=document.knowledge_base_id,
                        document_id=document.id,
                        delete_existing=bool(job.payload.get("delete_existing", False)),
                        chunk_method=(
                            document_parser_configuration.chunk_method
                            if document_parser_configuration is not None
                            else None
                        ),
                        parser_config=(
                            document_parser_configuration.parser_config
                            if document_parser_configuration is not None
                            else None
                        ),
                    )
                )
            except Exception as exc:
                # 配置 PATCH 或启动解析失败发生在轮询之前；若这里只失败 Job，文档会
                # 永久停在 PARSING，标准重试接口也无法接管。与轮询失败/超时保持一致，
                # 先把文档和初始版本收敛为 FAILED，再交给外层统一处理 Job 状态。
                await self._mark_document_failure(document, exc)
                raise
            engine_status = None

        # 使用单调时钟计算 Deadline，不受系统时间校准或时区变化影响。
        deadline = self._monotonic() + self._settings.parse_deadline_seconds
        delay = float(self._settings.parse_poll_initial_seconds)
        while self._monotonic() < deadline:
            # 每轮重新读任务，才能及时响应 API 写入的 CANCEL_REQUESTED。
            refreshed_job = await self._jobs.get(job.security_scope_id, job.id)
            if refreshed_job is not None and refreshed_job.status == JobStatus.CANCEL_REQUESTED:
                await self._rag_engine.stop_parse(
                    StopParseCommand(
                        security=security,
                        knowledge_base_id=document.knowledge_base_id,
                        document_id=document.id,
                    )
                )
                if not bool(refreshed_job.payload.get("delete_after_cancel", False)):
                    await self._mark_document_failure(
                        document,
                        RagError(
                            "RAG-JOB-007",
                            "解析已取消",
                            http_status=409,
                            retryable=False,
                        ),
                    )
                raise _JobCancelled

            if engine_status is None:
                engine_status = await self._rag_engine.get_document(
                    GetEngineDocumentQuery(
                        security=security,
                        knowledge_base_id=document.knowledge_base_id,
                        document_id=document.id,
                    )
                )
            run_state = engine_status.run_state.upper()
            expected_version = document.row_version
            # RAGFlow 按 OCR、版面分析、Embedding 和索引阶段更新百分比，期间偶尔
            # 可能短暂返回较低值。取历史值、引擎值和解析起点的最大值，保证对外
            # 进度只前进不倒退；99% 为 Gateway 后续 Metadata/侧车索引保留，只有
            # 所有步骤都成功后 execute() 才会把 Job 和文档置为 100%。
            document.progress = min(
                max(document.progress, engine_status.progress, 0.25),
                0.99,
            )
            document.chunk_count = engine_status.chunk_count
            document.token_count = engine_status.token_count
            document.engine_progress_message = run_state
            job.progress = min(max(job.progress, document.progress), 0.99)
            if not await self._jobs.update(job.security_scope_id, job):
                raise RagError(
                    "RAG-DB-002",
                    "Job 解析进度保存失败",
                    http_status=409,
                    retryable=True,
                )
            # 无论引擎处于运行、完成还是失败状态，都先提交本轮真实进度。DONE 后的
            # Metadata、层级索引和实体索引可能继续耗时，此检查点让客户端在这段时间
            # 看到 99% 而不是停留在旧值，且 Job/Document 两个接口保持一致。
            await self._update_document(document, expected_version)
            expected_version = document.row_version
            if run_state in {"DONE", "3"}:
                # 引擎可能在解析器缺失后仍返回 DONE；零切块不能标记可检索成功。
                if engine_status.chunk_count <= 0:
                    error = RagError(
                        "RAG-DOC-008",
                        "解析未生成任何切块，请检查解析服务配置后重试",
                        http_status=502,
                        retryable=False,
                    )
                    await self._mark_document_failure(document, error)
                    raise error
                # RAGFlow 已经生成最终 Chunk 后再做 Metadata 补全，才能基于真实正文
                # 判断语种、页数和表格迹象。补全仅增强后续语言路由和 Metadata Filter，
                # 即使抽样或同步失败也不能把已经成功的文档重新标成解析失败。
                await self._complete_document_metadata(security, document)
                await self._rebuild_hierarchical_index(security, document)
                await self._rebuild_entity_graph(security, document)
                document.status = require_transition(
                    document.status,
                    DocumentStatus.READY,
                    DOCUMENT_TRANSITIONS,
                )
                document.progress = 1.0
                document.indexed_at = utc_now()
                primary_version = await self._tracked_primary_version(document)
                if primary_version is not None and self._versions is not None:
                    primary_version.status = DocumentVersionStatus.INDEXING
                    primary_version.chunk_count = document.chunk_count
                    primary_version.token_count = document.token_count
                    primary_version.metadata = dict(document.metadata)
                    if not await self._versions.update(
                        document.security_scope_id,
                        primary_version,
                    ):
                        raise RagError(
                            "RAG-DB-002",
                            "文档初始版本统计保存失败",
                            http_status=409,
                            retryable=True,
                        )
                    activated_at = document.indexed_at or utc_now()
                    if not await self._versions.activate(
                        document.security_scope_id,
                        document_id=document.id,
                        version_id=primary_version.id,
                        activated_at=activated_at,
                    ):
                        raise RagError(
                            "RAG-DB-002",
                            "文档初始版本激活失败",
                            http_status=409,
                            retryable=True,
                        )
                await self._update_document(document, expected_version)
                await self._increment_knowledge_base_revision(
                    job.security_scope_id,
                    document.knowledge_base_id,
                )
                job.result.update(
                    {
                        "chunk_count": document.chunk_count,
                        "token_count": document.token_count,
                    }
                )
                return
            if run_state in {"FAIL", "FAILED", "CANCEL", "CANCELLED", "2", "4"}:
                error = RagError(
                    "RAG-DOC-008",
                    "RAGFlow 文档解析失败",
                    http_status=502,
                    retryable=True,
                )
                await self._mark_document_failure(document, error)
                raise error
            engine_status = None
            await self._sleep(delay)
            # 指数退避降低长文档对 RAGFlow 的轮询压力，同时受最大间隔限制。
            delay = min(delay * 2, float(self._settings.parse_poll_max_seconds))

        error = RagError(
            "RAG-DOC-009",
            "RAGFlow 文档解析超时",
            http_status=504,
            retryable=True,
        )
        await self._mark_document_failure(document, error)
        raise error

    async def _complete_document_metadata(
        self,
        security: SecurityContext,
        document: DocumentRecord,
    ) -> None:
        """从解析后的真实 Chunk 补全安全、可解释的文档 Metadata。

        最多读取前 100 个块。语言、页码与表格识别只看前 50,000 字符；确定性实体、
        日期、版本和文档类型最多看前 200,000 字符。用户已经提供的业务字段绝不
        覆盖，系统状态字段则按本次解析结果更新。补全结果随后通过官方文档更新接口
        同步给 RAGFlow，使 Metadata Filter 与 App PostgreSQL 中看到的值保持一致。
        """
        try:
            page = await self._rag_engine.list_chunks(
                ListEngineChunksQuery(
                    security=security,
                    knowledge_base_id=document.knowledge_base_id,
                    document_id=document.id,
                    page=1,
                    page_size=min(max(document.chunk_count, 1), 100),
                )
            )
            chunks = tuple(item for item in page.items if isinstance(item, EngineChunk))
        except Exception:
            # Metadata 是增强索引，不是文档正文的唯一事实源。下游暂时不可读时记录
            # 绕过状态，保留解析成功结果，后续重解析或索引维护任务可再次补全。
            document.metadata = {
                **document.metadata,
                "lazytt_metadata_version": 1,
                "lazytt_metadata_status": "bypassed",
            }
            return

        sample = "\n".join(item.content for item in chunks)[:50_000]
        pages = {
            position.page
            for item in chunks
            for position in item.positions
            if position.page is not None
        }
        completed = dict(document.metadata)
        completed.setdefault("title", Path(document.filename).stem)
        completed.setdefault("file_type", Path(document.filename).suffix.lower().lstrip("."))
        if "language" not in completed and "lang" not in completed:
            detected_language = _detect_document_language(sample)
            if detected_language is not None:
                completed["language"] = detected_language
        structured_metadata = extract_structured_metadata(document, chunks)
        for key, value in structured_metadata.items():
            if key.startswith("lazytt_"):
                # ``lazytt_*`` 是系统拥有的状态/版本字段，重解析必须反映最新算法结果；
                # 普通业务字段使用 setdefault，始终尊重用户在上传时显式提供的值。
                completed[key] = value
            else:
                completed.setdefault(key, value)
        completed.update(
            {
                "lazytt_metadata_version": 2,
                "lazytt_metadata_status": "completed",
                "lazytt_page_count": max(pages) if pages else 0,
                "lazytt_has_table": _sample_contains_table(sample, chunks),
            }
        )
        document.metadata = completed
        try:
            await self._rag_engine.update_document(
                UpdateEngineDocumentCommand(
                    security=security,
                    knowledge_base_id=document.knowledge_base_id,
                    document_id=document.id,
                    metadata=completed,
                )
            )
        except Exception:
            # 本地 Metadata 仍然可供语言路由和管理页面使用；状态明确写出同步失败，
            # 不伪装成 RAGFlow 过滤字段已经更新，也不破坏核心解析成功状态。
            document.metadata["lazytt_metadata_status"] = "local_only"

    async def _rebuild_hierarchical_index(
        self,
        security: SecurityContext,
        document: DocumentRecord,
    ) -> None:
        """重解析完成后重建文档/章节/Parent 多粒度节点，并尽力生成同版本向量。

        节点构建和数据库替换位于当前 Worker 事务中，只有全部新节点写入成功才会与
        READY 状态一起提交。Embedding 暂时不可用时仍保存节点到真实 Chunk 的映射，
        并把向量状态标为降级；后续重解析即可自动补齐，不让质量增强故障把已经成功
        的原始索引变成用户可见上传失败。
        """
        if self._index_nodes is None or self._embedding is None:
            return
        chunks = await self._load_index_chunks(security, document, max_chunks=1000)
        nodes = build_hierarchical_nodes(
            document,
            chunks,
            max_chunks=1000,
            total_chunk_count=document.chunk_count,
        )
        if not nodes:
            document.metadata["lazytt_hierarchical_index"] = "empty"
            document.metadata["lazytt_hierarchy_version"] = 3
            document.metadata["lazytt_context_prefix_version"] = 1
            document.metadata["lazytt_parent_strategy"] = "heading-length-v3"
            document.metadata["lazytt_parent_count"] = 0
            document.metadata["lazytt_table_parent_count"] = 0
            document.metadata["lazytt_section_count"] = 0
            document.metadata["lazytt_summary_count"] = 0
            await self._index_nodes.replace_document_nodes(
                document.security_scope_id,
                document.id,
                (),
            )
            return
        vectors: list[tuple[float, ...]] = []
        try:
            # 云端 OpenAI-compatible Embedding 服务对单批条数通常有限制；每批 32
            # 个节点可控制请求体和内存峰值，同时保持节点顺序可严格映射回结果。
            for offset in range(0, len(nodes), 32):
                batch = nodes[offset : offset + 32]
                vectors.extend(await self._embedding.embed(tuple(node.content for node in batch)))
            nodes = attach_embeddings(nodes, tuple(vectors))
            document.metadata["lazytt_hierarchical_index"] = "ready"
        except Exception:
            document.metadata["lazytt_hierarchical_index"] = "parent_only"
        # v3 表示节点已采用“标题 + 长度”动态 Parent、表格行组和
        # 仅供索引使用的上下文前缀。这些 Metadata 让管理页和验收脚本
        # 能够确认新策略确实生效，而不是只看到文档最终变成 READY。
        document.metadata["lazytt_hierarchy_version"] = 3
        document.metadata["lazytt_context_prefix_version"] = 1
        document.metadata["lazytt_parent_strategy"] = "heading-length-v3"
        document.metadata["lazytt_parent_count"] = sum(
            node.node_type.value == "PARENT" for node in nodes
        )
        document.metadata["lazytt_table_parent_count"] = sum(
            node.node_type.value == "PARENT" and node.metadata.get("segment_kind") == "table"
            for node in nodes
        )
        document.metadata["lazytt_section_count"] = sum(
            node.node_type.value == "SECTION" for node in nodes
        )
        document.metadata["lazytt_summary_count"] = sum(
            node.node_type.value == "SUMMARY" for node in nodes
        )
        await self._index_nodes.replace_document_nodes(
            document.security_scope_id,
            document.id,
            nodes,
        )

    async def _load_index_chunks(
        self,
        security: SecurityContext,
        document: DocumentRecord,
        *,
        max_chunks: int,
    ) -> tuple[EngineChunk, ...]:
        """分页读取最多指定数量的原始 RAGFlow Chunk，顺序用于构造连续 Parent。"""
        values: list[EngineChunk] = []
        page_number = 1
        page_size = min(100, max_chunks)
        while len(values) < max_chunks:
            page = await self._rag_engine.list_chunks(
                ListEngineChunksQuery(
                    security=security,
                    knowledge_base_id=document.knowledge_base_id,
                    document_id=document.id,
                    page=page_number,
                    page_size=min(page_size, max_chunks - len(values)),
                )
            )
            current = tuple(item for item in page.items if isinstance(item, EngineChunk))
            values.extend(current)
            if not current or len(values) >= page.total:
                break
            page_number += 1
        return tuple(values[:max_chunks])

    async def _rebuild_entity_graph(
        self,
        security: SecurityContext,
        document: DocumentRecord,
    ) -> None:
        """从解析完成的原始 Chunk 重建实体和显式关系侧车索引。

        当前规则抽取不调用 LLM，不会猜测隐含事实。最多读取前 1,000 块以限制超大
        文档的 Worker 时间；RAGFlow 原始全文索引不受该保护上限影响。空结果也会
        原子替换旧图，防止重解析后继续召回已经从新版本文档中删除的实体。
        """
        if self._entity_graph is None:
            return
        chunks = await self._load_index_chunks(security, document, max_chunks=1000)
        entities, relations = build_lightweight_graph(document, chunks)
        await self._entity_graph.replace_document_graph(
            document.security_scope_id,
            document.id,
            entities,
            relations,
        )
        document.metadata["lazytt_entity_graph"] = "ready" if entities else "empty"
        document.metadata["lazytt_entity_count"] = len(entities)
        document.metadata["lazytt_relation_count"] = len(relations)

    async def _load_version_chunks(
        self,
        security: SecurityContext,
        document: DocumentRecord,
        version: DocumentVersion,
    ) -> tuple[EngineChunk, ...]:
        """分页读取候选物理版本全部 Chunk，供验证、侧车构建和可用性切换。

        版本更新必须掌握完整 Chunk ID 集合，才能在发布时停用旧版并在失败时补偿。
        每次只请求 100 条，避免超大文档形成单个巨型 HTTP 响应；结果只在单个 Worker
        任务内存中短暂存在，不写日志、不复制正文到 PostgreSQL 版本表。
        """
        if not version.ragflow_document_id:
            raise RagError(
                "RAG-DOC-015",
                "文档版本缺少 RAGFlow 映射",
                http_status=409,
                retryable=True,
            )
        values: list[EngineChunk] = []
        page_number = 1
        while True:
            page = await self._rag_engine.list_document_version_chunks(
                ListEngineVersionChunksQuery(
                    security=security,
                    knowledge_base_id=document.knowledge_base_id,
                    document_id=document.id,
                    engine_document_id=version.ragflow_document_id,
                    page=page_number,
                    page_size=100,
                )
            )
            current = tuple(item for item in page.items if isinstance(item, EngineChunk))
            values.extend(current)
            if not current or len(values) >= page.total:
                break
            page_number += 1
        return tuple(values)

    async def _complete_version_metadata(
        self,
        security: SecurityContext,
        document: DocumentRecord,
        version: DocumentVersion,
        chunks: tuple[EngineChunk, ...],
    ) -> DocumentRecord:
        """根据候选真实 Chunk 生成发布后的文档视图，并同步物理版本 Metadata。

        返回的是 ``dataclasses.replace`` 创建的临时对象，原 ``DocumentRecord`` 在
        激活事务前不会被修改。用户字段来自版本快照，``lazytt_*`` 字段重新计算；
        RAGFlow Metadata 同步失败只标记 ``local_only``，不把已经成功的解析判为失败。
        """
        candidate = replace(
            document,
            filename=version.filename,
            safe_filename=version.safe_filename,
            mime_type=version.mime_type,
            file_size_bytes=version.file_size_bytes,
            content_sha256=version.content_sha256,
            parser_profile_ref=version.parser_profile_ref,
            chunk_profile_ref=version.chunk_profile_ref,
            metadata=dict(version.metadata),
            ragflow_document_id=version.ragflow_document_id,
            chunk_count=version.chunk_count,
            token_count=version.token_count,
        )
        sample_chunks = chunks[:100]
        sample = "\n".join(item.content for item in sample_chunks)[:50_000]
        pages = {
            position.page
            for item in sample_chunks
            for position in item.positions
            if position.page is not None
        }
        completed = dict(candidate.metadata)
        completed.setdefault("title", Path(candidate.filename).stem)
        completed.setdefault("file_type", Path(candidate.filename).suffix.lower().lstrip("."))
        if "language" not in completed and "lang" not in completed:
            detected_language = _detect_document_language(sample)
            if detected_language is not None:
                completed["language"] = detected_language
        for key, value in extract_structured_metadata(candidate, sample_chunks).items():
            if key.startswith("lazytt_"):
                completed[key] = value
            else:
                completed.setdefault(key, value)
        completed.update(
            {
                "lazytt_metadata_version": 2,
                "lazytt_metadata_status": "completed",
                "lazytt_page_count": max(pages) if pages else 0,
                "lazytt_has_table": _sample_contains_table(sample, sample_chunks),
            }
        )
        candidate.metadata = completed
        try:
            await self._rag_engine.update_document_version(
                UpdateEngineDocumentVersionCommand(
                    security=security,
                    knowledge_base_id=document.knowledge_base_id,
                    document_id=document.id,
                    engine_document_id=version.ragflow_document_id or "",
                    metadata=completed,
                )
            )
        except Exception:
            candidate.metadata["lazytt_metadata_status"] = "local_only"
        return candidate

    async def _replace_version_sidecars(
        self,
        candidate: DocumentRecord,
        chunks: tuple[EngineChunk, ...],
    ) -> None:
        """在当前发布事务中用候选 Chunk 原子替换层级索引和实体图。

        节点只取前 1,000 个 Chunk 以保持原有资源保护上限，Metadata 会标明部分覆盖；
        RAGFlow 基础全文索引和版本可用性切换仍使用全部 Chunk。Embedding 失败时保留
        Parent 到真实 Chunk 的映射并明确降级，不阻止正文版本发布。
        """
        limited_chunks = chunks[:1000]
        if self._index_nodes is not None and self._embedding is not None:
            nodes = build_hierarchical_nodes(
                candidate,
                limited_chunks,
                max_chunks=1000,
                total_chunk_count=len(chunks),
            )
            if nodes:
                vectors: list[tuple[float, ...]] = []
                try:
                    for offset in range(0, len(nodes), 32):
                        batch = nodes[offset : offset + 32]
                        vectors.extend(
                            await self._embedding.embed(tuple(node.content for node in batch))
                        )
                    nodes = attach_embeddings(nodes, tuple(vectors))
                    candidate.metadata["lazytt_hierarchical_index"] = "ready"
                except Exception:
                    candidate.metadata["lazytt_hierarchical_index"] = "parent_only"
            else:
                candidate.metadata["lazytt_hierarchical_index"] = "empty"
            candidate.metadata["lazytt_hierarchy_version"] = 3
            candidate.metadata["lazytt_context_prefix_version"] = 1
            candidate.metadata["lazytt_parent_strategy"] = "heading-length-v3"
            candidate.metadata["lazytt_parent_count"] = sum(
                node.node_type.value == "PARENT" for node in nodes
            )
            candidate.metadata["lazytt_table_parent_count"] = sum(
                node.node_type.value == "PARENT" and node.metadata.get("segment_kind") == "table"
                for node in nodes
            )
            candidate.metadata["lazytt_section_count"] = sum(
                node.node_type.value == "SECTION" for node in nodes
            )
            candidate.metadata["lazytt_summary_count"] = sum(
                node.node_type.value == "SUMMARY" for node in nodes
            )
            await self._index_nodes.replace_document_nodes(
                candidate.security_scope_id,
                candidate.id,
                nodes,
            )
        if self._entity_graph is not None:
            entities, relations = build_lightweight_graph(candidate, limited_chunks)
            await self._entity_graph.replace_document_graph(
                candidate.security_scope_id,
                candidate.id,
                entities,
                relations,
            )
            candidate.metadata["lazytt_entity_graph"] = "ready" if entities else "empty"
            candidate.metadata["lazytt_entity_count"] = len(entities)
            candidate.metadata["lazytt_relation_count"] = len(relations)

    async def _set_version_available(
        self,
        security: SecurityContext,
        document: DocumentRecord,
        version: DocumentVersion,
        *,
        available: bool,
    ) -> None:
        """切换完整物理文档及其全部 Chunk，并保留本地 Scope 校验。

        版本发布不再只改 Chunk：RAGFlow 的文档 ``status`` 也必须同步变化，管理界面
        才会把退役文件显示为关闭。该外部动作不在 PostgreSQL 事务内，调用者必须在
        本地提交失败时发送相反命令完成 Saga 补偿。
        """
        if not version.ragflow_document_id:
            return
        await self._rag_engine.set_document_version_available(
            SetEngineDocumentVersionAvailabilityCommand(
                security=security,
                knowledge_base_id=document.knowledge_base_id,
                document_id=document.id,
                engine_document_id=version.ragflow_document_id,
                available=available,
            )
        )

    async def _execute_update_document(self, job: IngestionJob) -> None:
        """上传/解析候选版本或回滚退役版本，并原子发布到稳定逻辑文档。

        发布前所有状态检查点只写 ``DocumentVersion``，主文档一直保持 READY。候选
        Chunk 与侧车索引准备完毕后，Worker 先启用目标物理文档、停用旧物理文档，
        再在单一数据库事务中切换版本、主文档和知识库 Revision；数据库失败会反向
        恢复完整文档状态。该流程不修改 RAGFlow 源码，也不通过数据库伪造状态。
        """
        if self._versions is None or self._profiles is None:
            raise RagError(
                "RAG-DOC-015",
                "文档版本更新能力未启用",
                http_status=503,
                retryable=True,
            )
        document = await self._document_for_job(job)
        if document.status != DocumentStatus.READY:
            raise RagError(
                "RAG-DOC-016",
                "文档更新期间主文档必须保持可用",
                http_status=409,
                retryable=True,
            )
        try:
            version_id = UUID(str(job.payload["version_id"]))
            operation = str(job.payload.get("operation", "publish"))
        except (KeyError, TypeError, ValueError) as exc:
            raise RagError(
                "RAG-DOC-015",
                "文档更新 Job Payload 无效",
                http_status=409,
                retryable=True,
            ) from exc
        version = await self._versions.get(job.security_scope_id, version_id)
        if version is None or version.document_id != document.id:
            raise ResourceNotFoundError("RAG-DOC-017", "文档版本不存在")
        if version.status == DocumentVersionStatus.ACTIVE:
            job.result.update(
                {
                    "version_id": str(version.id),
                    "version_number": version.version_number,
                    "document_revision": document.document_revision,
                    "effective_mode": version.effective_mode.value,
                }
            )
            return

        security = _worker_security_context(job.security_scope_id, job.id)
        try:
            if operation == "publish":
                await self._prepare_published_version(job, document, version, security)
            elif operation == "rollback":
                if version.status != DocumentVersionStatus.RETIRED:
                    raise RagError(
                        "RAG-DOC-018",
                        "目标版本当前不可回滚",
                        http_status=409,
                        retryable=False,
                    )
            else:
                raise RagError(
                    "RAG-DOC-015",
                    "文档更新操作无效",
                    http_status=409,
                    retryable=False,
                )

            target_chunks = await self._load_version_chunks(security, document, version)
            if not target_chunks:
                raise RagError(
                    "RAG-DOC-008",
                    "候选版本没有生成可发布的 Chunk",
                    http_status=502,
                    retryable=True,
                )
            # RAGFlow 完成解析后候选物理文档默认启用，但稳定 DocumentRecord 仍映射旧版。
            # 立即关闭候选整份文件可避免侧车索引构建期间它抢占 Dataset Top-K，也让
            # RAGFlow 管理界面准确显示候选尚未发布；真正发布前会再次打开。
            await self._set_version_available(
                security,
                document,
                version,
                available=False,
            )
            version.chunk_count = len(target_chunks)
            candidate = await self._complete_version_metadata(
                security,
                document,
                version,
                target_chunks,
            )
            await self._replace_version_sidecars(candidate, target_chunks)
            version.metadata = dict(candidate.metadata)
            version.status = DocumentVersionStatus.INDEXING
            version.last_error_code = None
            version.last_error_message = None
            if not await self._versions.update(job.security_scope_id, version):
                raise RagError(
                    "RAG-DB-002",
                    "候选版本索引状态保存失败",
                    http_status=409,
                    retryable=True,
                )

            active_version = await self._versions.get_active(
                job.security_scope_id,
                document.id,
            )
            # 回滚目标过去已被关闭，需要先启用；新版本在侧车构建前也已被主动关闭。
            # 文档级操作会同步该物理文件所有 Chunk，无需再次读取旧版本 Chunk ID。
            await self._set_version_available(
                security,
                document,
                version,
                available=True,
            )
            if (
                active_version is not None
                and active_version.id != version.id
                and active_version.ragflow_document_id
            ):
                await self._set_version_available(
                    security,
                    document,
                    active_version,
                    available=False,
                )
            try:
                activated_at = utc_now()
                if not await self._versions.activate(
                    job.security_scope_id,
                    document_id=document.id,
                    version_id=version.id,
                    activated_at=activated_at,
                ):
                    raise RagError(
                        "RAG-DB-002",
                        "候选版本并发激活失败",
                        http_status=409,
                        retryable=True,
                    )
                expected_row_version = document.row_version
                document.ragflow_document_id = version.ragflow_document_id
                document.active_version_id = version.id
                document.active_version_number = version.version_number
                document.document_revision += 1
                document.filename = version.filename
                document.safe_filename = version.safe_filename
                document.mime_type = version.mime_type
                document.file_size_bytes = version.file_size_bytes
                document.content_sha256 = version.content_sha256
                document.metadata = dict(candidate.metadata)
                document.parser_profile_ref = version.parser_profile_ref
                document.chunk_profile_ref = version.chunk_profile_ref
                document.chunk_count = version.chunk_count
                document.token_count = version.token_count
                document.progress = 1.0
                document.indexed_at = activated_at
                document.last_error_code = None
                document.last_error_message = None
                if not await self._documents.update(
                    job.security_scope_id,
                    document,
                    expected_row_version,
                ):
                    raise RagError(
                        "RAG-DB-002",
                        "文档版本发布并发冲突",
                        http_status=409,
                        retryable=True,
                    )
                # Revision 更新是这个发布事务最后一步；它内部的 checkpoint 一次提交
                # 版本、主记录、侧车索引和缓存边界，形成原子可见切换。
                await self._increment_knowledge_base_revision(
                    job.security_scope_id,
                    document.knowledge_base_id,
                )
            except Exception:
                # RAGFlow 文档状态不参与 PostgreSQL 事务，提交失败必须重新打开旧版并
                # 关闭候选；两次操作都会同步整份文档的 Chunk 和管理界面开关。
                if (
                    active_version is not None
                    and active_version.id != version.id
                    and active_version.ragflow_document_id
                ):
                    await self._set_version_available(
                        security,
                        document,
                        active_version,
                        available=True,
                    )
                await self._set_version_available(
                    security,
                    document,
                    version,
                    available=False,
                )
                raise
            job.result.update(
                {
                    "version_id": str(version.id),
                    "version_number": version.version_number,
                    "document_revision": document.document_revision,
                    "chunk_count": version.chunk_count,
                    "token_count": version.token_count,
                    "effective_mode": version.effective_mode.value,
                }
            )
        except Exception as exc:
            # 可重试故障保留当前进度和物理映射，下一次 Job 可从解析状态继续；只有
            # 不可重试或次数耗尽才把候选标记 FAILED。主文档始终不写失败状态。
            # 发布阶段可能已经在当前事务写入候选侧车和版本状态。先回滚这些未提交
            # 写入，再单独记录候选错误，避免旧主文档仍 ACTIVE 时侧车却提前变成新版。
            await self._rollback()
            retryable = (
                isinstance(exc, RagError) and exc.retryable and job.attempt < job.max_attempts
            )
            if not retryable:
                version.status = DocumentVersionStatus.FAILED
            if isinstance(exc, RagError):
                version.last_error_code = exc.code
                version.last_error_message = exc.message[:1024]
            else:
                version.last_error_code = "RAG-COMMON-011"
                version.last_error_message = "文档版本更新失败"
            await self._versions.update(job.security_scope_id, version)
            await self._checkpoint()
            raise

    async def _prepare_published_version(
        self,
        job: IngestionJob,
        document: DocumentRecord,
        version: DocumentVersion,
        security: SecurityContext,
    ) -> None:
        """幂等上传候选文件、应用 Profile 并轮询到 RAGFlow 解析完成。

        上传成功后删除暂存文件并持久化物理 ID；Worker 崩溃重试时会跳过文件读取，
        直接查询同一物理文档。解析轮询只更新版本行和 Job 进度，旧主文档的 READY、
        Chunk 数和错误快照保持不变。
        """
        # 上层已经做过能力检查；这里把可选依赖收窄为明确局部变量，既让严格类型
        # 检查理解后续调用安全，也避免在版本流程中出现静默降级到旧原地覆盖路径。
        versions = self._versions
        profiles = self._profiles
        if versions is None or profiles is None:
            raise RagError(
                "RAG-DOC-015",
                "文档版本更新能力未启用",
                http_status=503,
                retryable=True,
            )
        if not version.ragflow_document_id:
            try:
                staged_file_id = UUID(str(job.payload["staged_file_id"]))
                safe_filename = str(job.payload.get("safe_filename", version.safe_filename))
                size_bytes = int(job.payload["size_bytes"])
                content_sha256 = str(job.payload["content_sha256"])
            except (KeyError, TypeError, ValueError) as exc:
                raise RagError(
                    "RAG-DOC-015",
                    "文档更新缺少暂存文件",
                    http_status=409,
                    retryable=True,
                ) from exc
            (
                staged_file_id,
                safe_filename,
                size_bytes,
                content_sha256,
            ) = await self._prepare_version_media_if_needed(
                job,
                version,
                staged_file_id=staged_file_id,
                safe_filename=safe_filename,
                size_bytes=size_bytes,
                content_sha256=content_sha256,
            )
            effective_media_type = (
                str(job.payload["content_media_type"])
                if job.payload.get("media_preprocessed")
                else version.mime_type
            )
            version.status = DocumentVersionStatus.UPLOADING
            await versions.update(job.security_scope_id, version)
            await self._checkpoint()
            async with self._staging.open(
                staged_file_id,
                expected_size=size_bytes,
                expected_sha256=content_sha256,
            ) as content:
                engine_document = await self._rag_engine.upload_document_version(
                    UploadDocumentVersionCommand(
                        security=security,
                        knowledge_base_id=document.knowledge_base_id,
                        document_id=document.id,
                        version_id=version.id,
                        # 新版本可采用不同策略，不能从当前主文档推断转换行为。
                        parser_profile_ref=version.parser_profile_ref,
                        filename=_engine_filename(version.id, safe_filename),
                        content=content,
                        media_type=effective_media_type,
                        expected_size_bytes=size_bytes,
                        expected_sha256=content_sha256,
                        metadata=version.metadata,
                    )
                )
            version.ragflow_document_id = engine_document.engine_id
            version.status = DocumentVersionStatus.PARSING
            if not await versions.update(job.security_scope_id, version):
                raise RagError(
                    "RAG-DB-002",
                    "候选版本 RAGFlow 映射保存失败",
                    http_status=409,
                    retryable=True,
                )
            await self._checkpoint()
            await self._staging.delete(staged_file_id)

        if version.status == DocumentVersionStatus.FAILED:
            version.status = DocumentVersionStatus.PARSING
        configuration = profiles.parser_configuration(
            parser_ref=version.parser_profile_ref,
            chunk_ref=version.chunk_profile_ref,
        )
        current = await self._rag_engine.get_document_version(
            GetEngineDocumentVersionQuery(
                security=security,
                knowledge_base_id=document.knowledge_base_id,
                document_id=document.id,
                engine_document_id=version.ragflow_document_id or "",
            )
        )
        run_state = current.run_state.upper()
        if run_state not in {"RUNNING", "PARSING", "DONE", "3"}:
            await self._rag_engine.start_document_version_parse(
                ParseEngineDocumentVersionCommand(
                    security=security,
                    knowledge_base_id=document.knowledge_base_id,
                    document_id=document.id,
                    engine_document_id=version.ragflow_document_id or "",
                    chunk_method=configuration.chunk_method,
                    parser_config=configuration.parser_config,
                )
            )
            current = replace(current, run_state="RUNNING", progress=0.0)

        deadline = self._monotonic() + self._settings.parse_deadline_seconds
        delay = float(self._settings.parse_poll_initial_seconds)
        while self._monotonic() < deadline:
            refreshed_job = await self._jobs.get(job.security_scope_id, job.id)
            if refreshed_job is not None and refreshed_job.status == JobStatus.CANCEL_REQUESTED:
                await self._rag_engine.stop_document_version_parse(
                    StopEngineDocumentVersionCommand(
                        security=security,
                        knowledge_base_id=document.knowledge_base_id,
                        document_id=document.id,
                        engine_document_id=version.ragflow_document_id or "",
                    )
                )
                raise _JobCancelled
            current = await self._rag_engine.get_document_version(
                GetEngineDocumentVersionQuery(
                    security=security,
                    knowledge_base_id=document.knowledge_base_id,
                    document_id=document.id,
                    engine_document_id=version.ragflow_document_id or "",
                )
            )
            run_state = current.run_state.upper()
            version.status = DocumentVersionStatus.PARSING
            version.chunk_count = current.chunk_count
            version.token_count = current.token_count
            job.progress = min(max(current.progress, 0.05), 0.95)
            if run_state in {"DONE", "3"}:
                # 引擎可能在解析器缺失后仍返回 DONE；零切块不能标记可检索成功。
                if current.chunk_count <= 0:
                    raise RagError(
                        "RAG-DOC-008",
                        "解析未生成任何切块，请检查解析服务配置后重试",
                        http_status=502,
                        retryable=False,
                    )
                version.status = DocumentVersionStatus.INDEXING
                await versions.update(job.security_scope_id, version)
                await self._checkpoint()
                return
            if run_state in {"FAIL", "FAILED", "CANCEL", "CANCELLED", "2", "4"}:
                raise RagError(
                    "RAG-DOC-008",
                    "RAGFlow 候选版本解析失败",
                    http_status=502,
                    retryable=True,
                )
            await versions.update(job.security_scope_id, version)
            await self._jobs.update(job.security_scope_id, job)
            await self._checkpoint()
            await self._sleep(delay)
            delay = min(delay * 2, float(self._settings.parse_poll_max_seconds))
        raise RagError(
            "RAG-DOC-009",
            "RAGFlow 候选版本解析超时",
            http_status=504,
            retryable=True,
        )

    async def _execute_delete_document(self, job: IngestionJob) -> None:
        """删除逻辑文档的全部物理版本，再软删除本地记录并推进 Revision。

        版本更新会在同一 RAGFlow Dataset 中保留已停用的历史物理文档以支持快速
        回滚。若删除逻辑文档时只删除当前映射，历史 Chunk 和源文件会成为孤儿并继续
        占用存储；因此启用版本仓库后必须分页枚举并逐一删除全部物理版本。旧数据库
        尚未迁移时仍兼容原单文档删除路径，但绝不同时走两条路径重复删除活动版本。
        """
        document = await self._document_for_job(job)
        if document.status != DocumentStatus.DELETING:
            raise RagError(
                "RAG-DOC-006",
                "文档当前状态不可删除",
                http_status=409,
                retryable=False,
            )
        security = _worker_security_context(job.security_scope_id, job.id)
        deleted_engine_ids: set[str] = set()
        if self._versions is not None:
            page_number = 1
            while True:
                page, total = await self._versions.list(
                    job.security_scope_id,
                    document.id,
                    page=page_number,
                    page_size=100,
                )
                for version in page:
                    if (
                        version.ragflow_document_id
                        and version.ragflow_document_id not in deleted_engine_ids
                    ):
                        await self._rag_engine.delete_document_version(
                            DeleteEngineDocumentVersionCommand(
                                security=security,
                                knowledge_base_id=document.knowledge_base_id,
                                document_id=document.id,
                                engine_document_id=version.ragflow_document_id,
                            )
                        )
                        deleted_engine_ids.add(version.ragflow_document_id)
                if not page or page_number * 100 >= total:
                    break
                page_number += 1
        elif document.ragflow_document_id:
            await self._rag_engine.delete_document(
                DeleteEngineDocumentCommand(
                    security=security,
                    knowledge_base_id=document.knowledge_base_id,
                    document_id=document.id,
                )
            )
        expected_version = document.row_version
        document.status = require_transition(
            document.status,
            DocumentStatus.DELETED,
            DOCUMENT_TRANSITIONS,
        )
        document.deleted_at = utc_now()
        await self._update_document(document, expected_version)
        await self._increment_knowledge_base_revision(
            job.security_scope_id,
            document.knowledge_base_id,
        )

    async def _execute_delete_knowledge_base(self, job: IngestionJob) -> None:
        """按批删除库内文档，随后删除外部数据集并软删除本地知识库。

        非强制任务遇到非空知识库立即拒绝；强制任务逐文档清理外部资源并更新本地
        状态，最后才删除数据集，避免留下指向已消失数据集的活动文档。
        """
        if job.knowledge_base_id is None or self._knowledge_bases is None:
            raise RagError(
                "RAG-KB-006",
                "删除知识库 Job 缺少依赖",
                http_status=409,
                retryable=True,
            )
        knowledge_base = await self._knowledge_bases.get(
            job.security_scope_id,
            job.knowledge_base_id,
        )
        if knowledge_base is None:
            raise ResourceNotFoundError("RAG-KB-001", "知识库不存在")
        force = bool(job.payload.get("force", False))
        while True:
            documents, total = await self._documents.list(
                job.security_scope_id,
                knowledge_base.id,
                page=1,
                page_size=200,
            )
            if total and not force:
                raise RagError(
                    "RAG-KB-005",
                    "知识库非空，删除需要 force=true",
                    http_status=409,
                    retryable=False,
                )
            if not documents:
                break
            for document in documents:
                if document.ragflow_document_id:
                    await self._rag_engine.delete_document(
                        DeleteEngineDocumentCommand(
                            security=_worker_security_context(
                                job.security_scope_id,
                                job.id,
                            ),
                            knowledge_base_id=knowledge_base.id,
                            document_id=document.id,
                        )
                    )
                if document.status != DocumentStatus.DELETING:
                    document.status = require_transition(
                        document.status,
                        DocumentStatus.DELETING,
                        DOCUMENT_TRANSITIONS,
                    )
                expected_version = document.row_version
                document.status = require_transition(
                    document.status,
                    DocumentStatus.DELETED,
                    DOCUMENT_TRANSITIONS,
                )
                document.deleted_at = utc_now()
                await self._update_document(document, expected_version)
            if len(documents) >= total:
                break

        await self._rag_engine.delete_dataset(
            DeleteEngineDatasetCommand(
                security=_worker_security_context(
                    job.security_scope_id,
                    job.id,
                ),
                knowledge_base_id=knowledge_base.id,
            )
        )
        expected_version = knowledge_base.row_version
        knowledge_base.status = require_transition(
            knowledge_base.status,
            KnowledgeBaseStatus.DELETED,
            KNOWLEDGE_BASE_TRANSITIONS,
        )
        knowledge_base.deleted_at = utc_now()
        if not await self._knowledge_bases.update(
            job.security_scope_id,
            knowledge_base,
            expected_version,
        ):
            raise RagError(
                "RAG-RF-007",
                "RAGFlow 删除成功但本地映射未完成",
                http_status=409,
                retryable=True,
            )
        await self._checkpoint()

    async def _mark_document_failure(
        self,
        document: DocumentRecord,
        error: Exception,
    ) -> None:
        """按当前阶段记录文档和初始版本失败，并隐藏未知异常内部消息。

        安全更新候选有自己的错误处理，不会调用这里；首次上传的 v1 则与逻辑文档
        共用同一次解析。若只把主文档标成 FAILED，版本历史会一直错误显示 PENDING
        或 PARSING，因此这里在同一事务中同步尚未激活的初始版本错误快照。
        """
        expected_version = document.row_version
        target = (
            DocumentStatus.DELETE_FAILED
            if document.status == DocumentStatus.DELETING
            else DocumentStatus.FAILED
        )
        if target in DOCUMENT_TRANSITIONS.get(document.status, frozenset()):
            document.status = require_transition(
                document.status,
                target,
                DOCUMENT_TRANSITIONS,
            )
        if isinstance(error, RagError):
            document.last_error_code = error.code
            document.last_error_message = error.message[:1024]
        else:
            document.last_error_code = "RAG-COMMON-011"
            document.last_error_message = "Worker 执行失败"
        if not await self._documents.update(
            document.security_scope_id,
            document,
            expected_version,
        ):
            raise RagError(
                "RAG-DB-002",
                "文档并发更新冲突",
                http_status=409,
                retryable=True,
            )
        if (
            self._versions is not None
            and document.active_version_id is not None
            and document.status == DocumentStatus.FAILED
        ):
            initial_version = await self._versions.get(
                document.security_scope_id,
                document.active_version_id,
            )
            if initial_version is not None and initial_version.status not in {
                DocumentVersionStatus.ACTIVE,
                DocumentVersionStatus.RETIRED,
            }:
                initial_version.status = DocumentVersionStatus.FAILED
                initial_version.last_error_code = document.last_error_code
                initial_version.last_error_message = document.last_error_message
                if not await self._versions.update(
                    document.security_scope_id,
                    initial_version,
                ):
                    raise RagError(
                        "RAG-DB-002",
                        "文档初始版本失败状态保存失败",
                        http_status=409,
                        retryable=True,
                    )
        await self._checkpoint()

    async def _increment_knowledge_base_revision(
        self,
        security_scope_id: str,
        knowledge_base_id: UUID,
    ) -> None:
        """原子递增内容修订号，使旧缓存失效且无需因版本竞争重跑解析。"""
        if self._knowledge_bases is None:
            return
        if not await self._knowledge_bases.increment_revision(
            security_scope_id, knowledge_base_id,
        ):
            raise ResourceNotFoundError("RAG-KB-001", "知识库不存在")
        await self._checkpoint()
