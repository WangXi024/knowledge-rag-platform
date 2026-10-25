-- 管理员连接 postgres 执行。只读检查本系统两库权限以及访问其他非模板库的授权。
-- postgres 管理库和本系统两库除外；发现额外 CONNECT 仅报错，不修改共享 ACL。
BEGIN READ ONLY;
DO $$ BEGIN
    IF NOT has_database_privilege('lazytt_rag','rag_app','CONNECT')
       OR NOT has_database_privilege('lazytt_rag','rag_flow','CONNECT') THEN
        RAISE EXCEPTION '本系统数据库连接授权缺失'; END IF;
    IF EXISTS (SELECT 1 FROM pg_database WHERE NOT datistemplate AND datallowconn
      AND datname NOT IN ('postgres','rag_app','rag_flow')
      AND has_database_privilege('lazytt_rag',oid,'CONNECT')) THEN
        RAISE EXCEPTION '仍可连接其他系统数据库，需管理员单独审阅共享 CONNECT ACL'; END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='lazytt_rag'
      AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)) THEN
        RAISE EXCEPTION '业务账号持有实例管理权限'; END IF;
END $$;
COMMIT;
SELECT 'database access verified' AS result;
