"""领域状态机测试：声明内转换通过，越级/逆向转换返回稳定冲突错误。"""

from __future__ import annotations

import pytest

from rag_service.domain.errors import RagError
from rag_service.domain.models import DocumentStatus
from rag_service.domain.state import DOCUMENT_TRANSITIONS, require_transition


def test_state_machine_accepts_declared_transition() -> None:
    assert (
        require_transition(
            DocumentStatus.RECEIVED,
            DocumentStatus.UPLOADING,
            DOCUMENT_TRANSITIONS,
        )
        == DocumentStatus.UPLOADING
    )


def test_state_machine_rejects_undeclared_transition() -> None:
    with pytest.raises(RagError) as captured:
        require_transition(
            DocumentStatus.RECEIVED,
            DocumentStatus.READY,
            DOCUMENT_TRANSITIONS,
        )
    assert captured.value.code == "RAG-COMMON-005"
