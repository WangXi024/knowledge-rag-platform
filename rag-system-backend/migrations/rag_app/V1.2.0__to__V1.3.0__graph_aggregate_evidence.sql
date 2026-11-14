-- 第二期聚合证据向前升级：只从审核后的V1.2.0追加字段，保留所有原文引用和业务行。
-- 原有查询默认为original且聚合列表为空，不为历史答案生成摘要或改变旧引用语义。
-- 图谱报告与图谱行同事务/CAS发布；资源Scope继续由原组合主键及外键约束隔离。
-- 回退前先关闭社区模式；不删除本期新增字段，灾备恢复使用完整备份。
BEGIN;
SELECT pg_advisory_xact_lock(716281104);
DO $$
BEGIN
    IF current_database() <> 'rag_app' THEN
        RAISE EXCEPTION 'V1.3.0 migration can only run in rag_app';
    END IF;
    IF (SELECT count(*) FROM rag_app.alembic_version) <> 1 OR NOT EXISTS (
        SELECT 1 FROM rag_app.alembic_version
        WHERE version_num = '0007_native_graphrag' AND release_version = 'V1.2.0'
          AND schema_checksum = 'b8b9d47888f009e8b1800eab08092eef3227cbff69b50a81f7a51746e3833c49'
    ) THEN
        RAISE EXCEPTION 'rag_app is not the reviewed V1.2.0 baseline; overwrite refused';
    END IF;
END $$;
ALTER TABLE rag_app.graph_state
    ADD COLUMN community_reports_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    ADD CONSTRAINT chk_graph_community_reports CHECK (
        jsonb_typeof(community_reports_json) = 'array'
        AND jsonb_array_length(community_reports_json) <= 4
        AND octet_length(community_reports_json::text) <= 1048576
    );
ALTER TABLE rag_app.query_record
    ADD COLUMN evidence_mode character varying(16) NOT NULL DEFAULT 'original',
    ADD COLUMN aggregate_profile_ref character varying(128),
    ADD COLUMN aggregate_evidence_json jsonb NOT NULL DEFAULT '[]'::jsonb,
    ADD CONSTRAINT chk_query_evidence_mode CHECK (evidence_mode IN ('original','community')),
    ADD CONSTRAINT chk_query_aggregate_evidence CHECK (
        jsonb_typeof(aggregate_evidence_json) = 'array'
        AND jsonb_array_length(aggregate_evidence_json) <= 4
        AND octet_length(aggregate_evidence_json::text) <= 1048576
    );
COMMENT ON COLUMN rag_app.graph_state.community_reports_json IS
    '本平台基于原生图社区和活动原文生成的报告；与图代数同事务发布，非RAGFlow原生community_report';
COMMENT ON COLUMN rag_app.query_record.evidence_mode IS '本次问答的证据模式：original默认原文，community显式聚合';
COMMENT ON COLUMN rag_app.query_record.aggregate_profile_ref IS '本次聚合编排不可变策略引用；旧请求为空';
COMMENT ON COLUMN rag_app.query_record.aggregate_evidence_json IS
    '仅保存发布前已复验的独立聚合证据及真实多来源快照；不冒充citation_record原文引用';
UPDATE rag_app.alembic_version
SET version_num = '0008_graph_aggregate_evidence', release_version = 'V1.3.0',
    schema_checksum = '08964c430c371b296844af2cd6445c7ff54407904a16b151b65e119f8aae2c74', applied_at = CURRENT_TIMESTAMP;
COMMIT;
