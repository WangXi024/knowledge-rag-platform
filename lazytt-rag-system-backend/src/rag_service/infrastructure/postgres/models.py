"""应用 PostgreSQL 的 SQLAlchemy 表映射、约束与索引。

领域 ``dataclass`` 描述业务对象，本文件描述这些对象如何持久化。除主键外，多数外键
同时包含 ``security_scope_id``，让数据库也参与租户隔离；Check/Unique 约束作为最后
防线，阻止绕过应用层写入非法状态、负数进度或重复外部映射。
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
    Integer,
    LargeBinary,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import CHAR, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCHEMA = "rag_app"


class Base(DeclarativeBase):
    """所有 ORM 表的声明式基类和 Alembic metadata 入口。"""

    pass


class TimestampMixin:
    """为业务表统一提供数据库生成的创建/更新时间。"""
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
    )


class ConfigProfileRow(Base, TimestampMixin):
    """版本化配置表；类型+名称+版本唯一，内容以 JSONB 和 SHA-256 保存。"""
    __tablename__ = "config_profile"
    __table_args__ = (
        UniqueConstraint("profile_type", "name", "version", name="uk_config_profile_name_version"),
        CheckConstraint("version > 0", name="chk_config_profile_version"),
        CheckConstraint(
            "profile_type IN "
            "('PARSER','CHUNK','EMBEDDING','RETRIEVAL','RERANK','PROMPT','MODEL','CACHE')",
            name="chk_config_profile_type",
        ),
        Index("idx_config_profile_enabled", "profile_type", "enabled"),
        {"schema": SCHEMA},
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    profile_type: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str | None] = mapped_column(String(512))
    config_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    checksum_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")


class KnowledgeBaseRow(Base, TimestampMixin):
    """知识库主表，保存隔离键、Profile、RAGFlow 映射、修订号和乐观锁版本。"""
    __tablename__ = "knowledge_base"
    __table_args__ = (
        UniqueConstraint("security_scope_id", "id", name="uk_knowledge_base_scope_id"),
        UniqueConstraint(
            "security_scope_id", "external_id", name="uk_knowledge_base_external_id"
        ),
        UniqueConstraint("ragflow_dataset_id", name="uk_knowledge_base_ragflow_dataset"),
        CheckConstraint(
            "security_scope_id ~ '^[0-9a-f]{64}$'", name="chk_knowledge_base_scope"
        ),
        CheckConstraint("revision >= 0", name="chk_knowledge_base_revision"),
        CheckConstraint("row_version >= 0", name="chk_knowledge_base_row_version"),
        CheckConstraint(
            "scope_type IN ('TENANT','SYSTEM')",
            name="chk_knowledge_base_scope_type",
        ),
        CheckConstraint(
            "status IN ('CREATING','READY','CREATE_FAILED','DELETING','DELETE_FAILED','DELETED')",
            name="chk_knowledge_base_status",
        ),
        Index(
            "idx_knowledge_base_scope_status",
            "security_scope_id",
            "status",
            "updated_at",
        ),
        Index("idx_knowledge_base_scope_name", "security_scope_id", "name"),
        Index(
            "idx_knowledge_base_type_scope_status",
            "scope_type",
            "security_scope_id",
            "status",
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    security_scope_id: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    scope_type: Mapped[str] = mapped_column(String(16), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(128))
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2048))
    ragflow_dataset_id: Mapped[str | None] = mapped_column(String(64))
    parser_profile_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    chunk_profile_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    embedding_profile_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    retrieval_profile_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    row_version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error_message: Mapped[str | None] = mapped_column(String(512))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DocumentRecordRow(Base, TimestampMixin):
    """文档主表，保存当前活动版本、解析统计和软删除状态。

    这张表仍是检索与普通文档 API 的稳定入口。历史版本放在
    ``document_version``；更新成功时两张表在一个事务中切换，因此在线查询永远只
    看到一个完整版本，不会读取正在解析的候选文件。
    """
    __tablename__ = "document_record"
    __table_args__ = (
        UniqueConstraint("security_scope_id", "id", name="uk_document_scope_id"),
        Index(
            "uk_document_external",
            "security_scope_id",
            "knowledge_base_id",
            "external_id",
            unique=True,
            postgresql_where=text("external_id IS NOT NULL AND deleted_at IS NULL"),
        ),
        UniqueConstraint("ragflow_document_id", name="uk_document_ragflow_id"),
        ForeignKeyConstraint(
            ["security_scope_id", "knowledge_base_id"],
            [f"{SCHEMA}.knowledge_base.security_scope_id", f"{SCHEMA}.knowledge_base.id"],
            name="fk_document_knowledge_base",
            ondelete="RESTRICT",
        ),
        CheckConstraint("security_scope_id ~ '^[0-9a-f]{64}$'", name="chk_document_scope"),
        CheckConstraint("file_size_bytes >= 0", name="chk_document_file_size"),
        CheckConstraint("progress >= 0 AND progress <= 1", name="chk_document_progress"),
        CheckConstraint("chunk_count >= 0", name="chk_document_chunk_count"),
        CheckConstraint("token_count >= 0", name="chk_document_token_count"),
        CheckConstraint("row_version >= 0", name="chk_document_row_version"),
        CheckConstraint(
            "active_version_number > 0", name="chk_document_active_version_number"
        ),
        CheckConstraint("document_revision > 0", name="chk_document_revision"),
        CheckConstraint(
            "status IN ('RECEIVED','UPLOADING','UPLOADED','QUEUED','PARSING','INDEXING',"
            "'READY','FAILED','RETRYING','DELETING','DELETE_FAILED','DELETED')",
            name="chk_document_status",
        ),
        Index(
            "idx_document_scope_kb_status",
            "security_scope_id",
            "knowledge_base_id",
            "status",
            "updated_at",
        ),
        Index(
            "idx_document_scope_hash",
            "security_scope_id",
            "knowledge_base_id",
            "content_sha256",
        ),
        Index(
            "idx_document_scope_filename",
            "security_scope_id",
            "knowledge_base_id",
            "filename",
        ),
        Index("idx_document_metadata_gin", "metadata_json", postgresql_using="gin"),
        {"schema": SCHEMA},
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    security_scope_id: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    knowledge_base_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(128))
    ragflow_document_id: Mapped[str | None] = mapped_column(String(64))
    # 版本 ID 不设置数据库外键，避免 document_record 与 document_version 形成创建
    # 顺序循环；激活事务由 Repository 同时验证 Scope、document_id 和 ACTIVE 状态。
    active_version_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    active_version_number: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1"
    )
    document_revision: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="1"
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    safe_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    parser_profile_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    chunk_profile_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    progress: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False, server_default="0")
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    token_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    row_version: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error_message: Mapped[str | None] = mapped_column(String(1024))
    engine_progress_message: Mapped[str | None] = mapped_column(String(2048))
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DocumentVersionRow(Base, TimestampMixin):
    """逻辑文档的不可变文件版本和独立 RAGFlow 物理文档映射。

    每个候选版本拥有自己的 UUID 文件名族，所以 RAGFlow 上传幂等恢复不会误删当前
    活动版本。部分唯一索引保证同一逻辑文档最多一个 ``ACTIVE``；失败候选与退役
    版本保留摘要、统计和错误用于审计，但不参与在线检索。
    """

    __tablename__ = "document_version"
    __table_args__ = (
        UniqueConstraint("security_scope_id", "id", name="uk_document_version_scope_id"),
        UniqueConstraint(
            "security_scope_id",
            "document_id",
            "version_number",
            name="uk_document_version_number",
        ),
        UniqueConstraint(
            "ragflow_document_id", name="uk_document_version_ragflow_document_id"
        ),
        ForeignKeyConstraint(
            ["security_scope_id", "knowledge_base_id"],
            [f"{SCHEMA}.knowledge_base.security_scope_id", f"{SCHEMA}.knowledge_base.id"],
            name="fk_document_version_knowledge_base",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["security_scope_id", "document_id"],
            [f"{SCHEMA}.document_record.security_scope_id", f"{SCHEMA}.document_record.id"],
            name="fk_document_version_document",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "security_scope_id ~ '^[0-9a-f]{64}$'", name="chk_document_version_scope"
        ),
        CheckConstraint("version_number > 0", name="chk_document_version_number"),
        CheckConstraint("file_size_bytes >= 0", name="chk_document_version_file_size"),
        CheckConstraint("chunk_count >= 0", name="chk_document_version_chunk_count"),
        CheckConstraint("token_count >= 0", name="chk_document_version_token_count"),
        CheckConstraint(
            "status IN ('PENDING','UPLOADING','PARSING','INDEXING','ACTIVE','RETIRED','FAILED')",
            name="chk_document_version_status",
        ),
        CheckConstraint(
            "requested_mode IN ('AUTO','SAFE_REPLACE')",
            name="chk_document_version_requested_mode",
        ),
        CheckConstraint(
            "effective_mode IN ('AUTO','SAFE_REPLACE')",
            name="chk_document_version_effective_mode",
        ),
        Index(
            "uk_document_version_active",
            "security_scope_id",
            "document_id",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
        Index(
            "idx_document_version_scope_document",
            "security_scope_id",
            "document_id",
            "version_number",
        ),
        Index(
            "idx_document_version_scope_status",
            "security_scope_id",
            "status",
            "updated_at",
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    security_scope_id: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    knowledge_base_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    document_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    ragflow_document_id: Mapped[str | None] = mapped_column(String(64))
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    safe_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    parser_profile_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    chunk_profile_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    requested_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    effective_mode: Mapped[str] = mapped_column(String(32), nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    token_count: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    last_error_code: Mapped[str | None] = mapped_column(String(64))
    last_error_message: Mapped[str | None] = mapped_column(String(1024))
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DocumentIndexNodeRow(Base, TimestampMixin):
    """Gateway 侧车 Parent/Summary 索引，不复制或替代 RAGFlow 原始 Chunk 表。

    Embedding 以固定顺序 FLOAT32 字节保存，避免要求 PostgreSQL 安装 pgvector；在线
    层只会读取当前租户和知识库的受限节点集后在 Python 中计算余弦相似度。源 Chunk
    ID 使用 JSONB 数组，确保任何摘要命中都能回查真实 RAGFlow 内容并生成引用。
    """

    __tablename__ = "document_index_node"
    __table_args__ = (
        UniqueConstraint("security_scope_id", "id", name="uk_index_node_scope_id"),
        UniqueConstraint(
            "security_scope_id",
            "document_id",
            "node_type",
            "ordinal",
            "index_version",
            name="uk_index_node_document_ordinal_version",
        ),
        ForeignKeyConstraint(
            ["security_scope_id", "knowledge_base_id"],
            [f"{SCHEMA}.knowledge_base.security_scope_id", f"{SCHEMA}.knowledge_base.id"],
            name="fk_index_node_knowledge_base",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["security_scope_id", "document_id"],
            [f"{SCHEMA}.document_record.security_scope_id", f"{SCHEMA}.document_record.id"],
            name="fk_index_node_document",
            ondelete="CASCADE",
        ),
        CheckConstraint("security_scope_id ~ '^[0-9a-f]{64}$'", name="chk_index_node_scope"),
        # 层级索引从 v2 起增加 SECTION。约束必须与领域枚举同步，否则 Worker 在真实
        # PostgreSQL 中写入章节节点时会被数据库拒绝，而只使用内存仓库的单元测试
        # 无法发现该问题。
        CheckConstraint(
            "node_type IN ('PARENT','SECTION','SUMMARY')",
            name="chk_index_node_type",
        ),
        CheckConstraint("ordinal >= 0", name="chk_index_node_ordinal"),
        CheckConstraint("embedding_dimension >= 0", name="chk_index_node_embedding_dimension"),
        CheckConstraint("index_version > 0", name="chk_index_node_version"),
        Index(
            "idx_index_node_scope_kb_type",
            "security_scope_id",
            "knowledge_base_id",
            "node_type",
        ),
        Index(
            "idx_index_node_source_chunks_gin",
            "source_chunk_ids_json",
            postgresql_using="gin",
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    security_scope_id: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    knowledge_base_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    document_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    node_type: Mapped[str] = mapped_column(String(16), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source_chunk_ids_json: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    embedding_bytes: Mapped[bytes | None] = mapped_column(LargeBinary)
    embedding_dimension: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    index_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class DocumentEntityRow(Base, TimestampMixin):
    """文档实体侧车索引；每个实体只保存名称、类型和真实源 Chunk ID。

    该表不是 RAGFlow Chunk 的副本，也不保存模型生成答案。查询命中实体后，在线层
    必须通过 ``source_chunk_ids_json`` 回读 RAGFlow 原文。租户、知识库和文档复合
    外键确保级联删除时不会残留跨范围实体。
    """

    __tablename__ = "document_entity"
    __table_args__ = (
        UniqueConstraint("security_scope_id", "id", name="uk_document_entity_scope_id"),
        UniqueConstraint(
            "security_scope_id",
            "document_id",
            "normalized_name",
            "index_version",
            name="uk_document_entity_name_version",
        ),
        ForeignKeyConstraint(
            ["security_scope_id", "knowledge_base_id"],
            [f"{SCHEMA}.knowledge_base.security_scope_id", f"{SCHEMA}.knowledge_base.id"],
            name="fk_document_entity_knowledge_base",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["security_scope_id", "document_id"],
            [f"{SCHEMA}.document_record.security_scope_id", f"{SCHEMA}.document_record.id"],
            name="fk_document_entity_document",
            ondelete="CASCADE",
        ),
        CheckConstraint("security_scope_id ~ '^[0-9a-f]{64}$'", name="chk_document_entity_scope"),
        CheckConstraint("mention_count > 0", name="chk_document_entity_mentions"),
        CheckConstraint("index_version > 0", name="chk_document_entity_version"),
        Index(
            "idx_document_entity_scope_kb_name",
            "security_scope_id",
            "knowledge_base_id",
            "normalized_name",
        ),
        Index(
            "idx_document_entity_source_chunks_gin",
            "source_chunk_ids_json",
            postgresql_using="gin",
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    security_scope_id: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    knowledge_base_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    document_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    canonical_name: Mapped[str] = mapped_column(String(128), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(128), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    aliases_json: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    source_chunk_ids_json: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    mention_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    index_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class EntityRelationRow(Base, TimestampMixin):
    """文档中明确出现的主体—谓词—客体关系和原文证据位置。

    关系由确定性规则抽取，``confidence`` 只表达抽取器置信，不等于语义相似度。
    主体和客体不建立跨文档外键，避免同名实体在不同租户或不同版本之间错误合并。
    """

    __tablename__ = "entity_relation"
    __table_args__ = (
        UniqueConstraint("security_scope_id", "id", name="uk_entity_relation_scope_id"),
        UniqueConstraint(
            "security_scope_id",
            "document_id",
            "subject_normalized",
            "predicate",
            "object_normalized",
            "index_version",
            name="uk_entity_relation_triple_version",
        ),
        ForeignKeyConstraint(
            ["security_scope_id", "knowledge_base_id"],
            [f"{SCHEMA}.knowledge_base.security_scope_id", f"{SCHEMA}.knowledge_base.id"],
            name="fk_entity_relation_knowledge_base",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["security_scope_id", "document_id"],
            [f"{SCHEMA}.document_record.security_scope_id", f"{SCHEMA}.document_record.id"],
            name="fk_entity_relation_document",
            ondelete="CASCADE",
        ),
        CheckConstraint("security_scope_id ~ '^[0-9a-f]{64}$'", name="chk_entity_relation_scope"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="chk_relation_confidence"),
        CheckConstraint("index_version > 0", name="chk_entity_relation_version"),
        Index(
            "idx_entity_relation_scope_kb_subject",
            "security_scope_id",
            "knowledge_base_id",
            "subject_normalized",
        ),
        Index(
            "idx_entity_relation_scope_kb_object",
            "security_scope_id",
            "knowledge_base_id",
            "object_normalized",
        ),
        Index(
            "idx_entity_relation_source_chunks_gin",
            "source_chunk_ids_json",
            postgresql_using="gin",
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    security_scope_id: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    knowledge_base_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    document_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    subject_name: Mapped[str] = mapped_column(String(128), nullable=False)
    subject_normalized: Mapped[str] = mapped_column(String(128), nullable=False)
    predicate: Mapped[str] = mapped_column(String(64), nullable=False)
    object_name: Mapped[str] = mapped_column(String(128), nullable=False)
    object_normalized: Mapped[str] = mapped_column(String(128), nullable=False)
    source_chunk_ids_json: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    confidence: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False, server_default="1")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    index_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")


class IngestionJobRow(Base, TimestampMixin):
    """可认领/续租/重试的异步任务表，JSONB 保存类型特定输入和结果。"""
    __tablename__ = "ingestion_job"
    __table_args__ = (
        UniqueConstraint("security_scope_id", "id", name="uk_ingestion_job_scope_id"),
        ForeignKeyConstraint(
            ["security_scope_id", "knowledge_base_id"],
            [f"{SCHEMA}.knowledge_base.security_scope_id", f"{SCHEMA}.knowledge_base.id"],
            name="fk_job_knowledge_base",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["security_scope_id", "document_id"],
            [f"{SCHEMA}.document_record.security_scope_id", f"{SCHEMA}.document_record.id"],
            name="fk_job_document",
            ondelete="RESTRICT",
        ),
        CheckConstraint("security_scope_id ~ '^[0-9a-f]{64}$'", name="chk_job_scope"),
        CheckConstraint("progress >= 0 AND progress <= 1", name="chk_job_progress"),
        CheckConstraint("attempt >= 0 AND max_attempts >= 0", name="chk_job_attempt"),
        CheckConstraint(
            "job_type IN "
            "('UPLOAD','PARSE','REPARSE','UPDATE_DOCUMENT',"
            "'DELETE_DOCUMENT','DELETE_KB','CLEANUP')",
            name="chk_job_type",
        ),
        CheckConstraint(
            "status IN "
            "('PENDING','RUNNING','RETRY_WAIT','SUCCEEDED','FAILED',"
            "'CANCEL_REQUESTED','CANCELLED')",
            name="chk_job_status",
        ),
        Index(
            "idx_job_scope_claim",
            "security_scope_id",
            "status",
            "next_retry_at",
            "locked_until",
            "created_at",
        ),
        Index("idx_job_scope_document", "security_scope_id", "document_id", "created_at"),
        Index("idx_job_scope_kb", "security_scope_id", "knowledge_base_id", "created_at"),
        {"schema": SCHEMA},
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    security_scope_id: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    job_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    knowledge_base_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    document_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    progress: Mapped[float] = mapped_column(Numeric(5, 4), nullable=False, server_default="0")
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="2")
    payload_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(String(1024))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(128))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IdempotencyRecordRow(Base):
    """按隔离范围、幂等键、方法和路径唯一的请求指纹与响应快照。"""
    __tablename__ = "idempotency_record"
    __table_args__ = (
        CheckConstraint("security_scope_id ~ '^[0-9a-f]{64}$'", name="chk_idempotency_scope"),
        CheckConstraint(
            "response_status IS NULL OR response_status BETWEEN 100 AND 599",
            name="chk_idempotency_http_status",
        ),
        CheckConstraint("expires_at > created_at", name="chk_idempotency_expiry"),
        CheckConstraint(
            "processing_status IN ('PROCESSING','COMPLETED','FAILED')",
            name="chk_idempotency_status",
        ),
        Index("idx_idempotency_scope_expiry", "security_scope_id", "expires_at"),
        {"schema": SCHEMA},
    )

    security_scope_id: Mapped[str] = mapped_column(CHAR(64), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    http_method: Mapped[str] = mapped_column(String(8), primary_key=True)
    request_path: Mapped[str] = mapped_column(String(255), primary_key=True)
    request_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    processing_status: Mapped[str] = mapped_column(String(16), nullable=False)
    response_status: Mapped[int | None] = mapped_column(SmallInteger)
    response_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    resource_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class QueryRecordRow(Base):
    """在线检索/问答审计表，只保存脱敏查询、状态、答案和诊断统计。"""
    __tablename__ = "query_record"
    __table_args__ = (
        UniqueConstraint("security_scope_id", "request_id", name="uk_query_record_scope_id"),
        CheckConstraint("security_scope_id ~ '^[0-9a-f]{64}$'", name="chk_query_scope"),
        CheckConstraint(
            "candidate_count >= 0 AND context_count >= 0", name="chk_query_counts"
        ),
        CheckConstraint(
            "(prompt_tokens IS NULL OR prompt_tokens >= 0) "
            "AND (completion_tokens IS NULL OR completion_tokens >= 0) "
            "AND (total_tokens IS NULL OR total_tokens >= 0)",
            name="chk_query_token_counts",
        ),
        CheckConstraint(
            "request_status IN ('ACCEPTED','RUNNING','COMPLETED','FAILED','CANCELLED')",
            name="chk_query_status",
        ),
        CheckConstraint(
            "outcome IS NULL OR outcome IN ('success','no_evidence','refused','cancelled')",
            name="chk_query_outcome",
        ),
        CheckConstraint(
            "cache_status IN ('BYPASS','MISS','EXACT_HIT','SEMANTIC_HIT','WRITE_FAILED')",
            name="chk_query_cache",
        ),
        Index("idx_query_scope_created", "security_scope_id", "created_at"),
        Index("idx_query_scope_outcome", "security_scope_id", "outcome", "created_at"),
        Index("idx_query_expiry", "expires_at"),
        Index("idx_query_kb_ids_gin", "knowledge_base_ids_json", postgresql_using="gin"),
        {"schema": SCHEMA},
    )

    request_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    security_scope_id: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    request_status: Mapped[str] = mapped_column(String(32), nullable=False)
    outcome: Mapped[str | None] = mapped_column(String(32))
    query_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    query_redacted: Mapped[str | None] = mapped_column(Text)
    effective_query_redacted: Mapped[str | None] = mapped_column(Text)
    knowledge_base_ids_json: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    retrieval_profile_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_profile_ref: Mapped[str | None] = mapped_column(String(128))
    model_profile_ref: Mapped[str | None] = mapped_column(String(128))
    cache_status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="BYPASS")
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    context_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    answer_text: Mapped[str | None] = mapped_column(Text)
    timings_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    warnings_json: Mapped[list[str] | None] = mapped_column(JSONB)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CitationRecordRow(Base):
    """一次问答实际使用的引用表，包含来源、顺序、坐标、摘录和得分。"""
    __tablename__ = "citation_record"
    __table_args__ = (
        UniqueConstraint("security_scope_id", "id", name="uk_citation_scope_id"),
        UniqueConstraint(
            "security_scope_id", "request_id", "label", name="uk_citation_request_label"
        ),
        ForeignKeyConstraint(
            ["security_scope_id", "request_id"],
            [f"{SCHEMA}.query_record.security_scope_id", f"{SCHEMA}.query_record.request_id"],
            name="fk_citation_query",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["resource_scope_id", "knowledge_base_id"],
            [f"{SCHEMA}.knowledge_base.security_scope_id", f"{SCHEMA}.knowledge_base.id"],
            name="fk_citation_knowledge_base",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["resource_scope_id", "document_id"],
            [f"{SCHEMA}.document_record.security_scope_id", f"{SCHEMA}.document_record.id"],
            name="fk_citation_document",
            ondelete="RESTRICT",
        ),
        CheckConstraint("security_scope_id ~ '^[0-9a-f]{64}$'", name="chk_citation_scope"),
        CheckConstraint(
            "resource_scope_id ~ '^[0-9a-f]{64}$'",
            name="chk_citation_resource_scope",
        ),
        CheckConstraint("ordinal >= 0", name="chk_citation_ordinal"),
        CheckConstraint(
            "(page_start IS NULL OR page_start >= 0) "
            "AND (page_end IS NULL OR page_end >= 0)",
            name="chk_citation_pages",
        ),
        CheckConstraint(
            "verification_status IN ('VALID','INVALID','UNVERIFIED')",
            name="chk_citation_status",
        ),
        Index(
            "idx_citation_resource_document",
            "resource_scope_id",
            "document_id",
            "created_at",
        ),
        {"schema": SCHEMA},
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    security_scope_id: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    resource_scope_id: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    request_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String(16), nullable=False)
    knowledge_base_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    document_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    engine_chunk_id: Mapped[str] = mapped_column(String(128), nullable=False)
    document_name: Mapped[str] = mapped_column(String(255), nullable=False)
    heading_path_json: Mapped[list[str] | None] = mapped_column(JSONB)
    page_start: Mapped[int | None] = mapped_column(Integer)
    page_end: Mapped[int | None] = mapped_column(Integer)
    positions_json: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    quote_text: Mapped[str] = mapped_column(Text, nullable=False)
    retrieval_score: Mapped[float | None] = mapped_column(Numeric(10, 8))
    rerank_score: Mapped[float | None] = mapped_column(Numeric(10, 8))
    verification_status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )


class FeedbackRecordRow(Base):
    """关联在线请求的用户反馈表。"""
    __tablename__ = "feedback_record"
    __table_args__ = (
        UniqueConstraint("security_scope_id", "id", name="uk_feedback_scope_id"),
        ForeignKeyConstraint(
            ["security_scope_id", "request_id"],
            [f"{SCHEMA}.query_record.security_scope_id", f"{SCHEMA}.query_record.request_id"],
            name="fk_feedback_query",
            ondelete="CASCADE",
        ),
        CheckConstraint("security_scope_id ~ '^[0-9a-f]{64}$'", name="chk_feedback_scope"),
        CheckConstraint("rating IS NULL OR rating IN (-1, 1)", name="chk_feedback_rating"),
        CheckConstraint(
            "feedback_type IN ('helpful','not_helpful','answer_incorrect',"
            "'citation_incorrect','should_answer','should_refuse','other')",
            name="chk_feedback_type",
        ),
        Index("idx_feedback_scope_request", "security_scope_id", "request_id", "created_at"),
        Index("idx_feedback_scope_type", "security_scope_id", "feedback_type", "created_at"),
        {"schema": SCHEMA},
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    security_scope_id: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    request_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    rating: Mapped[int | None] = mapped_column(SmallInteger)
    feedback_type: Mapped[str] = mapped_column(String(32), nullable=False)
    comment_text: Mapped[str | None] = mapped_column(Text)
    corrected_answer: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.current_timestamp()
    )
