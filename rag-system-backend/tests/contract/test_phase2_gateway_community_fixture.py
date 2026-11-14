"""真实合成二期导出的公共 C/A 与 SSE 契约；不生成占位数据或跳过缺失证据。"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from rag_service.api.aggregate_schemas import AggregateEvidenceData
from rag_service.api.graph_browser_routes import CommunityListData
from rag_service.api.online_routes import _citation
from rag_service.api.schemas import AnswerResponse, ChunkData, CitationData, QueryPlanData
from rag_service.application.online import StreamEvent, stream_event_json
from rag_service.domain.models import Citation

_FIXTURE = (
    Path(__file__).parents[1] / "fixtures" / "ragflow-v0.26.4-phase2-gateway-community.json"
)
_ORIGINALS = {
    "aurora-atlas.txt": (
        "Aurora Labs develops the Atlas project. Atlas is a research project "
        "for renewable energy forecasting. Aurora Labs appointed Mira Chen as the leader of Atlas. "
        "Mira Chen manages the Atlas engineering team. This is a fictional acceptance fixture.\n"
    ),
    "atlas-pilot.txt": (
        "Mira Chen leads the Atlas project for Aurora Labs. Atlas is tested at "
        "the Harbor Research Center in Northport. "
        "The Harbor Research Center hosts the Atlas pilot. "
        "Aurora Labs collaborates with the Harbor Research Center. All names are synthetic.\n"
    ),
}


@pytest.fixture(scope="module")
def captured() -> dict[str, Any]:
    """缺少真实导出就失败，不能把未完成的真实验收标成 skip 或用手写报告替代。"""
    assert _FIXTURE.is_file(), (
        "Real phase-two fixture is required: finish browse/answers/SSE acceptance and export it."
    )
    result = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(result, dict)
    return result


def _canonical_report(report: dict[str, Any]) -> dict[str, Any]:
    """请求级 aggregate_id 和 A 标签不同不改变同一报告来源及生成版本。"""
    return {key: value for key, value in report.items() if key not in {"aggregate_id", "label"}}


def _canonical_citation(citation: dict[str, Any]) -> dict[str, Any]:
    """核对原捕获的来源语义；请求级 ID 和修复前两种分数口径另外验证。"""
    value = CitationData.model_validate(citation).model_dump(mode="json")
    return {key: item for key, item in value.items() if key not in {"citation_id", "score"}}


def _frames(raw: str) -> list[dict[str, Any]]:
    """独立解析已捕获的文本帧，不调用导出脚本或内部 StreamEvent 来自证协议。"""
    assert raw.endswith("\n\n")
    frames = []
    for frame in raw.split("\n\n"):
        if not frame:
            continue
        lines = frame.splitlines()
        assert len(lines) == 3
        assert lines[0].startswith("event: ") and lines[1].startswith("id: ")
        assert lines[2].startswith("data: ")
        frames.append({"event": lines[0][7:], "id": lines[1][4:], "data": json.loads(lines[2][6:])})
    return frames


def test_rest_and_sse_share_citation_score_precedence(captured: dict[str, Any]) -> None:
    """保留修复前真实帧，再用同一引用的实际两种分数验证当前序列化口径。"""
    old_wire = next(frame["data"]["citation"] for frame in _frames(captured["sse"]["raw_frames"])
                    if frame["event"] == "citation")
    rest = captured["community_answer"]["data"]["citations"][0]
    assert old_wire["document_id"] == rest["document_id"]
    assert old_wire["score"] != rest["score"]
    source = next(source for report in captured["community_answer"]["data"]["aggregate_evidence"]
                  for source in report["sources"] if source["document_id"] == rest["document_id"])
    value = Citation(
        citation_id=UUID(rest["citation_id"]), label=rest["label"],
        knowledge_base_id=UUID(rest["knowledge_base_id"]), document_id=UUID(rest["document_id"]),
        document_name=rest["document_name"], engine_chunk_id=source["chunk_id"],
        quote=rest["quote"],
        score=old_wire["score"], rerank_score=rest["score"],
    )
    for citation in (value, replace(value, rerank_score=None)):
        frame = stream_event_json(
            request_id=UUID(captured["community_answer"]["request_id"]), sequence=1,
            event=StreamEvent("citation", {"citation": citation}),
        )
        score = _frames(frame)[0]["data"]["citation"]["score"]
        assert score == _citation(citation).score
        expected = citation.rerank_score if citation.rerank_score is not None else citation.score
        assert score == expected


def _assert_reports(
    reports: list[dict[str, Any]], documents: dict[str, dict[str, Any]],
    *, knowledge_base_id: str, labeled: bool,
) -> None:
    """报告具备两文档/两块，quote 逐字匹配独立块且有效正文来自原上传 TXT。"""
    assert 1 <= len(reports) <= 4
    for raw in reports:
        report = AggregateEvidenceData.model_validate(raw)
        assert str(report.knowledge_base_id) == knowledge_base_id
        assert report.origin == "gateway_generated" and report.derived is True
        assert report.kind == "community_aggregate"
        assert report.generator_model_ref == "synthetic-chat"
        assert report.strategy_ref == "gateway-connected-community@1"
        assert report.graph_profile_ref == "native-graph-with-summaries@1"
        if labeled:
            assert re.fullmatch(r"A[1-9][0-9]*", report.label)
        else:
            assert report.label == ""
        assert len({source.document_id for source in report.sources}) >= 2
        assert len({source.chunk_id for source in report.sources}) >= 2
        identities = {(source.document_id, source.chunk_id) for source in report.sources}
        assert len(identities) == len(report.sources)
        for source in report.sources:
            original = documents[str(source.document_id)]
            assert str(source.knowledge_base_id) == knowledge_base_id
            assert source.document_name == original["filename"]
            assert source.quote.strip() and source.quote.strip() in original["text"]
            assert source.positions == []
            readback = original["readback"]
            assert source.document_revision == readback["document_revision"]
            active_version = str(source.active_version_id) if source.active_version_id else None
            assert active_version == readback["active_version_id"]
            assert any(
                chunk["id"] == source.chunk_id
                and chunk["document_id"] == str(source.document_id)
                and source.quote in chunk["content"]
                and chunk["positions"] == []
                for chunk in readback["chunks"]
            )
            assert any(
                item["chunk_id"] == source.chunk_id
                and item["sha256"] == hashlib.sha256(source.quote.encode()).hexdigest()
                and item["origin"] == "captured_independent_acceptance_verification"
                for item in readback["quote_hashes"]
            )


def _assert_used_sources(
    text: str, reports: list[dict[str, Any]], citations: list[dict[str, Any]],
    documents: dict[str, dict[str, Any]],
) -> None:
    """公开普通 C 引用只能来自同一回答实际使用 A 的原文来源集合。"""
    a_labels = {report["label"] for report in reports}
    assert set(re.findall(r"\[(A[1-9][0-9]*)\]", text)) == a_labels
    c_labels = {citation["label"] for citation in citations}
    assert set(re.findall(r"\[(C[1-9][0-9]*)\]", text)) == c_labels
    assert not a_labels.intersection(c_labels)
    assert set(re.findall(r"\[([CA][^\]\r\n]*)\]", text)) <= a_labels | c_labels
    sources = [source for report in reports for source in report["sources"]]
    for raw in citations:
        citation = CitationData.model_validate(raw)
        original = documents[str(citation.document_id)]
        assert citation.quote.strip() and citation.quote.strip() in original["text"]
        assert citation.positions == []
        assert citation.page_start is None and citation.page_end is None
        matching = [source for source in sources if (
            raw["knowledge_base_id"] == source["knowledge_base_id"]
            and raw["document_id"] == source["document_id"]
            and raw["document_name"] == source["document_name"]
            and raw["quote"] == source["quote"]
            and raw["positions"] == source["positions"]
        )]
        assert matching
        assert any(
            chunk["id"] == source["chunk_id"]
            and chunk["document_id"] == str(citation.document_id)
            and citation.quote in chunk["content"]
            for source in matching for chunk in original["readback"]["chunks"]
        )


def test_captured_public_dtos_and_uploaded_originals(captured: dict[str, Any]) -> None:
    """验证公开 Pydantic 协议和原始文本，不能把重写的合成摘要冒充真实捕获。"""
    provenance = captured["provenance"]
    assert provenance["ragflow_version"] == "v0.26.4" and provenance["synthetic_only"] is True
    assert provenance["origin"] == "gateway_generated"
    assert provenance["native_community_report"] is False
    assert provenance["body_text_changed"] is False
    assert provenance["acceptance_stages"] == {"browse": True, "answers": True, "sse": True}
    assert provenance["sensitive_metadata_exported"] is False
    documents = {item["document_id"]: item for item in captured["documents"]}
    assert len(documents) == 2
    assert {item["filename"] for item in documents.values()} == set(_ORIGINALS)
    for document_id, item in documents.items():
        assert UUID(document_id).version == 5
        assert item["text"] == _ORIGINALS[item["filename"]]
        assert item["sha256"] == hashlib.sha256(item["text"].encode()).hexdigest()
        readback = item["readback"]
        assert readback["capture"] == "independent_public_document_and_chunk_get"
        assert readback["status"] == "READY" and readback["document_revision"] >= 1
        assert readback["verified_stages"] == ["community_json", "tcp_community_complete"]
        assert readback["chunks"]
        for raw_chunk in readback["chunks"]:
            chunk = ChunkData.model_validate(raw_chunk)
            assert str(chunk.document_id) == document_id
            # 真实 v0.26.4 此次 TXT 解析增加了开头换行；不得 trim 两份已捕获的块来过测。
            assert chunk.content.startswith("\n") and chunk.content != item["text"]
            assert chunk.content.strip() == item["text"].strip()
            assert chunk.positions == []
            assert any(
                digest["chunk_id"] == chunk.id
                and digest["sha256"] == hashlib.sha256(chunk.content.encode()).hexdigest()
                for digest in readback["chunk_hashes"]
            )
    browse = CommunityListData.model_validate(captured["community_browse"])
    response = AnswerResponse.model_validate(captured["community_answer"])
    kb_id = str(browse.knowledge_base_id)
    assert browse.status == "READY" and browse.graph_generation > 0
    assert response.data.outcome == "success" and response.data.evidence_mode == "community"
    browse_reports = captured["community_browse"]["communities"]
    answer_reports = captured["community_answer"]["data"]["aggregate_evidence"]
    _assert_reports(browse_reports, documents, knowledge_base_id=kb_id, labeled=False)
    _assert_reports(answer_reports, documents, knowledge_base_id=kb_id, labeled=True)
    for report in answer_reports:
        assert any(_canonical_report(report) == _canonical_report(item) for item in browse_reports)
    _assert_used_sources(
        response.data.answer, answer_reports, captured["community_answer"]["data"]["citations"],
        documents,
    )


def test_actual_sse_wire_frames_match_json_semantics(captured: dict[str, Any]) -> None:
    """实际 event: aggregate.evidence 与 completed 必须一致，重放 ID 可与 JSON 不同。"""
    assert captured["sse"]["capture"] == "real_tcp_wire_frames_with_anonymized_identifiers"
    raw = captured["sse"]["raw_frames"]
    assert "event: aggregate.evidence\n" in raw and "event: aggregate\n" not in raw
    frames = _frames(raw)
    names = [frame["event"] for frame in frames]
    assert names[0] == "accepted" and names[-1] == "completed"
    assert names.count("completed") == 1 and "error" not in names
    request_ids = {frame["data"]["request_id"] for frame in frames}
    assert len(request_ids) == 1 and UUID(next(iter(request_ids))).version == 5
    assert [frame["data"]["sequence"] for frame in frames] == list(range(1, len(frames) + 1))
    assert [frame["id"] for frame in frames] == [str(index) for index in range(1, len(frames) + 1)]
    for frame in frames:
        if frame["event"] == "retrieval.completed":
            QueryPlanData.model_validate(frame["data"]["query_plan"])
    terminal = frames[-1]["data"]
    assert terminal["outcome"] == "success" and terminal["evidence_mode"] == "community"
    assert terminal["cache_type"] == "exact"
    reports = [
        frame["data"]["evidence"] for frame in frames if frame["event"] == "aggregate.evidence"
    ]
    citations = [frame["data"]["citation"] for frame in frames if frame["event"] == "citation"]
    assert reports == terminal["aggregate_evidence"]
    text = "".join(frame["data"]["delta"] for frame in frames if frame["event"] == "answer.delta")
    original = captured["community_answer"]["data"]
    assert text == original["answer"]
    assert [_canonical_report(item) for item in reports] == [
        _canonical_report(item) for item in original["aggregate_evidence"]
    ]
    assert [_canonical_citation(item) for item in citations] == [
        _canonical_citation(item) for item in original["citations"]
    ]
    documents = {item["document_id"]: item for item in captured["documents"]}
    _assert_reports(
        reports, documents,
        knowledge_base_id=captured["community_browse"]["knowledge_base_id"], labeled=True,
    )
    _assert_used_sources(text, reports, citations, documents)


def test_capture_retains_real_content_hashes_and_no_private_transport_metadata(
    captured: dict[str, Any],
) -> None:
    """哈希从真实保留正文独立重算；私有 Scope、请求头和实例 URL 不得进入提交。"""
    manifest = captured["content_hashes"]
    assert manifest["algorithm"] == "sha256"
    assert manifest["origin"] == "independently_calculated_from_captured_public_text"
    frames = _frames(captured["sse"]["raw_frames"])
    reports = [
        *captured["community_browse"]["communities"],
        *captured["community_answer"]["data"]["aggregate_evidence"],
        *(frame["data"]["evidence"] for frame in frames if frame["event"] == "aggregate.evidence"),
    ]
    summaries = {(item["community_id"], hashlib.sha256(item["summary"].encode()).hexdigest())
                 for item in reports}
    quotes = {
        (
            source["document_id"], source["chunk_id"],
            hashlib.sha256(source["quote"].encode()).hexdigest(),
        )
        for item in reports for source in item["sources"]
    }
    assert {(item["community_id"], item["sha256"]) for item in manifest["summaries"]} == summaries
    assert {
        (item["document_id"], item["chunk_id"], item["sha256"]) for item in manifest["quotes"]
    } == quotes

    def public_fields(value: Any) -> None:
        """连同已解析 SSE JSON 一起检查，避免转义后的原始帧字符串藏私有字段。"""
        if isinstance(value, dict):
            for key, item in value.items():
                assert not re.search(
                    r"scope|tenant_id|user_id|headers?|api_key|password|authorization|"
                    r"base_url|server_url|access_token",
                    key, re.IGNORECASE,
                )
                if key in {"request_id", "knowledge_base_id", "document_id", "active_version_id",
                           "aggregate_id", "citation_id"} and item is not None:
                    assert UUID(item).version == 5
                public_fields(item)
        elif isinstance(value, list):
            for item in value:
                public_fields(item)

    public_fields(captured)
    public_fields(frames)
    serialized = json.dumps(captured, ensure_ascii=False)
    assert not re.search(
        r"https?://|redis://|postgres(?:ql)?://|Bearer |sk-[A-Za-z0-9_-]{12,}", serialized
    )
