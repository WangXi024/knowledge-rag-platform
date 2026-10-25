"""系统 API 测试：存活/就绪、请求 ID、版本和能力发现。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from rag_service.api.app import create_app
from rag_service.api.schemas import HealthData, HealthStatus
from rag_service.settings import ServiceSettings


class HealthyProbe:
    async def check(self) -> HealthData:
        return HealthData(status=HealthStatus.OK)


def test_liveness_is_public_and_propagates_request_id(
    service_settings: ServiceSettings,
) -> None:
    app = create_app(
        service_settings,
        readiness_probe=HealthyProbe(),
    )
    request_id = "19d1f2e7-99cd-43b9-a25f-0ee8eec5706d"

    with TestClient(app) as client:
        response = client.get("/health/live", headers={"X-Request-ID": request_id})

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == request_id
    assert response.json() == {
        "request_id": request_id,
        "data": {"status": "ok", "dependencies": {}},
    }


def test_invalid_request_id_uses_safe_error_shape(
    service_settings: ServiceSettings,
) -> None:
    app = create_app(service_settings)

    with TestClient(app) as client:
        response = client.get("/health/live", headers={"X-Request-ID": "not-a-uuid"})

    assert response.status_code == 400
    payload = response.json()
    assert payload["request_id"] == response.headers["X-Request-ID"]
    assert payload["error"]["code"] == "RAG-COMMON-001"
    assert payload["error"]["details"] == [{"reason": "invalid_uuid", "field": "X-Request-ID"}]


def test_version_is_public_for_deployment_diagnostics(
    service_settings: ServiceSettings,
) -> None:
    app = create_app(service_settings)

    with TestClient(app) as client:
        response = client.get("/v1/version")

    assert response.status_code == 200
    assert response.json()["data"] == {
        "api_version": "v1",
        "build_commit": "test-commit",
        "build_time": "2026-07-27T00:00:00Z",
        "ragflow_contract_version": "v0.26.4",
    }


def test_default_readiness_fails_closed(service_settings: ServiceSettings) -> None:
    app = create_app(service_settings)

    with TestClient(app) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "RAG-COMMON-012"


def test_capabilities_come_from_configured_profiles(
    service_settings: ServiceSettings,
) -> None:
    app = create_app(service_settings)

    with TestClient(app) as client:
        response = client.get("/v1/capabilities")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["file_extensions"] == [".pdf", ".txt", ".md"]
    assert "deepdoc-default@1" in data["parser_profiles"]
    assert "json-default@1" in data["parser_profiles"]
    assert "general-512@1" in data["chunk_profiles"]
    assert "general-256@2" in data["chunk_profiles"]
    assert "hybrid-default@1" in data["retrieval_profiles"]
    assert data["features"]["semantic_cache"] is True
    assert data["features"]["rerank"] is False
    assert data["features"]["retrieval_vector_bm25_rrf"] is True
    assert data["features"]["identity_mode_direct_tenant_user"] is True
    assert data["features"]["authentication_enabled"] is False


def test_business_openapi_declares_tenant_and_user_headers_required(
    service_settings: ServiceSettings,
) -> None:
    """Swagger 必须把两个身份 Header 都标为必填，避免联调方漏传租户。"""
    operation = create_app(service_settings).openapi()["paths"][
        "/v1/management/knowledge-bases"
    ]["get"]
    identity_headers = {
        parameter["name"]: parameter["required"]
        for parameter in operation["parameters"]
        if parameter["in"] == "header"
    }

    assert identity_headers["X-Tenant-ID"] is True
    assert identity_headers["X-User-ID"] is True
