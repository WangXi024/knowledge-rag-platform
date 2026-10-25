"""Redis 精确答案缓存与 RediSearch 向量语义缓存实现。

精确缓存以完整边界和问题摘要直接 GET；语义缓存先生成问题向量，再用所有边界 TAG
过滤 KNN。写入时同时保存完整 JSON 和可搜索 Hash；任何反序列化都重新构造强类型
对象，不能把 Redis 内容直接信任为领域对象。
"""

from __future__ import annotations

import json
import re
import struct
import time
from typing import Any, Protocol
from uuid import UUID

from rag_service.domain.cache import cache_id
from rag_service.domain.models import Citation, SourcePosition
from rag_service.domain.ports import (
    CacheBoundary,
    CacheEntry,
    EmbeddingPort,
    LlmUsage,
)
from rag_service.settings import CacheSection

_TAG_SPECIAL = re.compile(r"([^A-Za-z0-9_])")
_QUERY_TYPES = frozenset({"fact", "list", "comparison", "summary", "multi_hop"})


class RedisPipeline(Protocol):
    """写入 Hash、TTL 所需的最小 Redis Pipeline 接口。"""

    def hset(self, name: str, mapping: dict[str, Any]) -> RedisPipeline:
        """把字段映射排入 Hash 写入队列，并返回管道自身以支持链式调用。"""
        ...

    def expire(self, name: str, seconds: int) -> RedisPipeline:
        """把指定键的秒级过期时间排入队列，并返回管道自身。"""
        ...

    async def execute(self) -> Any:
        """一次发送管道中积累的全部命令，并返回 Redis 客户端结果。"""
        ...


class RedisCacheClient(Protocol):
    """答案缓存所需的最小 Redis 字符串、Hash、命令与 Pipeline 接口。"""

    async def get(self, key: str) -> Any:
        """读取字符串键；键不存在时由具体客户端返回空值。"""
        ...

    async def set(self, key: str, value: Any, *, ex: int) -> Any:
        """写入字符串值，并用 ``ex`` 设置秒级生存时间。"""
        ...

    async def hset(self, key: str, mapping: dict[str, Any]) -> Any:
        """把一组字段写入用于 RediSearch 索引的 Hash。"""
        ...

    async def execute_command(self, *args: Any) -> Any:
        """执行 FT.SEARCH 等高级 Redis 原生命令并返回原始响应。"""
        ...

    def pipeline(self, transaction: bool = True) -> RedisPipeline:
        """创建写入管道；``transaction`` 指定命令是否按事务方式提交。"""
        ...


