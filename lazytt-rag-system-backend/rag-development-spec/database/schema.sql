-- PostgreSQL 18.x 的 RAG 应用参考数据库结构。
-- RAGFlow 使用名为 rag_flow 的独立 PostgreSQL 数据库，严禁把本文件执行到该数据库中。
--
-- security_scope_id（安全作用域标识）只能由服务端身份解析器计算。当前 Direct Identity
-- 模式把请求 Tenant/User 组合成稳定隔离范围：
-- SHA-256(tenant_id + "\x1f" + rag_scope_id)
-- 浏览器不能直接指定最终摘要；两个身份 Header 都不是认证凭据，只允许受控联调。

SET client_encoding = 'UTF8';
SET TIME ZONE 'UTC';

CREATE SCHEMA IF NOT EXISTS rag_app;
SET search_path TO rag_app, public;

CREATE OR REPLACE FUNCTION rag_app.set_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$;

CREATE TABLE config_profile (
    id                  UUID           NOT NULL,
    profile_type        VARCHAR(32)    NOT NULL,
    name                VARCHAR(64)    NOT NULL,
    version             INTEGER        NOT NULL,
    description         VARCHAR(512),
    config_json         JSONB          NOT NULL,
    checksum_sha256     CHAR(64)       NOT NULL,
    enabled             BOOLEAN        NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT pk_config_profile PRIMARY KEY (id),
    CONSTRAINT uk_config_profile_name_version UNIQUE (profile_type, name, version),
    CONSTRAINT chk_config_profile_version CHECK (version > 0),
    CONSTRAINT chk_config_profile_type CHECK (
        profile_type IN ('PARSER','CHUNK','EMBEDDING','RETRIEVAL','RERANK','PROMPT','MODEL','CACHE')
    )
);

CREATE INDEX idx_config_profile_enabled
    ON config_profile (profile_type, enabled);

CREATE TRIGGER trg_config_profile_updated_at
BEFORE UPDATE ON config_profile
FOR EACH ROW EXECUTE FUNCTION rag_app.set_updated_at();

CREATE TABLE knowledge_base (
    id                      UUID           NOT NULL,
    security_scope_id       CHAR(64)       NOT NULL,
    scope_type             VARCHAR(16)    NOT NULL,
    external_id             VARCHAR(128),
    name                    VARCHAR(128)   NOT NULL,
    description             VARCHAR(2048),
    ragflow_dataset_id      VARCHAR(64),
    parser_profile_ref      VARCHAR(128)   NOT NULL,
    chunk_profile_ref       VARCHAR(128)   NOT NULL,
    embedding_profile_ref   VARCHAR(128)   NOT NULL,
    retrieval_profile_ref   VARCHAR(128)   NOT NULL,
    status                  VARCHAR(32)    NOT NULL,
    revision                BIGINT         NOT NULL DEFAULT 0,
    row_version             BIGINT         NOT NULL DEFAULT 0,
    last_error_code         VARCHAR(64),
    last_error_message      VARCHAR(512),
    created_at              TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at              TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    deleted_at              TIMESTAMPTZ(3),
    CONSTRAINT pk_knowledge_base PRIMARY KEY (id),
    CONSTRAINT uk_knowledge_base_scope_id UNIQUE (security_scope_id, id),
    CONSTRAINT uk_knowledge_base_external_id UNIQUE (security_scope_id, external_id),
    CONSTRAINT uk_knowledge_base_ragflow_dataset UNIQUE (ragflow_dataset_id),
    CONSTRAINT chk_knowledge_base_scope CHECK (security_scope_id ~ '^[0-9a-f]{64}$'),
    CONSTRAINT chk_knowledge_base_scope_type CHECK (scope_type IN ('TENANT','SYSTEM')),
    CONSTRAINT chk_knowledge_base_revision CHECK (revision >= 0),
    CONSTRAINT chk_knowledge_base_row_version CHECK (row_version >= 0),
    CONSTRAINT chk_knowledge_base_status CHECK (
        status IN ('CREATING','READY','CREATE_FAILED','DELETING','DELETE_FAILED','DELETED')
    )
);

CREATE INDEX idx_knowledge_base_scope_status
    ON knowledge_base (security_scope_id, status, updated_at);
CREATE INDEX idx_knowledge_base_scope_name
    ON knowledge_base (security_scope_id, name);
