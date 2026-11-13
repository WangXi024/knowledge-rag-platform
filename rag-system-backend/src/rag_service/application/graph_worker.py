"""原生构图持久化任务的可恢复执行器。

外部提交前先写检查点；提交响应丢失后仅回读原生任务，不自动再次收费构图。
轮询失败可有限重试并沿用原生任务 ID。完成后再次核对活动来源、内容和配置版本。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta

from rag_service.application.graph import GraphService, noop_graph_checkpoint
from rag_service.domain.errors import JobCancellationRequested, JobLeaseLostError, RagError
from rag_service.domain.graph import GraphPort, GraphRepository, GraphState, GraphStatus, GraphTask
from rag_service.domain.models import IngestionJob, JobStatus, KnowledgeBaseStatus, utc_now
from rag_service.domain.ports import JobRepository, KnowledgeBaseRepository


class GraphWorker:
    """只处理 GRAPH_BUILD，复用 Runner 的认领、独立心跳和租约保护提交。"""

    def __init__(
        self,
        knowledge_bases: KnowledgeBaseRepository,
        graphs: GraphRepository,
        jobs: JobRepository,
        engine: GraphPort,
        service: GraphService,
        *,
        checkpoint: Callable[[], Awaitable[None]] = noop_graph_checkpoint,
        rollback: Callable[[], Awaitable[None]] = noop_graph_checkpoint,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        poll_seconds: float = 3.0,
        timeout_seconds: float = 1800.0,
    ) -> None:
        """注入事务回调和有界轮询参数；测试可以使用确定性时钟而无需等待。"""
        self._knowledge_bases = knowledge_bases
        self._graphs = graphs
        self._jobs = jobs
        self._engine = engine
        self._service = service
        self._checkpoint = checkpoint
        self._rollback = rollback
        self._sleep = sleep
        self._monotonic = monotonic
        self._poll_seconds = poll_seconds
        self._timeout_seconds = timeout_seconds

    async def execute(self, job: IngestionJob) -> IngestionJob:
        """执行已认领任务，失败只保存脱敏稳定错误，不能覆盖其他任务的新状态。"""
        try:
            await self._run(job)
        except JobLeaseLostError:
            raise
        except JobCancellationRequested:
            # 成功提交与API取消可以同时发生；回滚未发布图谱后优先确认原生取消。
            await self._rollback()
            state = await self._state(job)
            if state is not None:
                try:
                    await self._cancel(job, state)
                except RagError as error:
                    await self._rollback()
                    await self._fail(job, error)
        except RagError as error:
            await self._rollback()
            await self._fail(job, error)
        except Exception:
            await self._rollback()
            await self._fail(job, RagError("RAG-GRAPH-099", "图谱任务执行失败", 500, False))
        return job

    async def _run(self, job: IngestionJob) -> None:
        """恢复已有引擎任务或提交唯一新任务，然后轮询并按版本快照发布。"""
        state = await self._state(job)
        if state is None:
            await self._finish(job, JobStatus.CANCELLED)
            return
        if state.status == GraphStatus.CANCELLING or not state.configuration.enabled:
            await self._cancel(job, state)
            return
        # 在任何外部写操作前验证当前租约并释放事务锁，避免失去所有权后仍配置引擎。
        await self._checkpoint()
        generation = state.graph_generation + 1
        task: GraphTask | None
        if not job.payload.get("submission_started"):
            kb = await self._knowledge_bases.get(job.security_scope_id, state.knowledge_base_id)
            if kb is None or kb.status != KnowledgeBaseStatus.READY:
                await self._finish(job, JobStatus.CANCELLED)
                return
            # 排队期间可合并多份解析结果；和提交标记一起持久化后才开始收费构图。
            # 一旦 submission_started 已提交，恢复分支必须保留原内容快照。
            state.target_content_revision = kb.revision
            job.payload["content_revision"] = kb.revision
            # 原生参数的更新可重试；真正构图之前的持久化标记不允许省略。
            await self._engine.configure(
                job.security_scope_id, state.knowledge_base_id, generation, state.configuration
            )
            state.status = GraphStatus.BUILDING
            state.started_at = state.started_at or utc_now()
            await self._save(state)
            job.payload["submission_started"] = True
            job.payload["submitted_at"] = utc_now().isoformat()
            await self._jobs.update(job.security_scope_id, job)
            await self._checkpoint()
            task = await self._engine.build(
                job.security_scope_id, state.knowledge_base_id, generation
            )
        else:
            # 网络错误或进程崩溃都只能沿当前任务恢复。无法区分是否已提交时失败封闭。
            task = await self._engine.task(
                job.security_scope_id, state.knowledge_base_id, generation
            )
            if task is None or (
                not state.engine_task_id
                and task.engine_task_id == job.payload.get("previous_engine_task_id")
            ):
                raise RagError(
                    "RAG-GRAPH-006", "无法确认原生构图提交结果，请核查后手动重试", 409, False
                )
        if task.engine_task_id is None:
            raise RagError("RAG-GRAPH-006", "原生构图未返回可恢复任务标识", 502, False)
        if state.engine_task_id is not None and state.engine_task_id != task.engine_task_id:
            raise RagError("RAG-GRAPH-007", "原生任务已被其他构建替换", 409, False)
        state.engine_task_id = task.engine_task_id
        await self._save(state)
        await self._checkpoint()
        deadline = self._monotonic() + self._timeout_seconds
        while True:
            state = await self._state(job)
            if state is None:
                await self._finish(job, JobStatus.CANCELLED)
                return
            current_job = await self._jobs.get(job.security_scope_id, job.id)
            if (
                state.status == GraphStatus.CANCELLING
                or not state.configuration.enabled
                or (current_job is not None and current_job.status == JobStatus.CANCEL_REQUESTED)
            ):
                await self._cancel(job, state)
                return
            if task.engine_task_id != state.engine_task_id:
                raise RagError("RAG-GRAPH-007", "原生任务已被其他构建替换", 409, False)
            if task.status == GraphStatus.READY:
                await self._publish(job, state, generation)
                return
            if task.status == GraphStatus.FAILED:
                raise RagError("RAG-GRAPH-008", "RAGFlow 原生构图失败", 502, False)
            if self._monotonic() >= deadline:
                # 超时保持原生任务身份，重试只轮询；不会再次提交收费任务。
                raise RagError("RAG-GRAPH-009", "构图轮询超时，原生任务可能仍在运行", 504, True)
            job.progress = min(0.99, max(0.0, task.progress))
            await self._jobs.update(job.security_scope_id, job)
            await self._checkpoint()
            await self._sleep(self._poll_seconds)
            observed = await self._engine.task(
                job.security_scope_id, state.knowledge_base_id, generation
            )
            if observed is None:
                raise RagError("RAG-GRAPH-010", "原生任务暂时不可确认", 502, True)
            task = observed

    async def _publish(self, job: IngestionJob, state: GraphState, generation: int) -> None:
        """二次验证来源后发布新代数；内容变化时只保存过期状态并安排最新快照。"""
        # snapshot 适配器会重新核对 Dataset 的全部物理 ID 与本地活动文档边界。
        await self._engine.snapshot(job.security_scope_id, state.knowledge_base_id, generation)
        kb = await self._knowledge_bases.get(job.security_scope_id, state.knowledge_base_id)
        refreshed = await self._state(job)
        if kb is None or kb.status != KnowledgeBaseStatus.READY or refreshed is None:
            await self._finish(job, JobStatus.CANCELLED)
            return
        state = refreshed
        current_job = await self._jobs.get(job.security_scope_id, job.id)
        if (
            state.status == GraphStatus.CANCELLING
            or not state.configuration.enabled
            or (current_job is not None and current_job.status == JobStatus.CANCEL_REQUESTED)
        ):
            await self._cancel(job, state)
            return
        publishable = (
            state.configuration.enabled
            and kb.revision == job.payload.get("content_revision")
            # 检索开关改变不使已经付费的图过期；不可变构图策略引用才是构建身份。
            and state.configuration.profile_ref == job.payload.get("profile_ref")
        )
        state.status = GraphStatus.READY if publishable else GraphStatus.STALE
        if publishable:
            state.graph_generation = generation
            state.built_for_revision = kb.revision
        state.completed_at = utc_now()
        await self._save(state)
        job.result = {"graph_generation": state.graph_generation, "published": publishable}
        # 先在同一事务内将旧任务标为终态，释放唯一在途约束，再新增后继任务。
        # 中间不能提交，否则旧租约 guard 会拒绝新任务的检查点，造成永久 STALE。
        await self._finish(job, JobStatus.SUCCEEDED, commit=False)
        if not publishable and state.configuration.enabled and state.configuration.auto_build:
            await self._service.enqueue(job.security_scope_id, state.knowledge_base_id)
        await self._checkpoint()

    async def _cancel(self, job: IngestionJob, state: GraphState) -> None:
        """请求停止；只有回读明确终态才完成本地取消，否则保留待确认状态。"""
        if not job.payload.get("submission_started"):
            state.status = (
                GraphStatus.STALE if state.configuration.enabled else GraphStatus.DISABLED
            )
            await self._save(state)
            await self._finish(job, JobStatus.CANCELLED)
            return
        state.status = GraphStatus.CANCELLING
        await self._save(state)
        await self._checkpoint()
        generation = state.graph_generation + 1
        task = await self._engine.task(job.security_scope_id, state.knowledge_base_id, generation)
        if (
            task is not None
            and state.engine_task_id is None
            and task.engine_task_id == job.payload.get("previous_engine_task_id")
        ):
            raise RagError(
                "RAG-GRAPH-006", "无法确认原生构图提交结果，请核查后手动重试", 409, False
            )
        if (
            task is not None
            and state.engine_task_id is not None
            and task.engine_task_id != state.engine_task_id
        ):
            # Dataset 只提供最新任务；不能误取消后继任务或借其终态确认旧任务停止。
            raise RagError("RAG-GRAPH-007", "原生任务已被其他构建替换", 409, False)
        if task is None or task.status not in {GraphStatus.READY, GraphStatus.FAILED}:
            # 已结束的任务可直接确认停止，不能先DELETE掉唯一可观察的终态证据。
            await self._engine.cancel(job.security_scope_id, state.knowledge_base_id, generation)
        deadline = self._monotonic() + self._timeout_seconds
        while self._monotonic() < deadline:
            if task is not None and task.status in {GraphStatus.READY, GraphStatus.FAILED}:
                # 已完成也属于确定停止；取消期间的结果绝不发布成 READY。
                state.status = (
                    GraphStatus.STALE if state.configuration.enabled else GraphStatus.DISABLED
                )
                state.completed_at = utc_now()
                await self._save(state)
                await self._finish(job, JobStatus.CANCELLED)
                return
            await self._sleep(self._poll_seconds)
            task = await self._engine.task(
                job.security_scope_id, state.knowledge_base_id, generation
            )
            if (
                task is not None
                and state.engine_task_id is None
                and task.engine_task_id == job.payload.get("previous_engine_task_id")
            ):
                raise RagError(
                    "RAG-GRAPH-006", "无法确认原生构图提交结果，请核查后手动重试", 409, False
                )
            if (
                task is not None
                and state.engine_task_id is not None
                and task.engine_task_id != state.engine_task_id
            ):
                raise RagError("RAG-GRAPH-007", "原生任务已被其他构建替换", 409, False)
        raise RagError("RAG-GRAPH-011", "取消已请求，原生任务终止尚未确认", 504, True)

    async def _state(self, job: IngestionJob) -> GraphState | None:
        """每个外部动作前重新读取任务归属，阻止旧 Worker 改写新任务。"""
        if job.knowledge_base_id is None:
            return None
        state = await self._graphs.get(job.security_scope_id, job.knowledge_base_id)
        return state if state is not None and state.job_id == job.id else None

    async def _save(self, state: GraphState) -> None:
        """仅保存读取时的行版本，数据库冲突交由有限重试处理。"""
        if not await self._graphs.update(
            state.security_scope_id, state, expected_row_version=state.row_version
        ):
            raise RagError("RAG-GRAPH-005", "图谱状态并发更新", 409, True)

    async def _finish(self, job: IngestionJob, status: JobStatus, *, commit: bool = True) -> None:
        """通过同一租约保护的检查点提交任务终态。"""
        job.status = status
        job.finished_at = utc_now()
        job.progress = 1.0 if status == JobStatus.SUCCEEDED else job.progress
        job.locked_by = None
        job.locked_until = None
        job.error_code = None
        job.error_message = None
        await self._jobs.update(job.security_scope_id, job)
        if commit:
            await self._checkpoint()

    async def _fail(self, job: IngestionJob, error: RagError) -> None:
        """把可恢复轮询留给现有 Job 重试；未知提交不会盲目再建图。"""
        state = await self._state(job)
        current_job = await self._jobs.get(job.security_scope_id, job.id)
        cancel_requested = (
            state is not None
            and (state.status == GraphStatus.CANCELLING or not state.configuration.enabled)
        ) or (current_job is not None and current_job.status == JobStatus.CANCEL_REQUESTED)
        retry = error.retryable and job.attempt < job.max_attempts
        job.status = JobStatus.RETRY_WAIT if retry else JobStatus.FAILED
        job.error_code = error.code
        job.error_message = error.message
        job.locked_by = None
        job.locked_until = None
        job.next_retry_at = utc_now() + timedelta(seconds=30) if retry else None
        job.finished_at = None if retry else utc_now()
        if state is not None:
            # 取消未确认和轮询可重试时保留在途状态，阻止另一个新任务重复构图。
            if cancel_requested:
                state.status = GraphStatus.CANCELLING
            elif not retry:
                state.status = GraphStatus.FAILED
            state.last_error_code = error.code
            state.last_error_message = error.message
            await self._save(state)
        await self._jobs.update(job.security_scope_id, job)
        await self._checkpoint()
