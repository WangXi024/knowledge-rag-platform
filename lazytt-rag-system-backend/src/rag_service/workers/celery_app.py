"""Celery 应用、周期调度与同步任务入口。

事务提交后即时唤醒，Beat 周期补漏；多 Worker 通过 PostgreSQL 行锁安全
竞争真实 Job；维护队列每十五分钟接收一次暂存清理任务。所有任务都不返回业务结果，
因此只使用 Redis Broker、不配置 Result Backend，避免统一 Redis DB 2 积累无用数据。
"""

from __future__ import annotations

import asyncio
from typing import Any

from celery import Celery

from rag_service.settings import get_settings
from rag_service.workers.celery_policy import build_celery_configuration
from rag_service.workers.runner import cleanup_staging, drain_pending_jobs

# 模块由 Celery Worker 导入一次，配置和应用对象在进程级复用。
settings = get_settings()
celery_app = Celery(
    "rag-service",
    broker=settings.redis.url,
)
celery_app.conf.update(build_celery_configuration(settings))


@celery_app.task(  # type: ignore[untyped-decorator]
    name="rag.process_pending_job", bind=True, ignore_result=True
)
def process_pending_job(task: Any) -> None:
    """同步入口占用一个并发名额，在有界批次内持续领取已提交任务。"""
    asyncio.run(drain_pending_jobs(settings))


@celery_app.task(  # type: ignore[untyped-decorator]
    name="rag.cleanup_upload_staging", ignore_result=True
)
def cleanup_upload_staging() -> None:
    """同步 Celery 入口：运行一次孤立/过期上传文件清理。"""
    asyncio.run(cleanup_staging(settings))
