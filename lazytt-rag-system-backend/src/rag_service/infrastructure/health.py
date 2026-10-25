"""生产就绪探针：并发检查数据库、Profile、Redis、缓存索引、暂存区、模型和 RAGFlow。"""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx
from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from rag_service.api.schemas import (
    DependencyHealth,
    HealthData,
    HealthStatus,
)
from rag_service.bootstrap.cache_index import RedisSemanticIndexBootstrap
from rag_service.infrastructure.postgres.models import ConfigProfileRow
from rag_service.settings import ServiceSettings


class RuntimeReadinessProbe:
    """复用长连接客户端生成可用于 ``/health/ready`` 的依赖快照。"""
    def __init__(self, settings: ServiceSettings, database_engine: AsyncEngine) -> None:
        """保存配置/引擎，并创建 Redis、RAGFlow、LLM 和两类视觉探测客户端。"""
        self._settings = settings
        self._database_engine = database_engine
        self._app_redis = Redis.from_url(
            settings.redis.url,
            decode_responses=False,
            socket_connect_timeout=settings.redis.socket_connect_timeout_seconds,
            socket_timeout=settings.redis.socket_timeout_seconds,
        )
        self._cache_redis = Redis.from_url(
            settings.cache.url,
            decode_responses=False,
            socket_connect_timeout=settings.redis.socket_connect_timeout_seconds,
            socket_timeout=settings.redis.socket_timeout_seconds,
        )
        timeout = httpx.Timeout(5.0, connect=min(5, settings.ragflow.connect_timeout_seconds))
        self._ragflow = httpx.AsyncClient(
            base_url=settings.ragflow.base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {settings.ragflow.api_key}"},
            timeout=timeout,
            trust_env=False,
        )
        self._models = httpx.AsyncClient(
            base_url=settings.models.llm.base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {settings.models.llm.api_key}"},
            timeout=timeout,
            trust_env=False,
        )
        self._vision_models = httpx.AsyncClient(
            base_url=settings.models.vision.base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {settings.models.vision.api_key}"},
            timeout=timeout,
            trust_env=False,
        )
        media_vision = settings.models.media_vision
        self._media_vision_models = (
            httpx.AsyncClient(
                base_url=media_vision.base_url.rstrip("/") + "/",
                headers={"Authorization": f"Bearer {media_vision.api_key}"},
                timeout=timeout,
                trust_env=False,
            )
            if settings.models.media.enabled and media_vision is not None
            else None
        )

    async def close(self) -> None:
        """关闭探针持有的 Redis 和 HTTP 连接池。"""
        await self._app_redis.aclose()
        await self._cache_redis.aclose()
        await self._ragflow.aclose()
        await self._models.aclose()
        await self._vision_models.aclose()
        if self._media_vision_models is not None:
            await self._media_vision_models.aclose()

    async def check(self) -> HealthData:
        """并发执行所有依赖检查，并按关键性汇总整体状态。

        应用数据库、Profile、Redis、缓存索引、暂存、RAGFlow 和模型任一不可用都会使
        服务 unavailable；可选/非关键依赖可报告 degraded。并发执行避免就绪接口耗时
        等于所有下游超时之和。
        """
        names = (
            "application_database",
            "app_redis",
            "cache_redis",
            "profiles",
            "upload_staging",
            "ragflow",
            "model_api",
        )
        results = await asyncio.gather(
            self._check_database(),
            self._check_redis(self._app_redis),
            self._check_cache(),
            self._check_profiles(),
            self._check_staging(),
            self._check_ragflow(),
            self._check_models(),
        )
        dependencies = dict(zip(names, results, strict=True))
        required = (
            "application_database",
            "app_redis",
            "profiles",
            "upload_staging",
            "ragflow",
            "model_api",
        )
        if any(dependencies[name].status == HealthStatus.UNAVAILABLE for name in required):
            status = HealthStatus.UNAVAILABLE
        elif dependencies["cache_redis"].status == HealthStatus.UNAVAILABLE:
            status = (
                HealthStatus.DEGRADED
                if dependencies["cache_redis"].message == "cache_unavailable"
                else HealthStatus.UNAVAILABLE
            )
        else:
            status = HealthStatus.OK
        return HealthData(status=status, dependencies=dependencies)

    async def _check_database(self) -> DependencyHealth:
        """执行最小 ``SELECT 1`` 验证应用数据库连接和语句执行。"""
        async def operation() -> bool:
            async with self._database_engine.connect() as connection:
                return bool(await connection.scalar(text("SELECT 1")))

        return await _timed_check(operation)

    async def _check_profiles(self) -> DependencyHealth:
        """确认数据库内全部必需 Profile 版本均存在且启用。"""
        async def operation() -> bool:
            required = set(self._settings.profiles.required_refs)
            async with self._database_engine.connect() as connection:
                rows = (
                    await connection.execute(
                        select(ConfigProfileRow.name, ConfigProfileRow.version).where(
                            ConfigProfileRow.enabled.is_(True)
                        )
                    )
                ).all()
            enabled = {f"{name}@{version}" for name, version in rows}
            return required.issubset(enabled)

        return await _timed_check(operation)

    async def _check_cache(self) -> DependencyHealth:
        """检查缓存 Redis 连通性及现有语义索引与当前模型契约一致。"""
        started_at = time.perf_counter()
        try:
            if not await self._cache_redis.ping():
                raise ConnectionError("cache ping failed")
        except Exception:
            return DependencyHealth(
                status=HealthStatus.UNAVAILABLE,
                latency_ms=_elapsed_ms(started_at),
                message="cache_unavailable",
            )
        try:
            await RedisSemanticIndexBootstrap(
                self._cache_redis,
                self._settings.cache.semantic,
            ).validate_existing()
        except Exception:
            return DependencyHealth(
                status=HealthStatus.UNAVAILABLE,
                latency_ms=_elapsed_ms(started_at),
                message="cache_index_mismatch",
            )
        return DependencyHealth(
            status=HealthStatus.OK,
            latency_ms=_elapsed_ms(started_at),
        )

    async def _check_staging(self) -> DependencyHealth:
        """在线程中执行暂存目录创建、写入和删除探针。"""
        async def operation() -> bool:
            return await asyncio.to_thread(
                _staging_write_probe,
                self._settings.uploads.staging_dir,
            )

        return await _timed_check(operation)

    @staticmethod
    async def _check_redis(client: Redis) -> DependencyHealth:
        """通过 PING 检查 Redis 并记录延迟。"""
        async def operation() -> bool:
            return bool(await client.ping())

        return await _timed_check(operation)

    async def _check_ragflow(self) -> DependencyHealth:
        """调用 RAGFlow 健康接口并转换网络、超时和状态码。"""
        async def operation() -> bool:
            health_response, models_response = await asyncio.gather(
                self._ragflow.get("api/v1/system/healthz"),
                self._ragflow.get("api/v1/users/me/models"),
            )
            if not health_response.is_success or not models_response.is_success:
                return False
            health = health_response.json()
            models = models_response.json()
            configured = models.get("data") if isinstance(models, dict) else None
            return (
                isinstance(health, dict)
                and health.get("status") == "ok"
                and isinstance(configured, dict)
                and configured.get("embd_id") == self._settings.ragflow.embedding_model
                and configured.get("img2txt_id") == self._settings.ragflow.vision_model
            )

        return await _timed_check(operation)

    async def _check_models(self) -> DependencyHealth:
        """低成本检查文本与 OCR 兼容端点，不强制供应商实现模型清单。

        就绪探针不会反复发送付费生成或图片请求；真实 Chat、Embedding 和 OCR 调用
        已由启动预检完成。这里优先用低成本 ``GET /models`` 检测网络和鉴权：成功时
        校验配置模型，401/403/5xx 判为不可用；404/405 表示该兼容服务没有实现可选
        清单端点，不能据此否定已经真实验证过的模型能力。
        """
        async def operation() -> bool:
            response, vision_response = await asyncio.gather(
                self._models.get("models"),
                self._vision_models.get("models"),
            )
            healthy = self._models_response_is_healthy(
                response,
                {
                    self._settings.models.llm.model,
                    self._settings.models.llm.rewrite_model,
                },
            ) and self._models_response_is_healthy(
                vision_response,
                {self._settings.models.vision.model},
            )
            if not healthy or self._media_vision_models is None:
                return healthy
            media_vision = self._settings.models.media_vision
            if media_vision is None:
                return False
            # 媒体视觉可以使用另一供应商和 Key，因此必须独立检查它自己的模型清单；
            # 404/405 仍按下方兼容规则接受，因为真实图片调用已在启动预检完成。
            media_response = await self._media_vision_models.get("models")
            return self._models_response_is_healthy(
                media_response,
                {media_vision.model},
            )

        return await _timed_check(operation)

    @staticmethod
    def _models_response_is_healthy(
        response: httpx.Response,
        required_models: set[str],
    ) -> bool:
        """解释兼容端点的模型清单响应，并区分“未实现”和“真实不可用”。

        404/405 只代表供应商没有提供清单接口，启动时已经完成的真实调用仍是能力
        事实来源。成功响应则必须是标准 ``data[].id`` 结构且包含配置模型；认证失败、
        限流和服务端故障均返回 ``False``，由就绪接口阻止继续接收新请求。
        """
        if response.status_code in {404, 405}:
            return True
        if not response.is_success:
            return False
        try:
            value = response.json()
        except ValueError:
            return False
        data = value.get("data") if isinstance(value, dict) else None
        if not isinstance(data, list):
            return False
        available = {
            str(item["id"])
            for item in data
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        return required_models.issubset(available)


async def _timed_check(operation: Callable[[], Awaitable[bool]]) -> DependencyHealth:
    """运行布尔探测、记录延迟，并把任意异常收敛为不可用快照。"""
    started_at = time.perf_counter()
    try:
        healthy = await operation()
    except Exception:
        healthy = False
    return DependencyHealth(
        status=HealthStatus.OK if healthy else HealthStatus.UNAVAILABLE,
        latency_ms=_elapsed_ms(started_at),
        message=None if healthy else "dependency_check_failed",
    )


def _elapsed_ms(started_at: float) -> float:
    """计算三位小数的探测毫秒耗时。"""
    return round((time.perf_counter() - started_at) * 1000, 3)


def _staging_write_probe(path: Path) -> bool:
    """在目标目录写入并删除随机小文件，验证真实可写性而非只看权限位。"""
    path.mkdir(parents=True, exist_ok=True)
    descriptor, probe_path = tempfile.mkstemp(prefix=".health-", dir=path)
    try:
        os.write(descriptor, b"ok")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
        Path(probe_path).unlink(missing_ok=True)
    return True
