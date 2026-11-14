"""原生图谱管理协议；不向客户端公开引擎 ID、Scope 或模型密钥。"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import Field

from rag_service.api.schemas import ApiErrorSchema, ApiModel
from rag_service.domain.graph import GraphState, GraphStatus


class GraphConfigurationData(ApiModel):
    """知识库级受控图谱配置，默认关闭；查询开关与构图开关独立。"""

    enabled: bool = False
    profile_ref: Literal["native-graph-light@1", "native-graph-with-summaries@1"] = (
        "native-graph-light@1"
    )
    auto_build: bool = True
    retrieval_enabled: bool = True


class GraphStateData(ApiModel):
    """图谱状态和内容/图谱版本；任务接收与图谱就绪通过独立状态表达。"""

    knowledge_base_id: UUID
    configuration: GraphConfigurationData
    status: GraphStatus
    target_content_revision: int = Field(ge=0)
    built_for_revision: int | None = Field(default=None, ge=0)
    graph_generation: int = Field(ge=0)
    configuration_version: int = Field(ge=0)
    job_id: UUID | None = None
    last_error: ApiErrorSchema | None = None
    queued_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    updated_at: datetime

    @classmethod
    def from_domain(cls, state: GraphState) -> GraphStateData:
        """只投影公开字段，取消待确认也保留原样，不伪装已完成。"""
        config = state.configuration
        return cls(
            knowledge_base_id=state.knowledge_base_id,
            configuration=GraphConfigurationData.model_validate(asdict(config)),
            status=state.status,
            target_content_revision=state.target_content_revision,
            built_for_revision=state.built_for_revision,
            graph_generation=state.graph_generation,
            configuration_version=state.configuration_version,
            job_id=state.job_id,
            last_error=(
                ApiErrorSchema(
                    code=state.last_error_code,
                    message=state.last_error_message or "图谱执行失败",
                    retryable=False,
                )
                if state.last_error_code
                else None
            ),
            queued_at=state.queued_at,
            started_at=state.started_at,
            completed_at=state.completed_at,
            updated_at=state.updated_at,
        )


class GraphStateResponse(ApiModel):
    """图谱管理操作的统一响应。"""

    request_id: UUID
    data: GraphStateData
