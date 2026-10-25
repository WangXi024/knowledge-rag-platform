"""真实 PostgreSQL 集成测试：迁移、租户隔离、唯一键、任务恢复、幂等和审计外键。"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse
from uuid import uuid4

import psycopg
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from rag_service.bootstrap.database_schema import migration_sql
from rag_service.bootstrap.profiles import (
    SqlAlchemyProfileStore,
    import_profiles,
    load_profile_seeds,
)
from rag_service.domain.models import (
    AnswerOutcome,
    Citation,
    DocumentIndexNode,
    DocumentRecord,
    DocumentStatus,
    FeedbackRecord,
    IdempotencyRecord,
    IndexNodeType,
    IngestionJob,
    JobStatus,
    JobType,
    KnowledgeBase,
    KnowledgeBaseStatus,
    QueryRecord,
    QueryStatus,
    SourcePosition,
)
from rag_service.infrastructure.postgres.repositories import (
    SqlAlchemyDocumentRepository,
    SqlAlchemyIdempotencyRepository,
    SqlAlchemyIndexNodeRepository,
    SqlAlchemyJobRepository,
    SqlAlchemyKnowledgeBaseRepository,
    SqlAlchemyQueryRepository,
)
from rag_service.settings import ServiceSettings


@pytest.fixture(scope="session")
def migrated_database_dsn() -> str:
    dsn = os.getenv("TEST_DATABASE_DSN")
    if not dsn:
        pytest.skip("TEST_DATABASE_DSN is not configured")
    database_name = urlparse(dsn).path.lstrip("/")
    if database_name != "rag_app_test":
        pytest.fail("integration tests require a dedicated rag_app_test database")

    # 只允许显式命名的隔离测试库；生产 SQL 仅替换目标库保护中的固定测试库名，
    # 结构和升级语句逐字执行。canonical rag_app 的真实脚本另由 SQL 部署测试验证。
    with psycopg.connect(
        dsn.replace("postgresql+psycopg://", "postgresql://"), autocommit=True
    ) as connection:
        connection.execute("DROP SCHEMA IF EXISTS rag_app CASCADE")
        for filename in (
            "V1.0.0__init_schema.sql",
            "V1.0.0__to__V1.1.0__shared_knowledge_bases.sql",
        ):
            statement = migration_sql("rag_app", filename).replace(
                "current_database() <> 'rag_app'", "current_database() <> 'rag_app_test'"
            )
            connection.execute(statement, prepare=False)
    return dsn


@pytest_asyncio.fixture
async def database_session(
    migrated_database_dsn: str,
) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(migrated_database_dsn)
    async with engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(bind=connection, expire_on_commit=False)
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()
    await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_migration_creates_exactly_twelve_application_tables(
    database_session: AsyncSession,
) -> None:
    count = await database_session.scalar(
        text(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'rag_app' AND table_name <> 'alembic_version'"
        )
    )
    # 层级索引一张、轻量实体图两张、安全文档版本一张；精确数量断言可以防止只改
    # ORM 却漏掉 Alembic migration，或迁移意外生成未评审的业务表。
    assert count == 12


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_migration_and_repository_accept_all_multigranular_node_types(
    database_session: AsyncSession,
) -> None:
    """真实迁移后的约束必须允许 Parent、Section 和 Summary 一起原子写入。

    该回归测试专门防止领域枚举已经增加 SECTION、数据库 CHECK 却仍停留在旧版本的
    漂移。测试通过公开 Repository 写入并回读三类节点，不直接绕过应用层修改表；
    事务由 fixture 回滚，不会在测试数据库留下文档或侧车索引。
    """
    scope = "7" * 64
    knowledge_bases = SqlAlchemyKnowledgeBaseRepository(database_session)
    documents = SqlAlchemyDocumentRepository(database_session)
    index_nodes = SqlAlchemyIndexNodeRepository(database_session)
    knowledge_base = KnowledgeBase(
        security_scope_id=scope,
        name="section-constraint-fixture",
        parser_profile_ref="office-default@1",
        chunk_profile_ref="general-256@2",
        embedding_profile_ref="embedding-openai-compatible@3",
        retrieval_profile_ref="hybrid-multigranular@4",
    )
    await knowledge_bases.add(scope, knowledge_base)
    document = DocumentRecord(
        security_scope_id=scope,
        knowledge_base_id=knowledge_base.id,
        filename="section-fixture.md",
        safe_filename="section-fixture.md",
        mime_type="text/markdown",
        file_size_bytes=32,
        content_sha256="7" * 64,
        parser_profile_ref="office-default@1",
        chunk_profile_ref="general-256@2",
        status=DocumentStatus.READY,
    )
    await documents.add(scope, document)
    nodes = tuple(
        DocumentIndexNode(
            security_scope_id=scope,
            knowledge_base_id=knowledge_base.id,
            document_id=document.id,
            node_type=node_type,
            ordinal=ordinal,
            content=f"{node_type.value} synthetic content",
            source_chunk_ids=("engine-chunk-1",),
            embedding=(1.0, 0.0),
            index_version=2,
        )
        for ordinal, node_type in enumerate(
            (IndexNodeType.PARENT, IndexNodeType.SECTION, IndexNodeType.SUMMARY)
        )
    )

    await index_nodes.replace_document_nodes(scope, document.id, nodes)
    loaded = await index_nodes.list_for_knowledge_bases(
        scope,
        (knowledge_base.id,),
        node_types=(IndexNodeType.PARENT, IndexNodeType.SECTION, IndexNodeType.SUMMARY),
        limit=10,
    )

    assert {node.node_type for node in loaded} == {
        IndexNodeType.PARENT,
        IndexNodeType.SECTION,
        IndexNodeType.SUMMARY,
    }


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_knowledge_base_repository_never_reads_another_scope(
    database_session: AsyncSession,
) -> None:
    repository = SqlAlchemyKnowledgeBaseRepository(database_session)
    scope_a = "a" * 64
    scope_b = "b" * 64
    kb_a = KnowledgeBase(
        security_scope_id=scope_a,
        name="scope-a",
        parser_profile_ref="deepdoc-default@1",
        chunk_profile_ref="general-512@1",
        embedding_profile_ref="embedding-openai-compatible@3",
        retrieval_profile_ref="hybrid-default@1",
    )
    kb_b = KnowledgeBase(
        security_scope_id=scope_b,
        name="scope-b",
        parser_profile_ref="deepdoc-default@1",
        chunk_profile_ref="general-512@1",
        embedding_profile_ref="embedding-openai-compatible@3",
        retrieval_profile_ref="hybrid-default@1",
    )
    await repository.add(scope_a, kb_a)
    await repository.add(scope_b, kb_b)

    assert await repository.get(scope_a, kb_a.id) == kb_a
    assert await repository.get(scope_a, kb_b.id) is None
    items, total = await repository.list(scope_a, page=1, page_size=20)
    assert total == 1
    assert [item.id for item in items] == [kb_a.id]

    documents = SqlAlchemyDocumentRepository(database_session)
    for index in range(2):
        await documents.add(
            scope_a,
            DocumentRecord(
                security_scope_id=scope_a,
                knowledge_base_id=kb_a.id,
                filename=f"scope-a-{index}.txt",
                safe_filename=f"scope-a-{index}.txt",
                mime_type="text/plain",
                file_size_bytes=7,
                content_sha256=f"{index + 1:064x}",
                parser_profile_ref="office-default@1",
                chunk_profile_ref="general-512@1",
            ),
        )
    await documents.add(
        scope_a,
        DocumentRecord(
            security_scope_id=scope_a,
            knowledge_base_id=kb_a.id,
            filename="scope-a-deleted.txt",
            safe_filename="scope-a-deleted.txt",
            mime_type="text/plain",
            file_size_bytes=7,
            content_sha256=f"{3:064x}",
            parser_profile_ref="office-default@1",
            chunk_profile_ref="general-512@1",
            status=DocumentStatus.DELETED,
            deleted_at=datetime.now(UTC),
        ),
    )
    await documents.add(
        scope_b,
        DocumentRecord(
            security_scope_id=scope_b,
            knowledge_base_id=kb_b.id,
            filename="scope-b.txt",
            safe_filename="scope-b.txt",
            mime_type="text/plain",
            file_size_bytes=7,
            content_sha256=f"{4:064x}",
            parser_profile_ref="office-default@1",
            chunk_profile_ref="general-512@1",
        ),
    )

    counted = await repository.get(scope_a, kb_a.id)
    assert counted is not None
    assert counted.document_count == 2
    counted_items, _ = await repository.list(scope_a, page=1, page_size=20)
    assert counted_items[0].document_count == 2
    many = await repository.get_many(scope_a, [kb_a.id, kb_b.id])
    assert [(item.id, item.document_count) for item in many] == [(kb_a.id, 2)]


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_deleted_document_external_id_can_be_reused(
    database_session: AsyncSession,
) -> None:
    scope = "9" * 64
    knowledge_bases = SqlAlchemyKnowledgeBaseRepository(database_session)
    documents = SqlAlchemyDocumentRepository(database_session)
    knowledge_base = KnowledgeBase(
        security_scope_id=scope,
        name="external-id-reuse-fixture",
        parser_profile_ref="office-default@1",
        chunk_profile_ref="general-512@1",
        embedding_profile_ref="embedding-openai-compatible@3",
        retrieval_profile_ref="hybrid-default@1",
    )
    await knowledge_bases.add(scope, knowledge_base)
    deleted = DocumentRecord(
        security_scope_id=scope,
        knowledge_base_id=knowledge_base.id,
        external_id="reusable-external-id",
        filename="deleted.txt",
        safe_filename="deleted.txt",
        mime_type="text/plain",
        file_size_bytes=7,
        content_sha256="a" * 64,
        parser_profile_ref="office-default@1",
        chunk_profile_ref="general-512@1",
        status=DocumentStatus.DELETED,
        deleted_at=datetime.now(UTC),
    )
    replacement = DocumentRecord(
        security_scope_id=scope,
        knowledge_base_id=knowledge_base.id,
        external_id="reusable-external-id",
        filename="replacement.txt",
        safe_filename="replacement.txt",
        mime_type="text/plain",
        file_size_bytes=11,
        content_sha256="b" * 64,
        parser_profile_ref="office-default@1",
        chunk_profile_ref="general-512@1",
    )

    await documents.add(scope, deleted)
    await documents.add(scope, replacement)

    items, total = await documents.list(
        scope,
        knowledge_base.id,
        page=1,
        page_size=20,
        external_id="reusable-external-id",
    )
    assert total == 1
    assert [item.id for item in items] == [replacement.id]


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_deleted_knowledge_base_lookup_is_scope_and_status_limited(
    database_session: AsyncSession,
) -> None:
    """只有同一资源范围的软删除记录可供删除任务核验，普通读取仍隐藏该库。"""
    repository = SqlAlchemyKnowledgeBaseRepository(database_session)
    scope = "a" * 64
    value = KnowledgeBase(
        security_scope_id=scope,
        name="deleted-job-fixture",
        parser_profile_ref="office-default@1",
        chunk_profile_ref="general-512@1",
        embedding_profile_ref="embedding-openai-compatible@3",
        retrieval_profile_ref="hybrid-default@1",
        status=KnowledgeBaseStatus.READY,
    )
    await repository.add(scope, value)
    assert await repository.get_deleted(scope, value.id) is None

    value.status = KnowledgeBaseStatus.DELETED
    assert await repository.update(scope, value, value.row_version)
    assert await repository.get_accessible((scope,), value.id) is None
    assert await repository.get_deleted("b" * 64, value.id) is None
    deleted = await repository.get_deleted(scope, value.id)
    assert deleted is not None and deleted.id == value.id
    assert deleted.status == KnowledgeBaseStatus.DELETED


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_repository_rejects_entity_scope_mismatch(
    database_session: AsyncSession,
) -> None:
    repository = SqlAlchemyKnowledgeBaseRepository(database_session)
    value = KnowledgeBase(
        security_scope_id="a" * 64,
        name="scope-a",
        parser_profile_ref="deepdoc-default@1",
        chunk_profile_ref="general-512@1",
        embedding_profile_ref="embedding-openai-compatible@3",
        retrieval_profile_ref="hybrid-default@1",
    )

    with pytest.raises(ValueError):
        await repository.add("b" * 64, value)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_profile_import_is_idempotent_in_postgresql(
    database_session: AsyncSession,
    service_settings: ServiceSettings,
) -> None:
    store = SqlAlchemyProfileStore(database_session)
    seeds = load_profile_seeds(service_settings.profiles)

    first = await import_profiles(
        store,
        seeds,
        service_settings.profiles.required_refs,
    )
    second = await import_profiles(
        store,
        seeds,
        service_settings.profiles.required_refs,
    )

    assert first.inserted == len(seeds)
    assert second.skipped == len(seeds)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_expired_running_job_is_recovered_without_consuming_an_attempt(
    database_session: AsyncSession,
) -> None:
    scope = "c" * 64
    knowledge_bases = SqlAlchemyKnowledgeBaseRepository(database_session)
    documents = SqlAlchemyDocumentRepository(database_session)
    jobs = SqlAlchemyJobRepository(database_session)
    knowledge_base = KnowledgeBase(
        security_scope_id=scope,
        name="lease-fixture",
        parser_profile_ref="deepdoc-default@1",
        chunk_profile_ref="general-512@1",
        embedding_profile_ref="embedding-openai-compatible@3",
        retrieval_profile_ref="hybrid-default@1",
    )
    await knowledge_bases.add(scope, knowledge_base)
    document = DocumentRecord(
        security_scope_id=scope,
        knowledge_base_id=knowledge_base.id,
        filename="fixture.txt",
        safe_filename="fixture.txt",
        mime_type="text/plain",
        file_size_bytes=7,
        content_sha256="a" * 64,
        parser_profile_ref="deepdoc-default@1",
        chunk_profile_ref="general-512@1",
    )
    await documents.add(scope, document)
    job = IngestionJob(
        security_scope_id=scope,
        job_type=JobType.UPLOAD,
        knowledge_base_id=knowledge_base.id,
        document_id=document.id,
        max_attempts=2,
    )
    await jobs.add(scope, job)
    now = datetime.now(UTC)

    first = await jobs.claim_next(
        worker_id="worker-1",
        now=now,
        locked_until=now + timedelta(seconds=30),
    )
    assert first is not None
    assert first.status == JobStatus.RUNNING
    assert first.attempt == 1

    first.locked_until = now - timedelta(seconds=1)
    assert await jobs.update(scope, first)
    recovered = await jobs.claim_next(
        worker_id="worker-2",
        now=now,
        locked_until=now + timedelta(seconds=30),
    )
    assert recovered is not None
    assert recovered.id == job.id
    assert recovered.attempt == 1
    assert recovered.locked_by == "worker-2"

    recovered.locked_until = now - timedelta(seconds=1)
    assert await jobs.update(scope, recovered)
    recovered_again = await jobs.claim_next(
        worker_id="worker-3",
        now=now,
        locked_until=now + timedelta(seconds=30),
    )
    assert recovered_again is not None
    assert recovered_again.id == job.id
    assert recovered_again.attempt == 1
    assert recovered_again.locked_by == "worker-3"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_idempotency_reservation_is_scoped_and_atomic(
    database_session: AsyncSession,
) -> None:
    repository = SqlAlchemyIdempotencyRepository(database_session)
    now = datetime.now(UTC)
    value = IdempotencyRecord(
        security_scope_id="d" * 64,
        idempotency_key="fixture-key",
        http_method="POST",
        request_path="/v1/knowledge-bases",
        request_sha256="a" * 64,
        created_at=now,
        expires_at=now + timedelta(hours=1),
    )

    first = await repository.reserve(value)
    second = await repository.reserve(value)
    assert first.acquired is True
    assert second.acquired is False

    other_scope = IdempotencyRecord(
        security_scope_id="e" * 64,
        idempotency_key=value.idempotency_key,
        http_method=value.http_method,
        request_path=value.request_path,
        request_sha256="b" * 64,
        created_at=now,
        expires_at=now + timedelta(hours=1),
    )
    assert (await repository.reserve(other_scope)).acquired is True


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_query_citation_and_feedback_are_scope_qualified(
    database_session: AsyncSession,
) -> None:
    scope = "f" * 64
    knowledge_bases = SqlAlchemyKnowledgeBaseRepository(database_session)
    documents = SqlAlchemyDocumentRepository(database_session)
    queries = SqlAlchemyQueryRepository(database_session)
    knowledge_base = KnowledgeBase(
        security_scope_id=scope,
        name="query-fixture",
        parser_profile_ref="deepdoc-default@1",
        chunk_profile_ref="general-512@1",
        embedding_profile_ref="embedding-openai-compatible@3",
        retrieval_profile_ref="hybrid-default@1",
    )
    await knowledge_bases.add(scope, knowledge_base)
    document = DocumentRecord(
        security_scope_id=scope,
        knowledge_base_id=knowledge_base.id,
        filename="fixture.txt",
        safe_filename="fixture.txt",
        mime_type="text/plain",
        file_size_bytes=7,
        content_sha256="a" * 64,
        parser_profile_ref="deepdoc-default@1",
        chunk_profile_ref="general-512@1",
    )
    await documents.add(scope, document)
    request_id = uuid4()
    record = QueryRecord(
        security_scope_id=scope,
        request_id=request_id,
        query_sha256="b" * 64,
        knowledge_base_ids=(knowledge_base.id,),
        retrieval_profile_ref="hybrid-default@1",
    )
    await queries.add(scope, record)
    record.request_status = QueryStatus.COMPLETED
    record.outcome = AnswerOutcome.SUCCESS
    record.answer_text = "answer [C1]"
    assert await queries.update(scope, record)
    citation = Citation(
        citation_id=uuid4(),
        label="C1",
        knowledge_base_id=knowledge_base.id,
        document_id=document.id,
        document_name=document.filename,
        engine_chunk_id="engine-chunk",
        quote="synthetic",
        resource_scope_id=scope,
        page_start=1,
        page_end=1,
        positions=(SourcePosition(page=1),),
        score=0.9,
    )
    await queries.add_citations(scope, request_id, (citation,))
    await queries.add_feedback(
        scope,
        FeedbackRecord(
            security_scope_id=scope,
            request_id=request_id,
            feedback_type="helpful",
            rating=1,
        ),
    )

    loaded = await queries.get(scope, request_id)
    assert loaded is not None and loaded.outcome == AnswerOutcome.SUCCESS
    assert await queries.get("0" * 64, request_id) is None
    assert await queries.get_citation("0" * 64, citation.citation_id) is None
    assert (await queries.list_citations(scope, request_id))[0].label == "C1"


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_worker_progress_preserves_heartbeat_and_fences_previous_owner(
    migrated_database_dsn: str,
) -> None:
    """真实独立事务验证续租不会被旧进度覆盖，旧持有者不能写回新持有者状态。"""
    from sqlalchemy import delete

    from rag_service.domain.errors import JobLeaseLostError
    from rag_service.infrastructure.postgres.models import IngestionJobRow
    from rag_service.infrastructure.postgres.session import create_session_factory

    engine = create_async_engine(migrated_database_dsn)
    factory = create_session_factory(engine)
    scope = "e" * 64
    job = IngestionJob(security_scope_id=scope, job_type=JobType.UPLOAD, max_attempts=2)
    now = datetime.now(UTC)
    try:
        async with factory() as session:
            repo = SqlAlchemyJobRepository(session)
            await repo.add(scope, job)
            claimed = await repo.claim_next(
                worker_id="owner-a", now=now, locked_until=now + timedelta(seconds=30)
            )
            assert claimed is not None and claimed.id == job.id
            await session.commit()
        extended = now + timedelta(seconds=120)
        async with factory() as heartbeat:
            assert await SqlAlchemyJobRepository(heartbeat).renew_lease(
                scope, job.id, worker_id="owner-a", locked_until=extended
            )
            await heartbeat.commit()
        async with factory() as worker:
            repo = SqlAlchemyJobRepository(worker, worker_id="owner-a", owned_job=(scope, job.id))
            claimed.progress = 0.5
            assert await repo.update(scope, claimed)
            await worker.commit()
            repo.reset_ownership_guard()
        async with factory() as other:
            repo = SqlAlchemyJobRepository(other)
            current = await repo.get(scope, job.id)
            assert current is not None and current.locked_until == extended
            # 故意制造租约到期，再由另一持有者认领，模拟崩溃恢复。
            current.locked_until = now - timedelta(seconds=1)
            await repo.update(scope, current)
            recovered = await repo.claim_next(worker_id="owner-b", now=now, locked_until=extended)
            assert recovered is not None and recovered.id == job.id
            await other.commit()
        async with factory() as stale:
            repo = SqlAlchemyJobRepository(stale, worker_id="owner-a", owned_job=(scope, job.id))
            claimed.status = JobStatus.SUCCEEDED
            claimed.locked_by = None
            claimed.locked_until = None
            with pytest.raises(JobLeaseLostError):
                await repo.update(scope, claimed)
            await stale.rollback()
        async with factory() as verify:
            current = await SqlAlchemyJobRepository(verify).get(scope, job.id)
            assert current is not None and current.locked_by == "owner-b"
            assert current.status == JobStatus.RUNNING
    finally:
        async with factory() as cleanup:
            await cleanup.execute(delete(IngestionJobRow).where(IngestionJobRow.id == job.id))
            await cleanup.commit()
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_parallel_claims_do_not_duplicate_jobs(migrated_database_dsn: str) -> None:
    """十个并发事务只能认领十个不同任务；第十一个等待下一轮，不重复认领。"""
    import asyncio

    from sqlalchemy import delete

    from rag_service.infrastructure.postgres.models import IngestionJobRow
    from rag_service.infrastructure.postgres.session import create_session_factory

    engine = create_async_engine(migrated_database_dsn)
    factory = create_session_factory(engine)
    scope = "f" * 64
    jobs = [IngestionJob(security_scope_id=scope, job_type=JobType.UPLOAD) for _ in range(10)]
    try:
        async with factory() as session:
            repo = SqlAlchemyJobRepository(session)
            for job in jobs:
                await repo.add(scope, job)
            await session.commit()

        async def claim(index: int) -> IngestionJob | None:
            """独立事务模拟不同执行进程，持锁时让出事件循环制造认领竞争。"""
            async with factory() as session:
                now = datetime.now(UTC)
                job = await SqlAlchemyJobRepository(session).claim_next(
                    worker_id=f"parallel-{index}", now=now,
                    locked_until=now + timedelta(seconds=120),
                )
                await asyncio.sleep(0.01)
                await session.commit()
                return job

        claimed = await asyncio.gather(*(claim(i) for i in range(11)))
        ids = [job.id for job in claimed if job is not None]
        assert len(ids) == len(set(ids)) == 10
        assert set(ids) == {job.id for job in jobs}
    finally:
        async with factory() as cleanup:
            await cleanup.execute(
                delete(IngestionJobRow).where(IngestionJobRow.id.in_([j.id for j in jobs]))
            )
            await cleanup.commit()
        await engine.dispose()


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_cancel_preserves_lease_and_cannot_be_erased(database_session: AsyncSession) -> None:
    """API 取消不覆盖续租，旧 Worker 进度不撤销取消，成功写回也不能抢过取消。"""
    from rag_service.domain.errors import JobCancellationRequested

    scope = "a" * 64
    api = SqlAlchemyJobRepository(database_session)
    job = IngestionJob(security_scope_id=scope, job_type=JobType.UPLOAD)
    await api.add(scope, job)
    now = datetime.now(UTC)
    claimed = await api.claim_next(
        worker_id="cancel-owner", now=now, locked_until=now + timedelta(seconds=30)
    )
    assert claimed is not None
    stale = await api.get(scope, job.id)
    assert stale is not None
    extended = now + timedelta(seconds=120)
    await api.renew_lease(scope, job.id, worker_id="cancel-owner", locked_until=extended)
    stale.status = JobStatus.CANCEL_REQUESTED
    stale.payload["delete_after_cancel"] = True
    assert await api.update(scope, stale)
    worker = SqlAlchemyJobRepository(
        database_session, worker_id="cancel-owner", owned_job=(scope, job.id)
    )
    claimed.progress = 0.5
    assert await worker.update(scope, claimed)
    current = await api.get(scope, job.id)
    assert current is not None
    assert current.status == JobStatus.CANCEL_REQUESTED
    assert current.locked_by == "cancel-owner" and current.locked_until == extended
    assert current.payload["delete_after_cancel"] is True
    assert await api.renew_lease(scope, job.id, worker_id="cancel-owner", locked_until=extended)
    claimed.status = JobStatus.SUCCEEDED
    with pytest.raises(JobCancellationRequested):
        await worker.update(scope, claimed)
    current.status = JobStatus.CANCELLED
    current.locked_by = None
    current.locked_until = None
    assert await worker.update(scope, current)
    # 已经终结后，迟到的旧取消请求不能重新写成 CANCEL_REQUESTED。
    assert not await api.update(scope, stale)


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_concurrent_content_revisions_preserve_edits_and_scope(
    migrated_database_dsn: str,
) -> None:
    """十个独立事务同时递增不丢失更新，也不能覆盖编辑或绕过租户隔离。"""
    engine = create_async_engine(migrated_database_dsn, pool_size=10)
    scope = uuid4().hex * 2
    kb = KnowledgeBase(
        security_scope_id=scope,
        name="revision-fixture",
        parser_profile_ref="deepdoc-default@1",
        chunk_profile_ref="general-512@1",
        embedding_profile_ref="embedding-openai-compatible@3",
        retrieval_profile_ref="hybrid-default@1",
    )
    try:
        async with AsyncSession(engine) as session, session.begin():
            await SqlAlchemyKnowledgeBaseRepository(session).add(scope, kb)

        async def increment() -> None:
            """每次更新使用独立事务，复现不同 Worker 同时完成的竞争。"""
            async with AsyncSession(engine) as session, session.begin():
                assert await SqlAlchemyKnowledgeBaseRepository(session).increment_revision(
                    scope, kb.id,
                )

        await asyncio.gather(*(increment() for _ in range(10)))
        async with AsyncSession(engine) as session, session.begin():
            repository = SqlAlchemyKnowledgeBaseRepository(session)
            current = await repository.get(scope, kb.id)
            assert current is not None
            assert current.revision == kb.revision + 10
            assert current.row_version == kb.row_version + 10
            assert current.name == kb.name
            # 旧对象不能覆盖已经推进的版本，跨租户和不存在的 ID 也不可写。
            assert not await repository.update(scope, kb, kb.row_version)
            assert not await repository.increment_revision("other-scope", kb.id)
            assert not await repository.increment_revision(scope, uuid4())
            current.name = "edited-name"
            assert await repository.update(scope, current, current.row_version)
            assert await repository.increment_revision(scope, kb.id)
            loaded = await repository.get(scope, kb.id)
            assert loaded is not None and loaded.name == "edited-name"
            assert loaded.revision == kb.revision + 11
            # 删除后的知识库不能被完成任务重新激活或增加内容版本。
            await session.execute(
                text("UPDATE rag_app.knowledge_base SET status = 'DELETED' WHERE id = :id"),
                {"id": kb.id},
            )
            assert not await repository.increment_revision(scope, kb.id)
    finally:
        await engine.dispose()
