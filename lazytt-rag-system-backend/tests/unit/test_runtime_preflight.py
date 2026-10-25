"""启动预检测试：模型清单、Embedding 实际维度、视觉模型、RAGFlow 与 API Key。"""

from __future__ import annotations

import httpx
import pytest

from rag_service.bootstrap.runtime import RuntimeDependencyPreflight
from rag_service.domain.errors import EmbeddingError
from rag_service.settings import MediaSection, ServiceSettings, VisionSection


class FakeEmbedding:
    def __init__(self, dimension: int) -> None:
        self.dimension = dimension
        self.calls = 0

    async def embed(self, texts: list[str]) -> tuple[tuple[float, ...], ...]:
        self.calls += 1
        return (tuple(0.0 for _ in range(self.dimension)),)


@pytest.mark.asyncio
async def test_runtime_preflight_checks_models_embedding_and_ragflow(
    service_settings: ServiceSettings,
) -> None:
    async def model_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/chat/completions":
            body = request.read().decode("utf-8")
            # 同一个 Mock 同时服务普通回答、结构化 Rewrite 和视觉 OCR。按请求形态
            # 返回不同内容，确保预检真的校验三种协议，而不是任意文本都能蒙混过关。
            content = (
                '{"status":"ok"}'
                if '"response_format"' in body
                else "LAZYTT 2026"
                if '"image_url"' in body
                else "OK"
            )
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": content}}]},
            )
        assert request.url.path == "/v1/models"
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "synthetic-chat"},
                    {"id": "synthetic-embedding"},
                    {"id": "synthetic-vision"},
                ]
            },
        )

    async def ragflow_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/healthz"):
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "tenant_id": "tenant",
                        "embd_id": service_settings.ragflow.embedding_model,
                        "img2txt_id": service_settings.ragflow.vision_model,
                    },
                },
            )
        return httpx.Response(200, json={"code": 0, "data": {"id": "tenant"}})

    model_client = httpx.AsyncClient(
        base_url="http://models.test/v1/",
        transport=httpx.MockTransport(model_handler),
    )
    ragflow_client = httpx.AsyncClient(
        base_url="http://ragflow.test/",
        transport=httpx.MockTransport(ragflow_handler),
    )
    embedding = FakeEmbedding(1024)
    preflight = RuntimeDependencyPreflight(
        service_settings,
        model_client=model_client,
        vision_client=model_client,
        ragflow_client=ragflow_client,
        embedding=embedding,
    )

    result = await preflight.check()

    assert result.embedding_dimension == 1024
    assert result.vision_model == "synthetic-vision"
    assert result.media_vision_model is None
    assert result.ragflow_api_key == "ok"
    assert result.rerank == "disabled_vector_bm25_rrf_fallback"
    assert embedding.calls == 1
    await model_client.aclose()
    await ragflow_client.aclose()


