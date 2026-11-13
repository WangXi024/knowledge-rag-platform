"""图谱持久化编排测试：隔离、幂等、提交恢复、版本发布和取消未知状态。"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from rag_service.application.graph import GraphService
from rag_service.application.graph_worker import GraphWorker
from rag_service.domain.errors import RagError
from rag_service.domain.graph import (
    GraphConfiguration,
    GraphSnapshot,
    GraphState,
    GraphStatus,
    GraphTask,
)
from rag_service.domain.models import (
    IngestionJob,
    JobStatus,
    KnowledgeBase,
    KnowledgeBaseStatus,
)
from rag_service.security.context import SecurityContext


class GraphMemory:
    """复制读写并执行 CAS 的内存仓库，避免共享对象掩盖迟到更新。"""

    def __init__(self) -> None:
        """建立按 Scope 和 KB 复合键隔离的状态。"""
        self.items: dict[tuple[str, UUID], GraphState] = {}
        self.conflict = False

    async def get(self, scope: str, kb_id: UUID) -> GraphState | None:
        """返回独立快照，跨租户不会命中。"""
        return deepcopy(self.items.get((scope, kb_id)))

    async def add(self, scope: str, value: GraphState) -> None:
        """拒绝隔离参数错配和重复插入。"""
        assert scope == value.security_scope_id
        assert (scope, value.knowledge_base_id) not in self.items
        self.items[scope, value.knowledge_base_id] = deepcopy(value)

    async def update(self, scope: str, value: GraphState, *, expected_row_version: int) -> bool:
        """仅旧版本相符时推进版本，模拟真实数据库乐观锁。"""
        old = self.items.get((scope, value.knowledge_base_id))
        if self.conflict or old is None or old.row_version != expected_row_version:
            return False
        value.row_version += 1
        self.items[scope, value.knowledge_base_id] = deepcopy(value)
        return True


class Resources:
    """供用例使用的知识库和任务存储，不承载外部模型行为。"""

    def __init__(self, kb: KnowledgeBase) -> None:
        """保存唯一测试知识库及其后台任务。"""
        self.kb = kb
        self.jobs: dict[UUID, IngestionJob] = {}

    async def get(self, scope: str, identifier: UUID) -> Any:
        """在正确 Scope 中查知识库或 Job。"""
        if scope != self.kb.security_scope_id:
            return None
        if identifier == self.kb.id:
            return deepcopy(self.kb)
        return deepcopy(self.jobs.get(identifier))

    async def get_accessible(
        self, scopes: tuple[str, ...], identifier: UUID
    ) -> KnowledgeBase | None:
        """模拟先 SQL 过滤范围的资源读取。"""
        return (
            deepcopy(self.kb)
            if self.kb.security_scope_id in scopes and identifier == self.kb.id
            else None
        )

    async def add(self, scope: str, value: IngestionJob) -> None:
        """增加任务并检查隔离范围。"""
        assert scope == value.security_scope_id
        self.jobs[value.id] = deepcopy(value)

    async def update(self, scope: str, value: IngestionJob) -> bool:
        """保存任务检查点。"""
        assert scope == value.security_scope_id
        self.jobs[value.id] = deepcopy(value)
        return True


class Engine:
    """可控的原生图谱替身；单独计数真正构建调用。"""

    def __init__(self) -> None:
        """默认立即完成构图，允许测试注入网络不确定和未确认取消。"""
        self.build_count = 0
        self.cancel_count = 0
        self.status = GraphStatus.READY
        self.response_lost = False
        self.absent = False
        self.on_snapshot: Any = None

    async def configure(self, *args: Any) -> None:
        """配置操作不计作收费构图。"""
        return None

    async def build(self, scope: str, kb_id: UUID, generation: int) -> GraphTask:
        """模拟请求可能已被接受但响应丢失。"""
        self.build_count += 1
        if self.response_lost:
            raise RagError("RAG-RF-003", "构图响应超时", 504, True)
        return GraphTask(scope, kb_id, generation, "task-1", self.status, 1.0)

    async def task(self, scope: str, kb_id: UUID, generation: int) -> GraphTask | None:
        """回读同一任务或模拟取消后上游删除任务记录。"""
        if self.absent:
            return None
        return GraphTask(scope, kb_id, generation, "task-1", self.status, 1.0)

    async def snapshot(self, scope: str, kb_id: UUID, generation: int) -> GraphSnapshot:
        """在二次来源校验处注入并发内容变更。"""
        if self.on_snapshot:
            await self.on_snapshot()
        return GraphSnapshot(scope, kb_id, generation, ("合成实体",), 1, 0)

    async def cancel(self, *args: Any) -> None:
        """只表示取消请求受理，不能证明后台执行结束。"""
        self.cancel_count += 1


@pytest.fixture
def graph_env() -> tuple[SecurityContext, Resources, GraphMemory, GraphService, Engine]:
    """建立相同租户的最小图谱环境，不访问网络和数据库。"""
    security = SecurityContext(
        subject_id="fixture-user",
        tenant_id="fixture-tenant",
        rag_scope_id="fixture",
        security_scope_id="a" * 64,
        token_id="fixture",
        expires_at=datetime.max.replace(tzinfo=UTC),
    )
    kb = KnowledgeBase(
        security_scope_id=security.tenant_resource_scope_id,
        name="合成知识库",
        parser_profile_ref="parser@1",
        chunk_profile_ref="chunk@1",
        embedding_profile_ref="embedding@1",
        retrieval_profile_ref="retrieval@1",
        status=KnowledgeBaseStatus.READY,
        revision=3,
    )
    resources = Resources(kb)
    graphs = GraphMemory()
    service = GraphService(resources, graphs, resources, enabled=True, model_ref="synthetic-chat")
    return security, resources, graphs, service, Engine()


async def enabled_job(env: Any, *, auto_build: bool = False) -> IngestionJob:
    """启用配置并认领一个构图 Job，模拟 Runner 已完成的原子认领。"""
    security, resources, _graphs, service, _engine = env
    await service.configure(
        security, resources.kb.id, GraphConfiguration(enabled=True, auto_build=auto_build)
    )
    state = await service.build(security, resources.kb.id)
    job = resources.jobs[state.job_id]
    job.status = JobStatus.RUNNING
    job.attempt = 1
    return job


async def test_defaults_do_not_write_or_require_model(graph_env: Any) -> None:
    """默认关闭的旧库读取不会创建状态，普通部署不需要构图模型。"""
    security, resources, graphs, _service, _engine = graph_env
    service = GraphService(resources, graphs, resources)
    assert (await service.get(security, resources.kb.id)).status == GraphStatus.DISABLED
    assert not graphs.items and not resources.jobs
    with pytest.raises(RagError, match="RAG-GRAPH-002"):
        await service.configure(security, resources.kb.id, GraphConfiguration(enabled=True))


async def test_cross_tenant_and_unknown_are_same_404(graph_env: Any) -> None:
    """范围外知识库与不存在返回相同错误，不泄漏图谱是否存在。"""
    security, resources, _graphs, service, _engine = graph_env
    resources.kb.security_scope_id = "b" * 64
    for identifier in (resources.kb.id, uuid4()):
        with pytest.raises(RagError) as error:
            await service.get(security, identifier)
        assert error.value.http_status == 404
        assert error.value.code == "RAG-KB-001"


async def test_duplicate_build_coalesces_and_cas_conflict_creates_no_job(graph_env: Any) -> None:
    """重复请求共享在途任务，并发 CAS 失败时不新增 Job。"""
    security, resources, graphs, service, _engine = graph_env
    job = await enabled_job(graph_env)
    assert (await service.build(security, resources.kb.id)).job_id == job.id
    assert len(resources.jobs) == 1
    graphs.items[resources.kb.security_scope_id, resources.kb.id].status = GraphStatus.FAILED
    graphs.conflict = True
    with pytest.raises(RagError):
        await service.build(security, resources.kb.id)
    assert len(resources.jobs) == 1


async def test_success_publishes_generation_without_changing_content_revision(
    graph_env: Any,
) -> None:
    """构图发布只推进派生图版本，不让刚建好的图自我过期。"""
    _security, resources, graphs, service, engine = graph_env
    job = await enabled_job(graph_env)
    await GraphWorker(resources, graphs, resources, engine, service).execute(job)
    state = await graphs.get(resources.kb.security_scope_id, resources.kb.id)
    assert state.status == GraphStatus.READY
    assert state.graph_generation == 1 and state.built_for_revision == 3
    assert resources.kb.revision == 3 and engine.build_count == 1
    assert job.status == JobStatus.SUCCEEDED


async def test_changed_content_cannot_publish_ready_and_auto_rebuilds_once(graph_env: Any) -> None:
    """构建期间新增内容，旧结果标过期并以最新 revision 合并新任务。"""
    _security, resources, graphs, service, engine = graph_env
    job = await enabled_job(graph_env, auto_build=True)

    async def change_content() -> None:
        """模拟摄取事务已发布新内容。"""
        resources.kb.revision = 4

    engine.on_snapshot = change_content
    await GraphWorker(resources, graphs, resources, engine, service).execute(job)
    state = await graphs.get(resources.kb.security_scope_id, resources.kb.id)
    assert state.status == GraphStatus.QUEUED and state.target_content_revision == 4
    assert state.graph_generation == 0 and len(resources.jobs) == 2
    assert job.result["published"] is False


async def test_queued_build_coalesces_latest_completed_content_before_submission(
    graph_env: Any,
) -> None:
    """多份文档解析在提交前完成，刷新同一任务的快照且只付费构图一次。"""
    _security, resources, graphs, service, engine = graph_env
    job = await enabled_job(graph_env, auto_build=True)
    for revision in (4, 5):
        resources.kb.revision = revision
        await service.content_changed(resources.kb.security_scope_id, resources.kb.id)
    assert len(resources.jobs) == 1 and job.payload["content_revision"] == 3
    original_build = engine.build

    async def verify_submission_checkpoint(
        scope: str, kb_id: UUID, generation: int,
    ) -> GraphTask:
        """模型调用开始前，图谱目标与 Job 内容快照已共同保存。"""
        current = await graphs.get(scope, kb_id)
        stored_job = resources.jobs[job.id]
        assert current.target_content_revision == 5
        assert stored_job.payload["content_revision"] == 5
        assert stored_job.payload["submission_started"]
        return await original_build(scope, kb_id, generation)

    engine.build = verify_submission_checkpoint
    await GraphWorker(resources, graphs, resources, engine, service).execute(job)
    state = await graphs.get(resources.kb.security_scope_id, resources.kb.id)
    assert state.status == GraphStatus.READY and state.built_for_revision == 5
    assert state.target_content_revision == job.payload["content_revision"] == 5
    assert state.graph_generation == 1 and len(resources.jobs) == engine.build_count == 1
    assert job.status == JobStatus.SUCCEEDED and job.result["published"] is True


async def test_submitted_build_recovery_keeps_original_content_snapshot(graph_env: Any) -> None:
    """已提交后内容变化，恢复只跟踪旧任务，不重定位快照或重复收费。"""
    _security, resources, graphs, service, engine = graph_env
    job = await enabled_job(graph_env)
    engine.response_lost = True
    worker = GraphWorker(resources, graphs, resources, engine, service)
    await worker.execute(job)
    assert job.status == JobStatus.RETRY_WAIT and job.payload["submission_started"]
    resources.kb.revision = 4
    await service.content_changed(resources.kb.security_scope_id, resources.kb.id)
    job.status = JobStatus.RUNNING
    job.attempt += 1
    await worker.execute(job)
    state = await graphs.get(resources.kb.security_scope_id, resources.kb.id)
    # 状态行记录最新重建需求，已经提交的 Job 继续绑定旧快照，不能重定位收费任务。
    assert state.target_content_revision == 4
    assert job.payload["content_revision"] == 3
    assert state.status == GraphStatus.STALE and state.graph_generation == 0
    assert state.built_for_revision is None and engine.build_count == len(resources.jobs) == 1
    assert job.status == JobStatus.SUCCEEDED and job.result["published"] is False


async def test_lost_submission_recovers_without_second_build(graph_env: Any) -> None:
    """提交响应超时后恢复到原生任务，不能重复构图计费。"""
    _security, resources, graphs, service, engine = graph_env
    job = await enabled_job(graph_env)
    engine.response_lost = True
    worker = GraphWorker(resources, graphs, resources, engine, service)
    await worker.execute(job)
    assert job.status == JobStatus.RETRY_WAIT and job.payload["submission_started"]
    job.status = JobStatus.RUNNING
    job.attempt += 1
    await worker.execute(job)
    assert job.status == JobStatus.SUCCEEDED and engine.build_count == 1


async def test_unknown_submission_does_not_resubmit(graph_env: Any) -> None:
    """无法确认已经受理的构建时停止自动提交，保留人工核查错误。"""
    _security, resources, graphs, service, engine = graph_env
    job = await enabled_job(graph_env)
    job.payload["submission_started"] = True
    engine.absent = True
    await GraphWorker(resources, graphs, resources, engine, service).execute(job)
    assert job.status == JobStatus.FAILED and job.error_code == "RAG-GRAPH-006"
    assert engine.build_count == 0


async def test_cancel_before_submit_is_terminal_without_engine_call(graph_env: Any) -> None:
    """尚未交给 RAGFlow 的排队任务可以确定取消且不产生模型调用。"""
    security, resources, graphs, service, engine = graph_env
    job = await enabled_job(graph_env)
    await service.cancel(security, resources.kb.id)
    await GraphWorker(resources, graphs, resources, engine, service).execute(job)
    assert job.status == JobStatus.CANCELLED
    assert engine.cancel_count == 0 and engine.build_count == 0


async def test_cancel_ack_without_terminal_keeps_cancelling(graph_env: Any) -> None:
    """v0.26.4 取消后任务消失不等于已停止，不将其报告为取消成功。"""
    security, resources, graphs, service, engine = graph_env
    job = await enabled_job(graph_env)
    job.payload["submission_started"] = True
    await service.cancel(security, resources.kb.id)
    engine.absent = True
    ticks = iter([0.0, 1.0, 3.0])

    async def immediate(_delay: float) -> None:
        """确定性推进轮询，不引入测试睡眠。"""
        return None

    await GraphWorker(
        resources,
        graphs,
        resources,
        engine,
        service,
        sleep=immediate,
        monotonic=lambda: next(ticks),
        timeout_seconds=2,
    ).execute(job)
    state = await graphs.get(resources.kb.security_scope_id, resources.kb.id)
    assert state.status == GraphStatus.CANCELLING
    assert job.status == JobStatus.RETRY_WAIT and job.error_code == "RAG-GRAPH-011"
    assert engine.cancel_count == 1


async def test_retrieval_switch_does_not_enqueue_another_paid_build(graph_env: Any) -> None:
    """已就绪图谱只切换查询开关时保持图代数，不创建额外收费任务。"""
    security, resources, graphs, service, engine = graph_env
    job = await enabled_job(graph_env, auto_build=True)
    await GraphWorker(resources, graphs, resources, engine, service).execute(job)
    state = await service.configure(
        security,
        resources.kb.id,
        GraphConfiguration(enabled=True, auto_build=True, retrieval_enabled=False),
    )
    assert state.status == GraphStatus.READY and state.graph_generation == 1
    assert len(resources.jobs) == engine.build_count == 1


async def test_cancel_rejects_replaced_native_task_before_request(graph_env: Any) -> None:
    """原生最新任务已更换时，不向后继任务发送取消，也不确认旧任务结束。"""
    security, resources, graphs, service, engine = graph_env
    job = await enabled_job(graph_env)
    job.payload["submission_started"] = True
    graphs.items[resources.kb.security_scope_id, resources.kb.id].engine_task_id = "task-1"
    await service.cancel(security, resources.kb.id)

    async def replacement_task(scope: str, kb_id: UUID, generation: int) -> GraphTask:
        """模拟同 Dataset 的后继原生任务仍在运行。"""
        return GraphTask(scope, kb_id, generation, "task-2", GraphStatus.BUILDING, 0.5)

    engine.task = replacement_task
    await GraphWorker(
        resources, graphs, resources, engine, service, timeout_seconds=0
    ).execute(job)
    state = await graphs.get(resources.kb.security_scope_id, resources.kb.id)
    assert state.status == GraphStatus.CANCELLING and state.engine_task_id == "task-1"
    assert job.status == JobStatus.FAILED and job.error_code == "RAG-GRAPH-007"
    assert engine.cancel_count == engine.build_count == 0


async def test_cancel_rejects_replaced_native_task_while_polling(graph_env: Any) -> None:
    """取消等待期间出现后继任务的终态，不能将其当成旧任务已停止。"""
    security, resources, graphs, service, engine = graph_env
    job = await enabled_job(graph_env)
    job.payload["submission_started"] = True
    graphs.items[resources.kb.security_scope_id, resources.kb.id].engine_task_id = "task-1"
    await service.cancel(security, resources.kb.id)
    observations = 0

    async def replacement_during_poll(scope: str, kb_id: UUID, generation: int) -> GraphTask:
        """先回读原任务，取消后回读到已结束的后继任务。"""
        nonlocal observations
        observations += 1
        if observations == 1:
            return GraphTask(scope, kb_id, generation, "task-1", GraphStatus.BUILDING, 0.5)
        return GraphTask(scope, kb_id, generation, "task-2", GraphStatus.READY, 1.0)

    async def immediate(_delay: float) -> None:
        """跳过轮询间隔，不产生真实等待。"""
        return None

    engine.task = replacement_during_poll
    await GraphWorker(
        resources, graphs, resources, engine, service, sleep=immediate
    ).execute(job)
    state = await graphs.get(resources.kb.security_scope_id, resources.kb.id)
    assert state.status == GraphStatus.CANCELLING and state.engine_task_id == "task-1"
    assert job.status == JobStatus.FAILED and job.error_code == "RAG-GRAPH-007"
    assert observations == 2 and engine.cancel_count == 1 and engine.build_count == 0


@pytest.mark.parametrize("during_poll", [False, True])
async def test_cancel_unknown_submission_never_uses_previous_task_terminal(
    graph_env: Any, during_poll: bool,
) -> None:
    """提交响应丢失时，上次构建的终态不能证明本次构建已经停止。"""
    security, resources, graphs, service, engine = graph_env
    job = await enabled_job(graph_env)
    job.payload["submission_started"] = True
    job.payload["previous_engine_task_id"] = "task-previous"
    await service.cancel(security, resources.kb.id)
    observations = 0

    async def previous_task(scope: str, kb_id: UUID, generation: int) -> GraphTask:
        """可在取消前或轮询期间回读到旧任务的完成记录。"""
        nonlocal observations
        observations += 1
        if during_poll and observations == 1:
            return GraphTask(scope, kb_id, generation, "task-1", GraphStatus.BUILDING, 0.5)
        return GraphTask(scope, kb_id, generation, "task-previous", GraphStatus.READY, 1.0)

    async def immediate(_delay: float) -> None:
        """跳过轮询间隔，不产生真实等待。"""
        return None

    engine.task = previous_task
    await GraphWorker(
        resources, graphs, resources, engine, service, sleep=immediate
    ).execute(job)
    state = await graphs.get(resources.kb.security_scope_id, resources.kb.id)
    assert state.status == GraphStatus.CANCELLING and state.engine_task_id is None
    assert job.status == JobStatus.FAILED and job.error_code == "RAG-GRAPH-006"
    assert observations == 1 + int(during_poll) and engine.cancel_count == int(during_poll)
    assert engine.build_count == 0


async def test_cancel_during_final_snapshot_never_auto_rebuilds(graph_env: Any) -> None:
    """用户在最终来源校验期间取消，不能被发布覆盖或自动排新收费任务。"""
    security, resources, graphs, service, engine = graph_env
    job = await enabled_job(graph_env, auto_build=True)

    async def cancel_during_snapshot() -> None:
        """模拟API取消与最后一次模型完成观察交错。"""
        await service.cancel(security, resources.kb.id)

    engine.on_snapshot = cancel_during_snapshot
    await GraphWorker(resources, graphs, resources, engine, service).execute(job)
    state = await graphs.get(resources.kb.security_scope_id, resources.kb.id)
    assert job.status == JobStatus.CANCELLED
    assert state.status == GraphStatus.STALE and state.graph_generation == 0
    assert len(resources.jobs) == 1 and engine.cancel_count == 0


async def test_successor_is_created_before_committing_old_terminal_job(graph_env: Any) -> None:
    """内容过期的后继任务和旧终态必须一次提交，不能复用已清除的旧租约。"""
    _security, resources, graphs, service, engine = graph_env
    job = await enabled_job(graph_env, auto_build=True)
    terminal_committed = False

    async def guarded_checkpoint() -> None:
        """模拟真实PG：旧任务终态提交后，任何下一次旧租约检查都被拒绝。"""
        nonlocal terminal_committed
        assert not terminal_committed, "old terminal lease cannot guard a new transaction"
        stored = resources.jobs[job.id]
        if stored.status == JobStatus.SUCCEEDED:
            current = await graphs.get(resources.kb.security_scope_id, resources.kb.id)
            assert current.job_id != job.id
            terminal_committed = True

    async def replace_content() -> None:
        """模拟构图期间合法的内容版本递增。"""
        resources.kb.revision += 1

    engine.on_snapshot = replace_content
    await GraphWorker(
        resources, graphs, resources, engine, service, checkpoint=guarded_checkpoint
    ).execute(job)
    assert terminal_committed and job.status == JobStatus.SUCCEEDED
    assert len(resources.jobs) == 2


async def test_poll_failure_preserves_concurrent_cancel_intent(graph_env: Any) -> None:
    """外部超时与API取消交错，错误恢复仍保留图谱取消意图。"""
    security, resources, graphs, service, engine = graph_env
    job = await enabled_job(graph_env)

    async def lost_response(scope: str, kb_id: UUID, generation: int) -> GraphTask:
        """原生任务可能已经受理，同时管理员请求取消。"""
        await service.cancel(security, kb_id)
        raise RagError("RAG-RF-003", "合成响应超时", 504, True)

    engine.build = lost_response
    await GraphWorker(resources, graphs, resources, engine, service).execute(job)
    state = await graphs.get(resources.kb.security_scope_id, resources.kb.id)
    assert state.status == GraphStatus.CANCELLING
    assert job.status == JobStatus.RETRY_WAIT


async def test_pending_cancel_is_persisted_as_request_until_worker_confirms(graph_env: Any) -> None:
    """图谱取消API与通用Job取消共用请求状态，排队取消也会被Worker认领收尾。"""
    security, resources, graphs, service, engine = graph_env
    job = await enabled_job(graph_env)
    state = await service.cancel(security, resources.kb.id)
    assert state.status == GraphStatus.CANCELLING
    assert resources.jobs[job.id].status == JobStatus.CANCEL_REQUESTED
    await GraphWorker(resources, graphs, resources, engine, service).execute(job)
    assert job.status == JobStatus.CANCELLED and engine.build_count == 0


async def test_cancel_for_old_job_cannot_cancel_current_successor(graph_env: Any) -> None:
    """取消绑定原请求Job，不能将一个旧任务ID转成对同库新任务的取消。"""
    security, resources, graphs, service, _engine = graph_env
    current = await enabled_job(graph_env)
    original_status = resources.jobs[current.id].status
    with pytest.raises(RagError, match="RAG-GRAPH-005"):
        await service.cancel(security, resources.kb.id, expected_job_id=uuid4())
    state = await graphs.get(resources.kb.security_scope_id, resources.kb.id)
    assert state.status == GraphStatus.QUEUED and state.job_id == current.id
    assert resources.jobs[current.id].status == original_status


async def test_query_switch_during_build_does_not_discard_completed_graph(graph_env: Any) -> None:
    """构建中关闭图谱检索仅影响查询，不废弃已付费结果或触发第二次构图。"""
    security, resources, graphs, service, engine = graph_env
    job = await enabled_job(graph_env, auto_build=True)
    engine.status = GraphStatus.BUILDING

    async def change_retrieval(_seconds: float) -> None:
        """在轮询间隔模拟管理员仅关闭问答检索。"""
        await service.configure(
            security, resources.kb.id,
            GraphConfiguration(enabled=True, auto_build=True, retrieval_enabled=False),
        )
        engine.status = GraphStatus.READY

    await GraphWorker(
        resources, graphs, resources, engine, service, sleep=change_retrieval
    ).execute(job)
    state = await graphs.get(resources.kb.security_scope_id, resources.kb.id)
    assert state.status == GraphStatus.READY and not state.configuration.retrieval_enabled
    assert job.status == JobStatus.SUCCEEDED
    assert len(resources.jobs) == engine.build_count == 1


@pytest.mark.parametrize("auto_build", [False, True])
async def test_query_switch_cannot_publish_a_failed_rebuild_from_previous_revision_marker(
    graph_env: Any, auto_build: bool,
) -> None:
    """同内容重建失败仍保留上次成功修订号，切换检索开关不能据此重新发布 READY。

    自动构建关闭时保持 FAILED；开启时只登记新的构建任务，必须再次通过 Worker
    验收才能恢复 READY，不能把可能部分写入的引擎索引当成旧代数的完整产物。
    """
    security, resources, graphs, service, engine = graph_env
    first = await enabled_job(graph_env, auto_build=auto_build)
    worker = GraphWorker(resources, graphs, resources, engine, service)
    await worker.execute(first)
    rebuild = await service.build(security, resources.kb.id)
    failed = resources.jobs[rebuild.job_id]
    failed.status = JobStatus.RUNNING
    failed.attempt = 1
    engine.status = GraphStatus.FAILED
    await worker.execute(failed)
    previous = await graphs.get(resources.kb.security_scope_id, resources.kb.id)
    assert previous.status == GraphStatus.FAILED
    assert previous.graph_generation == 1 and previous.built_for_revision == resources.kb.revision

    configured = await service.configure(
        security, resources.kb.id,
        GraphConfiguration(
            enabled=True, auto_build=auto_build, retrieval_enabled=False,
        ),
    )
    assert configured.status == (GraphStatus.QUEUED if auto_build else GraphStatus.FAILED)
    assert configured.graph_generation == 1
    assert configured.built_for_revision == resources.kb.revision
    assert len(resources.jobs) == 2 + int(auto_build)
    assert engine.build_count == 2


async def test_query_switch_preserves_stale_state_even_when_content_revision_matches(
    graph_env: Any,
) -> None:
    """取消或保守失效后的 STALE 不代表内容号变化，检索开关也不能绕过重新构建。"""
    security, resources, graphs, service, engine = graph_env
    job = await enabled_job(graph_env)
    await GraphWorker(resources, graphs, resources, engine, service).execute(job)
    graphs.items[resources.kb.security_scope_id, resources.kb.id].status = GraphStatus.STALE

    state = await service.configure(
        security, resources.kb.id,
        GraphConfiguration(enabled=True, auto_build=False, retrieval_enabled=False),
    )
    assert state.status == GraphStatus.STALE
    assert state.built_for_revision == resources.kb.revision and state.graph_generation == 1
    assert len(resources.jobs) == engine.build_count == 1


@pytest.mark.parametrize("had_successful_graph", [False, True])
async def test_reenable_requires_build_instead_of_reusing_unverified_engine_artifacts(
    graph_env: Any, had_successful_graph: bool,
) -> None:
    """停用后重新启用保持未构建或过期；历史成功修订号不能替代当前引擎验收。"""
    security, resources, graphs, service, engine = graph_env
    if had_successful_graph:
        job = await enabled_job(graph_env)
        await GraphWorker(resources, graphs, resources, engine, service).execute(job)
    await service.configure(
        security, resources.kb.id, GraphConfiguration(enabled=False, auto_build=False),
    )
    state = await service.configure(
        security, resources.kb.id, GraphConfiguration(enabled=True, auto_build=False),
    )
    assert state.status == (GraphStatus.STALE if had_successful_graph else GraphStatus.NOT_BUILT)
    assert state.graph_generation == int(had_successful_graph)
    assert len(resources.jobs) == engine.build_count == int(had_successful_graph)


@pytest.mark.parametrize(
    "status", [GraphStatus.QUEUED, GraphStatus.BUILDING, GraphStatus.CANCELLING]
)
@pytest.mark.parametrize("revision_delta", [0, 1])
async def test_content_change_advances_inflight_cas_without_rewriting_submitted_job_snapshot(
    graph_env: Any, status: GraphStatus, revision_delta: int,
) -> None:
    """排队、构建和取消中的内容事件也推进行版本，保留原任务身份及已提交快照。

    revision 不变代表摄取刚启动、活动物理集合可能变化；它同样必须使旧发布重新
    核验来源。目标修订用于后继调度，不覆盖已经用于原生任务的 Job payload。
    """
    _security, resources, graphs, service, _engine = graph_env
    job = await enabled_job(graph_env, auto_build=True)
    job.payload["submission_started"] = True
    stored = graphs.items[resources.kb.security_scope_id, resources.kb.id]
    stored.status = status
    previous_row_version = stored.row_version
    submitted_payload = deepcopy(resources.jobs[job.id].payload)
    resources.kb.revision += revision_delta

    await service.content_changed(resources.kb.security_scope_id, resources.kb.id)

    state = await graphs.get(resources.kb.security_scope_id, resources.kb.id)
    assert state.status == status and state.job_id == job.id
    assert state.row_version == previous_row_version + 1
    assert state.target_content_revision == resources.kb.revision
    assert resources.jobs[job.id].payload == submitted_payload
    assert len(resources.jobs) == 1


@pytest.mark.parametrize("change", ["content", "retrieval_switch"])
@pytest.mark.parametrize("conflict_count", [1, 2])
async def test_publish_cas_conflict_rolls_back_and_revalidates_without_another_paid_build(
    graph_env: Any, change: str, conflict_count: int,
) -> None:
    """在发布已读取内容及图谱状态后提交并发事务，CAS 失败须局部恢复而非永久失败。

    测试从最后一次 Job attempt 开始，故回退普通失败重试会直接 FAILED；只有在
    发布阶段回滚、重新校验来源和重读状态，才能保留原生完成结果及必要的后继任务。
    """
    security, resources, graphs, service, engine = graph_env
    job = await enabled_job(graph_env, auto_build=True)
    job.attempt = job.max_attempts
    previous_revision = resources.kb.revision
    original_update = graphs.update
    changes = 0
    rollbacks = 0
    snapshots: list[int] = []

    async def observe_snapshot() -> None:
        """记录每次来源复核看到的内容修订，证明冲突后没有复用第一次来源快照。"""
        snapshots.append(resources.kb.revision)

    async def concurrent_commit_before_publish(
        scope: str, value: GraphState, *, expected_row_version: int,
    ) -> bool:
        """在发布 CAS 即将执行时独立提交内容或查询开关，留下真实的旧行版本。"""
        nonlocal changes
        if value.status in {GraphStatus.READY, GraphStatus.STALE} and changes < conflict_count:
            changes += 1
            if value.status == GraphStatus.READY:
                assert value.built_for_revision == previous_revision
            if change == "content":
                resources.kb.revision += 1
                await service.content_changed(scope, resources.kb.id)
            else:
                current = await graphs.get(scope, resources.kb.id)
                await service.configure(
                    security, resources.kb.id,
                    GraphConfiguration(
                        enabled=True, auto_build=True,
                        retrieval_enabled=not current.configuration.retrieval_enabled,
                    ),
                )
            current = await graphs.get(scope, resources.kb.id)
            assert current.status == GraphStatus.BUILDING
            assert current.row_version > expected_row_version
        return await original_update(scope, value, expected_row_version=expected_row_version)

    async def rollback_failed_publish() -> None:
        """模拟回滚当前失败的发布事务，已经独立提交的内容/配置事务必须保留。"""
        nonlocal rollbacks
        rollbacks += 1
        state = await graphs.get(resources.kb.security_scope_id, resources.kb.id)
        assert state.status == GraphStatus.BUILDING
        assert resources.jobs[job.id].status == JobStatus.RUNNING

    async def guarded_checkpoint() -> None:
        """最终检查点同时看到旧 Job 终态与必要的新 Job，防止后继被拆到丢租约事务。"""
        if resources.jobs[job.id].status == JobStatus.SUCCEEDED and change == "content":
            state = await graphs.get(resources.kb.security_scope_id, resources.kb.id)
            assert state.status == GraphStatus.QUEUED and state.job_id != job.id

    graphs.update = concurrent_commit_before_publish
    engine.on_snapshot = observe_snapshot
    await GraphWorker(
        resources, graphs, resources, engine, service,
        checkpoint=guarded_checkpoint, rollback=rollback_failed_publish,
    ).execute(job)

    state = await graphs.get(resources.kb.security_scope_id, resources.kb.id)
    assert changes == rollbacks == conflict_count
    assert snapshots == [
        previous_revision + (index if change == "content" else 0)
        for index in range(conflict_count + 1)
    ]
    assert job.status == JobStatus.SUCCEEDED and job.attempt == job.max_attempts
    assert job.payload["content_revision"] == previous_revision
    assert engine.build_count == 1
    if change == "content":
        assert state.status == GraphStatus.QUEUED
        assert state.target_content_revision == resources.kb.revision
        assert state.graph_generation == 0 and job.result["published"] is False
        assert len(resources.jobs) == 2
        assert resources.jobs[state.job_id].payload["content_revision"] == resources.kb.revision
    else:
        assert state.status == GraphStatus.READY
        assert state.configuration.retrieval_enabled == (conflict_count % 2 == 0)
        assert state.graph_generation == 1 and job.result["published"] is True
        assert len(resources.jobs) == 1


async def test_publish_observes_target_revision_when_content_commits_between_two_reads(
    graph_env: Any,
) -> None:
    """内容提交位于 KB 与图谱行两次读取之间时，最新目标修订仍能阻止旧图发布。

    此时 Worker 拿到旧 KB 快照、但图谱行已经是新行版本，单独 CAS 会成功；必须
    同时比较 target_content_revision，才能识别旧内容并原子安排后继构建。
    """
    _security, resources, graphs, service, engine = graph_env
    job = await enabled_job(graph_env, auto_build=True)
    original_get = resources.get
    final_snapshot_seen = False
    changed = False

    async def observe_final_snapshot() -> None:
        """只在最终来源核验之后打开并发注入窗口，不影响首次提交时的内容快照。"""
        nonlocal final_snapshot_seen
        final_snapshot_seen = True

    async def commit_after_knowledge_base_read(scope: str, identifier: UUID) -> Any:
        """先获取旧内容号再提交独立内容变更，之后才让 Worker 读取最新图谱行。"""
        nonlocal changed
        result = await original_get(scope, identifier)
        if final_snapshot_seen and identifier == resources.kb.id and not changed:
            changed = True
            resources.kb.revision += 1
            await service.content_changed(scope, identifier)
        return result

    resources.get = commit_after_knowledge_base_read
    engine.on_snapshot = observe_final_snapshot
    await GraphWorker(resources, graphs, resources, engine, service).execute(job)

    state = await graphs.get(resources.kb.security_scope_id, resources.kb.id)
    assert changed and job.status == JobStatus.SUCCEEDED
    assert job.result["published"] is False and state.graph_generation == 0
    assert state.status == GraphStatus.QUEUED and len(resources.jobs) == 2
    assert state.target_content_revision == resources.kb.revision
    assert job.payload["content_revision"] < state.target_content_revision
    assert engine.build_count == 1


async def test_persistent_publish_cas_conflicts_stop_after_three_local_attempts(
    graph_env: Any,
) -> None:
    """持续内容事件最多重试三次发布；任务额度耗尽明确失败，不能无限占用 Worker。"""
    _security, resources, graphs, service, engine = graph_env
    job = await enabled_job(graph_env, auto_build=True)
    job.attempt = job.max_attempts
    original_update = graphs.update
    publication_attempts = 0

    async def keep_conflicting_with_publish(
        scope: str, value: GraphState, *, expected_row_version: int,
    ) -> bool:
        """每次 READY CAS 前提交一次内容事件；只推进真实行版本，不伪造仓库失败。"""
        nonlocal publication_attempts
        if value.status == GraphStatus.READY:
            publication_attempts += 1
            await service.content_changed(scope, resources.kb.id, auto_build=False)
        return await original_update(scope, value, expected_row_version=expected_row_version)

    graphs.update = keep_conflicting_with_publish
    await GraphWorker(resources, graphs, resources, engine, service).execute(job)

    state = await graphs.get(resources.kb.security_scope_id, resources.kb.id)
    assert publication_attempts == 3
    assert job.status == JobStatus.FAILED and job.error_code == "RAG-GRAPH-005"
    assert state.status == GraphStatus.FAILED and len(resources.jobs) == 1
    assert engine.build_count == 1 and job.payload["submission_started"]
