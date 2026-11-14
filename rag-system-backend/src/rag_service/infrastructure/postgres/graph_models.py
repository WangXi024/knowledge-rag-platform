"""原生 GraphRAG 的知识库级状态映射，不存储原生图谱正文或模型凭据。

每个资源 Scope 与知识库只有一行，组合外键让数据库拒绝跨 Scope 指向其他知识库。
内容修订、图谱代数与配置版本分开保存；乐观行版本用于阻止迟到构图覆盖新状态。
实际构图仍由 RAGFlow 执行，本表只保存可恢复的应用状态和审计标识。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import CHAR, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from rag_service.infrastructure.postgres.models import SCHEMA, Base, TimestampMixin


class GraphStateRow(Base, TimestampMixin):
    """以资源范围和知识库为组合主键保存派生图谱的生命周期。

    对象由请求或 Worker 自己的 AsyncSession 管理，不跨协程共享。图谱代数只在有效
    发布时推进，不修改知识库内容 revision；job_id 只作审计指针，不承担鉴权职责。
    """

    __tablename__ = "graph_state"
    __table_args__ = (
        ForeignKeyConstraint(
            ["security_scope_id", "knowledge_base_id"],
            [f"{SCHEMA}.knowledge_base.security_scope_id", f"{SCHEMA}.knowledge_base.id"],
            name="fk_graph_state_knowledge_base",
            ondelete="CASCADE",
        ),
        CheckConstraint("security_scope_id ~ '^[0-9a-f]{64}$'", name="chk_graph_state_scope"),
        CheckConstraint(
            "status IN ('DISABLED','NOT_BUILT','QUEUED','BUILDING','READY','STALE',"
            "'FAILED','CANCELLING')", name="chk_graph_state_status",
        ),
        CheckConstraint(
            "target_content_revision >= 0 AND graph_generation >= 0 "
            "AND configuration_version >= 0 AND row_version >= 0 "
            "AND (built_for_revision IS NULL OR built_for_revision >= 0)",
            name="chk_graph_state_versions",
        ),
        CheckConstraint(
            "status <> 'READY' OR (built_for_revision IS NOT NULL AND graph_generation > 0)",
            name="chk_graph_state_ready_version",
        ),
        CheckConstraint(
            "jsonb_typeof(community_reports_json) = 'array' "
            "AND jsonb_array_length(community_reports_json) <= 4 "
            "AND octet_length(community_reports_json::text) <= 1048576",
            name="chk_graph_community_reports",
        ),
        Index("idx_graph_state_scope_status", "security_scope_id", "status", "updated_at"),
        {"schema": SCHEMA},
    )

    security_scope_id: Mapped[str] = mapped_column(CHAR(64), primary_key=True)
    knowledge_base_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    # 报告与图状态在同一CAS更新中提交，旧图过期后不得独立读取此JSON。
    community_reports_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    profile_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    auto_build: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    retrieval_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default="DISABLED")
    target_content_revision: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    built_for_revision: Mapped[int | None] = mapped_column(BigInteger)
    graph_generation: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    configuration_version: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    row_version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    engine_task_id: Mapped[str | None] = mapped_column(String(128))
    job_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error_message: Mapped[str | None] = mapped_column(Text)
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
