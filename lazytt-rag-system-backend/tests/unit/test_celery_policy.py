"""统一 Redis Stack 的 Celery 策略测试。

这些测试不连接 Redis，也不启动 Worker。它们直接检查生成的配置，防止未来重新启用
无人读取的 Result Backend、把维护任务路由到摄取队列，或去掉用于限制轮询积压的
消息过期时间。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from rag_service.settings import JobsSection, RedisSection, ServiceSettings
from rag_service.workers.celery_policy import build_celery_configuration


def test_celery_policy_separates_queues_and_expires_poll_messages(
    service_settings: ServiceSettings,
) -> None:
    """摄取和维护必须分队列，周期唤醒消息必须短于长文档执行时限。"""
    configuration = build_celery_configuration(service_settings)
    beat = configuration["beat_schedule"]
    routes = configuration["task_routes"]

    assert configuration["result_backend"] is None
    assert configuration["task_ignore_result"] is True
    assert routes["rag.process_pending_job"]["queue"] == "rag-ingestion"
    assert routes["rag.cleanup_upload_staging"]["queue"] == "rag-maintenance"
    assert beat["claim-pending-ingestion-job"]["schedule"] == 5.0
    assert beat["claim-pending-ingestion-job"]["options"] == {
        "queue": "rag-ingestion",
        "expires": 15,
    }
    assert beat["cleanup-upload-staging"]["options"] == {
        "queue": "rag-maintenance",
        "expires": 840,
    }


def test_celery_policy_namespaces_broker_keys_and_bounds_redelivery(
    service_settings: ServiceSettings,
) -> None:
    """Kombu 内部 Key 必须有独立前缀，长任务重投时间不能落回默认值。"""
    transport = build_celery_configuration(service_settings)[
        "broker_transport_options"
    ]

    assert transport["global_keyprefix"] == "rag:celery:"
    assert transport["visibility_timeout"] == 14_400
    assert transport["retry_on_timeout"] is True
    assert transport["queue_order_strategy"] == "round_robin"


def test_redis_and_job_settings_reject_unsafe_queue_configuration() -> None:
    """重名队列、无分隔符前缀和过短消息寿命应在启动阶段直接失败。"""
    with pytest.raises(ValidationError, match="queues must differ"):
        RedisSection(
            url="redis://127.0.0.1:6379/2",
            ingestion_queue_name="same",
            maintenance_queue_name="same",
        )
    with pytest.raises(ValidationError, match="must end with"):
        RedisSection(
            url="redis://127.0.0.1:6379/2",
            celery_key_prefix="rag-celery",
        )
    with pytest.raises(ValidationError, match="must exceed"):
        JobsSection(
            recovery_scan_interval_seconds=5,
            dispatch_message_expires_seconds=5,
        )
