"""知识库原生图谱的配置、持久化调度和版本发布编排。

API 只创建 PostgreSQL Job；Celery 仍只负责唤醒。图谱是派生索引，构图成功不能
改变内容 revision。所有发布操作都校验内容和配置快照，迟到的结果只能进入 STALE。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import UUID

from rag_service.application.access import (
    KnowledgeBaseAccessPolicy,
    ManagementAccessPolicy,
    SharedKnowledgeBaseAccessPolicy,
    TrustedNetworkManagementAccessPolicy,
)
from rag_service.application.profiles import ProfileCatalog
from rag_service.domain.errors import RagError, ResourceNotFoundError
from rag_service.domain.graph import GraphConfiguration, GraphRepository, GraphState, GraphStatus
from rag_service.domain.models import (
    IngestionJob,
    JobStatus,
    JobType,
    KnowledgeBase,
    KnowledgeBaseStatus,
    utc_now,
)
from rag_service.domain.ports import JobRepository, KnowledgeBaseRepository
from rag_service.security.context import SecurityContext

IN_FLIGHT = frozenset({GraphStatus.QUEUED, GraphStatus.BUILDING, GraphStatus.CANCELLING})


class GraphService:
    """在已授权资源范围内管理图谱；默认关闭且不调用模型或修改外部 Dataset。"""

    def __init__(
        self,
        knowledge_bases: KnowledgeBaseRepository,
        graphs: GraphRepository,
        jobs: JobRepository,
        *,
        enabled: bool = False,
        model_ref: str | None = None,
        access_policy: KnowledgeBaseAccessPolicy | None = None,
        management_access: ManagementAccessPolicy | None = None,
        profiles: ProfileCatalog | None = None,
    ) -> None:
        """注入同一事务中的资源、图谱和任务仓库及部署能力。"""
        self._knowledge_bases = knowledge_bases
        self._graphs = graphs
        self._jobs = jobs
        self._enabled = enabled
        self._model_ref = model_ref
        self._access = access_policy or SharedKnowledgeBaseAccessPolicy()
        self._management = management_access or TrustedNetworkManagementAccessPolicy()
        self._profiles = profiles

    async def _resource(self, security: SecurityContext, knowledge_base_id: UUID) -> KnowledgeBase:
        """先按候选范围查库，再应用可替换权限策略，统一隐藏跨租户资源存在性。"""
        await self._management.require_access(security, "knowledge_base.graph")
        value = await self._knowledge_bases.get_accessible(
            self._access.candidate_resource_scopes(security), knowledge_base_id
        )
        if value is None or not await self._access.filter_readable(security, (value,)):
            raise ResourceNotFoundError("RAG-KB-001", "知识库不存在")
        if value.status != KnowledgeBaseStatus.READY:
            raise RagError("RAG-GRAPH-001", "知识库当前不可构图", 409, False)
        return value

    async def get(self, security: SecurityContext, knowledge_base_id: UUID) -> GraphState:
        """只读返回配置与状态；旧知识库没有图谱行时保持默认关闭且不写数据库。"""
        kb = await self._resource(security, knowledge_base_id)
        value = await self._graphs.get(kb.security_scope_id, kb.id)
        if value is None:
            return GraphState(kb.security_scope_id, kb.id)
        # 只读投影也立即反映内容失效，不能等待异步维护任务才停止在线使用。
        if value.status == GraphStatus.READY and value.built_for_revision != kb.revision:
            value.status = GraphStatus.STALE
        return value

    def _require_runtime(self) -> None:
        """只有显式启用部署能力并注册构图模型时才允许新增收费构建。"""
        if not self._enabled or not self._model_ref:
            raise RagError("RAG-GRAPH-002", "请先启用 GraphRAG 并注册原生 Chat 模型", 503, False)

    async def configure(
        self, security: SecurityContext, knowledge_base_id: UUID, configuration: GraphConfiguration
    ) -> GraphState:
        """原子保存受控配置；关闭后立即停止查询，运行中构图转为待确认取消。"""
        kb = await self._resource(security, knowledge_base_id)
        if configuration.enabled:
            self._require_runtime()
            if self._profiles is not None:
                self._profiles.get("GRAPH", configuration.profile_ref)
        state = await self._graphs.get(kb.security_scope_id, kb.id)
        if state is None:
            state = GraphState(kb.security_scope_id, kb.id)
            await self._graphs.add(kb.security_scope_id, state)
        if state.configuration == configuration:
            return state
        expected = state.row_version
        changed_build_policy = state.configuration.profile_ref != configuration.profile_ref
        state.configuration = configuration
        state.configuration_version += 1
        state.updated_at = utc_now()
        if state.status in IN_FLIGHT and (changed_build_policy or not configuration.enabled):
            # 只有构图策略变化或停用才取消；查询/自动调度开关不重收构图费用。
            state.status = GraphStatus.CANCELLING
        elif state.status in IN_FLIGHT:
            pass
        elif not configuration.enabled:
            state.status = GraphStatus.DISABLED
        elif (
            state.status == GraphStatus.DISABLED
            or changed_build_policy
            or state.built_for_revision != kb.revision
        ):
            state.status = (
                GraphStatus.NOT_BUILT if state.graph_generation == 0 else GraphStatus.STALE
            )
        # 其他状态保持原值：built_for_revision 只证明上次成功构建采用的内容版本，
        # 不能证明失败/取消后的引擎产物仍完整。查询开关不能把 FAILED/STALE 升为
        # READY；停用后重新启用也须重建，只有 Worker 的成功发布才能重新确认为可用。
        await self._save(state, expected)
        if (
            configuration.enabled
            and configuration.auto_build
            and state.status not in IN_FLIGHT
            and state.status != GraphStatus.READY
        ):
            return await self.enqueue(kb.security_scope_id, kb.id)
        return state

    async def build(self, security: SecurityContext, knowledge_base_id: UUID) -> GraphState:
        """手动构建、重建或重试；同一知识库的在途请求合并到已有持久化任务。"""
        kb = await self._resource(security, knowledge_base_id)
        self._require_runtime()
        return await self.enqueue(kb.security_scope_id, kb.id)

    async def enqueue(self, security_scope_id: str, knowledge_base_id: UUID) -> GraphState:
        """在调用方事务内通过乐观锁抢占唯一任务，再写入内容和策略输入快照。"""
        kb = await self._knowledge_bases.get(security_scope_id, knowledge_base_id)
        state = await self._graphs.get(security_scope_id, knowledge_base_id)
        if kb is None or kb.status != KnowledgeBaseStatus.READY:
            raise ResourceNotFoundError("RAG-KB-001", "知识库不存在")
        if state is None or not state.configuration.enabled:
            raise RagError("RAG-GRAPH-003", "知识库尚未启用 GraphRAG", 409, False)
        if state.status in IN_FLIGHT:
            return state
        self._require_runtime()
        job = IngestionJob(
            security_scope_id=security_scope_id,
            knowledge_base_id=knowledge_base_id,
            job_type=JobType.GRAPH_BUILD,
            payload={
                "content_revision": kb.revision,
                "configuration_version": state.configuration_version,
                "profile_ref": state.configuration.profile_ref,
                "previous_engine_task_id": state.engine_task_id,
            },
        )
        expected = state.row_version
        state.target_content_revision = kb.revision
        state.status = GraphStatus.QUEUED
        state.job_id = job.id
        state.engine_task_id = None
        state.last_error_code = None
        state.last_error_message = None
        state.queued_at = utc_now()
        state.started_at = None
        state.completed_at = None
        state.updated_at = utc_now()
        await self._save(state, expected)
        # CAS 与 Job INSERT 必须由外层事务一起提交，失败时不留下已抢占却无任务的状态。
        await self._jobs.add(security_scope_id, job)
        return state

    async def cancel(
        self,
        security: SecurityContext,
        knowledge_base_id: UUID,
        *,
        expected_job_id: UUID | None = None,
    ) -> GraphState:
        """只登记取消意图；响应 CANCELLING 不代表 RAGFlow 已经终止执行。"""
        kb = await self._resource(security, knowledge_base_id)
        state = await self._graphs.get(kb.security_scope_id, kb.id)
        if state is None:
            return GraphState(kb.security_scope_id, kb.id)
        if expected_job_id is not None and state.job_id != expected_job_id:
            raise RagError("RAG-GRAPH-005", "该构图任务已被后继任务替换，请重新读取", 409, False)
        if state.status not in IN_FLIGHT:
            return state
        expected = state.row_version
        state.status = GraphStatus.CANCELLING
        state.updated_at = utc_now()
        await self._save(state, expected)
        if state.job_id is not None:
            job = await self._jobs.get(kb.security_scope_id, state.job_id)
            if job is not None and not job_is_terminal(job):
                job.status = JobStatus.CANCEL_REQUESTED
                if not await self._jobs.update(kb.security_scope_id, job):
                    raise RagError("RAG-GRAPH-005", "构图取消状态并发更新", 409, True)
        return state

    async def content_changed(
        self, security_scope_id: str, knowledge_base_id: UUID, *, auto_build: bool = True
    ) -> None:
        """在内容事务内推进目标修订及行版本，阻止旧构图跨过并发内容提交发布。

        在途任务仍属于原 Job，不覆盖已经提交给引擎的 payload 快照；本行的最新
        目标修订用于记录后继需求，CAS 则让发布读取之后发生的内容变更可被检测。
        """
        state = await self._graphs.get(security_scope_id, knowledge_base_id)
        if state is None or not state.configuration.enabled:
            return
        kb = await self._knowledge_bases.get(security_scope_id, knowledge_base_id)
        if kb is None:
            raise ResourceNotFoundError("RAG-KB-001", "知识库不存在")
        expected = state.row_version
        in_flight = state.status in IN_FLIGHT
        state.target_content_revision = kb.revision
        state.updated_at = utc_now()
        if not in_flight:
            state.status = GraphStatus.STALE
        # 即使 revision 尚未递增（例如摄取刚开始），也要推进版本：物理来源集合
        # 可能已经变化，旧发布必须重新执行来源核验，而不能靠旧 READY 快照通过。
        await self._save(state, expected)
        if (
            not in_flight and auto_build and state.configuration.auto_build
            and self._enabled and self._model_ref
        ):
            await self.enqueue(security_scope_id, knowledge_base_id)

    async def stop_for_deletion(self, security_scope_id: str, knowledge_base_id: UUID) -> None:
        """删除事务先停用派生索引，运行中的旧任务只能取消而不能迟到发布。"""
        state = await self._graphs.get(security_scope_id, knowledge_base_id)
        if state is None:
            return
        expected = state.row_version
        config = state.configuration
        state.configuration = GraphConfiguration(
            enabled=False,
            profile_ref=config.profile_ref,
            auto_build=config.auto_build,
            retrieval_enabled=False,
        )
        state.configuration_version += 1
        state.status = GraphStatus.CANCELLING if state.status in IN_FLIGHT else GraphStatus.DISABLED
        await self._save(state, expected)

    async def _save(self, state: GraphState, expected: int) -> None:
        """保存版本化状态；并发冲突交由调用方事务回滚，不能继续外部收费调用。"""
        if not await self._graphs.update(
            state.security_scope_id, state, expected_row_version=expected
        ):
            raise RagError("RAG-GRAPH-005", "图谱状态并发更新，请重新读取", 409, True)


async def noop_graph_checkpoint() -> None:
    """供隔离测试使用的默认提交钩子，真实 Worker 必须传入租约保护的事务检查点。"""
    return None


GraphCheckpoint = Callable[[], Awaitable[None]]


def job_is_terminal(job: IngestionJob) -> bool:
    """判定图谱任务是否已经结束，避免恢复流程重新提交同一构建。"""
    return job.status in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}
