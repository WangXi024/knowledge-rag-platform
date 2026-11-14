"""原生构图持久化任务的可恢复执行器。

外部提交前先写检查点；提交响应丢失后仅回读原生任务，不自动再次收费构图。
轮询失败可有限重试并沿用原生任务 ID。完成后再次核对活动来源、内容和配置版本。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import timedelta

from rag_service.application.graph import GraphService, noop_graph_checkpoint
from rag_service.domain.aggregate import AggregateEvidence
from rag_service.domain.errors import JobCancellationRequested, JobLeaseLostError, RagError
from rag_service.domain.graph import (
    GraphPort,
    GraphProfile,
    GraphRepository,
    GraphState,
    GraphStatus,
    GraphTask,
)
from rag_service.domain.graph_read import GraphReportBuilder
from rag_service.domain.models import (
    IngestionJob,
    JobStatus,
    KnowledgeBase,
    KnowledgeBaseStatus,
    utc_now,
)
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
        report_builder: GraphReportBuilder | None = None,
        checkpoint: Callable[[], Awaitable[None]] = noop_graph_checkpoint,
        rollback: Callable[[], Awaitable[None]] = noop_graph_checkpoint,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        poll_seconds: float = 3.0,
        timeout_seconds: float = 1800.0,
    ) -> None:
        """注入租约事务、有界轮询及可选报告阶段；旧 light 策略不调用报告模型。"""
        self._knowledge_bases = knowledge_bases
        self._graphs = graphs
        self._jobs = jobs
        self._engine = engine
        self._service = service
        self._report_builder = report_builder
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
            if not job.payload.get("preparation_started"):
                previous = await self._engine.task(
                    job.security_scope_id, state.knowledge_base_id, generation
                )
                if previous is not None and (
                    previous.status not in {GraphStatus.READY, GraphStatus.FAILED}
                    or not isinstance(previous.engine_task_id, str)
                    or not previous.engine_task_id.strip()
                    or previous.security_scope_id != job.security_scope_id
                    or previous.knowledge_base_id != state.knowledge_base_id
                    or previous.graph_generation != generation
                ):
                    raise RagError(
                        "RAG-GRAPH-006", "旧原生任务未明确结束，不能清理并开始新的构图", 409, False
                    )
                previous_task_id = previous.engine_task_id if previous is not None else None
                # 清理可能删除原生任务记录。必须先独立提交所观察到的终态身份，崩溃
                # 后才能区分“已获准清理后任务为空”和“任意缺失任务被误认为已停止”。
                job.payload["preparation_started"] = True
                job.payload["preparation_task_id"] = previous_task_id
                job.payload["previous_engine_task_id"] = previous_task_id
                await self._jobs.update(job.security_scope_id, job)
                await self._checkpoint()
            if "preparation_task_id" not in job.payload:
                raise RagError("RAG-GRAPH-006", "构图准备缺少已持久化的旧任务身份", 409, False)
            marker = job.payload["preparation_task_id"]
            if marker is not None and (not isinstance(marker, str) or not marker.strip()):
                raise RagError("RAG-GRAPH-006", "构图准备中的旧任务身份无效", 409, False)
            previous_task_id = marker if isinstance(marker, str) else None
            state = await self._state(job)
            if state is None:
                await self._finish(job, JobStatus.CANCELLED)
                return
            current_job = await self._jobs.get(job.security_scope_id, job.id)
            if (
                state.status == GraphStatus.CANCELLING or not state.configuration.enabled
                or (current_job is not None and current_job.status == JobStatus.CANCEL_REQUESTED)
            ):
                await self._cancel(job, state)
                return
            kb = await self._knowledge_bases.get(job.security_scope_id, state.knowledge_base_id)
            if kb is None or kb.status != KnowledgeBaseStatus.READY:
                await self._finish(job, JobStatus.CANCELLED)
                return
            # 排队期间可合并多份解析结果；和提交标记一起持久化后才开始收费构图。
            # 一旦 submission_started 已提交，恢复分支必须保留原内容快照。
            state.target_content_revision = kb.revision
            job.payload["content_revision"] = kb.revision
            # 清理与参数更新可沿准备标记恢复；实际收费构图之前还需另存提交标记。
            await self._engine.configure(
                job.security_scope_id, state.knowledge_base_id, generation, state.configuration,
                previous_task_id=previous_task_id,
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
        """当前快照严格核验后 CAS 发布；已过时的完成任务只安排后继，不再读取旧图。

        读取内容号之后仍可能有摄取提交，content_changed 必须同步推进图谱行版本。
        此类冲突在当前阶段最多回滚、复核三次，不额外消耗 Job attempt。少量并发
        即使发生在最后一次 attempt 也能收敛；持续竞争则交回既有有限任务重试，
        耗尽后明确 FAILED、允许人工重试，不能无限占用 Worker。
        """
        for publication_attempt in range(3):
            context = await self._publication_context(job)
            if context is None:
                return
            kb, state = context
            publishable = not self._obsolete(job, kb, state)
            reports: tuple[AggregateEvidence, ...] = ()
            if publishable:
                observed_row_version = state.row_version
                try:
                    await self._engine.snapshot(
                        job.security_scope_id, state.knowledge_base_id, generation
                    )
                    # 原生验收期间也可能完成内容提交；在报告模型收费之前复读一次。
                    # 行版本变化须沿原有有限循环重验，已落后的图则直接进入过时收敛。
                    before_reports = await self._publication_context(job)
                    if before_reports is None:
                        return
                    candidate_kb, candidate_state = before_reports
                    if candidate_state.row_version != observed_row_version:
                        await self._retry_publication(publication_attempt)
                        continue
                    if (
                        not self._obsolete(job, candidate_kb, state)
                        and state.configuration.profile_ref == GraphProfile.NATIVE_WITH_SUMMARIES_V1
                    ):
                        if self._report_builder is None:
                            raise RagError(
                                "RAG-GRAPH-013", "当前策略要求 Gateway 社区报告构建器", 503, False
                            )
                        built = await self._report_builder.build(job, replace(state), generation)
                        if not isinstance(built, tuple) or any(
                            not isinstance(report, AggregateEvidence)
                            or report.resource_scope_id != job.security_scope_id
                            or report.knowledge_base_id != state.knowledge_base_id
                            or report.graph_generation != generation
                            or report.content_revision != job.payload.get("content_revision")
                            or report.graph_profile_ref != state.configuration.profile_ref
                            or report.origin != "gateway_generated"
                            or any(
                                source.resource_scope_id != job.security_scope_id
                                or source.knowledge_base_id != state.knowledge_base_id
                                for source in report.sources
                            )
                            for report in built
                        ):
                            raise RagError("RAG-GRAPH-013", "社区报告范围或版本不一致", 409, False)
                        # 配置中的检索/自动调度开关变化不重收模型费用；相同图、内容和
                        # 构图策略的恢复结果绑定本次发布配置版本，模型输入复用由 builder 核验。
                        reports = tuple(
                            replace(report, configuration_version=state.configuration_version)
                            for report in built
                        )
                        after_reports = await self._publication_context(job)
                        if after_reports is None:
                            return
                        report_kb, report_state = after_reports
                        if report_state.row_version != observed_row_version:
                            await self._retry_publication(publication_attempt)
                            continue
                        # 报告阶段含模型 I/O 和多个检查点；发布前再核完整物理/图来源。
                        # 最终 CAS 仍使用进入第一次 snapshot 之前的原始行版本。
                        if not self._obsolete(job, report_kb, state):
                            await self._engine.snapshot(
                                job.security_scope_id, state.knowledge_base_id, generation
                            )
                except RagError as error:
                    if error.code != "RAG-GRAPH-004":
                        raise
                    latest = await self._publication_context(job)
                    if latest is None:
                        return
                    latest_kb, latest_state = latest
                    if not self._obsolete(job, latest_kb, latest_state):
                        # 内容快照没有确实落后，来源异常仍是验收失败，不能借机放行。
                        raise
                else:
                    latest = await self._publication_context(job)
                    if latest is None:
                        return
                    latest_kb, latest_state = latest
                if latest_state.row_version != observed_row_version:
                    # 不能用验收之后读到的新版本直接 CAS。摄取可能只登记了 receipt，
                    # 内容号尚未递增，仍必须回滚并重新执行来源核验或确认已过时。
                    await self._retry_publication(publication_attempt)
                    continue
                # 保留 snapshot 之前的 state/row_version，KB 复读还能识别仅内容号
                # 改变的交错；过时结果从不发布 READY，因此无需验收将被丢弃的旧图。
                kb = latest_kb
                publishable = not self._obsolete(job, kb, state)
            state.status = GraphStatus.READY if publishable else GraphStatus.STALE
            state.community_reports = reports if publishable else ()
            if publishable:
                state.graph_generation = generation
                state.built_for_revision = kb.revision
            state.completed_at = utc_now()
            try:
                await self._save(state)
                break
            except RagError as error:
                if error.code != "RAG-GRAPH-005":
                    raise
                await self._retry_publication(publication_attempt)
        job.result = {"graph_generation": state.graph_generation, "published": publishable}
        # 先在同一事务内将旧任务标为终态，释放唯一在途约束，再新增后继任务。
        # 中间不能提交，否则旧租约 guard 会拒绝新任务的检查点，造成永久 STALE。
        await self._finish(job, JobStatus.SUCCEEDED, commit=False)
        if not publishable and state.configuration.enabled and state.configuration.auto_build:
            await self._service.enqueue(job.security_scope_id, state.knowledge_base_id)
        await self._checkpoint()

    async def _publication_context(
        self, job: IngestionJob,
    ) -> tuple[KnowledgeBase, GraphState] | None:
        """复读发布所需内容和图谱状态；删除或取消优先收尾，返回 None 表示已处理。"""
        if job.knowledge_base_id is None:
            await self._finish(job, JobStatus.CANCELLED)
            return None
        kb = await self._knowledge_bases.get(job.security_scope_id, job.knowledge_base_id)
        state = await self._state(job)
        if kb is None or kb.status != KnowledgeBaseStatus.READY or state is None:
            await self._finish(job, JobStatus.CANCELLED)
            return None
        current_job = await self._jobs.get(job.security_scope_id, job.id)
        if (
            state.status == GraphStatus.CANCELLING or not state.configuration.enabled
            or (current_job is not None and current_job.status == JobStatus.CANCEL_REQUESTED)
        ):
            await self._cancel(job, state)
            return None
        return kb, state

    @staticmethod
    def _obsolete(job: IngestionJob, kb: KnowledgeBase, state: GraphState) -> bool:
        """按内容/目标修订及不可变构图策略判断旧结果；纯检索开关不废弃已付费构建。"""
        return (
            kb.revision != job.payload.get("content_revision")
            or state.target_content_revision != job.payload.get("content_revision")
            or state.configuration.profile_ref != job.payload.get("profile_ref")
        )

    async def _retry_publication(self, publication_attempt: int) -> None:
        """回滚失效验收并复核租约，最多三次局部尝试；不清图或再次提交原生任务。"""
        await self._rollback()
        if publication_attempt == 2:
            raise RagError("RAG-GRAPH-005", "图谱状态并发更新", 409, True)
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
        """保存读取时的行版本及实际变更时间，数据库冲突交由有限重试处理。"""
        # 管理端轮询需要区分真实状态变更时间和最初排队时间，发布/失败也必须刷新。
        state.updated_at = utc_now()
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
