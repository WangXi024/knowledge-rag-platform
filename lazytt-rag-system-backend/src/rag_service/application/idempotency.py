"""写操作的幂等执行编排。

客户端可在网络超时后使用同一幂等键重试。服务先在数据库抢占该键：首个请求执行
真实操作，已完成请求复用旧响应，仍在处理或“同键不同请求”则返回明确冲突。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import timedelta
from typing import Any
from uuid import UUID

from rag_service.domain.errors import RagError
from rag_service.domain.models import IdempotencyRecord, IdempotencyStatus, utc_now
from rag_service.domain.ports import IdempotencyRepository
from rag_service.security.context import SecurityContext
from rag_service.settings import IdempotencySection


def canonical_request_hash(
    *,
    method: str,
    path: str,
    payload: dict[str, Any],
) -> str:
    """把请求方法、路径和 JSON 规范化后计算稳定指纹。

    JSON 键排序和紧凑分隔符确保语义相同但键顺序不同的对象得到相同哈希；方法统一
    转大写，避免 ``post`` 与 ``POST`` 被误认为两个请求。
    """
    encoded = json.dumps(
        {"method": method.upper(), "path": path, "payload": payload},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class IdempotencyService:
    """围绕任意异步写操作执行“抢占—执行—保存响应”协议。"""
    def __init__(
        self,
        repository: IdempotencyRepository,
        settings: IdempotencySection,
    ) -> None:
        """注入持久化仓库和记录有效期配置。"""
        self._repository = repository
        self._settings = settings

    async def execute(
        self,
        *,
        security: SecurityContext,
        key: str,
        method: str,
        path: str,
        payload: dict[str, Any],
        response_status: int,
        operation: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        """在指定安全范围内幂等执行 ``operation`` 并返回其 JSON 结果。"""
        now = utc_now()
        record = IdempotencyRecord(
            security_scope_id=security.security_scope_id,
            idempotency_key=key,
            http_method=method.upper(),
            request_path=path,
            request_sha256=canonical_request_hash(
                method=method,
                path=path,
                payload=payload,
            ),
            created_at=now,
            expires_at=now + timedelta(seconds=self._settings.ttl_seconds),
        )
        # 数据库唯一约束决定谁获得执行权；不能先查后写，否则并发请求会同时通过。
        reservation = await self._repository.reserve(record)
        stored = reservation.record
        if stored.request_sha256 != record.request_sha256:
            raise RagError(
                "RAG-COMMON-009",
                "幂等键与不同请求冲突",
                http_status=409,
                retryable=False,
            )
        if not reservation.acquired:
            if (
                stored.processing_status == IdempotencyStatus.COMPLETED
                and stored.response_json is not None
            ):
                return stored.response_json
            raise RagError(
                "RAG-COMMON-008",
                "相同请求仍在处理中",
                http_status=409,
                retryable=True,
            )
        try:
            response = await operation()
        except BaseException:
            # 数据库错误可能已使当前事务失效。这里尽力标记失败，但绝不能让二次更新
            # 的异常遮蔽真正的业务异常；外层事务最终还会回滚本次占位。
            with suppress(Exception):
                await self._repository.fail(record)
            raise
        await self._repository.complete(
            record,
            response_status=response_status,
            response_json=response,
            resource_id=_resource_id(response),
        )
        return response


def _resource_id(response: dict[str, Any]) -> UUID | None:
    """从常见响应字段中尽力提取新资源 UUID，供幂等审计关联资源。"""
    data = response.get("data")
    if not isinstance(data, dict):
        return None
    for key in ("id", "document_id", "job_id"):
        raw = data.get(key)
        if raw is None:
            continue
        try:
            return UUID(str(raw))
        except ValueError:
            continue
    return None
