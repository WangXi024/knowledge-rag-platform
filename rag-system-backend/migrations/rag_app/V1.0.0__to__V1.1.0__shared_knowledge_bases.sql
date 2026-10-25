-- LazyTT RAG Gateway PostgreSQL Schema V1.0.0 -> V1.1.0
--
-- V1.1.0 removes personal knowledge-base ownership and introduces two resource
-- scopes: TENANT and SYSTEM. The migration intentionally refuses to translate
-- old resource rows because their former user ownership cannot be inferred as
-- a shared-resource policy. It never deletes data: operators must separately
-- clear the listed business tables after an approved backup/cleanup procedure.

BEGIN;

DO $$
DECLARE
    history_rows integer;
    history_head text;
    current_release text;
    current_checksum text;
BEGIN
    IF current_database() <> 'rag_app' THEN
        RAISE EXCEPTION
            'V1.1.0 migration can only run in rag_app; current database=%',
            current_database();
    END IF;

    SELECT count(*), max(version_num), max(release_version), max(schema_checksum)
    INTO history_rows, history_head, current_release, current_checksum
    FROM rag_app.alembic_version;

    IF history_rows <> 1
       OR history_head <> '0006_document_version_updates'
       OR current_release <> 'V1.0.0'
       OR current_checksum <> '427e6747cf6054d521ad3b292b81a4721612e43494e1560794a821f8bfed83d3'
    THEN
        RAISE EXCEPTION
            'rag_app is not the reviewed V1.0.0 baseline: rows=%, head=%, release=%, checksum=%',
            history_rows, history_head, current_release, current_checksum;
    END IF;

    IF EXISTS (SELECT 1 FROM rag_app.knowledge_base LIMIT 1)
       OR EXISTS (SELECT 1 FROM rag_app.document_record LIMIT 1)
       OR EXISTS (SELECT 1 FROM rag_app.document_version LIMIT 1)
       OR EXISTS (SELECT 1 FROM rag_app.document_index_node LIMIT 1)
       OR EXISTS (SELECT 1 FROM rag_app.document_entity LIMIT 1)
       OR EXISTS (SELECT 1 FROM rag_app.entity_relation LIMIT 1)
       OR EXISTS (SELECT 1 FROM rag_app.ingestion_job LIMIT 1)
       OR EXISTS (SELECT 1 FROM rag_app.query_record LIMIT 1)
       OR EXISTS (SELECT 1 FROM rag_app.citation_record LIMIT 1)
       OR EXISTS (SELECT 1 FROM rag_app.feedback_record LIMIT 1)
       OR EXISTS (SELECT 1 FROM rag_app.idempotency_record LIMIT 1)
    THEN
        RAISE EXCEPTION
            'V1.1.0 requires empty knowledge-resource, job, query, feedback, and idempotency tables; no data was deleted';
    END IF;
END
$$;

ALTER TABLE rag_app.knowledge_base
    ADD COLUMN scope_type character varying(16) NOT NULL,
    ADD CONSTRAINT chk_knowledge_base_scope_type
        CHECK (scope_type IN ('TENANT', 'SYSTEM'));

CREATE INDEX idx_knowledge_base_type_scope_status
    ON rag_app.knowledge_base (scope_type, security_scope_id, status);

ALTER TABLE rag_app.citation_record
    DROP CONSTRAINT fk_citation_document,
    DROP CONSTRAINT fk_citation_knowledge_base,
    ADD COLUMN resource_scope_id character(64) NOT NULL,
    ADD CONSTRAINT chk_citation_resource_scope
        CHECK (resource_scope_id ~ '^[0-9a-f]{64}$'),
    ADD CONSTRAINT fk_citation_document
        FOREIGN KEY (resource_scope_id, document_id)
        REFERENCES rag_app.document_record (security_scope_id, id)
        ON DELETE RESTRICT,
    ADD CONSTRAINT fk_citation_knowledge_base
        FOREIGN KEY (resource_scope_id, knowledge_base_id)
        REFERENCES rag_app.knowledge_base (security_scope_id, id)
        ON DELETE RESTRICT;

DROP INDEX rag_app.idx_citation_scope_document;

CREATE INDEX idx_citation_resource_document
    ON rag_app.citation_record (resource_scope_id, document_id, created_at);

UPDATE rag_app.alembic_version
SET release_version = 'V1.1.0',
    schema_checksum = '6f61654d651c73816a9dd85db3a55bfbd8f5ab5f1296908de60d93aa873f3109',
    applied_at = CURRENT_TIMESTAMP
WHERE version_num = '0006_document_version_updates';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'rag_app'
          AND table_name = 'knowledge_base'
          AND column_name = 'scope_type'
          AND is_nullable = 'NO'
    ) OR NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'rag_app'
          AND table_name = 'citation_record'
          AND column_name = 'resource_scope_id'
          AND is_nullable = 'NO'
    ) THEN
        RAISE EXCEPTION 'V1.1.0 post-migration column validation failed';
    END IF;
END
$$;

COMMIT;
