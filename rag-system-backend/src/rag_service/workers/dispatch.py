"""事务提交后的尽力唤醒：数据库保存任务，Redis 通知失败由 Beat 补偿。

通知不携带文档或凭据；通过线程发送，避免阻塞异步 API。每次发送关闭连接，
不启用发布重试，避免 Redis 故障把已成功提交的上传变成失败响应。
"""

import asyncio
import logging

from celery import Celery
from sqlalchemy.ext.asyncio import AsyncSession

from rag_service.settings import ServiceSettings
from rag_service.workers.celery_policy import build_celery_configuration

logger = logging.getLogger(__name__)
PENDING_WAKEUPS = "pending_job_wakeups"


def notify_workers(settings: ServiceSettings, count: int = 1) -> None:
    """发送有限数量的空载唤醒消息；失败只记固定警告，持久任务不会被删除。"""
    if count <= 0:
        return
    try:
        with Celery("rag-dispatch", broker=settings.redis.url) as app:
            app.conf.update(build_celery_configuration(settings))
            for _ in range(count):
                app.send_task(
                    "rag.process_pending_job",
                    queue=settings.redis.ingestion_queue_name,
                    expires=settings.jobs.dispatch_message_expires_seconds,
                    retry=False,
                )
    except Exception:
        logger.warning("job_wakeup_failed_periodic_recovery_pending")


async def notify_committed(session: AsyncSession, settings: ServiceSettings) -> None:
    """只在成功提交后调用；取走本事务新增任务计数，发送失败由周期扫描恢复。"""
    if session.in_transaction():
        raise RuntimeError("job notification requires a committed transaction")
    count = int(session.info.pop(PENDING_WAKEUPS, 0))
    if count:
        await asyncio.to_thread(notify_workers, settings, count)
