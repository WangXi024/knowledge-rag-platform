"""领域 Repository 的 SQLAlchemy 实现与 ORM/领域对象双向映射。

所有资源查询都把 ``security_scope_id`` 写入 SQL 条件，而不是先按全局 ID 查询再在
Python 中判断。更新知识库/文档使用 ``row_version`` 乐观锁；Worker 认领使用
``FOR UPDATE SKIP LOCKED``，保证多实例并发时同一任务只被一个实例获得。
"""

from __future__ import annotations

import struct
from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, case, delete, func, literal, or_, select, update
from sqlalchemy.dialects.postgresql import array, insert
from sqlalchemy.ext.asyncio import AsyncSession

from rag_service.domain.errors import JobCancellationRequested, JobLeaseLostError
from rag_service.domain.models import (
    AnswerOutcome,
    Citation,
    DocumentEntity,
    DocumentIndexNode,
    DocumentRecord,
    DocumentStatus,
    DocumentUpdateMode,
    DocumentVersion,
    DocumentVersionStatus,
    EntityRelation,
    FeedbackRecord,
    GraphEvidence,
    IdempotencyRecord,
    IdempotencyReservation,
    IdempotencyStatus,
    IndexNodeType,
    IngestionJob,
    JobStatus,
    JobType,
    KnowledgeBase,
    KnowledgeBaseScopeType,
    KnowledgeBaseStatus,
    QueryRecord,
    QueryStatus,
    SourcePosition,
)
from rag_service.infrastructure.postgres.models import (
    CitationRecordRow,
    DocumentEntityRow,
    DocumentIndexNodeRow,
    DocumentRecordRow,
    DocumentVersionRow,
    EntityRelationRow,
    FeedbackRecordRow,
    IdempotencyRecordRow,
    IngestionJobRow,
    KnowledgeBaseRow,
    QueryRecordRow,
)


def _require_matching_scope(security_scope_id: str, value_scope_id: str) -> None:
    """在构造 SQL 前阻止调用方用一个租户参数保存另一个租户的领域对象。"""
    if security_scope_id != value_scope_id:
        raise ValueError("repository scope argument does not match entity scope")


