"""显式禁用 Rerank 时使用的可观察降级适配器。"""

from __future__ import annotations

from collections.abc import Sequence

from rag_service.domain.ports import RerankOutcome


class DisabledRerankAdapter:
    """不改变排序，返回与输入等长空分数并附带降级警告。"""
    @property
    def enabled(self) -> bool:
        """始终报告重排功能关闭。"""
        return False

    async def rerank(
        self,
        query: str,
        documents: Sequence[str],
    ) -> RerankOutcome:
        """忽略查询，为每篇文档生成 ``None`` 分数并说明当前使用 RRF 排序。"""
        del query
        return RerankOutcome(
            scores=tuple(None for _ in documents),
            enabled=False,
            warning="rerank_disabled_vector_bm25_rrf_fallback",
        )
