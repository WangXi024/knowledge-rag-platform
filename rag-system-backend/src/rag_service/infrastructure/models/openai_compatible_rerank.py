"""OpenAI 风格 ``/rerank`` Cross-Encoder 服务的异步适配器。

OpenAI 官方没有定义 Rerank API，因此本项目把常见的 ``POST /v1/rerank`` 请求与
``results[index, relevance_score]`` 响应定义为明确的兼容契约。任何供应商只要实现
该契约，就能通过 Base URL、API Key 和模型名接入，不需要新增厂商专属 Adapter。
本模块按照响应中的原始候选下标恢复等长分数序列。它只负责在线候选重排，不负责
召回、RRF、知识库权限或答案生成；这些边界仍分别由应用服务和既有 Adapter 维护。

网络超时、限流、服务过载和无效响应都不会扩大成问答 500：适配器返回明确的降级
Warning，让上层继续使用向量、BM25 与 RRF 排序。启动预检会对同一结果执行严格检查，
因此错误 API Key 或错误协议会阻止新版本启动，而不会被静默标记为已验证。
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Sequence
from typing import Any

import httpx

from rag_service.domain.ports import RerankOutcome
from rag_service.settings import RerankSection

_RETRY_STATUSES = frozenset({429, 502, 503, 504})
_FALLBACK_WARNING = "rerank_service_unavailable_vector_bm25_rrf_fallback"


class OpenAICompatibleRerankAdapter:
    """调用兼容 ``/rerank`` 的文本模型，并在运行故障时安全退回 RRF。

    实例通常随一次 Gateway 请求创建和关闭；并发信号量限制单个实例的请求数量，
    ``httpx.AsyncClient`` 负责连接池和总超时。测试可以注入 Mock Client，此时关闭
    Adapter 不会误关调用方拥有的客户端。
    """

    def __init__(
        self,
        settings: RerankSection,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """校验启用配置并创建带 Bearer 认证的异步 HTTP 客户端。

        ``base_url`` 应指向包含版本号的服务根地址，例如 ``.../v1``；请求方法会追加
        ``rerank``。真实 API Key 只进入 Authorization Header，从不写入日志、异常或
        返回对象。配置缺失属于部署错误，会在创建实例时直接失败。
        """
        base_url = settings.base_url
        api_key = settings.api_key
        model = settings.model
        if not settings.enabled or base_url is None or api_key is None or model is None:
            raise ValueError("OpenAI-compatible rerank adapter requires complete settings")
        self._settings = settings
        self._model = model
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(settings.timeout_seconds),
            trust_env=False,
        )
        self._semaphore = asyncio.Semaphore(settings.max_concurrency)

    @property
    def enabled(self) -> bool:
        """报告该实例已配置真实 Cross-Encoder；单次故障仍由 Outcome 标记降级。"""
        return True

    async def close(self) -> None:
        """仅关闭本实例自行创建的连接池，保留测试或调用方注入的共享客户端。"""
        if self._owns_client:
            await self._client.aclose()

    async def rerank(
        self,
        query: str,
        documents: Sequence[str],
    ) -> RerankOutcome:
        """为每个候选计算相关性分数，并保持分数与输入下标严格一一对应。

        兼容服务通常按相关性返回结果，而领域层要求分数仍按输入文档顺序排列，所以
        必须使用每项 ``index`` 重新装配。空候选无需产生付费外部调用。所有可处理的
        网络或协议故障统一返回等长 ``None``，上层据此保留原 RRF 排名并附加 Warning；
        协程取消不会被捕获，会继续传播到 HTTP 请求和客户端。
        """
        if not documents:
            return RerankOutcome(scores=(), enabled=True)
        if not query.strip() or any(not document.strip() for document in documents):
            return self._fallback(len(documents))

        payload = {
            "model": self._model,
            "query": query,
            "documents": list(documents),
            "top_n": len(documents),
            "return_documents": False,
        }
        for attempt in range(self._settings.max_attempts):
            try:
                async with self._semaphore:
                    response = await self._client.post("rerank", json=payload)
            except (httpx.TimeoutException, httpx.RequestError):
                if attempt + 1 < self._settings.max_attempts:
                    await self._retry_delay(attempt)
                    continue
                return self._fallback(len(documents))

            if response.status_code in _RETRY_STATUSES:
                if attempt + 1 < self._settings.max_attempts:
                    await self._retry_delay(attempt)
                    continue
                return self._fallback(len(documents))
            if not response.is_success:
                return self._fallback(len(documents))

            try:
                scores = self._parse_scores(response.json(), len(documents))
            except (ValueError, TypeError):
                return self._fallback(len(documents))
            return RerankOutcome(scores=scores, enabled=True)
        return self._fallback(len(documents))

    @staticmethod
    def _parse_scores(value: Any, document_count: int) -> tuple[float, ...]:
        """严格校验 ``results``、唯一候选下标和有限数值，再恢复输入顺序。

        ``top_n`` 与输入数量相同，因此响应必须覆盖每个候选，不能把缺项悄悄填成零；
        否则相关文档可能被错误压到末尾。服务端即使改变返回排序，也不会影响映射。
        """
        if not isinstance(value, dict) or not isinstance(value.get("results"), list):
            raise ValueError("rerank response must contain a results list")
        resolved: list[float | None] = [None] * document_count
        for item in value["results"]:
            if not isinstance(item, dict):
                raise ValueError("rerank result must be an object")
            index = item.get("index")
            relevance_score = item.get("relevance_score")
            if (
                not isinstance(index, int)
                or isinstance(index, bool)
                or index < 0
                or index >= document_count
                or resolved[index] is not None
                or not isinstance(relevance_score, (int, float))
                or isinstance(relevance_score, bool)
                or not math.isfinite(float(relevance_score))
            ):
                raise ValueError("rerank result index or score is invalid")
            resolved[index] = float(relevance_score)
        if any(score is None for score in resolved):
            raise ValueError("rerank response does not cover every input document")
        return tuple(score for score in resolved if score is not None)

    @staticmethod
    async def _retry_delay(attempt: int) -> None:
        """对临时限流或过载做短暂线性退避，且始终受最大尝试次数约束。"""
        await asyncio.sleep(0.25 * (attempt + 1))

    @staticmethod
    def _fallback(document_count: int) -> RerankOutcome:
        """构造可观察降级结果，不伪造 Cross-Encoder 分数，也不改变候选顺序。"""
        return RerankOutcome(
            scores=tuple(None for _ in range(document_count)),
            enabled=False,
            warning=_FALLBACK_WARNING,
        )