def _index_node_to_domain(row: DocumentIndexNodeRow) -> DocumentIndexNode:
    """把 FLOAT32 字节和 JSONB 源块数组还原成不可变层级索引领域对象。"""
    dimension = row.embedding_dimension
    embedding = (
        tuple(struct.unpack(f"<{dimension}f", row.embedding_bytes))
        if row.embedding_bytes is not None and dimension > 0
        else ()
    )
    return DocumentIndexNode(
        id=row.id,
        security_scope_id=row.security_scope_id,
        knowledge_base_id=row.knowledge_base_id,
        document_id=row.document_id,
        node_type=IndexNodeType(row.node_type),
        ordinal=row.ordinal,
        content=row.content,
        source_chunk_ids=tuple(row.source_chunk_ids_json),
        embedding=embedding,
        metadata=dict(row.metadata_json),
        index_version=row.index_version,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _kb_to_domain(row: KnowledgeBaseRow, document_count: int = 0) -> KnowledgeBase:
    """把知识库 ORM 行转换成脱离 Session 的领域对象。"""
    return KnowledgeBase(
        id=row.id,
        security_scope_id=row.security_scope_id,
        scope_type=KnowledgeBaseScopeType(row.scope_type),
        external_id=row.external_id,
        name=row.name,
        description=row.description,
        ragflow_dataset_id=row.ragflow_dataset_id,
        parser_profile_ref=row.parser_profile_ref,
        chunk_profile_ref=row.chunk_profile_ref,
        embedding_profile_ref=row.embedding_profile_ref,
        retrieval_profile_ref=row.retrieval_profile_ref,
        status=KnowledgeBaseStatus(row.status),
        revision=row.revision,
        document_count=document_count,
        row_version=row.row_version,
        last_error_code=row.last_error_code,
        last_error_message=row.last_error_message,
        deleted_at=row.deleted_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _knowledge_base_document_count() -> Any:
    """构造按知识库相关的未删除文档计数标量子查询。"""
    return (
        select(func.count(DocumentRecordRow.id))
        .where(
            DocumentRecordRow.security_scope_id == KnowledgeBaseRow.security_scope_id,
            DocumentRecordRow.knowledge_base_id == KnowledgeBaseRow.id,
            DocumentRecordRow.status != DocumentStatus.DELETED.value,
        )
        .correlate(KnowledgeBaseRow)
        .scalar_subquery()
    )


class SqlAlchemyKnowledgeBaseRepository:
    """知识库 CRUD、过滤分页和乐观锁更新实现。"""
    def __init__(self, session: AsyncSession) -> None:
        """保存由外层请求/Worker 管理事务的 Session。"""
        self._session = session

    async def increment_revision(self, security_scope_id: str, knowledge_base_id: UUID) -> bool:
        """只递增版本，不写回旧对象，避免并发解析覆盖知识库名称、状态等字段。"""
        result = await self._session.execute(
            update(KnowledgeBaseRow)
            .where(
                KnowledgeBaseRow.security_scope_id == security_scope_id,
                KnowledgeBaseRow.id == knowledge_base_id,
                KnowledgeBaseRow.status != KnowledgeBaseStatus.DELETED.value,
            )
            .values(
                revision=KnowledgeBaseRow.revision + 1,
                # 同步推进行版本，使普通编辑仍能检测到内容变化。
                row_version=KnowledgeBaseRow.row_version + 1,
            )
        )
        return getattr(result, "rowcount", 0) == 1

    async def add(self, security_scope_id: str, value: KnowledgeBase) -> None:
        """验证对象范围，插入知识库并 flush 以尽早暴露约束错误。"""
        _require_matching_scope(security_scope_id, value.security_scope_id)
        self._session.add(
            KnowledgeBaseRow(
                id=value.id,
                security_scope_id=security_scope_id,
                scope_type=value.scope_type.value,
                external_id=value.external_id,
                name=value.name,
                description=value.description,
                ragflow_dataset_id=value.ragflow_dataset_id,
                parser_profile_ref=value.parser_profile_ref,
                chunk_profile_ref=value.chunk_profile_ref,
                embedding_profile_ref=value.embedding_profile_ref,
                retrieval_profile_ref=value.retrieval_profile_ref,
                status=value.status.value,
                revision=value.revision,
                row_version=value.row_version,
                last_error_code=value.last_error_code,
                last_error_message=value.last_error_message,
                deleted_at=value.deleted_at,
                created_at=value.created_at,
                updated_at=value.updated_at,
            )
        )
        await self._session.flush()

    async def get(self, security_scope_id: str, knowledge_base_id: UUID) -> KnowledgeBase | None:
        """按范围和 ID 读取未软删除知识库及其实时文档数。"""
        return await self.get_accessible((security_scope_id,), knowledge_base_id)

    async def get_accessible(
        self,
        security_scope_ids: Sequence[str],
        knowledge_base_id: UUID,
    ) -> KnowledgeBase | None:
        """按候选资源范围和 ID 读取知识库。"""
        if not security_scope_ids:
            return None
        row = (
            await self._session.execute(
                select(KnowledgeBaseRow, _knowledge_base_document_count()).where(
                    KnowledgeBaseRow.security_scope_id.in_(security_scope_ids),
                    KnowledgeBaseRow.id == knowledge_base_id,
                    KnowledgeBaseRow.status != KnowledgeBaseStatus.DELETED.value,
                )
            )
        ).one_or_none()
        return _kb_to_domain(row[0], int(row[1] or 0)) if row is not None else None

    async def get_deleted(
        self, security_scope_id: str, knowledge_base_id: UUID
    ) -> KnowledgeBase | None:
        """精确按任务的资源范围读取软删除知识库，不让普通列表重新显示它。"""
        row = (
            await self._session.execute(
                select(KnowledgeBaseRow).where(
                    KnowledgeBaseRow.security_scope_id == security_scope_id,
                    KnowledgeBaseRow.id == knowledge_base_id,
                    KnowledgeBaseRow.status == KnowledgeBaseStatus.DELETED.value,
                )
            )
        ).scalar_one_or_none()
        return _kb_to_domain(row) if row is not None else None

    async def get_many(
        self, security_scope_id: str, knowledge_base_ids: Sequence[UUID]
    ) -> tuple[KnowledgeBase, ...]:
        """在同一范围批量读取知识库，调用方可用返回数量检查输入完整性。"""
        return await self.get_many_accessible((security_scope_id,), knowledge_base_ids)

    async def get_many_accessible(
        self,
        security_scope_ids: Sequence[str],
        knowledge_base_ids: Sequence[UUID],
    ) -> tuple[KnowledgeBase, ...]:
        """按候选资源范围批量读取知识库。"""
        if not security_scope_ids or not knowledge_base_ids:
            return ()
        rows = (
            await self._session.execute(
                select(KnowledgeBaseRow, _knowledge_base_document_count())
                .where(
                    KnowledgeBaseRow.security_scope_id.in_(security_scope_ids),
                    KnowledgeBaseRow.id.in_(knowledge_base_ids),
                    KnowledgeBaseRow.status != KnowledgeBaseStatus.DELETED.value,
                )
                .order_by(KnowledgeBaseRow.id)
            )
        ).all()
        return tuple(_kb_to_domain(row, int(document_count or 0)) for row, document_count in rows)

    async def list(
        self,
        security_scope_id: str,
        *,
        page: int,
        page_size: int,
        name: str | None = None,
        status: str | None = None,
        scope_type: str | None = None,
    ) -> tuple[tuple[KnowledgeBase, ...], int]:
        """按名称/状态可选过滤，返回当前页与相同条件下的总数。"""
        return await self.list_accessible(
            (security_scope_id,),
            page=page,
            page_size=page_size,
            name=name,
            status=status,
            scope_type=scope_type,
        )

    async def list_accessible(
        self,
        security_scope_ids: Sequence[str],
        *,
        page: int,
        page_size: int,
        name: str | None = None,
        status: str | None = None,
        scope_type: str | None = None,
    ) -> tuple[tuple[KnowledgeBase, ...], int]:
        """按候选资源范围分页列出知识库。"""
        if not security_scope_ids:
            return (), 0
        filters: list[Any] = [
            KnowledgeBaseRow.security_scope_id.in_(security_scope_ids),
            KnowledgeBaseRow.status != KnowledgeBaseStatus.DELETED.value,
        ]
        if name:
            filters.append(KnowledgeBaseRow.name.ilike(f"%{name}%"))
        if status:
            filters.append(KnowledgeBaseRow.status == status)
        if scope_type:
            filters.append(KnowledgeBaseRow.scope_type == scope_type)
        total = await self._session.scalar(
            select(func.count()).select_from(KnowledgeBaseRow).where(*filters)
        )
        rows = (
            await self._session.execute(
                select(KnowledgeBaseRow, _knowledge_base_document_count())
                .where(*filters)
                .order_by(KnowledgeBaseRow.created_at.desc(), KnowledgeBaseRow.id)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
        return (
            tuple(_kb_to_domain(row, int(document_count or 0)) for row, document_count in rows),
            int(total or 0),
        )

    async def list_ready_accessible(
        self,
        security_scope_ids: Sequence[str],
    ) -> tuple[KnowledgeBase, ...]:
        """列出候选资源范围内全部 READY 知识库。"""
        if not security_scope_ids:
            return ()
        rows = (
            await self._session.execute(
                select(KnowledgeBaseRow, _knowledge_base_document_count())
                .where(
                    KnowledgeBaseRow.security_scope_id.in_(security_scope_ids),
                    KnowledgeBaseRow.status == KnowledgeBaseStatus.READY.value,
                )
                .order_by(KnowledgeBaseRow.created_at, KnowledgeBaseRow.id)
            )
        ).all()
        return tuple(
            _kb_to_domain(row, int(document_count or 0))
            for row, document_count in rows
        )

    async def update(
        self, security_scope_id: str, value: KnowledgeBase, expected_row_version: int
    ) -> bool:
        """仅当 ``row_version`` 等于预期值时更新，并在成功时原子递增版本。"""
        _require_matching_scope(security_scope_id, value.security_scope_id)
        result = await self._session.execute(
            update(KnowledgeBaseRow)
            .where(
                KnowledgeBaseRow.security_scope_id == security_scope_id,
                KnowledgeBaseRow.id == value.id,
                KnowledgeBaseRow.row_version == expected_row_version,
            )
            .values(
                scope_type=value.scope_type.value,
                external_id=value.external_id,
                name=value.name,
                description=value.description,
                ragflow_dataset_id=value.ragflow_dataset_id,
                parser_profile_ref=value.parser_profile_ref,
                chunk_profile_ref=value.chunk_profile_ref,
                embedding_profile_ref=value.embedding_profile_ref,
                retrieval_profile_ref=value.retrieval_profile_ref,
                status=value.status.value,
                revision=value.revision,
                row_version=expected_row_version + 1,
                last_error_code=value.last_error_code,
                last_error_message=value.last_error_message,
                deleted_at=value.deleted_at,
            )
        )
        if getattr(result, "rowcount", 0) == 1:
            value.row_version = expected_row_version + 1
            return True
        return False


def _document_to_domain(row: DocumentRecordRow) -> DocumentRecord:
    """把文档 ORM 行、活动版本指针和 JSON 元数据转换为领域对象。"""
    return DocumentRecord(
        id=row.id,
        security_scope_id=row.security_scope_id,
        knowledge_base_id=row.knowledge_base_id,
        external_id=row.external_id,
        ragflow_document_id=row.ragflow_document_id,
        active_version_id=row.active_version_id,
        active_version_number=row.active_version_number,
        document_revision=row.document_revision,
        filename=row.filename,
        safe_filename=row.safe_filename,
        mime_type=row.mime_type,
        file_size_bytes=row.file_size_bytes,
        content_sha256=row.content_sha256,
        metadata=row.metadata_json or {},
        parser_profile_ref=row.parser_profile_ref,
        chunk_profile_ref=row.chunk_profile_ref,
        status=DocumentStatus(row.status),
        progress=float(row.progress),
        chunk_count=row.chunk_count,
        token_count=row.token_count,
        row_version=row.row_version,
        last_error_code=row.last_error_code,
        last_error_message=row.last_error_message,
        engine_progress_message=row.engine_progress_message,
        created_at=row.created_at,
        updated_at=row.updated_at,
        indexed_at=row.indexed_at,
        deleted_at=row.deleted_at,
    )


class SqlAlchemyDocumentRepository:
    """文档 CRUD、引擎 ID 映射、语种统计、过滤分页和乐观锁实现。"""
    def __init__(self, session: AsyncSession) -> None:
        """保存当前事务 Session。"""
        self._session = session

    async def add(self, security_scope_id: str, value: DocumentRecord) -> None:
        """插入文档完整性、Profile、状态和错误字段。"""
        _require_matching_scope(security_scope_id, value.security_scope_id)
        self._session.add(
            DocumentRecordRow(
                id=value.id,
                security_scope_id=security_scope_id,
                knowledge_base_id=value.knowledge_base_id,
                external_id=value.external_id,
                ragflow_document_id=value.ragflow_document_id,
                active_version_id=value.active_version_id,
                active_version_number=value.active_version_number,
                document_revision=value.document_revision,
                filename=value.filename,
                safe_filename=value.safe_filename,
                mime_type=value.mime_type,
                file_size_bytes=value.file_size_bytes,
                content_sha256=value.content_sha256,
                metadata_json=value.metadata,
                parser_profile_ref=value.parser_profile_ref,
                chunk_profile_ref=value.chunk_profile_ref,
                status=value.status.value,
                progress=value.progress,
                chunk_count=value.chunk_count,
                token_count=value.token_count,
                row_version=value.row_version,
                last_error_code=value.last_error_code,
                last_error_message=value.last_error_message,
                engine_progress_message=value.engine_progress_message,
                created_at=value.created_at,
                updated_at=value.updated_at,
                indexed_at=value.indexed_at,
                deleted_at=value.deleted_at,
            )
        )
        await self._session.flush()

    async def get(self, security_scope_id: str, document_id: UUID) -> DocumentRecord | None:
        """按范围和 ID 读取未软删除文档。"""
        return await self.get_accessible((security_scope_id,), document_id)

    async def get_accessible(
        self,
        security_scope_ids: Sequence[str],
        document_id: UUID,
    ) -> DocumentRecord | None:
        """按候选资源范围和 ID 读取未软删除文档。"""
        if not security_scope_ids:
            return None
        row = await self._session.scalar(
            select(DocumentRecordRow).where(
                DocumentRecordRow.security_scope_id.in_(security_scope_ids),
                DocumentRecordRow.id == document_id,
                DocumentRecordRow.status != DocumentStatus.DELETED.value,
            )
        )
        return _document_to_domain(row) if row is not None else None

    async def get_many(
        self, security_scope_id: str, document_ids: Sequence[UUID]
    ) -> tuple[DocumentRecord, ...]:
        """按范围批量读取未删除文档。"""
        return await self.get_many_accessible((security_scope_id,), document_ids)

    async def get_many_accessible(
        self,
        security_scope_ids: Sequence[str],
        document_ids: Sequence[UUID],
    ) -> tuple[DocumentRecord, ...]:
        """按候选资源范围批量读取未删除文档。"""
        if not security_scope_ids or not document_ids:
            return ()
        rows = (
            await self._session.scalars(
                select(DocumentRecordRow)
                .where(
                    DocumentRecordRow.security_scope_id.in_(security_scope_ids),
                    DocumentRecordRow.id.in_(document_ids),
                    DocumentRecordRow.status != DocumentStatus.DELETED.value,
                )
                .order_by(DocumentRecordRow.id)
            )
        ).all()
        return tuple(_document_to_domain(row) for row in rows)

    async def get_by_engine_ids(
        self, security_scope_id: str, engine_document_ids: Sequence[str]
    ) -> tuple[DocumentRecord, ...]:
        """在当前范围把一批 RAGFlow document ID 映射回本地文档。"""
        if not engine_document_ids:
            return ()
        rows = (
            await self._session.scalars(
                select(DocumentRecordRow).where(
                    DocumentRecordRow.security_scope_id == security_scope_id,
                    DocumentRecordRow.ragflow_document_id.in_(engine_document_ids),
                    DocumentRecordRow.status != DocumentStatus.DELETED.value,
                )
            )
        ).all()
        return tuple(_document_to_domain(row) for row in rows)

    async def find_active_by_content_sha256(
        self,
        security_scope_id: str,
        knowledge_base_id: UUID,
        content_sha256: str,
    ) -> DocumentRecord | None:
        """按租户、知识库和原始文件摘要查找一条未删除文档。

        三个条件缺一不可：安全范围防止跨用户泄漏，知识库 ID 允许同一文件被明确
        放入不同知识库，SHA-256 则保证只有逐字节完全一致的文件才会命中。这里不
        限制 READY 状态，因为正在上传、解析或失败但尚未删除的记录仍占有该文件，
        再次创建只会产生重复存储和重复索引。查询前使用事务级 PostgreSQL advisory
        lock 串行化同一去重键：两个真正同时到达、且使用不同幂等键的请求也不会都
        在“尚无记录”的窗口中通过检查。锁只持续到当前请求事务提交，不创建新表、
        不影响其他文件，也不需要清理历史数据才能增加唯一索引。
        """
        lock_key = f"{security_scope_id}:{knowledge_base_id}:{content_sha256}"
        await self._session.execute(
            select(func.pg_advisory_xact_lock(func.hashtextextended(lock_key, 0)))
        )
        row = await self._session.scalar(
            select(DocumentRecordRow)
            .where(
                DocumentRecordRow.security_scope_id == security_scope_id,
                DocumentRecordRow.knowledge_base_id == knowledge_base_id,
                DocumentRecordRow.content_sha256 == content_sha256,
                DocumentRecordRow.status != DocumentStatus.DELETED.value,
            )
            .order_by(DocumentRecordRow.created_at, DocumentRecordRow.id)
            .limit(1)
        )
        return _document_to_domain(row) if row is not None else None

    async def language_counts(
        self,
        security_scope_id: str,
        knowledge_base_ids: Sequence[UUID],
    ) -> dict[UUID, dict[str | None, int]]:
        """按知识库聚合 READY 文档 metadata 中的 language/lang 值和数量。"""
        if not knowledge_base_ids:
            return {}
        language = func.coalesce(
            DocumentRecordRow.metadata_json["language"].astext,
            DocumentRecordRow.metadata_json["lang"].astext,
            DocumentRecordRow.metadata_json["lazytt_language"].astext,
        )
        rows = (
            await self._session.execute(
                select(
                    DocumentRecordRow.knowledge_base_id,
                    language.label("language"),
                    func.count().label("document_count"),
                )
                .where(
                    DocumentRecordRow.security_scope_id == security_scope_id,
                    DocumentRecordRow.knowledge_base_id.in_(knowledge_base_ids),
                    DocumentRecordRow.status != DocumentStatus.DELETED.value,
                )
                .group_by(DocumentRecordRow.knowledge_base_id, language)
            )
        ).all()
        result: dict[UUID, dict[str | None, int]] = {}
        for knowledge_base_id, raw_language, count in rows:
            result.setdefault(knowledge_base_id, {})[
                str(raw_language) if raw_language is not None else None
            ] = int(count)
        return result

    async def list(
        self,
        security_scope_id: str,
        knowledge_base_id: UUID,
        *,
        page: int,
        page_size: int,
        status: str | None = None,
        filename: str | None = None,
        external_id: str | None = None,
    ) -> tuple[tuple[DocumentRecord, ...], int]:
        """在指定知识库内按状态、文件名、外部 ID 过滤分页。"""
        filters: list[Any] = [
            DocumentRecordRow.security_scope_id == security_scope_id,
            DocumentRecordRow.knowledge_base_id == knowledge_base_id,
            DocumentRecordRow.status != DocumentStatus.DELETED.value,
        ]
        if status:
            filters.append(DocumentRecordRow.status == status)
        if filename:
            filters.append(DocumentRecordRow.filename.ilike(f"%{filename}%"))
        if external_id:
            filters.append(DocumentRecordRow.external_id == external_id)
        total = await self._session.scalar(
            select(func.count()).select_from(DocumentRecordRow).where(*filters)
        )
        rows = (
            await self._session.scalars(
                select(DocumentRecordRow)
                .where(*filters)
                .order_by(DocumentRecordRow.created_at.desc(), DocumentRecordRow.id)
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
        return tuple(_document_to_domain(row) for row in rows), int(total or 0)

    async def update(
        self, security_scope_id: str, value: DocumentRecord, expected_row_version: int
    ) -> bool:
        """以范围、ID、预期 row_version 原子保存文档并递增乐观锁版本。"""
        _require_matching_scope(security_scope_id, value.security_scope_id)
        result = await self._session.execute(
            update(DocumentRecordRow)
            .where(
                DocumentRecordRow.security_scope_id == security_scope_id,
                DocumentRecordRow.id == value.id,
                DocumentRecordRow.row_version == expected_row_version,
            )
            .values(
                external_id=value.external_id,
                ragflow_document_id=value.ragflow_document_id,
                active_version_id=value.active_version_id,
                active_version_number=value.active_version_number,
                document_revision=value.document_revision,
                filename=value.filename,
                safe_filename=value.safe_filename,
                mime_type=value.mime_type,
                file_size_bytes=value.file_size_bytes,
                content_sha256=value.content_sha256,
                metadata_json=value.metadata,
                parser_profile_ref=value.parser_profile_ref,
                chunk_profile_ref=value.chunk_profile_ref,
                status=value.status.value,
                progress=value.progress,
                chunk_count=value.chunk_count,
                token_count=value.token_count,
                indexed_at=value.indexed_at,
                last_error_code=value.last_error_code,
                last_error_message=value.last_error_message,
                engine_progress_message=value.engine_progress_message,
                deleted_at=value.deleted_at,
                row_version=expected_row_version + 1,
            )
        )
        if getattr(result, "rowcount", 0) == 1:
            value.row_version = expected_row_version + 1
            return True
        return False


def _document_version_to_domain(row: DocumentVersionRow) -> DocumentVersion:
    """把版本 ORM 行转换为不依赖 Session 的领域版本对象。"""
    return DocumentVersion(
        id=row.id,
        security_scope_id=row.security_scope_id,
        knowledge_base_id=row.knowledge_base_id,
        document_id=row.document_id,
        version_number=row.version_number,
        ragflow_document_id=row.ragflow_document_id,
        filename=row.filename,
        safe_filename=row.safe_filename,
        mime_type=row.mime_type,
        file_size_bytes=row.file_size_bytes,
        content_sha256=row.content_sha256,
        metadata=dict(row.metadata_json),
        parser_profile_ref=row.parser_profile_ref,
        chunk_profile_ref=row.chunk_profile_ref,
        status=DocumentVersionStatus(row.status),
        requested_mode=DocumentUpdateMode(row.requested_mode),
        effective_mode=DocumentUpdateMode(row.effective_mode),
        chunk_count=row.chunk_count,
        token_count=row.token_count,
        last_error_code=row.last_error_code,
        last_error_message=row.last_error_message,
        created_at=row.created_at,
        updated_at=row.updated_at,
        activated_at=row.activated_at,
        retired_at=row.retired_at,
    )


class SqlAlchemyDocumentVersionRepository:
    """文档版本编号、状态、历史和原子 ACTIVE 切换的 PostgreSQL 实现。

    Repository 复用调用方事务 Session；``activate`` 只切换版本行，主文档、侧车
    索引和知识库 Revision 由 Worker 在同一事务中继续更新后统一提交。任何一步失败
    都会整体回滚，不会留下两个 ACTIVE 版本或主记录指向未发布候选的中间状态。
    """

    def __init__(self, session: AsyncSession) -> None:
        """保存当前请求或 Worker 任务的事务 Session。"""
        self._session = session

    async def add(self, security_scope_id: str, value: DocumentVersion) -> None:
        """验证 Scope 后插入文件身份、Profile、执行模式和版本状态。"""
        _require_matching_scope(security_scope_id, value.security_scope_id)
        self._session.add(
            DocumentVersionRow(
                id=value.id,
                security_scope_id=security_scope_id,
                knowledge_base_id=value.knowledge_base_id,
                document_id=value.document_id,
                version_number=value.version_number,
                ragflow_document_id=value.ragflow_document_id,
                filename=value.filename,
                safe_filename=value.safe_filename,
                mime_type=value.mime_type,
                file_size_bytes=value.file_size_bytes,
                content_sha256=value.content_sha256,
                metadata_json=value.metadata,
                parser_profile_ref=value.parser_profile_ref,
                chunk_profile_ref=value.chunk_profile_ref,
                status=value.status.value,
                requested_mode=value.requested_mode.value,
                effective_mode=value.effective_mode.value,
                chunk_count=value.chunk_count,
                token_count=value.token_count,
                last_error_code=value.last_error_code,
                last_error_message=value.last_error_message,
                activated_at=value.activated_at,
                retired_at=value.retired_at,
                created_at=value.created_at,
                updated_at=value.updated_at,
            )
        )
        await self._session.flush()

    async def get(
        self, security_scope_id: str, version_id: UUID
    ) -> DocumentVersion | None:
        """按 Scope 和版本 UUID 读取，跨租户与不存在使用同一个空结果。"""
        row = await self._session.scalar(
            select(DocumentVersionRow).where(
                DocumentVersionRow.security_scope_id == security_scope_id,
                DocumentVersionRow.id == version_id,
            )
        )
        return _document_version_to_domain(row) if row is not None else None

    async def get_active(
        self, security_scope_id: str, document_id: UUID
    ) -> DocumentVersion | None:
        """读取同一 Scope、逻辑文档下唯一 ACTIVE 行。"""
        row = await self._session.scalar(
            select(DocumentVersionRow).where(
                DocumentVersionRow.security_scope_id == security_scope_id,
                DocumentVersionRow.document_id == document_id,
                DocumentVersionRow.status == DocumentVersionStatus.ACTIVE.value,
            )
        )
        return _document_version_to_domain(row) if row is not None else None

    async def next_version_number(
        self, security_scope_id: str, document_id: UUID
    ) -> int:
        """锁定 Scope+Document 编号键后读取最大版本并返回下一编号。"""
        lock_key = f"document-version:{security_scope_id}:{document_id}"
        await self._session.execute(
            select(func.pg_advisory_xact_lock(func.hashtextextended(lock_key, 0)))
        )
        current = await self._session.scalar(
            select(func.max(DocumentVersionRow.version_number)).where(
                DocumentVersionRow.security_scope_id == security_scope_id,
                DocumentVersionRow.document_id == document_id,
            )
        )
        return int(current or 0) + 1

    async def list(
        self,
        security_scope_id: str,
        document_id: UUID,
        *,
        page: int,
        page_size: int,
    ) -> tuple[tuple[DocumentVersion, ...], int]:
        """分页返回一个逻辑文档的版本历史，最新版本排在最前。"""
        filters = (
            DocumentVersionRow.security_scope_id == security_scope_id,
            DocumentVersionRow.document_id == document_id,
        )
        total = await self._session.scalar(
            select(func.count()).select_from(DocumentVersionRow).where(*filters)
        )
        rows = (
            await self._session.scalars(
                select(DocumentVersionRow)
                .where(*filters)
                .order_by(DocumentVersionRow.version_number.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        ).all()
        return tuple(_document_version_to_domain(row) for row in rows), int(total or 0)

    async def update(self, security_scope_id: str, value: DocumentVersion) -> bool:
        """保存候选处理状态和统计；文件摘要与版本号创建后不可变。"""
        _require_matching_scope(security_scope_id, value.security_scope_id)
        result = await self._session.execute(
            update(DocumentVersionRow)
            .where(
                DocumentVersionRow.security_scope_id == security_scope_id,
                DocumentVersionRow.id == value.id,
                DocumentVersionRow.document_id == value.document_id,
            )
            .values(
                ragflow_document_id=value.ragflow_document_id,
                metadata_json=value.metadata,
                status=value.status.value,
                requested_mode=value.requested_mode.value,
                effective_mode=value.effective_mode.value,
                chunk_count=value.chunk_count,
                token_count=value.token_count,
                last_error_code=value.last_error_code,
                last_error_message=value.last_error_message,
                activated_at=value.activated_at,
                retired_at=value.retired_at,
            )
        )
        return getattr(result, "rowcount", 0) == 1

    async def activate(
        self,
        security_scope_id: str,
        *,
        document_id: UUID,
        version_id: UUID,
        activated_at: datetime,
    ) -> bool:
        """退役旧 ACTIVE，再将同 Scope、同文档的 READY 候选切换为 ACTIVE。

        候选在 Worker 中以 ``INDEXING`` 表示 RAGFlow 已完成且侧车产物已准备好。
        先锁定版本集合可串行化并发发布；目标不存在或状态不符时返回 False，由应用层
        转换为可重试冲突，整个外层事务会回滚旧版本退役操作。
        """
        await self._session.execute(
            select(DocumentVersionRow.id)
            .where(
                DocumentVersionRow.security_scope_id == security_scope_id,
                DocumentVersionRow.document_id == document_id,
            )
            .with_for_update()
        )
        await self._session.execute(
            update(DocumentVersionRow)
            .where(
                DocumentVersionRow.security_scope_id == security_scope_id,
                DocumentVersionRow.document_id == document_id,
                DocumentVersionRow.status == DocumentVersionStatus.ACTIVE.value,
            )
            .values(
                status=DocumentVersionStatus.RETIRED.value,
                retired_at=activated_at,
            )
        )
        result = await self._session.execute(
            update(DocumentVersionRow)
            .where(
                DocumentVersionRow.security_scope_id == security_scope_id,
                DocumentVersionRow.document_id == document_id,
                DocumentVersionRow.id == version_id,
                DocumentVersionRow.status == DocumentVersionStatus.INDEXING.value,
            )
            .values(
                status=DocumentVersionStatus.ACTIVE.value,
                activated_at=activated_at,
                retired_at=None,
                last_error_code=None,
                last_error_message=None,
            )
        )
        return getattr(result, "rowcount", 0) == 1


class SqlAlchemyIndexNodeRepository:
    """层级/摘要侧车索引的 PostgreSQL 实现，所有查询先应用安全范围。"""

    def __init__(self, session: AsyncSession) -> None:
        """复用外层事务 Session，使索引替换与文档 READY 状态能够一起提交。"""
        self._session = session

    async def replace_document_nodes(
        self,
        security_scope_id: str,
        document_id: UUID,
        nodes: Sequence[DocumentIndexNode],
    ) -> None:
        """先删除该文档旧节点，再批量写入本次解析对应的新版本节点。

        每个节点都必须与方法参数属于同一租户和文档；验证发生在执行 DELETE 前，
        因而调用方传错对象不会先清空正确索引。外层事务若后续失败会整体回滚，避免
        读到一半旧节点、一半新节点的混合状态。
        """
        if any(
            node.security_scope_id != security_scope_id or node.document_id != document_id
            for node in nodes
        ):
            raise ValueError("index nodes do not match repository scope or document")
        await self._session.execute(
            delete(DocumentIndexNodeRow).where(
                DocumentIndexNodeRow.security_scope_id == security_scope_id,
                DocumentIndexNodeRow.document_id == document_id,
            )
        )
        for node in nodes:
            embedding_bytes = (
                struct.pack(f"<{len(node.embedding)}f", *node.embedding)
                if node.embedding
                else None
            )
            self._session.add(
                DocumentIndexNodeRow(
                    id=node.id,
                    security_scope_id=security_scope_id,
                    knowledge_base_id=node.knowledge_base_id,
                    document_id=node.document_id,
                    node_type=node.node_type.value,
                    ordinal=node.ordinal,
                    content=node.content,
                    source_chunk_ids_json=list(node.source_chunk_ids),
                    embedding_bytes=embedding_bytes,
                    embedding_dimension=len(node.embedding),
                    metadata_json=node.metadata,
                    index_version=node.index_version,
                    created_at=node.created_at,
                    updated_at=node.updated_at,
                )
            )
        await self._session.flush()

    async def list_for_knowledge_bases(
        self,
        security_scope_id: str,
        knowledge_base_ids: Sequence[UUID],
        *,
        node_types: Sequence[IndexNodeType],
        limit: int,
    ) -> tuple[DocumentIndexNode, ...]:
        """只返回范围内、知识库内且所属文档当前 READY 的可用索引节点。"""
        if not knowledge_base_ids or not node_types or limit <= 0:
            return ()
        rows = (
            await self._session.scalars(
                select(DocumentIndexNodeRow)
                .join(
                    DocumentRecordRow,
                    and_(
                        DocumentRecordRow.security_scope_id
                        == DocumentIndexNodeRow.security_scope_id,
                        DocumentRecordRow.id == DocumentIndexNodeRow.document_id,
                    ),
                )
                .where(
                    DocumentIndexNodeRow.security_scope_id == security_scope_id,
                    DocumentIndexNodeRow.knowledge_base_id.in_(knowledge_base_ids),
                    DocumentIndexNodeRow.node_type.in_(
                        [node_type.value for node_type in node_types]
                    ),
                    DocumentRecordRow.status == DocumentStatus.READY.value,
                )
                .order_by(
                    DocumentIndexNodeRow.document_id,
                    DocumentIndexNodeRow.node_type,
                    DocumentIndexNodeRow.ordinal,
                )
                .limit(limit)
            )
        ).all()
        return tuple(_index_node_to_domain(row) for row in rows)

    async def find_parents_for_chunks(
        self,
        security_scope_id: str,
        knowledge_base_ids: Sequence[UUID],
        source_chunk_ids: Sequence[str],
        *,
        limit: int,
    ) -> tuple[DocumentIndexNode, ...]:
        """使用 JSONB ``?|`` 索引查找包含任一真实 Chunk ID 的 Parent 节点。"""
        if not knowledge_base_ids or not source_chunk_ids or limit <= 0:
            return ()
        rows = (
            await self._session.scalars(
                select(DocumentIndexNodeRow)
                .join(
                    DocumentRecordRow,
                    and_(
                        DocumentRecordRow.security_scope_id
                        == DocumentIndexNodeRow.security_scope_id,
                        DocumentRecordRow.id == DocumentIndexNodeRow.document_id,
                    ),
                )
                .where(
                    DocumentIndexNodeRow.security_scope_id == security_scope_id,
                    DocumentIndexNodeRow.knowledge_base_id.in_(knowledge_base_ids),
                    DocumentIndexNodeRow.node_type == IndexNodeType.PARENT.value,
                    DocumentIndexNodeRow.source_chunk_ids_json.op("?|")(
                        array(list(dict.fromkeys(source_chunk_ids)))
                    ),
                    DocumentRecordRow.status == DocumentStatus.READY.value,
                )
                .order_by(DocumentIndexNodeRow.document_id, DocumentIndexNodeRow.ordinal)
                .limit(limit)
            )
        ).all()
        return tuple(_index_node_to_domain(row) for row in rows)


class SqlAlchemyEntityGraphRepository:
    """实体/关系侧车索引的 PostgreSQL 实现，查询只返回 READY 文档证据。"""

    def __init__(self, session: AsyncSession) -> None:
        """保存由 API 请求或 Worker 外层管理提交/回滚的事务 Session。"""
        self._session = session

    async def replace_document_graph(
        self,
        security_scope_id: str,
        document_id: UUID,
        entities: Sequence[DocumentEntity],
        relations: Sequence[EntityRelation],
    ) -> None:
        """验证全部新对象后，原子替换一个文档的实体与关系。

        先验证再 DELETE，防止调用方误传其他租户对象导致正确索引被提前清空；关系
        先删、实体后删与降级顺序一致。真正提交由外层事务控制，任何写入异常都会
        恢复旧版本，不会留下半张图。
        """
        entity_mismatch = any(
            entity.security_scope_id != security_scope_id or entity.document_id != document_id
            for entity in entities
        )
        relation_mismatch = any(
            relation.security_scope_id != security_scope_id
            or relation.document_id != document_id
            for relation in relations
        )
        if entity_mismatch or relation_mismatch:
            raise ValueError("entity graph values do not match repository scope or document")
        await self._session.execute(
            delete(EntityRelationRow).where(
                EntityRelationRow.security_scope_id == security_scope_id,
                EntityRelationRow.document_id == document_id,
            )
        )
        await self._session.execute(
            delete(DocumentEntityRow).where(
                DocumentEntityRow.security_scope_id == security_scope_id,
                DocumentEntityRow.document_id == document_id,
            )
        )
        for entity in entities:
            self._session.add(
                DocumentEntityRow(
                    id=entity.id,
                    security_scope_id=security_scope_id,
                    knowledge_base_id=entity.knowledge_base_id,
                    document_id=entity.document_id,
                    canonical_name=entity.canonical_name,
                    normalized_name=entity.normalized_name,
                    entity_type=entity.entity_type,
                    aliases_json=list(entity.aliases),
                    source_chunk_ids_json=list(entity.source_chunk_ids),
                    mention_count=entity.mention_count,
                    metadata_json=entity.metadata,
                    index_version=entity.index_version,
                    created_at=entity.created_at,
                    updated_at=entity.updated_at,
                )
            )
        for relation in relations:
            self._session.add(
                EntityRelationRow(
                    id=relation.id,
                    security_scope_id=security_scope_id,
                    knowledge_base_id=relation.knowledge_base_id,
                    document_id=relation.document_id,
                    subject_name=relation.subject_name,
                    subject_normalized=relation.subject_normalized,
                    predicate=relation.predicate,
                    object_name=relation.object_name,
                    object_normalized=relation.object_normalized,
                    source_chunk_ids_json=list(relation.source_chunk_ids),
                    confidence=relation.confidence,
                    metadata_json=relation.metadata,
                    index_version=relation.index_version,
                    created_at=relation.created_at,
                    updated_at=relation.updated_at,
                )
            )
        await self._session.flush()

    async def search_evidence(
        self,
        security_scope_id: str,
        knowledge_base_ids: Sequence[UUID],
        query: str,
        *,
        limit: int,
    ) -> tuple[GraphEvidence, ...]:
        """按查询中明确出现的实体名查找关系/实体，并保持租户与 READY 边界。

        这里采用“查询包含完整规范化实体名”，而不是数据库模糊相似搜索，避免短词
        造成大面积误召回。关系证据权重略高于孤立实体，因为它同时匹配两个有方向
        的节点；返回值仅含真实源 Chunk ID，后续仍由在线层回读 RAGFlow 原文。
        """
        normalized_query = " ".join(query.casefold().split())
        if not knowledge_base_ids or len(normalized_query) < 2 or limit <= 0:
            return ()
        query_literal = literal(normalized_query)
        relation_rows = (
            await self._session.scalars(
                select(EntityRelationRow)
                .join(
                    DocumentRecordRow,
                    and_(
                        DocumentRecordRow.security_scope_id
                        == EntityRelationRow.security_scope_id,
                        DocumentRecordRow.id == EntityRelationRow.document_id,
                    ),
                )
                .where(
                    EntityRelationRow.security_scope_id == security_scope_id,
                    EntityRelationRow.knowledge_base_id.in_(knowledge_base_ids),
                    DocumentRecordRow.status == DocumentStatus.READY.value,
                    or_(
                        func.strpos(query_literal, EntityRelationRow.subject_normalized) > 0,
                        func.strpos(query_literal, EntityRelationRow.object_normalized) > 0,
                    ),
                )
                .order_by(EntityRelationRow.confidence.desc(), EntityRelationRow.created_at)
                .limit(limit)
            )
        ).all()
        entity_rows = (
            await self._session.scalars(
                select(DocumentEntityRow)
                .join(
                    DocumentRecordRow,
                    and_(
                        DocumentRecordRow.security_scope_id
                        == DocumentEntityRow.security_scope_id,
                        DocumentRecordRow.id == DocumentEntityRow.document_id,
                    ),
                )
                .where(
                    DocumentEntityRow.security_scope_id == security_scope_id,
                    DocumentEntityRow.knowledge_base_id.in_(knowledge_base_ids),
                    DocumentRecordRow.status == DocumentStatus.READY.value,
                    func.char_length(DocumentEntityRow.normalized_name) >= 2,
                    func.strpos(query_literal, DocumentEntityRow.normalized_name) > 0,
                )
                .order_by(DocumentEntityRow.mention_count.desc(), DocumentEntityRow.created_at)
                .limit(limit)
            )
        ).all()
        evidence = [
            GraphEvidence(
                knowledge_base_id=row.knowledge_base_id,
                document_id=row.document_id,
                source_chunk_ids=tuple(row.source_chunk_ids_json),
                score=1.2 * float(row.confidence),
                matched_text=f"{row.subject_name} {row.predicate} {row.object_name}",
                evidence_type="RELATION",
            )
            for row in relation_rows
        ]
        evidence.extend(
            GraphEvidence(
                knowledge_base_id=row.knowledge_base_id,
                document_id=row.document_id,
                source_chunk_ids=tuple(row.source_chunk_ids_json),
                score=1.0 + min(row.mention_count, 20) / 100,
                matched_text=row.canonical_name,
                evidence_type="ENTITY",
            )
            for row in entity_rows
        )
        # 同一原始块可能同时被实体和关系命中；保留分数最高的一项即可，避免一条证据
        # 因规则重复而在后续 RRF 中获得不合理的多次加分。
        deduplicated: dict[tuple[UUID, tuple[str, ...]], GraphEvidence] = {}
        for item in evidence:
            key = (item.document_id, item.source_chunk_ids)
            previous = deduplicated.get(key)
            if previous is None or item.score > previous.score:
                deduplicated[key] = item
        return tuple(
            sorted(deduplicated.values(), key=lambda item: item.score, reverse=True)[:limit]
        )


def _job_to_domain(row: IngestionJobRow) -> IngestionJob:
    """把任务 ORM 行及 JSON payload/result 转换为领域任务。"""
    return IngestionJob(
        id=row.id,
        security_scope_id=row.security_scope_id,
        job_type=JobType(row.job_type),
        status=JobStatus(row.status),
        knowledge_base_id=row.knowledge_base_id,
        document_id=row.document_id,
        progress=float(row.progress),
        attempt=row.attempt,
        max_attempts=row.max_attempts,
        payload=row.payload_json or {},
        result=row.result_json or {},
        error_code=row.error_code,
        error_message=row.error_message,
        next_retry_at=row.next_retry_at,
        locked_by=row.locked_by,
        locked_until=row.locked_until,
        started_at=row.started_at,
        finished_at=row.finished_at,
        created_at=row.created_at,
    )


class SqlAlchemyJobRepository:
    """任务 CRUD、活动任务查找、原子认领、续租与暂存引用查询实现。"""
    def __init__(
        self, session: AsyncSession, *, worker_id: str | None = None,
        owned_job: tuple[str, UUID] | None = None,
    ) -> None:
        """绑定事务；Worker 额外绑定本次认领身份与任务，API 不启用持有者校验。"""
        self._session = session
        self._worker_id = worker_id
        self._owned_job = owned_job
        self._ownership_locked = False

    async def add(self, security_scope_id: str, value: IngestionJob) -> None:
        """验证范围并插入可调度任务。"""
        _require_matching_scope(security_scope_id, value.security_scope_id)
        self._session.add(
            IngestionJobRow(
                id=value.id,
                security_scope_id=security_scope_id,
                job_type=value.job_type.value,
                status=value.status.value,
                knowledge_base_id=value.knowledge_base_id,
                document_id=value.document_id,
                progress=value.progress,
                attempt=value.attempt,
                max_attempts=value.max_attempts,
                payload_json=value.payload,
                result_json=value.result,
                error_code=value.error_code,
                error_message=value.error_message,
                next_retry_at=value.next_retry_at,
                locked_by=value.locked_by,
                locked_until=value.locked_until,
                started_at=value.started_at,
                finished_at=value.finished_at,
                created_at=value.created_at,
                updated_at=value.created_at,
            )
        )
        await self._session.flush()
        # 事务提交后由 API/Worker 发送唤醒；回滚时必须丢弃此计数。
        self._session.info["pending_job_wakeups"] = (
            self._session.info.get("pending_job_wakeups", 0) + 1
        )

    async def get(self, security_scope_id: str, job_id: UUID) -> IngestionJob | None:
        """按安全范围读取单个任务。"""
        row = await self._session.scalar(
            select(IngestionJobRow).where(
                IngestionJobRow.security_scope_id == security_scope_id,
                IngestionJobRow.id == job_id,
            )
        )
        return _job_to_domain(row) if row is not None else None

    async def update(self, security_scope_id: str, value: IngestionJob) -> bool:
        """保存任务状态并保护心跳租约；旧 Worker 被拒绝，待处理任务登记提交后通知。"""
        _require_matching_scope(security_scope_id, value.security_scope_id)
        # API 取消只更新取消意图，不写回旧进度、旧所有者或旧租约。
        if not self._worker_id and value.status in {
            JobStatus.CANCELLED, JobStatus.CANCEL_REQUESTED,
        }:
            pending = value.status == JobStatus.CANCELLED
            fields: dict[str, Any] = {"status": value.status.value}
            if pending:
                fields.update(finished_at=value.finished_at, locked_by=None, locked_until=None)
            if value.payload.get("delete_after_cancel"):
                fields["payload_json"] = func.coalesce(
                    IngestionJobRow.payload_json,
                    literal({}, type_=IngestionJobRow.payload_json.type)
                ).op("||")(literal(
                    {"delete_after_cancel": True}, type_=IngestionJobRow.payload_json.type
                ))
            result = await self._session.execute(
                update(IngestionJobRow).where(
                    IngestionJobRow.security_scope_id == security_scope_id,
                    IngestionJobRow.id == value.id,
                    IngestionJobRow.status.in_(
                        ["PENDING", "RETRY_WAIT"] if pending else ["RUNNING", "CANCEL_REQUESTED"]
                    ),
                ).values(**fields)
            )
            return getattr(result, "rowcount", 0) == 1
        await self.guard_ownership()
        if self._worker_id and value.status == JobStatus.SUCCEEDED:
            current_status = await self._session.scalar(select(IngestionJobRow.status).where(
                IngestionJobRow.id == value.id,
                IngestionJobRow.security_scope_id == security_scope_id,
            ))
            if current_status == "CANCEL_REQUESTED":
                raise JobCancellationRequested("job cancellation requested")
        result = await self._session.execute(
            update(IngestionJobRow)
            .where(
                IngestionJobRow.security_scope_id == security_scope_id,
                IngestionJobRow.id == value.id,
            )
            .values(
                # 进度写回不能撤销同时到达的取消请求。
                status=(case(
                    (IngestionJobRow.status == "CANCEL_REQUESTED", "CANCEL_REQUESTED"),
                    else_=value.status.value,
                ) if self._worker_id and value.status == JobStatus.RUNNING else value.status.value),
                progress=value.progress,
                attempt=value.attempt,
                max_attempts=value.max_attempts,
                payload_json=(case(
                    (IngestionJobRow.payload_json["delete_after_cancel"].as_boolean().is_(True),
                     literal({**value.payload, "delete_after_cancel": True},
                             type_=IngestionJobRow.payload_json.type)),
                    else_=literal(value.payload, type_=IngestionJobRow.payload_json.type),
                ) if self._worker_id else value.payload),
                result_json=value.result,
                error_code=value.error_code,
                error_message=value.error_message,
                next_retry_at=value.next_retry_at,
                locked_by=value.locked_by,
                # 运行中保留心跳的最新租约；结束/重试状态才清空。
                locked_until=(
                    IngestionJobRow.locked_until
                    if self._worker_id and value.locked_by == self._worker_id
                    else value.locked_until
                ),
                started_at=value.started_at,
                finished_at=value.finished_at,
            )
        )
        changed = getattr(result, "rowcount", 0) == 1
        if changed and value.status == JobStatus.PENDING:
            # 手动重试可能复用任务记录，成功提交后也需要及时唤醒。
            self._session.info["pending_job_wakeups"] = (
                self._session.info.get("pending_job_wakeups", 0) + 1
            )
        return changed

    async def guard_ownership(self) -> None:
        """提交前锁定当前任务并校验有效持有者；失去租约时禁止提交文档和任务状态。

        行锁保持到本次提交/回滚，因此同一事务内结束任务后不再重复校验空所有者。
        普通 API Repository 不绑定 Worker，不走此检查。
        """
        if not self._worker_id or not self._owned_job or self._ownership_locked:
            return
        scope, job_id = self._owned_job
        row = await self._session.scalar(
            select(IngestionJobRow.id).where(
                IngestionJobRow.id == job_id,
                IngestionJobRow.security_scope_id == scope,
                IngestionJobRow.locked_by == self._worker_id,
                IngestionJobRow.locked_until > func.clock_timestamp(),
                IngestionJobRow.status.in_(["RUNNING", "CANCEL_REQUESTED"]),
            ).with_for_update()
        )
        if row is None:
            raise JobLeaseLostError("job lease no longer owned")
        self._ownership_locked = True

    def reset_ownership_guard(self) -> None:
        """每次事务结束后清除行锁标记，下个检查点必须重新验证所有权。"""
        self._ownership_locked = False

    async def find_active_for_document(
        self, security_scope_id: str, document_id: UUID
    ) -> IngestionJob | None:
        """读取文档最新一个未结束任务，用于阻止冲突操作。"""
        row = await self._session.scalar(
            select(IngestionJobRow)
            .where(
                IngestionJobRow.security_scope_id == security_scope_id,
                IngestionJobRow.document_id == document_id,
                IngestionJobRow.status.in_(
                    (
                        JobStatus.PENDING.value,
                        JobStatus.RUNNING.value,
                        JobStatus.RETRY_WAIT.value,
                        JobStatus.CANCEL_REQUESTED.value,
                    )
                ),
            )
            .order_by(IngestionJobRow.created_at.desc(), IngestionJobRow.id)
            .limit(1)
        )
        return _job_to_domain(row) if row is not None else None

    async def latest_for_document(
        self, security_scope_id: str, document_id: UUID
    ) -> IngestionJob | None:
        """读取文档最近任务，无论是否终态，用于判断失败阶段。"""
        row = await self._session.scalar(
            select(IngestionJobRow)
            .where(
                IngestionJobRow.security_scope_id == security_scope_id,
                IngestionJobRow.document_id == document_id,
            )
            .order_by(IngestionJobRow.created_at.desc(), IngestionJobRow.id)
            .limit(1)
        )
        return _job_to_domain(row) if row is not None else None

    async def claim_next(
        self,
        *,
        worker_id: str,
        now: datetime,
        locked_until: datetime,
    ) -> IngestionJob | None:
        """原子选择一个到期可运行任务，跳过其他 Worker 已锁行并写入新租约。

        ``SKIP LOCKED`` 让并发 Worker 不互相等待；SQL 同时检查 PENDING、到期 RETRY_WAIT
        及租约过期 RUNNING，后者实现崩溃恢复。
        """
        retryable_status = or_(
            IngestionJobRow.status == JobStatus.PENDING.value,
            and_(
                IngestionJobRow.status == JobStatus.RETRY_WAIT.value,
                or_(
                    IngestionJobRow.next_retry_at.is_(None),
                    IngestionJobRow.next_retry_at <= now,
                ),
            ),
        )
        expired_running = and_(
            IngestionJobRow.status == JobStatus.RUNNING.value,
            IngestionJobRow.locked_until < now,
        )
        row = await self._session.scalar(
            select(IngestionJobRow)
            .where(
                or_(
                    and_(
                        IngestionJobRow.attempt < IngestionJobRow.max_attempts,
                        retryable_status,
                    ),
                    expired_running,
                ),
            )
            .order_by(IngestionJobRow.created_at, IngestionJobRow.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if row is None:
            return None
        recovering_expired_run = row.status == JobStatus.RUNNING.value
        row.status = JobStatus.RUNNING.value
        if not recovering_expired_run:
            row.attempt += 1
        row.locked_by = worker_id
        row.locked_until = locked_until
        row.next_retry_at = None
        row.started_at = row.started_at or now
        row.finished_at = None
        await self._session.flush()
        return _job_to_domain(row)

    async def renew_lease(
        self,
        security_scope_id: str,
        job_id: UUID,
        *,
        worker_id: str,
        locked_until: datetime,
    ) -> bool:
        """仅当前有效持有者可续租运行/取消中的任务；过期后由正常认领恢复。"""
        result = await self._session.execute(
            update(IngestionJobRow)
            .where(
                IngestionJobRow.security_scope_id == security_scope_id,
                IngestionJobRow.id == job_id,
                IngestionJobRow.status.in_(["RUNNING", "CANCEL_REQUESTED"]),
                IngestionJobRow.locked_by == worker_id,
                # 已过期的执行不能靠迟到心跳复活，交由正常认领恢复。
                IngestionJobRow.locked_until > func.clock_timestamp(),
            )
            .values(locked_until=locked_until)
        )
        return getattr(result, "rowcount", 0) == 1

    async def active_staged_file_ids(self) -> frozenset[UUID]:
        """从活动 UPLOAD Job 的 payload 收集暂存 UUID，供清理任务排除。"""
        payloads = (
            await self._session.scalars(
                select(IngestionJobRow.payload_json).where(
                    IngestionJobRow.status.in_(
                        (
                            JobStatus.PENDING.value,
                            JobStatus.RUNNING.value,
                            JobStatus.RETRY_WAIT.value,
                            JobStatus.CANCEL_REQUESTED.value,
                        )
                    ),
                    IngestionJobRow.job_type == JobType.UPLOAD.value,
                )
            )
        ).all()
        identifiers: set[UUID] = set()
        for payload in payloads:
            if not payload or "staged_file_id" not in payload:
                continue
            try:
                identifiers.add(UUID(str(payload["staged_file_id"])))
            except ValueError:
                continue
        return frozenset(identifiers)


def _idempotency_to_domain(row: IdempotencyRecordRow) -> IdempotencyRecord:
    """把幂等 ORM 行转换为领域记录。"""
    return IdempotencyRecord(
        security_scope_id=row.security_scope_id,
        idempotency_key=row.idempotency_key,
        http_method=row.http_method,
        request_path=row.request_path,
        request_sha256=row.request_sha256,
        processing_status=IdempotencyStatus(row.processing_status),
        response_status=row.response_status,
        response_json=row.response_json,
        resource_id=row.resource_id,
        created_at=row.created_at,
        expires_at=row.expires_at,
    )


class SqlAlchemyIdempotencyRepository:
    """PostgreSQL 支持的原子、按租户隔离的幂等键抢占实现。"""

    def __init__(self, session: AsyncSession) -> None:
        """保存当前写请求事务 Session。"""
        self._session = session

    async def reserve(self, value: IdempotencyRecord) -> IdempotencyReservation:
        """使用 UPSERT 原子抢占新键/过期键；未获得时锁定并返回现有记录。

        ``ON CONFLICT ... WHERE expires_at <= now`` 只替换过期记录，不会覆盖仍处理或已
        完成的响应；随后 ``FOR UPDATE`` 保证当前事务读取稳定快照。
        """
        if value.expires_at is None:
            raise ValueError("idempotency expiry is required")
        statement = (
            insert(IdempotencyRecordRow)
            .values(
                security_scope_id=value.security_scope_id,
                idempotency_key=value.idempotency_key,
                http_method=value.http_method,
                request_path=value.request_path,
                request_sha256=value.request_sha256,
                processing_status=IdempotencyStatus.PROCESSING.value,
                created_at=value.created_at,
                expires_at=value.expires_at,
            )
            .on_conflict_do_update(
                index_elements=[
                    IdempotencyRecordRow.security_scope_id,
                    IdempotencyRecordRow.idempotency_key,
                    IdempotencyRecordRow.http_method,
                    IdempotencyRecordRow.request_path,
                ],
                set_={
                    "request_sha256": value.request_sha256,
                    "processing_status": IdempotencyStatus.PROCESSING.value,
                    "response_status": None,
                    "response_json": None,
                    "resource_id": None,
                    "created_at": value.created_at,
                    "expires_at": value.expires_at,
                },
                where=IdempotencyRecordRow.expires_at <= value.created_at,
            )
            .returning(IdempotencyRecordRow)
        )
        inserted = (await self._session.execute(statement)).scalar_one_or_none()
        if inserted is not None:
            return IdempotencyReservation(
                record=_idempotency_to_domain(inserted),
                acquired=True,
            )
        existing = await self._session.scalar(
            select(IdempotencyRecordRow)
            .where(
                IdempotencyRecordRow.security_scope_id == value.security_scope_id,
                IdempotencyRecordRow.idempotency_key == value.idempotency_key,
                IdempotencyRecordRow.http_method == value.http_method,
                IdempotencyRecordRow.request_path == value.request_path,
            )
            .with_for_update()
        )
        if existing is None:
            raise RuntimeError("idempotency record disappeared during reservation")
        return IdempotencyReservation(
            record=_idempotency_to_domain(existing),
            acquired=False,
        )

    async def complete(
        self,
        value: IdempotencyRecord,
        *,
        response_status: int,
        response_json: dict[str, Any],
        resource_id: UUID | None,
    ) -> None:
        """按完整请求身份标记完成，并保存响应和可选资源 ID。"""
        await self._session.execute(
            update(IdempotencyRecordRow)
            .where(
                IdempotencyRecordRow.security_scope_id == value.security_scope_id,
                IdempotencyRecordRow.idempotency_key == value.idempotency_key,
                IdempotencyRecordRow.http_method == value.http_method,
                IdempotencyRecordRow.request_path == value.request_path,
                IdempotencyRecordRow.request_sha256 == value.request_sha256,
            )
            .values(
                processing_status=IdempotencyStatus.COMPLETED.value,
                response_status=response_status,
                response_json=response_json,
                resource_id=resource_id,
            )
        )

    async def fail(self, value: IdempotencyRecord) -> None:
        """按完整请求身份将抢占记录标记为失败。"""
        await self._session.execute(
            update(IdempotencyRecordRow)
            .where(
                IdempotencyRecordRow.security_scope_id == value.security_scope_id,
                IdempotencyRecordRow.idempotency_key == value.idempotency_key,
                IdempotencyRecordRow.http_method == value.http_method,
                IdempotencyRecordRow.request_path == value.request_path,
                IdempotencyRecordRow.request_sha256 == value.request_sha256,
            )
            .values(processing_status=IdempotencyStatus.FAILED.value)
        )


def _query_to_domain(row: QueryRecordRow) -> QueryRecord:
    """从 JSON 数组与标量字段重建查询审计领域对象。"""
    return QueryRecord(
        request_id=row.request_id,
        security_scope_id=row.security_scope_id,
        request_status=QueryStatus(row.request_status),
        outcome=AnswerOutcome(row.outcome) if row.outcome is not None else None,
        query_sha256=row.query_sha256,
        query_redacted=row.query_redacted,
        effective_query_redacted=row.effective_query_redacted,
        knowledge_base_ids=tuple(UUID(item) for item in row.knowledge_base_ids_json),
        retrieval_profile_ref=row.retrieval_profile_ref,
        prompt_profile_ref=row.prompt_profile_ref,
        model_profile_ref=row.model_profile_ref,
        cache_status=row.cache_status,
        candidate_count=row.candidate_count,
        context_count=row.context_count,
        answer_text=row.answer_text,
        timings=row.timings_json or {},
        warnings=tuple(row.warnings_json or []),
        prompt_tokens=row.prompt_tokens,
        completion_tokens=row.completion_tokens,
        total_tokens=row.total_tokens,
        error_code=row.error_code,
        created_at=row.created_at,
        completed_at=row.completed_at,
        expires_at=row.expires_at,
    )


def _citation_to_domain(row: CitationRecordRow) -> Citation:
    """从引用行安全重建页码、坐标、摘录和检索分数。"""
    raw_positions = row.positions_json or []
    positions = tuple(
        SourcePosition(
            page=(
                int(item["page"])
                if isinstance(item, dict) and item.get("page") is not None
                else None
            ),
            bbox=(
                (
                    float(item["bbox"][0]),
                    float(item["bbox"][1]),
                    float(item["bbox"][2]),
                    float(item["bbox"][3]),
                )
                if isinstance(item, dict)
                and isinstance(item.get("bbox"), list)
                and len(item["bbox"]) == 4
                else None
            ),
        )
        for item in raw_positions
    )
    return Citation(
        citation_id=row.id,
        label=row.label,
        knowledge_base_id=row.knowledge_base_id,
        document_id=row.document_id,
        document_name=row.document_name,
        engine_chunk_id=row.engine_chunk_id,
        quote=row.quote_text,
        resource_scope_id=row.resource_scope_id,
        page_start=row.page_start,
        page_end=row.page_end,
        positions=positions,
        score=float(row.retrieval_score) if row.retrieval_score is not None else None,
        rerank_score=float(row.rerank_score) if row.rerank_score is not None else None,
    )


class SqlAlchemyQueryRepository:
    """查询审计、引用和反馈的范围限定持久化实现。"""
    def __init__(self, session: AsyncSession) -> None:
        """保存当前短事务 Session。"""
        self._session = session

    async def add(self, security_scope_id: str, value: QueryRecord) -> None:
        """新增脱敏查询审计及其初始状态。"""
        _require_matching_scope(security_scope_id, value.security_scope_id)
        self._session.add(
            QueryRecordRow(
                request_id=value.request_id,
                security_scope_id=security_scope_id,
                request_status=value.request_status.value,
                outcome=value.outcome.value if value.outcome is not None else None,
                query_sha256=value.query_sha256,
                query_redacted=value.query_redacted,
                effective_query_redacted=value.effective_query_redacted,
                knowledge_base_ids_json=[
                    str(identifier) for identifier in value.knowledge_base_ids
                ],
                retrieval_profile_ref=value.retrieval_profile_ref,
                prompt_profile_ref=value.prompt_profile_ref,
                model_profile_ref=value.model_profile_ref,
                cache_status=value.cache_status,
                candidate_count=value.candidate_count,
                context_count=value.context_count,
                answer_text=value.answer_text,
                timings_json=value.timings,
                warnings_json=list(value.warnings),
                prompt_tokens=value.prompt_tokens,
                completion_tokens=value.completion_tokens,
                total_tokens=value.total_tokens,
                error_code=value.error_code,
                created_at=value.created_at,
                completed_at=value.completed_at,
                expires_at=value.expires_at,
            )
        )
        await self._session.flush()

    async def get(
        self,
        security_scope_id: str,
        request_id: UUID,
    ) -> QueryRecord | None:
        """按范围和请求 ID 读取审计记录。"""
        row = await self._session.scalar(
            select(QueryRecordRow).where(
                QueryRecordRow.security_scope_id == security_scope_id,
                QueryRecordRow.request_id == request_id,
            )
        )
        return _query_to_domain(row) if row is not None else None

    async def update(self, security_scope_id: str, value: QueryRecord) -> bool:
        """更新执行状态、答案、耗时、警告、Token 和完成时间。"""
        _require_matching_scope(security_scope_id, value.security_scope_id)
        result = await self._session.execute(
            update(QueryRecordRow)
            .where(
                QueryRecordRow.security_scope_id == security_scope_id,
                QueryRecordRow.request_id == value.request_id,
            )
            .values(
                request_status=value.request_status.value,
                outcome=value.outcome.value if value.outcome is not None else None,
                effective_query_redacted=value.effective_query_redacted,
                cache_status=value.cache_status,
                candidate_count=value.candidate_count,
                context_count=value.context_count,
                answer_text=value.answer_text,
                timings_json=value.timings,
                warnings_json=list(value.warnings),
                prompt_tokens=value.prompt_tokens,
                completion_tokens=value.completion_tokens,
                total_tokens=value.total_tokens,
                error_code=value.error_code,
                completed_at=value.completed_at,
            )
        )
        return getattr(result, "rowcount", 0) == 1

    async def add_citations(
        self,
        security_scope_id: str,
        request_id: UUID,
        citations: Sequence[Citation],
    ) -> None:
        """按输入顺序批量保存实际引用及其原文位置。"""
        self._session.add_all(
            [
                CitationRecordRow(
                    id=value.citation_id,
                    security_scope_id=security_scope_id,
                    resource_scope_id=value.resource_scope_id,
                    request_id=request_id,
                    ordinal=index,
                    label=value.label,
                    knowledge_base_id=value.knowledge_base_id,
                    document_id=value.document_id,
                    engine_chunk_id=value.engine_chunk_id,
                    document_name=value.document_name,
                    heading_path_json=[],
                    page_start=value.page_start,
                    page_end=value.page_end,
                    positions_json=[
                        {
                            "page": item.page,
                            "bbox": list(item.bbox) if item.bbox is not None else None,
                        }
                        for item in value.positions
                    ],
                    quote_text=value.quote,
                    retrieval_score=value.score,
                    rerank_score=value.rerank_score,
                    verification_status="VALID",
                )
                for index, value in enumerate(citations)
            ]
        )
        await self._session.flush()

    async def list_citations(
        self,
        security_scope_id: str,
        request_id: UUID,
    ) -> tuple[Citation, ...]:
        """按 ordinal 稳定顺序读取指定请求引用。"""
        rows = (
            await self._session.scalars(
                select(CitationRecordRow)
                .where(
                    CitationRecordRow.security_scope_id == security_scope_id,
                    CitationRecordRow.request_id == request_id,
                )
                .order_by(CitationRecordRow.ordinal)
            )
        ).all()
        return tuple(_citation_to_domain(row) for row in rows)

    async def get_citation(
        self,
        security_scope_id: str,
        citation_id: UUID,
    ) -> Citation | None:
        """按范围和引用 ID 读取单条引用。"""
        row = await self._session.scalar(
            select(CitationRecordRow).where(
                CitationRecordRow.security_scope_id == security_scope_id,
                CitationRecordRow.id == citation_id,
            )
        )
        return _citation_to_domain(row) if row is not None else None

    async def add_feedback(
        self,
        security_scope_id: str,
        value: FeedbackRecord,
    ) -> None:
        """验证范围并保存关联请求的结构化反馈。"""
        _require_matching_scope(security_scope_id, value.security_scope_id)
        self._session.add(
            FeedbackRecordRow(
                id=value.id,
                security_scope_id=security_scope_id,
                request_id=value.request_id,
                rating=value.rating,
                feedback_type=value.feedback_type,
                comment_text=value.comment,
                corrected_answer=value.corrected_answer,
                created_at=value.created_at,
            )
        )
        await self._session.flush()
