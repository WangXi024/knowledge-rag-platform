"""准备本地 PostgreSQL Schema，并只读校验所有存储模式的结构与所有权边界。

``local`` 模式供本地开发使用：启动脚本会先执行冻结 SQL、登记兼容的
V1.0.0 基线，再执行仓库评审的 V1.1.0 增量 SQL。该写入入口显式拒绝 ``external``，
且在表集合、历史版本、发布字段或待清理数据不满足前置条件时停止。所有模式随后仍
执行同一套只读门禁。外部数据库继续完全由 DBA 执行评审 SQL。
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

APP_SCHEMA = "rag_app"
APP_ALEMBIC_HEAD = "0006_document_version_updates"
APP_PREVIOUS_SCHEMA_RELEASE = "V1.0.0"
APP_PREVIOUS_SCHEMA_CHECKSUM = "427e6747cf6054d521ad3b292b81a4721612e43494e1560794a821f8bfed83d3"
APP_SCHEMA_RELEASE = "V1.1.0"
# 该摘要来自“Schema 名 + 发布版本 + Alembic 头 + 13 张基础表”的规范化清单。
# 启动门禁只比较固定值，不在运行时重新生成，避免不同平台的序列化差异。
APP_SCHEMA_CHECKSUM = "6f61654d651c73816a9dd85db3a55bfbd8f5ab5f1296908de60d93aa873f3109"
APP_LOCAL_RELEASE_SQL = "V1.0.0__to__V1.1.0__shared_knowledge_bases.sql"
APP_RELEASE_COLUMNS = frozenset({"version_num", "release_version", "schema_checksum", "applied_at"})
APP_RELEASE_METADATA_COLUMNS = APP_RELEASE_COLUMNS - {"version_num"}
APP_TABLES = frozenset(
    {
        "alembic_version",
        "citation_record",
        "config_profile",
        "document_entity",
        "document_index_node",
        "document_record",
        "document_version",
        "entity_relation",
        "feedback_record",
        "idempotency_record",
        "ingestion_job",
        "knowledge_base",
        "query_record",
    }
)
# 开发期旧卷可能停留在任一已发布的 Alembic Revision。下面的精确表集合只用于在
# 自动升级前识别“合法旧版本”；如果版本号与表集合互相矛盾，就把现场留给开发者
# 检查，而不是猜测应该补表还是改版本号。
APP_INITIAL_TABLES = frozenset(
    {
        "alembic_version",
        "citation_record",
        "config_profile",
        "document_record",
        "feedback_record",
        "idempotency_record",
        "ingestion_job",
        "knowledge_base",
        "query_record",
    }
)
APP_TABLES_BY_REVISION = {
    "0001_initial_rag_app": APP_INITIAL_TABLES,
    "0002_document_external_reuse": APP_INITIAL_TABLES,
    "0003_document_hierarchical_index": APP_INITIAL_TABLES | {"document_index_node"},
    "0004_entity_graph_index": APP_INITIAL_TABLES
    | {"document_entity", "document_index_node", "entity_relation"},
    "0005_allow_section_index_nodes": APP_INITIAL_TABLES
    | {"document_entity", "document_index_node", "entity_relation"},
    APP_ALEMBIC_HEAD: APP_TABLES,
}
RAGFLOW_TABLES = frozenset(
    {
        "api_4_conversation",
        "api_token",
        "canvas_template",
        "chat_channel",
        "compilation_template",
        "compilation_template_group",
        "connector",
        "connector2kb",
        "conversation",
        "dialog",
        "document",
        "evaluation_cases",
        "evaluation_datasets",
        "evaluation_results",
        "evaluation_runs",
        "file",
        "file2document",
        "file_commit",
        "file_commit_item",
        "invitation_code",
        "knowledgebase",
        "llm",
        "llm_factories",
        "mcp_server",
        "memory",
        "pipeline_operation_log",
        "search",
        "sync_logs",
        "system_settings",
        "task",
        "tenant",
        "tenant_langfuse",
        "tenant_llm",
        "tenant_model",
        "tenant_model_group",
        "tenant_model_group_mapping",
        "tenant_model_instance",
        "tenant_model_provider",
        "user",
        "user_canvas",
        "user_canvas_version",
        "user_tenant",
    }
)


@dataclass(frozen=True)
class SchemaCheckResult:
    """一套数据库的只读校验摘要，不包含密码、连接串或其他敏感值。"""

    database: str
    schema: str
    table_count: int
    create_privilege: bool
    schema_owned_by_runtime: bool
    runtime_owned_table_count: int
    alembic_revision: str | None = None
    schema_release: str | None = None
    schema_checksum: str | None = None


@dataclass(frozen=True)
class LocalSchemaPlan:
    """本地 Schema 当前可执行的唯一安全动作。

    ``action`` 只会是 ``migrate``、``stamp``、``upgrade`` 或 ``ready``：分别表示运行
    现有迁移、登记旧基线、执行正式增量 SQL、以及无需写入。
    """

    action: str
    revision: str | None


def _classify_local_app_schema(
    *,
    actual_tables: frozenset[str],
    revisions: tuple[str, ...],
    release_columns: frozenset[str],
    release_version: str | None = None,
    schema_checksum: str | None = None,
    release_applied: bool = False,
) -> LocalSchemaPlan:
    """根据只读目录快照决定本地库是否可迁移、可盖章或已经就绪。

    参数是系统目录查询的脱敏结果；函数本身不连接数据库、不执行 DDL。空库可以进入
    冻结 SQL，合法旧 Revision 必须与当时的精确表集合一致，0006 只能在发布字段全部
    缺失时盖章。任何部分字段、未知版本、额外表或错误摘要都会抛错并保留现场。
    """
    if not actual_tables:
        if revisions or release_columns:
            raise RuntimeError("local rag_app schema metadata exists without application tables")
        return LocalSchemaPlan(action="migrate", revision=None)

    if "alembic_version" not in actual_tables:
        raise RuntimeError(
            "local rag_app schema contains business tables without alembic_version; "
            "automatic repair was refused"
        )
    if len(revisions) != 1:
        raise RuntimeError("local rag_app must contain exactly one Alembic revision")

    revision = revisions[0]
    expected_tables = APP_TABLES_BY_REVISION.get(revision)
    if expected_tables is None:
        raise RuntimeError(
            f"local rag_app Alembic revision {revision} is unknown; automatic migration was refused"
        )
    if actual_tables != expected_tables:
        missing = ", ".join(sorted(expected_tables - actual_tables)) or "none"
        extra = ", ".join(sorted(actual_tables - expected_tables)) or "none"
        raise RuntimeError(
            f"local rag_app schema does not match Alembic revision {revision}; "
            f"missing tables: {missing}; extra tables: {extra}; automatic repair was refused"
        )

    present_release_columns = release_columns & APP_RELEASE_METADATA_COLUMNS
    if present_release_columns and present_release_columns != APP_RELEASE_METADATA_COLUMNS:
        columns = ", ".join(sorted(present_release_columns))
        raise RuntimeError(
            f"local rag_app has a partial release stamp ({columns}); automatic repair was refused"
        )
    if revision != APP_ALEMBIC_HEAD:
        if present_release_columns:
            raise RuntimeError(
                f"local rag_app revision {revision} already has release metadata; "
                "automatic migration was refused"
            )
        raise RuntimeError(
            "legacy schema before 0006 requires the checkpoint upgrade procedure; "
            "automatic migration was refused"
        )

    if not present_release_columns:
        return LocalSchemaPlan(action="stamp", revision=revision)
    if not release_applied:
        raise RuntimeError(
            "local rag_app release timestamp is missing; automatic overwrite was refused"
        )
    if (
        release_version == APP_PREVIOUS_SCHEMA_RELEASE
        and schema_checksum == APP_PREVIOUS_SCHEMA_CHECKSUM
    ):
        return LocalSchemaPlan(action="upgrade", revision=revision)
    if release_version != APP_SCHEMA_RELEASE or schema_checksum != APP_SCHEMA_CHECKSUM:
        raise RuntimeError(
            f"local rag_app release metadata does not match {APP_SCHEMA_RELEASE}; "
            "automatic overwrite was refused"
        )
    return LocalSchemaPlan(action="ready", revision=revision)


async def _inspect_local_app_schema(
    connection: psycopg.AsyncConnection[tuple[object, ...]],
) -> LocalSchemaPlan:
    """只读采集本地 ``rag_app`` 的表、Revision 与发布字段并生成安全动作。

    查询不读取任何业务行，只访问 ``information_schema`` 和单行 ``alembic_version``。
    返回动作交给调用方执行；目录快照不满足已知状态时直接报错，不尝试修改数据库。
    """
    async with connection.cursor() as cursor:
        await cursor.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = %s AND table_type = 'BASE TABLE'
            """,
            (APP_SCHEMA,),
        )
        actual_tables = frozenset(str(row[0]) for row in await cursor.fetchall())
        if not actual_tables:
            return _classify_local_app_schema(
                actual_tables=actual_tables,
                revisions=(),
                release_columns=frozenset(),
            )

        await cursor.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = 'alembic_version'
            """,
            (APP_SCHEMA,),
        )
        release_columns = frozenset(str(row[0]) for row in await cursor.fetchall())
        if "version_num" not in release_columns:
            return _classify_local_app_schema(
                actual_tables=actual_tables,
                revisions=(),
                release_columns=release_columns,
            )

        complete_release_metadata = APP_RELEASE_METADATA_COLUMNS <= release_columns
        if complete_release_metadata:
            await cursor.execute(
                sql.SQL(
                    "SELECT version_num, release_version, schema_checksum, "
                    "applied_at IS NOT NULL FROM {}.alembic_version"
                ).format(sql.Identifier(APP_SCHEMA))
            )
        else:
            await cursor.execute(
                sql.SQL("SELECT version_num FROM {}.alembic_version").format(
                    sql.Identifier(APP_SCHEMA)
                )
            )
        rows = await cursor.fetchall()

    revisions = tuple(str(row[0]) for row in rows)
    release_version = str(rows[0][1]) if len(rows) == 1 and complete_release_metadata else None
    schema_checksum = str(rows[0][2]) if len(rows) == 1 and complete_release_metadata else None
    release_applied = bool(rows[0][3]) if len(rows) == 1 and complete_release_metadata else False
    return _classify_local_app_schema(
        actual_tables=actual_tables,
        revisions=revisions,
        release_columns=release_columns,
        release_version=release_version,
        schema_checksum=schema_checksum,
        release_applied=release_applied,
    )


def migration_sql(database: str, filename: str) -> str:
    """读取固定数据库目录内的 SQL；拒绝路径穿越和非 SQL 文件，不包含连接凭据。

    本地初始化、显式 ECS 迁移及隔离测试复用同一文件。镜像通过 Dockerfile 携带
    migrations，函数只读取资源，不负责决定何时允许数据库写入。
    """
    if database not in {"rag_app", "rag_flow"} or Path(filename).name != filename:
        raise ValueError("invalid migration database or filename")
    if not filename.endswith(".sql"):
        raise ValueError("migration must be a SQL file")
    for candidate in (Path.cwd().resolve(), *Path(__file__).resolve().parents):
        resource = candidate / "migrations" / database / filename
        if resource.is_file():
            return resource.read_text(encoding="utf-8")
    raise RuntimeError("the requested SQL migration is not packaged with this candidate")


def _connection_parameters(database: str) -> dict[str, Any]:
    """只读取目标数据库所需的凭据；迁移不要求模型、账号或缓存配置。"""
    if database == "rag_app":
        url = make_url(_required_environment("APP_DATABASE_DSN"))
        return {
            "host": url.host,
            "port": url.port or 5432,
            "dbname": url.database,
            "user": url.username,
            "password": url.password,
            "connect_timeout": 5,
        }
    return {
        "host": _required_environment("RAGFLOW_POSTGRES_HOST"),
        "port": int(_required_environment("RAGFLOW_POSTGRES_PORT")),
        "dbname": _required_environment("RAGFLOW_POSTGRES_DATABASE"),
        "user": _required_environment("RAGFLOW_POSTGRES_USER"),
        "password": _required_environment("RAGFLOW_POSTGRES_PASSWORD"),
        "connect_timeout": 5,
    }


async def execute_migration(database: str, filename: str) -> dict[str, object]:
    """显式管理入口：执行候选内一个 SQL 文件；普通检查和服务启动不调用此函数。

    SQL 自行管理事务及前后置条件，失败关闭连接并保留已提交的旧结构；只输出文件名
    和状态，不输出 DSN、SQL 参数或数据库错误详情。
    """
    statement = migration_sql(database, filename)
    async with await psycopg.AsyncConnection.connect(
        **_connection_parameters(database), autocommit=True
    ) as connection:
        await connection.execute(statement, prepare=False)
    return {"status": "applied", "database": database, "migration": filename}


def _required_environment(name: str) -> str:
    """读取必填环境变量；错误只包含变量名，绝不回显真实凭据。"""
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"database schema check requires {name}")
    return value


async def prepare_local_app_database_schema() -> dict[str, object]:
    """在显式 local 模式准备应用库；空库和 0006/V1.0/V1.1 复用同一纯 SQL 链。

    旧的 alembic_version 是兼容版本表，绝不因取消 Alembic 删除其数据。更早版本和
    半成品结构拒绝自动处理；V1.1 升级对有业务数据的 V1.0 库继续安全拒绝。
    """
    if os.environ.get("RAG_STORAGE_MODE", "").strip().lower() != "local":
        raise RuntimeError(
            "schema prepare-local requires explicit RAG_STORAGE_MODE=local; "
            "external databases remain DBA-managed"
        )
    executed: list[str] = []
    async with await psycopg.AsyncConnection.connect(
        **_connection_parameters("rag_app"), autocommit=True
    ) as connection:
        # 锁只串行化本应用的本地初始化，不锁共享实例或其他子系统。
        await connection.execute("SELECT pg_advisory_lock(716281104)")
        initial_plan = await _inspect_local_app_schema(connection)
        plan = initial_plan
        files = {
            "migrate": "V1.0.0__init_schema.sql",
            "stamp": "V1.0.0__stamp_existing_schema.sql",
            "upgrade": APP_LOCAL_RELEASE_SQL,
        }
        while plan.action != "ready":
            filename = files[plan.action]
            await connection.execute(migration_sql("rag_app", filename), prepare=False)
            executed.append(filename)
            plan = await _inspect_local_app_schema(connection)
    checked = await _check_app_database()
    return {
        "status": "prepared",
        "storage_mode": "local",
        "ddl_executed": bool(executed),
        "migrations": executed,
        "previous_revision": initial_plan.revision,
        "databases": [asdict(checked)],
    }


async def prepare_local_ragflow_schema() -> dict[str, object]:
    """为本地空 RAGFlow 库安装冻结结构；已有完整库只读复用，缺表则停止。

    不注册账号、不创建模型或业务记录。此入口解决空库尚未建表时 schema check 与
    RAGFlow Server 启动互相等待的问题，external 始终拒绝自动写入。
    """
    if os.environ.get("RAG_STORAGE_MODE") != "local":
        raise RuntimeError("external databases remain DBA-managed")
    async with await psycopg.AsyncConnection.connect(
        **_connection_parameters("rag_flow"), autocommit=True
    ) as connection:
        await connection.execute("SELECT pg_advisory_lock(716281105)")
        result = await connection.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        )
        tables = frozenset(str(row[0]) for row in await result.fetchall())
        if tables and not RAGFLOW_TABLES.issubset(tables):
            raise RuntimeError("RAGFlow has a partial schema; automatic repair was refused")
        if not tables:
            await connection.execute(
                migration_sql("rag_flow", "V0.26.4__init_schema.sql"), prepare=False
            )
    return {"status": "prepared", "database": "rag_flow", "ddl_executed": not tables}


async def _inspect_database(
    connection: psycopg.AsyncConnection[tuple[object, ...]],
    *,
    database: str,
    schema: str,
    expected_tables: frozenset[str],
    expected_revision: str | None = None,
    expected_release: str | None = None,
    expected_checksum: str | None = None,
    require_create_revoked: bool = True,
    require_create_granted: bool = False,
    require_runtime_ownership: bool = False,
) -> SchemaCheckResult:
    """只读检查表、版本、DDL 权限和对象所有权，发现漂移时立即拒绝启动。

    外部 PostgreSQL 由 DBA 预先创建结构，但项目运行时统一使用对象所有者
    ``lazytt_rag``。因此外部模式必须证明该账号拥有两个业务 Schema、全部基础表和
    Schema CREATE 权限；检查本身始终只读，不会代替 DBA 执行任何 DDL。
    """
    if require_create_revoked and require_create_granted:
        raise RuntimeError("database schema check received conflicting CREATE requirements")

    await connection.execute("SET TRANSACTION READ ONLY")
    async with connection.cursor() as cursor:
        await cursor.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = %s AND table_type = 'BASE TABLE'
            """,
            (schema,),
        )
        actual_tables = frozenset(str(row[0]) for row in await cursor.fetchall())
        missing_tables = sorted(expected_tables - actual_tables)
        if missing_tables:
            preview = ", ".join(missing_tables[:8])
            suffix = " ..." if len(missing_tables) > 8 else ""
            raise RuntimeError(
                f"{database} schema is incomplete; missing tables: {preview}{suffix}; "
                "ask the DBA to run the reviewed fresh-install SQL"
            )

        revision: str | None = None
        schema_release: str | None = None
        schema_checksum: str | None = None
        if expected_revision is not None:
            # Schema 名来自代码内固定常量，但仍使用 Identifier 组合 SQL，避免把标识符
            # 当成普通字符串拼接，也让静态检查能证明这里不存在注入入口。
            if expected_release is not None or expected_checksum is not None:
                if expected_release is None or expected_checksum is None:
                    raise RuntimeError(
                        "database schema check requires release and checksum together"
                    )
                await cursor.execute(
                    """
                    SELECT column_name
                    FROM information_schema.columns
                    WHERE table_schema = %s AND table_name = 'alembic_version'
                    """,
                    (schema,),
                )
                actual_release_columns = frozenset(str(row[0]) for row in await cursor.fetchall())
                missing_release_columns = sorted(APP_RELEASE_COLUMNS - actual_release_columns)
                if missing_release_columns:
                    raise RuntimeError(
                        f"{database} has not been stamped as {expected_release}; "
                        f"ask the DBA to prepare the reviewed {expected_release} release"
                    )
                await cursor.execute(
                    sql.SQL(
                        "SELECT version_num, release_version, schema_checksum "
                        "FROM {}.alembic_version"
                    ).format(sql.Identifier(schema))
                )
            else:
                await cursor.execute(
                    sql.SQL("SELECT version_num FROM {}.alembic_version").format(
                        sql.Identifier(schema)
                    )
                )
            rows = await cursor.fetchall()
            if len(rows) != 1:
                raise RuntimeError(f"{database} must contain exactly one Alembic revision")
            revision = str(rows[0][0])
            if revision != expected_revision:
                raise RuntimeError(
                    f"{database} Alembic revision is {revision}, expected {expected_revision}; "
                    "application-side migration is disabled"
                )
            if expected_release is not None and expected_checksum is not None:
                schema_release = str(rows[0][1])
                schema_checksum = str(rows[0][2])
                if schema_release != expected_release:
                    raise RuntimeError(
                        f"{database} schema release is {schema_release}, "
                        f"expected {expected_release}; DBA migration is required"
                    )
                if schema_checksum != expected_checksum:
                    raise RuntimeError(
                        f"{database} schema checksum does not match {expected_release}; "
                        "the DBA must review and repair schema drift"
                    )
                # 发布标记是历史记录，不是实际结构的哈希；关键隔离列必须同时存在。
                await cursor.execute(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema=%s AND is_nullable='NO' "
                    "AND (table_name,column_name) IN "
                    "(('knowledge_base','scope_type'),('citation_record','resource_scope_id'))",
                    (schema,),
                )
                if len(await cursor.fetchall()) != 2:
                    raise RuntimeError("V1.1.0 scope columns are missing or nullable")

        await cursor.execute(
            "SELECT has_schema_privilege(current_user, %s, 'CREATE')",
            (schema,),
        )
        row = await cursor.fetchone()
        create_privilege = bool(row and row[0])
        if create_privilege and require_create_revoked:
            raise RuntimeError(
                f"runtime database role still has CREATE on {database}.{schema}; "
                "the DBA must apply the least-privilege grants"
            )
        if not create_privilege and require_create_granted:
            raise RuntimeError(
                f"runtime database role requires CREATE on {database}.{schema}; "
                "the DBA must apply the reviewed single-role ownership grants"
            )

        # 单角色模型要求运行账号直接拥有两个业务 Schema 及其全部基础表。RAGFlow
        # 启动时需要 ALTER 已有表；自研应用虽然正常运行不主动迁移，但同一账号仍是
        # rag_app 的明确所有者，后续结构变更继续由 DBA 审核 SQL 后人工执行。
        await cursor.execute(
            """
            SELECT pg_get_userbyid(nspowner) = current_user
            FROM pg_namespace
            WHERE nspname = %s
            """,
            (schema,),
        )
        schema_owner_row = await cursor.fetchone()
        schema_owned_by_runtime = bool(schema_owner_row and schema_owner_row[0])

        await cursor.execute(
            """
            SELECT count(*)
            FROM pg_class AS relation
            JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
            WHERE namespace.nspname = %s
              AND relation.relkind IN ('r', 'p')
              AND pg_get_userbyid(relation.relowner) = current_user
            """,
            (schema,),
        )
        owned_count_row = await cursor.fetchone()
        # psycopg 的通用游标把查询列标注为 object；COUNT(*) 在 PostgreSQL 中实际
        # 一定返回整数。先转成字符串再转 int，既保留运行语义，也让严格类型检查
        # 明确知道这里不会把任意 Python 对象直接交给 int()。
        runtime_owned_table_count = int(str(owned_count_row[0])) if owned_count_row else 0

        if require_runtime_ownership:
            if not schema_owned_by_runtime:
                raise RuntimeError(
                    f"runtime database role must own schema {database}.{schema}; "
                    "the DBA must apply the single-role ownership grants"
                )
            if runtime_owned_table_count != len(actual_tables):
                raise RuntimeError(
                    f"runtime database role owns {runtime_owned_table_count} of "
                    f"{len(actual_tables)} tables in {database}.{schema}; "
                    "the DBA must transfer every business table to the runtime role"
                )

    return SchemaCheckResult(
        database=database,
        schema=schema,
        table_count=len(actual_tables),
        create_privilege=create_privilege,
        schema_owned_by_runtime=schema_owned_by_runtime,
        runtime_owned_table_count=runtime_owned_table_count,
        alembic_revision=revision,
        schema_release=schema_release,
        schema_checksum=schema_checksum,
    )


