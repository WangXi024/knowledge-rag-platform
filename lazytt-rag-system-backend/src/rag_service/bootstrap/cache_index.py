"""Redis Search 语义缓存向量索引的创建和严格契约验证。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from redis.exceptions import ResponseError

from rag_service.domain.errors import CacheIndexMismatchError
from rag_service.settings import SemanticCacheSection


class RedisCommandClient(Protocol):
    """Bootstrap 所需的最小 Redis 原生命令接口。"""

    async def execute_command(self, *args: Any) -> Any:
        """按给定参数执行一条 Redis 原生命令，并原样返回客户端响应。"""
        ...


@dataclass(frozen=True, slots=True)
class CacheIndexResult:
    """索引是否新建、索引名和向量维度的启动结果。"""
    created: bool
    index_name: str
    dimension: int


def _decode(value: Any) -> Any:
    """递归把 Redis 返回的 bytes 容器转换为普通字符串容器。"""
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if isinstance(value, list):
        return [_decode(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_decode(item) for item in value)
    if isinstance(value, dict):
        return {_decode(key): _decode(item) for key, item in value.items()}
    return value


def _pairs_to_dict(value: Any) -> dict[str, Any]:
    """兼容 Redis 客户端返回字典或交替键值列表的两种响应形式。"""
    decoded = _decode(value)
    if isinstance(decoded, dict):
        return decoded
    if not isinstance(decoded, list) or len(decoded) % 2:
        raise CacheIndexMismatchError("FT.INFO 返回格式无法识别")
    return {str(decoded[index]): decoded[index + 1] for index in range(0, len(decoded), 2)}


def _find_vector_attribute(info: dict[str, Any], field_name: str) -> dict[str, Any]:
    """从 FT.INFO attributes 中定位配置的向量字段。"""
    raw_attributes = info.get("attributes")
    if not isinstance(raw_attributes, list):
        raise CacheIndexMismatchError("索引缺少 attributes")
    for raw in raw_attributes:
        attribute = _pairs_to_dict(raw)
        if attribute.get("attribute") == field_name or attribute.get("identifier") == field_name:
            return attribute
    raise CacheIndexMismatchError(f"索引缺少向量字段 {field_name}")


class RedisSemanticIndexBootstrap:
    """确保索引存在且 Prefix、算法、类型、维度、距离和模型元数据完全匹配。"""
    def __init__(self, client: RedisCommandClient, settings: SemanticCacheSection) -> None:
        """保存 Redis 命令客户端和期望索引契约。"""
        self._client = client
        self._settings = settings

    async def ensure(self) -> CacheIndexResult:
        """索引不存在则创建并写元数据；存在则只验证，绝不原地修改不兼容索引。"""
        try:
            raw_info = await self._client.execute_command("FT.INFO", self._settings.index_name)
        except ResponseError as exc:
            if "unknown index" not in str(exc).lower() and "no such index" not in str(exc).lower():
                raise
            await self._create()
            await self._write_metadata()
            return CacheIndexResult(
                created=True,
                index_name=self._settings.index_name,
                dimension=self._settings.embedding_dimension,
            )
        self._validate(_pairs_to_dict(raw_info))
        raw_metadata = await self._client.execute_command("HGETALL", self._metadata_key())
        self._validate_metadata(_pairs_to_dict(raw_metadata))
        return CacheIndexResult(
            created=False,
            index_name=self._settings.index_name,
            dimension=self._settings.embedding_dimension,
        )

    async def validate_existing(self) -> CacheIndexResult:
        """只读验证运行期索引，缺失也视为启动错误。"""
        try:
            raw_info = await self._client.execute_command("FT.INFO", self._settings.index_name)
        except ResponseError as exc:
            if "unknown index" in str(exc).lower() or "no such index" in str(exc).lower():
                raise CacheIndexMismatchError(
                    f"语义缓存索引 {self._settings.index_name} 不存在"
                ) from exc
            raise
        self._validate(_pairs_to_dict(raw_info))
        raw_metadata = await self._client.execute_command("HGETALL", self._metadata_key())
        self._validate_metadata(_pairs_to_dict(raw_metadata))
        return CacheIndexResult(
            created=False,
            index_name=self._settings.index_name,
            dimension=self._settings.embedding_dimension,
        )

    async def _create(self) -> None:
        """创建 HASH 上的字段化 HNSW 向量索引和全部缓存边界 TAG 字段。"""
        s = self._settings
        await self._client.execute_command(
            "FT.CREATE",
            s.index_name,
            "ON",
            "HASH",
            "PREFIX",
            1,
            s.key_prefix,
            "SCHEMA",
            "cache_id",
            "TAG",
            "security_scope_id",
            "TAG",
            "knowledge_base_revisions_hash",
            "TAG",
            "retrieval_profile",
            "TAG",
            "prompt_profile",
            "TAG",
            "model_profile",
            "TAG",
            "language",
            "TAG",
            "history_hash",
            "TAG",
            "created_at_ms",
            "NUMERIC",
            "expires_at_ms",
            "NUMERIC",
            s.vector_field,
            "VECTOR",
            s.algorithm,
            10,
            "TYPE",
            s.vector_data_type,
            "DIM",
            s.embedding_dimension,
            "DISTANCE_METRIC",
            s.distance_metric.upper(),
            "M",
            s.hnsw_m,
            "EF_CONSTRUCTION",
            s.hnsw_ef_construction,
        )

    def _metadata_key(self) -> str:
        """返回独立保存索引 Schema/模型身份的 Redis Hash 键。"""
        return f"rag-cache:index-metadata:{self._settings.index_name}"

    async def _write_metadata(self) -> None:
        """记录 FT.INFO 不包含的模型名和显式 Schema 版本。"""
        s = self._settings
        await self._client.execute_command(
            "HSET",
            self._metadata_key(),
            "index_schema_version",
            s.index_schema_version,
            "embedding_model",
            s.embedding_model,
            "embedding_dimension",
            s.embedding_dimension,
            "vector_data_type",
            s.vector_data_type,
        )

    def _validate(self, info: dict[str, Any]) -> None:
        """逐项对比 FT.INFO 的 Prefix 与向量字段参数。"""
        s = self._settings
        prefixes = info.get("index_definition")
        if isinstance(prefixes, list):
            definition = _pairs_to_dict(prefixes)
            prefixes = definition.get("prefixes")
        decoded_prefixes = _decode(prefixes)
        if decoded_prefixes not in ([s.key_prefix], (s.key_prefix,), s.key_prefix):
            raise CacheIndexMismatchError("Prefix 不一致")

        attribute = _find_vector_attribute(info, s.vector_field)
        attributes = attribute.get("attributes", [])
        vector_options = _pairs_to_dict(attributes) if attributes else attribute
        expected = {
            "algorithm": s.algorithm,
            "data_type": s.vector_data_type,
            "dim": s.embedding_dimension,
            "distance_metric": s.distance_metric.upper(),
        }
        actual = {
            "algorithm": str(
                vector_options.get("algorithm", attribute.get("algorithm", ""))
            ).upper(),
            "data_type": str(
                vector_options.get("data_type", attribute.get("data_type", ""))
            ).upper(),
            "dim": int(vector_options.get("dim", attribute.get("dim", -1))),
            "distance_metric": str(
                vector_options.get("distance_metric", attribute.get("distance_metric", ""))
            ).upper(),
        }
        for name, expected_value in expected.items():
            if actual[name] != expected_value:
                raise CacheIndexMismatchError(f"{name} 期望 {expected_value}，实际 {actual[name]}")

    def _validate_metadata(self, metadata: dict[str, Any]) -> None:
        """核对索引版本、模型、维度和类型，变化时要求创建新版本索引。"""
        s = self._settings
        if not metadata:
            raise CacheIndexMismatchError("索引缺少模型版本元数据；请创建新的 index_schema_version")
        expected = {
            "index_schema_version": str(s.index_schema_version),
            "embedding_model": s.embedding_model,
            "embedding_dimension": str(s.embedding_dimension),
            "vector_data_type": s.vector_data_type,
        }
        actual = {name: str(metadata.get(name, "")) for name in expected}
        for name, expected_value in expected.items():
            if actual[name] != expected_value:
                raise CacheIndexMismatchError(
                    f"{name} 期望 {expected_value}，实际 {actual[name]}；"
                    "模型或维度变化必须使用新的索引版本"
                )
