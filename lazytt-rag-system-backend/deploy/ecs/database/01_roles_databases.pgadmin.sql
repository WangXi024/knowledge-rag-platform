-- pgAdmin 管理员连接 postgres。A、B、C 必须分别选中执行，不能整份包进事务。
-- A 只创建缺失角色；已有角色属性不同则停止，不改密码。密码通过 pgAdmin 角色属性设置。
DO $$
BEGIN
    IF current_database() <> 'postgres' THEN RAISE EXCEPTION '请连接 postgres'; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lazytt_rag') THEN
        CREATE ROLE lazytt_rag LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
            NOINHERIT NOREPLICATION NOBYPASSRLS;
    ELSIF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'lazytt_rag'
                 AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication
                      OR rolbypassrls OR rolinherit OR NOT rolcanlogin)) THEN
        RAISE EXCEPTION '已有 lazytt_rag 角色属性不符，需人工审阅';
    END IF;
END
$$;
-- 先查询下面结果。数据库存在时核对 owner，不再执行对应 CREATE DATABASE。
SELECT datname, pg_get_userbyid(datdba) AS owner FROM pg_database
WHERE datname IN ('rag_app', 'rag_flow');

-- B：只在 rag_app 不存在时，独立执行这一条。
CREATE DATABASE rag_app OWNER lazytt_rag ENCODING 'UTF8' TEMPLATE template0;

-- C：只在 rag_flow 不存在时，独立执行这一条。
CREATE DATABASE rag_flow OWNER lazytt_rag ENCODING 'UTF8' TEMPLATE template0;
