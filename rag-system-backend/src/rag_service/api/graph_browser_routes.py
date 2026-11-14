"""只读图谱和社区浏览 HTTP 契约；参数限额、投影和超时不改变核心业务状态。

公开节点标识只用于定位当前图代数；原文来源必须由应用服务映射和复验，客户端不能
提交原生 Dataset、模型配置或任意图遍历参数。普通问答的聚合证据有独立 DTO。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Query, Request
from pydantic import Field

from rag_service.api.aggregate_schemas import AggregateEvidenceData, AggregateSourceData
from rag_service.api.schemas import ApiModel
from rag_service.application.graph_browser import GraphBrowserService, GraphView
from rag_service.domain.errors import RagError, ResourceNotFoundError
from rag_service.domain.graph import GraphStatus
from rag_service.security.context import SecurityContext


class GraphNodeData(ApiModel):
    """生成的实体描述与真实来源分区，不把描述投影为 quote。"""

    id: str
    name: str
    entity_type: str
    description: str
    generated: Literal[True] = True
    sources: list[AggregateSourceData] = Field(default_factory=list)
    source_status: Literal["available", "unavailable"]


class GraphEdgeData(ApiModel):
    """有界关系详情；端点只引用同次响应内的公开实体。"""

    id: str
    source: str
    target: str
    description: str
    generated: Literal[True] = True
    sources: list[AggregateSourceData] = Field(default_factory=list)
    source_status: Literal["available", "unavailable"]


class GraphViewData(ApiModel):
    """管理浏览页面的状态与受限结构；读取不会生成模型摘要。"""

    knowledge_base_id: UUID
    status: GraphStatus
    graph_generation: int
    content_revision: int
    profile_ref: str
    nodes: list[GraphNodeData] = Field(default_factory=list)
    edges: list[GraphEdgeData] = Field(default_factory=list)
    truncated: bool = False
    warnings: list[str] = Field(default_factory=list)

    @classmethod
    def from_domain(cls, view: GraphView) -> GraphViewData:
        """仅导出公开来源，不透传内部 Scope、物理文档 ID 和校验摘要。"""
        return cls(
            knowledge_base_id=view.knowledge_base_id,
            status=view.status,
            graph_generation=view.graph_generation,
            content_revision=view.content_revision,
            profile_ref=view.profile_ref,
            truncated=view.truncated,
            warnings=list(view.warnings),
            nodes=[
                GraphNodeData(
                    id=item.id,
                    name=item.name,
                    entity_type=item.entity_type,
                    description=item.description,
                    sources=[AggregateSourceData.from_domain(source) for source in item.sources],
                    source_status="available" if item.sources else "unavailable",
                )
                for item in view.nodes
            ],
            edges=[
                GraphEdgeData(
                    id=item.id,
                    source=item.source,
                    target=item.target,
                    description=item.description,
                    sources=[AggregateSourceData.from_domain(source) for source in item.sources],
                    source_status="available" if item.sources else "unavailable",
                )
                for item in view.edges
            ],
        )


class GraphViewResponse(ApiModel):
    """与其他 API 一致的请求标识和单次图视图。"""

    request_id: UUID
    data: GraphViewData


class CommunityListData(ApiModel):
    """版本固定的已生成报告；不可用时明确空列表和状态。"""

    knowledge_base_id: UUID
    status: GraphStatus
    graph_generation: int
    content_revision: int
    communities: list[AggregateEvidenceData]
    warnings: list[str]


class CommunityListResponse(ApiModel):
    """社区只读列表响应。"""

    request_id: UUID
    data: CommunityListData


class CommunityResponse(ApiModel):
    """单份可回溯报告响应，失败时不返回旧正文。"""

    request_id: UUID
    data: AggregateEvidenceData


def _request_id(request: Request) -> UUID:
    """复用已通过格式检查的请求 ID；测试注入缺失时只生成本地标识。"""
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, UUID) else uuid4()


def install_graph_browser_routes(
    app: FastAPI,
    *,
    authenticate: Callable[..., Any],
    get_graph_browser: Callable[[], AsyncIterator[GraphBrowserService]],
) -> None:
    """安装三个管理只读入口；保留原图谱状态 GET，不改变旧客户端契约。"""
    service_dependency, security_dependency = Depends(get_graph_browser), Depends(authenticate)
    prefix = "/v1/management/knowledge-bases/{knowledge_base_id}/graph"

    @app.get(prefix + "/view", response_model=GraphViewResponse, tags=["Graph"])
    async def graph_view(
        request: Request,
        knowledge_base_id: UUID,
        query: str = Query(default="", max_length=100),
        node_id: str | None = Query(default=None, pattern=r"^[0-9a-f]{32}$"),
        depth: int = Query(default=1, ge=0, le=2),
        limit: int = Query(default=40, ge=1, le=40),
        generation: int | None = Query(default=None, ge=0),
        security: SecurityContext = security_dependency,
        service: GraphBrowserService = service_dependency,
    ) -> GraphViewResponse:
        """搜索实体或展开有界邻域；图版本变化409，超时503，不沿用旧图。"""
        try:
            view = await service.view(
                security,
                knowledge_base_id,
                query=query,
                node_id=node_id,
                depth=depth,
                limit=limit,
                generation=generation,
            )
        except TimeoutError as error:
            raise RagError("RAG-GRAPH-024", "图谱读取超时，请重试", 503, True) from error
        return GraphViewResponse(
            request_id=_request_id(request), data=GraphViewData.from_domain(view)
        )

    @app.get(prefix + "/communities", response_model=CommunityListResponse, tags=["Graph"])
    async def communities(
        request: Request,
        knowledge_base_id: UUID,
        generation: int | None = Query(default=None, ge=0),
        security: SecurityContext = security_dependency,
        service: GraphBrowserService = service_dependency,
    ) -> CommunityListResponse:
        """只列出已生成且来源仍有效的社区，不因浏览请求调用模型。"""
        try:
            view, reports = await service.communities(
                security, knowledge_base_id, generation=generation
            )
        except TimeoutError as error:
            raise RagError("RAG-GRAPH-024", "社区读取超时，请重试", 503, True) from error
        return CommunityListResponse(
            request_id=_request_id(request),
            data=CommunityListData(
                knowledge_base_id=view.knowledge_base_id,
                status=view.status,
                graph_generation=view.graph_generation,
                content_revision=view.content_revision,
                communities=[AggregateEvidenceData.from_domain(report) for report in reports],
                warnings=list(view.warnings),
            ),
        )

    @app.get(
        prefix + "/communities/{community_id}", response_model=CommunityResponse, tags=["Graph"]
    )
    async def community_detail(
        request: Request,
        knowledge_base_id: UUID,
        community_id: str,
        generation: int | None = Query(default=None, ge=0),
        security: SecurityContext = security_dependency,
        service: GraphBrowserService = service_dependency,
    ) -> CommunityResponse:
        """详情沿相同权限/版本/原文核查；不存在与不可见报告保持统一404。"""
        try:
            _view, reports = await service.communities(
                security, knowledge_base_id, generation=generation
            )
        except TimeoutError as error:
            raise RagError("RAG-GRAPH-024", "社区读取超时，请重试", 503, True) from error
        report = next((value for value in reports if value.community_id == community_id), None)
        if report is None:
            raise ResourceNotFoundError("RAG-GRAPH-023", "社区摘要不存在或已失效")
        return CommunityResponse(
            request_id=_request_id(request), data=AggregateEvidenceData.from_domain(report)
        )
