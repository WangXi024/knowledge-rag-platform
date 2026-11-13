"""原生图谱状态的 PostgreSQL 仓库，供 GraphService 和 GraphWorker 共用事务。

所有查询同时携带资源 Scope 与知识库 ID；写入不提交事务，由调用方与 Job 一起原子
提交。更新使用数据库 CAS，成功后同步领域对象行版本，失败不改变对象或吞掉并发冲突。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from rag_service.domain.graph import GraphConfiguration, GraphState, GraphStatus
from rag_service.infrastructure.postgres.graph_models import GraphStateRow


def _state_values(value: GraphState) -> dict[str, Any]:
    """把状态与受控配置转换为 ORM 字段，不序列化模型密钥或图谱生成正文。"""
    return {
        "security_scope_id": value.security_scope_id,
        "knowledge_base_id": value.knowledge_base_id,
        "enabled": value.configuration.enabled,
        "profile_ref": value.configuration.profile_ref,
        "auto_build": value.configuration.auto_build,
        "retrieval_enabled": value.configuration.retrieval_enabled,
        "status": value.status.value,
        "target_content_revision": value.target_content_revision,
        "built_for_revision": value.built_for_revision,
        "graph_generation": value.graph_generation,
        "configuration_version": value.configuration_version,
        "row_version": value.row_version,
        "engine_task_id": value.engine_task_id,
        "job_id": value.job_id,
        "last_error_code": value.last_error_code,
        "last_error_message": value.last_error_message,
        "queued_at": value.queued_at,
        "started_at": value.started_at,
        "completed_at": value.completed_at,
        "created_at": value.created_at,
        "updated_at": value.updated_at,
    }


def _to_domain(row: GraphStateRow) -> GraphState:
    """把 ORM 状态转换成脱离 Session 的对象，避免多个编排协程共享可变 ORM 实例。"""
    return GraphState(
        security_scope_id=row.security_scope_id,
        knowledge_base_id=row.knowledge_base_id,
        configuration=GraphConfiguration(
            enabled=row.enabled, profile_ref=row.profile_ref,
            auto_build=row.auto_build, retrieval_enabled=row.retrieval_enabled,
        ),
        status=GraphStatus(row.status),
        target_content_revision=row.target_content_revision,
        built_for_revision=row.built_for_revision,
        graph_generation=row.graph_generation,
        configuration_version=row.configuration_version,
        row_version=row.row_version,
        engine_task_id=row.engine_task_id,
        job_id=row.job_id,
        last_error_code=row.last_error_code,
        last_error_message=row.last_error_message,
        queued_at=row.queued_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class SqlAlchemyGraphRepository:
    """在外层事务内提供 Scope 化读取、创建和乐观锁更新。

    每个实例仅属于一个请求/Worker 的 Session，不负责提交、重试或调用 RAGFlow。
    约束异常向上传播，调用方必须回滚；并发版本不匹配返回 False，不覆盖较新的构图。
    """

    def __init__(self, session: AsyncSession) -> None:
        """保存外层拥有的会话，使状态 CAS 与持久 Job 能在同一事务内提交。"""
        self._session = session

    async def get(self, security_scope_id: str, knowledge_base_id: UUID) -> GraphState | None:
        """读取指定资源范围的图谱；未知与跨 Scope 知识库均返回 None，不写入默认行。"""
        row = await self._session.scalar(
            select(GraphStateRow).where(
                GraphStateRow.security_scope_id == security_scope_id,
                GraphStateRow.knowledge_base_id == knowledge_base_id,
            ).execution_options(populate_existing=True)
        )
        return _to_domain(row) if row is not None else None

    async def add(self, security_scope_id: str, value: GraphState) -> None:
        """校验对象 Scope 后创建并 flush，及时暴露复合外键/唯一键错误，但不提交事务。"""
        if value.security_scope_id != security_scope_id:
            raise ValueError("graph state security scope does not match")
        self._session.add(GraphStateRow(**_state_values(value)))
        await self._session.flush()

    async def update(
        self, security_scope_id: str, value: GraphState, *, expected_row_version: int,
    ) -> bool:
        """按 Scope、知识库和读取时行版本执行 CAS，成功才推进内存对象版本。

        参数 expected_row_version 是调用方读取的版本；不匹配时数据库不写任何行，
        返回 False 且保留 value.row_version。成功后不提交，由外层与 Job 一起提交；
        发生数据库异常时向上传播，事务回滚由调用方负责。
        """
        if value.security_scope_id != security_scope_id:
            raise ValueError("graph state security scope does not match")
        values = _state_values(value)
        for identity_field in ("security_scope_id", "knowledge_base_id", "created_at"):
            values.pop(identity_field)
        values["row_version"] = expected_row_version + 1
        result = await self._session.execute(
            update(GraphStateRow).where(
                GraphStateRow.security_scope_id == security_scope_id,
                GraphStateRow.knowledge_base_id == value.knowledge_base_id,
                GraphStateRow.row_version == expected_row_version,
            ).values(**values).execution_options(synchronize_session=False)
        )
        if getattr(result, "rowcount", 0) != 1:
            return False
        value.row_version = expected_row_version + 1
        return True
