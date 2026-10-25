"""Celery 任务与应用层 IngestionWorker 之间的事务、租约和资源生命周期桥梁。"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from uuid import UUID, uuid4

from rag_service.application.ingestion_worker import IngestionWorker
from rag_service.application.profiles import ProfileCatalog
from rag_service.domain.models import utc_now
from rag_service.infrastructure.media.openai_compatible import OpenAICompatibleMediaProcessor
from rag_service.infrastructure.models.openai_compatible import OpenAICompatibleEmbeddingAdapter
from rag_service.infrastructure.postgres.repositories import (
    SqlAlchemyDocumentRepository,
    SqlAlchemyDocumentVersionRepository,
    SqlAlchemyEntityGraphRepository,
    SqlAlchemyIndexNodeRepository,
    SqlAlchemyJobRepository,
    SqlAlchemyKnowledgeBaseRepository,
)
from rag_service.infrastructure.postgres.session import create_engine, create_session_factory
from rag_service.infrastructure.ragflow.adapter import RagFlowAdapter
from rag_service.infrastructure.staging.filesystem import FileSystemUploadStaging
from rag_service.settings import ServiceSettings
from rag_service.workers.dispatch import PENDING_WAKEUPS, notify_committed, notify_workers

logger = logging.getLogger(__name__)


async def _renew_lease_until_stopped(
    settings: ServiceSettings,
    *,
    security_scope_id: str,
    job_id: UUID,
    worker_id: str,
    stop: asyncio.Event,
    owner_task: asyncio.Task[object] | None = None,
) -> None:
    """按租约时长三分之一周期在独立短事务中续租，直到主任务发出停止信号。

    使用独立引擎/Session 是为了主任务进行长时间外部轮询时仍能提交心跳；若续租不再
    匹配当前 worker，或续租发生异常时，取消 owner_task，避免旧执行继续外部调用；
    已提交终态属于正常收尾，不误取消。官网已接受的任务不保证随本机取消而停止。
    """
    interval = max(1.0, settings.jobs.lease_seconds / 3)
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
            return
        except TimeoutError:
            pass
        engine = create_engine(settings.database)
        factory = create_session_factory(engine)
        try:
            async with factory() as session, session.begin():
                renewed = await SqlAlchemyJobRepository(session).renew_lease(
                    security_scope_id,
                    job_id,
                    worker_id=worker_id,
                    locked_until=utc_now()
                    + timedelta(seconds=settings.jobs.lease_seconds),
                )
            if not renewed:
                # 终态可能刚刚提交，这是正常收尾；否则立即取消旧执行协程。
                async with factory() as session:
                    current = await SqlAlchemyJobRepository(session).get(security_scope_id, job_id)
                if current is not None and current.status.value in {
                    "SUCCEEDED", "FAILED", "CANCELLED", "RETRY_WAIT",
                }:
                    return
                logger.warning("ingestion_job_lease_lost job_id=%s", job_id)
                if owner_task is not None:
                    owner_task.cancel()
                return
        except Exception:
            logger.warning("ingestion_job_heartbeat_failed job_id=%s", job_id)
            if owner_task is not None:
                owner_task.cancel()
            return
        finally:
            await engine.dispose()


async def run_one_job(settings: ServiceSettings, worker_id: str) -> bool:
    """原子认领并执行最多一个任务；有任务返回 True，无任务返回 False。

    认领事务先快速提交，随后启动独立续租协程。Worker 内检查点提交阶段状态；异常
    只记录类型且回滚当前未提交事务，因为 IngestionWorker 已负责把可重试/失败状态
    写入检查点。最终无条件停止心跳并释放数据库与 HTTP 连接。
    """
    engine = create_engine(settings.database)
    factory = create_session_factory(engine)
    claimed = None
    try:
        async with factory() as session, session.begin():
            claimed = await SqlAlchemyJobRepository(session).claim_next(
                worker_id=worker_id,
                now=utc_now(),
                locked_until=utc_now() + timedelta(seconds=settings.jobs.lease_seconds),
            )
        if claimed is None:
            return False

        failure: Exception | None = None
        stop_heartbeat = asyncio.Event()
        heartbeat = asyncio.create_task(
            _renew_lease_until_stopped(
                settings,
                security_scope_id=claimed.security_scope_id,
                job_id=claimed.id,
                worker_id=worker_id,
                stop=stop_heartbeat,
                owner_task=asyncio.current_task(),
            )
        )
        try:
            async with factory() as session:
                knowledge_bases = SqlAlchemyKnowledgeBaseRepository(session)
                documents = SqlAlchemyDocumentRepository(session)
                versions = SqlAlchemyDocumentVersionRepository(session)
                index_nodes = SqlAlchemyIndexNodeRepository(session)
                entity_graph = SqlAlchemyEntityGraphRepository(session)
                jobs = SqlAlchemyJobRepository(
                    session, worker_id=worker_id,
                    owned_job=(claimed.security_scope_id, claimed.id),
                )

                async def checkpoint() -> None:
                    """先验证租约再原子提交阶段结果，提交后才通知新任务。"""
                    await jobs.guard_ownership()
                    await session.commit()
                    jobs.reset_ownership_guard()
                    await notify_committed(session, settings)

                async def rollback() -> None:
                    """回滚同时丢弃未提交的通知计数和已释放的行锁标记。"""
                    await session.rollback()
                    jobs.reset_ownership_guard()
                    session.info.pop(PENDING_WAKEUPS, None)

                staging = FileSystemUploadStaging(settings.uploads)
                rag_engine = RagFlowAdapter(
                    settings.ragflow,
                    knowledge_bases,
                    documents,
                )
                embedding = OpenAICompatibleEmbeddingAdapter(settings.models.embedding)
                media_processor = None
                if settings.models.media.enabled:
                    # Settings 已在启动时保证媒体视觉三元组存在；这里仍显式检查，防止
                    # 未来绕过配置加载器构造对象时把视频帧误交给文档 OCR 模型。
                    media_vision = settings.models.media_vision
                    if media_vision is None:
                        raise RuntimeError("media vision configuration is required")
                    media_processor = OpenAICompatibleMediaProcessor(
                        settings.models.media,
                        media_vision,
                    )
                # ProfileCatalog 从同一事务中的已启用配置读取，确保 Worker 应用的
                # 文档级 Parser/Chunk 版本与 API 接受上传时的事实来源一致。目录只含
                # 非敏感策略参数，可安全跨整个单任务生命周期复用。
                profiles = await ProfileCatalog.from_database(session, settings.profiles)
                try:
                    await IngestionWorker(
                        documents,
                        jobs,
                        staging,
                        rag_engine,
                        knowledge_bases,
                        settings.jobs,
                        checkpoint=checkpoint,
                        rollback=rollback,
                        index_nodes=index_nodes,
                        entity_graph=entity_graph,
                        embedding=embedding,
                        media_processor=media_processor,
                        profiles=profiles,
                        versions=versions,
                    ).execute(
                        claimed.security_scope_id,
                        claimed.id,
                        worker_id=worker_id,
                    )
                except Exception as exc:
                    failure = exc
                    await rollback()
                finally:
                    if media_processor is not None:
                        await media_processor.close()
                    await embedding.close()
                    await rag_engine.close()
        finally:
            stop_heartbeat.set()
            await heartbeat
        if failure is not None:
            logger.warning(
                "ingestion_job_failed job_id=%s error_type=%s",
                claimed.id,
                type(failure).__name__,
            )
        return True
    finally:
        await engine.dispose()


async def cleanup_staging(settings: ServiceSettings) -> dict[str, int]:
    """读取活动任务引用集合，清理过期暂存文件并返回数量/字节统计。"""
    engine = create_engine(settings.database)
    factory = create_session_factory(engine)
    try:
        async with factory() as session, session.begin():
            active = await SqlAlchemyJobRepository(session).active_staged_file_ids()
        result = await FileSystemUploadStaging(settings.uploads).cleanup_expired(
            utc_now(),
            settings.jobs.cleanup_batch_size,
            active,
        )
        return {
            "removed_uploading": result.removed_uploading,
            "removed_ready": result.removed_ready,
            "bytes_reclaimed": result.bytes_reclaimed,
        }
    finally:
        await engine.dispose()


def worker_identity(task_id: str | None = None) -> str:
    """用 Celery task ID 生成不超过数据库 128 字符的租约持有者标识。"""
    suffix = task_id or str(uuid4())
    return f"celery:{suffix}"[:128]


async def drain_pending_jobs(settings: ServiceSettings) -> None:
    """单个执行名额连续领取积压任务；最多十个或一轮六十秒后交还 Celery。

    时间预算只在任务边界检查，不中断正在解析的文件。达到预算后发送续接通知，
    通知失败仍由 Beat 恢复；没有可运行任务立即返回，不占进程等待未来重试。
    """
    deadline = asyncio.get_running_loop().time() + 60
    for _ in range(10):
        if not await run_one_job(settings, worker_identity(None)):
            return
        if asyncio.get_running_loop().time() >= deadline:
            break
    await asyncio.to_thread(notify_workers, settings)
