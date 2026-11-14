"""答案缓存键的纯函数。

缓存键不仅包含问题文本，还包含租户、知识库版本和所有影响答案的配置边界。这样
任何知识内容或模型配置变化都会得到新键，旧答案不会被误用于新的运行条件。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from rag_service.domain.ports import CacheBoundary


def cache_id(boundary: CacheBoundary, normalized_question: str) -> str:
    """把完整缓存边界和标准化问题计算成稳定、不可逆的 SHA-256 标识。

    过滤摘要、图谱版本和模式同时参与键计算；即使默认关闭图谱，也会得到不同于旧
    边界格式的键，历史答案不会被误认作已经执行新隔离规则的结果。不访问外部存储。
    """
    question_hash = hashlib.sha256(normalized_question.encode("utf-8")).hexdigest()
    values = (
        boundary.security_scope_id,
        boundary.knowledge_base_revisions_hash,
        boundary.retrieval_profile,
        boundary.prompt_profile,
        boundary.model_profile,
        boundary.language,
        boundary.history_hash,
        boundary.filter_hash,
        boundary.graph_version_hash,
        boundary.graph_mode,
        boundary.evidence_mode,
        boundary.aggregate_profile_ref,
        question_hash,
    )
    return hashlib.sha256("\x1f".join(values).encode("utf-8")).hexdigest()


def metadata_filter_hash(metadata_filter: dict[str, Any] | None) -> str:
    """把 Metadata 过滤条件规范化后计算摘要，防止相同问题跨过滤范围复用答案。

    ``None`` 使用显式空范围标记；实际 JSON 对象按键排序并递归规范化，键的传输顺序
    不影响缓存身份，而数组顺序、数值类型及具体条件仍保留，避免扩大缓存复用语义。
    非 JSON 或非有限数值会抛出序列化错误；本函数不写库、不调用模型、不保存原文。
    """
    if metadata_filter is None:
        return "none"
    canonical = json.dumps(
        metadata_filter,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def knowledge_base_revisions_hash(values: list[tuple[UUID, int]]) -> str:
    """将“知识库 ID + 修订号”集合排序后计算摘要，使调用顺序不影响结果。"""
    serialized = "\n".join(
        f"{identifier}:{revision}"
        for identifier, revision in sorted(values, key=lambda item: str(item[0]))
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
