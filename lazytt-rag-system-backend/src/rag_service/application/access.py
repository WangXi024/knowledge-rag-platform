"""可替换的知识库访问策略。

当前版本只有系统共享与同租户共享两条规则。所有应用服务只依赖本模块的协议，未来
接入完整身份、成员状态或知识库多对多 ACL 时可以替换实现，而不改检索和文档流程。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from rag_service.domain.errors import ResourceNotFoundError
from rag_service.domain.models import KnowledgeBase, KnowledgeBaseScopeType
from rag_service.security.context import (
    SecurityContext,
    derive_system_resource_scope_id,
)


class KnowledgeBaseAccessPolicy(Protocol):
    """知识库读取与创建目标范围的策略端口。"""

    def candidate_resource_scopes(self, principal: SecurityContext) -> tuple[str, ...]:
        """返回进入更细权限判断前允许查询的候选资源范围。"""
        ...

    def resource_scope_for_creation(
        self,
        principal: SecurityContext,
        scope_type: KnowledgeBaseScopeType,
    ) -> str:
        """返回新知识库的资源归属范围。"""
        ...

    async def filter_readable(
        self,
        principal: SecurityContext,
        knowledge_bases: Sequence[KnowledgeBase],
    ) -> tuple[KnowledgeBase, ...]:
        """执行最终知识库可读判断；未来 ACL 在此扩展。"""
        ...


class SharedKnowledgeBaseAccessPolicy:
    """当前系统共享 + 同租户共享的默认访问策略。"""

    def candidate_resource_scopes(self, principal: SecurityContext) -> tuple[str, ...]:
        return (
            principal.tenant_resource_scope_id,
            derive_system_resource_scope_id(),
        )

    def resource_scope_for_creation(
        self,
        principal: SecurityContext,
        scope_type: KnowledgeBaseScopeType,
    ) -> str:
        if scope_type == KnowledgeBaseScopeType.TENANT:
            return principal.tenant_resource_scope_id
        if scope_type == KnowledgeBaseScopeType.SYSTEM:
            return derive_system_resource_scope_id()
        raise ValueError(f"unsupported knowledge base scope type: {scope_type}")

    async def filter_readable(
        self,
        principal: SecurityContext,
        knowledge_bases: Sequence[KnowledgeBase],
    ) -> tuple[KnowledgeBase, ...]:
        allowed = set(self.candidate_resource_scopes(principal))
        return tuple(item for item in knowledge_bases if item.security_scope_id in allowed)

    async def require_readable(
        self,
        principal: SecurityContext,
        knowledge_base: KnowledgeBase | None,
    ) -> KnowledgeBase:
        """对不可见和不存在资源统一返回 404。"""
        if knowledge_base is None:
            raise ResourceNotFoundError("RAG-KB-001", "知识库不存在")
        readable = await self.filter_readable(principal, (knowledge_base,))
        if not readable:
            raise ResourceNotFoundError("RAG-KB-001", "知识库不存在")
        return readable[0]


class ManagementAccessPolicy(Protocol):
    """内部管理入口的可替换授权策略。"""

    async def require_access(self, principal: SecurityContext, operation: str) -> None:
        """无权时抛出领域错误。"""
        ...


class TrustedNetworkManagementAccessPolicy:
    """当前阶段依赖内部网络边界，暂不实施管理员或角色鉴权。"""

    async def require_access(self, principal: SecurityContext, operation: str) -> None:
        del principal, operation
