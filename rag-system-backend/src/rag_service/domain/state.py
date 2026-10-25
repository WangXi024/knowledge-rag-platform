"""领域状态机规则。

三个映射明确列出“当前状态可以去往哪些目标状态”。业务服务在修改数据库对象前
必须通过本文件校验，因此即使调用顺序错误，也不会产生诸如“已删除文档重新变为
解析中”的不可能状态。
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from rag_service.domain.errors import RagError
from rag_service.domain.models import (
    DocumentStatus,
    JobStatus,
    KnowledgeBaseStatus,
)

# 文档状态转换覆盖上传、解析、重试和删除四条路径；空集合表示终态。
DOCUMENT_TRANSITIONS: Mapping[DocumentStatus, frozenset[DocumentStatus]] = {
    DocumentStatus.RECEIVED: frozenset(
        {DocumentStatus.UPLOADING, DocumentStatus.FAILED, DocumentStatus.DELETING}
    ),
    DocumentStatus.UPLOADING: frozenset(
        {
            DocumentStatus.UPLOADED,
            DocumentStatus.FAILED,
            DocumentStatus.DELETING,
        }
    ),
    DocumentStatus.UPLOADED: frozenset(
        {DocumentStatus.QUEUED, DocumentStatus.DELETING, DocumentStatus.FAILED}
    ),
    DocumentStatus.QUEUED: frozenset(
        {DocumentStatus.PARSING, DocumentStatus.FAILED, DocumentStatus.DELETING}
    ),
    DocumentStatus.PARSING: frozenset(
        {
            DocumentStatus.INDEXING,
            DocumentStatus.READY,
            DocumentStatus.FAILED,
            DocumentStatus.DELETING,
        }
    ),
    DocumentStatus.INDEXING: frozenset(
        {DocumentStatus.READY, DocumentStatus.FAILED, DocumentStatus.DELETING}
    ),
    DocumentStatus.READY: frozenset({DocumentStatus.DELETING, DocumentStatus.QUEUED}),
    DocumentStatus.FAILED: frozenset({DocumentStatus.RETRYING, DocumentStatus.DELETING}),
    DocumentStatus.RETRYING: frozenset(
        {
            DocumentStatus.UPLOADING,
            DocumentStatus.QUEUED,
            DocumentStatus.PARSING,
            DocumentStatus.FAILED,
            DocumentStatus.DELETING,
        }
    ),
    DocumentStatus.DELETING: frozenset({DocumentStatus.DELETED, DocumentStatus.DELETE_FAILED}),
    DocumentStatus.DELETE_FAILED: frozenset({DocumentStatus.DELETING}),
    DocumentStatus.DELETED: frozenset(),
}

# 任务状态独立于文档状态：任务可被取消或重试，最终只能进入三个终态之一。
JOB_TRANSITIONS: Mapping[JobStatus, frozenset[JobStatus]] = {
    JobStatus.PENDING: frozenset(
        {JobStatus.RUNNING, JobStatus.CANCEL_REQUESTED, JobStatus.CANCELLED}
    ),
    JobStatus.RUNNING: frozenset(
        {
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.RETRY_WAIT,
            JobStatus.CANCEL_REQUESTED,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.RETRY_WAIT: frozenset(
        {
            JobStatus.PENDING,
            JobStatus.RUNNING,
            JobStatus.FAILED,
            JobStatus.CANCEL_REQUESTED,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.CANCEL_REQUESTED: frozenset({JobStatus.CANCELLED, JobStatus.FAILED}),
    JobStatus.SUCCEEDED: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
}

# 知识库创建/删除失败后允许再次尝试，但删除成功后不可恢复。
KNOWLEDGE_BASE_TRANSITIONS: Mapping[KnowledgeBaseStatus, frozenset[KnowledgeBaseStatus]] = {
    KnowledgeBaseStatus.CREATING: frozenset(
        {KnowledgeBaseStatus.READY, KnowledgeBaseStatus.CREATE_FAILED}
    ),
    KnowledgeBaseStatus.CREATE_FAILED: frozenset({KnowledgeBaseStatus.CREATING}),
    KnowledgeBaseStatus.READY: frozenset({KnowledgeBaseStatus.DELETING}),
    KnowledgeBaseStatus.DELETING: frozenset(
        {KnowledgeBaseStatus.DELETED, KnowledgeBaseStatus.DELETE_FAILED}
    ),
    KnowledgeBaseStatus.DELETE_FAILED: frozenset({KnowledgeBaseStatus.DELETING}),
    KnowledgeBaseStatus.DELETED: frozenset(),
}


def require_transition[StateT: StrEnum](
    current: StateT,
    target: StateT,
    transitions: Mapping[StateT, frozenset[StateT]],
) -> StateT:
    """验证并返回目标状态；非法跳转统一转换为 HTTP 409 领域错误。

    泛型 ``StateT`` 保证传入的当前状态、目标状态和转换表属于同一种枚举，调用方
    可直接把返回值赋回对象的状态字段。
    """
    if target not in transitions.get(current, frozenset()):
        raise RagError(
            code="RAG-COMMON-005",
            message=f"非法状态转换：{current.value} -> {target.value}",
            http_status=409,
            retryable=False,
        )
    return target
