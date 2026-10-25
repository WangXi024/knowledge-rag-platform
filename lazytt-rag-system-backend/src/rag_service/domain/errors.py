"""全服务统一的领域错误类型和对外错误码。

业务代码抛出 ``RagError`` 子类，API 层再把它稳定地转换成 HTTP 状态、错误码、
是否可重试和字段级详情。集中定义可避免各模块泄漏下游原始异常或返回互相矛盾的
状态码。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from http import HTTPStatus


@dataclass(slots=True)
class ErrorDetail:
    """错误的补充说明；``field`` 可定位到具体请求字段。"""
    reason: str
    field: str | None = None


@dataclass(slots=True)
class RagError(Exception):
    """所有预期业务错误的基类，也是 API 异常处理器识别错误的统一入口。"""
    code: str
    message: str
    http_status: int = HTTPStatus.INTERNAL_SERVER_ERROR
    retryable: bool = False
    details: list[ErrorDetail] = field(default_factory=list)

    def __str__(self) -> str:
        """生成适合日志阅读的“错误码: 消息”文本。"""
        return f"{self.code}: {self.message}"


class ResourceNotFoundError(RagError):
    """当前安全范围内找不到指定资源；不区分资源不存在还是属于其他租户。"""

    def __init__(self, code: str = "RAG-COMMON-004", message: str = "资源不存在") -> None:
        """用调用场景对应的错误码和消息构造不可重试的 404 响应。"""
        super().__init__(code, message, HTTPStatus.NOT_FOUND, False)


class ProfileConflictError(RagError):
    """同名同版本配置已存在，但内容摘要不同。"""

    def __init__(self, ref: str) -> None:
        """把冲突的 ``名称@版本`` 引用写入固定的 409 配置错误消息。"""
        super().__init__(
            "RAG-CONFIG-003",
            f"同名同版本 Profile 的内容校验值冲突：{ref}",
            HTTPStatus.CONFLICT,
            False,
        )


class ProfileMissingError(RagError):
    """启动所需的配置版本尚未导入或被禁用。"""

    def __init__(self, refs: list[str]) -> None:
        """列出全部缺失引用，并构造阻止服务就绪的 503 错误。"""
        super().__init__(
            "RAG-CONFIG-004",
            f"必需 Profile 未初始化或未启用：{', '.join(refs)}",
            HTTPStatus.SERVICE_UNAVAILABLE,
            False,
        )


class CacheIndexMismatchError(RagError):
    """已有 Redis 向量索引与当前模型维度或字段定义不兼容。"""

    def __init__(self, reason: str) -> None:
        """保留具体不兼容原因，并构造需要人工修复配置的 503 错误。"""
        super().__init__(
            "RAG-CONFIG-005",
            f"语义缓存索引定义不兼容：{reason}",
            HTTPStatus.SERVICE_UNAVAILABLE,
            False,
        )


class RagFlowError(RagError):
    """调用 RAGFlow 时可能出现的稳定错误分类。"""
    @classmethod
    def connection(cls) -> RagFlowError:
        """构造网络连接失败；通常可在服务恢复后重试。"""
        return cls(
            "RAG-RF-001",
            "无法连接 RAGFlow",
            HTTPStatus.SERVICE_UNAVAILABLE,
            True,
        )

    @classmethod
    def downstream(cls, *, retryable: bool = False) -> RagFlowError:
        """构造下游返回非成功业务结果的错误。"""
        return cls(
            "RAG-RF-002",
            "RAGFlow 请求失败",
            HTTPStatus.BAD_GATEWAY,
            retryable,
        )

    @classmethod
    def timeout(cls) -> RagFlowError:
        """构造超过 RAGFlow 调用时限的错误。"""
        return cls(
            "RAG-RF-003",
            "RAGFlow 请求超时",
            HTTPStatus.GATEWAY_TIMEOUT,
            True,
        )

    @classmethod
    def invalid_response(cls) -> RagFlowError:
        """构造响应 JSON 缺字段或类型不符合约定的错误。"""
        return cls(
            "RAG-RF-004",
            "RAGFlow 响应协议无效",
            HTTPStatus.BAD_GATEWAY,
            True,
        )

    @classmethod
    def unauthorized(cls) -> RagFlowError:
        """构造 RAGFlow API Key 被拒绝且不应盲目重试的错误。"""
        return cls(
            "RAG-RF-006",
            "RAGFlow API Key 无效",
            HTTPStatus.UNAUTHORIZED,
            False,
        )


class LlmError(RagError):
    """OpenAI-compatible 文本生成服务的稳定错误分类。"""
    @classmethod
    def unavailable(cls) -> LlmError:
        """构造模型服务不可连接或持续返回临时故障的错误。"""
        return cls(
            "RAG-LLM-001",
            "LLM 不可用",
            HTTPStatus.SERVICE_UNAVAILABLE,
            True,
        )

    @classmethod
    def timeout(cls) -> LlmError:
        """构造文本生成超过截止时间的错误。"""
        return cls(
            "RAG-LLM-002",
            "LLM 生成超时",
            HTTPStatus.GATEWAY_TIMEOUT,
            True,
        )

    @classmethod
    def invalid_response(cls) -> LlmError:
        """构造模型响应无法按 OpenAI-compatible 协议解析的错误。"""
        return cls(
            "RAG-LLM-003",
            "LLM 响应协议无效",
            HTTPStatus.BAD_GATEWAY,
            True,
        )


class EmbeddingError(RagError):
    """文本向量服务的稳定错误分类。"""
    @classmethod
    def unavailable(cls) -> EmbeddingError:
        """构造向量服务暂时不可用的错误。"""
        return cls(
            "RAG-CONFIG-006",
            "Embedding 服务不可用",
            HTTPStatus.SERVICE_UNAVAILABLE,
            True,
        )

    @classmethod
    def dimension_mismatch(cls, expected: int, actual: int) -> EmbeddingError:
        """构造向量长度与索引契约不一致的不可重试错误。"""
        return cls(
            "RAG-CONFIG-007",
            f"Embedding 维度不匹配：期望 {expected}，实际 {actual}",
            HTTPStatus.SERVICE_UNAVAILABLE,
            False,
        )

    @classmethod
    def invalid_response(cls) -> EmbeddingError:
        """构造向量响应结构或数值无效的错误。"""
        return cls(
            "RAG-CONFIG-008",
            "Embedding 响应协议无效",
            HTTPStatus.BAD_GATEWAY,
            True,
        )


class RerankError(RagError):
    """启动阶段验证真实 Cross-Encoder 服务失败时使用的稳定配置错误。"""

    @classmethod
    def preflight_failed(cls) -> RerankError:
        """阻止把错误 Key、模型或响应协议静默发布为已启用 Rerank。"""
        return cls(
            "RAG-CONFIG-009",
            "Rerank 服务启动验证失败",
            HTTPStatus.SERVICE_UNAVAILABLE,
            True,
        )


class JobLeaseLostError(RuntimeError):
    """Worker 已失去租约；必须回滚并停止，不得把其他持有者的任务标记失败。"""


class JobCancellationRequested(RuntimeError):
    """提交成功之前发现已存在取消请求，转入取消收尾而不是覆盖取消状态。"""
