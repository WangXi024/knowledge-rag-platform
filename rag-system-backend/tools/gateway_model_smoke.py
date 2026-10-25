"""直接验证 Gateway 所配置 Chat、结构化输出、流式输出和 Embedding 模型契约。"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from rag_service.domain.ports import LlmMessage, LlmRequest
from rag_service.infrastructure.models.openai_compatible import (
    OpenAICompatibleEmbeddingAdapter,
    OpenAICompatibleLlmAdapter,
)
from rag_service.settings import EmbeddingSection, LlmSection


class ModelSmokeFailure(RuntimeError):
    """Smoke 断言或环境配置不满足预期。"""
    pass


def required_env(name: str) -> str:
    """读取必需环境变量并拒绝空值。"""
    value = os.getenv(name, "").strip()
    if not value:
        raise ModelSmokeFailure(f"required environment variable is missing: {name}")
    return value


def boolean_env(name: str) -> bool:
    """读取 Bootstrap 写回的严格布尔值，避免 Smoke 与生产 Adapter 配置漂移。"""
    value = required_env(name).lower()
    if value not in {"true", "false"}:
        raise ModelSmokeFailure(f"environment variable {name} must be true or false")
    return value == "true"


def extra_body_env() -> dict[str, Any]:
    """读取不含密钥的 Chat 协议扩展，并要求它是 JSON Object。"""
    try:
        value = json.loads(required_env("LLM_EXTRA_BODY_JSON"))
    except json.JSONDecodeError as exc:
        raise ModelSmokeFailure("LLM_EXTRA_BODY_JSON is invalid") from exc
    if not isinstance(value, dict):
        raise ModelSmokeFailure("LLM_EXTRA_BODY_JSON must be a JSON object")
    return value


async def run() -> dict[str, Any]:
    """调用真实模型端点并验证内容、SSE 增量、JSON Object、向量数量与维度。"""
    smoke_base_url = os.getenv("MODEL_SMOKE_BASE_URL", "").strip()
    llm_base_url = smoke_base_url or required_env("LLM_BASE_URL")
    embedding_base_url = smoke_base_url or required_env("EMBEDDING_BASE_URL")
    dimension = int(required_env("EMBEDDING_DIMENSION"))
    llm_settings = LlmSection(
        base_url=llm_base_url,
        api_key=required_env("LLM_API_KEY"),
        model=required_env("LLM_MODEL"),
        rewrite_model=required_env("REWRITE_MODEL"),
        max_attempts=2,
        retry_base_delay_ms=100,
        max_concurrency=2,
        # 直接复用 Bootstrap 真实探测出的非敏感协议结果，确保 Smoke 和生产 Adapter
        # 对任意兼容供应商发送完全一致的 Token、采样和扩展字段。
        omit_sampling_parameters=boolean_env("LLM_OMIT_SAMPLING_PARAMETERS"),
        max_output_tokens_field=required_env("LLM_MAX_OUTPUT_TOKENS_FIELD"),
        extra_body=extra_body_env(),
    )
    embedding_settings = EmbeddingSection(
        base_url=embedding_base_url,
        api_key=required_env("EMBEDDING_API_KEY"),
        model=required_env("EMBEDDING_MODEL"),
        dimension=dimension,
        max_attempts=2,
        retry_base_delay_ms=100,
        max_concurrency=2,
        batch_size=32,
    )
    llm = OpenAICompatibleLlmAdapter(llm_settings)
    embedding = OpenAICompatibleEmbeddingAdapter(embedding_settings)
    evidence: dict[str, Any] = {"steps": []}
    try:
        single = await embedding.embed(["Synthetic gateway embedding probe."])
        if len(single) != 1 or len(single[0]) != dimension:
            raise ModelSmokeFailure("single embedding shape is invalid")
        evidence["steps"].append(
            {
                "operation": "gateway_embedding_single",
                "result": "ok",
                "dimension": len(single[0]),
            }
        )

        batch = await embedding.embed(
            [
                "Synthetic gateway embedding probe alpha.",
                "Synthetic gateway embedding probe beta.",
            ]
        )
        if len(batch) != 2 or any(len(vector) != dimension for vector in batch):
            raise ModelSmokeFailure("batch embedding shape is invalid")
        evidence["steps"].append(
            {
                "operation": "gateway_embedding_batch",
                "result": "ok",
                "count": len(batch),
                "dimensions": [len(vector) for vector in batch],
            }
        )

        ordinary = await llm.complete(
            LlmRequest(
                messages=(
                    LlmMessage(
                        role="user",
                        content="Reply with exactly MODEL_SMOKE_OK.",
                    ),
                ),
                model=llm_settings.model,
                max_output_tokens=256,
                temperature=0,
            )
        )
        if not ordinary.content.strip():
            raise ModelSmokeFailure("non-streaming chat returned empty content")
        evidence["steps"].append(
            {
                "operation": "gateway_chat_non_stream",
                "result": "ok",
                "model": llm_settings.model,
                "content_present": True,
            }
        )

        structured = await llm.complete(
            LlmRequest(
                messages=(
                    LlmMessage(
                        role="system",
                        content="Return only a JSON object with boolean field ok.",
                    ),
                    LlmMessage(role="user", content="Return the successful result."),
                ),
                model=llm_settings.model,
                max_output_tokens=256,
                temperature=0,
                response_format="json_object",
            )
        )
        parsed = json.loads(structured.content)
        if not isinstance(parsed, dict) or not parsed:
            raise ModelSmokeFailure("structured chat did not return a JSON object")
        evidence["steps"].append(
            {
                "operation": "gateway_chat_structured",
                "result": "ok",
                "json_object": True,
            }
        )

        deltas: list[str] = []
        usage_seen = False
        async for event in llm.stream(
            LlmRequest(
                messages=(
                    LlmMessage(
                        role="user",
                        content="Reply with exactly STREAM_SMOKE_OK.",
                    ),
                ),
                model=llm_settings.model,
                max_output_tokens=256,
                temperature=0,
            )
        ):
            deltas.append(event.delta)
            usage_seen = usage_seen or event.usage is not None
        if not "".join(deltas).strip():
            raise ModelSmokeFailure("streaming chat returned no deltas")
        evidence["steps"].append(
            {
                "operation": "gateway_chat_sse",
                "result": "ok",
                "delta_count": sum(bool(delta) for delta in deltas),
                "usage_seen": usage_seen,
            }
        )
    finally:
        await llm.close()
        await embedding.close()

    evidence["result"] = "ok"
    return evidence


def main() -> None:
    """运行异步 Smoke，并将证据以 JSON 输出。"""
    try:
        print(json.dumps(asyncio.run(run()), ensure_ascii=False, indent=2, sort_keys=True))
    except (ModelSmokeFailure, ValueError) as exc:
        raise SystemExit(f"gateway model smoke failed: {exc}") from exc


if __name__ == "__main__":
    main()
