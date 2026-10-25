"""RAG 服务的核心领域数据模型。

本文件只描述业务世界里“有哪些对象、对象保存哪些状态”，不包含数据库或 HTTP
细节。可以把这些 ``dataclass`` 理解成在 API、业务服务、数据库适配器之间传递的
标准数据卡片；枚举则限定了状态字段允许出现的值，避免用任意字符串表达状态。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4


def utc_now() -> datetime:
    """返回带 UTC 时区的当前时间，作为领域对象统一的时间来源。"""
    return datetime.now(UTC)


class KnowledgeBaseStatus(StrEnum):
    """知识库从创建、可用、删除到失败的完整生命周期状态。"""
    CREATING = "CREATING"
    READY = "READY"
    CREATE_FAILED = "CREATE_FAILED"
    DELETING = "DELETING"
    DELETE_FAILED = "DELETE_FAILED"
    DELETED = "DELETED"


class KnowledgeBaseScopeType(StrEnum):
    """知识库资源归属；不表达用户权限或未来成员 ACL。"""

    TENANT = "TENANT"
    SYSTEM = "SYSTEM"


class DocumentStatus(StrEnum):
    """文档从接收文件到解析、建立索引、删除的生命周期状态。"""
    RECEIVED = "RECEIVED"
    UPLOADING = "UPLOADING"
    UPLOADED = "UPLOADED"
    QUEUED = "QUEUED"
    PARSING = "PARSING"
    INDEXING = "INDEXING"
    READY = "READY"
    FAILED = "FAILED"
    RETRYING = "RETRYING"
    DELETING = "DELETING"
    DELETE_FAILED = "DELETE_FAILED"
    DELETED = "DELETED"


class JobStatus(StrEnum):
    """异步任务的调度状态；任务失败和业务资源失败是两个不同概念。"""
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    RETRY_WAIT = "RETRY_WAIT"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"


class JobType(StrEnum):
    """Worker 能执行的任务种类，用于决定任务载荷应交给哪一段处理逻辑。

    ``UPDATE_DOCUMENT`` 与 ``REPARSE`` 的边界非常重要：重解析会在同一个物理
    RAGFlow 文档上重建 Chunk，而更新文档会先创建隔离的新物理版本。只有新版本
    完整解析并建立侧车索引后，Worker 才原子切换主记录，因此更新失败不会让旧版
    正文退出检索。
    """
    UPLOAD = "UPLOAD"
    PARSE = "PARSE"
    REPARSE = "REPARSE"
    UPDATE_DOCUMENT = "UPDATE_DOCUMENT"
    DELETE_DOCUMENT = "DELETE_DOCUMENT"
    DELETE_KB = "DELETE_KB"
    CLEANUP = "CLEANUP"


class DocumentVersionStatus(StrEnum):
    """一个逻辑文档的物理版本在安全发布流程中的状态。

    ``ACTIVE`` 同一时刻最多只能有一个；``RETIRED`` 仍保留审计信息，并可在物理
    RAGFlow 文档尚未清理时用于快速回滚；``FAILED`` 明确表示候选版本没有影响当前
    可检索版本。状态独立于 ``DocumentStatus``，所以用户更新文件时文档主记录仍可
    保持 ``READY`` 并继续提供旧版答案。
    """

    PENDING = "PENDING"
    UPLOADING = "UPLOADING"
    PARSING = "PARSING"
    INDEXING = "INDEXING"
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"
    FAILED = "FAILED"


class DocumentUpdateMode(StrEnum):
    """客户端请求和 Worker 最终执行的文档更新模式。

    当前首个生产安全版本支持 ``SAFE_REPLACE``：新旧物理文档并存直到原子切换。
    ``AUTO`` 为向前兼容入口，允许以后在文本格式满足严格条件时选择真正 Chunk
    增量；在能力尚未启用时必须明确落到安全替换，不能伪装成已经复用了旧索引。
    """

    AUTO = "AUTO"
    SAFE_REPLACE = "SAFE_REPLACE"


@dataclass(frozen=True, slots=True)
class ProfileRef:
    """不可变的配置版本引用，例如 ``default-parser@1``。"""
    name: str
    version: int

    @classmethod
    def parse(cls, value: str) -> ProfileRef:
        """把 ``名称@正整数版本`` 字符串解析为强类型引用，格式不合法时拒绝启动。"""
        name, separator, raw_version = value.rpartition("@")
        if not separator or not name or not raw_version.isdigit() or int(raw_version) <= 0:
            raise ValueError(f"invalid profile reference: {value}")
        return cls(name=name, version=int(raw_version))

    def __str__(self) -> str:
        """还原为配置文件和数据库共同使用的标准引用格式。"""
        return f"{self.name}@{self.version}"


@dataclass(slots=True)
class KnowledgeBase:
    """一个租户或系统共享的知识库聚合根。

    ``security_scope_id`` 决定数据归属；四个 profile 引用冻结该知识库采用的解析、
    分块、向量和检索策略；``ragflow_dataset_id`` 是外部引擎中的对应 ID；
    ``revision`` 在内容变化时递增，用于使旧缓存自然失效；``row_version`` 用于数据库
    乐观锁，防止两个并发请求相互覆盖。
    """
    security_scope_id: str
    name: str
    parser_profile_ref: str
    chunk_profile_ref: str
    embedding_profile_ref: str
    retrieval_profile_ref: str
    scope_type: KnowledgeBaseScopeType = KnowledgeBaseScopeType.TENANT
    id: UUID = field(default_factory=uuid4)
    external_id: str | None = None
    description: str | None = None
    ragflow_dataset_id: str | None = None
    status: KnowledgeBaseStatus = KnowledgeBaseStatus.CREATING
    revision: int = 0
    document_count: int = 0
    row_version: int = 0
    last_error_code: str | None = None
    last_error_message: str | None = None
    deleted_at: datetime | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(slots=True)
class DocumentRecord:
    """上传文档在本系统中的主记录。

    原始文件名只用于展示，``safe_filename`` 才能用于磁盘；大小与 SHA-256 用于上传
    完整性校验；``ragflow_document_id`` 连接外部解析引擎；进度、块数、Token 数以及
    最近错误让 API 能准确展示长时间解析任务的当前情况。``active_version_id``、
    ``active_version_number`` 和 ``document_revision`` 描述当前对外可见版本；更新
    过程中这些字段保持不变，只有候选版本全部成功后才一起切换。
    """
    security_scope_id: str
    knowledge_base_id: UUID
    filename: str
    safe_filename: str
    mime_type: str
    file_size_bytes: int
    content_sha256: str
    parser_profile_ref: str
    chunk_profile_ref: str
    id: UUID = field(default_factory=uuid4)
    external_id: str | None = None
    ragflow_document_id: str | None = None
    active_version_id: UUID | None = None
    active_version_number: int = 1
    document_revision: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)
    status: DocumentStatus = DocumentStatus.RECEIVED
    progress: float = 0.0
    chunk_count: int = 0
    token_count: int = 0
    row_version: int = 0
    last_error_code: str | None = None
    last_error_message: str | None = None
    engine_progress_message: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    indexed_at: datetime | None = None
    deleted_at: datetime | None = None


@dataclass(slots=True)
class DocumentVersion:
    """一个逻辑文档的不可变文件版本及其独立 RAGFlow 映射。

    API 上传新文件后先把原始字节安全暂存，再创建 ``PENDING`` 版本和
    ``UPDATE_DOCUMENT`` Job。Worker 使用 ``id`` 生成不会与旧版冲突的引擎文件名，
    将候选版本上传、解析和验证；全部成功后才把该记录改成 ``ACTIVE``，旧记录改成
    ``RETIRED``，并同步更新 ``DocumentRecord``。失败信息只写候选版本，绝不覆盖
    当前可用文档的状态和错误字段。

    ``requested_mode`` 是客户端意图，``effective_mode`` 是实际执行方式。首版只会
    返回 ``SAFE_REPLACE``，为后续真正 Chunk 增量保留可审计的协议字段。
    """

    security_scope_id: str
    knowledge_base_id: UUID
    document_id: UUID
    version_number: int
    filename: str
    safe_filename: str
    mime_type: str
    file_size_bytes: int
    content_sha256: str
    parser_profile_ref: str
    chunk_profile_ref: str
    id: UUID = field(default_factory=uuid4)
    ragflow_document_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    status: DocumentVersionStatus = DocumentVersionStatus.PENDING
    requested_mode: DocumentUpdateMode = DocumentUpdateMode.AUTO
    effective_mode: DocumentUpdateMode = DocumentUpdateMode.SAFE_REPLACE
    chunk_count: int = 0
    token_count: int = 0
    last_error_code: str | None = None
    last_error_message: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    activated_at: datetime | None = None
    retired_at: datetime | None = None


@dataclass(slots=True)
class IngestionJob:
    """交给 Worker 异步执行的持久化任务。

    ``attempt/max_attempts`` 控制重试，``locked_by/locked_until`` 构成租约，避免多个
    Worker 同时处理同一任务；``payload`` 是输入快照，``result`` 是完成后的结构化
    结果。知识库级任务可以没有 ``document_id``。
    """
    security_scope_id: str
    job_type: JobType
    id: UUID = field(default_factory=uuid4)
    status: JobStatus = JobStatus.PENDING
    knowledge_base_id: UUID | None = None
    document_id: UUID | None = None
    progress: float = 0.0
    attempt: int = 0
    max_attempts: int = 2
    payload: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    next_retry_at: datetime | None = None
    locked_by: str | None = None
    locked_until: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime = field(default_factory=utc_now)


class IdempotencyStatus(StrEnum):
    """一次幂等请求当前是处理中、已完成还是已失败。"""
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


@dataclass(slots=True)
class IdempotencyRecord:
    """幂等键及其请求指纹和已缓存响应。

    同一隔离范围内，相同幂等键只能对应同一个请求哈希；完成后保存响应，重复请求
    可直接返回原结果，而不会再次创建知识库、文档或任务。
    """
    security_scope_id: str
    idempotency_key: str
    http_method: str
    request_path: str
    request_sha256: str
    processing_status: IdempotencyStatus = IdempotencyStatus.PROCESSING
    response_status: int | None = None
    response_json: dict[str, Any] | None = None
    resource_id: UUID | None = None
    created_at: datetime = field(default_factory=utc_now)
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class IdempotencyReservation:
    """幂等键抢占结果；``acquired`` 表示当前调用方是否获得实际执行权。"""
    record: IdempotencyRecord
    acquired: bool


@dataclass(frozen=True, slots=True)
class SourcePosition:
    """文本块在原文件中的位置，可包含页码和页面坐标框。"""
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None


@dataclass(frozen=True, slots=True)
class RetrievalCandidate:
    """检索引擎返回并完成本地身份映射后的候选文本块。

    ``score`` 是当前排序使用的综合分；向量、关键词和重排分数单独保留，便于调试
    排名来源。``engine_chunk_id`` 用于回查外部引擎，``chunk_id`` 是对外稳定标识。
    """
    chunk_id: str
    engine_chunk_id: str
    knowledge_base_id: UUID
    document_id: UUID
    document_name: str
    content: str
    score: float
    resource_scope_id: str = ""
    vector_score: float | None = None
    term_score: float | None = None
    rerank_score: float | None = None
    positions: tuple[SourcePosition, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


class IndexNodeType(StrEnum):
    """Gateway 侧车层级索引的节点类型，不改变 RAGFlow 原始 Chunk。

    ``SUMMARY`` 表示覆盖整篇文档的摘要入口，``SECTION`` 表示章节级摘要/导航，
    ``PARENT`` 则连接一小组连续原始 Chunk。三类节点共同形成
    “文档摘要 → 章节 → Parent → RAGFlow Chunk”的多粒度路径。
    """

    PARENT = "PARENT"
    SECTION = "SECTION"
    SUMMARY = "SUMMARY"


@dataclass(frozen=True, slots=True)
class DocumentIndexNode:
    """由真实 Chunk 派生的 Parent、Section 或文档摘要侧车节点。

    ``source_chunk_ids`` 始终保存真实 RAGFlow Chunk ID。节点正文和 Embedding 仅用来
    先定位相关区域，最终回答必须回读这些原始块并生成引用，不能直接把摘要当作
    无法核验的事实来源。``index_version`` 允许未来更换分组或摘要算法时并行重建。
    """

    security_scope_id: str
    knowledge_base_id: UUID
    document_id: UUID
    node_type: IndexNodeType
    ordinal: int
    content: str
    source_chunk_ids: tuple[str, ...]
    id: UUID = field(default_factory=uuid4)
    embedding: tuple[float, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)
    index_version: int = 1
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class DocumentEntity:
    """从一个文档中抽取并聚合的实体，以及出现该实体的真实源 Chunk。"""

    security_scope_id: str
    knowledge_base_id: UUID
    document_id: UUID
    canonical_name: str
    normalized_name: str
    entity_type: str
    source_chunk_ids: tuple[str, ...]
    id: UUID = field(default_factory=uuid4)
    aliases: tuple[str, ...] = ()
    mention_count: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)
    index_version: int = 1
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class EntityRelation:
    """文档中明确出现的“主体—谓词—客体”轻量关系及其原文来源。"""

    security_scope_id: str
    knowledge_base_id: UUID
    document_id: UUID
    subject_name: str
    subject_normalized: str
    predicate: str
    object_name: str
    object_normalized: str
    source_chunk_ids: tuple[str, ...]
    id: UUID = field(default_factory=uuid4)
    confidence: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)
    index_version: int = 1
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class GraphEvidence:
    """实体/关系查询命中的可回查证据引用，不携带合成答案正文。"""

    knowledge_base_id: UUID
    document_id: UUID
    source_chunk_ids: tuple[str, ...]
    score: float
    matched_text: str
    evidence_type: str


class AnswerOutcome(StrEnum):
    """问答在业务语义上的结果，而不是 HTTP 请求是否成功。"""
    SUCCESS = "success"
    NO_EVIDENCE = "no_evidence"
    REFUSED = "refused"
    CANCELLED = "cancelled"


class QueryStatus(StrEnum):
    """一次检索或问答请求的执行状态。"""
    ACCEPTED = "ACCEPTED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass(frozen=True, slots=True)
class Citation:
    """答案引用的一段可追溯证据，连接知识库、文档、引擎块和原文位置。"""
    citation_id: UUID
    label: str
    knowledge_base_id: UUID
    document_id: UUID
    document_name: str
    engine_chunk_id: str
    quote: str
    resource_scope_id: str = ""
    page_start: int | None = None
    page_end: int | None = None
    positions: tuple[SourcePosition, ...] = ()
    score: float | None = None
    rerank_score: float | None = None


@dataclass(slots=True)
class QueryRecord:
    """一次在线检索/问答的审计记录。

    只保存经过脱敏的查询和必要诊断信息，不保存原始 Prompt/上下文；候选数量、
    耗时、Token 用量、缓存状态与错误码用于可观测性。``request_id`` 同时也是 API
    请求的稳定查询键。
    """
    security_scope_id: str
    request_id: UUID
    query_sha256: str
    knowledge_base_ids: tuple[UUID, ...]
    retrieval_profile_ref: str
    request_status: QueryStatus = QueryStatus.ACCEPTED
    outcome: AnswerOutcome | None = None
    query_redacted: str | None = None
    effective_query_redacted: str | None = None
    prompt_profile_ref: str | None = None
    model_profile_ref: str | None = None
    cache_status: str = "BYPASS"
    candidate_count: int = 0
    context_count: int = 0
    answer_text: str | None = None
    timings: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    error_code: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    completed_at: datetime | None = None
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class FeedbackRecord:
    """用户对指定请求结果提交的评价，供后续质量分析使用。"""
    security_scope_id: str
    request_id: UUID
    feedback_type: str
    id: UUID = field(default_factory=uuid4)
    rating: int | None = None
    comment: str | None = None
    corrected_answer: str | None = None
    created_at: datetime = field(default_factory=utc_now)
