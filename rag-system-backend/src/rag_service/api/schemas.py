"""HTTP API 的请求体、响应体和 OpenAPI 数据结构。

这些 Pydantic 模型是网络边界：进入服务的数据在这里检查类型、长度、格式和集合
约束，离开服务的数据也按固定结构序列化。领域对象不会直接暴露给客户端，因而内部
字段可演进且敏感字段不会被意外返回。
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ApiModel(BaseModel):
    """所有 API 模型的基类；拒绝客户端偷偷传入未声明字段。"""
    model_config = ConfigDict(extra="forbid")


class ErrorDetailSchema(ApiModel):
    """面向客户端的单条字段级错误详情。"""
    reason: str
    field: str | None = None


class ApiErrorSchema(ApiModel):
    """稳定错误码、可读消息、重试建议和详情列表。"""
    code: str = Field(pattern=r"^RAG-[A-Z]+-[0-9]{3}$")
    message: str
    retryable: bool
    details: list[ErrorDetailSchema] = Field(default_factory=list)


class ErrorResponse(ApiModel):
    """所有失败响应的统一外壳，并携带可追踪的请求 ID。"""
    request_id: UUID
    error: ApiErrorSchema


class HealthStatus(StrEnum):
    """整体或单项依赖的健康等级。"""
    OK = "ok"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class DependencyHealth(ApiModel):
    """一个外部依赖的状态、探测耗时和可选诊断消息。"""
    status: HealthStatus
    latency_ms: float | None = Field(default=None, ge=0)
    message: str | None = None


class HealthData(ApiModel):
    """服务整体健康状态及按名称索引的依赖快照。"""
    status: HealthStatus
    dependencies: dict[str, DependencyHealth] = Field(default_factory=dict)


class HealthResponse(ApiModel):
    """健康接口响应。"""
    request_id: UUID
    data: HealthData


class VersionData(ApiModel):
    """当前 API、构建和所适配 RAGFlow 契约的版本信息。"""
    api_version: str
    build_commit: str
    build_time: str | None = None
    ragflow_contract_version: str


class VersionResponse(ApiModel):
    """版本接口响应。"""
    request_id: UUID
    data: VersionData


class CapabilitiesData(ApiModel):
    """客户端可用的文件格式、Profile 和功能开关。"""
    file_extensions: list[str]
    max_file_bytes: int | None = None
    parser_profiles: list[str]
    chunk_profiles: list[str]
    retrieval_profiles: list[str]
    features: dict[str, bool]


class CapabilitiesResponse(ApiModel):
    """能力发现接口响应。"""
    request_id: UUID
    data: CapabilitiesData


class SuccessResponse(ApiModel):
    """供通用成功场景使用的请求 ID、数据和可选元信息外壳。"""
    request_id: UUID
    data: Any
    meta: dict[str, Any] | None = None


class PageMeta(ApiModel):
    """分页响应的页码、每页条数和总条数。"""
    page: int
    page_size: int
    total: int


class CreateKnowledgeBaseRequest(ApiModel):
    """创建知识库所需名称、可选外部 ID 和四类版本化 Profile。"""
    scope_type: Literal["TENANT", "SYSTEM"]
    external_id: str | None = Field(default=None, max_length=128)
    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2048)
    parser_profile: str = Field(max_length=128)
    chunk_profile: str = Field(max_length=128)
    embedding_profile: str = Field(max_length=128)
    retrieval_profile: str = Field(max_length=128)


class UpdateKnowledgeBaseRequest(ApiModel):
    """知识库局部更新请求；只传需要改变的字段。"""
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2048)
    parser_profile: str | None = Field(default=None, max_length=128)
    chunk_profile: str | None = Field(default=None, max_length=128)
    retrieval_profile: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def require_change(self) -> UpdateKnowledgeBaseRequest:
        """拒绝空 JSON 对象，因为它不会产生任何业务变化。"""
        if not self.model_fields_set:
            raise ValueError("at least one field is required")
        return self


class KnowledgeBaseData(ApiModel):
    """可安全返回给客户端的知识库视图。"""
    id: UUID
    scope_type: Literal["TENANT", "SYSTEM"]
    external_id: str | None = None
    name: str
    description: str | None = None
    status: str
    revision: int = Field(ge=0)
    parser_profile: str
    chunk_profile: str
    embedding_profile: str
    retrieval_profile: str
    document_count: int = Field(ge=0)
    reindex_required: bool = False
    created_at: datetime
    updated_at: datetime


class KnowledgeBaseResponse(ApiModel):
    """单个知识库响应。"""
    request_id: UUID
    data: KnowledgeBaseData


class KnowledgeBasePageResponse(ApiModel):
    """知识库列表及分页信息。"""
    request_id: UUID
    data: list[KnowledgeBaseData]
    meta: PageMeta


class DocumentData(ApiModel):
    """文档身份、当前活动版本、完整性摘要、解析统计和最近错误的公开视图。"""
    id: UUID
    knowledge_base_id: UUID
    external_id: str | None = None
    filename: str
    mime_type: str
    file_size_bytes: int = Field(ge=0)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    active_version_id: UUID | None = None
    active_version_number: int = Field(default=1, ge=1)
    document_revision: int = Field(default=1, ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)
    parser_profile: str
    chunk_profile: str
    status: str
    progress: float = Field(ge=0, le=1)
    chunk_count: int = Field(ge=0)
    token_count: int = Field(ge=0)
    last_error: ApiErrorSchema | None = None
    created_at: datetime
    updated_at: datetime
    indexed_at: datetime | None = None


class DocumentResponse(ApiModel):
    """单个文档响应。"""
    request_id: UUID
    data: DocumentData


class DocumentPageResponse(ApiModel):
    """文档列表及分页信息。"""
    request_id: UUID
    data: list[DocumentData]
    meta: PageMeta


class DocumentAcceptedData(ApiModel):
    """文件已接收但仍需异步处理时返回的文档 ID、任务 ID 和状态。"""
    document_id: UUID
    job_id: UUID
    status: str


class DocumentAcceptedResponse(ApiModel):
    """文档上传接收响应。"""
    request_id: UUID
    data: DocumentAcceptedData


class DocumentVersionData(ApiModel):
    """文档历史版本的文件身份、处理状态、真实执行模式和错误快照。

    响应不暴露 RAGFlow 物理文档 ID、暂存路径或安全 Scope。客户端通过稳定逻辑
    ``document_id`` 和 ``version_number`` 展示历史，通过 ``status`` 判断候选是否
    已发布、失败或可以回滚。
    """

    id: UUID
    document_id: UUID
    version_number: int = Field(ge=1)
    filename: str
    mime_type: str
    file_size_bytes: int = Field(ge=0)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    metadata: dict[str, Any] = Field(default_factory=dict)
    parser_profile: str
    chunk_profile: str
    status: str
    requested_mode: Literal["AUTO", "SAFE_REPLACE"]
    effective_mode: Literal["AUTO", "SAFE_REPLACE"]
    chunk_count: int = Field(ge=0)
    token_count: int = Field(ge=0)
    last_error: ApiErrorSchema | None = None
    created_at: datetime
    updated_at: datetime
    activated_at: datetime | None = None
    retired_at: datetime | None = None


class DocumentVersionResponse(ApiModel):
    """单个候选或历史版本响应。"""

    request_id: UUID
    data: DocumentVersionData


class DocumentVersionPageResponse(ApiModel):
    """文档版本历史及分页信息。"""

    request_id: UUID
    data: list[DocumentVersionData]
    meta: PageMeta


class DocumentVersionAcceptedData(ApiModel):
    """新版本已接收时返回稳定版本 ID、编号和后台 Job。"""

    document_id: UUID
    version_id: UUID
    version_number: int = Field(ge=1)
    job_id: UUID
    status: str
    effective_mode: Literal["AUTO", "SAFE_REPLACE"]


class DocumentVersionAcceptedResponse(ApiModel):
    """版本上传或回滚任务的 202 响应。"""

    request_id: UUID
    data: DocumentVersionAcceptedData


class JobData(ApiModel):
    """异步任务的类型、状态、进度、重试次数、关联资源和错误。"""
    id: UUID
    type: str
    status: str
    progress: float = Field(ge=0, le=1)
    attempt: int = Field(ge=0)
    max_attempts: int = Field(ge=1)
    knowledge_base_id: UUID | None = None
    document_id: UUID | None = None
    error: ApiErrorSchema | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class JobResponse(ApiModel):
    """单个异步任务响应。"""
    request_id: UUID
    data: JobData


class JobAcceptedData(ApiModel):
    """已创建后台任务的最小确认信息。"""
    job_id: UUID
    status: str


class JobAcceptedResponse(ApiModel):
    """后台任务接收响应。"""
    request_id: UUID
    data: JobAcceptedData


class ReparseDocumentRequest(ApiModel):
    """重新解析时可覆盖的解析与分块 Profile。"""
    parser_profile: str | None = Field(default=None, max_length=128)
    chunk_profile: str | None = Field(default=None, max_length=128)


class SourcePositionData(ApiModel):
    """一基页码；bbox 为 RAGFlow 左/右/上/下坐标，PDF 使用 72 DPI 页面单位。"""
    page: int | None = Field(default=None, ge=1)
    bbox: tuple[float, float, float, float] | None = None


class ChunkData(ApiModel):
    """解析文本块的内容、文档归属、原文位置和元数据。"""
    id: str
    document_id: UUID
    content: str
    positions: list[SourcePositionData] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ChunkPageResponse(ApiModel):
    """文本块列表及分页信息。"""
    request_id: UUID
    data: list[ChunkData]
    meta: PageMeta


class MetadataCondition(ApiModel):
    """一个受限的元数据比较条件，字段名与操作符均使用白名单格式。"""
    field: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    operator: str = Field(
        pattern=r"^(eq|ne|contains|not_contains|starts_with|empty|not_empty|gt|gte|lt|lte)$"
    )
    value: Any = None


class MetadataFilter(ApiModel):
    """最多 20 个元数据条件以及它们之间的 AND/OR 逻辑。"""
    logic: str = Field(pattern=r"^(and|or)$")
    conditions: list[MetadataCondition] = Field(max_length=20)


class RetrievalOptions(ApiModel):
    """检索返回数量及是否返回大字段内容/位置的选项。"""
    page_size: int = Field(default=10, ge=1, le=50)
    include_content: bool = True
    include_positions: bool = True


class RetrievalRequest(ApiModel):
    """检索范围、问题、过滤器、Profile 和返回选项。"""
    knowledge_base_ids: list[UUID] = Field(default_factory=list, max_length=20)
    document_ids: list[UUID] = Field(default_factory=list, max_length=100)
    query: str = Field(min_length=1, max_length=8000)
    metadata_filter: MetadataFilter | None = None
    # 默认指向带侧车相似度门槛的不可变 v6；调用方仍可显式传 @5 做回滚对照。
    retrieval_profile: str = Field(default="hybrid-structure-aware@6", max_length=128)
    options: RetrievalOptions = Field(default_factory=RetrievalOptions)

    @model_validator(mode="after")
    def unique_ids(self) -> RetrievalRequest:
        """拒绝重复知识库或文档 ID，避免重复检索和结果计数歧义。"""
        if len(set(self.knowledge_base_ids)) != len(self.knowledge_base_ids):
            raise ValueError("knowledge_base_ids must be unique")
        if len(set(self.document_ids)) != len(self.document_ids):
            raise ValueError("document_ids must be unique")
        return self


class RetrievalScores(ApiModel):
    """候选的综合、向量、关键词和重排分数。"""
    score: float
    vector_score: float | None = None
    term_score: float | None = None
    rerank_score: float | None = None


class RetrievalCandidateData(ApiModel):
    """面向客户端的单个检索候选及其来源和得分。"""
    chunk_id: str
    knowledge_base_id: UUID
    document_id: UUID
    document_name: str
    heading_path: list[str] = Field(default_factory=list)
    content: str
    positions: list[SourcePositionData]
    scores: RetrievalScores
    metadata: dict[str, Any]


class QueryPlanData(ApiModel):
    """语言、问题类型、查询变体、实际路线和三阶段动态预算的可解释轨迹。

    预算字段描述本次检索采用的上限，而不是最终返回数量。字段允许为空仅用于读取
    升级前的答案缓存；实时检索和升级后写入的新缓存都会提供完整正整数。
    """
    detected_language: Literal["zh-CN", "en-US"]
    variants: dict[str, str] = Field(default_factory=dict)
    knowledge_languages: list[Literal["zh-CN", "en-US"]] = Field(default_factory=list)
    retrieval_routes: list[str] = Field(default_factory=list)
    query_type: Literal["fact", "list", "comparison", "summary", "multi_hop"] | None = None
    retrieval_top_k: int | None = Field(default=None, ge=1)
    rerank_top_n: int | None = Field(default=None, ge=1)
    context_top_n: int | None = Field(default=None, ge=1)


class RetrievalData(ApiModel):
    """原始/生效查询、查询计划、候选集合、总数和降级警告。"""
    original_query: str
    effective_query: str
    query_plan: QueryPlanData
    candidates: list[RetrievalCandidateData]
    total: int = Field(ge=0)
    warnings: list[str] = Field(default_factory=list)


class RetrievalResponse(ApiModel):
    """检索接口响应。"""
    request_id: UUID
    data: RetrievalData


class ChatMessage(ApiModel):
    """问答请求携带的一条有限历史消息。"""
    role: str = Field(pattern=r"^(user|assistant)$")
    content: str = Field(max_length=10000)


class AnswerRequest(ApiModel):
    """知识库问答所需查询、历史、过滤器、Profile 和目标语言。"""
    knowledge_base_ids: list[UUID] = Field(default_factory=list, max_length=20)
    query: str = Field(min_length=1, max_length=8000)
    messages: list[ChatMessage] = Field(default_factory=list, max_length=12)
    metadata_filter: MetadataFilter | None = None
    retrieval_profile: str = Field(default="hybrid-structure-aware@6", max_length=128)
    # 公共 API 使用供应商无关 Profile；实际模型和协议来自通用环境与 Bootstrap。
    generation_profile: str = Field(default="answer-openai-compatible@4", max_length=128)
    response_language: Literal["zh-CN", "en-US"] = "zh-CN"

    @model_validator(mode="after")
    def unique_knowledge_base_ids(self) -> AnswerRequest:
        """拒绝重复知识库 ID，保持查询范围和缓存边界稳定。"""
        if len(set(self.knowledge_base_ids)) != len(self.knowledge_base_ids):
            raise ValueError("knowledge_base_ids must be unique")
        return self


class UsageData(ApiModel):
    """返回给调用方的模型 Token 用量。"""
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)


class CitationData(ApiModel):
    """答案引用的编号、来源文档、原文位置、摘录和相关分数。"""
    citation_id: UUID
    label: str = Field(pattern=r"^C[1-9][0-9]*$")
    knowledge_base_id: UUID
    document_id: UUID
    document_name: str
    heading_path: list[str] = Field(default_factory=list)
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    positions: list[SourcePositionData] = Field(default_factory=list)
    quote: str
    score: float | None = None


class AnswerMeta(ApiModel):
    """答案的附加执行信息，目前记录命中的缓存类型。"""
    cache_type: str = Field(pattern=r"^(none|exact|semantic)$")


class AnswerData(ApiModel):
    """完整答案、业务结果、查询规划、引用、警告和 Token 用量。"""
    answer: str
    outcome: str = Field(pattern=r"^(success|no_evidence|refused|cancelled)$")
    response_language: Literal["zh-CN", "en-US"]
    original_query: str
    effective_query: str
    query_rewritten: bool
    query_plan: QueryPlanData
    citations: list[CitationData]
    warnings: list[str]
    usage: UsageData | None = None
    meta: AnswerMeta


class AnswerResponse(ApiModel):
    """非流式问答接口响应。"""
    request_id: UUID
    data: AnswerData


class RequestRecordData(ApiModel):
    """可查询的脱敏请求审计记录。"""
    request_id: UUID
    status: str = Field(pattern=r"^(ACCEPTED|RUNNING|COMPLETED|FAILED|CANCELLED)$")
    outcome: str | None = Field(
        default=None,
        pattern=r"^(success|no_evidence|refused|cancelled)$",
    )
    original_query: str | None = None
    effective_query: str | None = None
    answer: str | None = None
    warnings: list[str] = Field(default_factory=list)
    created_at: datetime
    completed_at: datetime | None = None


class RequestRecordResponse(ApiModel):
    """请求审计记录响应。"""
    request_id: UUID
    data: RequestRecordData


class CitationResponse(ApiModel):
    """单条引用响应。"""
    request_id: UUID
    data: CitationData


class CitationListResponse(ApiModel):
    """指定请求产生的全部引用响应。"""
    request_id: UUID
    data: list[CitationData]


class CreateFeedbackRequest(ApiModel):
    """点赞/点踩、可选评分、标签和文字评价。"""
    request_id: UUID
    rating: int | None = Field(default=None)
    feedback_type: str = Field(
        pattern=(
            r"^(helpful|not_helpful|answer_incorrect|citation_incorrect|"
            r"should_answer|should_refuse|other)$"
        )
    )
    comment: str | None = Field(default=None, max_length=4000)
    corrected_answer: str | None = Field(default=None, max_length=20000)

    @model_validator(mode="after")
    def validate_rating(self) -> CreateFeedbackRequest:
        """显式保证评分在 1～5 之间；未评分时允许为 ``None``。"""
        if self.rating not in {-1, 1, None}:
            raise ValueError("rating must be -1, 1, or null")
        return self


class FeedbackData(ApiModel):
    """反馈创建后返回的稳定 ID、请求关联和创建时间。"""
    id: UUID
    request_id: UUID
    rating: int | None = None
    feedback_type: str
    created_at: datetime


class FeedbackResponse(ApiModel):
    """反馈提交响应。"""
    request_id: UUID
    data: FeedbackData
