"""构造统一 Redis Stack 场景下的 Celery 队列、重投和过期策略。

本模块不创建 Celery 进程，也不访问 Redis；它只把强类型 ``ServiceSettings`` 转换为
Celery 配置字典。将策略与进程入口分开后，单元测试可以直接验证队列和超时契约，
而不需要启动 Worker 或读取本机密钥。PostgreSQL Job 始终是持久任务事实来源，Redis
消息只是唤醒信号，因此这里明确关闭无用 Result Backend，并让周期信号自动过期。
"""

from __future__ import annotations

from typing import Any

from kombu import Queue

from rag_service.settings import ServiceSettings


def build_celery_configuration(settings: ServiceSettings) -> dict[str, Any]:
    """返回 Celery Worker 与 Beat 共用的完整运行策略。

    参数 ``settings`` 提供 DB 2 连接超时、Key 前缀、两条队列名和 Job 扫描周期。
    返回值只包含可安全序列化或由 Celery 直接消费的配置对象，不包含 Redis 密码的
    展示形式。所有任务都声明 ``ignore_result``，所以关闭 Result Backend 不会改变
    API 行为，反而避免 DB 2 写入永远无人读取的任务结果 Key。
    """
    ingestion_queue = settings.redis.ingestion_queue_name
    maintenance_queue = settings.redis.maintenance_queue_name
    return {
        "task_serializer": "json",
        "accept_content": ("json",),
        # 显式覆盖任何进程级 Celery 环境变量，确保任务结果只落 PostgreSQL，DB 2
        # 不会因为部署环境残留 CELERY_RESULT_BACKEND 而重新产生结果 Key。
        "result_backend": None,
        "task_ignore_result": True,
        "task_acks_late": True,
        "worker_prefetch_multiplier": 1,
        "task_reject_on_worker_lost": True,
        # Broker 连接丢失时终止尚未确认的长任务。任务对应的 PostgreSQL 租约到期后，
        # 后续扫描会重新认领它，不会依赖不确定的 Python 协程继续写外部系统。
        "worker_cancel_long_running_tasks_on_connection_loss": True,
        "broker_connection_retry": True,
        "broker_connection_retry_on_startup": True,
        "task_default_queue": ingestion_queue,
        "task_queues": (
            Queue(ingestion_queue),
            Queue(maintenance_queue),
        ),
        "task_routes": {
            "rag.process_pending_job": {"queue": ingestion_queue},
            "rag.cleanup_upload_staging": {"queue": maintenance_queue},
        },
        # Kombu 的 global_keyprefix 会覆盖队列、未确认集合和互斥锁，方便诊断与定向
        # 清理。visibility_timeout 只控制 Broker 消息重投，业务 Job 恢复仍看数据库租约。
        "broker_transport_options": {
            "global_keyprefix": settings.redis.celery_key_prefix,
            "visibility_timeout": settings.redis.broker_visibility_timeout_seconds,
            "socket_connect_timeout": settings.redis.socket_connect_timeout_seconds,
            "socket_timeout": settings.redis.socket_timeout_seconds,
            "health_check_interval": (
                settings.redis.broker_health_check_interval_seconds
            ),
            "retry_on_timeout": True,
            "queue_order_strategy": "round_robin",
        },
        "beat_schedule": {
            "claim-pending-ingestion-job": {
                "task": "rag.process_pending_job",
                "schedule": float(settings.jobs.recovery_scan_interval_seconds),
                "options": {
                    "queue": ingestion_queue,
                    "expires": settings.jobs.dispatch_message_expires_seconds,
                },
            },
            "cleanup-upload-staging": {
                "task": "rag.cleanup_upload_staging",
                "schedule": 900.0,
                "options": {
                    "queue": maintenance_queue,
                    "expires": settings.jobs.cleanup_message_expires_seconds,
                },
            },
        },
    }
