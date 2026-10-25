"""启用 Rerank 时启动预检必须真实验证能力的回归测试。

这里使用可注入的合成 Reranker 验证启动编排：成功结果才允许报告
``enabled_validated``，而在线 Adapter 返回的安全降级结果在启动阶段必须转成配置错误。
这样运行中偶发故障可以继续 RRF，错误 API Key 却不能被发布成“已启用”。
"""

from __future__ import annotations

import httpx
import pytest

from rag_service.bootstrap.runtime import RuntimeDependencyPreflight
from rag_service.domain.errors import RerankError
from rag_service.domain.ports import RerankOutcome
from rag_service.settings import RerankSection, ServiceSettings


class FakeEmbedding:
    """返回固定 1024 维合成向量，使测试不依赖任何真实模型端点。"""

    async def embed(self, _texts: list[str]) -> tuple[tuple[float, ...], ...]:
        """满足启动维度探针，且不执行任何外部或持久化操作。"""
        return (tuple(0.0 for _ in range(1024)),)


class FakeReranker:
    """记录调用并返回测试指定结果，用于模拟真实 Adapter 成功或降级。"""

    def __init__(self, outcome: RerankOutcome) -> None:
        self.outcome = outcome
        self.calls = 0

    @property
    def enabled(self) -> bool:
        """测试替身代表已配置的真实服务。"""
        return True

    async def rerank(self, _query: str, _documents: tuple[str, ...]) -> RerankOutcome:
        """返回预设结果并记录启动预检确实发起过能力验证。"""
        self.calls += 1
        return self.outcome


def _enabled_settings(settings: ServiceSettings) -> ServiceSettings:
    """仅替换冻结配置中的 Rerank 段，不改变数据库、模型或 RAGFlow Fixture。"""
    rerank = RerankSection(
        enabled=True,
        base_url="https://rerank.test/v1",
        api_key="synthetic-test-key",
        model="synthetic-rerank-model",
    )
    return settings.model_copy(
        update={"models": settings.models.model_copy(update={"rerank": rerank})}
    )


def _model_client() -> httpx.AsyncClient:
    """返回能区分普通回答、JSON Rewrite、视觉 OCR 和模型清单的 Mock Client。"""
    def handler(request: httpx.Request) -> httpx.Response:
        """按请求形态返回最小合法协议，避免结构化预检被普通文本假通过。"""
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

    return httpx.AsyncClient(
        base_url="http://models.test/v1/",
        transport=httpx.MockTransport(handler),
    )


def _ragflow_client() -> httpx.AsyncClient:
    """模拟 RAGFlow 健康、账号和租户模型接口，避免真实数据读写。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/healthz"):
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path.endswith("/models"):
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "embd_id": (
                            "synthetic-embedding@lazytt-embedding-fixture@"
                            "OpenAI-API-Compatible"
                        ),
                        "img2txt_id": (
                            "synthetic-vision@lazytt-vision-fixture@"
                            "OpenAI-API-Compatible"
                        ),
                    },
                },
            )
        return httpx.Response(200, json={"code": 0, "data": {"id": "tenant"}})

    return httpx.AsyncClient(
        base_url="http://ragflow.test/",
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
async def test_enabled_rerank_preflight_reports_only_validated_success(
    service_settings: ServiceSettings,
) -> None:
    """两项真实分数都存在时才允许启动摘要声明 Rerank 已验证。"""
    model_client = _model_client()
    ragflow_client = _ragflow_client()
    reranker = FakeReranker(RerankOutcome(scores=(0.9, 0.1), enabled=True))
    preflight = RuntimeDependencyPreflight(
        _enabled_settings(service_settings),
        model_client=model_client,
        vision_client=model_client,
        ragflow_client=ragflow_client,
        embedding=FakeEmbedding(),
        reranker=reranker,
    )

    result = await preflight.check()

    assert result.rerank == "enabled_validated"
    assert reranker.calls == 1
    await model_client.aclose()
    await ragflow_client.aclose()


@pytest.mark.asyncio
async def test_enabled_rerank_preflight_rejects_runtime_fallback(
    service_settings: ServiceSettings,
) -> None:
    """配置开启但外部服务降级时必须阻止启动，避免能力接口产生假阳性。"""
    model_client = _model_client()
    ragflow_client = _ragflow_client()
    reranker = FakeReranker(
        RerankOutcome(
            scores=(None, None),
            enabled=False,
            warning="rerank_service_unavailable_vector_bm25_rrf_fallback",
        )
    )
    preflight = RuntimeDependencyPreflight(
        _enabled_settings(service_settings),
        model_client=model_client,
        vision_client=model_client,
        ragflow_client=ragflow_client,
        embedding=FakeEmbedding(),
        reranker=reranker,
    )

    with pytest.raises(RerankError) as captured:
        await preflight.check()

    assert captured.value.code == "RAG-CONFIG-009"
    assert reranker.calls == 1
    await model_client.aclose()
    await ragflow_client.aclose()
