"""图谱持久化编排测试：隔离、幂等、提交恢复、版本发布和取消未知状态。"""

from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from rag_service.application.graph import GraphService
from rag_service.application.graph_worker import GraphWorker
from rag_service.domain.aggregate import AggregateEvidence, AggregateSource
from rag_service.domain.errors import RagError
from rag_service.domain.graph import (
    GraphConfiguration,
    GraphProfile,
    GraphSnapshot,
    GraphState,
    GraphStatus,
    GraphTask,
    native_graph_parser_config,
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
        self.current_task_id: str | None = None
        self.configure_calls: list[str | None] = []
        self.on_snapshot: Any = None

    async def configure(
        self, scope: str, kb_id: UUID, generation: int, configuration: GraphConfiguration,
        *, previous_task_id: str | None,
    ) -> None:
        """配置操作不计作收费构图；记录准备身份并模拟终态图清理后的任务记录消失。"""
        self.configure_calls.append(previous_task_id)
        if previous_task_id is None and self.current_task_id is not None:
            raise RagError("RAG-GRAPH-007", "合成原生任务已变化", 409, False)
        if previous_task_id is not None and self.current_task_id is not None:
            if self.current_task_id != previous_task_id:
                raise RagError("RAG-GRAPH-007", "合成原生任务已替换", 409, False)
            if self.status not in {GraphStatus.READY, GraphStatus.FAILED}:
                raise RagError("RAG-GRAPH-006", "合成原生任务未结束", 409, False)
        self.current_task_id = None

    async def build(self, scope: str, kb_id: UUID, generation: int) -> GraphTask:
        """模拟请求可能已被接受但响应丢失。"""
        self.build_count += 1
        self.current_task_id = f"task-{self.build_count}"
        if self.response_lost:
            raise RagError("RAG-RF-003", "构图响应超时", 504, True)
        return GraphTask(scope, kb_id, generation, self.current_task_id, self.status, 1.0)

    async def task(self, scope: str, kb_id: UUID, generation: int) -> GraphTask | None:
        """回读同一任务或模拟取消后上游删除任务记录。"""
        if self.absent or self.current_task_id is None:
            return None
        return GraphTask(scope, kb_id, generation, self.current_task_id, self.status, 1.0)

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


async def enabled_job(
    env: Any, *, auto_build: bool = False,
    profile_ref: str = GraphProfile.NATIVE_LIGHT_V1.value,
) -> IngestionJob:
    """启用配置并认领一个构图 Job，模拟 Runner 已完成的原子认领。"""
    security, resources, _graphs, service, _engine = env
    await service.configure(
        security, resources.kb.id,
        GraphConfiguration(enabled=True, auto_build=auto_build, profile_ref=profile_ref),
    )
    state = await service.build(security, resources.kb.id)
    job = resources.jobs[state.job_id]
    job.status = JobStatus.RUNNING
    job.attempt = 1
    return job


class Reports:
    """可恢复的报告替身；模型结果只生成一次，后续调用复用原始配置版本的结果。"""

    def __init__(self) -> None:
        """计数调用与实际生成，支持在收费阶段注入内容、查询配置和取消交错。"""
        self.calls = 0
        self.generated = 0
        self.result: AggregateEvidence | None = None
        self.on_build: Any = None
        self.overrides: dict[str, Any] = {}

    async def build(
        self, job: IngestionJob, state: GraphState, generation: int,
    ) -> tuple[AggregateEvidence, ...]:
        """生成带两份真实形状原文来源的合成报告，不访问模型或外部服务。"""
        self.calls += 1
        if self.result is None:
            self.generated += 1
            sources = tuple(AggregateSource(
                knowledge_base_id=state.knowledge_base_id, document_id=uuid4(),
                document_name=f"source-{index}.txt", engine_chunk_id=f"chunk-{index}",
                quote=f"ATLAS original source {index}.", resource_scope_id=job.security_scope_id,
                document_revision=1, active_version_id=uuid4(),
                engine_document_id=f"document-{index}",
                quote_sha256=hashlib.sha256(f"ATLAS original source {index}.".encode()).hexdigest(),
            ) for index in (1, 2))
            self.result = AggregateEvidence(
                aggregate_id=uuid4(), label="", knowledge_base_id=state.knowledge_base_id,
                community_id="synthetic-community", title="合成社区", summary="合成多来源报告。",
                graph_generation=generation, content_revision=job.payload["content_revision"],
                configuration_version=state.configuration_version,
                graph_profile_ref=state.configuration.profile_ref, report_sha256="a" * 64,
                sources=sources, resource_scope_id=job.security_scope_id,
                generator_model_ref="synthetic-report-chat",
            )
        if self.on_build:
            await self.on_build()
        return (replace(self.result, **self.overrides),)


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
    # 完成后的更新时间必须覆盖发布时刻，不能一直停留在最初排队时间。
    assert state.completed_at is not None and state.updated_at >= state.completed_at
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
    assert engine.configure_calls == [None]


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
        """记录拟发布 READY 的来源复核；内容已过时后不再验收不会使用的旧图。"""
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
    assert snapshots == (
        [previous_revision] if change == "content"
        else [previous_revision] * (conflict_count + 1)
    )
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


async def test_preparation_intent_is_checkpointed_before_configure_and_submission(
    graph_env: Any,
) -> None:
    """首次准备身份先独立提交；配置成功后才持久化真正提交标记并调用收费构图。"""
    _security, resources, graphs, service, engine = graph_env
    job = await enabled_job(graph_env)
    original_configure = engine.configure
    original_build = engine.build

    async def verify_preparation(
        scope: str, kb_id: UUID, generation: int, configuration: GraphConfiguration,
        *, previous_task_id: str | None,
    ) -> None:
        """清理配置之前，仓库必须已有独立准备记录且尚未声明真正提交。"""
        stored = resources.jobs[job.id]
        assert stored.payload["preparation_started"] is True
        assert "preparation_task_id" in stored.payload and previous_task_id is None
        assert stored.payload["preparation_task_id"] is None
        assert stored.payload["previous_engine_task_id"] is None
        assert not stored.payload.get("submission_started") and engine.build_count == 0
        await original_configure(
            scope, kb_id, generation, configuration, previous_task_id=previous_task_id,
        )

    async def verify_submission(scope: str, kb_id: UUID, generation: int) -> GraphTask:
        """POST 原生任务之前，配置已经成功且真正提交标记已经保存在仓库。"""
        assert resources.jobs[job.id].payload["submission_started"] is True
        assert engine.configure_calls == [None]
        return await original_build(scope, kb_id, generation)

    engine.configure = verify_preparation
    engine.build = verify_submission
    await GraphWorker(resources, graphs, resources, engine, service).execute(job)
    assert job.status == JobStatus.SUCCEEDED and engine.build_count == 1


@pytest.mark.parametrize("status,identifier", [
    (GraphStatus.QUEUED, "busy-task"),
    (GraphStatus.BUILDING, "busy-task"),
    (GraphStatus.CANCELLING, "busy-task"),
    (GraphStatus.READY, None),
    (GraphStatus.FAILED, ""),
])
async def test_nonterminal_or_unidentified_preflight_never_clears_or_submits(
    graph_env: Any, status: GraphStatus, identifier: str | None,
) -> None:
    """准备之前必须知道旧任务已结束且具有真实 ID，不能把未知或运行状态当成清理授权。"""
    _security, resources, graphs, service, engine = graph_env
    job = await enabled_job(graph_env)

    async def unsafe_observation(scope: str, kb_id: UUID, generation: int) -> GraphTask:
        """显式模拟 Port 返回不可清理的任务观察；None 返回值与缺 ID 的对象不同。"""
        return GraphTask(scope, kb_id, generation, identifier, status)

    engine.task = unsafe_observation
    await GraphWorker(resources, graphs, resources, engine, service).execute(job)
    assert job.status == JobStatus.FAILED and job.error_code == "RAG-GRAPH-006"
    assert not job.payload.get("preparation_started")
    assert engine.configure_calls == [] and engine.build_count == 0


async def test_preparation_records_actually_observed_terminal_identity(graph_env: Any) -> None:
    """准备身份使用引擎真实观察，不沿用可能过期的本地历史任务 ID。"""
    _security, resources, graphs, service, engine = graph_env
    job = await enabled_job(graph_env)
    job.payload["previous_engine_task_id"] = "stale-local-task"
    engine.current_task_id = "observed-terminal-task"
    await GraphWorker(resources, graphs, resources, engine, service).execute(job)
    assert job.status == JobStatus.SUCCEEDED
    assert job.payload["preparation_task_id"] == "observed-terminal-task"
    assert job.payload["previous_engine_task_id"] == "observed-terminal-task"
    assert engine.configure_calls == ["observed-terminal-task"]
    assert engine.build_count == 1 and engine.current_task_id == "task-1"


class _PreparationProcessCrash(BaseException):
    """模拟清理已生效、但进程在真正提交之前退出，不被普通业务异常恢复吞掉。"""


async def test_crash_after_cleanup_recovers_from_persisted_terminal_intent_without_double_build(
    graph_env: Any,
) -> None:
    """清理已经删除任务记录后崩溃，恢复必须复用旧终态意图，而不是把空任务当新授权。"""
    _security, resources, graphs, service, engine = graph_env
    job = await enabled_job(graph_env)
    engine.current_task_id = "old-terminal-task"
    original_configure = engine.configure
    original_task = engine.task
    observations = 0

    async def count_preflight(scope: str, kb_id: UUID, generation: int) -> GraphTask | None:
        """记录旧任务终态观察次数，恢复不能重观察空任务并覆盖已经提交的旧身份。"""
        nonlocal observations
        observations += 1
        return await original_task(scope, kb_id, generation)

    async def crash_after_cleanup(
        scope: str, kb_id: UUID, generation: int, configuration: GraphConfiguration,
        *, previous_task_id: str | None,
    ) -> None:
        """先模拟真实清理效果，再让进程退出，保留独立准备事务及未提交收费的事实。"""
        await original_configure(
            scope, kb_id, generation, configuration, previous_task_id=previous_task_id,
        )
        raise _PreparationProcessCrash

    engine.task = count_preflight
    engine.configure = crash_after_cleanup
    with pytest.raises(_PreparationProcessCrash):
        await GraphWorker(resources, graphs, resources, engine, service).execute(job)
    persisted = deepcopy(resources.jobs[job.id])
    assert persisted.payload["preparation_task_id"] == "old-terminal-task"
    assert persisted.payload["preparation_started"] is True
    assert not persisted.payload.get("submission_started")
    assert engine.current_task_id is None and engine.build_count == 0

    engine.configure = original_configure
    await GraphWorker(resources, graphs, resources, engine, service).execute(persisted)
    state = await graphs.get(resources.kb.security_scope_id, resources.kb.id)
    assert observations == 1
    assert engine.configure_calls == ["old-terminal-task", "old-terminal-task"]
    assert persisted.status == JobStatus.SUCCEEDED and state.status == GraphStatus.READY
    assert engine.build_count == state.graph_generation == 1


async def test_incomplete_preparation_marker_is_not_treated_as_first_build(graph_env: Any) -> None:
    """已有准备标记却没有旧任务字段属于未知恢复状态，不能隐式采用首次构图清理规则。"""
    _security, resources, graphs, service, engine = graph_env
    job = await enabled_job(graph_env)
    job.payload["preparation_started"] = True
    await GraphWorker(resources, graphs, resources, engine, service).execute(job)
    assert job.status == JobStatus.FAILED and job.error_code == "RAG-GRAPH-006"
    assert engine.configure_calls == [] and engine.build_count == 0


async def test_cancel_after_preparation_checkpoint_stops_before_destructive_configuration(
    graph_env: Any,
) -> None:
    """准备意图提交后管理员取消，复读取消状态应阻止清图配置和收费提交。"""
    security, resources, graphs, service, engine = graph_env
    job = await enabled_job(graph_env)
    cancellation_sent = False

    async def cancel_after_preparation() -> None:
        """只在独立准备检查点注入一次并发取消，之后收尾检查点仍可正常提交。"""
        nonlocal cancellation_sent
        if job.payload.get("preparation_started") and not cancellation_sent:
            cancellation_sent = True
            await service.cancel(security, resources.kb.id)

    await GraphWorker(
        resources, graphs, resources, engine, service, checkpoint=cancel_after_preparation,
    ).execute(job)
    assert cancellation_sent and job.status == JobStatus.CANCELLED
    assert engine.configure_calls == [] and engine.build_count == 0


async def test_obsolete_completed_graph_schedules_one_successor_without_reading_deleted_sources(
    graph_env: Any,
) -> None:
    """旧原生任务已完成但内容已删除时，只创建最新后继，不验收或发布旧来源图。"""
    _security, resources, graphs, service, engine = graph_env
    job = await enabled_job(graph_env, auto_build=True)
    worker = GraphWorker(resources, graphs, resources, engine, service)
    engine.response_lost = True
    await worker.execute(job)
    assert job.status == JobStatus.RETRY_WAIT and engine.build_count == 1
    resources.kb.revision += 1
    await service.content_changed(resources.kb.security_scope_id, resources.kb.id)
    snapshot_count = 0

    async def deleted_source_snapshot(scope: str, kb_id: UUID, generation: int) -> GraphSnapshot:
        """旧图若被读取就会因删除来源失败；过时结果没有必要承担这项发布验收。"""
        nonlocal snapshot_count
        snapshot_count += 1
        raise RagError("RAG-GRAPH-004", "合成旧图仍含已删除文档来源", 409, False)

    engine.snapshot = deleted_source_snapshot
    job.status = JobStatus.RUNNING
    job.attempt += 1
    await worker.execute(job)
    state = await graphs.get(resources.kb.security_scope_id, resources.kb.id)
    assert snapshot_count == 0
    assert job.status == JobStatus.SUCCEEDED and job.result["published"] is False
    assert state.status == GraphStatus.QUEUED and state.graph_generation == 0
    assert len(resources.jobs) == 2 and engine.build_count == 1
    assert resources.jobs[state.job_id].payload["content_revision"] == resources.kb.revision
    assert engine.configure_calls == [None]


async def test_deleted_source_during_snapshot_converges_to_one_successor_without_ready(
    graph_env: Any,
) -> None:
    """来源验收期间完成删除导致 GRAPH004，复读证明已过时后安排后继，不永久失败。"""
    _security, resources, graphs, service, engine = graph_env
    job = await enabled_job(graph_env, auto_build=True)
    job.attempt = job.max_attempts
    snapshot_count = 0
    saved_statuses: list[GraphStatus] = []
    original_update = graphs.update

    async def record_update(scope: str, state: GraphState, *, expected_row_version: int) -> bool:
        """记录真实状态写入，确保旧图未曾以 READY 暴露而后又被补救撤回。"""
        saved_statuses.append(state.status)
        return await original_update(scope, state, expected_row_version=expected_row_version)

    async def delete_during_snapshot(scope: str, kb_id: UUID, generation: int) -> GraphSnapshot:
        """模拟文档删除事务已提交，但旧原生图节点仍保留该文档的 source_id。"""
        nonlocal snapshot_count
        snapshot_count += 1
        resources.kb.revision += 1
        await service.content_changed(scope, kb_id)
        raise RagError("RAG-GRAPH-004", "合成旧图仍含已删除文档来源", 409, False)

    graphs.update = record_update
    engine.snapshot = delete_during_snapshot
    await GraphWorker(resources, graphs, resources, engine, service).execute(job)
    state = await graphs.get(resources.kb.security_scope_id, resources.kb.id)
    assert snapshot_count == 1 and GraphStatus.READY not in saved_statuses
    assert job.status == JobStatus.SUCCEEDED and job.result["published"] is False
    assert state.status == GraphStatus.QUEUED and state.graph_generation == 0
    assert len(resources.jobs) == 2 and engine.build_count == 1
    assert resources.jobs[state.job_id].payload["content_revision"] == resources.kb.revision


@pytest.mark.parametrize("receipt_only", [False, True])
async def test_source_failure_without_obsolete_content_still_fails_closed(
    graph_env: Any, receipt_only: bool,
) -> None:
    """内容和目标修订未落后时，来源异常保持 FAILED；只有行版本推进不能替代证据。"""
    _security, resources, graphs, service, engine = graph_env
    job = await enabled_job(graph_env, auto_build=True)
    snapshot_count = 0

    async def invalid_source(scope: str, kb_id: UUID, generation: int) -> GraphSnapshot:
        """可选地只登记摄取 receipt，但没有内容提交可以证明旧快照已经过时。"""
        nonlocal snapshot_count
        snapshot_count += 1
        if receipt_only:
            await service.content_changed(scope, kb_id, auto_build=False)
        raise RagError("RAG-GRAPH-004", "合成图来源无法核验", 409, False)

    engine.snapshot = invalid_source
    await GraphWorker(resources, graphs, resources, engine, service).execute(job)
    state = await graphs.get(resources.kb.security_scope_id, resources.kb.id)
    assert snapshot_count == 1
    assert job.status == JobStatus.FAILED and job.error_code == "RAG-GRAPH-004"
    assert state.status == GraphStatus.FAILED and state.graph_generation == 0
    assert len(resources.jobs) == engine.build_count == 1


async def test_receipt_during_valid_snapshot_requires_revalidation_with_original_cas_version(
    graph_env: Any,
) -> None:
    """snapshot 期间只推进 row_version 也须重验，不能用之后新读的版本直接发布。"""
    _security, resources, graphs, service, engine = graph_env
    job = await enabled_job(graph_env, auto_build=True)
    job.attempt = job.max_attempts
    snapshots = 0
    rollbacks = 0
    original_revision = resources.kb.revision

    async def register_receipt_once() -> None:
        """第一次来源校验期间只登记物理集合可能变化，尚未递增内容修订。"""
        nonlocal snapshots
        snapshots += 1
        if snapshots == 1:
            await service.content_changed(resources.kb.security_scope_id, resources.kb.id)

    async def rollback_obsolete_validation() -> None:
        """回滚过期的来源验收，独立 receipt 保持提交，原生任务不重建。"""
        nonlocal rollbacks
        rollbacks += 1
        state = await graphs.get(resources.kb.security_scope_id, resources.kb.id)
        assert state.status == GraphStatus.BUILDING

    engine.on_snapshot = register_receipt_once
    await GraphWorker(
        resources, graphs, resources, engine, service, rollback=rollback_obsolete_validation,
    ).execute(job)
    state = await graphs.get(resources.kb.security_scope_id, resources.kb.id)
    assert resources.kb.revision == original_revision
    assert snapshots == 2 and rollbacks == 1
    assert state.status == GraphStatus.READY and state.graph_generation == 1
    assert job.status == JobStatus.SUCCEEDED and len(resources.jobs) == engine.build_count == 1


def test_summary_profile_keeps_existing_native_light_parser_flags_unchanged() -> None:
    """新版本只增加 Gateway 阶段，不能原地开启不可公开回溯的原生社区/消歧功能。"""
    light = GraphConfiguration(enabled=True)
    summaries = replace(light, profile_ref=GraphProfile.NATIVE_WITH_SUMMARIES_V1.value)
    assert light.profile_ref == "native-graph-light@1"
    assert native_graph_parser_config(light) == native_graph_parser_config(summaries)
    assert native_graph_parser_config(summaries)["community"] is False
    assert native_graph_parser_config(summaries)["resolution"] is False


async def test_summary_profile_publishes_reports_between_two_strict_snapshots(
    graph_env: Any,
) -> None:
    """报告必须在两次来源验收之间完成，最终状态保存独立聚合证据及同版本边界。"""
    _security, resources, graphs, service, engine = graph_env
    job = await enabled_job(graph_env, profile_ref=GraphProfile.NATIVE_WITH_SUMMARIES_V1.value)
    builder = Reports()
    events: list[str] = []

    async def snapshot() -> None:
        """记录发布前原生来源验收的真实先后顺序。"""
        events.append("snapshot")

    async def report() -> None:
        """报告模型阶段只能发生于第一次完整来源验收之后。"""
        events.append("report")

    engine.on_snapshot = snapshot
    builder.on_build = report
    await GraphWorker(
        resources, graphs, resources, engine, service, report_builder=builder,
    ).execute(job)
    state = await graphs.get(resources.kb.security_scope_id, resources.kb.id)
    assert state.status == GraphStatus.READY and job.status == JobStatus.SUCCEEDED
    assert events == ["snapshot", "report", "snapshot"]
    assert len(state.community_reports) == builder.generated == builder.calls == 1
    assert engine.build_count == 1
    assert state.community_reports[0].graph_generation == state.graph_generation == 1
    assert state.community_reports[0].configuration_version == state.configuration_version


@pytest.mark.parametrize("summary_profile", [False, True])
async def test_summary_builder_is_required_only_for_new_profile(
    graph_env: Any, summary_profile: bool,
) -> None:
    """旧策略不调用报告模型；新策略缺少构建器必须明确失败，不能假称摘要已可用。"""
    _security, resources, graphs, service, engine = graph_env
    profile = (GraphProfile.NATIVE_WITH_SUMMARIES_V1 if summary_profile
               else GraphProfile.NATIVE_LIGHT_V1)
    job = await enabled_job(graph_env, profile_ref=profile.value)
    builder = Reports()
    await GraphWorker(
        resources, graphs, resources, engine, service,
        report_builder=None if summary_profile else builder,
    ).execute(job)
    state = await graphs.get(resources.kb.security_scope_id, resources.kb.id)
    assert builder.calls == 0 and state.community_reports == ()
    assert engine.build_count == 1
    if summary_profile:
        assert state.status == GraphStatus.FAILED and job.error_code == "RAG-GRAPH-013"
    else:
        assert state.status == GraphStatus.READY and job.status == JobStatus.SUCCEEDED


@pytest.mark.parametrize("during_report", [False, True])
async def test_obsolete_report_stage_never_publishes_and_schedules_one_successor(
    graph_env: Any, during_report: bool,
) -> None:
    """原生验收或报告期间内容变更只安排一个后继；明知过时前不得额外调用报告模型。"""
    _security, resources, graphs, service, engine = graph_env
    job = await enabled_job(
        graph_env, auto_build=True, profile_ref=GraphProfile.NATIVE_WITH_SUMMARIES_V1.value,
    )
    builder = Reports()
    changed = False

    async def change_content_once() -> None:
        """模拟独立摄取事务已发布新内容，推进图谱目标和 CAS 版本。"""
        nonlocal changed
        if not changed:
            changed = True
            resources.kb.revision += 1
            await service.content_changed(resources.kb.security_scope_id, resources.kb.id)

    if during_report:
        builder.on_build = change_content_once
    else:
        engine.on_snapshot = change_content_once
    await GraphWorker(
        resources, graphs, resources, engine, service, report_builder=builder,
    ).execute(job)
    state = await graphs.get(resources.kb.security_scope_id, resources.kb.id)
    assert job.status == JobStatus.SUCCEEDED and job.result["published"] is False
    assert state.status == GraphStatus.QUEUED and state.graph_generation == 0
    assert state.community_reports == () and len(resources.jobs) == 2
    assert builder.generated == builder.calls == int(during_report)
    assert engine.build_count == 1


async def test_query_switch_during_report_reuses_paid_result_and_rebinds_configuration(
    graph_env: Any,
) -> None:
    """查询开关推进 CAS 后重新验收，复用同图同内容报告，不能重复模型或原生构图收费。"""
    security, resources, graphs, service, engine = graph_env
    job = await enabled_job(graph_env, profile_ref=GraphProfile.NATIVE_WITH_SUMMARIES_V1.value)
    builder = Reports()
    switched = False

    async def switch_query_once() -> None:
        """只变更检索开关，不改变不可变构图策略和内容指纹。"""
        nonlocal switched
        if not switched:
            switched = True
            await service.configure(security, resources.kb.id, GraphConfiguration(
                enabled=True, profile_ref=GraphProfile.NATIVE_WITH_SUMMARIES_V1.value,
                auto_build=False, retrieval_enabled=False,
            ))

    builder.on_build = switch_query_once
    await GraphWorker(
        resources, graphs, resources, engine, service, report_builder=builder,
    ).execute(job)
    state = await graphs.get(resources.kb.security_scope_id, resources.kb.id)
    assert state.status == GraphStatus.READY and job.status == JobStatus.SUCCEEDED
    assert builder.calls == 2 and builder.generated == engine.build_count == 1
    assert builder.result is not None
    assert builder.result.configuration_version < state.configuration_version
    assert state.community_reports[0].configuration_version == state.configuration_version
    assert len(resources.jobs) == 1


async def test_cancel_during_report_cannot_publish_ready_or_reports(graph_env: Any) -> None:
    """取消期间即使模型已返回，也必须优先按已完成原生任务确认取消，不能发布摘要。"""
    security, resources, graphs, service, engine = graph_env
    job = await enabled_job(graph_env, profile_ref=GraphProfile.NATIVE_WITH_SUMMARIES_V1.value)
    builder = Reports()

    async def cancel() -> None:
        """模拟报告完成的同一时刻管理员登记取消。"""
        await service.cancel(security, resources.kb.id)

    builder.on_build = cancel
    await GraphWorker(
        resources, graphs, resources, engine, service, report_builder=builder,
    ).execute(job)
    state = await graphs.get(resources.kb.security_scope_id, resources.kb.id)
    assert job.status == JobStatus.CANCELLED and state.status == GraphStatus.STALE
    assert state.community_reports == () and state.graph_generation == 0
    assert len(resources.jobs) == builder.generated == engine.build_count == 1


@pytest.mark.parametrize("field,value", [
    ("resource_scope_id", "foreign-scope"),
    ("knowledge_base_id", UUID("60000000-0000-0000-0000-000000000001")),
    ("graph_generation", 100),
    ("content_revision", 100),
    ("graph_profile_ref", GraphProfile.NATIVE_LIGHT_V1.value),
])
async def test_invalid_report_scope_or_build_fingerprint_is_never_published(
    graph_env: Any, field: str, value: Any,
) -> None:
    """报告范围、代数、内容和策略必须属于当前原生构建；不能重绑这些收费输入边界。"""
    _security, resources, graphs, service, engine = graph_env
    job = await enabled_job(graph_env, profile_ref=GraphProfile.NATIVE_WITH_SUMMARIES_V1.value)
    builder = Reports()
    builder.overrides[field] = value
    await GraphWorker(
        resources, graphs, resources, engine, service, report_builder=builder,
    ).execute(job)
    state = await graphs.get(resources.kb.security_scope_id, resources.kb.id)
    assert job.status == JobStatus.FAILED and job.error_code == "RAG-GRAPH-013"
    assert state.status == GraphStatus.FAILED and state.community_reports == ()
    assert engine.build_count == 1


async def test_second_source_snapshot_failure_refuses_generated_report(graph_env: Any) -> None:
    """模型返回后引擎来源变化而本地内容未变，二次严格验收必须拒绝发布。"""
    _security, resources, graphs, service, engine = graph_env
    job = await enabled_job(graph_env, profile_ref=GraphProfile.NATIVE_WITH_SUMMARIES_V1.value)
    builder = Reports()
    snapshots = 0

    async def source_changes_after_report() -> None:
        """第一轮正常，模型之后发现原生图不再满足活动来源边界。"""
        nonlocal snapshots
        snapshots += 1
        if snapshots == 2:
            raise RagError("RAG-GRAPH-004", "合成图来源发生无版本变化的异常", 409, False)

    engine.on_snapshot = source_changes_after_report
    await GraphWorker(
        resources, graphs, resources, engine, service, report_builder=builder,
    ).execute(job)
    state = await graphs.get(resources.kb.security_scope_id, resources.kb.id)
    assert snapshots == 2 and job.error_code == "RAG-GRAPH-004"
    assert state.status == GraphStatus.FAILED and state.community_reports == ()
    assert builder.generated == engine.build_count == 1
