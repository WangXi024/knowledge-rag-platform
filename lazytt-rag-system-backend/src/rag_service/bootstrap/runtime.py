"""启动前对模型 API、Embedding 契约和 RAGFlow 凭据的只读预检。"""

from __future__ import annotations

import base64
import io
import json
import shutil
import wave
from dataclasses import dataclass
from typing import Any

import httpx

from rag_service.domain.errors import EmbeddingError, LlmError, RagFlowError, RerankError
from rag_service.domain.ports import EmbeddingPort, RerankPort
from rag_service.infrastructure.models.openai_compatible import (
    OpenAICompatibleEmbeddingAdapter,
)
from rag_service.infrastructure.models.openai_compatible_rerank import (
    OpenAICompatibleRerankAdapter,
)
from rag_service.infrastructure.models.rerank import DisabledRerankAdapter
from rag_service.settings import ServiceSettings, VisionSection


@dataclass(frozen=True, slots=True)
class RuntimePreflightResult:
    """每项依赖检查通过后返回的机器可读摘要，不包含真实密钥。"""
    model_api: str
    chat_model: str
    embedding_model: str
    vision_model: str
    media_asr: str
    media_vision_model: str | None
    embedding_dimension: int
    ragflow: str
    ragflow_api_key: str
    rerank: str


