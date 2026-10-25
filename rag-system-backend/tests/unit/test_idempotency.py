"""幂等服务测试：响应重放、同键异请求冲突、租户独立和原异常保留。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from rag_service.application.idempotency import IdempotencyService
from rag_service.domain.errors import RagError
from rag_service.domain.models import (
    IdempotencyRecord,
    IdempotencyReservation,
    IdempotencyStatus,
)
from rag_service.security.context import SecurityContext
from rag_service.settings import IdempotencySection


class MemoryIdempotencyRepository:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str, str, str], IdempotencyRecord] = {}
        self.fail_on_mark_failed = False

    @staticmethod
    def _key(value: IdempotencyRecord) -> tuple[str, str, str, str]:
        return (
            value.security_scope_id,
            value.idempotency_key,
            value.http_method,
            value.request_path,
        )

    async def reserve(self, value: IdempotencyRecord) -> IdempotencyReservation:
        key = self._key(value)
        existing = self.items.get(key)
        if existing is not None:
            return IdempotencyReservation(record=existing, acquired=False)
        self.items[key] = value
        return IdempotencyReservation(record=value, acquired=True)

    async def complete(
        self,
        value: IdempotencyRecord,
        *,
        response_status: int,
        response_json: dict[str, Any],
        resource_id: Any,
    ) -> None:
        stored = self.items[self._key(value)]
        stored.processing_status = IdempotencyStatus.COMPLETED
        stored.response_status = response_status
        stored.response_json = response_json
        stored.resource_id = resource_id

    async def fail(self, value: IdempotencyRecord) -> None:
        if self.fail_on_mark_failed:
            raise RuntimeError("transaction is already closed")
        self.items[self._key(value)].processing_status = IdempotencyStatus.FAILED


def _security(tenant: str) -> SecurityContext:
    return SecurityContext.create(
        subject_id="subject",
        tenant_id=tenant,
        rag_scope_id="scope",
        token_id=f"token-{tenant}",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )


@pytest.mark.asyncio
async def test_replay_returns_original_response_without_reexecuting() -> None:
    repository = MemoryIdempotencyRepository()
    service = IdempotencyService(repository, IdempotencySection())
    calls = 0

    async def operation() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"data": {"id": "25894004-5f28-471f-8d38-262c61d162ff"}}

    arguments = {
        "security": _security("a"),
        "key": "same-key",
        "method": "POST",
        "path": "/v1/knowledge-bases",
        "payload": {"name": "fixture"},
        "response_status": 201,
        "operation": operation,
    }
    first = await service.execute(**arguments)
    second = await service.execute(**arguments)

    assert first == second
    assert calls == 1


@pytest.mark.asyncio
async def test_same_key_different_body_conflicts_but_scope_is_independent() -> None:
    repository = MemoryIdempotencyRepository()
    service = IdempotencyService(repository, IdempotencySection())

    async def operation() -> dict[str, Any]:
        return {"data": {"status": "ok"}}

    await service.execute(
        security=_security("a"),
        key="shared-key",
        method="POST",
        path="/v1/knowledge-bases",
        payload={"name": "first"},
        response_status=201,
        operation=operation,
    )
    with pytest.raises(RagError, match="幂等键") as error:
        await service.execute(
            security=_security("a"),
            key="shared-key",
            method="POST",
            path="/v1/knowledge-bases",
            payload={"name": "different"},
            response_status=201,
            operation=operation,
        )
    assert error.value.code == "RAG-COMMON-009"

    await service.execute(
        security=_security("b"),
        key="shared-key",
        method="POST",
        path="/v1/knowledge-bases",
        payload={"name": "different"},
        response_status=201,
        operation=operation,
    )
    assert len(repository.items) == 2


@pytest.mark.asyncio
async def test_operation_error_is_not_hidden_when_failure_marker_cannot_be_saved() -> None:
    repository = MemoryIdempotencyRepository()
    repository.fail_on_mark_failed = True
    service = IdempotencyService(repository, IdempotencySection())

    async def operation() -> dict[str, Any]:
        raise ValueError("original operation failure")

    with pytest.raises(ValueError, match="original operation failure"):
        await service.execute(
            security=_security("a"),
            key="failing-key",
            method="POST",
            path="/v1/knowledge-bases/fixture/documents",
            payload={"external_id": "fixture"},
            response_status=202,
            operation=operation,
        )
