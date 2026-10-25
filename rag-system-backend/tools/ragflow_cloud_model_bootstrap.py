"""探测通用模型端点，并通过 RAGFlow 官方 API 幂等注册模型。

用户只提供每种能力的 ``BASE_URL``、``API_KEY`` 和 ``MODEL``。本工具不会按供应商
名称分支，也不会访问 RAGFlow 数据库；它使用最小非业务 Fixture 完成以下工作：

1. 对 Embedding 执行单条和批量请求，读取并验证真实维度；
2. 对最终回答 LLM 执行普通回答与 JSON Object 请求，有限探测协议差异；
3. 对 Vision 模型执行合成图片 OCR 请求；
4. 通过 RAGFlow v0.26.4 的公开 Provider API 注册 Embedding 与 Vision；
5. 原子写回非敏感探测结果，供 Gateway、Worker 和后续启动预检使用。

Rerank 的 ``/rerank`` 和 ASR 的 ``input_audio`` 不是 OpenAI 官方统一标准，仍由运行时
预检在对应能力开启时验证。真实 API Key 只从环境读取，绝不会出现在证据或日志中。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import struct
import tempfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

_RAGFLOW_GENERIC_PROVIDER = "OpenAI-API-Compatible"


class BootstrapFailure(RuntimeError):
    """表示模型协议、鉴权、维度或 RAGFlow 持久化验证失败。"""


@dataclass(frozen=True, slots=True)
class ChatCompatibility:
    """一次成功的 Chat Completions 协议组合。

    标准端点使用 ``max_tokens`` 并保留采样参数。少数兼容端点要求
    ``max_completion_tokens``、省略 temperature/top_p，或显式关闭思考模式；这些
    非敏感差异在 Bootstrap 中有限探测，避免把厂商名称写入运行代码。
    """

    omit_sampling_parameters: bool = False
    max_output_tokens_field: str = "max_tokens"
    extra_body: dict[str, Any] | None = None

    def normalized_extra_body(self) -> dict[str, Any]:
        """返回独立字典，避免调用方意外修改冻结契约中的共享对象。"""
        return dict(self.extra_body or {})


@dataclass(frozen=True, slots=True)
class CloudModelContract:
    """一次 Bootstrap 使用的通用模型契约；任何密钥都不得被序列化输出。"""

    ragflow_base_url: str
    ragflow_api_key: str
    embedding_base_url: str
    embedding_api_key: str
    embedding_model: str
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    rewrite_model: str
    vision_base_url: str
    vision_api_key: str
    vision_model: str
    provider: str = _RAGFLOW_GENERIC_PROVIDER

    @property
    def embedding_instance(self) -> str:
        """按能力、URL 和模型生成稳定实例名，模型切换时自然创建新版本。"""
        return _instance_name("embedding", self.embedding_base_url, self.embedding_model)

    @property
    def vision_instance(self) -> str:
        """生成与 Embedding 相互独立的稳定 Vision 实例名。"""
        return _instance_name("vision", self.vision_base_url, self.vision_model)

    @property
    def embedding_ref(self) -> str:
        """返回 RAGFlow 所需的 ``模型@实例@Provider`` 完整引用。"""
        return f"{self.embedding_model}@{self.embedding_instance}@{self.provider}"

    @property
    def vision_ref(self) -> str:
        """返回 RAGFlow 所需的 ``模型@实例@Provider`` 完整引用。"""
        return f"{self.vision_model}@{self.vision_instance}@{self.provider}"


def _required_env(name: str) -> str:
    """读取非空且不是示例占位符的环境变量，错误只报告字段名。"""
    value = os.getenv(name, "").strip()
    if not value or value.startswith(("change-me", "pending-")):
        raise BootstrapFailure(f"required environment variable is missing: {name}")
    return value


def _contract() -> CloudModelContract:
    """从环境构造通用职责，不检查供应商域名、Key 前缀或特定模型名称。"""
    return CloudModelContract(
        ragflow_base_url=os.getenv("RAGFLOW_BASE_URL", "http://127.0.0.1:9380").strip(),
        ragflow_api_key=_required_env("RAGFLOW_API_KEY"),
        embedding_base_url=_required_env("EMBEDDING_BASE_URL"),
        embedding_api_key=_required_env("EMBEDDING_API_KEY"),
        embedding_model=_required_env("EMBEDDING_MODEL"),
        llm_base_url=_required_env("LLM_BASE_URL"),
        llm_api_key=_required_env("LLM_API_KEY"),
        llm_model=_required_env("LLM_MODEL"),
        rewrite_model=os.getenv("REWRITE_MODEL", "").strip() or _required_env("LLM_MODEL"),
        vision_base_url=_required_env("VISION_BASE_URL"),
        vision_api_key=_required_env("VISION_API_KEY"),
        vision_model=_required_env("VISION_MODEL"),
    )


def _instance_name(role: str, base_url: str, model: str) -> str:
    """生成不含密钥的短实例名，避免同名模型在不同端点互相覆盖。

    可读部分来自模型名，指纹只包含 Base URL 与模型名，API Key 及其摘要都不会进入
    实例名、日志或非敏感证据。固定镜像 v0.26.4 不提供 Provider Instance 更新接口，
    因此重复启动只验证当前配置，不声称能够原地轮换 RAGFlow 已保存的实例凭据。
    """
    slug = re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")[-32:] or "model"
    fingerprint = hashlib.sha256(
        f"{base_url.rstrip('/')}\n{model}".encode()
    ).hexdigest()[:10]
    return f"lazytt-{role}-{slug}-{fingerprint}"


def _json(response: httpx.Response, operation: str) -> dict[str, Any]:
    """要求 HTTP 成功且响应为 JSON Object；不回显请求头或请求体。"""
    try:
        value = response.json()
    except ValueError as exc:
        raise BootstrapFailure(
            f"{operation}: HTTP {response.status_code} returned invalid JSON"
        ) from exc
    if not response.is_success:
        message = value.get("message") if isinstance(value, dict) else None
        raise BootstrapFailure(
            f"{operation}: HTTP {response.status_code}"
            + (f" ({message})" if isinstance(message, str) else "")
        )
    if not isinstance(value, dict):
        raise BootstrapFailure(f"{operation}: response is not a JSON object")
    return value


def _ragflow_data(response: httpx.Response, operation: str) -> Any:
    """校验 RAGFlow ``code/data`` Envelope，并只返回 data。"""
    value = _json(response, operation)
    if value.get("code") != 0:
        message = value.get("message")
        raise BootstrapFailure(
            f"{operation}: RAGFlow business failure"
            + (f" ({message})" if isinstance(message, str) else "")
        )
    return value.get("data")


def _assistant_content(value: dict[str, Any], operation: str) -> str:
    """从首个 choice 提取非空 assistant 文本，并拒绝宽松类型转换。"""
    choices = value.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise BootstrapFailure(f"{operation}: choices are missing")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise BootstrapFailure(f"{operation}: assistant content is missing")
    content_value = message.get("content")
    if not isinstance(content_value, str):
        raise BootstrapFailure(f"{operation}: assistant content is missing")
    content = content_value.strip()
    if not content:
        raise BootstrapFailure(f"{operation}: assistant content is empty")
    return content


def _vector_dimensions(value: dict[str, Any], count: int, operation: str) -> list[int]:
    """验证向量条数、元素结构和正维度，并按响应 index 恢复输入顺序。"""
    data = value.get("data")
    if not isinstance(data, list) or len(data) != count:
        raise BootstrapFailure(f"{operation}: expected {count} embedding vectors")
    ordered: list[tuple[int, int]] = []
    for position, item in enumerate(data):
        if not isinstance(item, dict) or not isinstance(item.get("embedding"), list):
            raise BootstrapFailure(f"{operation}: vector payload is invalid")
        dimension = len(item["embedding"])
        if dimension <= 0:
            raise BootstrapFailure(f"{operation}: vector dimension is empty")
        index = item.get("index", position)
        if not isinstance(index, int):
            raise BootstrapFailure(f"{operation}: vector index is invalid")
        ordered.append((index, dimension))
    return [dimension for _, dimension in sorted(ordered)]


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    """组装 PNG 数据块并计算协议要求的 CRC32。"""
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _synthetic_ocr_png_data_url() -> str:
    """用标准库生成写有 ``LAZYTT 2026`` 的非业务 OCR Fixture。"""
    glyphs = {
        "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
        "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
        "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
        "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
        "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
        "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
        "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
        "6": ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
        " ": ("00000",) * 7,
    }
    text = "LAZYTT 2026"
    scale = 8
    margin = 16
    width = margin * 2 + (len(text) * 6 - 1) * scale
    height = margin * 2 + 7 * scale
    pixels = [[255] * width for _ in range(height)]
    for character_index, character in enumerate(text):
        for row_index, row in enumerate(glyphs[character]):
            for column_index, bit in enumerate(row):
                if bit != "1":
                    continue
                left = margin + (character_index * 6 + column_index) * scale
                top = margin + row_index * scale
                for y in range(top, top + scale):
                    pixels[y][left : left + scale] = [0] * scale
    scanlines = b"".join(b"\x00" + bytes(row) for row in pixels)
    png = b"\x89PNG\r\n\x1a\n"
    png += _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
    png += _png_chunk(b"IDAT", zlib.compress(scanlines, level=9))
    png += _png_chunk(b"IEND", b"")
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def _chat_candidates() -> tuple[ChatCompatibility, ...]:
    """返回从标准到常见兼容差异的有限探测顺序。

    这里不建立厂商注册表，也不会无限重试。第一个标准组合覆盖绝大多数端点；后续
    组合只处理已知的协议类别，全部失败时明确阻止启动，避免静默降级答案能力。
    """
    return (
        ChatCompatibility(),
        ChatCompatibility(extra_body={"enable_thinking": False}),
        ChatCompatibility(max_output_tokens_field="max_completion_tokens"),
        ChatCompatibility(
            max_output_tokens_field="max_completion_tokens",
            extra_body={"enable_thinking": False},
        ),
        ChatCompatibility(omit_sampling_parameters=True),
        ChatCompatibility(
            omit_sampling_parameters=True,
            extra_body={"enable_thinking": False},
        ),
        ChatCompatibility(
            omit_sampling_parameters=True,
            max_output_tokens_field="max_completion_tokens",
        ),
        ChatCompatibility(
            omit_sampling_parameters=True,
            max_output_tokens_field="max_completion_tokens",
            extra_body={"enable_thinking": False},
        ),
    )


def _chat_payload(
    contract: CloudModelContract,
    compatibility: ChatCompatibility,
    *,
    model: str,
    structured: bool,
) -> dict[str, Any]:
    """为探测生成与运行时适配器等价的最小 Chat Completions 请求。"""
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": (
                    'Return exactly this JSON object: {"status":"ok"}'
                    if structured
                    else "Reply with exactly OK."
                ),
            }
        ],
        compatibility.max_output_tokens_field: 64,
        "stream": False,
    }
    if not compatibility.omit_sampling_parameters:
        payload.update({"temperature": 0, "top_p": 1})
    if structured:
        payload["response_format"] = {"type": "json_object"}
    payload.update(compatibility.normalized_extra_body())
    return payload


def _probe_chat(
    client: httpx.Client,
    contract: CloudModelContract,
) -> ChatCompatibility:
    """寻找同时支持普通文本和 JSON Object 的最小协议组合。"""
    last_error: BootstrapFailure | None = None
    for compatibility in _chat_candidates():
        try:
            ordinary = _json(
                client.post(
                    "chat/completions",
                    json=_chat_payload(
                        contract,
                        compatibility,
                        model=contract.llm_model,
                        structured=False,
                    ),
                ),
                "llm_chat",
            )
            _assistant_content(ordinary, "llm_chat")
            structured = _json(
                client.post(
                    "chat/completions",
                    json=_chat_payload(
                        contract,
                        compatibility,
                        model=contract.llm_model,
                        structured=True,
                    ),
                ),
                "llm_json",
            )
            try:
                parsed = json.loads(_assistant_content(structured, "llm_json"))
            except json.JSONDecodeError as exc:
                raise BootstrapFailure("llm_json: assistant content is not valid JSON") from exc
            if not isinstance(parsed, dict):
                raise BootstrapFailure("llm_json: assistant JSON is not an object")
            return compatibility
        except BootstrapFailure as exc:
            last_error = exc
    raise BootstrapFailure(f"no supported chat compatibility profile: {last_error}")


def validate_cloud_models(
    contract: CloudModelContract,
    evidence: dict[str, Any],
    *,
    embedding_client: httpx.Client | None = None,
    llm_client: httpx.Client | None = None,
    vision_client: httpx.Client | None = None,
) -> tuple[int, ChatCompatibility]:
    """真实验证通用 Embedding、LLM 和 Vision，并返回探测结果。"""
    timeout = httpx.Timeout(connect=10, read=180, write=60, pool=10)
    owns_embedding = embedding_client is None
    embedding_client = embedding_client or httpx.Client(
        base_url=contract.embedding_base_url.rstrip("/") + "/",
        headers={"Authorization": f"Bearer {contract.embedding_api_key}"},
        timeout=timeout,
        trust_env=False,
    )
    try:
        single = _json(
            embedding_client.post(
                "embeddings",
                json={"model": contract.embedding_model, "input": "Synthetic dimension probe."},
            ),
            "embedding_single",
        )
        batch = _json(
            embedding_client.post(
                "embeddings",
                json={
                    "model": contract.embedding_model,
                    "input": ["Synthetic probe alpha.", "Synthetic probe beta."],
                },
            ),
            "embedding_batch",
        )
        dimensions = _vector_dimensions(single, 1, "embedding_single")
        dimensions.extend(_vector_dimensions(batch, 2, "embedding_batch"))
        if len(set(dimensions)) != 1:
            raise BootstrapFailure(f"embedding dimension is inconsistent: {dimensions}")
        dimension = dimensions[0]
        configured_dimension = os.getenv("EMBEDDING_DIMENSION", "").strip()
        if configured_dimension.isdigit() and int(configured_dimension) != dimension:
            raise BootstrapFailure(
                "embedding dimension mismatch: "
                f"configured={configured_dimension}, actual={dimension}"
            )
        evidence["steps"].append(
            {
                "operation": "embedding",
                "result": "ok",
                "model": contract.embedding_model,
                "dimension": dimension,
                "single_and_batch": True,
            }
        )
    finally:
        if owns_embedding:
            embedding_client.close()

    owns_llm = llm_client is None
    llm_client = llm_client or httpx.Client(
        base_url=contract.llm_base_url.rstrip("/") + "/",
        headers={"Authorization": f"Bearer {contract.llm_api_key}"},
        timeout=timeout,
        trust_env=False,
    )
    try:
        compatibility = _probe_chat(llm_client, contract)
        # Rewrite 可以显式选择同一端点上的另一个模型，但运行时只有一组协议覆盖。
        # 因此额外模型必须用刚探测出的同一请求形态完成 JSON Object 调用；若它需要
        # 另一套不兼容参数，启动会明确失败，而不是让第一条中文查询在在线阶段报错。
        if contract.rewrite_model != contract.llm_model:
            rewrite = _json(
                llm_client.post(
                    "chat/completions",
                    json=_chat_payload(
                        contract,
                        compatibility,
                        model=contract.rewrite_model,
                        structured=True,
                    ),
                ),
                "rewrite_json",
            )
            try:
                rewrite_value = json.loads(_assistant_content(rewrite, "rewrite_json"))
            except json.JSONDecodeError as exc:
                raise BootstrapFailure(
                    "rewrite_json: assistant content is not valid JSON"
                ) from exc
            if not isinstance(rewrite_value, dict):
                raise BootstrapFailure("rewrite_json: assistant JSON is not an object")
        evidence["steps"].append(
            {
                "operation": "llm_chat",
                "result": "ok",
                "model": contract.llm_model,
                "rewrite_model": contract.rewrite_model,
                "json_object": True,
                "compatibility_detected": True,
            }
        )
    finally:
        if owns_llm:
            llm_client.close()

    owns_vision = vision_client is None
    vision_client = vision_client or httpx.Client(
        base_url=contract.vision_base_url.rstrip("/") + "/",
        headers={"Authorization": f"Bearer {contract.vision_api_key}"},
        timeout=timeout,
        trust_env=False,
    )
    try:
        vision = _json(
            vision_client.post(
                "chat/completions",
                json={
                    "model": contract.vision_model,
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "image_url",
                                    "image_url": {"url": _synthetic_ocr_png_data_url()},
                                },
                                {
                                    "type": "text",
                                    "text": "Extract the exact text. Return text only.",
                                },
                            ],
                        }
                    ],
                    "max_tokens": 64,
                    "stream": False,
                },
            ),
            "vision_ocr",
        )
        content = _assistant_content(vision, "vision_ocr").upper()
        if "LAZYTT" not in content or "2026" not in content:
            raise BootstrapFailure("vision_ocr: fixture text was not recognized")
        evidence["steps"].append(
            {
                "operation": "vision_ocr",
                "result": "ok",
                "model": contract.vision_model,
                "fixture_text_recognized": True,
            }
        )
    finally:
        if owns_vision:
            vision_client.close()
    return dimension, compatibility


def _ensure_provider(client: httpx.Client, provider: str) -> None:
    """确保租户启用了 RAGFlow 内置通用 Provider。"""
    providers = _ragflow_data(client.get("/api/v1/providers"), "list_providers")
    if not isinstance(providers, list):
        raise BootstrapFailure("list_providers: data is not a list")
    names = {
        item.get("name")
        for item in providers
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    if provider not in names:
        _ragflow_data(
            client.put("/api/v1/providers", json={"provider_name": provider}),
            f"add_provider_{provider}",
        )


def _ensure_instance_model(
    client: httpx.Client,
    *,
    provider: str,
    instance: str,
    api_key: str,
    base_url: str,
    model: str,
    model_type: str,
    max_tokens: int,
    extra: dict[str, Any],
    verification_model_type: str | None = None,
) -> None:
    """幂等创建并验证 Bootstrap 管理的 Provider Instance。

    Base URL 或模型名变化时，稳定指纹会生成新实例，保护仍引用旧实例的 Dataset；
    实例已存在时使用官方 connection 接口重新验证当前配置。v0.26.4 的验证器没有
    ``image2text`` 分支，所以视觉模型先用其 OpenAI-compatible ``chat`` 能力完成
    实例验证，再通过官方模型类型接口切换为 Picture Parser 所需能力；不删除实例、
    不直接修改数据库，也不影响用户在管理界面创建的其他实例。
    """
    _ensure_provider(client, provider)
    instances = _ragflow_data(
        client.get(f"/api/v1/providers/{provider}/instances"),
        f"list_instances_{provider}",
    )
    if not isinstance(instances, list):
        raise BootstrapFailure(f"list_instances_{provider}: data is not a list")
    names = {
        item.get("instance_name")
        for item in instances
        if isinstance(item, dict) and isinstance(item.get("instance_name"), str)
    }
    desired_model_info = {
        "model_name": model,
        "model_type": [model_type],
        "max_tokens": max_tokens,
        "extra": extra,
    }
    verification_model_info = {
        **desired_model_info,
        "model_type": [verification_model_type or model_type],
    }
    instance_payload = {
        "instance_name": instance,
        "api_key": api_key,
        "base_url": base_url.rstrip("/"),
        "region": "cloud",
        "model_info": [verification_model_info],
    }
    if instance not in names:
        _ragflow_data(
            client.post(
                f"/api/v1/providers/{provider}/instances",
                json=instance_payload,
                timeout=180,
            ),
            f"create_instance_{instance}",
        )
    else:
        # 官方 v0.26.4 镜像没有 ``PUT /instances/{name}`` 更新路由。删除再创建会破坏
        # 已有 Dataset 对模型实例的引用，所以重复启动使用官方 connection 接口重新
        # 验证当前 Base URL、Key、模型和能力类型，绝不删除实例或直接改数据库。
        _ragflow_data(
            client.post(
                f"/api/v1/providers/{provider}/connection",
                json={
                    "api_key": api_key,
                    "base_url": base_url.rstrip("/"),
                    "region": "cloud",
                    "model_info": [verification_model_info],
                },
                timeout=180,
            ),
            f"verify_existing_instance_{instance}",
        )
    models = _ragflow_data(
        client.get(f"/api/v1/providers/{provider}/instances/{instance}/models"),
        f"list_models_{instance}",
    )
    if not isinstance(models, list):
        raise BootstrapFailure(f"list_models_{instance}: data is not a list")
    matching = [item for item in models if isinstance(item, dict) and item.get("name") == model]
    if not matching:
        _ragflow_data(
            client.post(
                f"/api/v1/providers/{provider}/instances/{instance}/models",
                json=desired_model_info,
                timeout=180,
            ),
            f"add_model_{model_type}_{instance}",
        )
    elif model_type not in set(matching[0].get("model_type") or []):
        # 两阶段视觉注册会先留下同名 chat 记录。v0.26.4 提供模型类型 PUT 路由，
        # 可以安全地把 Bootstrap 管理的这一个模型切换为 image2text，而不重建实例。
        _ragflow_data(
            client.put(
                f"/api/v1/providers/{provider}/instances/{instance}/models",
                json={"model_name": [model], "model_type": [model_type]},
                timeout=180,
            ),
            f"set_model_type_{model_type}_{instance}",
        )


def ensure_ragflow_models(
    contract: CloudModelContract,
    dimension: int,
    evidence: dict[str, Any],
    *,
    ragflow_client: httpx.Client | None = None,
) -> None:
    """注册通用 Embedding/Vision、设置租户默认值并回读验证。

    固定镜像 v0.26.4 使用 ``image2text`` 表示可接收图片的模型能力，但 Provider
    实例验证器遗漏了这一分支。视觉模型因此先用 ``chat`` 完成实例验证，再通过官方
    API 切换为 ``image2text``；租户默认模型继续写入该版本的 ``img2txt_id`` 字段。
    """
    owns_client = ragflow_client is None
    ragflow_client = ragflow_client or httpx.Client(
        base_url=contract.ragflow_base_url.rstrip("/"),
        headers={"Authorization": f"Bearer {contract.ragflow_api_key}"},
        timeout=httpx.Timeout(connect=5, read=180, write=60, pool=5),
        trust_env=False,
    )
    try:
        client = ragflow_client
        health = _json(client.get("/api/v1/system/healthz"), "ragflow_health")
        if health.get("status") != "ok":
            raise BootstrapFailure("ragflow_health: service is not healthy")
        profile = _ragflow_data(client.get("/api/v1/users/me"), "ragflow_api_key")
        if not isinstance(profile, dict) or not profile.get("id"):
            raise BootstrapFailure("ragflow_api_key: authenticated user is missing")
        _ensure_instance_model(
            client,
            provider=contract.provider,
            instance=contract.embedding_instance,
            api_key=contract.embedding_api_key,
            base_url=contract.embedding_base_url,
            model=contract.embedding_model,
            model_type="embedding",
            max_tokens=8192,
            extra={"expected_dimension": dimension, "managed_by": "lazytt-rag-bootstrap"},
        )
        _ensure_instance_model(
            client,
            provider=contract.provider,
            instance=contract.vision_instance,
            api_key=contract.vision_api_key,
            base_url=contract.vision_base_url,
            model=contract.vision_model,
            model_type="image2text",
            max_tokens=32768,
            extra={"managed_by": "lazytt-rag-bootstrap", "capability": "ocr"},
            verification_model_type="chat",
        )
        tenant = _ragflow_data(client.get("/api/v1/users/me/models"), "get_tenant_models")
        if not isinstance(tenant, dict) or not tenant.get("tenant_id"):
            raise BootstrapFailure("get_tenant_models: tenant information is missing")
        if (
            tenant.get("embd_id") != contract.embedding_ref
            or tenant.get("img2txt_id") != contract.vision_ref
        ):
            _ragflow_data(
                client.patch(
                    "/api/v1/users/me/models",
                    json={
                        "tenant_id": tenant["tenant_id"],
                        "asr_id": tenant.get("asr_id") or "",
                        "embd_id": contract.embedding_ref,
                        "img2txt_id": contract.vision_ref,
                        "llm_id": tenant.get("llm_id") or "",
                    },
                ),
                "set_default_models",
            )
        persisted = _ragflow_data(
            client.get("/api/v1/users/me/models"), "verify_default_models"
        )
        if not isinstance(persisted, dict) or (
            persisted.get("embd_id") != contract.embedding_ref
            or persisted.get("img2txt_id") != contract.vision_ref
        ):
            raise BootstrapFailure("RAGFlow did not persist the model defaults")
    finally:
        if owns_client:
            ragflow_client.close()
    evidence["steps"].extend(
        [
            {
                "operation": "ragflow_embedding",
                "result": "ok",
                "model": contract.embedding_ref,
                "dimension": dimension,
            },
            {
                "operation": "ragflow_vision",
                "result": "ok",
                "model": contract.vision_ref,
                "default_image2text": True,
            },
        ]
    )


def _atomic_update_env(path: Path, replacements: dict[str, str]) -> None:
    """只更新非敏感探测字段，并通过同目录临时文件原子替换。

    函数保留注释、未知字段和原始顺序；新增字段追加到末尾。文件权限固定为 ``0600``，
    避免私有环境在更新非敏感字段时意外放宽 API Key 的读取权限。
    """
    if not path.is_file():
        raise BootstrapFailure(f"model environment file is missing: {path}")
    lines = path.read_text(encoding="utf-8").splitlines()
    remaining = dict(replacements)
    rendered: list[str] = []
    for line in lines:
        if line and not line.lstrip().startswith("#") and "=" in line:
            name = line.split("=", 1)[0].strip()
            if name in remaining:
                rendered.append(f"{name}={remaining.pop(name)}")
                continue
        rendered.append(line)
    if remaining and rendered and rendered[-1]:
        rendered.append("")
    rendered.extend(f"{name}={value}" for name, value in remaining.items())
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.model-bootstrap.",
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write("\n".join(rendered) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _persist_results(
    paths: tuple[Path, ...],
    contract: CloudModelContract,
    dimension: int,
    compatibility: ChatCompatibility,
) -> None:
    """把同一组已验证结果写入源环境和当前运行环境。"""
    replacements = {
        "EMBEDDING_DIMENSION": str(dimension),
        "RAGFLOW_EMBEDDING_MODEL": contract.embedding_ref,
        "RAGFLOW_VISION_RUNTIME_MODEL": contract.vision_model,
        "RAGFLOW_VISION_MODEL": contract.vision_ref,
        "LLM_OMIT_SAMPLING_PARAMETERS": str(
            compatibility.omit_sampling_parameters
        ).lower(),
        "LLM_MAX_OUTPUT_TOKENS_FIELD": compatibility.max_output_tokens_field,
        "LLM_EXTRA_BODY_JSON": json.dumps(
            compatibility.normalized_extra_body(), separators=(",", ":")
        ),
    }
    seen: set[Path] = set()
    for path in paths:
        resolved = path.resolve()
        if resolved not in seen:
            _atomic_update_env(resolved, replacements)
            seen.add(resolved)


def run(*, source_env: Path | None = None, runtime_env: Path | None = None) -> dict[str, Any]:
    """按真实探测、RAGFlow 注册、持久化顺序执行并返回脱敏证据。"""
    contract = _contract()
    evidence: dict[str, Any] = {"steps": []}
    dimension, compatibility = validate_cloud_models(contract, evidence)
    ensure_ragflow_models(contract, dimension, evidence)
    paths = tuple(path for path in (source_env, runtime_env) if path is not None)
    if paths:
        _persist_results(paths, contract, dimension, compatibility)
    evidence.update(
        {
            "embedding_model_ref": contract.embedding_ref,
            "embedding_dimension": dimension,
            "vision_model_ref": contract.vision_ref,
            "llm_model": contract.llm_model,
            "configuration_persisted": bool(paths),
            "result": "ok",
        }
    )
    return evidence


def build_parser() -> argparse.ArgumentParser:
    """定义需要写回的 Git 忽略环境文件路径。"""
    parser = argparse.ArgumentParser(
        description="Probe generic model endpoints and register RAGFlow models."
    )
    parser.add_argument("--source-env", type=Path)
    parser.add_argument("--runtime-env", type=Path)
    return parser


def main() -> None:
    """执行 Bootstrap；失败时只输出不含密钥的单行原因。"""
    args = build_parser().parse_args()
    try:
        print(
            json.dumps(
                run(source_env=args.source_env, runtime_env=args.runtime_env),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
    except (BootstrapFailure, httpx.HTTPError, ValueError) as exc:
        raise SystemExit(f"RAGFlow model bootstrap failed: {exc}") from exc


if __name__ == "__main__":
    main()
