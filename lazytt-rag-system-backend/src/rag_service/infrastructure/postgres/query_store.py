"""为长时间在线/SSE 请求提供短事务查询审计 Repository。"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rag_service.domain.models import Citation, FeedbackRecord, QueryRecord
from rag_service.infrastructure.postgres.repositories import (
    SqlAlchemyQueryRepository,
)


class TransactionalQueryRepository:
    """每次方法调用独立提交，使 SSE 执行期间的状态能被其他请求立即看到。

    若沿用请求级长事务，RUNNING 状态会一直到流结束才可见，取消与轮询均失去意义。
    该包装器复用同一工厂，但每项审计写入都创建短 Session/事务。
    """

    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        """保存异步 Session 工厂。"""
        self._factory = factory

    async def add(self, security_scope_id: str, value: QueryRecord) -> None:
        """在独立事务中新增请求审计记录。"""
        async with self._factory() as session, session.begin():
            await SqlAlchemyQueryRepository(session).add(security_scope_id, value)

    async def get(
        self,
        security_scope_id: str,
        request_id: UUID,
    ) -> QueryRecord | None:
        """在独立只读事务中读取请求记录。"""
        async with self._factory() as session, session.begin():
            return await SqlAlchemyQueryRepository(session).get(
                security_scope_id,
                request_id,
            )

    async def update(self, security_scope_id: str, value: QueryRecord) -> bool:
        """在独立事务中更新请求状态与结果。"""
        async with self._factory() as session, session.begin():
            return await SqlAlchemyQueryRepository(session).update(
                security_scope_id,
                value,
            )

    async def add_citations(
        self,
        security_scope_id: str,
        request_id: UUID,
        citations: Sequence[Citation],
    ) -> None:
        """在独立事务中批量保存引用。"""
        async with self._factory() as session, session.begin():
            await SqlAlchemyQueryRepository(session).add_citations(
                security_scope_id,
                request_id,
                citations,
            )

    async def list_citations(
        self,
        security_scope_id: str,
        request_id: UUID,
    ) -> tuple[Citation, ...]:
        """按原始顺序读取请求引用。"""
        async with self._factory() as session, session.begin():
            return await SqlAlchemyQueryRepository(session).list_citations(
                security_scope_id,
                request_id,
            )

    async def get_citation(
        self,
        security_scope_id: str,
        citation_id: UUID,
    ) -> Citation | None:
        """按安全范围读取单条引用。"""
        async with self._factory() as session, session.begin():
            return await SqlAlchemyQueryRepository(session).get_citation(
                security_scope_id,
                citation_id,
            )

    async def add_feedback(
        self,
        security_scope_id: str,
        value: FeedbackRecord,
    ) -> None:
        """在独立事务中保存用户反馈。"""
        async with self._factory() as session, session.begin():
            await SqlAlchemyQueryRepository(session).add_feedback(
                security_scope_id,
                value,
            )
