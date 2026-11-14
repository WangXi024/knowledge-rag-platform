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
    """使用独立的新一代测试索引，不读取或改变任何历史运行数据。"""
    return SemanticCacheSection(
        key_prefix="rag-cache:semantic:v4:",
        index_name="rag_semantic_cache_v4",
        index_schema_version=4,
        embedding_model="test-embedding",
        embedding_dimension=128,
    )


def _boundary_attributes() -> list[list[Any]]:
    """模拟真实 FT.INFO 的完整 TAG 字段，证明索引能执行所有缓存隔离条件。"""
    return [
        ["identifier", name, "attribute", name, "type", "TAG"]
        for name in (
            "security_scope_id", "knowledge_base_revisions_hash", "retrieval_profile",
            "prompt_profile", "model_profile", "language", "history_hash",
            "filter_hash", "graph_version_hash", "graph_mode",
            "evidence_mode", "aggregate_profile_ref",
        )
    ]


@pytest.mark.asyncio
async def test_cache_bootstrap_creates_missing_index() -> None:
    client = FakeRedis()

    result = await RedisSemanticIndexBootstrap(client, _settings()).ensure()

    assert result.created is True
    assert result.dimension == 128
    create = next(command for command in client.commands if command[0] == "FT.CREATE")
    assert "security_scope_id" in create
    assert "knowledge_base_revisions_hash" in create
    for name in (
        "filter_hash", "graph_version_hash", "graph_mode", "evidence_mode", "aggregate_profile_ref",
    ):
        assert create[create.index(name) + 1] == "TAG"
    assert create[create.index("DIM") + 1] == 128
    metadata = next(command for command in client.commands if command[0] == "HSET")
    assert "test-embedding" in metadata
    assert 128 in metadata


@pytest.mark.asyncio
async def test_cache_bootstrap_accepts_matching_index() -> None:
    client = FakeRedis(
        [
            "index_definition",
            ["prefixes", ["rag-cache:semantic:v4:"]],
            "attributes",
            [
                *_boundary_attributes(),
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
            "4",
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
            ["prefixes", ["rag-cache:semantic:v4:"]],
            "attributes",
            [
                *_boundary_attributes(),
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
            "4",
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
            ["prefixes", ["rag-cache:semantic:v4:"]],
            "attributes",
            [
                *_boundary_attributes(),
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
            "4",
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


@pytest.mark.parametrize(
    "missing_field",
    ["filter_hash", "graph_version_hash", "graph_mode", "evidence_mode", "aggregate_profile_ref"],
)
@pytest.mark.asyncio
async def test_cache_bootstrap_rejects_missing_boundary_tag_without_mutating_index(
    missing_field: str,
) -> None:
    """名称和向量参数看似正确、却缺隔离标签的旧索引必须启动失败，不能原地修补。

    只模拟 Redis 的描述响应；验证不会发送 ALTER、DROP 或 CREATE，避免升级时改变
    既有索引内容，更不能带着缺失的过滤语义进入服务。
    """
    vector = [
        "identifier", "embedding", "attribute", "embedding", "algorithm", "HNSW",
        "data_type", "FLOAT32", "dim", 128, "distance_metric", "COSINE",
    ]
    attributes = [item for item in _boundary_attributes() if item[1] != missing_field]
    client = FakeRedis(
        ["index_definition", ["prefixes", ["rag-cache:semantic:v4:"]],
         "attributes", [*attributes, vector]],
        ["index_schema_version", "4", "embedding_model", "test-embedding",
         "embedding_dimension", "128", "vector_data_type", "FLOAT32"],
    )

    with pytest.raises(CacheIndexMismatchError, match=missing_field):
        await RedisSemanticIndexBootstrap(client, _settings()).ensure()

    assert all(command[0] in {"FT.INFO", "HGETALL"} for command in client.commands)