def _enforce_external_permission_contract() -> bool:
    """外部存储严格验证统一业务角色所有权；本地旧卷只报告当前权限。

    外部数据库由 DBA 管理，所以启动前必须证明 ``lazytt_rag`` 同时拥有
    ``rag_app`` 与 ``rag_flow`` 的 Schema、基础表和 CREATE 权限。本地历史卷可能
    使用旧权限模型，为避免升级时误伤已有开发数据，本地模式暂时只采集权限摘要。
    """
    return os.environ.get("RAG_STORAGE_MODE", "local").strip().lower() == "external"


async def _check_app_database() -> SchemaCheckResult:
    """连接自研应用数据库并校验 13 张表和唯一 Alembic 头版本。"""
    app_url = make_url(_required_environment("APP_DATABASE_DSN"))
    app_database = app_url.database or ""
    if not app_database:
        raise RuntimeError("APP_DATABASE_DSN must include a database name")

    async with await psycopg.AsyncConnection.connect(
        host=app_url.host,
        port=app_url.port or 5432,
        dbname=app_database,
        user=app_url.username,
        password=app_url.password,
        connect_timeout=5,
    ) as connection:
        app_result = await _inspect_database(
            connection,
            database=app_database,
            schema=APP_SCHEMA,
            expected_tables=APP_TABLES,
            expected_revision=APP_ALEMBIC_HEAD,
            expected_release=APP_SCHEMA_RELEASE,
            expected_checksum=APP_SCHEMA_CHECKSUM,
            require_create_revoked=False,
            require_create_granted=_enforce_external_permission_contract(),
            require_runtime_ownership=_enforce_external_permission_contract(),
        )
    return app_result


