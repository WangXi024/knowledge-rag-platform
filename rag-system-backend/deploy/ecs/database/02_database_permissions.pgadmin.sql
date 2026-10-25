-- 管理员分别连接 rag_app 和 rag_flow，各执行一次。不会修改其他系统库的 ACL。
-- 迁移前配置库和 public；迁移以 SET ROLE lazytt_rag 执行，表即归业务角色所有。
BEGIN;
DO $$
DECLARE target text := current_database();
BEGIN
    IF target NOT IN ('rag_app', 'rag_flow') THEN RAISE EXCEPTION '目标数据库错误'; END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = target
                   AND pg_get_userbyid(datdba) = 'lazytt_rag') THEN
        RAISE EXCEPTION '数据库归属不符，拒绝接管';
    END IF;
    EXECUTE format('REVOKE CONNECT, TEMPORARY ON DATABASE %I FROM PUBLIC', target);
    EXECUTE format('GRANT CONNECT, TEMPORARY ON DATABASE %I TO lazytt_rag', target);
END
$$;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO lazytt_rag;
GRANT USAGE, CREATE ON SCHEMA public TO lazytt_rag;
-- 本系统采用两库共用业务所有者；RAGFlow 的兼容性 DDL 仍需要对象所有权。
COMMENT ON ROLE lazytt_rag IS 'RAG 两个专用数据库的业务所有者，可在本库执行 DDL；无实例管理权限。';
COMMIT;
