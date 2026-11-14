"""图谱浏览、社区报告收费检查点及原文复核回归，不访问外部服务。"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from rag_service.api.app import create_app
from rag_service.application.community_reports import CommunityReportBuilder, connected_communities
from rag_service.application.graph_browser import SUMMARY_GRAPH_PROFILE, GraphBrowserService
from rag_service.application.profiles import ProfileCatalog
from rag_service.bootstrap.profiles import load_profile_seeds
from rag_service.domain.aggregate import AggregateEvidence, AggregateSource
from rag_service.domain.errors import RagError
from rag_service.domain.graph import GraphConfiguration, GraphState, GraphStatus
from rag_service.domain.graph_read import NativeGraphData, NativeGraphEntity, NativeGraphRelation
from rag_service.domain.models import IngestionJob, JobType, KnowledgeBase, KnowledgeBaseStatus
from rag_service.domain.ports import LlmResponse
from rag_service.security.direct_user import DirectUserResolver
from rag_service.settings import IdentitySection


class Fixture:
    """仅模拟资源存储和端口，保留应用的范围、版本、预算及JSON协议逻辑。"""

    def __init__(self) -> None:
        """构造两文档同一连通社区，原生描述特意与可引用原文分离。"""
        self.security = DirectUserResolver(IdentitySection()).resolve("graph-tenant", "graph-user")
        scope = self.security.tenant_resource_scope_id
        self.kb = KnowledgeBase(
            scope,
            "fixture",
            "office-default@1",
            "general-256@2",
            "embedding@1",
            "retrieval@1",
            status=KnowledgeBaseStatus.READY,
        )
        self.state = GraphState(
            scope,
            self.kb.id,
            GraphConfiguration(True, SUMMARY_GRAPH_PROFILE, False, True),
            status=GraphStatus.READY,
            graph_generation=2,
            built_for_revision=self.kb.revision,
            target_content_revision=self.kb.revision,
        )
        self.graph = NativeGraphData(
            scope,
            self.kb.id,
            2,
            (
                NativeGraphEntity(
                    "Atlas", "Atlas", "project", "Generated description", ("doc-1", "doc-2")
                ),
                NativeGraphEntity("Mira", "Mira", "person", "Generated person", ("doc-1",)),
            ),
            (NativeGraphRelation("edge", "Atlas", "Mira", "Generated relation", ("doc-1",)),),
        )
        self.sources = tuple(
            AggregateSource(
                self.kb.id,
                uuid4(),
                f"source-{i}.txt",
                f"chunk-{i}",
                f"Atlas source {i} with Mira.",
                scope,
                1,
                uuid4(),
                f"doc-{i}",
                hashlib.sha256(f"Atlas source {i} with Mira.".encode()).hexdigest(),
            )
            for i in (1, 2)
        )
        self.source_reads = 0
        self.graph_reads = 0
        self.fail_sources = False
        self.change_row_on_source = False
        self.change_body_after_first = False
        self.checkpoints = 0
        self.generations = 0
        self.generated_body: Any = {
            "summary": "Atlas has two supported sources.",
            "source_labels": ["S1", "S2"],
        }
        self.saved_payload: dict[str, Any] = {}

    async def get_accessible(self, scopes: tuple[str, ...], identifier: Any) -> Any:
        """未知和跨范围知识库均返回None，模拟实际读取策略而非总返回同一对象。"""
        if self.kb.security_scope_id in scopes and identifier == self.kb.id:
            return deepcopy(self.kb)
        return None

    async def get(self, scope: str, identifier: Any) -> Any:
        """图状态按Scope和KB复制读取，避免共享内存掩盖并发变化。"""
        return (
            deepcopy(self.state)
            if (scope, identifier) == (self.kb.security_scope_id, self.kb.id)
            else None
        )

    async def read_graph(self, *_args: Any) -> NativeGraphData:
        """返回有限原生图并记录调用数，默认不主动改变状态。"""
        self.graph_reads += 1
        return self.graph

    async def read_source_chunks(self, _scope: str, _kb: Any, doc_ids: tuple[str, ...]) -> Any:
        """可模拟原文失效或I/O期间版本变化，返回值仍来自所请求的真实形状文档。"""
        self.source_reads += 1
        if self.fail_sources:
            raise RagError("RAG-GRAPH-012", "来源不可用", 409, False)
        if self.change_row_on_source:
            self.state.row_version += 1
        result = tuple(source for source in self.sources if source.engine_document_id in doc_ids)
        if self.change_body_after_first and self.source_reads > 1:
            return tuple(replace(source, quote="Changed original") for source in result)
        return result

    async def update(self, _scope: str, job: IngestionJob) -> bool:
        """把真实应用写入的恢复意图复制保存，检查生成前已有独立检查点。"""
        self.saved_payload = deepcopy(job.payload)
        return True

    async def checkpoint(self) -> None:
        """模拟提交边界，模型会断言此前已完成该步骤。"""
        self.checkpoints += 1

    async def complete(self, request: Any) -> LlmResponse:
        """模拟唯一模型生成；原生描述不能进入上下文，未知请求不应被自动重发。"""
        self.generations += 1
        assert self.checkpoints > 0 and self.saved_payload["community_report_attempts"]
        assert "Generated description" not in request.messages[1].content
        assert request.max_output_tokens == 2048
        return LlmResponse(json.dumps(self.generated_body))

    def browser(self, enabled: bool = True) -> GraphBrowserService:
        """复用同一测试存储，模拟正式构造根装配方式。"""
        return GraphBrowserService(self, self, self, enabled=enabled)  # type: ignore[arg-type]

    def report(self) -> AggregateEvidence:
        """创建已发布的可核验报告，正文摘要由完整生成正文计算。"""
        summary = "Atlas has two supported sources."
        return AggregateEvidence(
            uuid4(),
            "",
            self.kb.id,
            "community",
            "Atlas",
            summary,
            2,
            self.kb.revision,
            0,
            SUMMARY_GRAPH_PROFILE,
            hashlib.sha256(summary.encode()).hexdigest(),
            self.sources,
            self.kb.security_scope_id,
            "synthetic-model",
        )

    def builder(self, settings: Any) -> CommunityReportBuilder:
        """使用仓库真实不可变Model Profile，不从测试代码覆盖Token或采样参数。"""
        return CommunityReportBuilder(
            self,
            self,
            settings.models.llm,
            ProfileCatalog(load_profile_seeds(settings.profiles)),
            self,
            self.checkpoint,  # type: ignore[arg-type]
        )


async def test_browser_search_neighborhood_and_source_projection() -> None:
    """搜索返回公开ID，随后同版本展开邻居；每个来源是独立原文而非生成描述。"""
    f = Fixture()
    view = await f.browser().view(f.security, f.kb.id, query="Atlas", depth=0)
    assert len(view.nodes) == 1 and len(view.nodes[0].id) == 32
    assert view.nodes[0].sources[0].quote == f.sources[0].quote
    expanded = await f.browser().view(f.security, f.kb.id, node_id=view.nodes[0].id, generation=2)
    assert len(expanded.nodes) == 2 and len(expanded.edges) == 1
    assert {expanded.edges[0].source, expanded.edges[0].target} == {n.id for n in expanded.nodes}
    limited = await f.browser().view(f.security, f.kb.id, node_id=view.nodes[0].id, limit=1)
    assert limited.truncated and len(limited.nodes) == 1


@pytest.mark.parametrize("status", [GraphStatus.STALE, GraphStatus.BUILDING, GraphStatus.FAILED])
async def test_browser_nonready_never_reads_native_body(status: GraphStatus) -> None:
    """过期、构图和失败状态不借旧图内容填页面。"""
    f = Fixture()
    f.state.status = status
    result = await f.browser().view(f.security, f.kb.id)
    assert result.status == status and not result.nodes and f.graph_reads == 0


async def test_browser_disabled_stale_versions_unknown_nodes_and_scope_are_closed() -> None:
    """关闭、版本错配、未知实体和跨租户都在返回正文前拒绝或清空。"""
    f = Fixture()
    assert (await f.browser(False).view(f.security, f.kb.id)).status == GraphStatus.DISABLED
    with pytest.raises(RagError, match="版本"):
        await f.browser().view(f.security, f.kb.id, generation=1)
    with pytest.raises(RagError) as missing:
        await f.browser().view(f.security, f.kb.id, node_id="f" * 32)
    assert missing.value.http_status == 404
    other = DirectUserResolver(IdentitySection()).resolve("other-tenant", "graph-user")
    with pytest.raises(RagError) as denied:
        await f.browser().view(other, f.kb.id)
    assert denied.value.http_status == 404
    f.graph = replace(f.graph, security_scope_id="untrusted")
    with pytest.raises(RagError) as unsafe:
        await f.browser().view(f.security, f.kb.id)
    assert unsafe.value.code == "RAG-GRAPH-004"


async def test_browser_missing_source_marks_unavailable_and_mid_read_change_rejects() -> None:
    """结构可读但原文不可用时明确警告；读取期间变更不能把旧正文送到前端。"""
    f = Fixture()
    f.fail_sources = True
    result = await f.browser().view(f.security, f.kb.id)
    assert result.warnings == ("graph_sources_unavailable",)
    assert not result.nodes[0].sources
    f.fail_sources = False
    f.change_row_on_source = True
    with pytest.raises(RagError) as changed:
        await f.browser().view(f.security, f.kb.id)
    assert changed.value.code == "RAG-GRAPH-020"


async def test_reports_revalidate_all_sources_and_reject_changed_digest() -> None:
    """有效报告可读，任一来源文本或报告摘要不一致都不能用于缓存或回答。"""
    f = Fixture()
    evidence = f.report()
    f.state.community_reports = (evidence,)
    available = await f.browser().list_available(f.security, (f.kb.id,))
    assert available == (evidence,)
    assert await f.browser().revalidate(f.security, (evidence,))
    # 每次回答重新分配的聚合ID不是底层报告身份；社区/版本/摘要/来源相同仍可复核。
    assert await f.browser().revalidate(
        f.security, (replace(evidence, aggregate_id=uuid4(), label="A1"),)
    )
    assert not await f.browser().revalidate(
        f.security, (replace(evidence, summary="Corrupted summary"),)
    )
    assert not await f.browser().revalidate(
        f.security, (replace(evidence, title="Corrupted title"),)
    )
    f.state.community_reports = (replace(evidence, report_sha256="0" * 64),)
    assert not await f.browser().list_available(f.security, (f.kb.id,))
    assert not await f.browser().revalidate(f.security, (evidence,))
    f.state.community_reports = (evidence,)
    f.sources = (replace(f.sources[0], document_revision=2), f.sources[1])
    assert not await f.browser().revalidate(f.security, (evidence,))


async def test_report_generation_has_durable_intent_and_reuses_completed_results(
    service_settings: Any,
) -> None:
    """首次生成前已持久化，第二次局部CAS恢复复用结果但再次复核原文。"""
    f = Fixture()
    job = IngestionJob(
        f.kb.security_scope_id,
        JobType.GRAPH_BUILD,
        knowledge_base_id=f.kb.id,
        payload={"content_revision": f.kb.revision},
    )
    reports = await f.builder(service_settings).build(job, f.state, 2)
    assert len(reports) == 1 and f.generations == 1 and f.checkpoints == 2
    assert len({s.document_id for s in reports[0].sources}) == 2
    assert await f.builder(service_settings).build(job, f.state, 2) == reports
    assert f.generations == 1


@pytest.mark.parametrize(
    "body",
    [
        {"summary": "unsupported", "source_labels": ["S1"]},
        {"summary": "unsupported", "source_labels": ["invented", "S2"]},
        {"summary": "", "source_labels": ["S1", "S2"]},
    ],
)
async def test_report_bad_model_result_never_retries_unknown_charge(
    service_settings: Any, body: Any
) -> None:
    """模型少来源或虚构标签导致拒绝；started无result的恢复不再次收费。"""
    f = Fixture()
    f.generated_body = body
    job = IngestionJob(
        f.kb.security_scope_id,
        JobType.GRAPH_BUILD,
        knowledge_base_id=f.kb.id,
        payload={"content_revision": f.kb.revision},
    )
    with pytest.raises(RagError) as bad:
        await f.builder(service_settings).build(job, f.state, 2)
    assert bad.value.code == "RAG-GRAPH-027"
    with pytest.raises(RagError) as unknown:
        await f.builder(service_settings).build(job, f.state, 2)
    assert unknown.value.code == "RAG-GRAPH-026" and f.generations == 1


async def test_report_generation_rechecks_original_after_model_completion(
    service_settings: Any,
) -> None:
    """模型处理期间上游原文改变，即使内容号未递增也不能发布旧摘要。"""
    f = Fixture()
    f.change_body_after_first = True
    job = IngestionJob(
        f.kb.security_scope_id,
        JobType.GRAPH_BUILD,
        knowledge_base_id=f.kb.id,
        payload={"content_revision": f.kb.revision},
    )
    with pytest.raises(RagError) as changed:
        await f.builder(service_settings).build(job, f.state, 2)
    assert changed.value.code == "RAG-GRAPH-025"


async def test_community_list_does_not_mix_initial_view_with_a_new_generation() -> None:
    """外层页面版本读取后、内层列表开始前重建完成，必须409而非拼接两代数据。"""
    f = Fixture()
    browser = f.browser()

    async def switched(_security: Any, _kb_ids: Any) -> Any:
        """模拟并发重建在列表协作调用的调度间隙完成。"""
        f.state.graph_generation = 3
        f.state.row_version += 1
        return (replace(f.report(), graph_generation=3),)

    browser.list_available = switched  # type: ignore[method-assign]
    with pytest.raises(RagError) as changed:
        await browser.communities(f.security, f.kb.id, generation=2)
    assert changed.value.code == "RAG-GRAPH-020"


async def test_community_document_budget_includes_relation_only_sources(
    service_settings: Any,
) -> None:
    """节点均在Doc1而边来自Doc2仍是多文档社区；来源数量不能只统计节点。"""
    f = Fixture()
    f.graph = replace(
        f.graph,
        entities=tuple(replace(node, source_document_ids=("doc-1",)) for node in f.graph.entities),
        relations=(replace(f.graph.relations[0], source_document_ids=("doc-2",)),),
    )
    job = IngestionJob(
        f.kb.security_scope_id,
        JobType.GRAPH_BUILD,
        knowledge_base_id=f.kb.id,
        payload={"content_revision": f.kb.revision},
    )
    reports = await f.builder(service_settings).build(job, f.state, 2)
    assert len(reports) == 1 and len({source.document_id for source in reports[0].sources}) == 2
    f.graph = replace(
        f.graph,
        relations=(
            replace(
                f.graph.relations[0],
                source_document_ids=tuple(f"doc-{number}" for number in range(1, 10)),
            ),
        ),
    )
    assert not await f.builder(service_settings).build(job, f.state, 2)
    assert f.generations == 1


def test_browser_http_fields_budget_identity_and_community_details(service_settings: Any) -> None:
    """真实FastAPI校验参数和公开投影，内部Scope/引擎文档指针不应泄漏。"""
    f = Fixture()
    f.state.community_reports = (f.report(),)
    app = create_app(service_settings, graph_browser=f.browser())
    prefix = f"/v1/management/knowledge-bases/{f.kb.id}/graph"
    headers = {"X-Tenant-ID": "graph-tenant", "X-User-ID": "graph-user"}
    with TestClient(app) as client:
        assert client.get(prefix + "/view").status_code == 400
        assert client.get(prefix + "/view?depth=3", headers=headers).status_code == 400
        result = client.get(prefix + "/view", headers=headers)
        assert result.status_code == 200
        assert result.json()["data"]["nodes"][0]["generated"] is True
        assert "engine_document_id" not in result.text and "resource_scope_id" not in result.text
        communities = client.get(prefix + "/communities?generation=2", headers=headers)
        assert communities.status_code == 200
        assert communities.json()["data"]["communities"][0]["origin"] == "gateway_generated"
        detail = client.get(prefix + "/communities/community?generation=2", headers=headers)
        assert detail.status_code == 200 and len(detail.json()["data"]["sources"]) == 2
        assert client.get(prefix + "/communities/missing", headers=headers).status_code == 404


def test_connected_components_are_deterministic_and_include_isolated_nodes() -> None:
    """有向关系按无向连通分组，确定性编号不假装来自上游Leiden。"""
    f = Fixture()
    f.graph = replace(
        f.graph,
        entities=f.graph.entities
        + (NativeGraphEntity("Isolated", "Isolated", "entity", "", ("doc-1",)),),
    )
    assert connected_communities(f.graph) == (("Atlas", "Mira"), ("Isolated",))
