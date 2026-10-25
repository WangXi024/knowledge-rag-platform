"""模型适配器测试：非流式/流式协议、取消、批量向量顺序、维度和 Rerank 降级。"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from rag_service.domain.errors import EmbeddingError
from rag_service.domain.ports import LlmMessage, LlmRequest
from rag_service.infrastructure.models.openai_compatible import (
    OpenAICompatibleEmbeddingAdapter,
    OpenAICompatibleLlmAdapter,
)
from rag_service.infrastructure.models.rerank import DisabledRerankAdapter
from rag_service.settings import EmbeddingSection, LlmSection


def _llm_settings(**overrides: Any) -> LlmSection:
    values: dict[str, Any] = {
        "base_url": "http://models.test/v1",
        "api_key": "test-key",
        "model": "synthetic-chat-model",
        "rewrite_model": "synthetic-rewrite-model",
        "retry_base_delay_ms": 1,
    }
    values.update(overrides)
    return LlmSection(
        **values,
    )


@pytest.mark.parametrize(
    "field",
    (
        "model",
        "messages",
        "stream",
        "response_format",
        "max_tokens",
        "max_completion_tokens",
    ),
)
def test_llm_settings_reject_extra_body_that_overrides_core_request(field: str) -> None:
    """环境附加 JSON 只能补兼容开关，不能改写模型、消息或响应控制字段。"""
    with pytest.raises(ValueError, match="protected request fields"):
        _llm_settings(extra_body={field: "synthetic-override"})


def _embedding_settings(**overrides: Any) -> EmbeddingSection:
    return EmbeddingSection(
        base_url="http://models.test/v1",
        api_key="test-key",
        model="synthetic-embedding-model",
        dimension=3,
        retry_base_delay_ms=1,
        **overrides,
    )


def _request(response_format: str = "text") -> LlmRequest:
    return LlmRequest(
        messages=(LlmMessage(role="user", content="synthetic question"),),
        model="synthetic-chat-model",
        max_output_tokens=64,
        response_format=response_format,
    )


@pytest.mark.asyncio
async def test_llm_complete_and_structured_output() -> None:
    bodies: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        content = (
            '{"status":"ok"}'
            if body.get("response_format") == {"type": "json_object"}
            else "ordinary answer"
        )
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": content}}],
                "usage": {
                    "prompt_tokens": 4,
                    "completion_tokens": 2,
                    "total_tokens": 6,
                },
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://models.test/v1/",
    )
    adapter = OpenAICompatibleLlmAdapter(_llm_settings(), client=client)

    ordinary = await adapter.complete(_request())
    structured = await adapter.complete(_request("json_object"))

    assert ordinary.content == "ordinary answer"
    assert ordinary.usage is not None
    assert ordinary.usage.total_tokens == 6
    assert json.loads(structured.content) == {"status": "ok"}
    assert bodies[1]["response_format"] == {"type": "json_object"}
    await client.aclose()


@pytest.mark.asyncio
async def test_llm_protocol_can_omit_sampling_and_add_endpoint_override() -> None:
    """兼容端点可省略采样参数、切换 Token 字段并附加探测出的协议覆盖。"""
    captured: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "synthetic answer"}}]},
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://provider-a.invalid/v1/",
    )
    adapter = OpenAICompatibleLlmAdapter(
        _llm_settings(
            model="synthetic-reasoning-model",
            rewrite_model="synthetic-reasoning-model",
            omit_sampling_parameters=True,
            max_output_tokens_field="max_completion_tokens",
            extra_body={"thinking": {"type": "disabled"}},
        ),
        client=client,
    )

    await adapter.complete(
        LlmRequest(
            messages=(LlmMessage(role="user", content="synthetic question"),),
            model="synthetic-reasoning-model",
            max_output_tokens=64,
            temperature=0.0,
            top_p=0.8,
        )
    )

    assert captured["max_completion_tokens"] == 64
    assert captured["thinking"] == {"type": "disabled"}
    assert "max_tokens" not in captured
    assert "temperature" not in captured
    assert "top_p" not in captured
    await client.aclose()


@pytest.mark.asyncio
async def test_llm_stream_uses_same_protocol_overrides() -> None:
    """SSE 必须复用非流式协议覆盖，且不强制端点接受可选用量扩展。"""
    captured: dict[str, Any] = {}
    stream_body = "\n\n".join(
        [
            'data: {"choices":[{"delta":{"content":"synthetic stream"}}]}',
            "data: [DONE]",
        ]
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        """保存实际 SSE 请求体，并返回最小合法增量流。"""
        captured.update(json.loads(request.content))
        return httpx.Response(200, text=stream_body + "\n\n")

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://provider-a.invalid/v1/",
    )
    adapter = OpenAICompatibleLlmAdapter(
        _llm_settings(
            model="synthetic-reasoning-model",
            rewrite_model="synthetic-reasoning-model",
            omit_sampling_parameters=True,
            max_output_tokens_field="max_completion_tokens",
            extra_body={"thinking": {"type": "disabled"}},
        ),
        client=client,
    )

    events = [
        event
        async for event in adapter.stream(
            LlmRequest(
                messages=(LlmMessage(role="user", content="synthetic stream"),),
                model="synthetic-reasoning-model",
                max_output_tokens=64,
            )
        )
    ]

    assert "".join(event.delta for event in events) == "synthetic stream"
    assert captured["stream"] is True
    assert "stream_options" not in captured
    assert captured["max_completion_tokens"] == 64
    assert captured["thinking"] == {"type": "disabled"}
    assert "temperature" not in captured
    assert "top_p" not in captured
    await client.aclose()


@pytest.mark.asyncio
async def test_llm_protocol_can_keep_sampling_and_disable_endpoint_thinking() -> None:
    """标准 Token/采样字段可以与端点要求的关闭思考参数共同发送。"""
    captured: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        """保存任意兼容端点请求体并返回最小普通回答。"""
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "synthetic answer"}}]},
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://provider-b.invalid/v1/",
    )
    adapter = OpenAICompatibleLlmAdapter(
        _llm_settings(
            model="synthetic-standard-model",
            rewrite_model="synthetic-standard-model",
            omit_sampling_parameters=False,
            max_output_tokens_field="max_tokens",
            extra_body={"enable_thinking": False},
        ),
        client=client,
    )

    await adapter.complete(
        LlmRequest(
            messages=(LlmMessage(role="user", content="synthetic question"),),
            model="synthetic-standard-model",
            max_output_tokens=64,
            temperature=0.2,
            top_p=0.8,
        )
    )

    assert captured["model"] == "synthetic-standard-model"
    assert captured["max_tokens"] == 64
    assert captured["enable_thinking"] is False
    assert captured["temperature"] == 0.2
    assert captured["top_p"] == 0.8
    assert "max_completion_tokens" not in captured
    await client.aclose()


@pytest.mark.asyncio
async def test_llm_stream_yields_deltas_and_usage() -> None:
    stream_body = "\n\n".join(
        [
            'data: {"choices":[{"delta":{"content":"synthetic "}}]}',
            'data: {"choices":[{"delta":{"content":"answer"}}]}',
            (
                'data: {"choices":[],"usage":{"prompt_tokens":3,'
                '"completion_tokens":2,"total_tokens":5}}'
            ),
            "data: [DONE]",
        ]
    )

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=stream_body + "\n\n")

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://models.test/v1/",
    )
    adapter = OpenAICompatibleLlmAdapter(_llm_settings(), client=client)

    events = [event async for event in adapter.stream(_request())]

    assert "".join(event.delta for event in events) == "synthetic answer"
    usage = [event.usage for event in events if event.usage is not None]
    assert len(usage) == 1
    assert usage[0].total_tokens == 5
    await client.aclose()


@pytest.mark.asyncio
async def test_llm_cancel_propagates() -> None:
    entered = asyncio.Event()

    async def handler(_request: httpx.Request) -> httpx.Response:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://models.test/v1/",
    )
    adapter = OpenAICompatibleLlmAdapter(_llm_settings(), client=client)
    task = asyncio.create_task(adapter.complete(_request()))
    await entered.wait()

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    await client.aclose()


@pytest.mark.asyncio
async def test_embedding_batches_and_preserves_order() -> None:
    requests: list[list[str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        inputs = json.loads(request.content)["input"]
        requests.append(inputs)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": index, "embedding": [float(index), 1.0, 2.0]}
                    for index, _ in enumerate(inputs)
                ]
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://models.test/v1/",
    )
    adapter = OpenAICompatibleEmbeddingAdapter(
        _embedding_settings(batch_size=2),
        client=client,
    )

    vectors = await adapter.embed(["a", "b", "c"])

    assert requests == [["a", "b"], ["c"]]
    assert vectors == ((0.0, 1.0, 2.0), (1.0, 1.0, 2.0), (0.0, 1.0, 2.0))
    await client.aclose()


@pytest.mark.asyncio
async def test_embedding_dimension_mismatch_fails_closed() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": [{"index": 0, "embedding": [1.0, 2.0]}]},
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://models.test/v1/",
    )
    adapter = OpenAICompatibleEmbeddingAdapter(
        _embedding_settings(),
        client=client,
    )

    with pytest.raises(EmbeddingError) as captured:
        await adapter.embed(["probe"])

    assert captured.value.code == "RAG-CONFIG-007"
    await client.aclose()


@pytest.mark.asyncio
async def test_disabled_rerank_is_explicit_non_failing_fallback() -> None:
    adapter = DisabledRerankAdapter()

    outcome = await adapter.rerank("query", ["one", "two"])

    assert adapter.enabled is False
    assert outcome.enabled is False
    assert outcome.scores == (None, None)
    assert outcome.warning == "rerank_disabled_vector_bm25_rrf_fallback"
