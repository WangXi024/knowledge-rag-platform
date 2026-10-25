"""运行时就绪测试：关键/可降级依赖汇总、索引不匹配和暂存写探针。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from rag_service.api.schemas import DependencyHealth, HealthStatus
from rag_service.infrastructure.health import RuntimeReadinessProbe, _staging_write_probe


def _dependency(
    status: HealthStatus = HealthStatus.OK,
    message: str | None = None,
) -> DependencyHealth:
    return DependencyHealth(status=status, message=message)


def _probe_with_results(
    *,
    cache: DependencyHealth,
    ragflow: DependencyHealth | None = None,
) -> RuntimeReadinessProbe:
    probe = object.__new__(RuntimeReadinessProbe)
    probe._check_database = AsyncMock(return_value=_dependency())  # type: ignore[method-assign]
    probe._check_redis = AsyncMock(return_value=_dependency())  # type: ignore[method-assign]
    probe._check_cache = AsyncMock(return_value=cache)  # type: ignore[method-assign]
    probe._check_profiles = AsyncMock(return_value=_dependency())  # type: ignore[method-assign]
    probe._check_staging = AsyncMock(return_value=_dependency())  # type: ignore[method-assign]
    probe._check_ragflow = AsyncMock(  # type: ignore[method-assign]
        return_value=ragflow or _dependency()
    )
    probe._check_models = AsyncMock(return_value=_dependency())  # type: ignore[method-assign]
    probe._app_redis = object()  # type: ignore[assignment]
    return probe


@pytest.mark.asyncio
async def test_cache_connection_failure_only_degrades_readiness() -> None:
    probe = _probe_with_results(
        cache=_dependency(HealthStatus.UNAVAILABLE, "cache_unavailable"),
    )

    result = await probe.check()

    assert result.status == HealthStatus.DEGRADED
    assert result.dependencies["cache_redis"].message == "cache_unavailable"


@pytest.mark.asyncio
async def test_cache_index_mismatch_and_required_failure_fail_readiness() -> None:
    index_mismatch = _probe_with_results(
        cache=_dependency(HealthStatus.UNAVAILABLE, "cache_index_mismatch"),
    )
    ragflow_down = _probe_with_results(
        cache=_dependency(),
        ragflow=_dependency(HealthStatus.UNAVAILABLE, "dependency_check_failed"),
    )

    assert (await index_mismatch.check()).status == HealthStatus.UNAVAILABLE
    assert (await ragflow_down.check()).status == HealthStatus.UNAVAILABLE


def test_staging_probe_writes_and_removes_temporary_file(tmp_path: Path) -> None:
    staging = tmp_path / "uploads"

    assert _staging_write_probe(staging) is True
    assert staging.is_dir()
    assert list(staging.iterdir()) == []


def test_model_health_accepts_missing_optional_models_endpoint() -> None:
    """端点真实能力已在启动时验证，404/405 不应让可用服务反复掉出就绪状态。"""
    required = {"synthetic-chat-model"}

    assert RuntimeReadinessProbe._models_response_is_healthy(
        httpx.Response(404), required
    )
    assert RuntimeReadinessProbe._models_response_is_healthy(
        httpx.Response(405), required
    )


def test_model_health_validates_available_list_and_rejects_auth_failure() -> None:
    """提供清单的端点必须包含配置模型，认证错误不能被兼容逻辑掩盖。"""
    required = {"synthetic-chat-model"}

    assert RuntimeReadinessProbe._models_response_is_healthy(
        httpx.Response(200, json={"data": [{"id": "synthetic-chat-model"}]}),
        required,
    )
    assert not RuntimeReadinessProbe._models_response_is_healthy(
        httpx.Response(200, json={"data": [{"id": "different-model"}]}),
        required,
    )
    assert not RuntimeReadinessProbe._models_response_is_healthy(
        httpx.Response(401), required
    )
