"""数据库启动门禁测试：确认两个逻辑数据库共用唯一业务所有者账号。"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from rag_service.bootstrap.database_schema import (
    APP_ALEMBIC_HEAD,
    APP_INITIAL_TABLES,
    APP_LEGACY_ALEMBIC_HEAD,
    APP_LEGACY_SCHEMA_CHECKSUM,
    APP_LEGACY_SCHEMA_RELEASE,
    APP_PREVIOUS_SCHEMA_CHECKSUM,
    APP_PREVIOUS_SCHEMA_RELEASE,
    APP_PREVIOUS_TABLES,
    APP_RELEASE_COLUMNS,
    APP_SCHEMA_CHECKSUM,
    APP_SCHEMA_RELEASE,
    APP_TABLES,
    RAGFLOW_TABLES,
    _classify_local_app_schema,
    _enforce_external_permission_contract,
    prepare_local_app_database_schema,
)
from rag_service.cli import build_parser


def test_schema_check_cli_is_explicit_read_only_startup_command() -> None:
    """只读检查与 local 写入入口必须是两个显式 CLI 目标，不能隐式混用。"""
    arguments = build_parser().parse_args(["schema", "check"])

    assert arguments.command == "schema"
    assert arguments.target == "check"

    app_arguments = build_parser().parse_args(["schema", "check-app"])
    assert app_arguments.command == "schema"
    assert app_arguments.target == "check-app"

    local_arguments = build_parser().parse_args(["schema", "prepare-local"])
    assert local_arguments.command == "schema"
    assert local_arguments.target == "prepare-local"


def test_local_schema_plan_covers_empty_legacy_stamped_and_ready_states() -> None:
    """逐级识别空库、未登记 0006、V1.0、V1.1 和 V1.2，不跳过历史迁移。"""
    empty = _classify_local_app_schema(
        actual_tables=frozenset(),
        revisions=(),
        release_columns=frozenset(),
    )
    # 更早历史库必须保留现场并按旧检查点升级，纯 SQL 入口不能猜测或重建。
    with pytest.raises(RuntimeError, match="legacy schema before 0006"):
        _classify_local_app_schema(
            actual_tables=APP_INITIAL_TABLES,
            revisions=("0002_document_external_reuse",),
            release_columns=frozenset({"version_num"}),
        )
    unstamped_head = _classify_local_app_schema(
        actual_tables=APP_PREVIOUS_TABLES,
        revisions=(APP_LEGACY_ALEMBIC_HEAD,),
        release_columns=frozenset({"version_num"}),
    )
    upgrade_shared = _classify_local_app_schema(
        actual_tables=APP_PREVIOUS_TABLES,
        revisions=(APP_LEGACY_ALEMBIC_HEAD,),
        release_columns=APP_RELEASE_COLUMNS,
        release_version=APP_LEGACY_SCHEMA_RELEASE,
        schema_checksum=APP_LEGACY_SCHEMA_CHECKSUM,
        release_applied=True,
    )
    upgrade = _classify_local_app_schema(
        actual_tables=APP_PREVIOUS_TABLES,
        revisions=(APP_LEGACY_ALEMBIC_HEAD,),
        release_columns=APP_RELEASE_COLUMNS,
        release_version=APP_PREVIOUS_SCHEMA_RELEASE,
        schema_checksum=APP_PREVIOUS_SCHEMA_CHECKSUM,
        release_applied=True,
    )
    ready = _classify_local_app_schema(
        actual_tables=APP_TABLES,
        revisions=(APP_ALEMBIC_HEAD,),
        release_columns=APP_RELEASE_COLUMNS,
        release_version=APP_SCHEMA_RELEASE,
        schema_checksum=APP_SCHEMA_CHECKSUM,
        release_applied=True,
    )

    assert (empty.action, empty.revision) == ("migrate", None)
    assert unstamped_head.action == "stamp"
    assert upgrade_shared.action == "upgrade_shared"
    assert upgrade.action == "upgrade"
    assert ready.action == "ready"


@pytest.mark.parametrize(
    ("tables", "revision", "columns"),
    [
        (APP_TABLES - {"document_version"}, APP_ALEMBIC_HEAD, {"version_num"}),
        (
            APP_TABLES,
            APP_ALEMBIC_HEAD,
            {"version_num", "release_version"},
        ),
        (APP_INITIAL_TABLES, "9999_unknown", {"version_num"}),
    ],
)
def test_local_schema_plan_refuses_partial_or_unknown_schema(
    tables: frozenset[str],
    revision: str,
    columns: set[str],
) -> None:
    """表集合、发布字段或 Revision 不一致时必须停机，不能猜测并覆盖现场。"""
    with pytest.raises(RuntimeError, match="automatic .* was refused|partial release stamp"):
        _classify_local_app_schema(
            actual_tables=tables,
            revisions=(revision,),
            release_columns=frozenset(columns),
        )


@pytest.mark.asyncio
async def test_prepare_local_rejects_external_before_reading_database_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """external 即使误调用写命令，也必须在读取 DSN 或连接数据库前拒绝。"""
    monkeypatch.setenv("RAG_STORAGE_MODE", "external")
    monkeypatch.delenv("APP_DATABASE_DSN", raising=False)

    with pytest.raises(RuntimeError, match="external databases remain DBA-managed"):
        await prepare_local_app_database_schema()


def test_start_full_runs_schema_prepare_only_inside_local_branch() -> None:
    """启动脚本只在 local 分支覆盖命令，随后仍由 Compose 执行普通只读检查。"""
    repository_root = Path(__file__).resolve().parents[2]
    start_script = (repository_root / "deploy" / "local" / "start-full.sh").read_text(
        encoding="utf-8"
    )
    compose = (repository_root / "compose.local.yml").read_text(encoding="utf-8")

    local_branch = 'if [ "${storage_mode:-local}" = "local" ]; then'
    prepare_command = "python -m rag_service.cli schema prepare-local"
    assert start_script.index(local_branch) < start_script.index(prepare_command)
    assert start_script.index(prepare_command) < start_script.index(
        "fi", start_script.index(prepare_command)
    )
    assert 'command: ["python", "-m", "rag_service.cli", "schema", "check"]' in compose
    assert "RAG_STORAGE_MODE: ${RAG_STORAGE_MODE}" in compose


def test_permission_contract_is_strict_only_for_external_storage(monkeypatch) -> None:
    """外部库严格检查单角色所有权，本地历史开发卷仍只读采集权限摘要。"""
    monkeypatch.setenv("RAG_STORAGE_MODE", "external")
    assert _enforce_external_permission_contract() is True

    monkeypatch.setenv("RAG_STORAGE_MODE", "local")
    assert _enforce_external_permission_contract() is False


def test_expected_schema_fingerprint_matches_reviewed_install_package() -> None:
    """锁定 V1.2.0 的 14 张应用表、0007 头和不可变摘要。"""
    assert len(APP_TABLES) == 14
    assert len(RAGFLOW_TABLES) == 42
    assert APP_ALEMBIC_HEAD == "0007_native_graphrag"
    assert APP_SCHEMA_RELEASE == "V1.2.0"
    # 摘要输入按“Schema、正式版本、历史头、排序后的表名”逐行编码，并保留末尾
    # 换行。这样 DBA、测试和启动门禁使用完全相同、可人工复算的固定规则。
    release_manifest = "\n".join(
        ["rag_app", APP_SCHEMA_RELEASE, APP_ALEMBIC_HEAD, *sorted(APP_TABLES), ""]
    )
    assert APP_SCHEMA_CHECKSUM == hashlib.sha256(release_manifest.encode()).hexdigest()
    assert APP_RELEASE_COLUMNS == {
        "version_num",
        "release_version",
        "schema_checksum",
        "applied_at",
    }
    assert "alembic_version" in APP_TABLES
    assert {"knowledgebase", "document", "task", "tenant"} <= RAGFLOW_TABLES


def test_reviewed_install_sql_matches_runtime_schema_contract() -> None:
    """结构迁移覆盖两库全部表，但不得混入角色、建库或授权操作。"""
    repository_root = Path(__file__).resolve().parents[2]
    install_sql = (
        repository_root / "migrations" / "rag_app" / "V1.0.0__init_schema.sql"
    ).read_text(encoding="utf-8")

    flow_sql = (repository_root / "migrations/rag_flow/V0.26.4__init_schema.sql").read_text(
        encoding="utf-8"
    )
    # 应用表没有引号标识符；RAGFlow 的 user 表使用双引号，因此分别捕获两种形式。
    app_tables = frozenset(re.findall(r"CREATE TABLE rag_app\.([a-z0-9_]+)", install_sql))
    ragflow_matches = re.findall(
        r'CREATE TABLE public\.(?:"([a-z0-9_]+)"|([a-z0-9_]+))',
        flow_sql,
    )
    ragflow_tables = frozenset(quoted or plain for quoted, plain in ragflow_matches)

    assert app_tables == APP_PREVIOUS_TABLES
    assert ragflow_tables == RAGFLOW_TABLES
    assert "release_version character varying(32) NOT NULL" in install_sql
    assert "schema_checksum character(64) NOT NULL" in install_sql
    assert "INSERT INTO rag_app.alembic_version (" in install_sql
    assert f"'{APP_LEGACY_ALEMBIC_HEAD}'" in install_sql
    assert f"'{APP_LEGACY_SCHEMA_RELEASE}'" in install_sql
    assert APP_LEGACY_SCHEMA_CHECKSUM in install_sql
    for script in (install_sql, flow_sql):
        assert re.search(r"(?m)^(CREATE ROLE|CREATE DATABASE|GRANT |REVOKE )", script) is None
        assert "BEGIN;" in script and "COMMIT;" in script
    assert all(
        path.suffix == ".sql"
        for path in (repository_root / "migrations").rglob("*")
        if path.is_file()
    )


def test_v1_release_stamp_sql_is_guarded_and_does_not_create_business_tables() -> None:
    """现有 0006 数据库只能由 DBA 盖章，脚本不得借机重建业务表。"""
    repository_root = Path(__file__).resolve().parents[2]
    stamp_sql = (
        repository_root / "migrations" / "rag_app" / "V1.0.0__stamp_existing_schema.sql"
    ).read_text(encoding="utf-8")

    assert "CREATE TABLE rag_app." not in stamp_sql
    assert "current_database() <> 'rag_app'" in stamp_sql
    assert "0006_document_version_updates" in stamp_sql
    assert f"'{APP_LEGACY_SCHEMA_RELEASE}'" in stamp_sql
    assert APP_LEGACY_SCHEMA_CHECKSUM in stamp_sql
    assert "BEGIN;" in stamp_sql
    assert "COMMIT;" in stamp_sql


def test_v11_release_sql_is_guarded_and_never_deletes_existing_data() -> None:
    """共享知识库迁移必须要求空业务表，并且只做结构和发布标识修改。"""
    repository_root = Path(__file__).resolve().parents[2]
    release_sql = (
        repository_root
        / "migrations"
        / "rag_app"
        / "V1.0.0__to__V1.1.0__shared_knowledge_bases.sql"
    ).read_text(encoding="utf-8")

    assert "scope_type character varying(16) NOT NULL" in release_sql
    assert "resource_scope_id character(64) NOT NULL" in release_sql
    assert "V1.0.0" in release_sql
    assert f"'{APP_PREVIOUS_SCHEMA_RELEASE}'" in release_sql
    assert APP_PREVIOUS_SCHEMA_CHECKSUM in release_sql
    assert "requires empty" in release_sql
    assert re.search(r"\b(DELETE FROM|TRUNCATE TABLE|DROP TABLE)\b", release_sql) is None
    assert "BEGIN;" in release_sql
    assert "COMMIT;" in release_sql


def test_v12_release_sql_adds_graph_state_and_preserves_existing_business_data() -> None:
    """新增状态和任务约束只能从 V1.1 前进；升级不得删除旧内容或隐式启用图谱。"""
    repository_root = Path(__file__).resolve().parents[2]
    release_sql = (
        repository_root / "migrations/rag_app/V1.1.0__to__V1.2.0__native_graphrag.sql"
    ).read_text(encoding="utf-8")

    assert "current_database() <> 'rag_app'" in release_sql
    assert f"current_release <> '{APP_PREVIOUS_SCHEMA_RELEASE}'" in release_sql
    assert APP_PREVIOUS_SCHEMA_CHECKSUM in release_sql
    assert f"version_num = '{APP_ALEMBIC_HEAD}'" in release_sql
    assert f"release_version = '{APP_SCHEMA_RELEASE}'" in release_sql
    assert APP_SCHEMA_CHECKSUM in release_sql
    assert "PRIMARY KEY (security_scope_id, knowledge_base_id)" in release_sql
    assert "REFERENCES rag_app.knowledge_base (security_scope_id, id)" in release_sql
    assert "CREATE UNIQUE INDEX uk_graph_job_inflight" in release_sql
    assert "job_type = 'GRAPH_BUILD'" in release_sql
    assert "'GRAPH'" in release_sql
    assert "INSERT INTO rag_app.graph_state" not in release_sql
    assert re.search(r"\b(DELETE FROM|TRUNCATE TABLE|DROP TABLE)\b", release_sql) is None
    assert "BEGIN;" in release_sql and "COMMIT;" in release_sql


@pytest.mark.parametrize(
    ("tables", "revision", "release", "checksum", "applied"),
    [
        (APP_TABLES, APP_ALEMBIC_HEAD, None, None, False),
        (APP_PREVIOUS_TABLES, APP_LEGACY_ALEMBIC_HEAD, "V1.1.0", "f" * 64, True),
        (APP_TABLES, APP_ALEMBIC_HEAD, APP_SCHEMA_RELEASE, APP_SCHEMA_CHECKSUM, False),
        (
            APP_TABLES, APP_ALEMBIC_HEAD,
            APP_PREVIOUS_SCHEMA_RELEASE, APP_PREVIOUS_SCHEMA_CHECKSUM, True,
        ),
    ],
)
def test_local_schema_plan_never_restamps_unrecognized_graph_release(
    tables: frozenset[str], revision: str, release: str | None,
    checksum: str | None, applied: bool,
) -> None:
    """图谱版缺登记、摘要不符或版本混搭时必须拒绝，不把现场误判为可升级旧库。"""
    with pytest.raises(RuntimeError, match="refused"):
        _classify_local_app_schema(
            actual_tables=tables, revisions=(revision,),
            release_columns=APP_RELEASE_COLUMNS if release else frozenset({"version_num"}),
            release_version=release, schema_checksum=checksum, release_applied=applied,
        )