CREATE INDEX idx_knowledge_base_type_scope_status
    ON knowledge_base (scope_type, security_scope_id, status);

CREATE TRIGGER trg_knowledge_base_updated_at
BEFORE UPDATE ON knowledge_base
FOR EACH ROW EXECUTE FUNCTION rag_app.set_updated_at();

CREATE TABLE document_record (
    id                      UUID           NOT NULL,
    security_scope_id       CHAR(64)       NOT NULL,
    knowledge_base_id       UUID           NOT NULL,
    external_id             VARCHAR(128),
    ragflow_document_id     VARCHAR(64),
    active_version_id       UUID,
    active_version_number   INTEGER        NOT NULL DEFAULT 1,
    document_revision       BIGINT         NOT NULL DEFAULT 1,
    filename                VARCHAR(255)   NOT NULL,
    safe_filename           VARCHAR(255)   NOT NULL,
    mime_type               VARCHAR(128)   NOT NULL,
    file_size_bytes         BIGINT         NOT NULL,
    content_sha256          CHAR(64)       NOT NULL,
    metadata_json           JSONB,
    parser_profile_ref      VARCHAR(128)   NOT NULL,
    chunk_profile_ref       VARCHAR(128)   NOT NULL,
    status                  VARCHAR(32)    NOT NULL,
    progress                NUMERIC(5,4)   NOT NULL DEFAULT 0,
    chunk_count             INTEGER        NOT NULL DEFAULT 0,
    token_count             BIGINT         NOT NULL DEFAULT 0,
    row_version             BIGINT         NOT NULL DEFAULT 0,
    last_error_code         VARCHAR(64),
    last_error_message      VARCHAR(1024),
    engine_progress_message VARCHAR(2048),
    created_at              TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at              TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    indexed_at              TIMESTAMPTZ(3),
    deleted_at              TIMESTAMPTZ(3),
    CONSTRAINT pk_document_record PRIMARY KEY (id),
    CONSTRAINT uk_document_scope_id UNIQUE (security_scope_id, id),
    CONSTRAINT uk_document_ragflow_id UNIQUE (ragflow_document_id),
    CONSTRAINT fk_document_knowledge_base FOREIGN KEY (security_scope_id, knowledge_base_id)
        REFERENCES knowledge_base(security_scope_id, id) ON DELETE RESTRICT,
    CONSTRAINT chk_document_scope CHECK (security_scope_id ~ '^[0-9a-f]{64}$'),
    CONSTRAINT chk_document_file_size CHECK (file_size_bytes >= 0),
    CONSTRAINT chk_document_progress CHECK (progress >= 0 AND progress <= 1),
    CONSTRAINT chk_document_chunk_count CHECK (chunk_count >= 0),
    CONSTRAINT chk_document_token_count CHECK (token_count >= 0),
    CONSTRAINT chk_document_row_version CHECK (row_version >= 0),
    CONSTRAINT chk_document_active_version_number CHECK (active_version_number > 0),
    CONSTRAINT chk_document_revision CHECK (document_revision > 0),
    CONSTRAINT chk_document_status CHECK (
        status IN ('RECEIVED','UPLOADING','UPLOADED','QUEUED','PARSING','INDEXING','READY',
                   'FAILED','RETRYING','DELETING','DELETE_FAILED','DELETED')
    )
);

CREATE UNIQUE INDEX uk_document_external
    ON document_record (security_scope_id, knowledge_base_id, external_id)
    WHERE external_id IS NOT NULL AND deleted_at IS NULL;
CREATE INDEX idx_document_scope_kb_status
    ON document_record (security_scope_id, knowledge_base_id, status, updated_at);
CREATE INDEX idx_document_scope_hash
    ON document_record (security_scope_id, knowledge_base_id, content_sha256);
CREATE INDEX idx_document_scope_filename
    ON document_record (security_scope_id, knowledge_base_id, filename);
CREATE INDEX idx_document_metadata_gin
    ON document_record USING GIN (metadata_json);

CREATE TRIGGER trg_document_record_updated_at
BEFORE UPDATE ON document_record
FOR EACH ROW EXECUTE FUNCTION rag_app.set_updated_at();