class RuntimeDependencyPreflight:
    """在写入 Profile/索引前验证所有关键外部运行依赖。"""
    def __init__(
        self,
        settings: ServiceSettings,
        *,
        model_client: httpx.AsyncClient | None = None,
        vision_client: httpx.AsyncClient | None = None,
        media_client: httpx.AsyncClient | None = None,
        media_vision_client: httpx.AsyncClient | None = None,
        ragflow_client: httpx.AsyncClient | None = None,
        embedding: EmbeddingPort | None = None,
        reranker: RerankPort | None = None,
    ) -> None:
        """创建或接收可测试的 HTTP、Embedding 与 Rerank 客户端并记录所有权。"""
        self._settings = settings
        self._owns_model_client = model_client is None
        self._owns_vision_client = vision_client is None
        self._owns_media_client = media_client is None
        self._owns_media_vision_client = media_vision_client is None
        self._owns_ragflow_client = ragflow_client is None
        self._owns_embedding = embedding is None
        self._owns_reranker = reranker is None
        self._model_client = model_client or httpx.AsyncClient(
            base_url=settings.models.llm.base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {settings.models.llm.api_key}"},
            timeout=httpx.Timeout(
                connect=settings.models.llm.connect_timeout_seconds,
                read=settings.models.llm.read_timeout_seconds,
                write=settings.models.llm.read_timeout_seconds,
                pool=settings.models.llm.connect_timeout_seconds,
            ),
            trust_env=False,
        )
        self._vision_client = vision_client or httpx.AsyncClient(
            base_url=settings.models.vision.base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {settings.models.vision.api_key}"},
            timeout=httpx.Timeout(
                connect=settings.models.vision.connect_timeout_seconds,
                read=settings.models.vision.read_timeout_seconds,
                write=settings.models.vision.read_timeout_seconds,
                pool=settings.models.vision.connect_timeout_seconds,
            ),
            trust_env=False,
        )
        self._media_client = media_client or (
            httpx.AsyncClient(
                base_url=settings.models.media.asr_base_url.rstrip("/") + "/",
                headers={
                    "Authorization": f"Bearer {settings.models.media.asr_api_key}"
                },
                timeout=httpx.Timeout(
                    connect=settings.models.media.connect_timeout_seconds,
                    read=settings.models.media.read_timeout_seconds,
                    write=settings.models.media.read_timeout_seconds,
                    pool=settings.models.media.connect_timeout_seconds,
                ),
                trust_env=False,
            )
            if settings.models.media.enabled
            else None
        )
        media_vision = settings.models.media_vision
        self._media_vision_client = media_vision_client or (
            httpx.AsyncClient(
                base_url=media_vision.base_url.rstrip("/") + "/",
                headers={"Authorization": f"Bearer {media_vision.api_key}"},
                timeout=httpx.Timeout(
                    connect=media_vision.connect_timeout_seconds,
                    read=media_vision.read_timeout_seconds,
                    write=media_vision.read_timeout_seconds,
                    pool=media_vision.connect_timeout_seconds,
                ),
                trust_env=False,
            )
            if settings.models.media.enabled and media_vision is not None
            else None
        )
        self._ragflow_client = ragflow_client or httpx.AsyncClient(
            base_url=settings.ragflow.base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {settings.ragflow.api_key}"},
            timeout=httpx.Timeout(
                connect=settings.ragflow.connect_timeout_seconds,
                read=settings.ragflow.read_timeout_seconds,
                write=settings.ragflow.read_timeout_seconds,
                pool=settings.ragflow.connect_timeout_seconds,
            ),
            trust_env=False,
        )
        self._embedding = embedding or OpenAICompatibleEmbeddingAdapter(settings.models.embedding)
        self._reranker = reranker or (
            OpenAICompatibleRerankAdapter(settings.models.rerank)
            if settings.models.rerank.enabled
            else DisabledRerankAdapter()
        )

    async def close(self) -> None:
        """只关闭由本对象创建的客户端，不影响调用方注入的共享客户端。"""
        if self._owns_embedding and isinstance(self._embedding, OpenAICompatibleEmbeddingAdapter):
            await self._embedding.close()
        if self._owns_reranker and isinstance(self._reranker, OpenAICompatibleRerankAdapter):
            await self._reranker.close()
        if self._owns_model_client:
            await self._model_client.aclose()
        if self._owns_vision_client:
            await self._vision_client.aclose()
        if self._owns_media_client and self._media_client is not None:
            await self._media_client.aclose()
        if self._owns_media_vision_client and self._media_vision_client is not None:
            await self._media_vision_client.aclose()
        if self._owns_ragflow_client:
            await self._ragflow_client.aclose()

    async def check(self) -> RuntimePreflightResult:
        """验证真实模型调用、实际向量维度、RAGFlow 健康和 API Key 权限。

        OpenAI-compatible 服务并不都实现 ``GET /models``，因此这里不再把模型清单
        当作硬性契约。Embedding、Chat 和 Vision 都发送最小合成请求；只有真实调用
        和响应结构通过后才允许启动。RAGFlow API 还需能用 Key 读取租户模型配置。
        """
        # 先验证向量维度。维度不匹配会直接污染 RAGFlow 与语义缓存索引，因此应在
        # 产生任何其他模型费用前失败；探测文本是固定非业务 Fixture，不会持久化。
        vectors = await self._embedding.embed(["Synthetic startup embedding dimension probe."])
        if len(vectors) != 1:
            raise EmbeddingError.invalid_response()
        actual_dimension = len(vectors[0])
        expected_dimension = self._settings.models.embedding.dimension
        if actual_dimension != expected_dimension:
            raise EmbeddingError.dimension_mismatch(expected_dimension, actual_dimension)

        # 文本生成和 OCR 都必须完成一次真实调用。这样既兼容不提供模型清单的服务，
        # 也能发现“模型名存在但 Key 没有调用权限”的常见配置错误。
        await self._check_chat_completion()
        await self._check_vision_completion(
            self._vision_client,
            self._settings.models.vision,
        )

        # 音视频能力开启时，启动必须同时证明容器中有 FFmpeg/ffprobe，并用合成静音
        # WAV 真正调用一次 ASR。这样错误 Key、模型无权限或镜像漏装编解码器会在
        # Worker 领取用户文件前暴露，而不是让任务排队后才失败。
        media_asr_status = "disabled"
        media_vision_model: str | None = None
        if self._settings.models.media.enabled:
            await self._check_media_completion()
            media_vision = self._settings.models.media_vision
            if media_vision is None or self._media_vision_client is None:
                raise LlmError.unavailable()
            # 视频抽帧模型与文档 OCR 模型分别真实探测，避免只验证 RAGFlow 图片模型
            # 就把未授权或不支持图片输入的媒体模型发布为可用。
            await self._check_vision_completion(
                self._media_vision_client,
                media_vision,
            )
            media_asr_status = "enabled_validated"
            media_vision_model = media_vision.model

        # 配置开启时必须执行一次最小真实 Cross-Encoder 请求。在线 Adapter 会在单次
        # 故障时降级，但启动阶段不能把降级结果当作成功，否则能力接口会错误宣称可用。
        rerank_status = "disabled_vector_bm25_rrf_fallback"
        if self._settings.models.rerank.enabled:
            rerank_outcome = await self._reranker.rerank(
                "Which synthetic document is relevant?",
                (
                    "This synthetic document is relevant to the startup probe.",
                    "This unrelated fixture discusses a different topic.",
                ),
            )
            if (
                not rerank_outcome.enabled
                or rerank_outcome.warning is not None
                or len(rerank_outcome.scores) != 2
                or any(score is None for score in rerank_outcome.scores)
            ):
                raise RerankError.preflight_failed()
            rerank_status = "enabled_validated"

        try:
            health_response = await self._ragflow_client.get("api/v1/system/healthz")
            profile_response = await self._ragflow_client.get("api/v1/users/me")
            tenant_models_response = await self._ragflow_client.get(
                "api/v1/users/me/models"
            )
        except httpx.TimeoutException as exc:
            raise RagFlowError.timeout() from exc
        except httpx.RequestError as exc:
            raise RagFlowError.connection() from exc
        if not health_response.is_success:
            raise RagFlowError.downstream()
        health = self._json(health_response)
        if health.get("status") != "ok":
            raise RagFlowError.downstream()
        if profile_response.status_code == 401:
            raise RagFlowError.unauthorized()
        if not profile_response.is_success:
            raise RagFlowError.downstream()
        profile = self._json(profile_response)
        if profile.get("code") != 0 or not isinstance(profile.get("data"), dict):
            raise RagFlowError.invalid_response()
        if not tenant_models_response.is_success:
            raise RagFlowError.downstream()
        tenant_models = self._json(tenant_models_response)
        configured_models = tenant_models.get("data")
        if (
            tenant_models.get("code") != 0
            or not isinstance(configured_models, dict)
            or configured_models.get("embd_id")
            != self._settings.ragflow.embedding_model
            or configured_models.get("img2txt_id")
            != self._settings.ragflow.vision_model
        ):
            raise RagFlowError.invalid_response()

        return RuntimePreflightResult(
            model_api="ok",
            chat_model=self._settings.models.llm.model,
            embedding_model=self._settings.models.embedding.model,
            vision_model=self._settings.ragflow.vision_runtime_model,
            media_asr=media_asr_status,
            media_vision_model=media_vision_model,
            embedding_dimension=actual_dimension,
            ragflow="ok",
            ragflow_api_key="ok",
            rerank=rerank_status,
        )

    async def _check_media_completion(self) -> None:
        """使用一秒合成静音 WAV 验证工具安装、ASR 鉴权和响应协议。

        Fixture 完全由内存生成，不读取用户文件，也不要求模型必须识别出某段文字；
        只要服务返回标准 Chat Completions 文本字段，即可证明实际媒体协议可调用。
        """
        media = self._settings.models.media
        if shutil.which(media.ffmpeg_binary) is None or shutil.which(media.ffprobe_binary) is None:
            raise LlmError.unavailable()
        if self._media_client is None:
            raise LlmError.unavailable()

        audio = io.BytesIO()
        with wave.open(audio, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(b"\x00\x00" * 16000)
        encoded = base64.b64encode(audio.getvalue()).decode("ascii")
        try:
            response = await self._media_client.post(
                "chat/completions",
                json={
                    "model": media.asr_model,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "input_audio",
                                    "input_audio": {
                                        "data": f"data:audio/wav;base64,{encoded}",
                                        "format": "wav",
                                    },
                                }
                            ],
                        }
                    ],
                    "stream": False,
                    "asr_options": {"enable_itn": False},
                },
            )
        except httpx.TimeoutException as exc:
            raise LlmError.timeout() from exc
        except httpx.RequestError as exc:
            raise LlmError.unavailable() from exc
        if not response.is_success:
            raise LlmError.unavailable()
        value = self._json(response, model=True)
        choices = value.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise LlmError.invalid_response()
        message = choices[0].get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            raise LlmError.invalid_response()

    @staticmethod
    def _json(response: httpx.Response, *, model: bool = False) -> dict[str, Any]:
        """解析必需 JSON Object，并按调用方类型映射为稳定下游协议错误。"""
        try:
            value = response.json()
        except ValueError as exc:
            if model:
                raise LlmError.invalid_response() from exc
            raise RagFlowError.invalid_response() from exc
        if not isinstance(value, dict):
            if model:
                raise LlmError.invalid_response()
            raise RagFlowError.invalid_response()
        return value

    async def _check_chat_completion(self) -> None:
        """验证回答、结构化 Rewrite、鉴权和自动探测出的协议覆盖。

        请求体使用与正式 Adapter 相同的 Token 字段、省略采样参数规则和额外 JSON。
        回答模型必须返回非空文本；Rewrite 模型必须遵守 JSON Object 契约。两次调用
        都只使用固定合成文本且不写入日志或数据库，能够在服务接收用户请求前发现
        独立 Rewrite 模型名无权限、模型不存在或协议与回答模型不一致。
        """
        llm = self._settings.models.llm
        probes = (
            (
                llm.model,
                "Reply with a short acknowledgement for a startup probe.",
                False,
            ),
            (
                llm.rewrite_model,
                'Return exactly this JSON object: {"status":"ok"}',
                True,
            ),
        )
        for model, prompt, structured in probes:
            payload: dict[str, Any] = {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                llm.max_output_tokens_field: 64 if structured else 16,
                "stream": False,
            }
            if not llm.omit_sampling_parameters:
                payload.update({"temperature": 0.0, "top_p": 1.0})
            if structured:
                payload["response_format"] = {"type": "json_object"}
            payload.update(llm.extra_body)
            try:
                response = await self._model_client.post(
                    "chat/completions", json=payload
                )
            except httpx.TimeoutException as exc:
                raise LlmError.timeout() from exc
            except httpx.RequestError as exc:
                raise LlmError.unavailable() from exc
            if not response.is_success:
                raise LlmError.unavailable()
            value = self._json(response, model=True)
            choices = value.get("choices")
            if (
                not isinstance(choices, list)
                or not choices
                or not isinstance(choices[0], dict)
            ):
                raise LlmError.invalid_response()
            message = choices[0].get("message")
            if (
                not isinstance(message, dict)
                or not isinstance(message.get("content"), str)
                or not message["content"].strip()
            ):
                raise LlmError.invalid_response()
            if structured:
                try:
                    parsed = json.loads(message["content"])
                except json.JSONDecodeError as exc:
                    raise LlmError.invalid_response() from exc
                if not isinstance(parsed, dict):
                    raise LlmError.invalid_response()

    async def _check_vision_completion(
        self,
        client: httpx.AsyncClient,
        vision: VisionSection,
    ) -> None:
        """真实调用一项视觉职责，并验证其能识别固定合成文字。

        文档 OCR 由 RAGFlow 调用，媒体视觉由 Worker 调用；两者都只消费内存中的固定
        图片，不生成 Chunk、不写数据库。通用文字识别是两类模型共同的最低图片输入
        契约，可以在启动前发现错误 Key、无模型权限、错误 URL 或纯文本模型误配置。
        """
        # 这是由仓库 Bootstrap 用 5×7 点阵生成的固定 ``LAZYTT 2026`` PNG。
        # 把生成结果固化在运行时，既不依赖 Pillow，也不会携带用户文件或业务文本。
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAigAAABYCAAAAAD621OxAAABX0lEQVR42u3cMQ7DIAxA"
            "Ue5/6XbJioqNnYL0/kxQAy8LSjo+0kLDEggUgSJQBIpAESgSKAJFoAgUgSJQBIoEipJQ"
            "xlN4okmr42bX/fo9s/HR+VfHpxc6OG/3+Nl1oIACCiiggAIKKKCAAgoooIACSh+U6EZG"
            "59udv/p+skC67jd7/+F1AQUUUEABBRRQQAEFFFBAAQUUUI6BUr1Ap0CpejCyB4DZdQMF"
            "FFBAAQUUUEABBRRQQAEFFFD6oGRf/OneyFOhrB6EVT8g6X0CBRRQQAEFFFBAAQUUUEAB"
            "BRRQ/nbgtvuh2a1Qoh9yVR9wpvcLFFBAAQUUUEABBRRQQAEFFFBAeR3K7kbcCqXrBa7X"
            "5gcFFFBAAQUUUEABBRRQQAEFFFCO/8Pg26Fk73d3XNX8oIACCiiggAIKKKCAAgoooIAC"
            "yhyKBIpAESgCRaAIFIEigSJQBIpAESgCRaBIoGijLyMlrccSQlJoAAAAAElFTkSuQmCC"
        )
        try:
            response = await client.post(
                "chat/completions",
                json={
                    "model": vision.model,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image_url",
                                    "image_url": {
                                        "url": "data:image/png;base64,"
                                        + base64.b64encode(png).decode("ascii")
                                    },
                                },
                                {
                                    "type": "text",
                                    "text": (
                                        "Extract the exact text from this image. "
                                        "Return text only."
                                    ),
                                },
                            ],
                        }
                    ],
                    "stream": False,
                    "max_tokens": 64,
                },
            )
        except httpx.TimeoutException as exc:
            raise LlmError.timeout() from exc
        except httpx.RequestError as exc:
            raise LlmError.unavailable() from exc
        if not response.is_success:
            raise LlmError.unavailable()
        value = self._json(response, model=True)
        choices = value.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise LlmError.invalid_response()
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise LlmError.invalid_response()
        content = str(message.get("content", "")).strip().upper()
        if "LAZYTT" not in content or "2026" not in content:
            raise LlmError.invalid_response()
