"""OpenAI-compatible Chat Completions、SSE 和 Embeddings 的严格异步适配器。

适配器负责网络超时、有限重试、并发上限和响应结构校验，把不同服务实现统一为领域
端口。只有在尚未向调用方发出流式文本时才重试，避免客户端收到重复答案片段。
"""

from __future__ import annotations

import asyncio
import json
import math
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx

from rag_service.domain.errors import EmbeddingError, LlmError
from rag_service.domain.ports import (
    LlmEvent,
    LlmRequest,
    LlmResponse,
    LlmUsage,
)
from rag_service.settings import EmbeddingSection, LlmSection

_RETRY_STATUSES = frozenset({429, 502, 503, 504})


def _usage(value: Any) -> LlmUsage | None:
    """宽容解析可选 Token 用量；字段缺失则返回 ``None``，错误类型则拒绝响应。"""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise LlmError.invalid_response()
    prompt = value.get("prompt_tokens", 0)
    completion = value.get("completion_tokens", 0)
    total = value.get("total_tokens", prompt + completion)
    if not all(isinstance(item, int) and item >= 0 for item in (prompt, completion, total)):
        raise LlmError.invalid_response()
    return LlmUsage(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
    )


def _llm_payload(
    request: LlmRequest,
    settings: LlmSection,
    *,
    stream: bool,
) -> dict[str, Any]:
    """把领域请求转换为 OpenAI-compatible JSON，并应用端点级协议覆盖。

    Profile 中仍保存可复现的业务采样参数；Bootstrap 会用真实请求探测兼容端点需要
    的有限协议覆盖，再由 Settings 统一控制省略规则、Token 字段和 ``extra_body``。
    业务代码不识别供应商或模型名称。额外字段不能覆盖
    model/messages/stream 等核心字段，防止环境配置绕过领域请求和流式控制。
    """
    payload: dict[str, Any] = {
        "model": request.model,
        "messages": [
            {"role": message.role, "content": message.content} for message in request.messages
        ],
        settings.max_output_tokens_field: request.max_output_tokens,
        "stream": stream,
    }
    if not settings.omit_sampling_parameters:
        payload["temperature"] = request.temperature
        payload["top_p"] = request.top_p
    if request.response_format == "json_object":
        payload["response_format"] = {"type": "json_object"}
    elif request.response_format != "text":
        raise ValueError(f"unsupported response format: {request.response_format}")
    # ``stream_options`` 并不是所有兼容服务都接受。Token 用量事件本来就是可选信息，
    # 省略该字段仍能获得标准 SSE 文本，并避免一个非必要扩展阻断第三方兼容端点。
    protected_fields = {
        "model",
        "messages",
        "stream",
        "stream_options",
        "response_format",
        "max_tokens",
        "max_completion_tokens",
    }
    if protected_fields.intersection(settings.extra_body):
        raise ValueError("LLM extra_body cannot override protected request fields")
    payload.update(settings.extra_body)
    return payload


