"""PostgreSQL 异步引擎、Session 工厂和事务作用域。"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from rag_service.settings import DatabaseSection


def create_engine(settings: DatabaseSection) -> AsyncEngine:
    """按配置创建带连接预检、连接池回收和服务端语句超时的异步引擎。"""
    return create_async_engine(
        settings.dsn,
        pool_pre_ping=True,
        pool_size=settings.pool_size,
        max_overflow=settings.max_overflow,
        pool_recycle=settings.pool_recycle_seconds,
        connect_args={
            "connect_timeout": settings.connect_timeout_seconds,
            "options": f"-c statement_timeout={settings.statement_timeout_seconds * 1000}",
        },
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """创建提交后对象不过期、禁用隐式 autoflush 的异步 Session 工厂。"""
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """为一次操作提供自动提交/异常回滚的 Session 上下文。"""
    async with factory() as session:
        async with session.begin():
            yield session
