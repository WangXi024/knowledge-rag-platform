"""图谱配置、状态、构建和取消路由，实际授权与调度由应用服务负责。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Request

from rag_service.api.graph_schemas import (
    GraphConfigurationData,
    GraphStateData,
    GraphStateResponse,
)
from rag_service.application.graph import GraphService
from rag_service.domain.graph import GraphConfiguration, GraphState
from rag_service.security.context import SecurityContext


def _response(request: Request, state: GraphState) -> GraphStateResponse:
    """把内部状态投影为不含安全摘要和引擎标识的响应。"""
    request_id = getattr(request.state, "request_id", None)
    return GraphStateResponse(
        request_id=request_id if isinstance(request_id, UUID) else uuid4(),
        data=GraphStateData.from_domain(state),
    )


def install_graph_routes(
    app: FastAPI,
    *,
    authenticate: Callable[..., Any],
    get_graph_service: Callable[[], AsyncIterator[GraphService]],
) -> None:
    """安装知识库管理图谱接口；没有接口暴露任意 Dataset ID 或引擎配置。"""
    service_dependency = Depends(get_graph_service)
    security_dependency = Depends(authenticate)

    @app.get(
        "/v1/management/knowledge-bases/{knowledge_base_id}/graph",
        response_model=GraphStateResponse,
        tags=["Graph"],
    )
    async def get_graph(
        request: Request,
        knowledge_base_id: UUID,
        security: SecurityContext = security_dependency,
        service: GraphService = service_dependency,
    ) -> GraphStateResponse:
        """查询当前配置和图谱状态，不触发构图或模型费用。"""
        return _response(request, await service.get(security, knowledge_base_id))

    @app.put(
        "/v1/management/knowledge-bases/{knowledge_base_id}/graph/configuration",
        response_model=GraphStateResponse,
        tags=["Graph"],
    )
    async def configure_graph(
        request: Request,
        knowledge_base_id: UUID,
        value: GraphConfigurationData,
        security: SecurityContext = security_dependency,
        service: GraphService = service_dependency,
    ) -> GraphStateResponse:
        """配置知识库图谱；自动构建开启时仅登记任务，不等待模型执行。"""
        state = await service.configure(
            security, knowledge_base_id, GraphConfiguration(**value.model_dump())
        )
        return _response(request, state)

    @app.post(
        "/v1/management/knowledge-bases/{knowledge_base_id}/graph/build",
        response_model=GraphStateResponse,
        status_code=202,
        tags=["Graph"],
    )
    async def build_graph(
        request: Request,
        knowledge_base_id: UUID,
        security: SecurityContext = security_dependency,
        service: GraphService = service_dependency,
    ) -> GraphStateResponse:
        """请求构建、重建或失败重试，相同知识库的在途请求合并。"""
        return _response(request, await service.build(security, knowledge_base_id))

    @app.post(
        "/v1/management/knowledge-bases/{knowledge_base_id}/graph/cancel",
        response_model=GraphStateResponse,
        status_code=202,
        tags=["Graph"],
    )
    async def cancel_graph(
        request: Request,
        knowledge_base_id: UUID,
        security: SecurityContext = security_dependency,
        service: GraphService = service_dependency,
    ) -> GraphStateResponse:
        """登记取消意图；CANCELLING 明确表示尚未确认引擎执行结束。"""
        return _response(request, await service.cancel(security, knowledge_base_id))
