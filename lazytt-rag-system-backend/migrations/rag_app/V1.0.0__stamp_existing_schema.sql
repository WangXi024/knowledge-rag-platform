
-- LazyTT 自研 RAG Gateway PostgreSQL Schema V1.0.0 现有库基线盖章脚本。
--
-- 使用边界：
-- 1. 只允许 DBA 在现有 rag_app 数据库执行；
-- 2. 不创建业务表，也不修改业务数据；
-- 3. 只有当 13 张基础表和 0006 Alembic 历史头完全匹配时才写入发布标识；
-- 4. RAGFlow 的 rag_flow 数据库不属于本脚本范围。

DO $$
BEGIN
    IF current_database() <> 'rag_app' THEN
        RAISE EXCEPTION
            'V1.0.0 基线盖章只能在 rag_app 执行，当前数据库=%',
            current_database();
    END IF;
END
$$;

BEGIN;

-- 已存在发布字段时只接受完整、正确的 V1.0.0；不能覆盖新版或半成品版本记录。
DO $$
DECLARE columns_count integer; valid_rows integer;
BEGIN
    SELECT count(*) INTO columns_count FROM information_schema.columns
    WHERE table_schema = 'rag_app' AND table_name = 'alembic_version'
      AND column_name IN ('release_version', 'schema_checksum', 'applied_at');
    IF columns_count NOT IN (0, 3) THEN
        RAISE EXCEPTION '版本字段不完整，拒绝登记';
    END IF;
    IF columns_count = 3 THEN
        SELECT count(*) INTO valid_rows FROM rag_app.alembic_version
        WHERE release_version = 'V1.0.0'
          AND schema_checksum = '427e6747cf6054d521ad3b292b81a4721612e43494e1560794a821f8bfed83d3'
          AND applied_at IS NOT NULL;
        IF valid_rows <> 1 THEN
            RAISE EXCEPTION '已有版本不是 V1.0.0，拒绝覆盖';
        END IF;
    END IF;
END
$$;

DO $$
DECLARE
    actual_tables text[];
    expected_tables constant text[] := ARRAY[
        'alembic_version',
        'citation_record',
        'config_profile',
        'document_entity',
        'document_index_node',
        'document_record',
        'document_version',
        'entity_relation',
        'feedback_record',
        'idempotency_record',
        'ingestion_job',
        'knowledge_base',
        'query_record'
    ];
    history_rows integer;
    history_head text;
BEGIN
    SELECT array_agg(table_name ORDER BY table_name)
    INTO actual_tables
    FROM information_schema.tables
    WHERE table_schema = 'rag_app'
      AND table_type = 'BASE TABLE';

    IF actual_tables IS DISTINCT FROM expected_tables THEN
        RAISE EXCEPTION
            'rag_app 表清单不符合 V1.0.0：actual=%, expected=%',
            actual_tables,
            expected_tables;
    END IF;

    SELECT count(*), max(version_num)
    INTO history_rows, history_head
    FROM rag_app.alembic_version;

    IF history_rows <> 1 OR history_head <> '0006_document_version_updates' THEN
        RAISE EXCEPTION
            'rag_app Alembic 历史头不符合 V1.0.0：rows=%, head=%',
            history_rows,
            history_head;
    END IF;
END
$$;

-- 0001～0006 只记录开发期演进；从 V1.0.0 开始，发布号和结构摘要成为 DBA
-- 增量 SQL 的正式基线。IF NOT EXISTS 让同一个合规环境可以安全重复验收。
ALTER TABLE rag_app.alembic_version
    ADD COLUMN IF NOT EXISTS release_version character varying(32),
    ADD COLUMN IF NOT EXISTS schema_checksum character(64),
    ADD COLUMN IF NOT EXISTS applied_at timestamp with time zone
        DEFAULT CURRENT_TIMESTAMP;

UPDATE rag_app.alembic_version
SET release_version = 'V1.0.0',
    schema_checksum = '427e6747cf6054d521ad3b292b81a4721612e43494e1560794a821f8bfed83d3',
    applied_at = COALESCE(applied_at, CURRENT_TIMESTAMP)
WHERE version_num = '0006_document_version_updates';

ALTER TABLE rag_app.alembic_version
    ALTER COLUMN release_version SET NOT NULL,
    ALTER COLUMN schema_checksum SET NOT NULL,
    ALTER COLUMN applied_at SET NOT NULL;

COMMIT;

SELECT version_num, release_version, schema_checksum, applied_at
FROM rag_app.alembic_version;
