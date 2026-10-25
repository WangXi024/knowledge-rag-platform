"""通用 Rerank Adapter 的协议、排序恢复和安全降级回归测试。

测试只使用 ``httpx.MockTransport`` 和合成短文本，不访问公网、不消耗真实额度，也不
保存 API Key。重点保证云端结果即使按分数乱序返回也能对应回原候选，并保证鉴权失败、
限流耗尽或响应缺项时继续使用 RRF，而不是让问答请求返回 500。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from rag_service.infrastructure.models.openai_compatible_rerank import (
    OpenAICompatibleRerankAdapter,
)
from rag_service.settings import RerankSection


def _settings(**overrides: Any) -> RerankSection:
    """构造不含真实凭据的启用配置；测试可按场景覆盖重试次数。"""
    return RerankSection(
        enabled=True,
        base_url="https://rerank.test/v1",
        api_key="synthetic-test-key",
        model="synthetic-rerank-model",
        **overrides,
    )


@pytest.mark.asyncio
async def test_compatible_rerank_maps_sorted_results_back_to_input_order() -> None:
    """云端按相关性返回 1、0 下标时，领域分数仍必须按原输入顺序排列。"""
    captured: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers.get("Authorization")
        captured["path"] = request.url.path
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "rerank-synthetic",
                "results": [
                    {"index": 1, "relevance_score": 0.91},
                    {"index": 0, "relevance_score": 0.23},
                ],
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://rerank.test/v1/",
        headers={"Authorization": "Bearer synthetic-test-key"},
    )
    adapter = OpenAICompatibleRerankAdapter(_settings(), client=client)

    outcome = await adapter.rerank("battery", ("first document", "battery document"))

    assert adapter.enabled is True
    assert outcome.enabled is True
    assert outcome.warning is None
    assert outcome.scores == (0.23, 0.91)
    assert captured["path"] == "/v1/rerank"
    assert captured["authorization"] == "Bearer synthetic-test-key"
    assert captured["body"] == {
        "model": "synthetic-rerank-model",
        "query": "battery",
        "documents": ["first document", "battery document"],
        "top_n": 2,
        "return_documents": False,
    }
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "body"),
    [
        (401, {"message": "Invalid token"}),
        (429, {"message": "rate limited"}),
        (200, {"results": [{"index": 0, "relevance_score": 0.8}]}),
        (
            200,
            {
                "results": [
                    {"index": 0, "relevance_score": "not-a-number"},
                    {"index": 1, "relevance_score": 0.2},
                ]
            },
        ),
    ],
)
async def test_compatible_rerank_failures_return_observable_rrf_fallback(
    status_code: int,
    body: dict[str, Any],
) -> None:
    """鉴权、限流和协议错误必须返回等长空分数及统一 Warning，不抛出 500。"""
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(status_code, json=body)),
        base_url="https://rerank.test/v1/",
    )
    adapter = OpenAICompatibleRerankAdapter(_settings(), client=client)

    outcome = await adapter.rerank("query", ("one", "two"))

    assert outcome.enabled is False
    assert outcome.scores == (None, None)
    assert outcome.warning == "rerank_service_unavailable_vector_bm25_rrf_fallback"
    await client.aclose()


@pytest.mark.asyncio
async def test_compatible_rerank_skips_paid_call_for_empty_candidates() -> None:
    """没有候选时直接返回空成功结果，避免产生无意义的外部计费请求。"""
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://rerank.test/v1/",
    )
    adapter = OpenAICompatibleRerankAdapter(_settings(), client=client)

    outcome = await adapter.rerank("query", ())

    assert outcome.enabled is True
    assert outcome.scores == ()
    assert calls == 0
    await client.aclose()
