"""Redis 答案缓存测试：精确/语义命中、边界过滤、向量字节、序列化和异常数据。"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any
from uuid import uuid4

import pytest

from rag_service.domain.cache import cache_id
from rag_service.domain.models import Citation, SourcePosition
from rag_service.domain.ports import CacheBoundary, CacheEntry, LlmUsage
from rag_service.infrastructure.cache.redis_cache import RedisAnswerCache
from rag_service.settings import ServiceSettings


class FakeEmbedding:
    def __init__(self, dimension: int) -> None:
        self.dimension = dimension
        self.calls: list[tuple[str, ...]] = []

    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        self.calls.append(texts)
        return (tuple(0.01 for _ in range(self.dimension)),)


class FakePipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self.redis = redis
        self.operations: list[tuple[str, Any]] = []

    def hset(self, name: str, mapping: dict[str, Any]) -> FakePipeline:
        self.operations.append(("hset", (name, mapping)))
        return self

    def expire(self, name: str, seconds: int) -> FakePipeline:
        self.operations.append(("expire", (name, seconds)))
        return self

    async def execute(self) -> list[bool]:
        for operation, arguments in self.operations:
            if operation == "hset":
                name, mapping = arguments
                self.redis.hashes[name] = mapping
            else:
                name, seconds = arguments
                self.redis.ttls[name] = seconds
        return [True for _ in self.operations]


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, Any] = {}
        self.hashes: dict[str, dict[str, Any]] = {}
        self.ttls: dict[str, int] = {}
        self.search_result: list[Any] = [0]
        self.commands: list[tuple[Any, ...]] = []

    async def get(self, key: str) -> Any:
        return self.values.get(key)

    async def set(self, key: str, value: Any, *, ex: int) -> bool:
        self.values[key] = value
        self.ttls[key] = ex
        return True

    async def hset(self, key: str, mapping: dict[str, Any]) -> bool:
        self.hashes.setdefault(key, {}).update(mapping)
        return True

    async def execute_command(self, *args: Any) -> Any:
        self.commands.append(args)
        return self.search_result

    def pipeline(self, transaction: bool = True) -> FakePipeline:
        assert transaction is True
        return FakePipeline(self)


def _boundary(scope: str = "a" * 64) -> CacheBoundary:
    return CacheBoundary(
        security_scope_id=scope,
        knowledge_base_revisions_hash="b" * 64,
        retrieval_profile="hybrid-default@1",
        prompt_profile="rag-answer-zh@1",
        model_profile="answer-default@1",
        language="zh-CN",
        history_hash="c" * 64,
    )


def _entry(boundary: CacheBoundary) -> CacheEntry:
    question = "合成问题"
    return CacheEntry(
        cache_id=cache_id(boundary, question),
        boundary=boundary,
        normalized_question=question,
        answer="合成答案 [C1]",
        original_query="它会怎样？",
        effective_query=question,
        query_language="zh-CN",
        query_variants={"zh-CN": "它会怎样?", "en-US": "What happens?"},
        knowledge_languages=("zh-CN", "en-US"),
        retrieval_routes=("vector:original:zh-CN", "term:en-US"),
        query_type="multi_hop",
        retrieval_top_k=80,
        rerank_top_n=16,
        context_top_n=8,
        citations=(
            Citation(
                citation_id=uuid4(),
                label="C1",
                knowledge_base_id=uuid4(),
                document_id=uuid4(),
                document_name="fixture.txt",
                engine_chunk_id="engine-chunk",
                quote="synthetic",
                page_start=1,
                page_end=1,
                positions=(SourcePosition(page=1),),
                score=0.9,
            ),
        ),
        usage=LlmUsage(10, 5, 15),
    )


@pytest.mark.asyncio
async def test_exact_and_semantic_upsert_are_versioned_atomic_and_ttl_bound(
    service_settings: ServiceSettings,
) -> None:
    redis = FakeRedis()
    embedding = FakeEmbedding(1024)
    adapter = RedisAnswerCache(redis, embedding, service_settings.cache)
    value = _entry(_boundary())

    await adapter.put(value)
    replay = await adapter.get_exact(value.boundary, value.normalized_question)

    assert replay is not None
    assert replay.answer == value.answer
    assert replay.original_query == value.original_query
    assert replay.effective_query == value.effective_query
    assert replay.query_language == value.query_language
    assert replay.query_variants == value.query_variants
    assert replay.knowledge_languages == value.knowledge_languages
    assert replay.retrieval_routes == value.retrieval_routes
    assert replay.query_type == value.query_type
    assert replay.retrieval_top_k == value.retrieval_top_k
    assert replay.rerank_top_n == value.rerank_top_n
    assert replay.context_top_n == value.context_top_n
    assert replay.boundary == value.boundary
    semantic_key = f"{service_settings.cache.semantic.key_prefix}{value.cache_id}"
    assert len(redis.hashes[semantic_key]["embedding"]) == 1024 * 4
    assert redis.ttls[semantic_key] == service_settings.cache.ttl_seconds
    assert redis.ttls[f"{service_settings.cache.key_prefix}:exact:{value.cache_id}"] == (
        service_settings.cache.ttl_seconds
    )


@pytest.mark.asyncio
async def test_semantic_query_has_all_mandatory_filters_and_rejects_other_scope(
    service_settings: ServiceSettings,
) -> None:
    redis = FakeRedis()
    adapter = RedisAnswerCache(
        redis,
        FakeEmbedding(1024),
        service_settings.cache,
    )
    attacker = _entry(_boundary("b" * 64))
    await adapter.put(attacker)
    stored = redis.hashes[f"{service_settings.cache.semantic.key_prefix}{attacker.cache_id}"]
    fields: list[Any] = []
    for name in (
        "cache_id",
        "security_scope_id",
        "knowledge_base_revisions_hash",
        "retrieval_profile",
        "prompt_profile",
        "model_profile",
        "language",
        "history_hash",
        "filter_hash",
        "graph_version_hash",
        "graph_mode",
        "evidence_mode",
        "aggregate_profile_ref",
        "normalized_question",
        "answer_json",
        "citations_json",
    ):
        fields.extend((name, stored[name]))
    fields.extend(("vector_distance", "0.01"))
    redis.search_result = [1, "attacker-key", fields]

    result = await adapter.get_semantic(_boundary("a" * 64), "相似问题")

    assert result == ()
    query = str(redis.commands[-1][2])
    for field in (
        "security_scope_id",
        "knowledge_base_revisions_hash",
        "retrieval_profile",
        "prompt_profile",
        "model_profile",
        "language",
        "history_hash",
        "filter_hash",
        "graph_version_hash",
        "graph_mode",
        "evidence_mode",
        "aggregate_profile_ref",
        "expires_at_ms",
    ):
        assert f"@{field}:" in query


def _semantic_search_result(stored: dict[str, Any]) -> list[Any]:
    """把一个缓存 Hash 模拟成检索命中，允许测试伪造越界或旧格式的下游响应。

    这里只排除体积大的向量；保留字段原值，以验证适配器会重新核验边界，不能只相信
    Redis 已经执行了 TAG 过滤。不会访问真实 Redis 或业务文件。
    """
    fields: list[Any] = []
    for name, value in stored.items():
        if name != "embedding":
            fields.extend((name, value))
    fields.extend(("vector_distance", "0.01"))
    return [1, "synthetic-cache-key", fields]


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("filter_hash", "d" * 64), ("graph_version_hash", "e" * 64), ("graph_mode", "native"),
        ("evidence_mode", "community"),
        ("aggregate_profile_ref", "rag-answer-community-localized@1"),
    ],
)
@pytest.mark.asyncio
async def test_filter_and_graph_boundaries_isolate_exact_and_semantic_answers(
    service_settings: ServiceSettings,
    field: str,
    changed: str,
) -> None:
    """相同问题只要过滤范围、图谱版本或模式变化，两级缓存均不得返回旧答案。

    语义结果刻意返回旧边界的高相似度记录，验证协议层仍会拒绝不符边界，防止错误
    索引或旧记录绕过过滤。精确缓存则必须使用不同键，避免覆盖同问题的另一份答案。
    """
    redis = FakeRedis()
    adapter = RedisAnswerCache(redis, FakeEmbedding(1024), service_settings.cache)
    original = _entry(_boundary())
    different_boundary = replace(original.boundary, **{field: changed})

    assert cache_id(different_boundary, original.normalized_question) != original.cache_id
    await adapter.put(original)
    stored = redis.hashes[f"{service_settings.cache.semantic.key_prefix}{original.cache_id}"]
    redis.search_result = _semantic_search_result(stored)

    assert await adapter.get_exact(different_boundary, original.normalized_question) is None
    assert await adapter.get_semantic(different_boundary, "近似但范围不同的问题") == ()


@pytest.mark.asyncio
async def test_extended_cache_boundary_roundtrips_in_both_storage_formats(
    service_settings: ServiceSettings,
) -> None:
    """非默认过滤摘要、图版本和模式能在 JSON 与 Hash 两种格式中完整往返。"""
    redis = FakeRedis()
    adapter = RedisAnswerCache(redis, FakeEmbedding(1024), service_settings.cache)
    boundary = replace(
        _boundary(), filter_hash="d" * 64, graph_version_hash="e" * 64, graph_mode="native"
    )
    value = _entry(boundary)
    await adapter.put(value)
    stored = redis.hashes[f"{service_settings.cache.semantic.key_prefix}{value.cache_id}"]
    redis.search_result = _semantic_search_result(stored)

    exact = await adapter.get_exact(boundary, value.normalized_question)
    semantic = await adapter.get_semantic(boundary, "相似问题")

    assert exact is not None and exact.boundary == boundary
    assert len(semantic) == 1 and semantic[0].boundary == boundary
    for field in (
        "filter_hash", "graph_version_hash", "graph_mode", "evidence_mode", "aggregate_profile_ref"
    ):
        assert stored[field] == getattr(boundary, field)
    command = redis.commands[-1]
    for field in (
        "filter_hash", "graph_version_hash", "graph_mode", "evidence_mode", "aggregate_profile_ref"
    ):
        assert field in command[command.index("RETURN") + 2 : command.index("DIALECT")]


@pytest.mark.parametrize(
    "missing_field",
    ["filter_hash", "graph_version_hash", "graph_mode", "evidence_mode", "aggregate_profile_ref"],
)
@pytest.mark.asyncio
async def test_legacy_cache_without_complete_extended_boundary_is_a_safe_miss(
    service_settings: ServiceSettings,
    missing_field: str,
) -> None:
    """旧缓存没有过滤/图谱边界时安全未命中，不为缺失值补默认后误认成当前答案。

    即使旧 JSON 被错误放在新键下，或旧 Hash 被搜索服务意外返回，也应逐条失效，
    不导致本次问题报错；测试只在内存中删除合成字段。
    """
    redis = FakeRedis()
    adapter = RedisAnswerCache(redis, FakeEmbedding(1024), service_settings.cache)
    value = _entry(_boundary())
    await adapter.put(value)
    exact_key = f"{service_settings.cache.key_prefix}:exact:{value.cache_id}"
    payload = json.loads(redis.values[exact_key])
    del payload["boundary"][missing_field]
    redis.values[exact_key] = json.dumps(payload)
    stored = dict(redis.hashes[f"{service_settings.cache.semantic.key_prefix}{value.cache_id}"])
    del stored[missing_field]
    redis.search_result = _semantic_search_result(stored)

    assert await adapter.get_exact(value.boundary, value.normalized_question) is None
    assert await adapter.get_semantic(value.boundary, "旧格式的相似问题") == ()


@pytest.mark.asyncio
async def test_embedding_dimension_mismatch_fails_closed(
    service_settings: ServiceSettings,
) -> None:
    adapter = RedisAnswerCache(
        FakeRedis(),
        FakeEmbedding(3),
        service_settings.cache,
    )

    with pytest.raises(ValueError, match="expected dimension 1024"):
        await adapter.put(_entry(_boundary()))
