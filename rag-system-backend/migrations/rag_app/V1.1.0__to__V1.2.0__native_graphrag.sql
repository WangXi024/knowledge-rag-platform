-- 原生图谱派生状态升级：从经过评审的 V1.1.0 / 0006 向前推进到 V1.2.0 / 0007。
-- 旧知识库和文档原样保留，不为它们批量创建或启用图谱。已有业务数据允许无损升级。
-- 回退应用代码前应先验证旧代码对新增表和枚举的兼容；本脚本不提供删除状态表的降级。
-- 失败由整个事务回滚；成功后保留新表和旧业务数据，灾备恢复使用事先验证的完整备份。
BEGIN;

SELECT pg_advisory_xact_lock(716281104);

DO $$
DECLARE
    history_rows integer;
    history_head text;
    current_release text;
    current_checksum text;
BEGIN
    IF current_database() <> 'rag_app' THEN
        RAISE EXCEPTION 'V1.2.0 migration can only run in rag_app; current database=%',
            current_database();
    END IF;
    SELECT count(*), max(version_num), max(release_version), max(schema_checksum)
    INTO history_rows, history_head, current_release, current_checksum
    FROM rag_app.alembic_version;
    IF history_rows <> 1 OR history_head <> '0006_document_version_updates'
       OR current_release <> 'V1.1.0'
       OR current_checksum <> '6f61654d651c73816a9dd85db3a55bfbd8f5ab5f1296908de60d93aa873f3109'
    THEN
        RAISE EXCEPTION
            'rag_app is not the reviewed V1.1.0 baseline; automatic overwrite was refused';
    END IF;
END
$$;

-- 资源 Scope 和知识库组成唯一身份，复合外键在数据库层拒绝跨租户引用。
CREATE TABLE rag_app.graph_state (
    security_scope_id character(64) NOT NULL,
    knowledge_base_id uuid NOT NULL,
    enabled boolean DEFAULT false NOT NULL,
    profile_ref character varying(128) NOT NULL,
    auto_build boolean DEFAULT true NOT NULL,
    retrieval_enabled boolean DEFAULT true NOT NULL,
    status character varying(24) DEFAULT 'DISABLED' NOT NULL,
    target_content_revision bigint DEFAULT 0 NOT NULL,
    built_for_revision bigint,
    graph_generation bigint DEFAULT 0 NOT NULL,
    configuration_version bigint DEFAULT 0 NOT NULL,
    row_version bigint DEFAULT 0 NOT NULL,
    engine_task_id character varying(128),
    job_id uuid,
    last_error_code character varying(64),
    last_error_message text,
    queued_at timestamp with time zone,
    started_at timestamp with time zone,
    completed_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT graph_state_pkey PRIMARY KEY (security_scope_id, knowledge_base_id),
    CONSTRAINT fk_graph_state_knowledge_base FOREIGN KEY (security_scope_id, knowledge_base_id)
        REFERENCES rag_app.knowledge_base (security_scope_id, id) ON DELETE CASCADE,
    CONSTRAINT chk_graph_state_scope CHECK (security_scope_id ~ '^[0-9a-f]{64}$'),
    CONSTRAINT chk_graph_state_status CHECK (
        status IN ('DISABLED','NOT_BUILT','QUEUED','BUILDING','READY','STALE','FAILED','CANCELLING')
    ),
    CONSTRAINT chk_graph_state_versions CHECK (
        target_content_revision >= 0 AND graph_generation >= 0
        AND configuration_version >= 0 AND row_version >= 0
        AND (built_for_revision IS NULL OR built_for_revision >= 0)
    ),
    CONSTRAINT chk_graph_state_ready_version CHECK (
        status <> 'READY' OR (built_for_revision IS NOT NULL AND graph_generation > 0)
    )
);

CREATE INDEX idx_graph_state_scope_status
    ON rag_app.graph_state (security_scope_id, status, updated_at);

