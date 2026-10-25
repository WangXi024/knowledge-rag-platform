"""调用方身份到数据隔离上下文的转换。

外部 User ID 不会直接作为数据库隔离键；本模块把租户和用户范围组合成固定摘要。
所有 Repository 查询都必须携带该摘要，从而把跨用户访问在最底层查询条件中挡住。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from datetime import datetime


def _scope_digest(*parts: str) -> str:
    """用不可混淆分隔符生成固定长度的内部范围摘要。"""
    # 使用不可出现在普通标识符中的单元分隔符，避免 ("ab", "c") 与 ("a", "bc")
    # 一类简单拼接碰撞；摘要只用于稳定隔离，不用于还原原始身份。
    canonical = "\x1f".join(parts)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def derive_security_scope_id(tenant_id: str, rag_scope_id: str) -> str:
    """生成调用主体范围；保留函数名以兼容现有调用方。"""
    return _scope_digest("principal:v1", tenant_id, rag_scope_id)


def derive_tenant_resource_scope_id(tenant_id: str) -> str:
    """生成同一租户全部成员共享的知识资源范围。"""
    return _scope_digest("tenant:v1", tenant_id)


def derive_system_resource_scope_id() -> str:
    """生成当前 RAG 系统中所有租户共享的知识资源范围。"""
    return _scope_digest("system:v1")


@dataclass(frozen=True, slots=True)
class SecurityContext:
    """一次已识别请求携带的不可变安全上下文。

    ``security_scope_id`` 默认是调用主体范围；访问知识资源时
    ``for_resource_scope`` 只替换资源查询范围，并在 ``request_scope_id`` 中保留
    原调用主体，避免缓存、审计和幂等记录被错误地共享。
    """
    subject_id: str
    tenant_id: str
    rag_scope_id: str
    security_scope_id: str
    request_scope_id: str | None = None
    token_id: str | None = None
    expires_at: datetime | None = None
    authentication_method: str = "direct_user"

    @classmethod
    def create(
        cls,
        *,
        subject_id: str,
        tenant_id: str,
        rag_scope_id: str,
        token_id: str | None = None,
        expires_at: datetime | None = None,
        authentication_method: str = "direct_user",
    ) -> SecurityContext:
        """校验身份字段并派生 ``security_scope_id``，生成隔离上下文。

        空字符串和异常长身份会被拒绝，防止产生意外共享范围或把未受控大值带入
        日志与数据库查询。``token_id`` 和 ``expires_at`` 仅供未来可信令牌模式使用。
        """
        values = {
            "subject_id": subject_id,
            "tenant_id": tenant_id,
            "rag_scope_id": rag_scope_id,
        }
        if any(not value or not value.strip() for value in values.values()):
            raise ValueError("security claims must be non-empty strings")
        if len(tenant_id) > 256 or len(rag_scope_id) > 256 or len(subject_id) > 256:
            raise ValueError("security claim exceeds maximum length")
        principal_scope_id = derive_security_scope_id(tenant_id, rag_scope_id)
        return cls(
            subject_id=subject_id,
            tenant_id=tenant_id,
            rag_scope_id=rag_scope_id,
            security_scope_id=principal_scope_id,
            request_scope_id=principal_scope_id,
            token_id=token_id,
            expires_at=expires_at,
            authentication_method=authentication_method,
        )

    @property
    def principal_scope_id(self) -> str:
        """返回原始调用主体范围，即使当前上下文已切换到资源范围。"""
        return self.request_scope_id or self.security_scope_id

    @property
    def tenant_resource_scope_id(self) -> str:
        """返回当前 Tenant 对应的共享资源范围。"""
        return derive_tenant_resource_scope_id(self.tenant_id)

    def for_resource_scope(self, resource_scope_id: str) -> SecurityContext:
        """派生只用于资源 Repository/RAG 引擎调用的上下文。"""
        return replace(
            self,
            security_scope_id=resource_scope_id,
            request_scope_id=self.principal_scope_id,
        )
