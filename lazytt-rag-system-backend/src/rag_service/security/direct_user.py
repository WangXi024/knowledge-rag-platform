"""开发联调阶段的直接 Tenant ID 与 User ID 解析器。

该解析器只负责把 ``X-Tenant-ID`` 和 ``X-User-ID`` 转成稳定的调用主体上下文，不
证明调用者确实拥有这些身份。它因此只能用于当前开发/联调阶段；生产接入 Identity
后，应由已经验签的令牌 Claims 调用同一个 ``SecurityContext`` 派生流程。
"""

from __future__ import annotations

import unicodedata

from rag_service.security.context import SecurityContext
from rag_service.settings import IdentitySection


class DirectIdentityValueError(ValueError):
    """携带出错身份字段的安全校验异常，供 API 精确指出错误请求头。

    ``field`` 只可能是 ``tenant_id`` 或 ``user_id``，``reason`` 只描述格式原因，
    不保存也不回显原始身份值，避免日志和错误响应泄露上游标识。
    """

    def __init__(self, field: str, reason: str) -> None:
        super().__init__(reason)
        self.field = field
        self.reason = reason


class DirectUserResolver:
    """校验不透明 Tenant/User ID，并为每个组合派生独立的调用主体范围。"""

    def __init__(self, settings: IdentitySection) -> None:
        """保存不可变的直接身份字段校验配置。"""
        self._settings = settings

    @staticmethod
    def _normalize(value: str, *, field: str, maximum_length: int) -> str:
        """规范化单个身份字段，并拒绝会造成范围混淆或日志污染的输入。

        身份 ID 被视为 Identity 生成的不透明字符串，所以不会强制 UUID、邮箱格式或
        小写化。只去除 HTTP 传输可能带来的首尾空白；控制字符和超长值在进入任何
        Repository、缓存或下游服务前就会被拒绝。
        """
        normalized = value.strip()
        if not normalized:
            raise DirectIdentityValueError(field, "missing")
        if len(normalized) > maximum_length:
            raise DirectIdentityValueError(field, "too_long")
        if any(unicodedata.category(character) == "Cc" for character in normalized):
            raise DirectIdentityValueError(field, "control_character")
        return normalized

    def resolve(self, raw_tenant_id: str, raw_user_id: str) -> SecurityContext:
        """解析两个请求头，并使用租户和用户的组合生成调用主体上下文。

        返回的 ``security_scope_id`` 最终由请求 Tenant ID 和 ``user:<User ID>``
        共同计算。同一个 User ID 出现在两个租户时会得到不同主体 Scope；知识库和
        文档随后切换到 TENANT/SYSTEM 资源 Scope，同一租户成员因此共享租户库。
        """
        tenant_id = self._normalize(
            raw_tenant_id,
            field="tenant_id",
            maximum_length=self._settings.max_tenant_id_length,
        )
        user_id = self._normalize(
            raw_user_id,
            field="user_id",
            maximum_length=self._settings.max_user_id_length,
        )

        rag_scope_id = f"user:{user_id}"
        return SecurityContext.create(
            subject_id=user_id,
            tenant_id=tenant_id,
            rag_scope_id=rag_scope_id,
            authentication_method="direct_user",
        )