class RedisAnswerCache:
    """实现精确查询和带强边界过滤的向量相似查询。"""
    def __init__(
        self,
        client: RedisCacheClient,
        embedding: EmbeddingPort,
        settings: CacheSection,
    ) -> None:
        """注入 Redis、Embedding 端口和缓存/索引配置。"""
        self._client = client
        self._embedding = embedding
        self._settings = settings

    async def get_exact(
        self,
        boundary: CacheBoundary,
        normalized_question: str,
    ) -> CacheEntry | None:
        """计算精确 cache_id，读取 JSON 并确认记录边界仍与调用边界相等。"""
        if not self._settings.exact_enabled:
            return None
        identifier = cache_id(boundary, normalized_question)
        raw = await self._client.get(self._exact_key(identifier))
        if raw is None:
            return None
        value = _decode_json(raw)
        entry = _entry(value, similarity=None)
        if (
            entry.cache_id != identifier
            or entry.boundary != boundary
            or entry.normalized_question != normalized_question
        ):
            return None
        return entry

    async def get_semantic(
        self,
        boundary: CacheBoundary,
        normalized_question: str,
    ) -> tuple[CacheEntry, ...]:
        """对问题向量执行 KNN，并只返回超过相似度阈值且边界完全一致的记录。

        Redis 返回 cosine distance，本方法转换为 ``1 - distance`` 相似度；查询中的每个
        TAG 值都转义，避免租户/Profile 文本改变 RediSearch 查询结构。
        """
        if not self._settings.semantic_enabled:
            return ()
        vector = await self._embedding.embed((normalized_question,))
        query_vector = self._vector_bytes(vector)
        now_ms = int(time.time() * 1000)
        filter_query = " ".join(
            (
                f"@security_scope_id:{{{_escape_tag(boundary.security_scope_id)}}}",
                "@knowledge_base_revisions_hash:"
                f"{{{_escape_tag(boundary.knowledge_base_revisions_hash)}}}",
                f"@retrieval_profile:{{{_escape_tag(boundary.retrieval_profile)}}}",
                f"@prompt_profile:{{{_escape_tag(boundary.prompt_profile)}}}",
                f"@model_profile:{{{_escape_tag(boundary.model_profile)}}}",
                f"@language:{{{_escape_tag(boundary.language)}}}",
                f"@history_hash:{{{_escape_tag(boundary.history_hash)}}}",
                f"@expires_at_ms:[{now_ms} +inf]",
            )
        )
        query = (
            f"({filter_query})=>[KNN $top_k "
            f"@{self._settings.semantic.vector_field} $query_vector "
            "AS vector_distance]"
        )
        raw = await self._client.execute_command(
            "FT.SEARCH",
            self._settings.semantic.index_name,
            query,
            "PARAMS",
            4,
            "top_k",
            self._settings.semantic.top_k,
            "query_vector",
            query_vector,
            "SORTBY",
            "vector_distance",
            "RETURN",
            12,
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
            "vector_distance",
            "DIALECT",
            2,
        )
        entries: list[CacheEntry] = []
        for fields in _search_documents(raw):
            distance = float(_text(fields.get("vector_distance", "1")))
            similarity = 1.0 - distance
            if similarity < self._settings.semantic.similarity_threshold:
                continue
            entry = _entry_from_hash(fields, similarity)
            if entry.boundary != boundary:
                continue
            entries.append(entry)
        return tuple(entries)

    async def put(self, value: CacheEntry) -> None:
        """生成问题向量，并通过 Pipeline 原子式写入 Hash 字段和 TTL。"""
        identifier = cache_id(value.boundary, value.normalized_question)
        if identifier != value.cache_id:
            raise ValueError("cache_id does not match cache boundary")
        serialized = _entry_json(value)
        encoded = json.dumps(
            serialized,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        if len(encoded.encode("utf-8")) > self._settings.max_answer_bytes:
            raise ValueError("cache answer exceeds configured size")
        if self._settings.exact_enabled:
            await self._client.set(
                self._exact_key(identifier),
                encoded,
                ex=self._settings.ttl_seconds,
            )
        if not self._settings.semantic_enabled:
            return
        vector = await self._embedding.embed((value.normalized_question,))
        vector_bytes = self._vector_bytes(vector)
        now_ms = int(time.time() * 1000)
        expires_at_ms = now_ms + self._settings.ttl_seconds * 1000
        boundary = value.boundary
        key = f"{self._settings.semantic.key_prefix}{identifier}"
        pipeline = self._client.pipeline(transaction=True)
        pipeline.hset(
            key,
            mapping={
                "cache_id": identifier,
                "security_scope_id": boundary.security_scope_id,
                "knowledge_base_revisions_hash": boundary.knowledge_base_revisions_hash,
                "retrieval_profile": boundary.retrieval_profile,
                "prompt_profile": boundary.prompt_profile,
                "model_profile": boundary.model_profile,
                "language": boundary.language,
                "history_hash": boundary.history_hash,
                "normalized_question": value.normalized_question,
                "answer_json": json.dumps(
                    {
                        "answer": value.answer,
                        "original_query": value.original_query,
                        "effective_query": value.effective_query,
                        "query_language": value.query_language,
                        "query_variants": value.query_variants,
                        "knowledge_languages": list(value.knowledge_languages),
                        "retrieval_routes": list(value.retrieval_routes),
                        "query_type": value.query_type,
                        "retrieval_top_k": value.retrieval_top_k,
                        "rerank_top_n": value.rerank_top_n,
                        "context_top_n": value.context_top_n,
                        "warnings": list(value.warnings),
                        "usage": _usage_json(value.usage),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "citations_json": json.dumps(
                    [_citation_json(item) for item in value.citations],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "created_at_ms": now_ms,
                "expires_at_ms": expires_at_ms,
                "last_hit_at_ms": now_ms,
                self._settings.semantic.vector_field: vector_bytes,
            },
        ).expire(key, self._settings.ttl_seconds)
        await pipeline.execute()

    async def mark_hit(self, value: CacheEntry) -> None:
        """递增命中次数并记录最后命中时间；不改变原 TTL。"""
        await self._client.hset(
            f"{self._settings.semantic.key_prefix}{value.cache_id}",
            mapping={"last_hit_at_ms": int(time.time() * 1000)},
        )

    def _vector_bytes(
        self,
        vectors: tuple[tuple[float, ...], ...],
    ) -> bytes:
        """把有限、定长浮点向量编码为 RediSearch 需要的 FLOAT32 原始字节。"""
        if len(vectors) != 1:
            raise ValueError("embedding adapter returned unexpected batch size")
        vector = vectors[0]
        dimension = self._settings.semantic.embedding_dimension
        if len(vector) != dimension:
            raise ValueError(f"semantic cache expected dimension {dimension}, got {len(vector)}")
        return struct.pack(f"<{dimension}f", *vector)

    def _exact_key(self, identifier: str) -> str:
        """拼接配置的精确缓存键前缀和稳定 cache_id。"""
        return f"{self._settings.key_prefix}:exact:{identifier}"


def _escape_tag(value: str) -> str:
    """转义 RediSearch TAG 查询中的保留字符。"""
    return _TAG_SPECIAL.sub(r"\\\1", value)


def _text(value: Any) -> str:
    """把 Redis bytes/str 统一为文本，其他类型视为损坏数据。"""
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _decode_json(value: Any) -> dict[str, Any]:
    """解析必须为 JSON Object 的 Redis 字段。"""
    decoded = _text(value)
    result = json.loads(decoded)
    if not isinstance(result, dict):
        raise ValueError("cache payload must be an object")
    return result


def _search_documents(value: Any) -> tuple[dict[str, Any], ...]:
    """把 FT.SEARCH 交替返回的文档 ID/字段列表转换为字典序列。"""
    if not isinstance(value, list) or not value:
        return ()
    documents: list[dict[str, Any]] = []
    for index in range(2, len(value), 2):
        raw_fields = value[index]
        if not isinstance(raw_fields, list) or len(raw_fields) % 2:
            continue
        fields = {
            _text(raw_fields[offset]): raw_fields[offset + 1]
            for offset in range(0, len(raw_fields), 2)
        }
        documents.append(fields)
    return tuple(documents)


def _boundary(value: dict[str, Any]) -> CacheBoundary:
    """从持久化 JSON 重建完整缓存边界。"""
    return CacheBoundary(
        security_scope_id=str(value["security_scope_id"]),
        knowledge_base_revisions_hash=str(value["knowledge_base_revisions_hash"]),
        retrieval_profile=str(value["retrieval_profile"]),
        prompt_profile=str(value["prompt_profile"]),
        model_profile=str(value["model_profile"]),
        language=str(value["language"]),
        history_hash=str(value["history_hash"]),
    )


def _entry(value: dict[str, Any], similarity: float | None) -> CacheEntry:
    """从精确缓存 JSON 严格重建答案、引用、轨迹和用量。"""
    citations = value.get("citations")
    if not isinstance(citations, list):
        raise ValueError("cache citations must be an array")
    usage = value.get("usage")
    return CacheEntry(
        cache_id=str(value["cache_id"]),
        boundary=_boundary(value["boundary"]),
        normalized_question=str(value["normalized_question"]),
        answer=str(value["answer"]),
        citations=tuple(_citation(item) for item in citations),
        original_query=(
            str(value["original_query"])
            if value.get("original_query") is not None
            else None
        ),
        effective_query=(
            str(value["effective_query"])
            if value.get("effective_query") is not None
            else None
        ),
        query_language=(
            str(value["query_language"])
            if value.get("query_language") is not None
            else None
        ),
        query_variants={
            str(key): str(item)
            for key, item in value.get("query_variants", {}).items()
        }
        if isinstance(value.get("query_variants"), dict)
        else {},
        knowledge_languages=tuple(
            str(item) for item in value.get("knowledge_languages", [])
        ),
        retrieval_routes=tuple(str(item) for item in value.get("retrieval_routes", [])),
        query_type=_optional_query_type(value.get("query_type")),
        retrieval_top_k=_optional_positive_integer(value.get("retrieval_top_k")),
        rerank_top_n=_optional_positive_integer(value.get("rerank_top_n")),
        context_top_n=_optional_positive_integer(value.get("context_top_n")),
        warnings=tuple(str(item) for item in value.get("warnings", [])),
        usage=_usage(usage),
        similarity=similarity,
    )


def _entry_from_hash(value: dict[str, Any], similarity: float) -> CacheEntry:
    """从语义索引 Hash 字段重建条目，并附加本次 KNN 相似度。"""
    answer = _decode_json(value["answer_json"])
    citations_raw = json.loads(_text(value["citations_json"]))
    if not isinstance(citations_raw, list):
        raise ValueError("cache citations must be an array")
    return CacheEntry(
        cache_id=_text(value["cache_id"]),
        boundary=CacheBoundary(
            security_scope_id=_text(value["security_scope_id"]),
            knowledge_base_revisions_hash=_text(value["knowledge_base_revisions_hash"]),
            retrieval_profile=_text(value["retrieval_profile"]),
            prompt_profile=_text(value["prompt_profile"]),
            model_profile=_text(value["model_profile"]),
            language=_text(value["language"]),
            history_hash=_text(value["history_hash"]),
        ),
        normalized_question=_text(value["normalized_question"]),
        answer=str(answer["answer"]),
        citations=tuple(_citation(item) for item in citations_raw),
        original_query=(
            str(answer["original_query"])
            if answer.get("original_query") is not None
            else None
        ),
        effective_query=(
            str(answer["effective_query"])
            if answer.get("effective_query") is not None
            else None
        ),
        query_language=(
            str(answer["query_language"])
            if answer.get("query_language") is not None
            else None
        ),
        query_variants={
            str(key): str(item)
            for key, item in answer.get("query_variants", {}).items()
        }
        if isinstance(answer.get("query_variants"), dict)
        else {},
        knowledge_languages=tuple(
            str(item) for item in answer.get("knowledge_languages", [])
        ),
        retrieval_routes=tuple(
            str(item) for item in answer.get("retrieval_routes", [])
        ),
        query_type=_optional_query_type(answer.get("query_type")),
        retrieval_top_k=_optional_positive_integer(answer.get("retrieval_top_k")),
        rerank_top_n=_optional_positive_integer(answer.get("rerank_top_n")),
        context_top_n=_optional_positive_integer(answer.get("context_top_n")),
        warnings=tuple(str(item) for item in answer.get("warnings", [])),
        usage=_usage(answer.get("usage")),
        similarity=similarity,
    )


def _entry_json(value: CacheEntry) -> dict[str, Any]:
    """把强类型缓存条目转换为可 JSON 序列化结构。"""
    return {
        "cache_id": value.cache_id,
        "boundary": {
            "security_scope_id": value.boundary.security_scope_id,
            "knowledge_base_revisions_hash": value.boundary.knowledge_base_revisions_hash,
            "retrieval_profile": value.boundary.retrieval_profile,
            "prompt_profile": value.boundary.prompt_profile,
            "model_profile": value.boundary.model_profile,
            "language": value.boundary.language,
            "history_hash": value.boundary.history_hash,
        },
        "normalized_question": value.normalized_question,
        "answer": value.answer,
        "citations": [_citation_json(item) for item in value.citations],
        "original_query": value.original_query,
        "effective_query": value.effective_query,
        "query_language": value.query_language,
        "query_variants": value.query_variants,
        "knowledge_languages": list(value.knowledge_languages),
        "retrieval_routes": list(value.retrieval_routes),
        "query_type": value.query_type,
        "retrieval_top_k": value.retrieval_top_k,
        "rerank_top_n": value.rerank_top_n,
        "context_top_n": value.context_top_n,
        "warnings": list(value.warnings),
        "usage": _usage_json(value.usage),
    }


def _citation_json(value: Citation) -> dict[str, Any]:
    """把引用及位置转换为缓存 JSON。"""
    return {
        "citation_id": str(value.citation_id),
        "label": value.label,
        "knowledge_base_id": str(value.knowledge_base_id),
        "resource_scope_id": value.resource_scope_id,
        "document_id": str(value.document_id),
        "document_name": value.document_name,
        "engine_chunk_id": value.engine_chunk_id,
        "quote": value.quote,
        "page_start": value.page_start,
        "page_end": value.page_end,
        "positions": [{"page": item.page, "bbox": item.bbox} for item in value.positions],
        "score": value.score,
        "rerank_score": value.rerank_score,
    }


def _citation(value: Any) -> Citation:
    """严格校验并从缓存 JSON 重建引用对象。"""
    if not isinstance(value, dict):
        raise ValueError("cache citation must be an object")
    positions = value.get("positions", [])
    if not isinstance(positions, list):
        raise ValueError("cache citation positions must be an array")
    return Citation(
        citation_id=UUID(str(value["citation_id"])),
        label=str(value["label"]),
        knowledge_base_id=UUID(str(value["knowledge_base_id"])),
        document_id=UUID(str(value["document_id"])),
        document_name=str(value["document_name"]),
        engine_chunk_id=str(value["engine_chunk_id"]),
        quote=str(value["quote"]),
        resource_scope_id=str(value.get("resource_scope_id", "")),
        page_start=_optional_int(value.get("page_start")),
        page_end=_optional_int(value.get("page_end")),
        positions=tuple(
            SourcePosition(
                page=_optional_int(item.get("page")),
                bbox=_bbox(item.get("bbox")),
            )
            for item in positions
            if isinstance(item, dict)
        ),
        score=_optional_float(value.get("score")),
        rerank_score=_optional_float(value.get("rerank_score")),
    )


def _usage_json(value: LlmUsage | None) -> dict[str, int] | None:
    """把可选 Token 用量转换为 JSON。"""
    if value is None:
        return None
    return {
        "prompt_tokens": value.prompt_tokens,
        "completion_tokens": value.completion_tokens,
        "total_tokens": value.total_tokens,
    }


def _usage(value: Any) -> LlmUsage | None:
    """从缓存 JSON 重建可选 Token 用量。"""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("cache usage must be an object")
    return LlmUsage(
        prompt_tokens=int(value["prompt_tokens"]),
        completion_tokens=int(value["completion_tokens"]),
        total_tokens=int(value["total_tokens"]),
    )


def _optional_int(value: Any) -> int | None:
    """把可选值转换为 int。"""
    return int(value) if value is not None else None


def _optional_positive_integer(value: Any) -> int | None:
    """读取升级后新增的可选检索预算，并拒绝损坏的零值或负值。

    旧缓存没有这些字段时返回 ``None``；新缓存必须是大于零的整数。这样 Redis 中
    被意外修改的动态预算不会悄悄进入公开查询轨迹，同时保持历史缓存向后兼容。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("cache retrieval budget must be an integer")
    result = int(value)
    if result < 1:
        raise ValueError("cache retrieval budget must be positive")
    return result


def _optional_query_type(value: Any) -> str | None:
    """读取可选问题类型，并阻止未知缓存值进入公开 API 的 Literal 字段。"""
    if value is None:
        return None
    result = str(value)
    if result not in _QUERY_TYPES:
        raise ValueError("cache query type is invalid")
    return result


def _optional_float(value: Any) -> float | None:
    """把可选值转换为 float。"""
    return float(value) if value is not None else None


def _bbox(value: Any) -> tuple[float, float, float, float] | None:
    """校验坐标框恰有四个数值并转换为浮点元组。"""
    if not isinstance(value, list | tuple) or len(value) != 4:
        return None
    return tuple(float(item) for item in value)  # type: ignore[return-value]
