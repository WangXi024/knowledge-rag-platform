"""只在显式配置的隔离 PostgreSQL 集群验证 canonical 库名的 SQL 交付链。"""

import asyncio
import os
import re
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from rag_service.bootstrap.database_schema import (
    check_database_schemas,
    prepare_local_app_database_schema,
    prepare_local_ragflow_schema,
)

ROOT = Path(__file__).resolve().parents[2]
SUPPORT = ROOT / "deploy/ecs/database"


@pytest.mark.postgres
def test_canonical_sql_install_guards_comments_and_access(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证两库安装、版本保护、原子拒绝、注释验收和跨库隔离；不连接项目现有库。"""
    dsn = os.getenv("TEST_SQL_ADMIN_DSN")
    if not dsn:
        pytest.skip("requires TEST_SQL_ADMIN_DSN on an empty disposable PostgreSQL cluster")
    parameters = conninfo_to_dict(dsn)
    if parameters.get("host") not in {"127.0.0.1", "localhost"}:
        pytest.fail("SQL acceptance only permits a loopback disposable cluster")
    with psycopg.connect(dsn, autocommit=True) as admin:
        if admin.execute(
            "SELECT 1 FROM pg_database WHERE datname IN ('rag_app','rag_flow') "
            "UNION ALL SELECT 1 FROM pg_roles WHERE rolname='lazytt_rag'"
        ).fetchone():
            pytest.fail("canonical database or role already exists; refusing to touch it")
        created: list[str] = []
        test_acl_changed = False
        try:
            # A/B/C 与 pgAdmin 操作卡一致，CREATE DATABASE 各自独立提交。
            role_sql = (SUPPORT / "01_roles_databases.pgadmin.sql").read_text().split("-- B：")[0]
            admin.execute(role_sql)
            for name in ("rag_app", "rag_flow"):
                admin.execute(
                    sql.SQL("CREATE DATABASE {} OWNER lazytt_rag TEMPLATE template0").format(
                        sql.Identifier(name)
                    )
                )
                created.append(name)
                with psycopg.connect(
                    **{**parameters, "dbname": name}, autocommit=True
                ) as connection:
                    connection.execute(
                        (SUPPORT / "02_database_permissions.pgadmin.sql").read_text()
                    )
                    connection.execute("SET ROLE lazytt_rag")
                    filename = (
                        "V1.0.0__init_schema.sql"
                        if name == "rag_app"
                        else "V0.26.4__init_schema.sql"
                    )
                    initial = (ROOT / "migrations" / name / filename).read_text()
                    connection.execute(initial)
                    with pytest.raises(psycopg.Error):
                        connection.execute(initial)
                    connection.execute("ROLLBACK")
                    if name == "rag_app":
                        stamp = (
                            ROOT / "migrations/rag_app/V1.0.0__stamp_existing_schema.sql"
                        ).read_text()
                        # 已登记 V1.0 可以幂等盖章；旧 0006 只缺版本字段时也可恢复登记。
                        connection.execute(stamp)
                        connection.execute(
                            "ALTER TABLE rag_app.alembic_version DROP COLUMN release_version, "
                            "DROP COLUMN schema_checksum, DROP COLUMN applied_at"
                        )
                        connection.execute(stamp)
                        upgrade = (
                            ROOT
                            / "migrations/rag_app/V1.0.0__to__V1.1.0__shared_knowledge_bases.sql"
                        ).read_text()
                        connection.execute(
                            "INSERT INTO rag_app.idempotency_record "
                            "(security_scope_id,idempotency_key,http_method,request_path,request_sha256,"
                            "processing_status,expires_at) VALUES "
                            "(repeat('a',64),'test-only','POST','/test',repeat('b',64),"
                            "'PROCESSING',now()+interval '1 hour')"
                        )
                        with pytest.raises(psycopg.Error, match="no data was deleted"):
                            connection.execute(upgrade)
                        connection.execute("ROLLBACK")
                        assert connection.execute(
                            "SELECT count(*) FROM rag_app.idempotency_record"
                        ).fetchone() == (1,)
                        assert connection.execute(
                            "SELECT release_version FROM rag_app.alembic_version"
                        ).fetchone() == ("V1.0.0",)
                        # 仅删除此测试刚插入的合成行，不提供部署数据清理功能。
                        connection.execute(
                            "DELETE FROM rag_app.idempotency_record "
                            "WHERE idempotency_key='test-only'"
                        )
                        connection.execute(upgrade)
                        with pytest.raises(psycopg.Error):
                            connection.execute(stamp)
                        connection.execute("ROLLBACK")
                        assert connection.execute(
                            "SELECT release_version FROM rag_app.alembic_version"
                        ).fetchone() == ("V1.1.0",)
                    connection.execute((SUPPORT / f"03_{name}_comments.sql").read_text())
                    verification = (SUPPORT / f"04_verify_{name}.sql").read_text()
                    expected = re.findall(
                        r"\('([^']+)', '([^']+)', '([^']+)', (true|false)\)", verification
                    )
                    actual = {
                        (table, column): (datatype, required)
                        for table, column, datatype, required in connection.execute(
                            "SELECT c.relname,a.attname,"
                            "format_type(a.atttypid,a.atttypmod),a.attnotnull "
                            "FROM pg_attribute a JOIN pg_class c ON c.oid=a.attrelid "
                            "JOIN pg_namespace n ON n.oid=c.relnamespace "
                            "WHERE n.nspname=%s AND a.attnum>0 AND NOT a.attisdropped",
                            ("rag_app" if name == "rag_app" else "public",),
                        ).fetchall()
                    }
                    assert not [
                        (table, column, datatype, required, actual.get((table, column)))
                        for table, column, datatype, required in expected
                        if actual.get((table, column)) != (datatype, required == "true")
                    ]
                    connection.execute(verification)
                    # 已有注释和验收可重复执行，不能写业务数据。
                    connection.execute((SUPPORT / f"03_{name}_comments.sql").read_text())
                    connection.execute((SUPPORT / f"04_verify_{name}.sql").read_text())
            # 用真实运行角色验证本地重复启动不改表，external 门禁只读且可消费同一结构。
            admin.execute("ALTER ROLE lazytt_rag PASSWORD 'test-only-password'")
            port = parameters.get("port", "5432")
            monkeypatch.setenv(
                "APP_DATABASE_DSN",
                f"postgresql+psycopg://lazytt_rag:test-only-password@127.0.0.1:{port}/rag_app",
            )
            for key, value in {
                "RAG_STORAGE_MODE": "local",
                "RAGFLOW_POSTGRES_HOST": "127.0.0.1",
                "RAGFLOW_POSTGRES_PORT": port,
                "RAGFLOW_POSTGRES_DATABASE": "rag_flow",
                "RAGFLOW_POSTGRES_USER": "lazytt_rag",
                "RAGFLOW_POSTGRES_PASSWORD": "test-only-password",
            }.items():
                monkeypatch.setenv(key, value)
            assert asyncio.run(prepare_local_app_database_schema())["ddl_executed"] is False
            assert asyncio.run(prepare_local_ragflow_schema())["ddl_executed"] is False
            monkeypatch.setenv("RAG_STORAGE_MODE", "external")
            assert asyncio.run(check_database_schemas())["status"] == "passed"
            admin.execute("SET ROLE lazytt_rag")
            # 测试集群预设的 rag_app_test 是其他库，先验证隔离检查会拒绝默认 PUBLIC CONNECT。
            if admin.execute(
                "SELECT 1 FROM pg_database WHERE datname='rag_app_test' "
                "AND has_database_privilege('lazytt_rag',oid,'CONNECT')"
            ).fetchone():
                with pytest.raises(psycopg.Error):
                    admin.execute((SUPPORT / "05_verify_database_access.sql").read_text())
                admin.execute("ROLLBACK")
                admin.execute("RESET ROLE")
                admin.execute("REVOKE CONNECT ON DATABASE rag_app_test FROM PUBLIC")
                test_acl_changed = True
                admin.execute("SET ROLE lazytt_rag")
            admin.execute((SUPPORT / "05_verify_database_access.sql").read_text())
            for name, filename in (
                ("rag_app", "V1.0.0__init_schema.sql"),
                ("rag_flow", "V0.26.4__init_schema.sql"),
            ):
                with pytest.raises(psycopg.Error):
                    admin.execute((ROOT / "migrations" / name / filename).read_text())
                admin.execute("ROLLBACK")
        finally:
            admin.execute("ROLLBACK")
            admin.execute("RESET ROLE")
            if test_acl_changed:
                admin.execute("GRANT CONNECT ON DATABASE rag_app_test TO PUBLIC")
            for name in created:
                admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
            admin.execute("DROP ROLE IF EXISTS lazytt_rag")