@pytest.mark.asyncio
async def test_runtime_preflight_validates_enabled_media_asr(
    service_settings: ServiceSettings,
) -> None:
    """音视频开启时，预检会真实发送合成 WAV 并报告能力已验证。"""
    settings = service_settings.model_copy(
        update={
            "models": service_settings.models.model_copy(
                update={
                    "media": MediaSection(
                        enabled=True,
                        asr_base_url="https://asr.invalid/v1",
                        asr_api_key="synthetic-key",
                        asr_model="synthetic-asr-model",
                        # 单元测试只验证可执行文件发现，不真正运行这两个命令。
                        ffmpeg_binary="true",
                        ffprobe_binary="true",
                    ),
                    # 媒体视觉与文档 OCR 使用不同模型，防止测试因复用同一 Client
                    # 和模型名而漏掉配置接线错误。
                    "media_vision": VisionSection(
                        base_url="https://media-vision.invalid/v1",
                        api_key="synthetic-media-vision-key",
                        model="synthetic-media-vision-model",
                    ),
                }
            )
        }
    )

    async def model_handler(request: httpx.Request) -> httpx.Response:
        """区分普通回答、JSON Rewrite、视觉 OCR 和可选模型清单。"""
        if request.url.path.endswith("/chat/completions"):
            body = request.read().decode("utf-8")
            content = (
                '{"status":"ok"}'
                if '"response_format"' in body
                else "LAZYTT 2026"
                if '"image_url"' in body
                else "OK"
            )
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": content}}]},
            )
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "synthetic-chat"},
                    {"id": "synthetic-embedding"},
                    {"id": "synthetic-vision"},
                ]
            },
        )

    async def asr_handler(request: httpx.Request) -> httpx.Response:
        """确认预检上传的是内存合成 Data URI，不包含用户文件路径。"""
        body = request.read().decode("utf-8")
        assert "data:audio/wav;base64," in body
        assert "synthetic-asr-model" in body
        return httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})

    async def media_vision_handler(request: httpx.Request) -> httpx.Response:
        """确认启动探测使用独立媒体视觉模型，而不是文档 OCR 模型。"""
        body = request.read().decode("utf-8")
        assert '"model":"synthetic-media-vision-model"' in body
        assert "data:image/png;base64," in body
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "LAZYTT 2026"}}]},
        )

    async def ragflow_handler(request: httpx.Request) -> httpx.Response:
        """模拟健康、账户及租户模型配置三个 RAGFlow 端点。"""
        if request.url.path.endswith("/healthz"):
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "embd_id": settings.ragflow.embedding_model,
                        "img2txt_id": settings.ragflow.vision_model,
                    },
                },
            )
        return httpx.Response(200, json={"code": 0, "data": {"id": "tenant"}})

    model_client = httpx.AsyncClient(
        base_url="http://models.test/v1/",
        transport=httpx.MockTransport(model_handler),
    )
    media_client = httpx.AsyncClient(
        base_url="https://asr.invalid/v1/",
        transport=httpx.MockTransport(asr_handler),
    )
    media_vision_client = httpx.AsyncClient(
        base_url="https://media-vision.invalid/v1/",
        transport=httpx.MockTransport(media_vision_handler),
    )
    ragflow_client = httpx.AsyncClient(
        base_url="http://ragflow.test/",
        transport=httpx.MockTransport(ragflow_handler),
    )
    preflight = RuntimeDependencyPreflight(
        settings,
        model_client=model_client,
        vision_client=model_client,
        media_client=media_client,
        media_vision_client=media_vision_client,
        ragflow_client=ragflow_client,
        embedding=FakeEmbedding(1024),
    )
    result = await preflight.check()

    assert result.media_asr == "enabled_validated"
    assert result.media_vision_model == "synthetic-media-vision-model"
    await model_client.aclose()
    await media_client.aclose()
    await media_vision_client.aclose()
    await ragflow_client.aclose()


@pytest.mark.asyncio
async def test_runtime_preflight_fails_closed_on_dimension_mismatch(
    service_settings: ServiceSettings,
) -> None:
    async def model_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "synthetic-chat"},
                    {"id": "synthetic-embedding"},
                    {"id": "synthetic-vision"},
                ]
            },
        )

    model_client = httpx.AsyncClient(
        base_url="http://models.test/v1/",
        transport=httpx.MockTransport(model_handler),
    )
    ragflow_client = httpx.AsyncClient(
        base_url="http://ragflow.test/",
        transport=httpx.MockTransport(lambda _: httpx.Response(500)),
    )
    preflight = RuntimeDependencyPreflight(
        service_settings,
        model_client=model_client,
        vision_client=model_client,
        ragflow_client=ragflow_client,
        embedding=FakeEmbedding(384),
    )

    with pytest.raises(EmbeddingError) as captured:
        await preflight.check()

    assert captured.value.code == "RAG-CONFIG-007"
    await model_client.aclose()
    await ragflow_client.aclose()