class OpenAICompatibleLlmAdapter:
    """带并发限制与有限重试的文本生成端口实现。"""
    def __init__(
        self,
        settings: LlmSection,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """保存设置，创建或接收异步 HTTP 客户端，并建立并发信号量。"""
        self._settings = settings
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=settings.base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {settings.api_key}"},
            timeout=httpx.Timeout(
                connect=settings.connect_timeout_seconds,
                read=settings.read_timeout_seconds,
                write=settings.read_timeout_seconds,
                pool=settings.connect_timeout_seconds,
            ),
            trust_env=False,
        )
        self._semaphore = asyncio.Semaphore(settings.max_concurrency)

    async def close(self) -> None:
        """仅关闭本适配器自己创建的 HTTP 客户端。"""
        if self._owns_client:
            await self._client.aclose()

    async def complete(self, request: LlmRequest) -> LlmResponse:
        """执行非流式生成，重试临时网络/429/5xx，并严格校验 JSON 响应。"""
        payload = _llm_payload(request, self._settings, stream=False)
        for attempt in range(self._settings.max_attempts):
            try:
                async with self._semaphore:
                    response = await self._client.post(
                        "chat/completions",
                        json=payload,
                    )
            except httpx.TimeoutException as exc:
                if attempt + 1 < self._settings.max_attempts:
                    await self._retry_delay(attempt)
                    continue
                raise LlmError.timeout() from exc
            except httpx.RequestError as exc:
                if attempt + 1 < self._settings.max_attempts:
                    await self._retry_delay(attempt)
                    continue
                raise LlmError.unavailable() from exc

            if response.status_code in _RETRY_STATUSES:
                if attempt + 1 < self._settings.max_attempts:
                    await self._retry_delay(attempt)
                    continue
                raise LlmError.unavailable()
            if not response.is_success:
                raise LlmError.unavailable()
            try:
                body = response.json()
            except ValueError as exc:
                raise LlmError.invalid_response() from exc
            result = self._parse_completion(body)
            if request.response_format == "json_object":
                try:
                    structured = json.loads(result.content)
                except json.JSONDecodeError as exc:
                    raise LlmError.invalid_response() from exc
                if not isinstance(structured, dict):
                    raise LlmError.invalid_response()
            return result
        raise LlmError.unavailable()

    def stream(self, request: LlmRequest) -> AsyncIterator[LlmEvent]:
        """返回异步事件迭代器，真正网络请求在迭代开始时发生。"""
        return self._stream(request)

    async def _stream(self, request: LlmRequest) -> AsyncIterator[LlmEvent]:
        """读取 SSE 增量；一旦发出任何文本，后续故障不再自动重试。"""
        payload = _llm_payload(request, self._settings, stream=True)
        for attempt in range(self._settings.max_attempts):
            emitted = False
            try:
                async with self._semaphore:
                    async with self._client.stream(
                        "POST",
                        "chat/completions",
                        json=payload,
                    ) as response:
                        if response.status_code in _RETRY_STATUSES:
                            if attempt + 1 < self._settings.max_attempts:
                                await response.aread()
                                await self._retry_delay(attempt)
                                continue
                            raise LlmError.unavailable()
                        if not response.is_success:
                            raise LlmError.unavailable()
                        async for data in self._sse_data(response):
                            if data == "[DONE]":
                                return
                            event = self._parse_stream_event(data)
                            if event.delta or event.usage is not None:
                                emitted = emitted or bool(event.delta)
                                yield event
                        return
            except httpx.TimeoutException as exc:
                if not emitted and attempt + 1 < self._settings.max_attempts:
                    await self._retry_delay(attempt)
                    continue
                raise LlmError.timeout() from exc
            except httpx.RequestError as exc:
                if not emitted and attempt + 1 < self._settings.max_attempts:
                    await self._retry_delay(attempt)
                    continue
                raise LlmError.unavailable() from exc
        raise LlmError.unavailable()

    async def _retry_delay(self, attempt: int) -> None:
        """按尝试序号线性增加重试等待时间。"""
        await asyncio.sleep(self._settings.retry_base_delay_ms * (attempt + 1) / 1000)

    @staticmethod
    async def _sse_data(response: httpx.Response) -> AsyncIterator[str]:
        """按 SSE 空行边界合并一个事件中的多条 ``data:`` 行。"""
        data_lines: list[str] = []
        async for line in response.aiter_lines():
            if not line:
                if data_lines:
                    yield "\n".join(data_lines)
                    data_lines.clear()
                continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if data_lines:
            yield "\n".join(data_lines)

    @staticmethod
    def _parse_completion(value: Any) -> LlmResponse:
        """从首个 choice 提取非空 assistant 文本和可选用量。"""
        if not isinstance(value, dict):
            raise LlmError.invalid_response()
        choices = value.get("choices")
        if not isinstance(choices, list) or not choices:
            raise LlmError.invalid_response()
        choice = choices[0]
        if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
            raise LlmError.invalid_response()
        content = choice["message"].get("content")
        if not isinstance(content, str) or not content:
            raise LlmError.invalid_response()
        return LlmResponse(content=content, usage=_usage(value.get("usage")))

    @staticmethod
    def _parse_stream_event(data: str) -> LlmEvent:
        """解析单个 SSE JSON 数据，提取增量文本和可选最终用量。"""
        try:
            value = json.loads(data)
        except json.JSONDecodeError as exc:
            raise LlmError.invalid_response() from exc
        if not isinstance(value, dict):
            raise LlmError.invalid_response()
        delta = ""
        choices = value.get("choices")
        if isinstance(choices, list) and choices:
            choice = choices[0]
            if not isinstance(choice, dict):
                raise LlmError.invalid_response()
            delta_object = choice.get("delta")
            if isinstance(delta_object, dict):
                content = delta_object.get("content")
                if content is not None and not isinstance(content, str):
                    raise LlmError.invalid_response()
                delta = content or ""
        return LlmEvent(delta=delta, usage=_usage(value.get("usage")))


