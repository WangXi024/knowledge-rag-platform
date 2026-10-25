"""安全边界测试：直连 Tenant/User ID 校验以及组合 Scope 的稳定隔离。"""

from __future__ import annotations

import pytest

from rag_service.security.context import derive_security_scope_id
from rag_service.security.direct_user import DirectIdentityValueError, DirectUserResolver
from rag_service.settings import IdentitySection


def _resolver() -> DirectUserResolver:
    """创建不访问网络的测试解析器。"""
    return DirectUserResolver(
        IdentitySection(
            max_tenant_id_length=16,
            max_user_id_length=16,
        )
    )


def test_security_scope_is_stable_and_separator_safe() -> None:
    first = derive_security_scope_id("tenant-a", "scope-a")
    second = derive_security_scope_id("tenant-a", "scope-a")
    ambiguous = derive_security_scope_id("tenant", "a\x1fscope-a")

    assert first == second
    assert len(first) == 64
    assert first != ambiguous


def test_direct_user_resolver_builds_stable_isolated_contexts() -> None:
    """同一租户用户组合保持稳定，改变任一维度都会进入新的数据范围。"""
    resolver = _resolver()

    first = resolver.resolve("tenant-a", "user-a")
    repeated = resolver.resolve(" tenant-a ", " user-a ")
    other_user = resolver.resolve("tenant-a", "user-b")
    other_tenant = resolver.resolve("tenant-b", "user-a")

    assert first.subject_id == "user-a"
    assert first.tenant_id == "tenant-a"
    assert first.rag_scope_id == "user:user-a"
    assert first.security_scope_id == repeated.security_scope_id
    assert first.security_scope_id != other_user.security_scope_id
    assert first.security_scope_id != other_tenant.security_scope_id
    assert first.token_id is None
    assert first.expires_at is None
    assert first.authentication_method == "direct_user"


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("tenant_id", "   ", "missing"),
        ("tenant_id", "tenant\nother", "control_character"),
        ("tenant_id", "x" * 17, "too_long"),
        ("user_id", "   ", "missing"),
        ("user_id", "user\nother", "control_character"),
        ("user_id", "x" * 17, "too_long"),
    ],
)
def test_direct_user_resolver_rejects_invalid_values(
    field: str,
    value: str,
    reason: str,
) -> None:
    """两个身份字段使用同一安全规则，并保留错误字段供 API 返回。"""
    tenant_id = value if field == "tenant_id" else "tenant-a"
    user_id = value if field == "user_id" else "user-a"
    with pytest.raises(DirectIdentityValueError, match=reason) as captured:
        _resolver().resolve(tenant_id, user_id)
    assert captured.value.field == field


def test_direct_user_ids_remain_case_sensitive() -> None:
    resolver = _resolver()
    assert resolver.resolve("tenant-a", "User-A").security_scope_id != resolver.resolve(
        "tenant-a", "user-a"
    ).security_scope_id
