"""Redis 语义索引 Bootstrap 测试：创建命令、既有索引校验和不兼容契约拒绝。"""

from __future__ import annotations

from typing import Any

import pytest
from redis.exceptions import ResponseError

from rag_service.bootstrap.cache_index import RedisSemanticIndexBootstrap
from rag_service.domain.errors import CacheIndexMismatchError
from rag_service.settings import SemanticCacheSection


class FakeRedis:
    def __init__(
        self,
        info: Any = None,
        metadata: Any = None,
    ) -> None:
        self.info = info
        self.metadata = metadata
        self.commands: list[tuple[Any, ...]] = []

    async def execute_command(self, *args: Any) -> Any:
        self.commands.append(args)
        if args[0] == "FT.INFO":
            if self.info is None:
                raise ResponseError("Unknown index name")
            return self.info
        if args[0] == "HGETALL":
            return self.metadata
        return "OK"


def _settings() -> SemanticCacheSection:
    return SemanticCacheSection(
        embedding_model="test-embedding",
        embedding_dimension=128,
    )


@pytest.mark.asyncio
async def test_cache_bootstrap_creates_missing_index() -> None:
    client = FakeRedis()

    result = await RedisSemanticIndexBootstrap(client, _settings()).ensure()

    assert result.created is True
    assert result.dimension == 128
    create = next(command for command in client.commands if command[0] == "FT.CREATE")
    assert "security_scope_id" in create
    assert "knowledge_base_revisions_hash" in create
    assert create[create.index("DIM") + 1] == 128
    metadata = next(command for command in client.commands if command[0] == "HSET")
    assert "test-embedding" in metadata
    assert 128 in metadata


@pytest.mark.asyncio
async def test_cache_bootstrap_accepts_matching_index() -> None:
    client = FakeRedis(
        [
            "index_definition",
            ["prefixes", ["rag-cache:semantic:v1:"]],
            "attributes",
            [
                [
                    "identifier",
                    "embedding",
                    "attribute",
                    "embedding",
                    "algorithm",
                    "HNSW",
                    "data_type",
                    "FLOAT32",
                    "dim",
                    128,
                    "distance_metric",
                    "COSINE",
                ]
            ],
        ],
        [
            "index_schema_version",
            "1",
            "embedding_model",
            "test-embedding",
            "embedding_dimension",
            "128",
            "vector_data_type",
            "FLOAT32",
        ],
    )

    result = await RedisSemanticIndexBootstrap(client, _settings()).ensure()

    assert result.created is False


@pytest.mark.asyncio
async def test_cache_bootstrap_rejects_dimension_mismatch() -> None:
    client = FakeRedis(
        [
            "index_definition",
            ["prefixes", ["rag-cache:semantic:v1:"]],
            "attributes",
            [
                [
                    "identifier",
                    "embedding",
                    "attribute",
                    "embedding",
                    "algorithm",
                    "HNSW",
                    "data_type",
                    "FLOAT32",
                    "dim",
                    256,
                    "distance_metric",
                    "COSINE",
                ]
            ],
        ],
        [
            "index_schema_version",
            "1",
            "embedding_model",
            "test-embedding",
            "embedding_dimension",
            "128",
            "vector_data_type",
            "FLOAT32",
        ],
    )

    with pytest.raises(CacheIndexMismatchError):
        await RedisSemanticIndexBootstrap(client, _settings()).ensure()


@pytest.mark.asyncio
async def test_cache_bootstrap_rejects_model_change_without_new_index() -> None:
    client = FakeRedis(
        [
            "index_definition",
            ["prefixes", ["rag-cache:semantic:v1:"]],
            "attributes",
            [
                [
                    "identifier",
                    "embedding",
                    "attribute",
                    "embedding",
                    "algorithm",
                    "HNSW",
                    "data_type",
                    "FLOAT32",
                    "dim",
                    128,
                    "distance_metric",
                    "COSINE",
                ]
            ],
        ],
        [
            "index_schema_version",
            "1",
            "embedding_model",
            "different-model",
            "embedding_dimension",
            "128",
            "vector_data_type",
            "FLOAT32",
        ],
    )

    with pytest.raises(CacheIndexMismatchError, match="新的索引版本"):
        await RedisSemanticIndexBootstrap(client, _settings()).ensure()


@pytest.mark.asyncio
async def test_cache_validation_does_not_create_a_missing_index() -> None:
    client = FakeRedis()

    with pytest.raises(CacheIndexMismatchError, match="不存在"):
        await RedisSemanticIndexBootstrap(client, _settings()).validate_existing()

    assert not any(command[0] == "FT.CREATE" for command in client.commands)
