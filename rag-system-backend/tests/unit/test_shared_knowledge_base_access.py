"""共享知识库策略测试：租户共享、系统共享以及调用主体范围保留。"""

from __future__ import annotations

from rag_service.application.access import SharedKnowledgeBaseAccessPolicy
from rag_service.domain.models import KnowledgeBaseScopeType
from rag_service.security.context import SecurityContext, derive_system_resource_scope_id


def _principal(tenant_id: str, user_id: str) -> SecurityContext:
    return SecurityContext.create(
        subject_id=user_id,
        tenant_id=tenant_id,
        rag_scope_id=f"user:{user_id}",
    )


def test_users_in_one_tenant_share_resource_scope_but_keep_principal_scopes() -> None:
    first = _principal("tenant-a", "user-a")
    second = _principal("tenant-a", "user-b")
    other_tenant = _principal("tenant-b", "user-a")

    assert first.security_scope_id != second.security_scope_id
    assert first.tenant_resource_scope_id == second.tenant_resource_scope_id
    assert first.tenant_resource_scope_id != other_tenant.tenant_resource_scope_id


def test_default_policy_selects_tenant_and_global_resource_scopes() -> None:
    principal = _principal("tenant-a", "user-a")
    policy = SharedKnowledgeBaseAccessPolicy()

    assert policy.candidate_resource_scopes(principal) == (
        principal.tenant_resource_scope_id,
        derive_system_resource_scope_id(),
    )
    assert policy.resource_scope_for_creation(
        principal,
        KnowledgeBaseScopeType.TENANT,
    ) == principal.tenant_resource_scope_id
    assert policy.resource_scope_for_creation(
        principal,
        KnowledgeBaseScopeType.SYSTEM,
    ) == derive_system_resource_scope_id()


def test_resource_context_preserves_original_principal_scope() -> None:
    principal = _principal("tenant-a", "user-a")
    resource = principal.for_resource_scope(principal.tenant_resource_scope_id)

    assert resource.security_scope_id == principal.tenant_resource_scope_id
    assert resource.principal_scope_id == principal.security_scope_id
    assert resource.request_scope_id == principal.security_scope_id