class OpenAICompatibleEmbeddingAdapter:
    """保持输入顺序、固定维度并拒绝无效数值的批量 Embedding 适配器。"""
    def __init__(
        self,
        settings: EmbeddingSection,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """创建或接收 HTTP 客户端，并按配置限制并发请求数。"""
        self._settings = settings
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=settings.base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {settings.api_key}"},
            timeout=httpx.Timeout(
                connect=settings.connect_timeout_seconds,
                read=settings.read_timeout_seconds,
                write=settings.read_timeout_seconds,
                pool=settings.connect_timeout_seconds,
            ),
            trust_env=False,
        )
        self._semaphore = asyncio.Semaphore(settings.max_concurrency)

    async def close(self) -> None:
        """仅关闭由本适配器创建的 HTTP 客户端。"""
        if self._owns_client:
            await self._client.aclose()

    async def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        """按 batch_size 切分输入、顺序调用批接口并拼回同序向量。"""
        if not texts:
            return ()
        vectors: list[tuple[float, ...]] = []
        for offset in range(0, len(texts), self._settings.batch_size):
            batch = list(texts[offset : offset + self._settings.batch_size])
            vectors.extend(await self._embed_batch(batch))
        return tuple(vectors)

    async def _embed_batch(self, texts: list[str]) -> tuple[tuple[float, ...], ...]:
        """请求单批向量，针对临时网络/状态码故障有限重试。"""
        for attempt in range(self._settings.max_attempts):
            try:
                async with self._semaphore:
                    response = await self._client.post(
                        "embeddings",
                        json={"model": self._settings.model, "input": texts},
                    )
            except httpx.TimeoutException as exc:
                if attempt + 1 < self._settings.max_attempts:
                    await self._retry_delay(attempt)
                    continue
                raise EmbeddingError.unavailable() from exc
            except httpx.RequestError as exc:
                if attempt + 1 < self._settings.max_attempts:
                    await self._retry_delay(attempt)
                    continue
                raise EmbeddingError.unavailable() from exc

            if response.status_code in _RETRY_STATUSES:
                if attempt + 1 < self._settings.max_attempts:
                    await self._retry_delay(attempt)
                    continue
                raise EmbeddingError.unavailable()
            if not response.is_success:
                raise EmbeddingError.unavailable()
            try:
                body = response.json()
            except ValueError as exc:
                raise EmbeddingError.invalid_response() from exc
            return self._parse_embeddings(body, len(texts))
        raise EmbeddingError.unavailable()

    async def _retry_delay(self, attempt: int) -> None:
        """按尝试次数线性退避，单位由毫秒配置转换为秒。"""
        await asyncio.sleep(self._settings.retry_base_delay_ms * (attempt + 1) / 1000)

    def _parse_embeddings(
        self,
        value: Any,
        expected_count: int,
    ) -> tuple[tuple[float, ...], ...]:
        """验证数量、索引、维度、有限数值和重复项，并恢复输入顺序。"""
        if not isinstance(value, dict) or not isinstance(value.get("data"), list):
            raise EmbeddingError.invalid_response()
        data = value["data"]
        if len(data) != expected_count:
            raise EmbeddingError.invalid_response()
        ordered: list[tuple[float, ...] | None] = [None] * expected_count
        for fallback_index, item in enumerate(data):
            if not isinstance(item, dict) or not isinstance(item.get("embedding"), list):
                raise EmbeddingError.invalid_response()
            index = item.get("index", fallback_index)
            if not isinstance(index, int) or not 0 <= index < expected_count:
                raise EmbeddingError.invalid_response()
            raw_vector = item["embedding"]
            if len(raw_vector) != self._settings.dimension:
                raise EmbeddingError.dimension_mismatch(self._settings.dimension, len(raw_vector))
            if not all(
                isinstance(component, int | float) and math.isfinite(float(component))
                for component in raw_vector
            ):
                raise EmbeddingError.invalid_response()
            if ordered[index] is not None:
                raise EmbeddingError.invalid_response()
            ordered[index] = tuple(float(component) for component in raw_vector)
        if any(vector is None for vector in ordered):
            raise EmbeddingError.invalid_response()
        return tuple(vector for vector in ordered if vector is not None)
