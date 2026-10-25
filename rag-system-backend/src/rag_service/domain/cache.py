"""答案缓存键的纯函数。

缓存键不仅包含问题文本，还包含租户、知识库版本和所有影响答案的配置边界。这样
任何知识内容或模型配置变化都会得到新键，旧答案不会被误用于新的运行条件。
"""

from __future__ import annotations

import hashlib
from uuid import UUID

from rag_service.domain.ports import CacheBoundary


def cache_id(boundary: CacheBoundary, normalized_question: str) -> str:
    """把完整缓存边界和标准化问题计算成稳定、不可逆的 SHA-256 标识。"""
    question_hash = hashlib.sha256(normalized_question.encode("utf-8")).hexdigest()
    values = (
        boundary.security_scope_id,
        boundary.knowledge_base_revisions_hash,
        boundary.retrieval_profile,
        boundary.prompt_profile,
        boundary.model_profile,
        boundary.language,
        boundary.history_hash,
        question_hash,
    )
    return hashlib.sha256("\x1f".join(values).encode("utf-8")).hexdigest()


def knowledge_base_revisions_hash(values: list[tuple[UUID, int]]) -> str:
    """将“知识库 ID + 修订号”集合排序后计算摘要，使调用顺序不影响结果。"""
    serialized = "\n".join(
        f"{identifier}:{revision}"
        for identifier, revision in sorted(values, key=lambda item: str(item[0]))
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
