"""服务配置模型、环境变量注入和跨组件契约校验。

YAML 文件描述配置结构以及敏感值对应的环境变量名，本模块再读取真实环境变量并
组装成不可变的强类型对象。调用方因此不需要散落地读取 ``os.environ``，且拼写错误、
多余字段、缺少密钥和模型维度不一致都会在进程启动时立即暴露。
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field, PositiveInt, model_validator


class StrictModel(BaseModel):
    """所有配置段的共同基类：禁止未知字段，并在创建后禁止原地修改。"""
    model_config = ConfigDict(extra="forbid", frozen=True)


class AppSection(StrictModel):
    """HTTP 服务自身的名称、监听地址、公开地址和请求大小限制。"""
    name: str = "rag-service"
    environment: str = "development"
    api_version: str = "v1"
    host: str = "0.0.0.0"  # noqa: S104 - container listener, publishing is deployment-scoped
    port: int = 8080
    public_base_url: str = "http://localhost:8080"
    max_request_body_bytes: int = 104_857_600
    max_query_chars: int = 8_000
    default_language: str = "zh-CN"


class IdentitySection(StrictModel):
    """临时直连身份配置：从请求头接收 Tenant ID 与 User ID，并派生主体范围。

    当前阶段由上游 Identity 或受控调用方直接提供两个不透明 ID，因此这里没有 JWT、
    公钥或登录会话配置。Tenant ID 决定租户共享资源候选范围，两项共同决定审计、
    缓存和幂等所用的调用主体范围。
    """

    mode: Literal["direct_user"] = "direct_user"
    tenant_header_name: Literal["X-Tenant-ID"] = "X-Tenant-ID"
    user_header_name: Literal["X-User-ID"] = "X-User-ID"
    max_tenant_id_length: int = Field(default=128, ge=1, le=256)
    max_user_id_length: int = Field(default=128, ge=1, le=256)


class DatabaseSection(StrictModel):
    """应用 PostgreSQL 的连接串、Schema、连接池和语句超时。"""
    dsn: str
    schema_name: str = "rag_app"
    pool_size: PositiveInt = 20
    max_overflow: int = 20
    pool_recycle_seconds: PositiveInt = 1800
    connect_timeout_seconds: PositiveInt = 5
    statement_timeout_seconds: PositiveInt = 15


class RedisSection(StrictModel):
    """Celery Broker 所用 Redis 逻辑库、Key 命名和连接恢复边界。

    一个物理 Redis Stack 同时承载缓存、RAGFlow 和 Celery，但本配置段只描述 DB 2
    的 Celery 职责。``celery_key_prefix`` 把 Kombu 创建的队列、未确认消息和锁统一
    放进可识别命名空间；两条队列则把耗时摄取触发与维护任务分开观察。逻辑 DB 和
    Key 前缀都不是权限隔离，生产安全仍依赖网络、凭据和部署边界。
    """
    url: str
    celery_key_prefix: str = Field(default="rag:celery:", min_length=1, max_length=128)
    ingestion_queue_name: str = Field(default="rag-ingestion", min_length=1, max_length=128)
    maintenance_queue_name: str = Field(
        default="rag-maintenance", min_length=1, max_length=128
    )
    broker_visibility_timeout_seconds: PositiveInt = 14_400
    broker_health_check_interval_seconds: PositiveInt = 30
    socket_connect_timeout_seconds: PositiveInt = 2
    socket_timeout_seconds: PositiveInt = 2

    @model_validator(mode="after")
    def validate_celery_namespaces(self) -> RedisSection:
        """拒绝含糊 Key 前缀或重名队列，防止不同职责重新混进同一列表。"""
        if not self.celery_key_prefix.endswith(":"):
            raise ValueError("Celery key prefix must end with ':'")
        if self.ingestion_queue_name == self.maintenance_queue_name:
            raise ValueError("Celery ingestion and maintenance queues must differ")
        return self


class RagFlowSection(StrictModel):
    """RAGFlow 地址、凭据、模型契约及不同操作的超时/重试限制。"""
    base_url: str
    api_key: str
    embedding_model: str
    vision_model: str
    vision_runtime_model: str
    embedding_dimension: PositiveInt
    contract_version: str = "v0.26.4"
    connect_timeout_seconds: PositiveInt = 3
    read_timeout_seconds: PositiveInt = 30
    upload_timeout_seconds: PositiveInt = 300
    retrieval_timeout_seconds: PositiveInt = 8
    max_attempts: PositiveInt = 2


class UploadSection(StrictModel):
    """文件暂存目录、容量、单文件上限、扩展名白名单和临时后缀。"""
    staging_dir: Path = Path("/var/lib/rag/uploads")
    uploading_suffix: str = ".uploading"
    ready_suffix: str = ".ready"
    retain_failed_seconds: PositiveInt = 86_400
    max_staging_bytes: PositiveInt = 10_737_418_240
    max_file_bytes: PositiveInt = 104_857_600
    allowed_extensions: tuple[str, ...]
    reject_double_extensions: bool = True


class JobsSection(StrictModel):
    """Worker 重试、租约、解析轮询以及 Celery 触发消息的寿命边界。

    PostgreSQL ``ingestion_jobs`` 是任务事实来源，Celery 消息只负责唤醒 Worker 去
    认领一行任务。因此周期触发消息必须设置较短过期时间：长文档占满 Worker 时，
    过期轮询不能在恢复后继续执行并制造无意义空查。业务 Job 本身不会因触发消息
    过期而丢失，下一次周期扫描仍会从 PostgreSQL 重新发现它。
    """
    default_max_attempts: PositiveInt = 2
    lease_seconds: PositiveInt = 120
    recovery_scan_interval_seconds: PositiveInt = 5
    dispatch_message_expires_seconds: PositiveInt = 15
    cleanup_message_expires_seconds: PositiveInt = 840
    parse_poll_initial_seconds: PositiveInt = 2
    parse_poll_max_seconds: PositiveInt = 15
    parse_deadline_seconds: PositiveInt = 1800
    cleanup_batch_size: PositiveInt = 200

    @model_validator(mode="after")
    def validate_dispatch_expiry(self) -> JobsSection:
        """确保一次轮询消息至少覆盖一个调度周期，避免正常抖动就被判定过期。"""
        if self.dispatch_message_expires_seconds <= self.recovery_scan_interval_seconds:
            raise ValueError(
                "dispatch message expiry must exceed the recovery scan interval"
            )
        return self


class AnswersSection(StrictModel):
    """问答总时限、SSE 心跳、历史消息上限和引用修复次数。"""
    request_deadline_seconds: PositiveInt = 150
    sse_heartbeat_seconds: PositiveInt = 15
    max_history_messages: PositiveInt = 12
    max_history_chars: PositiveInt = 20_000
    retry_generation_when_missing_citations: bool = True
    max_citation_repair_attempts: int = Field(default=1, ge=0, le=2)


class IdempotencySection(StrictModel):
    """幂等记录保留时间以及重复处理中请求的等待提示参数。"""
    ttl_seconds: PositiveInt = 86_400
    processing_wait_ms: PositiveInt = 500


class ObservabilitySection(StrictModel):
    """日志、审计留存和遥测开关；类型约束明确禁止保存 Prompt、上下文和原始身份。"""
    service_name: str = "rag-service"
    log_level: str = "INFO"
    store_redacted_query: bool = True
    store_prompt: Literal[False] = False
    store_context: Literal[False] = False
    query_record_retention_days: PositiveInt = 30
    log_security_scope_hash: bool = True
    log_raw_identity_claims: Literal[False] = False
    otel_enabled: bool = False


class LlmSection(StrictModel):
    """OpenAI-compatible 文本生成/查询改写模型的连接、协议差异和并发参数。

    ``omit_sampling_parameters`` 与 ``extra_body`` 用来吸收兼容端点之间的有限协议
    差异。Bootstrap 先发送标准请求，再记录端点实际需要的覆盖；业务代码不会按
    供应商名称或模型名称分支，调用方始终只依赖统一 LLM Port。
    """
    base_url: str
    api_key: str
    model: str
    rewrite_model: str
    connect_timeout_seconds: PositiveInt = 5
    read_timeout_seconds: PositiveInt = 120
    first_token_timeout_seconds: PositiveInt = 20
    max_attempts: PositiveInt = 2
    retry_base_delay_ms: PositiveInt = 250
    max_concurrency: PositiveInt = 4
    omit_sampling_parameters: bool = False
    max_output_tokens_field: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"
    extra_body: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_extra_body_boundary(self) -> LlmSection:
        """禁止附加 JSON 覆盖领域请求的核心字段。

        ``extra_body`` 只用于端点特有但不改变请求语义的开关，例如关闭思考模式。
        如果允许环境变量覆盖模型、消息、流式开关、输出格式或 Token 上限，调用者
        选择的 Profile、取消传播和结构化响应校验都会失效，因此配置加载阶段直接
        拒绝这些键，而不是等到第一条用户请求才失败。
        """
        protected_fields = {
            "model",
            "messages",
            "stream",
            "stream_options",
            "response_format",
            "max_tokens",
            "max_completion_tokens",
        }
        if protected_fields.intersection(self.extra_body):
            raise ValueError("LLM extra_body cannot override protected request fields")
        return self


class EmbeddingSection(StrictModel):
    """文本向量模型、固定维度、批大小、网络重试和并发参数。"""
    base_url: str
    api_key: str
    model: str
    dimension: PositiveInt
    connect_timeout_seconds: PositiveInt = 5
    read_timeout_seconds: PositiveInt = 60
    max_attempts: PositiveInt = 2
    retry_base_delay_ms: PositiveInt = 250
    max_concurrency: PositiveInt = 4
    batch_size: PositiveInt = 32


class VisionSection(StrictModel):
    """OpenAI-compatible 图片理解模型的独立通用连接契约。

    文档 OCR、视频抽帧理解与最终回答 LLM 即使共用同一供应商，也分别保留显式的
    URL、Key 和模型字段。这样启动探测可以准确指出哪项视觉职责不可用，Worker 也
    不会误用 RAGFlow 的文档 OCR 模型处理通用视频场景。
    """

    base_url: str
    api_key: str
    model: str
    connect_timeout_seconds: PositiveInt = 5
    read_timeout_seconds: PositiveInt = 120
    max_attempts: PositiveInt = 2


class MediaSection(StrictModel):
    """音视频预处理所用 ASR、FFmpeg 分段和视频抽帧边界。

    音视频不会直接交给 RAGFlow 猜测解析器。Worker 先用 FFmpeg 生成短 WAV 与有限
    JPEG 帧，再调用 OpenAI-compatible ASR 和独立媒体 Vision 模型，最后把带时间戳
    的 Markdown 交给 RAGFlow 建索引。关闭时允许凭据为空；开启时配置不完整会启动
    失败。
    """

    enabled: bool = False
    asr_base_url: str = ""
    asr_api_key: str = ""
    asr_model: str = ""
    ffmpeg_binary: str = "ffmpeg"
    ffprobe_binary: str = "ffprobe"
    segment_seconds: int = Field(default=60, ge=30, le=300)
    segment_overlap_seconds: int = Field(default=2, ge=0, le=30)
    frame_interval_seconds: int = Field(default=10, ge=10, le=600)
    max_video_frames: int = Field(default=20, ge=1, le=100)
    connect_timeout_seconds: PositiveInt = 5
    read_timeout_seconds: PositiveInt = 180
    process_timeout_seconds: PositiveInt = 1800
    max_attempts: PositiveInt = 2
    max_concurrency: PositiveInt = 2

    @model_validator(mode="after")
    def validate_enabled_configuration(self) -> MediaSection:
        """启用音视频时要求 ASR 地址、Key、模型和两个工具名全部存在。"""
        if self.segment_overlap_seconds >= self.segment_seconds:
            raise ValueError("media segment overlap must be shorter than the segment")
        if self.enabled and not all(
            (
                self.asr_base_url.strip(),
                self.asr_api_key.strip(),
                self.asr_model.strip(),
                self.ffmpeg_binary.strip(),
                self.ffprobe_binary.strip(),
            )
        ):
            raise ValueError(
                "enabled media processing requires ASR URL, API key, model and FFmpeg tools"
            )
        return self


class RerankSection(StrictModel):
    """可选重排服务、并发/重试边界及失败后继续使用原排序的固定降级策略。"""
    enabled: bool = False
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    timeout_seconds: PositiveInt = 8
    max_attempts: PositiveInt = 2
    max_concurrency: PositiveInt = 4
    failure_policy: Literal["use_unreranked_candidates_with_warning"] = (
        "use_unreranked_candidates_with_warning"
    )

    @model_validator(mode="after")
    def validate_enabled_configuration(self) -> RerankSection:
        """启用重排时要求 URL、API Key 和模型名同时存在。"""
        if self.enabled and not all((self.base_url, self.api_key, self.model)):
            raise ValueError(
                "enabled rerank requires RERANK_BASE_URL, RERANK_API_KEY and RERANK_MODEL"
            )
        return self


class ModelsSection(StrictModel):
    """聚合生成、向量、文档视觉、媒体视觉、音视频和重排模型链路。

    ``vision`` 专供 RAGFlow 图片与文档 OCR；``media_vision`` 只供 Worker 描述视频
    抽帧。媒体能力关闭时允许省略后者，开启时必须提供完整独立三元组，禁止从文档
    OCR 配置隐式复制凭据或模型名。
    """
    llm: LlmSection
    embedding: EmbeddingSection
    vision: VisionSection
    media_vision: VisionSection | None = None
    media: MediaSection = Field(default_factory=MediaSection)
    rerank: RerankSection

    @model_validator(mode="after")
    def validate_media_vision_configuration(self) -> ModelsSection:
        """音视频启用时要求独立媒体视觉模型，避免视频静默退回文档 OCR。"""
        if self.media.enabled and self.media_vision is None:
            raise ValueError("enabled media processing requires MEDIA_VISION configuration")
        return self


class ProfileSection(StrictModel):
    """版本化 Profile 种子文件、必需引用、运行时来源和冲突策略。"""
    seed_dir: Path
    # 仅覆盖自动上传 PDF 的默认选择；留空保留知识库默认和原路由。
    pdf_default_parser_ref: str | None = None
    seed_files: tuple[str, ...]
    required_refs: tuple[str, ...]
    runtime_source: Literal["database"] = "database"
    conflict_policy: Literal["fail"] = "fail"


class SemanticCacheSection(StrictModel):
    """Redis Search 向量索引的名称、版本、字段、HNSW 参数与命中阈值。"""
    key_prefix: str = "rag-cache:semantic:v1:"
    index_name: str = "rag_semantic_cache_v1"
    index_schema_version: PositiveInt = 1
    vector_field: str = "embedding"
    vector_data_type: Literal["FLOAT32"] = "FLOAT32"
    embedding_model: str
    embedding_dimension: PositiveInt
    distance_metric: Literal["cosine"] = "cosine"
    algorithm: Literal["HNSW"] = "HNSW"
    hnsw_m: PositiveInt = 16
    hnsw_ef_construction: PositiveInt = 200
    top_k: PositiveInt = 3
    similarity_threshold: float = Field(ge=0, le=1, default=0.95)
    security_scope_tag: str = "security_scope_id"
    revalidate_citations: Literal[True] = True

    @model_validator(mode="after")
    def validate_versioned_index(self) -> SemanticCacheSection:
        """要求索引名和键前缀显式携带版本，防止新旧 Schema 共用数据。"""
        if not self.index_name.endswith(f"_v{self.index_schema_version}"):
            raise ValueError("semantic cache index_name must end with its schema version")
        if f":v{self.index_schema_version}:" not in self.key_prefix:
            raise ValueError("semantic cache key_prefix must include its schema version")
        return self


class CacheSection(StrictModel):
    """精确/语义答案缓存总开关、地址、容量、TTL 和异常绕过策略。"""
    exact_enabled: bool = True
    semantic_enabled: bool = True
    url: str
    key_prefix: str = "rag-cache"
    ttl_seconds: PositiveInt = 3600
    max_answer_bytes: PositiveInt = 262_144
    bypass_on_error: Literal[True] = True
    semantic: SemanticCacheSection


class ServiceSettings(StrictModel):
    """完整服务配置，是各运行时组件唯一应依赖的配置入口。"""
    app: AppSection
    identity: IdentitySection
    database: DatabaseSection
    redis: RedisSection
    ragflow: RagFlowSection
    uploads: UploadSection
    jobs: JobsSection
    answers: AnswersSection = AnswersSection()
    models: ModelsSection
    profiles: ProfileSection
    cache: CacheSection
    idempotency: IdempotencySection = IdempotencySection()
    observability: ObservabilitySection = ObservabilitySection()
    build_commit: str = "development"
    build_time: str | None = None

    @model_validator(mode="after")
    def validate_embedding_contract(self) -> ServiceSettings:
        """校验 Gateway、RAGFlow 和语义缓存使用同一模型与向量维度。

        向量维度一旦不同，轻则所有相似度失真，重则 Redis/搜索引擎拒绝写入。因此
        这里采用“启动即失败”，绝不让不一致配置进入运行阶段。
        """
        model = self.models.embedding
        semantic = self.cache.semantic
        if model.model != semantic.embedding_model:
            raise ValueError("semantic cache and runtime embedding model names must match")
        if model.dimension != semantic.embedding_dimension:
            raise ValueError("semantic cache and runtime embedding dimensions must match")
        if self.ragflow.embedding_dimension != model.dimension:
            raise ValueError("RAGFlow and runtime embedding dimensions must match")
        if not self.ragflow.embedding_model.startswith(f"{model.model}@"):
            raise ValueError("RAGFlow embedding model must use the configured runtime model")
        if not self.ragflow.vision_model.startswith(
            f"{self.models.vision.model}@"
        ):
            raise ValueError("RAGFlow vision model must use the configured runtime model")
        if self.ragflow.vision_runtime_model != self.models.vision.model:
            raise ValueError("RAGFlow and runtime vision model names must match")
        # 直传 Tenant/User ID 没有签名校验，只允许开发/联调环境使用。生产环境必须
        # 先接入 Identity 签发的可信凭据，再替换该模式，避免客户端伪造租户或用户。
        if self.app.environment.lower() == "production":
            raise ValueError("direct_user identity mode is forbidden in production")
        return self


def _required_env(name: str) -> str:
    """读取必需环境变量，并把缺失或纯空白值转成清晰的启动错误。"""
    value = os.getenv(name)
    if value is None or not value.strip():
        raise ValueError(f"required environment variable is missing: {name}")
    return value.strip()


def _optional_env(name: str) -> str | None:
    """读取可选环境变量；不存在或纯空白统一表示未配置。"""
    value = os.getenv(name)
    return value.strip() if value and value.strip() else None


def _bool_env(name: str, default: bool = False) -> bool:
    """将常见布尔文本转成 ``bool``，拒绝模糊或拼错的值。"""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"environment variable {name} must be a boolean")


def _json_object_env(name: str) -> dict[str, Any]:
    """读取由模型探测器生成的 JSON Object 协议扩展，拒绝数组和无效 JSON。

    普通用户不需要手工填写这个字段。启动 Bootstrap 会先用标准 OpenAI 请求验证
    模型，只有模型确实需要关闭思考等扩展时，才把不含密钥的 JSON 写入忽略环境。
    在 Settings 层再次解析和限制对象类型，可以防止字符串配置覆盖请求核心结构。
    """
    raw = _required_env(name)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"environment variable {name} must contain valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"environment variable {name} must contain a JSON object")
    return value


def _max_output_tokens_field_env(name: str) -> Literal["max_tokens", "max_completion_tokens"]:
    """读取 Bootstrap 探测出的 Token 上限字段，并在 Settings 边界收窄类型。"""
    value = _required_env(name)
    if value not in {"max_tokens", "max_completion_tokens"}:
        raise ValueError(
            f"environment variable {name} must be max_tokens or max_completion_tokens"
        )
    return cast("Literal['max_tokens', 'max_completion_tokens']", value)


def _default_config_path() -> Path:
    """确定配置文件路径：优先使用环境变量，否则定位仓库内示例配置。"""
    return Path(
        os.getenv(
            "RAG_CONFIG_PATH",
            "rag-development-spec/configs/application.example.yaml",
        )
    )


def load_settings(path: Path | None = None) -> ServiceSettings:
    """读取 YAML、注入环境敏感值并构造完成全部校验的配置对象。

    YAML 中诸如 ``dsn_env`` 的值是“环境变量名称”而不是真实密码。先拆出各配置段
    只是为了后续映射清晰；最后构造 ``ServiceSettings`` 时，Pydantic 会递归检查类型、
    必填项、额外字段和所有自定义模型校验器。
    """
    config_path = (path or _default_config_path()).resolve()
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("application configuration must be a YAML object")

    # 为每个子系统建立局部别名，使下方 YAML → 强类型字段映射可逐项核对。
    identity_raw = raw["identity"]
    db_raw = raw["database"]
    redis_raw = raw["redis"]
    ragflow_raw = raw["ragflow"]
    uploads_raw = raw["uploads"]
    jobs_raw = raw["jobs"]
    answers_raw = raw["answers"]
    models_raw = raw["models"]
    llm_raw = models_raw["llm"]
    embedding_raw = models_raw["embedding"]
    vision_raw = models_raw["vision"]
    media_vision_raw = models_raw.get("media_vision", {})
    media_raw = models_raw.get("media", {})
    rerank_raw = models_raw["rerank"]
    profiles_raw = raw["profiles"]
    cache_raw = raw["cache"]
    semantic_raw = cache_raw["semantic"]
    idempotency_raw = raw["idempotency"]
    observability_raw = raw["observability"]

    # 媒体能力关闭时不读取媒体视觉凭据；开启时三个字段全部成为必填项。这里保持
    # 文档 OCR 与视频视觉的职责隔离，不根据供应商或相同 URL 猜测共享配置。
    media_enabled = _bool_env(media_raw["enabled_env"]) if media_raw else False
    media_vision = (
        VisionSection(
            base_url=_required_env(media_vision_raw["base_url_env"]),
            api_key=_required_env(media_vision_raw["api_key_env"]),
            model=_required_env(media_vision_raw["model_env"]),
            connect_timeout_seconds=media_vision_raw["connect_timeout_seconds"],
            read_timeout_seconds=media_vision_raw["read_timeout_seconds"],
            max_attempts=media_vision_raw["retries"]["max_attempts"],
        )
        if media_enabled
        else None
    )

    # Profile 路径可由部署环境覆盖；转换为绝对路径后不受进程工作目录影响。
    seed_dir = Path(os.getenv("PROFILE_SEED_DIR", str(config_path.parent))).resolve()
    # 部署环境与公开地址通过已命名的覆盖项注入；不改变版本化 Profile 的语义。
    app_raw = dict(raw["app"])
    for variable, field in (
        ("LAZYTT_RAG_ENVIRONMENT", "environment"),
        ("LAZYTT_RAG_PUBLIC_BASE_URL", "public_base_url"),
    ):
        if os.getenv(variable):
            app_raw[field] = os.environ[variable]
    return ServiceSettings(
        app=AppSection.model_validate(app_raw),
        identity=IdentitySection(
            mode=identity_raw["mode"],
            tenant_header_name=identity_raw["tenant_header_name"],
            user_header_name=identity_raw["user_header_name"],
            max_tenant_id_length=identity_raw["max_tenant_id_length"],
            max_user_id_length=identity_raw["max_user_id_length"],
        ),
        database=DatabaseSection(
            dsn=_required_env(db_raw["dsn_env"]),
            schema_name=db_raw["schema"],
            pool_size=int(os.getenv("LAZYTT_RAG_DB_POOL_SIZE", db_raw["pool_size"])),
            max_overflow=int(os.getenv("LAZYTT_RAG_DB_MAX_OVERFLOW", db_raw["max_overflow"])),
            pool_recycle_seconds=db_raw["pool_recycle_seconds"],
            connect_timeout_seconds=db_raw["connect_timeout_seconds"],
            statement_timeout_seconds=db_raw["statement_timeout_seconds"],
        ),
        redis=RedisSection(
            url=_required_env(redis_raw["url_env"]),
            celery_key_prefix=redis_raw["celery_key_prefix"],
            ingestion_queue_name=redis_raw["ingestion_queue_name"],
            maintenance_queue_name=redis_raw["maintenance_queue_name"],
            broker_visibility_timeout_seconds=redis_raw[
                "broker_visibility_timeout_seconds"
            ],
            broker_health_check_interval_seconds=redis_raw[
                "broker_health_check_interval_seconds"
            ],
            socket_connect_timeout_seconds=redis_raw["socket_connect_timeout_seconds"],
            socket_timeout_seconds=redis_raw["socket_timeout_seconds"],
        ),
        ragflow=RagFlowSection(
            base_url=_required_env(ragflow_raw["base_url_env"]),
            api_key=_required_env(ragflow_raw["api_key_env"]),
            embedding_model=_required_env(ragflow_raw["embedding_model_env"]),
            vision_model=_required_env(ragflow_raw["vision_model_env"]),
            vision_runtime_model=_required_env(ragflow_raw["vision_runtime_model_env"]),
            embedding_dimension=int(_required_env(ragflow_raw["embedding_dimension_env"])),
            contract_version=ragflow_raw["contract_version"],
            connect_timeout_seconds=ragflow_raw["connect_timeout_seconds"],
            read_timeout_seconds=ragflow_raw["read_timeout_seconds"],
            upload_timeout_seconds=ragflow_raw["upload_timeout_seconds"],
            retrieval_timeout_seconds=ragflow_raw["retrieval_timeout_seconds"],
            max_attempts=ragflow_raw["retries"]["max_attempts"],
        ),
        uploads=UploadSection(
            staging_dir=Path(os.getenv("UPLOAD_STAGING_DIR", uploads_raw["staging_dir"])),
            uploading_suffix=uploads_raw["uploading_suffix"],
            ready_suffix=uploads_raw["ready_suffix"],
            retain_failed_seconds=uploads_raw["retain_failed_seconds"],
            max_staging_bytes=uploads_raw["max_staging_bytes"],
            max_file_bytes=uploads_raw["max_file_bytes"],
            allowed_extensions=tuple(uploads_raw["allowed_extensions"]),
            reject_double_extensions=uploads_raw["reject_double_extensions"],
        ),
        jobs=JobsSection.model_validate(jobs_raw),
        answers=AnswersSection.model_validate(answers_raw),
        models=ModelsSection(
            llm=LlmSection(
                base_url=_required_env(llm_raw["base_url_env"]),
                api_key=_required_env(llm_raw["api_key_env"]),
                model=_required_env(llm_raw["model_env"]),
                rewrite_model=_required_env(llm_raw["rewrite_model_env"]),
                connect_timeout_seconds=llm_raw["connect_timeout_seconds"],
                read_timeout_seconds=llm_raw["read_timeout_seconds"],
                first_token_timeout_seconds=llm_raw["first_token_timeout_seconds"],
                max_attempts=llm_raw["retries"]["max_attempts"],
                retry_base_delay_ms=llm_raw["retries"]["base_delay_ms"],
                max_concurrency=llm_raw["max_concurrency"],
                omit_sampling_parameters=_bool_env(
                    llm_raw["omit_sampling_parameters_env"]
                ),
                max_output_tokens_field=_max_output_tokens_field_env(
                    llm_raw["max_output_tokens_field_env"]
                ),
                extra_body=_json_object_env(llm_raw["extra_body_json_env"]),
            ),
            embedding=EmbeddingSection(
                base_url=_required_env(embedding_raw["base_url_env"]),
                api_key=_required_env(embedding_raw["api_key_env"]),
                model=_required_env(embedding_raw["model_env"]),
                dimension=int(_required_env(embedding_raw["dimension_env"])),
                connect_timeout_seconds=embedding_raw["connect_timeout_seconds"],
                read_timeout_seconds=embedding_raw["read_timeout_seconds"],
                max_attempts=embedding_raw["retries"]["max_attempts"],
                retry_base_delay_ms=embedding_raw["retries"]["base_delay_ms"],
                max_concurrency=embedding_raw["max_concurrency"],
                batch_size=embedding_raw["batch_size"],
            ),
            vision=VisionSection(
                base_url=_required_env(vision_raw["base_url_env"]),
                api_key=_required_env(vision_raw["api_key_env"]),
                model=_required_env(vision_raw["model_env"]),
                connect_timeout_seconds=vision_raw["connect_timeout_seconds"],
                read_timeout_seconds=vision_raw["read_timeout_seconds"],
                max_attempts=vision_raw["retries"]["max_attempts"],
            ),
            media_vision=media_vision,
            media=(
                MediaSection(
                    enabled=media_enabled,
                    asr_base_url=_optional_env(media_raw["asr_base_url_env"]) or "",
                    asr_api_key=_optional_env(media_raw["asr_api_key_env"]) or "",
                    asr_model=_optional_env(media_raw["asr_model_env"]) or "",
                    ffmpeg_binary=media_raw["ffmpeg_binary"],
                    ffprobe_binary=media_raw["ffprobe_binary"],
                    segment_seconds=media_raw["segment_seconds"],
                    segment_overlap_seconds=media_raw.get("segment_overlap_seconds", 2),
                    frame_interval_seconds=media_raw["frame_interval_seconds"],
                    max_video_frames=media_raw["max_video_frames"],
                    connect_timeout_seconds=media_raw["connect_timeout_seconds"],
                    read_timeout_seconds=media_raw["read_timeout_seconds"],
                    process_timeout_seconds=media_raw["process_timeout_seconds"],
                    max_attempts=media_raw["retries"]["max_attempts"],
                    max_concurrency=media_raw["max_concurrency"],
                )
                if media_raw
                else MediaSection()
            ),
            rerank=RerankSection(
                enabled=_bool_env(rerank_raw["enabled_env"]),
                base_url=_optional_env(rerank_raw["base_url_env"]),
                api_key=_optional_env(rerank_raw["api_key_env"]),
                model=_optional_env(rerank_raw["model_env"]),
                timeout_seconds=rerank_raw["timeout_seconds"],
                max_attempts=rerank_raw["max_attempts"],
                max_concurrency=rerank_raw["max_concurrency"],
                failure_policy=rerank_raw["failure_policy"],
            ),
        ),
        profiles=ProfileSection(
            pdf_default_parser_ref=os.getenv("RAG_PDF_DEFAULT_PARSER", "").strip() or None,
            seed_dir=seed_dir,
            seed_files=tuple(profiles_raw["seed_files"]),
            required_refs=tuple(profiles_raw["required_refs"]),
            runtime_source=profiles_raw["runtime_source"],
            conflict_policy=profiles_raw["conflict_policy"],
        ),
        cache=CacheSection(
            exact_enabled=cache_raw["exact_enabled"],
            semantic_enabled=cache_raw["semantic_enabled"],
            url=_required_env(cache_raw["url_env"]),
            key_prefix=cache_raw["key_prefix"],
            ttl_seconds=cache_raw["ttl_seconds"],
            max_answer_bytes=cache_raw["max_answer_bytes"],
            bypass_on_error=cache_raw["bypass_on_error"],
            semantic=SemanticCacheSection(
                key_prefix=semantic_raw["key_prefix"],
                index_name=semantic_raw["index_name"],
                index_schema_version=semantic_raw["index_schema_version"],
                vector_field=semantic_raw["vector_field"],
                vector_data_type=semantic_raw["vector_data_type"],
                embedding_model=_required_env(semantic_raw["embedding_model_env"]),
                embedding_dimension=int(_required_env(semantic_raw["embedding_dimension_env"])),
                distance_metric=semantic_raw["distance_metric"],
                algorithm=semantic_raw["algorithm"],
                hnsw_m=semantic_raw["hnsw_m"],
                hnsw_ef_construction=semantic_raw["hnsw_ef_construction"],
                top_k=semantic_raw["top_k"],
                similarity_threshold=semantic_raw["similarity_threshold"],
                security_scope_tag=semantic_raw["security_scope_tag"],
                revalidate_citations=semantic_raw["revalidate_citations"],
            ),
        ),
        idempotency=IdempotencySection.model_validate(idempotency_raw),
        observability=ObservabilitySection.model_validate(observability_raw),
        build_commit=os.getenv("BUILD_COMMIT", "development"),
        build_time=_optional_env("BUILD_TIME"),
    )


@lru_cache(maxsize=1)
def get_settings() -> ServiceSettings:
    """加载并缓存唯一配置实例，避免每次依赖注入都重新读取文件和环境。"""
    return load_settings()


def reset_settings_cache() -> None:
    """清除配置缓存，主要供测试在修改环境变量后重新加载。"""
    get_settings.cache_clear()
