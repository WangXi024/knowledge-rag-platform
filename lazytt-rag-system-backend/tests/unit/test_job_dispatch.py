"""即时唤醒和连续领取的边界测试：不连接公网或真实 Broker。"""

from unittest.mock import AsyncMock, Mock

import pytest

from rag_service.settings import ServiceSettings
from rag_service.workers import dispatch, runner


@pytest.mark.asyncio
async def test_committed_notifications_consumed_once(
    service_settings: ServiceSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """已提交的新增任务每条通知一次；再次调用不会重复发送。"""
    session = Mock(info={dispatch.PENDING_WAKEUPS: 3})
    session.in_transaction.return_value = False
    notify = Mock()
    monkeypatch.setattr(dispatch, "notify_workers", notify)
    await dispatch.notify_committed(session, service_settings)
    await dispatch.notify_committed(session, service_settings)
    notify.assert_called_once_with(service_settings, 3)


def test_broker_failure_does_not_fail_committed_upload(
    service_settings: ServiceSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """通知发送故障不传播到已提交的 API 请求，等待 Beat 扫描恢复。"""
    monkeypatch.setattr(dispatch, "Celery", Mock(side_effect=OSError("test-only")))
    dispatch.notify_workers(service_settings, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 4, 10])
async def test_worker_drains_bounded_batch(
    service_settings: ServiceSettings, monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    """空队列退出，积压连续领取；十个任务后发送续接通知并交还执行名额。"""
    run = AsyncMock(side_effect=[True] * count + [False])
    notify = Mock()
    monkeypatch.setattr(runner, "run_one_job", run)
    monkeypatch.setattr(runner, "notify_workers", notify)
    await runner.drain_pending_jobs(service_settings)
    assert run.await_count == min(count + 1, 10)
    assert notify.call_count == (1 if count == 10 else 0)
    identities = [call.args[1] for call in run.await_args_list]
    assert len(set(identities)) == len(identities)


@pytest.mark.asyncio
async def test_uncommitted_transaction_cannot_notify(
    service_settings: ServiceSettings, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """事务未结束时禁止发送，避免 Worker 在提交之前领取不到真实任务。"""
    session = Mock(info={dispatch.PENDING_WAKEUPS: 1})
    session.in_transaction.return_value = True
    notify = Mock()
    monkeypatch.setattr(dispatch, "notify_workers", notify)
    with pytest.raises(RuntimeError, match="committed"):
        await dispatch.notify_committed(session, service_settings)
    notify.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["lost", "database_error", "finished"])
async def test_heartbeat_failure_stops_old_execution(
    service_settings: ServiceSettings, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    """租约丢失或续租异常取消旧协程；任务已正常完成时不误取消。"""
    import asyncio
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    from uuid import uuid4

    from rag_service.domain.models import JobStatus

    session = Mock()

    @asynccontextmanager
    async def context():
        """模拟独立心跳事务，完全不连接数据库。"""
        yield session

    session.begin = context
    engine = Mock(dispose=AsyncMock())
    repo = Mock(
        renew_lease=AsyncMock(return_value=False),
        get=AsyncMock(return_value=SimpleNamespace(
            status=JobStatus.SUCCEEDED if mode == "finished" else JobStatus.RUNNING,
        )),
    )
    if mode == "database_error":
        repo.renew_lease.side_effect = OSError("test-only")
    monkeypatch.setattr(runner, "create_engine", Mock(return_value=engine))
    monkeypatch.setattr(runner, "create_session_factory", Mock(return_value=context))
    monkeypatch.setattr(runner, "SqlAlchemyJobRepository", Mock(return_value=repo))
    settings = service_settings.model_copy(update={
        "jobs": service_settings.jobs.model_copy(update={"lease_seconds": 1}),
    })
    owner = Mock()
    await runner._renew_lease_until_stopped(
        settings, security_scope_id="a" * 64, job_id=uuid4(), worker_id="test-owner",
        stop=asyncio.Event(), owner_task=owner,
    )
    assert owner.cancel.call_count == (0 if mode == "finished" else 1)
    engine.dispose.assert_awaited_once()
