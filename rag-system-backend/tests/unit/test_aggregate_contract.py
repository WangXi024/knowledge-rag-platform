"""派生证据 codec、公共协议和缓存边界回归；所有内容都是合成资料。"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from tests.unit.test_redis_cache import FakeEmbedding, FakeRedis, _semantic_search_result

from rag_service.api.aggregate_schemas import AggregateEvidenceData
from rag_service.api.schemas import AnswerRequest
from rag_service.application.aggregate import labels_valid, public_payload
from rag_service.application.online import StreamEvent, stream_event_json
from rag_service.domain.aggregate import (
    AGGREGATE_PROFILE_REF,
    AggregateEvidence,
    AggregateSource,
    evidences_from_payload,
    fingerprint,
    from_payload,
    to_payload,
)
from rag_service.domain.cache import cache_id
from rag_service.domain.models import QueryRecord
from rag_service.domain.ports import CacheBoundary, CacheEntry
from rag_service.infrastructure.cache.redis_cache import RedisAnswerCache, _entry, _entry_json
from rag_service.infrastructure.postgres.repositories import (
    SqlAlchemyQueryRepository,
    _query_to_domain,
)
from rag_service.settings import ServiceSettings


def _evidence() -> AggregateEvidence:
    """真实引擎无需参与领域协议测试，两个不同文档的合成原文具有独立哈希。"""
    kb_id = uuid4()
    sources = tuple(
        AggregateSource(
            knowledge_base_id=kb_id,
            document_id=uuid4(),
            document_name=f"fixture-{index}.txt",
            engine_chunk_id=f"chunk-{index}",
            quote=quote,
            resource_scope_id="a" * 64,
            document_revision=1,
            active_version_id=uuid4(),
            engine_document_id=f"engine-doc-{index}",
            quote_sha256=hashlib.sha256(quote.encode()).hexdigest(),
        )
        for index, quote in enumerate(("Aurora Labs builds Atlas.", "Mira Chen leads Atlas."))
    )
    return AggregateEvidence(
        aggregate_id=uuid4(), label="A1", knowledge_base_id=kb_id, community_id="community-1",
        title="Atlas community", summary="Atlas connects Aurora Labs and Mira Chen.",
        graph_generation=2, content_revision=3, configuration_version=1,
        graph_profile_ref="native-graph-with-summaries@1",
        report_sha256=hashlib.sha256(b"Atlas connects Aurora Labs and Mira Chen.").hexdigest(),
        sources=sources, resource_scope_id="a" * 64,
        generator_model_ref="answer-openai-compatible@4",
    )


def test_aggregate_codec_and_public_projection_keep_original_source_identity() -> None:
    """持久化保留私有校验材料，HTTP/SSE 只暴露可回查的来源和明确派生标记。"""
    evidence = _evidence()
    restored = from_payload(json.loads(json.dumps(to_payload(evidence))))
    assert restored == evidence
    public = AggregateEvidenceData.from_domain(evidence).model_dump(mode="json")
    assert public == json.loads(json.dumps(public_payload(evidence)))
    assert public["kind"] == "community_aggregate" and public["derived"] is True
    assert public["origin"] == "gateway_generated"
    assert public["sources"][0]["chunk_id"] == evidence.sources[0].engine_chunk_id
    assert public["sources"][0]["quote"] == evidence.sources[0].quote
    assert public["summary"] != public["sources"][0]["quote"]
    for source in public["sources"]:
        assert not {"resource_scope_id", "engine_document_id", "quote_sha256"}.intersection(source)
    frame = stream_event_json(
        request_id=uuid4(), sequence=1,
        event=StreamEvent("aggregate.evidence", {"evidence": evidence}),
    )
    assert frame.startswith("event: aggregate.evidence\n")
    data = json.loads(next(line[6:] for line in frame.splitlines() if line.startswith("data: ")))
    assert data["evidence"] == public


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("graph_generation", True), ("content_revision", "3"),
        ("configuration_version", 1.5), ("summary", ["text"]),
        ("summary", "x" * 6001), ("origin", "native"),
        ("strategy_ref", "unknown@1"), ("label", "C1"),
        ("sources", []), ("report_sha256", "short"),
    ],
)
def test_aggregate_codec_rejects_malformed_report(field: str, bad_value: Any) -> None:
    """缓存中的布尔修订、错误文本类型和超预算摘要都不能成为可信证据。"""
    payload = to_payload(_evidence())
    payload[field] = bad_value
    with pytest.raises((KeyError, TypeError, ValueError)):
        from_payload(payload)


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("document_revision", True), ("quote", {"text": "wrong"}),
        ("quote_sha256", "0" * 64), ("knowledge_base_id", str(uuid4())),
        ("resource_scope_id", "foreign"), ("positions", [{"page": True}]),
        ("positions", [{"bbox": [0, 1, 2, float("inf")]}]),
    ],
)
def test_aggregate_codec_rejects_invalid_source_bindings(field: str, bad_value: Any) -> None:
    """来源必须有合法修订、真实文本哈希和与报告一致的资源范围。"""
    payload = to_payload(_evidence())
    payload["sources"][0][field] = bad_value
    with pytest.raises((KeyError, TypeError, ValueError)):
        from_payload(payload)


def test_aggregate_requires_distinct_documents_and_bounded_collection() -> None:
    """同文档多个块不满足跨文档约束，重复块和第五份报告也不能绕开预算。"""
    payload = to_payload(_evidence())
    same_document = copy.deepcopy(payload)
    same_document["sources"][1]["document_id"] = same_document["sources"][0]["document_id"]
    with pytest.raises(ValueError, match="multiple documents"):
        from_payload(same_document)
    duplicate = copy.deepcopy(payload)
    duplicate["sources"].append(duplicate["sources"][0])
    with pytest.raises(ValueError, match="duplicate"):
        from_payload(duplicate)
    with pytest.raises(ValueError, match="collection"):
        evidences_from_payload([payload] * 5)


def test_aggregate_cache_roundtrip_and_mode_version_boundaries() -> None:
    """缓存身份包含模式/版本，完整来源快照 roundtrip 且每次请求 ID 不改变计划身份。"""
    evidence = _evidence()
    boundary = CacheBoundary(
        security_scope_id="a" * 64, knowledge_base_revisions_hash="b" * 64,
        retrieval_profile="hybrid-structure-aware@6", prompt_profile=AGGREGATE_PROFILE_REF,
        model_profile="answer-openai-compatible@4", language="en-US", history_hash="none",
        evidence_mode="community", aggregate_profile_ref=AGGREGATE_PROFILE_REF,
    )
    entry = CacheEntry(
        cache_id=cache_id(boundary, "Atlas?"), boundary=boundary, normalized_question="Atlas?",
        answer="Atlas connects the two sources. [A1]", citations=(), aggregate_evidence=(evidence,),
    )
    assert _entry(json.loads(json.dumps(_entry_json(entry))), None) == entry
    assert cache_id(replace(boundary, evidence_mode="original"), "Atlas?") != entry.cache_id
    changed_profile = replace(boundary, aggregate_profile_ref="different@1")
    assert cache_id(changed_profile, "Atlas?") != entry.cache_id
    replay = replace(evidence, aggregate_id=uuid4(), label="A2")
    assert fingerprint((evidence,)) == fingerprint((replay,))
    assert fingerprint((evidence,)) != fingerprint((replace(evidence, graph_generation=3),))
    assert fingerprint((evidence,)) != fingerprint((replace(evidence, summary="Changed summary"),))


@pytest.mark.parametrize("answer", ["No label", "Only original [C1]", "Bad [A0]", "Bad [A2]"])
def test_community_answer_requires_a_valid_aggregate_label(answer: str) -> None:
    assert not labels_valid(answer, (), (_evidence(),))


def test_evidence_mode_http_defaults_to_original_and_rejects_unknown_mode() -> None:
    assert AnswerRequest(query="Synthetic question").evidence_mode == "original"
    with pytest.raises(ValueError):
        AnswerRequest.model_validate({"query": "Synthetic", "evidence_mode": "native"})


@pytest.mark.asyncio
async def test_redis_community_roundtrip_and_corrupt_source_are_safe_misses(
    service_settings: ServiceSettings,
) -> None:
    """真实缓存适配器的 JSON/Hash 两种格式都必须携带并严格恢复聚合来源。"""
    evidence = _evidence()
    boundary = CacheBoundary(
        security_scope_id="a" * 64, knowledge_base_revisions_hash="b" * 64,
        retrieval_profile="hybrid-structure-aware@6", prompt_profile=AGGREGATE_PROFILE_REF,
        model_profile="answer-openai-compatible@4", language="en-US", history_hash="none",
        evidence_mode="community", aggregate_profile_ref=AGGREGATE_PROFILE_REF,
    )
    entry = CacheEntry(
        cache_id=cache_id(boundary, "Atlas?"), boundary=boundary, normalized_question="Atlas?",
        answer="Atlas connects the two sources. [A1]", citations=(), aggregate_evidence=(evidence,),
    )
    redis = FakeRedis()
    cache = RedisAnswerCache(redis, FakeEmbedding(1024), service_settings.cache)
    await cache.put(entry)
    exact_key = f"{service_settings.cache.key_prefix}:exact:{entry.cache_id}"
    hash_key = f"{service_settings.cache.semantic.key_prefix}{entry.cache_id}"
    stored = redis.hashes[hash_key]
    redis.search_result = _semantic_search_result(stored)
    exact = await cache.get_exact(boundary, "Atlas?")
    semantic = await cache.get_semantic(boundary, "Atlas question")
    assert exact is not None and exact.aggregate_evidence == (evidence,)
    assert len(semantic) == 1 and semantic[0].aggregate_evidence == (evidence,)
    query = redis.commands[-1][2]
    assert "@evidence_mode:{community}" in query and "@aggregate_profile_ref:" in query
    corrupt_exact = json.loads(redis.values[exact_key])
    corrupt_exact["aggregate_evidence"][0]["sources"][0]["quote_sha256"] = "0" * 64
    redis.values[exact_key] = json.dumps(corrupt_exact)
    corrupt_hash = dict(stored)
    answer = json.loads(corrupt_hash["answer_json"])
    answer["aggregate_evidence"][0]["sources"][0]["document_revision"] = True
    corrupt_hash["answer_json"] = json.dumps(answer)
    redis.search_result = _semantic_search_result(corrupt_hash)
    assert await cache.get_exact(boundary, "Atlas?") is None
    assert await cache.get_semantic(boundary, "Atlas question") == ()
    corrupt_summary = _entry_json(entry)
    corrupt_summary["aggregate_evidence"][0]["summary"] = "An unrelated tampered summary."
    redis.values[exact_key] = json.dumps(corrupt_summary)
    assert await cache.get_exact(boundary, "Atlas?") is None


@pytest.mark.asyncio
async def test_query_repository_add_update_and_read_preserve_aggregate_snapshot() -> None:
    """Repository 通过正常审计入口保存来源快照，不能只在 HTTP 返回临时证据。"""
    evidence = _evidence()
    record = QueryRecord(
        security_scope_id="a" * 64, request_id=uuid4(), query_sha256="b" * 64,
        knowledge_base_ids=(evidence.knowledge_base_id,),
        retrieval_profile_ref="hybrid-structure-aware@6", evidence_mode="community",
        aggregate_profile_ref=AGGREGATE_PROFILE_REF, aggregate_evidence=(evidence,),
    )
    session = AsyncMock(spec=AsyncSession)
    repository = SqlAlchemyQueryRepository(session)
    await repository.add(record.security_scope_id, record)
    row = session.add.call_args.args[0]
    assert row.evidence_mode == "community" and row.aggregate_profile_ref == AGGREGATE_PROFILE_REF
    assert _query_to_domain(row).aggregate_evidence == (evidence,)
    await repository.update(record.security_scope_id, record)
    statement = session.execute.call_args.args[0]
    values = statement.compile().params
    assert values["evidence_mode"] == "community"
    assert values["aggregate_profile_ref"] == AGGREGATE_PROFILE_REF
    assert values["aggregate_evidence_json"] == [to_payload(evidence)]


@pytest.mark.asyncio
async def test_citation_owner_sql_lookup_always_contains_the_callers_scope() -> None:
    """实际 SQL 同时绑定引用 ID 与主体范围，不能只靠应用层先查一次权限。"""
    session = AsyncMock(spec=AsyncSession)
    request_id, citation_id = uuid4(), uuid4()
    session.scalar.return_value = request_id
    repository = SqlAlchemyQueryRepository(session)
    assert await repository.get_citation_request_id("a" * 64, citation_id) == request_id
    statement = session.scalar.call_args.args[0]
    assert list(statement.selected_columns.keys()) == ["request_id"]
    assert statement.compile().params == {
        "security_scope_id_1": "a" * 64,
        "id_1": citation_id,
    }
