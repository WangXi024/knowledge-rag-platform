"""原生知识图谱的隔离状态、不可变配置及基础设施端口。

图谱是知识库内容的派生索引，``graph_generation`` 与内容修订号相互独立。
原生图谱摘要只提供导航词和可核验来源，不属于可以直接引用的文档原文。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from rag_service.domain.models import utc_now

# 固定 v0.26.4 通过官方图结构做有界导航；与原生 use_kg 聚合模式区分缓存和诊断。
NATIVE_GRAPH_QUERY_MODE = "native_graph_read_v1"


class GraphStatus(StrEnum):
    """图谱配置、构建、失效和取消的对外生命周期；失败不能伪装为可用。"""

    DISABLED = "DISABLED"
    NOT_BUILT = "NOT_BUILT"
    QUEUED = "QUEUED"
    BUILDING = "BUILDING"
    READY = "READY"
    STALE = "STALE"
    FAILED = "FAILED"
    CANCELLING = "CANCELLING"


class GraphProfile(StrEnum):
    """受控不可变图谱方案；不接受外部任意上游参数或动态覆盖同版本含义。"""

    NATIVE_LIGHT_V1 = "native-graph-light@1"


@dataclass(frozen=True, slots=True)
class GraphConfiguration:
    """知识库图谱开关；配置只引用白名单方案，模型密钥不进入领域对象。"""

    enabled: bool = False
    profile_ref: str = GraphProfile.NATIVE_LIGHT_V1.value
    auto_build: bool = True
    retrieval_enabled: bool = True

    def __post_init__(self) -> None:
        """拒绝不存在的方案引用，防止调用方注入上游构建参数。"""
        GraphProfile(self.profile_ref)


def native_graph_parser_config(configuration: GraphConfiguration) -> dict[str, object]:
    """生成受控 light@1 参数，统一覆盖 Dataset 与文档 Parser 的旧关闭默认值。

    v0.26.4 普通解析不会因该标志额外发起构图；真正构图由独立 GRAPH_BUILD Job
    调用 index?type=graph。改变以下语义时必须新增策略版本及不可变种子。
    """
    return {
        "use_graphrag": configuration.enabled,
        "method": "light",
        "entity_types": ["organization", "person", "geo", "event", "category"],
        "community": False,
        "resolution": False,
    }


@dataclass(slots=True)
class GraphState:
    """某安全范围内一个知识库的图谱持久化状态。

    ``built_for_revision`` 标记最后成功使用的内容版本，构图不会递增知识库内容
    修订号；``row_version`` 供数据库乐观锁使用；任务 ID 不作为授权凭据。
    """

    security_scope_id: str
    knowledge_base_id: UUID
    configuration: GraphConfiguration = field(default_factory=GraphConfiguration)
    status: GraphStatus = GraphStatus.DISABLED
    target_content_revision: int = 0
    built_for_revision: int | None = None
    graph_generation: int = 0
    configuration_version: int = 0
    row_version: int = 0
    engine_task_id: str | None = None
    job_id: UUID | None = None
    last_error_code: str | None = None
    last_error_message: str | None = None
    queued_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)


@dataclass(frozen=True, slots=True)
class GraphTask:
    """经协议规范化的引擎任务；安全范围、知识库和代数随结果一同返回。"""

    security_scope_id: str
    knowledge_base_id: UUID
    graph_generation: int
    engine_task_id: str | None
    status: GraphStatus
    progress: float = 0.0
    message: str | None = None
    # 只有引擎提供明确终态时才能为 True；v0.26.4 删除任务记录不能证明执行器已退出。
    cancelled: bool = False


@dataclass(frozen=True, slots=True)
class GraphSnapshot:
    """有限的图谱结构统计与实体名；不把引擎生成的摘要公开为引用原文。"""

    security_scope_id: str
    knowledge_base_id: UUID
    graph_generation: int
    entity_names: tuple[str, ...] = ()
    entity_count: int = 0
    relation_count: int = 0


@dataclass(frozen=True, slots=True)
class GraphSourceReference:
    """引擎提供的真实文档/块指针；调用方仍须映射到当前生效的本地文档版本。"""

    engine_document_id: str
    engine_chunk_id: str | None = None


@dataclass(frozen=True, slots=True)
class GraphNavigation:
    """图谱检索导航；关键词只用于补召回，最终证据必须重新读取原始文档块。"""

    security_scope_id: str
    knowledge_base_id: UUID
    graph_generation: int
    entities: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    source_references: tuple[GraphSourceReference, ...] = ()


class GraphRepository(Protocol):
    """图谱状态仓库；每次操作显式隔离，更新通过行版本防止迟到任务覆盖新代数。"""

    async def get(self, security_scope_id: str, knowledge_base_id: UUID) -> GraphState | None:
        """读取可见图谱状态；跨范围与不存在均返回 ``None``。"""
        ...

    async def add(self, security_scope_id: str, value: GraphState) -> None:
        """在显式范围创建状态；实现必须拒绝对象范围与参数不一致。"""
        ...

    async def update(
        self, security_scope_id: str, value: GraphState, *, expected_row_version: int
    ) -> bool:
        """只更新范围和旧行版本均匹配的状态；冲突返回 ``False``。"""
        ...


class GraphPort(Protocol):
    """原生图谱能力；实现应先从本地仓库校验知识库映射，再调用引擎。"""

    async def configure(
        self,
        security_scope_id: str,
        knowledge_base_id: UUID,
        graph_generation: int,
        configuration: GraphConfiguration,
        *,
        previous_task_id: str | None,
    ) -> None:
        """按持久准备意图清理旧终态图并合并配置，保留原文与通用模型缓存。

        ``previous_task_id`` 必须来自 Worker 先持久化的终态观察；None 表示首次
        已确认无任务，仍须检查原生图为空。只清理原生图派生产物，不启动构建。
        已提交任务的恢复不得重新调用此操作，避免误删进行中的图或重复付费。
        """
        ...

    async def build(
        self, security_scope_id: str, knowledge_base_id: UUID, graph_generation: int
    ) -> GraphTask:
        """提交一次图谱索引；请求结果不确定时不得自动重复提交。"""
        ...

    async def task(
        self, security_scope_id: str, knowledge_base_id: UUID, graph_generation: int
    ) -> GraphTask | None:
        """读取引擎索引任务；尚无任务时返回 ``None``。"""
        ...

    async def snapshot(
        self, security_scope_id: str, knowledge_base_id: UUID, graph_generation: int
    ) -> GraphSnapshot:
        """读取受限结构统计；没有图谱时返回空快照。"""
        ...

    async def cancel(
        self, security_scope_id: str, knowledge_base_id: UUID, graph_generation: int
    ) -> None:
        """请求取消并保留已有图谱，不使用 wipe 删除索引。"""
        ...

    async def navigate(
        self,
        security_scope_id: str,
        knowledge_base_id: UUID,
        graph_generation: int,
        question: str,
        *,
        limit: int = 8,
    ) -> GraphNavigation:
        """从原生图结构获取受控实体导航；描述、聚合及关系均不作为原文引用。"""
        ...
