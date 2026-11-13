"""图谱模型配置与登记测试：验证默认关闭、独立凭据、真实协议探测和默认 Chat。

所有模型和 RAGFlow 请求均由 MockTransport 处理，不访问公网、不创建真实图谱；
文件写回只在 pytest 临时目录执行，合成密钥不进入探测结果或错误消息。
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from tools.ragflow_cloud_model_bootstrap import (
    BootstrapFailure,
    ChatCompatibility,
    CloudModelContract,
    _contract,
    _persist_results,
    ensure_ragflow_models,
    validate_graph_chat_model,
)

from rag_service.bootstrap.runtime import RuntimeDependencyPreflight
from rag_service.domain.errors import RagFlowError
from rag_service.settings import RagFlowSection, ServiceSettings


def graph_contract(*, enabled: bool = True) -> CloudModelContract:
    """构造互不共享凭据的合成模型职责，便于发现 Gateway/图谱模型接线混用。"""
    return CloudModelContract(
        ragflow_base_url="http://ragflow.test",
        ragflow_api_key="synthetic-ragflow-key",
        embedding_base_url="https://embedding.test/v1",
        embedding_api_key="synthetic-embedding-key",
        embedding_model="embedding-fixture",
        llm_base_url="https://gateway-chat.test/v1",
        llm_api_key="synthetic-gateway-key",
        llm_model="gateway-chat-fixture",
        rewrite_model="gateway-chat-fixture",
        vision_base_url="https://vision.test/v1",
        vision_api_key="synthetic-vision-key",
        vision_model="vision-fixture",
        graph_enabled=enabled,
        graph_chat_base_url="https://graph-chat.test/v1" if enabled else "",
        graph_chat_api_key="synthetic-graph-key" if enabled else "",
        graph_chat_model="graph-chat-fixture" if enabled else "",
    )


@pytest.mark.parametrize("missing", ["BASE_URL", "API_KEY", "MODEL"])
def test_graph_contract_requires_explicit_credentials(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    """即使 Gateway 三元组完整，缺任意图谱字段仍在联网前失败，禁止隐式复用。"""
    for name in (
        "RAGFLOW_API_KEY",
        "EMBEDDING_BASE_URL",
        "EMBEDDING_API_KEY",
        "EMBEDDING_MODEL",
        "LLM_BASE_URL",
        "LLM_API_KEY",
        "LLM_MODEL",
        "VISION_BASE_URL",
        "VISION_API_KEY",
        "VISION_MODEL",
    ):
        monkeypatch.setenv(name, "synthetic-configured-value")
    monkeypatch.setenv("RAGFLOW_GRAPH_ENABLED", "true")
    for suffix in ("BASE_URL", "API_KEY", "MODEL"):
        monkeypatch.setenv(f"RAGFLOW_GRAPH_CHAT_{suffix}", "synthetic-graph-value")
    monkeypatch.delenv(f"RAGFLOW_GRAPH_CHAT_{missing}")
    with pytest.raises(BootstrapFailure, match=f"RAGFLOW_GRAPH_CHAT_{missing}"):
        _contract()


def test_disabled_graph_does_not_call_model() -> None:
    """默认关闭能力时不发送额外请求，不增加旧部署的凭据要求和调用费用。"""

    def handler(_request: httpx.Request) -> httpx.Response:
        """关闭状态若发起请求立即失败，避免测试悄悄漏掉额外模型调用。"""
        raise AssertionError("disabled graph must not call a model")

    evidence: dict[str, Any] = {"steps": []}
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        validate_graph_chat_model(graph_contract(enabled=False), evidence, client=client)
    assert evidence == {"steps": []}


def test_graph_probe_uses_standard_protocol_and_separate_model() -> None:
    """图谱探测必须直接发送独立模型的标准 Chat 请求，而不能继承 Gateway 协议补丁。"""
    contract = graph_contract()
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """核对请求模型、凭据与原生协议，再返回固定的合成关系。"""
        payload = json.loads(request.content)
        requests.append(payload)
        assert request.headers["authorization"] == f"Bearer {contract.graph_chat_api_key}"
        assert payload["model"] == contract.graph_chat_model
        assert payload["max_tokens"] == 256
        assert payload["stream"] is False
        assert "enable_thinking" not in payload
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "subject": "SyntheticA",
                                    "predicate": "depends_on",
                                    "object": "SyntheticB",
                                }
                            )
                        }
                    }
                ]
            },
        )

    evidence: dict[str, Any] = {"steps": []}
    with httpx.Client(
        base_url=contract.graph_chat_base_url + "/", transport=httpx.MockTransport(handler)
    ) as client:
        validate_graph_chat_model(contract, evidence, client=client)
    assert len(requests) == 1
    assert evidence["steps"][0]["operation"] == "graph_chat"
    assert contract.graph_chat_api_key not in json.dumps(evidence)
    assert contract.graph_chat_api_key not in repr(contract)


def test_graph_probe_rejects_invalid_output_without_echoing_secret() -> None:
    """模型错误即使回显合成密钥，也只能产生固定错误，不能进入日志或登记结果。"""
    contract = graph_contract()
    evidence: dict[str, Any] = {"steps": []}
    with httpx.Client(
        base_url=contract.graph_chat_base_url + "/",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(400, json={"message": contract.graph_chat_api_key})
        ),
    ) as client:
        with pytest.raises(BootstrapFailure, match="standard Chat Completions") as error:
            validate_graph_chat_model(contract, evidence, client=client)
    assert contract.graph_chat_api_key not in str(error.value)
    assert not evidence["steps"]


def test_graph_model_is_registered_as_chat_and_selected_idempotently() -> None:
    """首次登记写入独立 chat 实例和租户 llm_id，再次执行复用实例而不删除已有资源。"""
    contract = graph_contract()
    models: dict[str, list[dict[str, Any]]] = {}
    created: list[dict[str, Any]] = []
    tenant = {"tenant_id": "fixture", "llm_id": "legacy-chat", "asr_id": "legacy-asr"}

    def envelope(data: Any) -> httpx.Response:
        """为合成 Provider 状态生成官方成功 Envelope，不读取或写入真实服务。"""
        return httpx.Response(200, json={"code": 0, "data": data})

    def handler(request: httpx.Request) -> httpx.Response:
        """仅在内存模拟实例登记、模型类型切换和租户默认值，禁止资源删除。"""
        path = request.url.path
        assert request.method != "DELETE"
        if path.endswith("/healthz"):
            return httpx.Response(200, json={"status": "ok"})
        if path.endswith("/users/me/models"):
            if request.method == "PATCH":
                tenant.update(json.loads(request.content))
            return envelope(dict(tenant))
        if path.endswith("/users/me"):
            return envelope({"id": "fixture"})
        if path.endswith("/providers"):
            return envelope([{"name": contract.provider}])
        if path.endswith("/connection"):
            return envelope(None)
        if path.endswith("/instances"):
            if request.method == "POST":
                payload = json.loads(request.content)
                created.append(payload)
                models[payload["instance_name"]] = [
                    {"name": value["model_name"], "model_type": value["model_type"]}
                    for value in payload["model_info"]
                ]
                return envelope({"instance_name": payload["instance_name"]})
            return envelope([{"instance_name": name} for name in models])
        instance = path.split("/")[6]
        if request.method == "PUT":
            payload = json.loads(request.content)
            for value in models[instance]:
                value["model_type"] = payload["model_type"]
        return envelope(models[instance])

    evidence: dict[str, Any] = {"steps": []}
    with httpx.Client(
        base_url=contract.ragflow_base_url, transport=httpx.MockTransport(handler)
    ) as client:
        ensure_ragflow_models(contract, 768, evidence, ragflow_client=client)
        ensure_ragflow_models(contract, 768, {"steps": []}, ragflow_client=client)
    assert len(created) == 3
    graph_instance = next(
        item for item in created if item["instance_name"] == contract.graph_chat_instance
    )
    assert graph_instance["api_key"] == contract.graph_chat_api_key
    assert models[contract.graph_chat_instance] == [
        {"name": contract.graph_chat_model, "model_type": ["chat"]}
    ]
    assert tenant["llm_id"] == contract.graph_chat_ref
    assert tenant["asr_id"] == "legacy-asr"
    assert evidence["steps"][-1]["operation"] == "ragflow_graph_chat"
    assert contract.graph_chat_api_key not in json.dumps(evidence)


@pytest.mark.parametrize("enabled", [False, True])
def test_graph_result_persistence_only_changes_reference(tmp_path: Path, enabled: bool) -> None:
    """注册结果写回不更改已有图谱密钥；关闭能力会清空曾经登记的旧引用。"""
    contract = graph_contract(enabled=enabled)
    target = tmp_path / ".env.ragflow"
    target.write_text(
        "RAGFLOW_GRAPH_CHAT_API_KEY=synthetic-original-key\n"
        "RAGFLOW_GRAPH_CHAT_MODEL_REF=stale-ref\n",
        encoding="utf-8",
    )
    _persist_results((target,), contract, 768, ChatCompatibility())
    content = target.read_text(encoding="utf-8")
    assert "RAGFLOW_GRAPH_CHAT_API_KEY=synthetic-original-key\n" in content
    assert f"RAGFLOW_GRAPH_CHAT_MODEL_REF={contract.graph_chat_ref}\n" in content


@pytest.mark.parametrize("reference", [None, "", "pending-model-bootstrap"])
def test_enabled_graph_settings_reject_missing_registration(
    service_settings: ServiceSettings, reference: str | None
) -> None:
    """运行时只接收登记后的引用，开启但漏跑 Bootstrap 时必须在领取任务前失败。"""
    values = service_settings.ragflow.model_dump()
    values.update(graph_enabled=True, graph_chat_model=reference)
    with pytest.raises(ValueError, match="registered graph Chat"):
        RagFlowSection.model_validate(values)


@pytest.mark.asyncio
@pytest.mark.parametrize("matches", [False, True])
async def test_runtime_checks_graph_default_without_exposing_credentials(
    service_settings: ServiceSettings, matches: bool
) -> None:
    """开启图谱后，RAGFlow 默认 Chat 必须匹配登记引用；成功也不冒称图谱已构建。"""
    reference = "graph-fixture@instance@OpenAI-API-Compatible"
    settings = service_settings.model_copy(
        update={
            "ragflow": service_settings.ragflow.model_copy(
                update={"graph_enabled": True, "graph_chat_model": reference}
            )
        }
    )

    class Embedding:
        """无网络的固定维度向量替身，只用于隔离本测试关注的图谱模型绑定。"""

        async def embed(self, _texts: list[str]) -> tuple[tuple[float, ...], ...]:
            """返回配置维度的零向量，不执行模型请求或持久化。"""
            return ((0.0,) * settings.models.embedding.dimension,)

    def model_handler(request: httpx.Request) -> httpx.Response:
        """为基础 Chat/视觉预检返回合成文本，不参与真实图谱模型探测。"""
        payload = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": '{"status":"ok"}'
                            if "response_format" in payload
                            else "LAZYTT 2026"
                        }
                    }
                ]
            },
        )

    def ragflow_handler(request: httpx.Request) -> httpx.Response:
        """模拟默认 Chat 正确或漂移，验证预检不会借用其他已登记模型。"""
        if request.url.path.endswith("/healthz"):
            return httpx.Response(200, json={"status": "ok"})
        data = (
            {
                "embd_id": settings.ragflow.embedding_model,
                "img2txt_id": settings.ragflow.vision_model,
                "llm_id": reference if matches else "other-chat",
            }
            if request.url.path.endswith("/models")
            else {"id": "fixture"}
        )
        return httpx.Response(200, json={"code": 0, "data": data})

    async with (
        httpx.AsyncClient(
            base_url="https://model.test/v1/", transport=httpx.MockTransport(model_handler)
        ) as model_client,
        httpx.AsyncClient(
            base_url="http://ragflow.test/", transport=httpx.MockTransport(ragflow_handler)
        ) as ragflow_client,
    ):
        preflight = RuntimeDependencyPreflight(
            settings,
            model_client=model_client,
            vision_client=model_client,
            ragflow_client=ragflow_client,
            embedding=Embedding(),
        )
        if matches:
            result = await preflight.check()
            assert result.graph == "enabled_model_registered"
            assert result.graph_chat_model == reference
        else:
            with pytest.raises(RagFlowError):
                await preflight.check()


def test_graph_contract_ref_is_separate_from_gateway() -> None:
    """同一供应商也显式登记独立职责；更改图谱模型不影响 Gateway 模型或模型引用。"""
    contract = graph_contract()
    changed = replace(contract, graph_chat_model="different-graph-chat")
    assert changed.graph_chat_ref != contract.graph_chat_ref
    assert changed.llm_model == contract.llm_model
