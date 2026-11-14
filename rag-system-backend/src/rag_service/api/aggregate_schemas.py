"""派生社区证据的公共 DTO；原文来源和 Gateway 摘要始终使用不同字段。"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from rag_service.domain.aggregate import AggregateEvidence, AggregateSource


class AggregatePositionData(BaseModel):
    """来源原文位置，不把 TXT 解析器的占位坐标解释为物理页码。"""

    model_config = ConfigDict(extra="forbid")
    page: int | None = Field(default=None, ge=1)
    bbox: tuple[float, float, float, float] | None = None


class AggregateSourceData(BaseModel):
    """可独立回读核验的一段原文，chunk_id 与文档 Chunk API 的原生 ID 一致。"""

    model_config = ConfigDict(extra="forbid")
    knowledge_base_id: UUID
    document_id: UUID
    document_name: str
    chunk_id: str
    quote: str
    document_revision: int = Field(ge=0)
    active_version_id: UUID | None = None
    positions: list[AggregatePositionData] = Field(default_factory=list)

    @classmethod
    def from_domain(cls, source: AggregateSource) -> AggregateSourceData:
        """只投影必要来源字段，内部资源范围与引擎映射不进入公共协议。"""
        return cls(
            knowledge_base_id=source.knowledge_base_id,
            document_id=source.document_id,
            document_name=source.document_name,
            chunk_id=source.engine_chunk_id,
            quote=source.quote,
            document_revision=source.document_revision,
            active_version_id=source.active_version_id,
            positions=[
                AggregatePositionData(page=item.page, bbox=item.bbox) for item in source.positions
            ],
        )


class AggregateEvidenceData(BaseModel):
    """带 A 标签的 Gateway 社区摘要；summary 是派生文本，sources.quote 才是原文。"""

    model_config = ConfigDict(extra="forbid")
    aggregate_id: UUID
    label: str = Field(pattern=r"^(A[1-9][0-9]*)?$")
    kind: Literal["community_aggregate"] = "community_aggregate"
    derived: Literal[True] = True
    origin: Literal["gateway_generated"] = "gateway_generated"
    generator_model_ref: str
    strategy_ref: str
    knowledge_base_id: UUID
    community_id: str
    title: str
    summary: str
    graph_generation: int = Field(ge=0)
    content_revision: int = Field(ge=0)
    configuration_version: int = Field(ge=0)
    graph_profile_ref: str
    sources: list[AggregateSourceData] = Field(min_length=2, max_length=16)

    @classmethod
    def from_domain(cls, evidence: AggregateEvidence) -> AggregateEvidenceData:
        """浏览、JSON 回答和审计读取使用同一个投影，避免不同接口语义漂移。"""
        return cls(
            aggregate_id=evidence.aggregate_id,
            label=evidence.label,
            origin=evidence.origin,
            generator_model_ref=evidence.generator_model_ref,
            strategy_ref=evidence.strategy_ref,
            knowledge_base_id=evidence.knowledge_base_id,
            community_id=evidence.community_id,
            title=evidence.title,
            summary=evidence.summary,
            graph_generation=evidence.graph_generation,
            content_revision=evidence.content_revision,
            configuration_version=evidence.configuration_version,
            graph_profile_ref=evidence.graph_profile_ref,
            sources=[AggregateSourceData.from_domain(item) for item in evidence.sources],
        )
