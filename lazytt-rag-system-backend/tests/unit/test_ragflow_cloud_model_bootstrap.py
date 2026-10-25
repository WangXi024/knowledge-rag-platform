"""通用模型 Bootstrap 的协议、RAGFlow 注册和安全写回测试。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import httpx

# 运维工具不安装进 Gateway 包；测试按真实路径加载，保持生产镜像边界不变。
_SCRIPT = Path(__file__).resolve().parents[2] / "tools" / "ragflow_cloud_model_bootstrap.py"
_SPEC = importlib.util.spec_from_file_location("ragflow_cloud_model_bootstrap", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
CloudModelContract = _MODULE.CloudModelContract
ensure_ragflow_models = _MODULE.ensure_ragflow_models
validate_cloud_models = _MODULE.validate_cloud_models


def _contract(
    *,
    embedding_api_key: str = "fixture-embedding-key",
    rewrite_model: str = "example/chat-model",
) -> CloudModelContract:
    """构造只含合成凭据、任意供应商 URL 和任意模型名的通用契约。

    ``embedding_api_key`` 可替换，用于证明只轮换密钥时会更新同一个 RAGFlow 实例；
    ``rewrite_model`` 可替换，用于证明独立 Rewrite 模型也会接受真实 JSON 协议探测。
    """
    return CloudModelContract(
        ragflow_base_url="http://ragflow.test",
        ragflow_api_key="fixture-ragflow-key",
        embedding_base_url="https://embedding.example.test/v1",
        embedding_api_key=embedding_api_key,
        embedding_model="example/embed-model",
        llm_base_url="https://chat.example.test/v1",
        llm_api_key="fixture-chat-key",
        llm_model="example/chat-model",
        rewrite_model=rewrite_model,
        vision_base_url="https://vision.example.test/v1",
        vision_api_key="fixture-vision-key",
        vision_model="example/vision-model",
    )


def test_generic_probe_detects_dimension_chat_compatibility_and_vision() -> None:
    """探测不得依赖厂商名，并应返回真实维度及成功的协议组合。"""
    llm_requests: list[dict[str, Any]] = []

    def embedding_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": index, "embedding": [0.0] * 768}
                    for index, _ in enumerate(inputs)
                ]
            },
        )

    def llm_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        llm_requests.append(body)
        # 模拟一个要求显式关闭思考的兼容端点，验证探测不是厂商硬编码。
        if body.get("enable_thinking") is not False:
            return httpx.Response(400, json={"message": "thinking must be disabled"})
        content = (
            '{"status":"ok"}'
            if body.get("response_format") == {"type": "json_object"}
            else "OK"
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    def vision_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "example/vision-model"
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "LAZYTT 2026"}}]},
        )

    embedding_client = httpx.Client(
        base_url="https://embedding.example.test/v1/",
        transport=httpx.MockTransport(embedding_handler),
    )
    llm_client = httpx.Client(
        base_url="https://chat.example.test/v1/",
        transport=httpx.MockTransport(llm_handler),
    )
    vision_client = httpx.Client(
        base_url="https://vision.example.test/v1/",
        transport=httpx.MockTransport(vision_handler),
    )
    evidence: dict[str, Any] = {"steps": []}

    dimension, compatibility = validate_cloud_models(
        _contract(rewrite_model="example/rewrite-model"),
        evidence,
        embedding_client=embedding_client,
        llm_client=llm_client,
        vision_client=vision_client,
    )

    assert dimension == 768
    assert compatibility.extra_body == {"enable_thinking": False}
    assert [item["operation"] for item in evidence["steps"]] == [
        "embedding",
        "llm_chat",
        "vision_ocr",
    ]
    # 标准组合失败一次；第二个组合完成回答文本、JSON 和独立 Rewrite 模型 JSON。
    assert len(llm_requests) == 4
    assert llm_requests[-1]["model"] == "example/rewrite-model"
    embedding_client.close()
    llm_client.close()
    vision_client.close()


def test_ragflow_bootstrap_uses_generic_provider_and_sets_defaults() -> None:
    """公开 Provider API 应注册模型、回读默认值并幂等验证已有实例。

    固定镜像的实例验证器不支持 ``image2text``，视觉模型必须先以 ``chat`` 完成
    实例创建，再用官方模型类型接口切换为 Picture Parser 所需的 ``image2text``。
    镜像也不提供实例更新路由，第二次执行必须走 connection 验证，不能删除实例。
    """
    providers: set[str] = set()
    instances: dict[str, set[str]] = {}
    models: dict[tuple[str, str], list[dict[str, Any]]] = {}
    instance_keys: dict[tuple[str, str], str] = {}
    verified_connection_keys: list[str] = []
    updated_model_types: list[tuple[str, list[str]]] = []
    tenant = {
        "tenant_id": "tenant-test",
        "asr_id": "",
        "embd_id": "legacy-embedding",
        "img2txt_id": "legacy-vision",
        "llm_id": "",
    }

    def envelope(data: Any) -> httpx.Response:
        """构造 RAGFlow v0.26.4 成功 Envelope。"""
        return httpx.Response(200, json={"code": 0, "data": data})

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/system/healthz"):
            return httpx.Response(200, json={"status": "ok"})
        if path.endswith("/users/me/models"):
            if request.method == "PATCH":
                tenant.update(json.loads(request.content))
            return envelope(dict(tenant))
        if path.endswith("/users/me"):
            return envelope({"id": "user-test"})
        if path.endswith("/providers"):
            if request.method == "PUT":
                providers.add(json.loads(request.content)["provider_name"])
            return envelope([{"name": name} for name in sorted(providers)])
        parts = path.split("/")
        provider = parts[4]
        if path.endswith("/connection"):
            assert request.method == "POST"
            verified_connection_keys.append(json.loads(request.content)["api_key"])
            return envelope(None)
        if path.endswith("/instances"):
            if request.method == "POST":
                body = json.loads(request.content)
                instance = body["instance_name"]
                instances.setdefault(provider, set()).add(instance)
                instance_keys[(provider, instance)] = body["api_key"]
                models[(provider, instance)] = [
                    {"name": item["model_name"], "model_type": item["model_type"]}
                    for item in body["model_info"]
                ]
                return envelope({"instance_name": instance})
            return envelope(
                [{"instance_name": name} for name in sorted(instances.get(provider, set()))]
            )
        instance = parts[6]
        if path.endswith("/models"):
            if request.method == "POST":
                body = json.loads(request.content)
                models.setdefault((provider, instance), []).append(
                    {"name": body["model_name"], "model_type": body["model_type"]}
                )
            elif request.method == "PUT":
                body = json.loads(request.content)
                updated_model_types.append((instance, body["model_type"]))
                selected_names = set(body["model_name"])
                for item in models.get((provider, instance), []):
                    if item["name"] in selected_names:
                        item["model_type"] = body["model_type"]
            return envelope(models.get((provider, instance), []))
        raise AssertionError(f"unexpected RAGFlow path: {request.method} {path}")

    client = httpx.Client(
        base_url="http://ragflow.test/",
        transport=httpx.MockTransport(handler),
    )
    evidence: dict[str, Any] = {"steps": []}

    ensure_ragflow_models(_contract(), 768, evidence, ragflow_client=client)
    # 第二次使用同一 URL/模型但不同 Key，只验证新配置；v0.26.4 不支持原地更新实例。
    rotated = _contract(embedding_api_key="fixture-embedding-key-rotated")
    ensure_ragflow_models(rotated, 768, {"steps": []}, ragflow_client=client)

    assert providers == {"OpenAI-API-Compatible"}
    assert instance_keys[(rotated.provider, rotated.embedding_instance)] == "fixture-embedding-key"
    assert verified_connection_keys == [
        "fixture-embedding-key-rotated",
        "fixture-vision-key",
    ]
    assert tenant["embd_id"] == _contract().embedding_ref
    assert tenant["img2txt_id"] == _contract().vision_ref
    assert models[(_contract().provider, _contract().vision_instance)] == [
        {"name": _contract().vision_model, "model_type": ["image2text"]}
    ]
    assert updated_model_types == [(_contract().vision_instance, ["image2text"])]
    assert [item["operation"] for item in evidence["steps"]] == [
        "ragflow_embedding",
        "ragflow_vision",
    ]
    client.close()


def test_probe_results_are_written_without_changing_secrets(tmp_path: Path) -> None:
    """写回只改变探测字段，必须保留 API Key、注释并收紧权限。"""
    env_file = tmp_path / ".env.ragflow"
    env_file.write_text(
        "# fixture\n"
        "LLM_API_KEY=fixture-secret-key\n"
        "EMBEDDING_DIMENSION=pending-auto-detection\n"
        "RAGFLOW_EMBEDDING_MODEL=pending-model-bootstrap\n",
        encoding="utf-8",
    )
    compatibility = _MODULE.ChatCompatibility(
        omit_sampling_parameters=True,
        max_output_tokens_field="max_completion_tokens",
        extra_body={"enable_thinking": False},
    )

    _MODULE._persist_results((env_file,), _contract(), 768, compatibility)

    content = env_file.read_text(encoding="utf-8")
    assert "# fixture" in content
    assert "LLM_API_KEY=fixture-secret-key" in content
    assert "EMBEDDING_DIMENSION=768" in content
    assert f"RAGFLOW_EMBEDDING_MODEL={_contract().embedding_ref}" in content
    assert "LLM_MAX_OUTPUT_TOKENS_FIELD=max_completion_tokens" in content
    assert 'LLM_EXTRA_BODY_JSON={"enable_thinking":false}' in content
    assert env_file.stat().st_mode & 0o777 == 0o600