async def check_app_database_schema() -> dict[str, object]:
    """只校验应用数据库，供不包含 RAGFlow 的规格最小栈使用。"""
    app_result = await _check_app_database()
    return {
        "status": "passed",
        "ddl_executed": False,
        "external_permission_contract_enforced": _enforce_external_permission_contract(),
        "databases": [asdict(app_result)],
    }


async def check_database_schemas() -> dict[str, object]:
    """校验自研应用与 RAGFlow 两个 PostgreSQL 数据库，返回脱敏摘要。"""
    app_result = await _check_app_database()

    ragflow_database = _required_environment("RAGFLOW_POSTGRES_DATABASE")
    async with await psycopg.AsyncConnection.connect(
        host=_required_environment("RAGFLOW_POSTGRES_HOST"),
        port=int(_required_environment("RAGFLOW_POSTGRES_PORT")),
        dbname=ragflow_database,
        user=_required_environment("RAGFLOW_POSTGRES_USER"),
        password=_required_environment("RAGFLOW_POSTGRES_PASSWORD"),
        connect_timeout=5,
    ) as connection:
        ragflow_result = await _inspect_database(
            connection,
            database=ragflow_database,
            schema="public",
            expected_tables=RAGFLOW_TABLES,
            require_create_revoked=False,
            require_create_granted=_enforce_external_permission_contract(),
            require_runtime_ownership=_enforce_external_permission_contract(),
        )

    return {
        "status": "passed",
        "ddl_executed": False,
        "external_permission_contract_enforced": _enforce_external_permission_contract(),
        "databases": [asdict(app_result), asdict(ragflow_result)],
    }
