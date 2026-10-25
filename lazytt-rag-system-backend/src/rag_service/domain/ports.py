"""应用层依赖的端口协议，以及跨边界传递的命令/结果对象。

“端口”只规定能力，不绑定实现：业务服务只知道可以创建知识库、保存文件、调用模型
或读写记录，却不知道背后是 RAGFlow、Redis、PostgreSQL 还是测试替身。带 ``Command``
或 ``Query`` 后缀的不可变对象把一次下游调用所需参数打包，避免位置参数混淆。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO, Protocol
from uuid import UUID

from rag_service.domain.models import (
    Citation,
    DocumentEntity,
    DocumentIndexNode,
    DocumentRecord,
    DocumentVersion,
    EntityRelation,
    FeedbackRecord,
    GraphEvidence,
    IdempotencyRecord,
    IdempotencyReservation,
    IndexNodeType,
    IngestionJob,
    KnowledgeBase,
    QueryRecord,
    RetrievalCandidate,
    SourcePosition,
)
from rag_service.security.context import SecurityContext


@dataclass(frozen=True, slots=True)
class Page:
    """通用分页结果：当前页数据、页码、每页大小和命中总数。"""

    items: tuple[Any, ...]
    page: int
    page_size: int
    total: int


@dataclass(frozen=True, slots=True)
class CreateDatasetCommand:
    """请求外部 RAG 引擎创建数据集时所需的完整参数。"""

    security: SecurityContext
    knowledge_base_id: UUID
    name: str
    description: str | None
    chunk_method: str
    parser_config: dict[str, Any]
    embedding_model: str


@dataclass(frozen=True, slots=True)
class GetEngineDatasetQuery:
    """按本地知识库 ID 查询对应引擎数据集。"""

    security: SecurityContext
    knowledge_base_id: UUID


@dataclass(frozen=True, slots=True)
class UpdateEngineDatasetCommand:
    """更新引擎数据集；值为 ``None`` 的字段表示保持不变。"""

    security: SecurityContext
    knowledge_base_id: UUID
    name: str | None = None
    description: str | None = None
    parser_config: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class DeleteEngineDatasetCommand:
    """删除本地知识库对应的引擎数据集。"""

    security: SecurityContext
    knowledge_base_id: UUID


@dataclass(frozen=True, slots=True)
class UploadDocumentCommand:
    """把已校验文件流上传到指定知识库对应的引擎数据集。"""

    security: SecurityContext
    knowledge_base_id: UUID
    document_id: UUID
    filename: str
    content: BinaryIO
    # 上传前转换由不可变策略决定；候选版本必须显式传自己的策略，不能沿用旧版本。
    parser_profile_ref: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # 音视频先转换成 Markdown，RAGFlow 实际接收内容与用户原始文件身份不同。下面
    # 三个可选字段只描述已经重新校验的派生字节；普通文档留空时继续使用 DocumentRecord。
    content_media_type: str | None = None
    expected_size_bytes: int | None = None
    expected_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class UploadDocumentVersionCommand:
    """把候选版本上传为独立 RAGFlow 物理文档。

    ``document_id`` 只用于验证逻辑文档仍属于当前 Scope；``version_id`` 参与生成
    唯一文件名并关联日志。大小和摘要来自暂存层的二次校验，Adapter 不得从当前
    ``DocumentRecord`` 猜测，因为主记录在发布前仍描述旧版本。
    """

    security: SecurityContext
    knowledge_base_id: UUID
    document_id: UUID
    version_id: UUID
    filename: str
    content: BinaryIO
    media_type: str
    expected_size_bytes: int
    expected_sha256: str
    # 候选版本的解析策略供适配器决定是否把图片包装为 PDF。
    parser_profile_ref: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GetEngineDocumentVersionQuery:
    """按已经过本地版本仓库校验的物理 ID 查询候选版本状态。"""

    security: SecurityContext
    knowledge_base_id: UUID
    document_id: UUID
    engine_document_id: str


@dataclass(frozen=True, slots=True)
class UpdateEngineDocumentVersionCommand:
    """在候选物理文档上写入 Metadata，不改变当前活动版本映射。"""

    security: SecurityContext
    knowledge_base_id: UUID
    document_id: UUID
    engine_document_id: str
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class DeleteEngineDocumentVersionCommand:
    """删除一个已明确绑定到本地版本的物理 RAGFlow 文档。"""

    security: SecurityContext
    knowledge_base_id: UUID
    document_id: UUID
    engine_document_id: str


@dataclass(frozen=True, slots=True)
class ParseEngineDocumentVersionCommand:
    """给候选物理文档应用已批准 Profile 并启动解析。"""

    security: SecurityContext
    knowledge_base_id: UUID
    document_id: UUID
    engine_document_id: str
    chunk_method: str
    parser_config: dict[str, Any]


@dataclass(frozen=True, slots=True)
class StopEngineDocumentVersionCommand:
    """取消候选物理文档解析，用于更新任务取消和超时补偿。"""

    security: SecurityContext
    knowledge_base_id: UUID
    document_id: UUID
    engine_document_id: str


@dataclass(frozen=True, slots=True)
class ListEngineVersionChunksQuery:
    """读取候选版本的原始 Chunk，并映射回稳定逻辑 document_id。"""

    security: SecurityContext
    knowledge_base_id: UUID
    document_id: UUID
    engine_document_id: str
    page: int = 1
    page_size: int = 20


@dataclass(frozen=True, slots=True)
class SetEngineDocumentVersionAvailabilityCommand:
    """启用或停用一个完整的 RAGFlow 物理文档版本。

    文档版本发布必须让 RAGFlow 文档列表里的“启用”开关与实际检索状态保持一致，
    因此主切换流程使用文档级状态接口。RAGFlow 会同时修改文档 ``status`` 和该文档
    所有 Chunk 的 ``available_int``；若后续 PostgreSQL 提交失败，Worker 会发送相反
    命令补偿，恢复旧版本并重新关闭候选版本。
    """

    security: SecurityContext
    knowledge_base_id: UUID
    document_id: UUID
    engine_document_id: str
    available: bool


@dataclass(frozen=True, slots=True)
class SetEngineVersionChunksAvailabilityCommand:
    """批量启用或停用一个物理版本的指定 Chunk。

    该命令保留给 Chunk 级修复和运维操作。正常的版本发布与回滚应使用完整物理文档
    可用性命令，让 RAGFlow 文档状态和所有 Chunk 状态一起变化，避免管理界面显示
    “文件已启用”但实际 Chunk 已停用的双重状态。
    """

    security: SecurityContext
    knowledge_base_id: UUID
    document_id: UUID
    engine_document_id: str
    chunk_ids: tuple[str, ...]
    available: bool


@dataclass(frozen=True, slots=True)
class GetEngineDocumentQuery:
    """查询某文档在外部引擎中的解析状态。"""

    security: SecurityContext
    knowledge_base_id: UUID
    document_id: UUID


@dataclass(frozen=True, slots=True)
class ListEngineDocumentsQuery:
    """分页列出一个知识库在外部引擎中的文档。"""

    security: SecurityContext
    knowledge_base_id: UUID
    page: int = 1
    page_size: int = 20


@dataclass(frozen=True, slots=True)
class UpdateEngineDocumentCommand:
    """更新外部引擎文档的元数据。"""

    security: SecurityContext
    knowledge_base_id: UUID
    document_id: UUID
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class DeleteEngineDocumentCommand:
    """从外部引擎删除文档及其索引块。"""

    security: SecurityContext
    knowledge_base_id: UUID
    document_id: UUID


@dataclass(frozen=True, slots=True)
class ParseDocumentCommand:
    """启动文档解析，并可先应用该文档自己的版本化解析/分块配置。

    ``chunk_method`` 与 ``parser_config`` 同时为空表示兼容旧任务，继续沿用 RAGFlow
    文档上传时继承的数据集配置；存在时 Adapter 必须先调用官方文档更新接口，再启动
    解析。配置只包含受 ProfileCatalog 校验的非敏感参数，不允许调用方透传任意 JSON。
    """

    security: SecurityContext
    knowledge_base_id: UUID
    document_id: UUID
    delete_existing: bool = False
    chunk_method: str | None = None
    parser_config: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class StopParseCommand:
    """请求外部引擎停止指定文档的解析任务。"""

    security: SecurityContext
    knowledge_base_id: UUID
    document_id: UUID


@dataclass(frozen=True, slots=True)
class ListEngineChunksQuery:
    """分页读取指定文档解析出的文本块。"""

    security: SecurityContext
    knowledge_base_id: UUID
    document_id: UUID
    page: int = 1
    page_size: int = 20
    document_revision: int | None = None


@dataclass(frozen=True, slots=True)
class GetDocumentSourceQuery:
    """读取当前原始 PDF；应用层校验资源身份，适配器用修订号锁定预览版本。"""

    security: SecurityContext
    knowledge_base_id: UUID
    document_id: UUID
    document_revision: int


@dataclass(frozen=True, slots=True)
class DocumentSource:
    """一次性原文件流；调用方消费结束或取消时必须调用 close 释放下游连接。"""

    filename: str
    size_bytes: int
    content: AsyncIterator[bytes]
    close: Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class EngineRetrievalQuery:
    """提交给 RAG 引擎的一次检索请求。

    可同时限制知识库和文档，``top_k`` 控制候选上限，阈值剔除弱相关结果，
    ``vector_similarity_weight`` 平衡语义向量与关键词得分。
    """

    security: SecurityContext
    knowledge_base_ids: tuple[UUID, ...]
    question: str
    document_ids: tuple[UUID, ...] = ()
    page_size: int = 20
    top_k: int = 50
    similarity_threshold: float = 0.2
    vector_similarity_weight: float = 0.5
    metadata_filter: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class EngineDataset:
    """外部引擎数据集的最小标准化视图。"""

    engine_id: str
    name: str
    chunk_method: str


@dataclass(frozen=True, slots=True)
class EngineDocument:
    """文档上传后外部引擎返回的身份和初始运行状态。"""

    engine_id: str
    dataset_engine_id: str
    name: str
    run_state: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class EngineDocumentStatus:
    """外部引擎文档的当前解析进度与产物统计。"""

    engine_id: str
    dataset_engine_id: str
    name: str
    run_state: str
    progress: float
    chunk_count: int
    token_count: int


@dataclass(frozen=True, slots=True)
class EngineChunk:
    """外部引擎产生的单个文本块及其原文位置。"""

    engine_chunk_id: str
    document_id: UUID
    content: str
    positions: tuple[SourcePosition, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EngineRetrievalResult:
    """标准化的候选集合和引擎侧总命中数。"""

    candidates: tuple[RetrievalCandidate, ...]
    total: int


@dataclass(frozen=True, slots=True)
class EngineHealth:
    """RAG 引擎整体状态以及它报告的子依赖状态。"""

    status: str
    dependencies: dict[str, str] = field(default_factory=dict)


class RagEnginePort(Protocol):
    """知识库、文档、解析、文本块、检索和健康检查的引擎抽象。

    每个方法都接收上方强类型命令/查询，并返回标准化领域结果；实现必须应用命令中
    的 ``SecurityContext`` 做 ID 映射，不能把调用方提供的本地 ID 直接当外部 ID。
    """

    async def create_dataset(self, command: CreateDatasetCommand) -> EngineDataset:
        """依据本地知识库配置创建外部数据集，并返回标准化后的引擎身份。"""
        ...

    async def get_dataset(self, query: GetEngineDatasetQuery) -> EngineDataset:
        """读取本地知识库所映射的外部数据集；无权或不存在时应抛领域错误。"""
        ...

    async def update_dataset(self, command: UpdateEngineDatasetCommand) -> EngineDataset:
        """只更新命令中非空的数据集字段，并返回更新后的标准化视图。"""
        ...

    async def delete_dataset(self, command: DeleteEngineDatasetCommand) -> None:
        """删除知识库映射的外部数据集及其引擎侧内容。"""
        ...

    async def upload_document(self, command: UploadDocumentCommand) -> EngineDocument:
        """上传已经校验的文件流，并返回外部文档 ID 和初始解析状态。"""
        ...

    async def upload_document_version(
        self, command: UploadDocumentVersionCommand
    ) -> EngineDocument:
        """上传候选版本，但不覆盖主文档当前使用的引擎 ID。"""
        ...

    async def get_document_version(
        self, query: GetEngineDocumentVersionQuery
    ) -> EngineDocumentStatus:
        """读取候选物理版本的解析状态和统计。"""
        ...

    async def update_document_version(
        self, command: UpdateEngineDocumentVersionCommand
    ) -> EngineDocument:
        """更新候选版本 Metadata，并返回最新物理文档摘要。"""
        ...

    async def delete_document_version(self, command: DeleteEngineDocumentVersionCommand) -> None:
        """删除失败或不再保留的候选物理版本。"""
        ...

    async def start_document_version_parse(
        self, command: ParseEngineDocumentVersionCommand
    ) -> None:
        """应用版本自己的文档级 Profile 后启动解析。"""
        ...

    async def stop_document_version_parse(self, command: StopEngineDocumentVersionCommand) -> None:
        """请求引擎停止候选版本解析。"""
        ...

    async def list_document_version_chunks(self, query: ListEngineVersionChunksQuery) -> Page:
        """分页读取候选版本 Chunk，供激活前验证和侧车索引构建。"""
        ...

    async def set_document_version_available(
        self, command: SetEngineDocumentVersionAvailabilityCommand
    ) -> None:
        """切换完整物理文档状态，并同步其全部 Chunk 的检索可用性。"""
        ...

    async def set_document_version_chunks_available(
        self, command: SetEngineVersionChunksAvailabilityCommand
    ) -> None:
        """按显式 Chunk ID 切换可用性，仅用于细粒度修复和运维。"""
        ...

    async def get_document(self, query: GetEngineDocumentQuery) -> EngineDocumentStatus:
        """读取外部文档的解析进度、文本块数量和 Token 统计。"""
        ...

    async def list_documents(self, query: ListEngineDocumentsQuery) -> Page:
        """分页列出一个知识库映射数据集中的外部文档。"""
        ...

    async def update_document(self, command: UpdateEngineDocumentCommand) -> EngineDocument:
        """更新外部文档元数据，并返回更新后的文档摘要。"""
        ...

    async def delete_document(self, command: DeleteEngineDocumentCommand) -> None:
        """删除外部文档及其已解析的索引文本块。"""
        ...

    async def start_parse(self, command: ParseDocumentCommand) -> None:
        """启动文档解析；命令可要求先删除旧文本块以执行干净重建。"""
        ...

    async def stop_parse(self, command: StopParseCommand) -> None:
        """请求引擎停止指定文档仍在进行的解析任务。"""
        ...

    async def list_chunks(self, query: ListEngineChunksQuery) -> Page:
        """分页读取文档文本块及其原文定位和元数据。"""
        ...

    async def get_document_source(self, query: GetDocumentSourceQuery) -> DocumentSource:
        """只读获取已映射的 PDF 流；禁止接受任意 URL、外部文件 ID 或落盘路径。"""
        ...

    async def retrieve(self, query: EngineRetrievalQuery) -> EngineRetrievalResult:
        """按查询中的范围、阈值和权重执行检索，返回统一候选集合。"""
        ...

    async def health(self) -> EngineHealth:
        """探测 RAG 引擎及其子依赖，并返回不含厂商格式的健康摘要。"""
        ...


@dataclass(frozen=True, slots=True)
class StagedFileMetadata:
    """上传流落盘前已知的客户端文件信息和可选完整性预期。"""

    original_filename: str
    media_type: str | None = None
    expected_size: int | None = None
    expected_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class StagedFile:
    """已原子落盘并可供 Worker 打开的安全暂存文件。"""

    staged_file_id: UUID
    safe_filename: str
    size_bytes: int
    content_sha256: str
    ready_path: Path


@dataclass(frozen=True, slots=True)
class CleanupResult:
    """一次暂存清理删除的文件数量和回收字节统计。"""

    removed_uploading: int = 0
    removed_ready: int = 0
    bytes_reclaimed: int = 0


class UploadStagingPort(Protocol):
    """大文件流式保存、校验打开、删除和过期清理能力。

    ``save`` 必须先写临时后缀再原子改名；``open`` 必须重新校验大小和摘要；
    ``cleanup_expired`` 不得删除仍被活动任务引用的文件。
    """

    async def save(self, stream: AsyncIterator[bytes], metadata: StagedFileMetadata) -> StagedFile:
        """流式接收上传内容，校验大小/摘要，原子落盘后返回可信文件信息。"""
        ...

    def open(
        self,
        staged_file_id: UUID,
        *,
        expected_size: int,
        expected_sha256: str,
    ) -> AbstractAsyncContextManager[BinaryIO]:
        """重新核对文件身份、大小和摘要，并以异步上下文管理器方式安全打开。"""
        ...

    async def delete(self, staged_file_id: UUID) -> None:
        """删除指定暂存文件；实现应允许对已不存在文件重复调用。"""
        ...

    async def cleanup_expired(
        self,
        now: datetime,
        limit: int,
        active_staged_file_ids: frozenset[UUID],
    ) -> CleanupResult:
        """在数量上限内回收过期文件，同时跳过仍被活动任务引用的文件。"""
        ...


@dataclass(frozen=True, slots=True)
class PreparedMediaDocument:
    """音视频预处理产生的可索引 Markdown 及可复核处理元数据。"""

    filename: str
    content: bytes
    mime_type: str
    metadata: dict[str, Any] = field(default_factory=dict)


class MediaProcessingPort(Protocol):
    """把受支持的音频/视频转换成带时间戳文本，不直接修改 RAGFlow。"""

    def supports(self, filename: str) -> bool:
        """返回扩展名是否属于当前音视频处理范围。"""
        ...

    async def prepare(
        self,
        content: BinaryIO,
        *,
        filename: str,
        mime_type: str,
    ) -> PreparedMediaDocument:
        """校验并转写媒体，失败时抛出稳定领域错误。"""
        ...


@dataclass(frozen=True, slots=True)
class LlmMessage:
    """一条模型对话消息，角色通常为 system/user/assistant。"""

    role: str
    content: str


@dataclass(frozen=True, slots=True)
class LlmRequest:
    """一次文本生成请求以及可重复的采样参数。"""

    messages: tuple[LlmMessage, ...]
    model: str
    max_output_tokens: int
    temperature: float = 0.1
    top_p: float = 0.8
    response_format: str = "text"


@dataclass(frozen=True, slots=True)
class LlmUsage:
    """模型报告的输入、输出和总 Token 用量。"""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


@dataclass(frozen=True, slots=True)
class LlmResponse:
    """非流式模型响应文本及可选 Token 用量。"""

    content: str
    usage: LlmUsage | None = None


@dataclass(frozen=True, slots=True)
class LlmEvent:
    """流式模型增量；结束事件可只携带最终 Token 用量。"""

    delta: str = ""
    usage: LlmUsage | None = None


class LlmPort(Protocol):
    """文本模型的非流式完成与异步流式生成接口。"""

    async def complete(self, request: LlmRequest) -> LlmResponse:
        """一次性生成完整文本，并尽可能返回模型报告的 Token 用量。"""
        ...

    def stream(self, request: LlmRequest) -> AsyncIterator[LlmEvent]:
        """按生成顺序异步产出文本增量，结束事件可携带最终用量。"""
        ...


class EmbeddingPort(Protocol):
    """将一批文本按原顺序转换为固定维度浮点向量。"""

    async def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        """批量向量化文本；返回数量和顺序必须与输入严格一致。"""
        ...


@dataclass(frozen=True, slots=True)
class RerankOutcome:
    """与输入文档一一对齐的重排分数，以及是否启用/是否降级的信息。"""

    scores: tuple[float | None, ...]
    enabled: bool
    warning: str | None = None


class RerankPort(Protocol):
    """可选的查询—文档相关性重排接口。"""

    @property
    def enabled(self) -> bool:
        """说明当前运行配置是否真正启用了重排能力。"""
        ...

    async def rerank(
        self,
        query: str,
        documents: Sequence[str],
    ) -> RerankOutcome:
        """为每个候选文档计算相对查询的分数；降级时在结果中说明原因。"""
        ...


@dataclass(frozen=True, slots=True)
class CacheBoundary:
    """所有会改变答案语义的缓存隔离维度。

    除租户外还包含知识库修订、检索/Prompt/模型版本、语言和历史摘要，因此缓存命中
    等价于“同一问题在完全相同运行条件下已有答案”。
    """

    security_scope_id: str
    knowledge_base_revisions_hash: str
    retrieval_profile: str
    prompt_profile: str
    model_profile: str
    language: str
    history_hash: str


@dataclass(frozen=True, slots=True)
class CacheEntry:
    """可缓存的完整答案、引用、查询规划轨迹、动态预算和模型用量。

    三个预算字段使用可空类型，是为了兼容升级前已经写入 Redis 的答案。新答案会
    完整保存实际参数；旧记录读取时保留未知状态，不会因为代码升级而伪造历史值。
    """

    cache_id: str
    boundary: CacheBoundary
    normalized_question: str
    answer: str
    citations: tuple[Citation, ...]
    original_query: str | None = None
    effective_query: str | None = None
    query_language: str | None = None
    query_variants: dict[str, str] = field(default_factory=dict)
    knowledge_languages: tuple[str, ...] = ()
    retrieval_routes: tuple[str, ...] = ()
    query_type: str | None = None
    retrieval_top_k: int | None = None
    rerank_top_n: int | None = None
    context_top_n: int | None = None
    warnings: tuple[str, ...] = ()
    usage: LlmUsage | None = None
    similarity: float | None = None


class AnswerCachePort(Protocol):
    """精确问题哈希和向量相似度两级答案缓存接口。"""

    async def get_exact(
        self,
        boundary: CacheBoundary,
        normalized_question: str,
    ) -> CacheEntry | None:
        """按完整语义边界和规范化问题查找完全相同的缓存答案。"""
        ...

    async def get_semantic(
        self,
        boundary: CacheBoundary,
        normalized_question: str,
    ) -> tuple[CacheEntry, ...]:
        """在同一语义边界内查找向量相近的问题答案，按相关性返回候选。"""
        ...

    async def put(self, value: CacheEntry) -> None:
        """同时保存可精确命中和可语义搜索的完整答案记录。"""
        ...

    async def mark_hit(self, value: CacheEntry) -> None:
        """记录缓存被使用的次数或最近命中时间，不改变答案内容。"""
        ...


class KnowledgeBaseRepository(Protocol):
    """知识库持久化接口；每次读写都必须显式带安全范围。"""

    async def increment_revision(self, security_scope_id: str, knowledge_base_id: UUID) -> bool:
        """原子递增内容修订号及行版本；不存在、跨范围或已删除时返回 False。"""
        ...

    async def add(self, security_scope_id: str, value: KnowledgeBase) -> None:
        """在指定安全作用域中新增知识库记录。"""
        ...

    async def get(self, security_scope_id: str, knowledge_base_id: UUID) -> KnowledgeBase | None:
        """按本地 ID 读取作用域内知识库；不可见或不存在均返回 ``None``。"""
        ...

    async def get_many(
        self, security_scope_id: str, knowledge_base_ids: Sequence[UUID]
    ) -> tuple[KnowledgeBase, ...]:
        """批量读取作用域内可见知识库，不返回其他租户记录。"""
        ...

    async def get_accessible(
        self,
        security_scope_ids: Sequence[str],
        knowledge_base_id: UUID,
    ) -> KnowledgeBase | None:
        """从访问策略给出的候选资源范围读取单个知识库。"""
        ...

    async def get_deleted(
        self, security_scope_id: str, knowledge_base_id: UUID
    ) -> KnowledgeBase | None:
        """只读取指定资源范围内已软删除的知识库，供删除任务完成后核验归属。"""
        ...

    async def get_many_accessible(
        self,
        security_scope_ids: Sequence[str],
        knowledge_base_ids: Sequence[UUID],
    ) -> tuple[KnowledgeBase, ...]:
        """从候选资源范围批量读取知识库。"""
        ...

    async def list_accessible(
        self,
        security_scope_ids: Sequence[str],
        *,
        page: int,
        page_size: int,
        name: str | None = None,
        status: str | None = None,
        scope_type: str | None = None,
    ) -> tuple[tuple[KnowledgeBase, ...], int]:
        """分页列出候选资源范围内的知识库。"""
        ...

    async def list_ready_accessible(
        self,
        security_scope_ids: Sequence[str],
    ) -> tuple[KnowledgeBase, ...]:
        """列出候选资源范围内全部 READY 知识库，供自动检索选择。"""
        ...

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
        """按名称和状态筛选分页，并同时返回当前页记录和总数。"""
        ...

    async def update(
        self, security_scope_id: str, value: KnowledgeBase, expected_row_version: int
    ) -> bool:
        """按预期行版本乐观更新；版本已变化时返回 ``False``。"""
        ...


class DocumentRepository(Protocol):
    """文档持久化、过滤分页和知识库语种统计接口。"""

    async def add(self, security_scope_id: str, value: DocumentRecord) -> None:
        """在指定安全作用域中新增一条本地文档记录。"""
        ...

    async def get(self, security_scope_id: str, document_id: UUID) -> DocumentRecord | None:
        """按本地文档 ID 读取作用域内记录；不可见时返回 ``None``。"""
        ...

    async def get_many(
        self, security_scope_id: str, document_ids: Sequence[UUID]
    ) -> tuple[DocumentRecord, ...]:
        """批量返回作用域内可见的本地文档记录。"""
        ...

    async def get_accessible(
        self,
        security_scope_ids: Sequence[str],
        document_id: UUID,
    ) -> DocumentRecord | None:
        """从候选资源范围读取单个文档。"""
        ...

    async def get_many_accessible(
        self,
        security_scope_ids: Sequence[str],
        document_ids: Sequence[UUID],
    ) -> tuple[DocumentRecord, ...]:
        """从候选资源范围批量读取文档。"""
        ...

    async def get_by_engine_ids(
        self, security_scope_id: str, engine_document_ids: Sequence[str]
    ) -> tuple[DocumentRecord, ...]:
        """把一批外部引擎文档 ID 安全映射回当前作用域的本地记录。"""
        ...

    async def find_active_by_content_sha256(
        self,
        security_scope_id: str,
        knowledge_base_id: UUID,
        content_sha256: str,
    ) -> DocumentRecord | None:
        """查找同一安全范围、同一知识库中内容完全相同的未删除文档。

        ``content_sha256`` 是上传文件原始字节的 SHA-256，而不是文件名或文本向量。
        因此该查询只负责没有误判风险的“完全相同文件”去重，不会把内容相近的合同
        版本、模板或修订稿误认为同一文档。Repository 必须排除已软删除记录，允许
        用户在明确删除旧文档后重新上传相同内容。
        """
        ...

    async def language_counts(
        self,
        security_scope_id: str,
        knowledge_base_ids: Sequence[UUID],
    ) -> dict[UUID, dict[str | None, int]]:
        """按知识库和语言统计可检索文档数，用于规划跨语种检索路线。"""
        ...

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
        """在单个知识库内按状态、文件名或外部 ID 筛选并分页。"""
        ...

    async def update(
        self, security_scope_id: str, value: DocumentRecord, expected_row_version: int
    ) -> bool:
        """使用乐观版本更新文档；并发修改导致版本不符时返回 ``False``。"""
        ...


class DocumentVersionRepository(Protocol):
    """文档版本的 Scope 隔离持久化、编号分配和活动版本切换接口。

    所有方法都必须把 ``security_scope_id`` 写入查询条件。版本编号通过知识库和逻辑
    文档共同约束，调用方即使猜到 UUID 也不能读取或修改其他用户的历史版本。
    """

    async def add(self, security_scope_id: str, value: DocumentVersion) -> None:
        """新增一个不可变文件版本；同一文档版本号冲突时由数据库拒绝。"""
        ...

    async def get(self, security_scope_id: str, version_id: UUID) -> DocumentVersion | None:
        """读取作用域内单个版本，不可见或不存在时返回 ``None``。"""
        ...

    async def get_active(self, security_scope_id: str, document_id: UUID) -> DocumentVersion | None:
        """读取逻辑文档当前唯一 ACTIVE 版本。"""
        ...

    async def next_version_number(self, security_scope_id: str, document_id: UUID) -> int:
        """在事务级锁保护下分配下一正整数版本号，避免并发请求拿到同一编号。"""
        ...

    async def list(
        self,
        security_scope_id: str,
        document_id: UUID,
        *,
        page: int,
        page_size: int,
    ) -> tuple[tuple[DocumentVersion, ...], int]:
        """按版本号倒序分页列出一个逻辑文档的历史。"""
        ...

    async def update(self, security_scope_id: str, value: DocumentVersion) -> bool:
        """更新版本处理状态、引擎映射、统计和错误快照。"""
        ...

    async def activate(
        self,
        security_scope_id: str,
        *,
        document_id: UUID,
        version_id: UUID,
        activated_at: datetime,
    ) -> bool:
        """在同一事务中把旧 ACTIVE 退役并激活目标版本。"""
        ...


class IndexNodeRepository(Protocol):
    """Parent-Child 与摘要侧车索引的租户隔离持久化接口。"""

    async def replace_document_nodes(
        self,
        security_scope_id: str,
        document_id: UUID,
        nodes: Sequence[DocumentIndexNode],
    ) -> None:
        """原子替换一个文档的当前索引节点，重解析不会遗留旧 Chunk 映射。"""
        ...

    async def list_for_knowledge_bases(
        self,
        security_scope_id: str,
        knowledge_base_ids: Sequence[UUID],
        *,
        node_types: Sequence[IndexNodeType],
        limit: int,
    ) -> tuple[DocumentIndexNode, ...]:
        """列出当前范围、指定知识库且文档仍 READY 的节点，供摘要路线排序。"""
        ...

    async def find_parents_for_chunks(
        self,
        security_scope_id: str,
        knowledge_base_ids: Sequence[UUID],
        source_chunk_ids: Sequence[str],
        *,
        limit: int,
    ) -> tuple[DocumentIndexNode, ...]:
        """查找包含任一召回 Chunk 的 Parent 节点，用于补回相邻子块。"""
        ...


class EntityGraphRepository(Protocol):
    """实体索引和轻量关系图的隔离持久化与证据查询接口。"""

    async def replace_document_graph(
        self,
        security_scope_id: str,
        document_id: UUID,
        entities: Sequence[DocumentEntity],
        relations: Sequence[EntityRelation],
    ) -> None:
        """原子替换文档实体与关系，重解析不会保留旧内容产生的幽灵节点。"""
        ...

    async def search_evidence(
        self,
        security_scope_id: str,
        knowledge_base_ids: Sequence[UUID],
        query: str,
        *,
        limit: int,
    ) -> tuple[GraphEvidence, ...]:
        """查找查询中明确出现的实体/关系，并只返回原始 Chunk 证据引用。"""
        ...


class JobRepository(Protocol):
    """摄取任务持久化和 Worker 原子认领/续租接口。"""

    async def add(self, security_scope_id: str, value: IngestionJob) -> None:
        """在指定安全作用域中新增待处理的文档摄取任务。"""
        ...

    async def get(self, security_scope_id: str, job_id: UUID) -> IngestionJob | None:
        """读取作用域内任务；任务不存在或不可见时返回 ``None``。"""
        ...

    async def update(self, security_scope_id: str, value: IngestionJob) -> bool:
        """保存任务状态、进度或租约变化，并报告是否成功更新。"""
        ...

    async def find_active_for_document(
        self, security_scope_id: str, document_id: UUID
    ) -> IngestionJob | None:
        """查找文档尚未终止的任务，防止同一文档并行重复解析。"""
        ...

    async def latest_for_document(
        self, security_scope_id: str, document_id: UUID
    ) -> IngestionJob | None:
        """返回文档最近创建的任务，用于状态展示和失败重试判断。"""
        ...

    async def claim_next(
        self,
        *,
        worker_id: str,
        now: datetime,
        locked_until: datetime,
    ) -> IngestionJob | None:
        """原子认领一个可运行任务并写入租约；没有任务时返回 ``None``。"""
        ...

    async def renew_lease(
        self,
        security_scope_id: str,
        job_id: UUID,
        *,
        worker_id: str,
        locked_until: datetime,
    ) -> bool:
        """仅允许当前持有者延长任务租约；所有权丢失时返回 ``False``。"""
        ...

    async def active_staged_file_ids(self) -> frozenset[UUID]:
        """列出活动任务引用的暂存文件 ID，供清理器建立不可删除集合。"""
        ...


class IdempotencyRepository(Protocol):
    """幂等键原子抢占、成功响应落库和失败标记接口。"""

    async def reserve(self, value: IdempotencyRecord) -> IdempotencyReservation:
        """原子抢占幂等键，并说明本次应执行、复用响应还是等待在途请求。"""
        ...

    async def complete(
        self,
        value: IdempotencyRecord,
        *,
        response_status: int,
        response_json: dict[str, Any],
        resource_id: UUID | None,
    ) -> None:
        """保存成功状态、可复用响应和可选资源 ID，结束该幂等请求。"""
        ...

    async def fail(self, value: IdempotencyRecord) -> None:
        """把本次执行标记为失败，使后续请求可以按幂等策略重新处理。"""
        ...


class QueryRepository(Protocol):
    """在线请求、引用和反馈的审计持久化接口。"""

    async def add(self, security_scope_id: str, value: QueryRecord) -> None:
        """保存一次在线问答请求的初始审计记录。"""
        ...

    async def get(
        self,
        security_scope_id: str,
        request_id: UUID,
    ) -> QueryRecord | None:
        """按请求 ID 读取当前作用域记录；不可见或不存在时返回 ``None``。"""
        ...

    async def update(self, security_scope_id: str, value: QueryRecord) -> bool:
        """保存请求执行状态、答案摘要、缓存来源或取消标记。"""
        ...

    async def add_citations(
        self,
        security_scope_id: str,
        request_id: UUID,
        citations: Sequence[Citation],
    ) -> None:
        """把经校验的引用批量关联到指定请求，供后续解释和审计。"""
        ...

    async def list_citations(
        self,
        security_scope_id: str,
        request_id: UUID,
    ) -> tuple[Citation, ...]:
        """按稳定顺序返回请求的全部可见引用。"""
        ...

    async def get_citation(
        self,
        security_scope_id: str,
        citation_id: UUID,
    ) -> Citation | None:
        """读取单条引用；作用域不匹配时与不存在一样返回 ``None``。"""
        ...

    async def add_feedback(
        self,
        security_scope_id: str,
        value: FeedbackRecord,
    ) -> None:
        """保存用户对请求或引用的反馈，保留安全作用域用于隔离。"""
        ...