-- 一个逻辑文档可拥有多个独立 RAGFlow 物理版本。新版本解析完成前保持 PENDING /
-- PARSING，只有发布事务能把它切换为 ACTIVE；部分唯一索引保证同一文档永远最多
-- 一个活动版本。历史物理文档的 Chunk 会被停用但暂时保留，以支持显式快速回滚。
CREATE TABLE document_version (
    id                  UUID           NOT NULL,
    security_scope_id   CHAR(64)       NOT NULL,
    knowledge_base_id   UUID           NOT NULL,
    document_id         UUID           NOT NULL,
    version_number      INTEGER        NOT NULL,
    ragflow_document_id VARCHAR(64),
    filename            VARCHAR(255)   NOT NULL,
    safe_filename       VARCHAR(255)   NOT NULL,
    mime_type           VARCHAR(128)   NOT NULL,
    file_size_bytes     BIGINT         NOT NULL,
    content_sha256      CHAR(64)       NOT NULL,
    metadata_json       JSONB          NOT NULL DEFAULT '{}'::jsonb,
    parser_profile_ref  VARCHAR(128)   NOT NULL,
    chunk_profile_ref   VARCHAR(128)   NOT NULL,
    status              VARCHAR(32)    NOT NULL,
    requested_mode      VARCHAR(32)    NOT NULL,
    effective_mode      VARCHAR(32)    NOT NULL,
    chunk_count         INTEGER        NOT NULL DEFAULT 0,
    token_count         BIGINT         NOT NULL DEFAULT 0,
    last_error_code     VARCHAR(64),
    last_error_message  VARCHAR(1024),
    activated_at        TIMESTAMPTZ(3),
    retired_at          TIMESTAMPTZ(3),
    created_at          TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT pk_document_version PRIMARY KEY (id),
    CONSTRAINT uk_document_version_scope_id UNIQUE (security_scope_id, id),
    CONSTRAINT uk_document_version_number UNIQUE (
        security_scope_id, document_id, version_number
    ),
    CONSTRAINT uk_document_version_ragflow_document_id UNIQUE (ragflow_document_id),
    CONSTRAINT fk_document_version_knowledge_base FOREIGN KEY (
        security_scope_id, knowledge_base_id
    ) REFERENCES knowledge_base(security_scope_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_document_version_document FOREIGN KEY (
        security_scope_id, document_id
    ) REFERENCES document_record(security_scope_id, id) ON DELETE CASCADE,
    CONSTRAINT chk_document_version_scope CHECK (security_scope_id ~ '^[0-9a-f]{64}$'),
    CONSTRAINT chk_document_version_number CHECK (version_number > 0),
    CONSTRAINT chk_document_version_file_size CHECK (file_size_bytes >= 0),
    CONSTRAINT chk_document_version_chunk_count CHECK (chunk_count >= 0),
    CONSTRAINT chk_document_version_token_count CHECK (token_count >= 0),
    CONSTRAINT chk_document_version_status CHECK (
        status IN ('PENDING','UPLOADING','PARSING','INDEXING','ACTIVE','RETIRED','FAILED')
    ),
    CONSTRAINT chk_document_version_requested_mode CHECK (
        requested_mode IN ('AUTO','SAFE_REPLACE')
    ),
    CONSTRAINT chk_document_version_effective_mode CHECK (
        effective_mode IN ('AUTO','SAFE_REPLACE')
    )
);

CREATE UNIQUE INDEX uk_document_version_active
    ON document_version (security_scope_id, document_id)
    WHERE status = 'ACTIVE';
CREATE INDEX idx_document_version_scope_document
    ON document_version (security_scope_id, document_id, version_number);
CREATE INDEX idx_document_version_scope_status
    ON document_version (security_scope_id, status, updated_at);

CREATE TRIGGER trg_document_version_updated_at
BEFORE UPDATE ON document_version
FOR EACH ROW EXECUTE FUNCTION rag_app.set_updated_at();

-- Gateway 侧车层级索引：不复制 RAGFlow 的底层索引，只保存 Parent/Summary 文本、
-- FLOAT32 Embedding 字节和可回查的真实源 Chunk ID。删除业务记录时可安全级联删除。
CREATE TABLE document_index_node (
    id                    UUID           NOT NULL,
    security_scope_id     CHAR(64)       NOT NULL,
    knowledge_base_id     UUID           NOT NULL,
    document_id           UUID           NOT NULL,
    node_type             VARCHAR(16)    NOT NULL,
    ordinal               INTEGER        NOT NULL,
    content               TEXT           NOT NULL,
    source_chunk_ids_json JSONB          NOT NULL,
    embedding_bytes       BYTEA,
    embedding_dimension   INTEGER        NOT NULL DEFAULT 0,
    metadata_json         JSONB          NOT NULL DEFAULT '{}'::jsonb,
    index_version         INTEGER        NOT NULL DEFAULT 1,
    created_at            TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at            TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT pk_document_index_node PRIMARY KEY (id),
    CONSTRAINT uk_index_node_scope_id UNIQUE (security_scope_id, id),
    CONSTRAINT uk_index_node_document_ordinal_version UNIQUE (
        security_scope_id, document_id, node_type, ordinal, index_version
    ),
    CONSTRAINT fk_index_node_knowledge_base FOREIGN KEY (
        security_scope_id, knowledge_base_id
    ) REFERENCES knowledge_base(security_scope_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_index_node_document FOREIGN KEY (
        security_scope_id, document_id
    ) REFERENCES document_record(security_scope_id, id) ON DELETE CASCADE,
    CONSTRAINT chk_index_node_scope CHECK (security_scope_id ~ '^[0-9a-f]{64}$'),
    -- SECTION 是文档摘要与 Parent 之间的章节级导航节点；它只保存可回查的真实
    -- RAGFlow Chunk ID，不会成为无法核验的最终引用正文。
    CONSTRAINT chk_index_node_type CHECK (node_type IN ('PARENT','SECTION','SUMMARY')),
    CONSTRAINT chk_index_node_ordinal CHECK (ordinal >= 0),
    CONSTRAINT chk_index_node_embedding_dimension CHECK (embedding_dimension >= 0),
    CONSTRAINT chk_index_node_version CHECK (index_version > 0)
);

CREATE INDEX idx_index_node_scope_kb_type
    ON document_index_node (security_scope_id, knowledge_base_id, node_type);
CREATE INDEX idx_index_node_source_chunks_gin
    ON document_index_node USING GIN (source_chunk_ids_json);

-- 轻量实体索引只保存规范化名称与真实 Chunk 来源；它用于召回导航，不替代全文索引。
CREATE TABLE document_entity (
    id                    UUID           NOT NULL,
    security_scope_id     CHAR(64)       NOT NULL,
    knowledge_base_id     UUID           NOT NULL,
    document_id           UUID           NOT NULL,
    canonical_name        VARCHAR(128)   NOT NULL,
    normalized_name       VARCHAR(128)   NOT NULL,
    entity_type           VARCHAR(32)    NOT NULL,
    aliases_json          JSONB          NOT NULL,
    source_chunk_ids_json JSONB          NOT NULL,
    mention_count         INTEGER        NOT NULL DEFAULT 1,
    metadata_json         JSONB          NOT NULL DEFAULT '{}'::jsonb,
    index_version         INTEGER        NOT NULL DEFAULT 1,
    created_at            TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at            TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT pk_document_entity PRIMARY KEY (id),
    CONSTRAINT uk_document_entity_scope_id UNIQUE (security_scope_id, id),
    CONSTRAINT uk_document_entity_name_version UNIQUE (
        security_scope_id, document_id, normalized_name, index_version
    ),
    CONSTRAINT fk_document_entity_knowledge_base FOREIGN KEY (
        security_scope_id, knowledge_base_id
    ) REFERENCES knowledge_base(security_scope_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_document_entity_document FOREIGN KEY (
        security_scope_id, document_id
    ) REFERENCES document_record(security_scope_id, id) ON DELETE CASCADE,
    CONSTRAINT chk_document_entity_scope CHECK (security_scope_id ~ '^[0-9a-f]{64}$'),
    CONSTRAINT chk_document_entity_mentions CHECK (mention_count > 0),
    CONSTRAINT chk_document_entity_version CHECK (index_version > 0)
);

CREATE INDEX idx_document_entity_scope_kb_name
    ON document_entity (security_scope_id, knowledge_base_id, normalized_name);
CREATE INDEX idx_document_entity_source_chunks_gin
    ON document_entity USING GIN (source_chunk_ids_json);

-- 轻量关系图只接收正文明确出现的主体—谓词—客体，并保留可核验源 Chunk。
CREATE TABLE entity_relation (
    id                    UUID           NOT NULL,
    security_scope_id     CHAR(64)       NOT NULL,
    knowledge_base_id     UUID           NOT NULL,
    document_id           UUID           NOT NULL,
    subject_name          VARCHAR(128)   NOT NULL,
    subject_normalized    VARCHAR(128)   NOT NULL,
    predicate             VARCHAR(64)    NOT NULL,
    object_name           VARCHAR(128)   NOT NULL,
    object_normalized     VARCHAR(128)   NOT NULL,
    source_chunk_ids_json JSONB          NOT NULL,
    confidence            NUMERIC(5,4)   NOT NULL DEFAULT 1,
    metadata_json         JSONB          NOT NULL DEFAULT '{}'::jsonb,
    index_version         INTEGER        NOT NULL DEFAULT 1,
    created_at            TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at            TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT pk_entity_relation PRIMARY KEY (id),
    CONSTRAINT uk_entity_relation_scope_id UNIQUE (security_scope_id, id),
    CONSTRAINT uk_entity_relation_triple_version UNIQUE (
        security_scope_id, document_id, subject_normalized, predicate,
        object_normalized, index_version
    ),
    CONSTRAINT fk_entity_relation_knowledge_base FOREIGN KEY (
        security_scope_id, knowledge_base_id
    ) REFERENCES knowledge_base(security_scope_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_entity_relation_document FOREIGN KEY (
        security_scope_id, document_id
    ) REFERENCES document_record(security_scope_id, id) ON DELETE CASCADE,
    CONSTRAINT chk_entity_relation_scope CHECK (security_scope_id ~ '^[0-9a-f]{64}$'),
    CONSTRAINT chk_relation_confidence CHECK (confidence >= 0 AND confidence <= 1),
    CONSTRAINT chk_entity_relation_version CHECK (index_version > 0)
);

CREATE INDEX idx_entity_relation_scope_kb_subject
    ON entity_relation (security_scope_id, knowledge_base_id, subject_normalized);
CREATE INDEX idx_entity_relation_scope_kb_object
    ON entity_relation (security_scope_id, knowledge_base_id, object_normalized);
CREATE INDEX idx_entity_relation_source_chunks_gin
    ON entity_relation USING GIN (source_chunk_ids_json);

CREATE TABLE ingestion_job (
    id                  UUID           NOT NULL,
    security_scope_id   CHAR(64)       NOT NULL,
    job_type            VARCHAR(32)    NOT NULL,
    status              VARCHAR(32)    NOT NULL,
    knowledge_base_id   UUID,
    document_id         UUID,
    progress            NUMERIC(5,4)   NOT NULL DEFAULT 0,
    attempt             INTEGER        NOT NULL DEFAULT 0,
    max_attempts        INTEGER        NOT NULL DEFAULT 2,
    payload_json        JSONB,
    result_json         JSONB,
    error_code          VARCHAR(64),
    error_message       VARCHAR(1024),
    next_retry_at       TIMESTAMPTZ(3),
    locked_by           VARCHAR(128),
    locked_until        TIMESTAMPTZ(3),
    created_at          TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at          TIMESTAMPTZ(3),
    finished_at         TIMESTAMPTZ(3),
    updated_at          TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT pk_ingestion_job PRIMARY KEY (id),
    CONSTRAINT uk_ingestion_job_scope_id UNIQUE (security_scope_id, id),
    CONSTRAINT fk_job_knowledge_base FOREIGN KEY (security_scope_id, knowledge_base_id)
        REFERENCES knowledge_base(security_scope_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_job_document FOREIGN KEY (security_scope_id, document_id)
        REFERENCES document_record(security_scope_id, id) ON DELETE RESTRICT,
    CONSTRAINT chk_job_scope CHECK (security_scope_id ~ '^[0-9a-f]{64}$'),
    CONSTRAINT chk_job_progress CHECK (progress >= 0 AND progress <= 1),
    CONSTRAINT chk_job_attempt CHECK (attempt >= 0 AND max_attempts >= 0),
    CONSTRAINT chk_job_type CHECK (
        job_type IN ('UPLOAD','PARSE','REPARSE','UPDATE_DOCUMENT',
                     'DELETE_DOCUMENT','DELETE_KB','CLEANUP')
    ),
    CONSTRAINT chk_job_status CHECK (
        status IN ('PENDING','RUNNING','RETRY_WAIT','SUCCEEDED','FAILED','CANCEL_REQUESTED','CANCELLED')
    )
);

CREATE INDEX idx_job_scope_claim
    ON ingestion_job (security_scope_id, status, next_retry_at, locked_until, created_at);
CREATE INDEX idx_job_scope_document
    ON ingestion_job (security_scope_id, document_id, created_at);
CREATE INDEX idx_job_scope_kb
    ON ingestion_job (security_scope_id, knowledge_base_id, created_at);

CREATE TRIGGER trg_ingestion_job_updated_at
BEFORE UPDATE ON ingestion_job
FOR EACH ROW EXECUTE FUNCTION rag_app.set_updated_at();

CREATE TABLE idempotency_record (
    security_scope_id   CHAR(64)       NOT NULL,
    idempotency_key     VARCHAR(128)   NOT NULL,
    http_method         VARCHAR(8)     NOT NULL,
    request_path        VARCHAR(255)   NOT NULL,
    request_sha256      CHAR(64)       NOT NULL,
    processing_status   VARCHAR(16)    NOT NULL,
    response_status     SMALLINT,
    response_json       JSONB,
    resource_id         UUID,
    created_at          TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at          TIMESTAMPTZ(3) NOT NULL,
    CONSTRAINT pk_idempotency_record
        PRIMARY KEY (security_scope_id, idempotency_key, http_method, request_path),
    CONSTRAINT chk_idempotency_scope CHECK (security_scope_id ~ '^[0-9a-f]{64}$'),
    CONSTRAINT chk_idempotency_http_status CHECK (
        response_status IS NULL OR response_status BETWEEN 100 AND 599
    ),
    CONSTRAINT chk_idempotency_expiry CHECK (expires_at > created_at),
    CONSTRAINT chk_idempotency_status CHECK (
        processing_status IN ('PROCESSING','COMPLETED','FAILED')
    )
);

CREATE INDEX idx_idempotency_scope_expiry
    ON idempotency_record (security_scope_id, expires_at);

CREATE TABLE query_record (
    request_id                  UUID           NOT NULL,
    security_scope_id           CHAR(64)       NOT NULL,
    request_status              VARCHAR(32)    NOT NULL,
    outcome                     VARCHAR(32),
    query_sha256                CHAR(64)       NOT NULL,
    query_redacted              TEXT,
    effective_query_redacted    TEXT,
    knowledge_base_ids_json     JSONB          NOT NULL,
    retrieval_profile_ref       VARCHAR(128)   NOT NULL,
    prompt_profile_ref          VARCHAR(128),
    model_profile_ref           VARCHAR(128),
    cache_status                VARCHAR(32)    NOT NULL DEFAULT 'BYPASS',
    candidate_count             INTEGER        NOT NULL DEFAULT 0,
    context_count               INTEGER        NOT NULL DEFAULT 0,
    answer_text                 TEXT,
    timings_json                JSONB,
    warnings_json               JSONB,
    prompt_tokens               INTEGER,
    completion_tokens           INTEGER,
    total_tokens                INTEGER,
    error_code                  VARCHAR(64),
    created_at                  TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at                TIMESTAMPTZ(3),
    expires_at                  TIMESTAMPTZ(3),
    CONSTRAINT pk_query_record PRIMARY KEY (request_id),
    CONSTRAINT uk_query_record_scope_id UNIQUE (security_scope_id, request_id),
    CONSTRAINT chk_query_scope CHECK (security_scope_id ~ '^[0-9a-f]{64}$'),
    CONSTRAINT chk_query_counts CHECK (candidate_count >= 0 AND context_count >= 0),
    CONSTRAINT chk_query_token_counts CHECK (
        (prompt_tokens IS NULL OR prompt_tokens >= 0)
        AND (completion_tokens IS NULL OR completion_tokens >= 0)
        AND (total_tokens IS NULL OR total_tokens >= 0)
    ),
    CONSTRAINT chk_query_status CHECK (
        request_status IN ('ACCEPTED','RUNNING','COMPLETED','FAILED','CANCELLED')
    ),
    CONSTRAINT chk_query_outcome CHECK (
        outcome IS NULL OR outcome IN ('success','no_evidence','refused','cancelled')
    ),
    CONSTRAINT chk_query_cache CHECK (
        cache_status IN ('BYPASS','MISS','EXACT_HIT','SEMANTIC_HIT','WRITE_FAILED')
    )
);

CREATE INDEX idx_query_scope_created
    ON query_record (security_scope_id, created_at);
CREATE INDEX idx_query_scope_outcome
    ON query_record (security_scope_id, outcome, created_at);
CREATE INDEX idx_query_expiry
    ON query_record (expires_at);
CREATE INDEX idx_query_kb_ids_gin
    ON query_record USING GIN (knowledge_base_ids_json);

CREATE TABLE citation_record (
    id                  UUID           NOT NULL,
    security_scope_id   CHAR(64)       NOT NULL,
    resource_scope_id   CHAR(64)       NOT NULL,
    request_id          UUID           NOT NULL,
    ordinal             INTEGER        NOT NULL,
    label               VARCHAR(16)    NOT NULL,
    knowledge_base_id   UUID           NOT NULL,
    document_id         UUID           NOT NULL,
    engine_chunk_id     VARCHAR(128)   NOT NULL,
    document_name       VARCHAR(255)   NOT NULL,
    heading_path_json   JSONB,
    page_start          INTEGER,
    page_end            INTEGER,
    positions_json      JSONB,
    quote_text          TEXT           NOT NULL,
    retrieval_score     NUMERIC(10,8),
    rerank_score        NUMERIC(10,8),
    verification_status VARCHAR(32)    NOT NULL,
    created_at          TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT pk_citation_record PRIMARY KEY (id),
    CONSTRAINT uk_citation_scope_id UNIQUE (security_scope_id, id),
    CONSTRAINT uk_citation_request_label UNIQUE (security_scope_id, request_id, label),
    CONSTRAINT fk_citation_query FOREIGN KEY (security_scope_id, request_id)
        REFERENCES query_record(security_scope_id, request_id) ON DELETE CASCADE,
    CONSTRAINT fk_citation_knowledge_base FOREIGN KEY (resource_scope_id, knowledge_base_id)
        REFERENCES knowledge_base(security_scope_id, id) ON DELETE RESTRICT,
    CONSTRAINT fk_citation_document FOREIGN KEY (resource_scope_id, document_id)
        REFERENCES document_record(security_scope_id, id) ON DELETE RESTRICT,
    CONSTRAINT chk_citation_scope CHECK (security_scope_id ~ '^[0-9a-f]{64}$'),
    CONSTRAINT chk_citation_resource_scope CHECK (resource_scope_id ~ '^[0-9a-f]{64}$'),
    CONSTRAINT chk_citation_ordinal CHECK (ordinal >= 0),
    CONSTRAINT chk_citation_pages CHECK (
        (page_start IS NULL OR page_start >= 0)
        AND (page_end IS NULL OR page_end >= 0)
    ),
    CONSTRAINT chk_citation_status CHECK (
        verification_status IN ('VALID','INVALID','UNVERIFIED')
    )
);

CREATE INDEX idx_citation_resource_document
    ON citation_record (resource_scope_id, document_id, created_at);

CREATE TABLE feedback_record (
    id                  UUID           NOT NULL,
    security_scope_id   CHAR(64)       NOT NULL,
    request_id          UUID           NOT NULL,
    rating              SMALLINT,
    feedback_type       VARCHAR(32)    NOT NULL,
    comment_text        TEXT,
    corrected_answer    TEXT,
    created_at          TIMESTAMPTZ(3) NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT pk_feedback_record PRIMARY KEY (id),
    CONSTRAINT uk_feedback_scope_id UNIQUE (security_scope_id, id),
    CONSTRAINT fk_feedback_query FOREIGN KEY (security_scope_id, request_id)
        REFERENCES query_record(security_scope_id, request_id) ON DELETE CASCADE,
    CONSTRAINT chk_feedback_scope CHECK (security_scope_id ~ '^[0-9a-f]{64}$'),
    CONSTRAINT chk_feedback_rating CHECK (rating IS NULL OR rating IN (-1, 1)),
    CONSTRAINT chk_feedback_type CHECK (
        feedback_type IN ('helpful','not_helpful','answer_incorrect','citation_incorrect',
                          'should_answer','should_refuse','other')
    )
);

CREATE INDEX idx_feedback_scope_request
    ON feedback_record (security_scope_id, request_id, created_at);
CREATE INDEX idx_feedback_scope_type
    ON feedback_record (security_scope_id, feedback_type, created_at);
