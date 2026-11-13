"""原生图谱管理 HTTP 契约测试，覆盖隔离头、受控配置与脱敏响应。"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient

from rag_service.api.app import create_app
from rag_service.domain.graph import GraphConfiguration, GraphState, GraphStatus
from rag_service.security.context import SecurityContext
from rag_service.settings import ServiceSettings


class GraphApiFake:
    """仅替代应用服务，保留 FastAPI 认证、请求校验和响应投影的真实行为。"""

    def __init__(self) -> None:
        """建立含内部引擎 ID 的状态，检查公开 API 不会意外序列化它。"""
        self.kb_id = uuid4()
        self.calls: list[str] = []
        self.state = GraphState("a" * 64, self.kb_id, engine_task_id="private-engine-id")

    async def get(self, security: SecurityContext, kb_id: UUID) -> GraphState:
        """记录已认证的操作，UUID 映射与授权已由应用服务单测覆盖。"""
        assert security.tenant_id == "fixture-tenant" and kb_id == self.kb_id
        self.calls.append("get")
        return self.state

    async def configure(
        self, security: SecurityContext, kb_id: UUID, configuration: GraphConfiguration
    ) -> GraphState:
        """保存经白名单 Schema 校验后的独立开关。"""
        await self.get(security, kb_id)
        self.state.configuration = configuration
        self.state.status = GraphStatus.NOT_BUILT
        return self.state

    async def build(self, security: SecurityContext, kb_id: UUID) -> GraphState:
        """构建响应仅表示接受，不伪装已经就绪。"""
        await self.get(security, kb_id)
        self.state.status = GraphStatus.QUEUED
        self.state.job_id = uuid4()
        return self.state

    async def cancel(self, security: SecurityContext, kb_id: UUID) -> GraphState:
        """取消响应保留待确认状态。"""
        await self.get(security, kb_id)
        self.state.status = GraphStatus.CANCELLING
        return self.state


def client_for(settings: ServiceSettings, service: Any) -> TestClient:
    """创建无需连接数据库的真实 HTTP 测试客户端。"""
    return TestClient(create_app(settings, graph_service=service))


def test_graph_requires_identity_before_calling_service(service_settings: ServiceSettings) -> None:
    """没有身份头的请求不能访问图谱服务。"""
    service = GraphApiFake()
    with client_for(service_settings, service) as client:
        response = client.get(f"/v1/management/knowledge-bases/{service.kb_id}/graph")
    assert response.status_code == 400
    assert not service.calls


def test_graph_config_and_commands_are_public_safe(service_settings: ServiceSettings) -> None:
    """四个管理操作返回稳定状态，内部 Scope 和引擎任务标识均不泄漏。"""
    service = GraphApiFake()
    headers = {"X-Tenant-ID": "fixture-tenant", "X-User-ID": "fixture-user"}
    path = f"/v1/management/knowledge-bases/{service.kb_id}/graph"
    with client_for(service_settings, service) as client:
        response = client.get(path, headers=headers)
        assert response.status_code == 200
        assert response.json()["data"]["status"] == "DISABLED"
        for private_field in ("security_scope_id", "engine_task_id", "private-engine-id"):
            assert private_field not in response.text
        response = client.put(path + "/configuration", headers=headers, json={
            "enabled": True, "profile_ref": "native-graph-light@1",
            "auto_build": False, "retrieval_enabled": False,
        })
        assert response.status_code == 200
        assert response.json()["data"]["configuration"]["retrieval_enabled"] is False
        response = client.post(path + "/build", headers=headers)
        assert response.status_code == 202
        assert response.json()["data"]["status"] == "QUEUED"
        response = client.post(path + "/cancel", headers=headers)
        assert response.status_code == 202
        assert response.json()["data"]["status"] == "CANCELLING"


def test_graph_rejects_arbitrary_upstream_parameters(service_settings: ServiceSettings) -> None:
    """客户端不能替换策略内容、传入模型密钥或注入任意原生构图 kwargs。"""
    service = GraphApiFake()
    path = f"/v1/management/knowledge-bases/{service.kb_id}/graph/configuration"
    headers = {"X-Tenant-ID": "fixture-tenant", "X-User-ID": "fixture-user"}
    with client_for(service_settings, service) as client:
        for payload in (
            {"enabled": True, "profile_ref": "untrusted@99"},
            {"enabled": True, "llm_id": "untrusted"},
            {"enabled": True, "parser_config": {"graphrag": {"method": "general"}}},
        ):
            assert client.put(path, headers=headers, json=payload).status_code == 400
    assert not service.calls
