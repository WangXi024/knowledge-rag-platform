"""原生图只读数据与 Gateway 聚合报告构建契约。

原生节点、边的描述属于生成内容；source_document_ids 只是引擎文档来源集合。
真正可引用的原文必须通过 read_source_chunks 回读活动文档，并绑定本地修订。
Gateway 报告不能冒充 RAGFlow 社区报告，构建职责由独立应用层协议注入。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

if TYPE_CHECKING:
    from rag_service.domain.aggregate import AggregateEvidence, AggregateSource
    from rag_service.domain.graph import GraphState
    from rag_service.domain.models import IngestionJob


@dataclass(frozen=True, slots=True)
class NativeGraphEntity:
    """真实原生实体详情；描述不作为原文 quote，来源 ID 尚须映射到活动文档。"""

    id: str
    name: str
    entity_type: str
    description: str
    source_document_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NativeGraphRelation:
    """真实原生关系；id 是 Gateway 稳定定位键，端点和描述来自已核验图结构。"""

    id: str
    source: str
    target: str
    description: str
    source_document_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NativeGraphData:
    """一个知识库指定图代数的完整受限图，显式携带资源范围且不跨库合并。"""

    security_scope_id: str
    knowledge_base_id: UUID
    graph_generation: int
    entities: tuple[NativeGraphEntity, ...] = ()
    relations: tuple[NativeGraphRelation, ...] = ()


class GraphReadPort(Protocol):
    """原生图和真实原文的只读端口，全部入口先验证当前资源范围映射。"""

    async def read_graph(
        self, security_scope_id: str, knowledge_base_id: UUID, graph_generation: int
    ) -> NativeGraphData:
        """返回已完整验证来源和官方裁剪界限的图，不能把截断图当完整社区输入。"""
        ...

    async def read_source_chunks(
        self,
        security_scope_id: str,
        knowledge_base_id: UUID,
        engine_document_ids: tuple[str, ...],
    ) -> tuple[AggregateSource, ...]:
        """回读有限且完整的活动原文块，绑定真实文档修订；超预算明确拒绝而不截断。"""
        ...


class GraphReportBuilder(Protocol):
    """Gateway 报告生成阶段；实现负责费用不确定性检查点及来源和输出预算。"""

    async def build(
        self, job: IngestionJob, state: GraphState, generation: int
    ) -> tuple[AggregateEvidence, ...]:
        """使用受控原文生成明确归属于 Gateway 的聚合证据，不伪造原生报告。

        生成前后的任务检查点、重试和未知提交费用由实现负责；Worker 仍须保留
        严格来源快照的旧行版本做最终 CAS，避免报告覆盖后来的内容或配置。
        """
        ...
