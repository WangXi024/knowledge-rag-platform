-- ============================================================================
-- LazyTT RAG PostgreSQL 双角色到单角色迁移脚本
-- ============================================================================
-- 适用场景：旧安装已经存在 lazytt_rag_owner（NOLOGIN）和 lazytt_rag（LOGIN），
-- 现在要让唯一业务账号 lazytt_rag 同时拥有 rag_app 与 rag_flow。
--
-- 执行者：PostgreSQL DBA 管理账号。
-- 数据影响：不修改业务行；会转移两个数据库内对象的 owner，并最终删除旧角色
--           lazytt_rag_owner。必须先备份并在维护窗口执行。
-- 安全边界：本脚本不会授予 SUPERUSER、CREATEDB、CREATEROLE、REPLICATION 或
--           BYPASSRLS；应用配置仍只能保存 lazytt_rag 的连接凭据。
-- ============================================================================

\set ON_ERROR_STOP on
\pset pager off

\echo '==> [1/6] 验证新旧角色和两个逻辑数据库'
SELECT EXISTS (
    SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'lazytt_rag'
) AS runtime_role_exists
\gset
\if :runtime_role_exists
\else
    \echo 'ERROR: 业务角色 lazytt_rag 不存在。'
    \quit 20
\endif

SELECT EXISTS (
    SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'lazytt_rag_owner'
) AS legacy_owner_role_exists
\gset
\if :legacy_owner_role_exists
\else
    \echo 'ERROR: 旧角色 lazytt_rag_owner 不存在；无需执行双角色迁移。'
    \quit 21
\endif

SELECT count(*) = 2 AS both_databases_exist
FROM pg_catalog.pg_database
WHERE datname IN ('rag_app', 'rag_flow')
\gset
\if :both_databases_exist
\else
    \echo 'ERROR: rag_app 或 rag_flow 不存在。'
    \quit 22
\endif

\echo '==> [2/6] 转移两个数据库的所有权'
ALTER DATABASE rag_app OWNER TO lazytt_rag;
ALTER DATABASE rag_flow OWNER TO lazytt_rag;

\echo '==> [3/6] 转移 rag_app 内所有对象并授予 Schema CREATE'
\connect rag_app
BEGIN;
REASSIGN OWNED BY lazytt_rag_owner TO lazytt_rag;
DROP OWNED BY lazytt_rag_owner;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA rag_app OWNER TO lazytt_rag;
GRANT USAGE, CREATE ON SCHEMA rag_app TO lazytt_rag;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA rag_app TO lazytt_rag;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA rag_app TO lazytt_rag;
ALTER DEFAULT PRIVILEGES FOR ROLE lazytt_rag IN SCHEMA rag_app
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO lazytt_rag;
ALTER DEFAULT PRIVILEGES FOR ROLE lazytt_rag IN SCHEMA rag_app
    GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO lazytt_rag;
COMMIT;

\echo '==> [4/6] 转移 rag_flow 内所有对象并授予 Schema CREATE'
\connect rag_flow
BEGIN;
REASSIGN OWNED BY lazytt_rag_owner TO lazytt_rag;
DROP OWNED BY lazytt_rag_owner;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO lazytt_rag;
GRANT USAGE, CREATE ON SCHEMA public TO lazytt_rag;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO lazytt_rag;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO lazytt_rag;
ALTER DEFAULT PRIVILEGES FOR ROLE lazytt_rag IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO lazytt_rag;
ALTER DEFAULT PRIVILEGES FOR ROLE lazytt_rag IN SCHEMA public
    GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO lazytt_rag;
COMMIT;

\echo '==> [5/6] 清理 postgres 数据库依赖并删除旧 NOLOGIN 角色'
\connect postgres
REASSIGN OWNED BY lazytt_rag_owner TO lazytt_rag;
DROP OWNED BY lazytt_rag_owner;
DROP ROLE lazytt_rag_owner;

\echo '==> [6/6] 输出脱敏验收结果'
SELECT rolname, rolcanlogin, rolsuper, rolcreaterole, rolcreatedb, rolreplication,
       rolbypassrls
FROM pg_catalog.pg_roles
WHERE rolname IN ('lazytt_rag', 'lazytt_rag_owner')
ORDER BY rolname;

SELECT datname, pg_catalog.pg_get_userbyid(datdba) AS owner
FROM pg_catalog.pg_database
WHERE datname IN ('rag_app', 'rag_flow')
ORDER BY datname;

\connect rag_app
SELECT
    has_schema_privilege('lazytt_rag', 'rag_app', 'USAGE') AS app_schema_usage,
    has_schema_privilege('lazytt_rag', 'rag_app', 'CREATE') AS app_schema_create,
    pg_get_userbyid(namespace.nspowner) AS app_schema_owner
FROM pg_namespace AS namespace
WHERE namespace.nspname = 'rag_app';

\connect rag_flow
SELECT
    has_schema_privilege('lazytt_rag', 'public', 'USAGE') AS ragflow_schema_usage,
    has_schema_privilege('lazytt_rag', 'public', 'CREATE') AS ragflow_schema_create,
    pg_get_userbyid(namespace.nspowner) AS ragflow_schema_owner
FROM pg_namespace AS namespace
WHERE namespace.nspname = 'public';

\echo 'SUCCESS: 两个逻辑数据库现在只使用 lazytt_rag 作为业务所有者和登录账号。'