-- GRAPH 配置与 GRAPH_BUILD Job 是新增类型；旧行均继续满足新约束，不改写业务正文。
ALTER TABLE rag_app.config_profile DROP CONSTRAINT chk_config_profile_type;
ALTER TABLE rag_app.config_profile ADD CONSTRAINT chk_config_profile_type CHECK (
    profile_type IN ('PARSER','CHUNK','EMBEDDING','RETRIEVAL','RERANK','PROMPT','MODEL','CACHE','GRAPH')
);
ALTER TABLE rag_app.ingestion_job DROP CONSTRAINT chk_job_type;
ALTER TABLE rag_app.ingestion_job ADD CONSTRAINT chk_job_type CHECK (
    job_type IN ('UPLOAD','PARSE','REPARSE','UPDATE_DOCUMENT','DELETE_DOCUMENT','DELETE_KB',
                'CLEANUP','GRAPH_BUILD')
);
ALTER TABLE rag_app.ingestion_job ADD CONSTRAINT chk_graph_job_knowledge_base CHECK (
    job_type <> 'GRAPH_BUILD' OR knowledge_base_id IS NOT NULL
);

-- 同一资源知识库只允许一项在途构图，终态记录保留后允许显式重建或重试。
CREATE UNIQUE INDEX uk_graph_job_inflight
    ON rag_app.ingestion_job (security_scope_id, knowledge_base_id)
    WHERE job_type = 'GRAPH_BUILD'
      AND status IN ('PENDING','RUNNING','RETRY_WAIT','CANCEL_REQUESTED');

COMMENT ON TABLE rag_app.graph_state IS
    '知识库原生 GraphRAG 派生状态；不存储图谱生成正文，不改变知识库内容修订号';
COMMENT ON COLUMN rag_app.graph_state.security_scope_id IS '资源安全范围，与知识库组成隔离主键';
COMMENT ON COLUMN rag_app.graph_state.knowledge_base_id IS '所属本地知识库，通过复合外键核验安全范围';
COMMENT ON COLUMN rag_app.graph_state.enabled IS '显式图谱配置开关；新增或既有知识库默认关闭';
COMMENT ON COLUMN rag_app.graph_state.profile_ref IS '不可变的受控图谱方案引用，不含模型凭据';
COMMENT ON COLUMN rag_app.graph_state.auto_build IS '启用图谱后内容改变是否自动排队构建';
COMMENT ON COLUMN rag_app.graph_state.retrieval_enabled IS '图谱就绪时是否允许在线原文导航召回';
COMMENT ON COLUMN rag_app.graph_state.status IS '应用图谱生命周期，不将未确认或过期结果标记为可用';
COMMENT ON COLUMN rag_app.graph_state.target_content_revision IS '当前构图目标的知识库内容修订号';
COMMENT ON COLUMN rag_app.graph_state.built_for_revision IS '最后成功发布图谱使用的内容修订号';
COMMENT ON COLUMN rag_app.graph_state.graph_generation IS '独立图谱发布代数，用于失效旧图谱答案缓存';
COMMENT ON COLUMN rag_app.graph_state.configuration_version IS '图谱配置版本，拒绝迟到构建覆盖新配置';
COMMENT ON COLUMN rag_app.graph_state.row_version IS '应用仓库进行乐观锁更新的行版本';
COMMENT ON COLUMN rag_app.graph_state.job_id IS '应用持久任务的审计指针，不作为授权凭据';
COMMENT ON COLUMN rag_app.graph_state.engine_task_id IS '可恢复的 RAGFlow 原生构图任务标识';
COMMENT ON COLUMN rag_app.graph_state.last_error_code IS '最近一次构建失败的稳定错误代码';
COMMENT ON COLUMN rag_app.graph_state.last_error_message IS '已脱敏的构建错误，不保存模型请求正文';
COMMENT ON COLUMN rag_app.graph_state.queued_at IS '最近一次应用构图任务入队时间';
COMMENT ON COLUMN rag_app.graph_state.started_at IS '最近一次构图执行开始时间';
COMMENT ON COLUMN rag_app.graph_state.completed_at IS '最近一次构图终结时间';
COMMENT ON COLUMN rag_app.graph_state.created_at IS '该知识库显式创建图谱状态的时间';
COMMENT ON COLUMN rag_app.graph_state.updated_at IS '应用最近一次更新图谱状态的时间';

-- 版本摘要按 Schema、发布版本、Revision、排序表名逐行编码并保留末尾换行计算。
UPDATE rag_app.alembic_version
SET version_num = '0007_native_graphrag',
    release_version = 'V1.2.0',
    schema_checksum = 'b8b9d47888f009e8b1800eab08092eef3227cbff69b50a81f7a51746e3833c49',
    applied_at = CURRENT_TIMESTAMP
WHERE version_num = '0006_document_version_updates';

COMMIT;
