"""真实 Redis Stack 集成测试：向量索引、范围隔离、原子覆盖、TTL 和版本边界。"""

from __future__ import annotations

import asyncio
import os
from dataclasses import replace
from uuid import uuid4

import pytest
import pytest_asyncio
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from rag_service.bootstrap.cache_index import RedisSemanticIndexBootstrap
from rag_service.domain.cache import cache_id
from rag_service.domain.models import Citation
from rag_service.domain.ports import CacheBoundary, CacheEntry
from rag_service.infrastructure.cache.redis_cache import RedisAnswerCache
from rag_service.settings import CacheSection, SemanticCacheSection


class DeterministicEmbedding:
    async def embed(self, texts: tuple[str, ...]) -> tuple[tuple[float, ...], ...]:
        return tuple(
            tuple(1.0 if index == len(text) % 1024 else 0.0 for index in range(1024))
            for text in texts
        )


@pytest_asyncio.fixture
async def redis_cache_fixture() -> tuple[Redis, CacheSection]:
    url = os.getenv("TEST_CACHE_REDIS_URL")
    if not url:
        pytest.skip("TEST_CACHE_REDIS_URL is not configured")
    suffix = uuid4().hex
    settings = CacheSection(
        url=url,
        key_prefix=f"rag-cache:test:{suffix}",
        ttl_seconds=2,
        semantic=SemanticCacheSection(
            key_prefix=f"rag-cache:test:{suffix}:semantic:v91:",
            index_name=f"rag_semantic_cache_test_{suffix}_v91",
            index_schema_version=91,
            embedding_model="synthetic-embedding",
            embedding_dimension=1024,
            similarity_threshold=0.99,
        ),
    )
    client = Redis.from_url(url, decode_responses=False)
    try:
        await client.ping()
        yield client, settings
    finally:
        try:
            await client.execute_command("FT.DROPINDEX", settings.semantic.index_name, "DD")
        except ResponseError:
            pass
        exact_keys = await client.keys(f"{settings.key_prefix}:exact:*")
        metadata_key = f"rag-cache:index-metadata:{settings.semantic.index_name}"
        if exact_keys:
            await client.delete(*exact_keys)
        await client.delete(metadata_key)
        await client.aclose()


def _boundary(scope: str) -> CacheBoundary:
    return CacheBoundary(
        security_scope_id=scope,
        knowledge_base_revisions_hash="b" * 64,
        retrieval_profile="hybrid-default@1",
        prompt_profile="rag-answer-zh@1",
        model_profile="answer-default@1",
        language="zh-CN",
        history_hash="c" * 64,
    )


def _entry(boundary: CacheBoundary, question: str = "synthetic cache question") -> CacheEntry:
    return CacheEntry(
        cache_id=cache_id(boundary, question),
        boundary=boundary,
        normalized_question=question,
        answer="Synthetic verified answer [C1]",
        original_query="What about it?",
        effective_query=question,
        query_language="en-US",
        query_variants={"zh-CN": "它会怎样?", "en-US": question},
        knowledge_languages=("zh-CN", "en-US"),
        retrieval_routes=("vector:original:en-US", "term:zh-CN", "term:en-US"),
        query_type="fact",
        retrieval_top_k=30,
        rerank_top_n=8,
        context_top_n=5,
        citations=(
            Citation(
                citation_id=uuid4(),
                label="C1",
                knowledge_base_id=uuid4(),
                document_id=uuid4(),
                document_name="fixture.txt",
                engine_chunk_id="fixture-chunk",
                quote="Synthetic evidence",
            ),
        ),
    )


@pytest.mark.redis
@pytest.mark.asyncio
async def test_real_redis_stack_scope_atomic_upsert_and_ttl(
    redis_cache_fixture: tuple[Redis, CacheSection],
) -> None:
    client, settings = redis_cache_fixture
    bootstrap = RedisSemanticIndexBootstrap(client, settings.semantic)
    assert (await bootstrap.ensure()).created is True
    assert (await bootstrap.ensure()).created is False
    cache = RedisAnswerCache(client, DeterministicEmbedding(), settings)
    boundary = _boundary("a" * 64)
    entry = _entry(boundary)

    await asyncio.gather(*(cache.put(entry) for _ in range(8)))

    semantic_key = f"{settings.semantic.key_prefix}{entry.cache_id}"
    fields = await client.hgetall(semantic_key)
    assert len(fields[b"embedding"]) == 1024 * 4
    assert fields[b"security_scope_id"] == boundary.security_scope_id.encode()
    assert await client.ttl(semantic_key) > 0
    exact = await cache.get_exact(boundary, entry.normalized_question)
    assert exact is not None
    assert exact.original_query == entry.original_query
    assert exact.effective_query == entry.effective_query
    assert exact.query_variants == entry.query_variants
    assert exact.knowledge_languages == entry.knowledge_languages
    assert exact.retrieval_routes == entry.retrieval_routes
    assert exact.query_type == entry.query_type
    assert exact.retrieval_top_k == entry.retrieval_top_k
    assert exact.rerank_top_n == entry.rerank_top_n
    assert exact.context_top_n == entry.context_top_n
    semantic = await cache.get_semantic(boundary, entry.normalized_question)
    assert len(semantic) == 1
    assert semantic[0].cache_id == entry.cache_id
    assert semantic[0].original_query == entry.original_query
    assert semantic[0].effective_query == entry.effective_query
    assert semantic[0].query_variants == entry.query_variants
    assert semantic[0].query_type == entry.query_type
    assert semantic[0].retrieval_top_k == entry.retrieval_top_k
    assert semantic[0].rerank_top_n == entry.rerank_top_n
    assert semantic[0].context_top_n == entry.context_top_n
    assert await cache.get_semantic(_boundary("d" * 64), entry.normalized_question) == ()

    ttl_before_hit = await client.ttl(semantic_key)
    await asyncio.sleep(0.2)
    await cache.mark_hit(entry)
    assert 0 < await client.ttl(semantic_key) <= ttl_before_hit

    await asyncio.sleep(2)
    assert await cache.get_exact(boundary, entry.normalized_question) is None
    assert await cache.get_semantic(boundary, entry.normalized_question) == ()


@pytest.mark.redis
@pytest.mark.asyncio
async def test_real_redis_stack_version_boundaries_do_not_mix(
    redis_cache_fixture: tuple[Redis, CacheSection],
) -> None:
    client, settings = redis_cache_fixture
    await RedisSemanticIndexBootstrap(client, settings.semantic).ensure()
    cache = RedisAnswerCache(client, DeterministicEmbedding(), settings)
    original = _entry(_boundary("e" * 64))
    await cache.put(original)

    changed_revision = replace(
        original.boundary,
        knowledge_base_revisions_hash="f" * 64,
    )
    changed_model = replace(original.boundary, model_profile="answer-default@2")

    assert await cache.get_semantic(changed_revision, original.normalized_question) == ()
    assert await cache.get_semantic(changed_model, original.normalized_question) == ()
