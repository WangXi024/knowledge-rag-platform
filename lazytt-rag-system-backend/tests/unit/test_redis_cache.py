"""Redis 答案缓存测试：精确/语义命中、边界过滤、向量字节、序列化和异常数据。"""

from __future__ import annotations

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
        "expires_at_ms",
    ):
        assert f"@{field}:" in query


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
