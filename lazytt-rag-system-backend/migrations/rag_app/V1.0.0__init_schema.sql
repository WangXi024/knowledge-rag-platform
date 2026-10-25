-- rag_app V1.0.0 冻结结构基线，来自改造前已审阅的结构快照。
-- 只在已创建的空数据库中以对象所有者执行；账号、授权和注释由 deploy/ecs/database 管理。
-- 不写入账号、模型、知识库或文档；失败回滚本文件全部结构，禁止删库重装。
-- rag_app 的 alembic_version 名称仅为兼容已有数据库，不再依赖 Python/Alembic 迁移。
BEGIN;
SET LOCAL lock_timeout = '10s';
DO $$
BEGIN
    IF current_database() <> 'rag_app' THEN
        RAISE EXCEPTION '初始化连接的数据库不正确，应为 rag_app';
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_schema NOT IN ('pg_catalog', 'information_schema')) THEN
        RAISE EXCEPTION '数据库不是空库，拒绝覆盖现有结构';
    END IF;
END
$$;

CREATE SCHEMA rag_app;


--
-- Name: set_updated_at(); Type: FUNCTION; Schema: rag_app; Owner: -
--

CREATE FUNCTION rag_app.set_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            NEW.updated_at = CURRENT_TIMESTAMP;
            RETURN NEW;
        END;
        $$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: alembic_version; Type: TABLE; Schema: rag_app; Owner: -
--

CREATE TABLE rag_app.alembic_version (
    version_num character varying(32) NOT NULL,
    release_version character varying(32) NOT NULL,
    schema_checksum character(64) NOT NULL,
    applied_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL
);


--
-- Name: citation_record; Type: TABLE; Schema: rag_app; Owner: -
--

CREATE TABLE rag_app.citation_record (
    id uuid NOT NULL,
    security_scope_id character(64) NOT NULL,
    request_id uuid NOT NULL,
    ordinal integer NOT NULL,
    label character varying(16) NOT NULL,
    knowledge_base_id uuid NOT NULL,
    document_id uuid NOT NULL,
    engine_chunk_id character varying(128) NOT NULL,
    document_name character varying(255) NOT NULL,
    heading_path_json jsonb,
    page_start integer,
    page_end integer,
    positions_json jsonb,
    quote_text text NOT NULL,
    retrieval_score numeric(10,8),
    rerank_score numeric(10,8),
    verification_status character varying(32) NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT chk_citation_ordinal CHECK ((ordinal >= 0)),
    CONSTRAINT chk_citation_pages CHECK ((((page_start IS NULL) OR (page_start >= 0)) AND ((page_end IS NULL) OR (page_end >= 0)))),
    CONSTRAINT chk_citation_scope CHECK ((security_scope_id ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT chk_citation_status CHECK (((verification_status)::text = ANY ((ARRAY['VALID'::character varying, 'INVALID'::character varying, 'UNVERIFIED'::character varying])::text[])))
);


--
-- Name: config_profile; Type: TABLE; Schema: rag_app; Owner: -
--

CREATE TABLE rag_app.config_profile (
    id uuid NOT NULL,
    profile_type character varying(32) NOT NULL,
    name character varying(64) NOT NULL,
    version integer NOT NULL,
    description character varying(512),
    config_json jsonb NOT NULL,
    checksum_sha256 character(64) NOT NULL,
    enabled boolean DEFAULT true NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT chk_config_profile_type CHECK (((profile_type)::text = ANY ((ARRAY['PARSER'::character varying, 'CHUNK'::character varying, 'EMBEDDING'::character varying, 'RETRIEVAL'::character varying, 'RERANK'::character varying, 'PROMPT'::character varying, 'MODEL'::character varying, 'CACHE'::character varying])::text[]))),
    CONSTRAINT chk_config_profile_version CHECK ((version > 0))
);


--
-- Name: document_entity; Type: TABLE; Schema: rag_app; Owner: -
--

CREATE TABLE rag_app.document_entity (
    id uuid NOT NULL,
    security_scope_id character(64) NOT NULL,
    knowledge_base_id uuid NOT NULL,
    document_id uuid NOT NULL,
    canonical_name character varying(128) NOT NULL,
    normalized_name character varying(128) NOT NULL,
    entity_type character varying(32) NOT NULL,
    aliases_json jsonb NOT NULL,
    source_chunk_ids_json jsonb NOT NULL,
    mention_count integer DEFAULT 1 NOT NULL,
    metadata_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    index_version integer DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT chk_document_entity_mentions CHECK ((mention_count > 0)),
    CONSTRAINT chk_document_entity_scope CHECK ((security_scope_id ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT chk_document_entity_version CHECK ((index_version > 0))
);


--
-- Name: document_index_node; Type: TABLE; Schema: rag_app; Owner: -
--

CREATE TABLE rag_app.document_index_node (
    id uuid NOT NULL,
    security_scope_id character(64) NOT NULL,
    knowledge_base_id uuid NOT NULL,
    document_id uuid NOT NULL,
    node_type character varying(16) NOT NULL,
    ordinal integer NOT NULL,
    content text NOT NULL,
    source_chunk_ids_json jsonb NOT NULL,
    embedding_bytes bytea,
    embedding_dimension integer DEFAULT 0 NOT NULL,
    metadata_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    index_version integer DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT chk_index_node_embedding_dimension CHECK ((embedding_dimension >= 0)),
    CONSTRAINT chk_index_node_ordinal CHECK ((ordinal >= 0)),
    CONSTRAINT chk_index_node_scope CHECK ((security_scope_id ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT chk_index_node_type CHECK (((node_type)::text = ANY ((ARRAY['PARENT'::character varying, 'SECTION'::character varying, 'SUMMARY'::character varying])::text[]))),
    CONSTRAINT chk_index_node_version CHECK ((index_version > 0))
);


--
-- Name: document_record; Type: TABLE; Schema: rag_app; Owner: -
--

CREATE TABLE rag_app.document_record (
    id uuid NOT NULL,
    security_scope_id character(64) NOT NULL,
    knowledge_base_id uuid NOT NULL,
    external_id character varying(128),
    ragflow_document_id character varying(64),
    active_version_id uuid,
    active_version_number integer DEFAULT 1 NOT NULL,
    document_revision bigint DEFAULT '1'::bigint NOT NULL,
    filename character varying(255) NOT NULL,
    safe_filename character varying(255) NOT NULL,
    mime_type character varying(128) NOT NULL,
    file_size_bytes bigint NOT NULL,
    content_sha256 character(64) NOT NULL,
    metadata_json jsonb,
    parser_profile_ref character varying(128) NOT NULL,
    chunk_profile_ref character varying(128) NOT NULL,
    status character varying(32) NOT NULL,
    progress numeric(5,4) DEFAULT '0'::numeric NOT NULL,
    chunk_count integer DEFAULT 0 NOT NULL,
    token_count bigint DEFAULT '0'::bigint NOT NULL,
    row_version bigint DEFAULT '0'::bigint NOT NULL,
    last_error_code character varying(64),
    last_error_message character varying(1024),
    engine_progress_message character varying(2048),
    indexed_at timestamp with time zone,
    deleted_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT chk_document_active_version_number CHECK ((active_version_number > 0)),
    CONSTRAINT chk_document_chunk_count CHECK ((chunk_count >= 0)),
    CONSTRAINT chk_document_file_size CHECK ((file_size_bytes >= 0)),
    CONSTRAINT chk_document_progress CHECK (((progress >= (0)::numeric) AND (progress <= (1)::numeric))),
    CONSTRAINT chk_document_revision CHECK ((document_revision > 0)),
    CONSTRAINT chk_document_row_version CHECK ((row_version >= 0)),
    CONSTRAINT chk_document_scope CHECK ((security_scope_id ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT chk_document_status CHECK (((status)::text = ANY ((ARRAY['RECEIVED'::character varying, 'UPLOADING'::character varying, 'UPLOADED'::character varying, 'QUEUED'::character varying, 'PARSING'::character varying, 'INDEXING'::character varying, 'READY'::character varying, 'FAILED'::character varying, 'RETRYING'::character varying, 'DELETING'::character varying, 'DELETE_FAILED'::character varying, 'DELETED'::character varying])::text[]))),
    CONSTRAINT chk_document_token_count CHECK ((token_count >= 0))
);


--
-- Name: document_version; Type: TABLE; Schema: rag_app; Owner: -
--

CREATE TABLE rag_app.document_version (
    id uuid NOT NULL,
    security_scope_id character(64) NOT NULL,
    knowledge_base_id uuid NOT NULL,
    document_id uuid NOT NULL,
    version_number integer NOT NULL,
    ragflow_document_id character varying(64),
    filename character varying(255) NOT NULL,
    safe_filename character varying(255) NOT NULL,
    mime_type character varying(128) NOT NULL,
    file_size_bytes bigint NOT NULL,
    content_sha256 character(64) NOT NULL,
    metadata_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    parser_profile_ref character varying(128) NOT NULL,
    chunk_profile_ref character varying(128) NOT NULL,
    status character varying(32) NOT NULL,
    requested_mode character varying(32) NOT NULL,
    effective_mode character varying(32) NOT NULL,
    chunk_count integer DEFAULT 0 NOT NULL,
    token_count bigint DEFAULT '0'::bigint NOT NULL,
    last_error_code character varying(64),
    last_error_message character varying(1024),
    activated_at timestamp with time zone,
    retired_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT chk_document_version_chunk_count CHECK ((chunk_count >= 0)),
    CONSTRAINT chk_document_version_effective_mode CHECK (((effective_mode)::text = ANY ((ARRAY['AUTO'::character varying, 'SAFE_REPLACE'::character varying])::text[]))),
    CONSTRAINT chk_document_version_file_size CHECK ((file_size_bytes >= 0)),
    CONSTRAINT chk_document_version_number CHECK ((version_number > 0)),
    CONSTRAINT chk_document_version_requested_mode CHECK (((requested_mode)::text = ANY ((ARRAY['AUTO'::character varying, 'SAFE_REPLACE'::character varying])::text[]))),
    CONSTRAINT chk_document_version_scope CHECK ((security_scope_id ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT chk_document_version_status CHECK (((status)::text = ANY ((ARRAY['PENDING'::character varying, 'UPLOADING'::character varying, 'PARSING'::character varying, 'INDEXING'::character varying, 'ACTIVE'::character varying, 'RETIRED'::character varying, 'FAILED'::character varying])::text[]))),
    CONSTRAINT chk_document_version_token_count CHECK ((token_count >= 0))
);


--
-- Name: entity_relation; Type: TABLE; Schema: rag_app; Owner: -
--

CREATE TABLE rag_app.entity_relation (
    id uuid NOT NULL,
    security_scope_id character(64) NOT NULL,
    knowledge_base_id uuid NOT NULL,
    document_id uuid NOT NULL,
    subject_name character varying(128) NOT NULL,
    subject_normalized character varying(128) NOT NULL,
    predicate character varying(64) NOT NULL,
    object_name character varying(128) NOT NULL,
    object_normalized character varying(128) NOT NULL,
    source_chunk_ids_json jsonb NOT NULL,
    confidence numeric(5,4) DEFAULT '1'::numeric NOT NULL,
    metadata_json jsonb DEFAULT '{}'::jsonb NOT NULL,
    index_version integer DEFAULT 1 NOT NULL,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT chk_entity_relation_scope CHECK ((security_scope_id ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT chk_entity_relation_version CHECK ((index_version > 0)),
    CONSTRAINT chk_relation_confidence CHECK (((confidence >= (0)::numeric) AND (confidence <= (1)::numeric)))
);


--
-- Name: feedback_record; Type: TABLE; Schema: rag_app; Owner: -
--

CREATE TABLE rag_app.feedback_record (
    id uuid NOT NULL,
    security_scope_id character(64) NOT NULL,
    request_id uuid NOT NULL,
    rating smallint,
    feedback_type character varying(32) NOT NULL,
    comment_text text,
    corrected_answer text,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT chk_feedback_rating CHECK (((rating IS NULL) OR (rating = ANY (ARRAY['-1'::integer, 1])))),
    CONSTRAINT chk_feedback_scope CHECK ((security_scope_id ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT chk_feedback_type CHECK (((feedback_type)::text = ANY ((ARRAY['helpful'::character varying, 'not_helpful'::character varying, 'answer_incorrect'::character varying, 'citation_incorrect'::character varying, 'should_answer'::character varying, 'should_refuse'::character varying, 'other'::character varying])::text[])))
);


--
-- Name: idempotency_record; Type: TABLE; Schema: rag_app; Owner: -
--

CREATE TABLE rag_app.idempotency_record (
    security_scope_id character(64) NOT NULL,
    idempotency_key character varying(128) NOT NULL,
    http_method character varying(8) NOT NULL,
    request_path character varying(255) NOT NULL,
    request_sha256 character(64) NOT NULL,
    processing_status character varying(16) NOT NULL,
    response_status smallint,
    response_json jsonb,
    resource_id uuid,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    CONSTRAINT chk_idempotency_expiry CHECK ((expires_at > created_at)),
    CONSTRAINT chk_idempotency_http_status CHECK (((response_status IS NULL) OR ((response_status >= 100) AND (response_status <= 599)))),
    CONSTRAINT chk_idempotency_scope CHECK ((security_scope_id ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT chk_idempotency_status CHECK (((processing_status)::text = ANY ((ARRAY['PROCESSING'::character varying, 'COMPLETED'::character varying, 'FAILED'::character varying])::text[])))
);


--
-- Name: ingestion_job; Type: TABLE; Schema: rag_app; Owner: -
--

CREATE TABLE rag_app.ingestion_job (
    id uuid NOT NULL,
    security_scope_id character(64) NOT NULL,
    job_type character varying(32) NOT NULL,
    status character varying(32) NOT NULL,
    knowledge_base_id uuid,
    document_id uuid,
    progress numeric(5,4) DEFAULT '0'::numeric NOT NULL,
    attempt integer DEFAULT 0 NOT NULL,
    max_attempts integer DEFAULT 2 NOT NULL,
    payload_json jsonb,
    result_json jsonb,
    error_code character varying(64),
    error_message character varying(1024),
    next_retry_at timestamp with time zone,
    locked_by character varying(128),
    locked_until timestamp with time zone,
    started_at timestamp with time zone,
    finished_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT chk_job_attempt CHECK (((attempt >= 0) AND (max_attempts >= 0))),
    CONSTRAINT chk_job_progress CHECK (((progress >= (0)::numeric) AND (progress <= (1)::numeric))),
    CONSTRAINT chk_job_scope CHECK ((security_scope_id ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT chk_job_status CHECK (((status)::text = ANY ((ARRAY['PENDING'::character varying, 'RUNNING'::character varying, 'RETRY_WAIT'::character varying, 'SUCCEEDED'::character varying, 'FAILED'::character varying, 'CANCEL_REQUESTED'::character varying, 'CANCELLED'::character varying])::text[]))),
    CONSTRAINT chk_job_type CHECK (((job_type)::text = ANY ((ARRAY['UPLOAD'::character varying, 'PARSE'::character varying, 'REPARSE'::character varying, 'UPDATE_DOCUMENT'::character varying, 'DELETE_DOCUMENT'::character varying, 'DELETE_KB'::character varying, 'CLEANUP'::character varying])::text[])))
);


--
-- Name: knowledge_base; Type: TABLE; Schema: rag_app; Owner: -
--

CREATE TABLE rag_app.knowledge_base (
    id uuid NOT NULL,
    security_scope_id character(64) NOT NULL,
    external_id character varying(128),
    name character varying(128) NOT NULL,
    description character varying(2048),
    ragflow_dataset_id character varying(64),
    parser_profile_ref character varying(128) NOT NULL,
    chunk_profile_ref character varying(128) NOT NULL,
    embedding_profile_ref character varying(128) NOT NULL,
    retrieval_profile_ref character varying(128) NOT NULL,
    status character varying(32) NOT NULL,
    revision bigint DEFAULT '0'::bigint NOT NULL,
    row_version bigint DEFAULT '0'::bigint NOT NULL,
    last_error_code character varying(64),
    last_error_message character varying(512),
    deleted_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    updated_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT chk_knowledge_base_revision CHECK ((revision >= 0)),
    CONSTRAINT chk_knowledge_base_row_version CHECK ((row_version >= 0)),
    CONSTRAINT chk_knowledge_base_scope CHECK ((security_scope_id ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT chk_knowledge_base_status CHECK (((status)::text = ANY ((ARRAY['CREATING'::character varying, 'READY'::character varying, 'CREATE_FAILED'::character varying, 'DELETING'::character varying, 'DELETE_FAILED'::character varying, 'DELETED'::character varying])::text[])))
);


--
-- Name: query_record; Type: TABLE; Schema: rag_app; Owner: -
--

CREATE TABLE rag_app.query_record (
    request_id uuid NOT NULL,
    security_scope_id character(64) NOT NULL,
    request_status character varying(32) NOT NULL,
    outcome character varying(32),
    query_sha256 character(64) NOT NULL,
    query_redacted text,
    effective_query_redacted text,
    knowledge_base_ids_json jsonb NOT NULL,
    retrieval_profile_ref character varying(128) NOT NULL,
    prompt_profile_ref character varying(128),
    model_profile_ref character varying(128),
    cache_status character varying(32) DEFAULT 'BYPASS'::character varying NOT NULL,
    candidate_count integer DEFAULT 0 NOT NULL,
    context_count integer DEFAULT 0 NOT NULL,
    answer_text text,
    timings_json jsonb,
    warnings_json jsonb,
    prompt_tokens integer,
    completion_tokens integer,
    total_tokens integer,
    error_code character varying(64),
    created_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    completed_at timestamp with time zone,
    expires_at timestamp with time zone,
    CONSTRAINT chk_query_cache CHECK (((cache_status)::text = ANY ((ARRAY['BYPASS'::character varying, 'MISS'::character varying, 'EXACT_HIT'::character varying, 'SEMANTIC_HIT'::character varying, 'WRITE_FAILED'::character varying])::text[]))),
    CONSTRAINT chk_query_counts CHECK (((candidate_count >= 0) AND (context_count >= 0))),
    CONSTRAINT chk_query_outcome CHECK (((outcome IS NULL) OR ((outcome)::text = ANY ((ARRAY['success'::character varying, 'no_evidence'::character varying, 'refused'::character varying, 'cancelled'::character varying])::text[])))),
    CONSTRAINT chk_query_scope CHECK ((security_scope_id ~ '^[0-9a-f]{64}$'::text)),
    CONSTRAINT chk_query_status CHECK (((request_status)::text = ANY ((ARRAY['ACCEPTED'::character varying, 'RUNNING'::character varying, 'COMPLETED'::character varying, 'FAILED'::character varying, 'CANCELLED'::character varying])::text[]))),
    CONSTRAINT chk_query_token_counts CHECK ((((prompt_tokens IS NULL) OR (prompt_tokens >= 0)) AND ((completion_tokens IS NULL) OR (completion_tokens >= 0)) AND ((total_tokens IS NULL) OR (total_tokens >= 0))))
);


--
-- Name: alembic_version alembic_version_pkc; Type: CONSTRAINT; Schema: rag_app; Owner: -
--

ALTER TABLE ONLY rag_app.alembic_version
    ADD CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num);


--
-- Name: citation_record citation_record_pkey; Type: CONSTRAINT; Schema: rag_app; Owner: -
--

ALTER TABLE ONLY rag_app.citation_record
    ADD CONSTRAINT citation_record_pkey PRIMARY KEY (id);


--
-- Name: config_profile config_profile_pkey; Type: CONSTRAINT; Schema: rag_app; Owner: -
--

ALTER TABLE ONLY rag_app.config_profile
    ADD CONSTRAINT config_profile_pkey PRIMARY KEY (id);


--
-- Name: document_entity document_entity_pkey; Type: CONSTRAINT; Schema: rag_app; Owner: -
--

ALTER TABLE ONLY rag_app.document_entity
    ADD CONSTRAINT document_entity_pkey PRIMARY KEY (id);


--
-- Name: document_index_node document_index_node_pkey; Type: CONSTRAINT; Schema: rag_app; Owner: -
--

ALTER TABLE ONLY rag_app.document_index_node
    ADD CONSTRAINT document_index_node_pkey PRIMARY KEY (id);


--
-- Name: document_record document_record_pkey; Type: CONSTRAINT; Schema: rag_app; Owner: -
--

ALTER TABLE ONLY rag_app.document_record
    ADD CONSTRAINT document_record_pkey PRIMARY KEY (id);


--
-- Name: document_version document_version_pkey; Type: CONSTRAINT; Schema: rag_app; Owner: -
--

ALTER TABLE ONLY rag_app.document_version
    ADD CONSTRAINT document_version_pkey PRIMARY KEY (id);


--
-- Name: entity_relation entity_relation_pkey; Type: CONSTRAINT; Schema: rag_app; Owner: -
--

ALTER TABLE ONLY rag_app.entity_relation
    ADD CONSTRAINT entity_relation_pkey PRIMARY KEY (id);


--
-- Name: feedback_record feedback_record_pkey; Type: CONSTRAINT; Schema: rag_app; Owner: -
--

ALTER TABLE ONLY rag_app.feedback_record
    ADD CONSTRAINT feedback_record_pkey PRIMARY KEY (id);


--
-- Name: idempotency_record idempotency_record_pkey; Type: CONSTRAINT; Schema: rag_app; Owner: -
--

ALTER TABLE ONLY rag_app.idempotency_record
    ADD CONSTRAINT idempotency_record_pkey PRIMARY KEY (security_scope_id, idempotency_key, http_method, request_path);


--
-- Name: ingestion_job ingestion_job_pkey; Type: CONSTRAINT; Schema: rag_app; Owner: -
--

ALTER TABLE ONLY rag_app.ingestion_job
    ADD CONSTRAINT ingestion_job_pkey PRIMARY KEY (id);


--
-- Name: knowledge_base knowledge_base_pkey; Type: CONSTRAINT; Schema: rag_app; Owner: -
--

ALTER TABLE ONLY rag_app.knowledge_base
    ADD CONSTRAINT knowledge_base_pkey PRIMARY KEY (id);


--
-- Name: query_record query_record_pkey; Type: CONSTRAINT; Schema: rag_app; Owner: -
--

ALTER TABLE ONLY rag_app.query_record
    ADD CONSTRAINT query_record_pkey PRIMARY KEY (request_id);


--
-- Name: citation_record uk_citation_request_label; Type: CONSTRAINT; Schema: rag_app; Owner: -
--

ALTER TABLE ONLY rag_app.citation_record
    ADD CONSTRAINT uk_citation_request_label UNIQUE (security_scope_id, request_id, label);


--
-- Name: citation_record uk_citation_scope_id; Type: CONSTRAINT; Schema: rag_app; Owner: -
--

ALTER TABLE ONLY rag_app.citation_record
    ADD CONSTRAINT uk_citation_scope_id UNIQUE (security_scope_id, id);


--
-- Name: config_profile uk_config_profile_name_version; Type: CONSTRAINT; Schema: rag_app; Owner: -
--

ALTER TABLE ONLY rag_app.config_profile
    ADD CONSTRAINT uk_config_profile_name_version UNIQUE (profile_type, name, version);


--
-- Name: document_entity uk_document_entity_name_version; Type: CONSTRAINT; Schema: rag_app; Owner: -
--

ALTER TABLE ONLY rag_app.document_entity
    ADD CONSTRAINT uk_document_entity_name_version UNIQUE (security_scope_id, document_id, normalized_name, index_version);


--
-- Name: document_entity uk_document_entity_scope_id; Type: CONSTRAINT; Schema: rag_app; Owner: -
--

ALTER TABLE ONLY rag_app.document_entity
    ADD CONSTRAINT uk_document_entity_scope_id UNIQUE (security_scope_id, id);


--
-- Name: document_record uk_document_ragflow_id; Type: CONSTRAINT; Schema: rag_app; Owner: -
--

ALTER TABLE ONLY rag_app.document_record
    ADD CONSTRAINT uk_document_ragflow_id UNIQUE (ragflow_document_id);


--
-- Name: document_record uk_document_scope_id; Type: CONSTRAINT; Schema: rag_app; Owner: -
--

ALTER TABLE ONLY rag_app.document_record
    ADD CONSTRAINT uk_document_scope_id UNIQUE (security_scope_id, id);


--
-- Name: document_version uk_document_version_number; Type: CONSTRAINT; Schema: rag_app; Owner: -
--

ALTER TABLE ONLY rag_app.document_version
    ADD CONSTRAINT uk_document_version_number UNIQUE (security_scope_id, document_id, version_number);


--
-- Name: document_version uk_document_version_ragflow_document_id; Type: CONSTRAINT; Schema: rag_app; Owner: -
--

ALTER TABLE ONLY rag_app.document_version
    ADD CONSTRAINT uk_document_version_ragflow_document_id UNIQUE (ragflow_document_id);


--
-- Name: document_version uk_document_version_scope_id; Type: CONSTRAINT; Schema: rag_app; Owner: -
--

ALTER TABLE ONLY rag_app.document_version
    ADD CONSTRAINT uk_document_version_scope_id UNIQUE (security_scope_id, id);


--
-- Name: entity_relation uk_entity_relation_scope_id; Type: CONSTRAINT; Schema: rag_app; Owner: -
--

ALTER TABLE ONLY rag_app.entity_relation
    ADD CONSTRAINT uk_entity_relation_scope_id UNIQUE (security_scope_id, id);


--
-- Name: entity_relation uk_entity_relation_triple_version; Type: CONSTRAINT; Schema: rag_app; Owner: -
--

ALTER TABLE ONLY rag_app.entity_relation
    ADD CONSTRAINT uk_entity_relation_triple_version UNIQUE (security_scope_id, document_id, subject_normalized, predicate, object_normalized, index_version);


--
-- Name: feedback_record uk_feedback_scope_id; Type: CONSTRAINT; Schema: rag_app; Owner: -
--

ALTER TABLE ONLY rag_app.feedback_record
    ADD CONSTRAINT uk_feedback_scope_id UNIQUE (security_scope_id, id);


--
-- Name: document_index_node uk_index_node_document_ordinal_version; Type: CONSTRAINT; Schema: rag_app; Owner: -
--

ALTER TABLE ONLY rag_app.document_index_node
    ADD CONSTRAINT uk_index_node_document_ordinal_version UNIQUE (security_scope_id, document_id, node_type, ordinal, index_version);


--
-- Name: document_index_node uk_index_node_scope_id; Type: CONSTRAINT; Schema: rag_app; Owner: -
--

ALTER TABLE ONLY rag_app.document_index_node
    ADD CONSTRAINT uk_index_node_scope_id UNIQUE (security_scope_id, id);


--
-- Name: ingestion_job uk_ingestion_job_scope_id; Type: CONSTRAINT; Schema: rag_app; Owner: -
--

ALTER TABLE ONLY rag_app.ingestion_job
    ADD CONSTRAINT uk_ingestion_job_scope_id UNIQUE (security_scope_id, id);


--
-- Name: knowledge_base uk_knowledge_base_external_id; Type: CONSTRAINT; Schema: rag_app; Owner: -
--

ALTER TABLE ONLY rag_app.knowledge_base
    ADD CONSTRAINT uk_knowledge_base_external_id UNIQUE (security_scope_id, external_id);


--
-- Name: knowledge_base uk_knowledge_base_ragflow_dataset; Type: CONSTRAINT; Schema: rag_app; Owner: -
--

ALTER TABLE ONLY rag_app.knowledge_base
    ADD CONSTRAINT uk_knowledge_base_ragflow_dataset UNIQUE (ragflow_dataset_id);


--
-- Name: knowledge_base uk_knowledge_base_scope_id; Type: CONSTRAINT; Schema: rag_app; Owner: -
--

ALTER TABLE ONLY rag_app.knowledge_base
    ADD CONSTRAINT uk_knowledge_base_scope_id UNIQUE (security_scope_id, id);


--
-- Name: query_record uk_query_record_scope_id; Type: CONSTRAINT; Schema: rag_app; Owner: -
--

ALTER TABLE ONLY rag_app.query_record
    ADD CONSTRAINT uk_query_record_scope_id UNIQUE (security_scope_id, request_id);


--
-- Name: idx_citation_scope_document; Type: INDEX; Schema: rag_app; Owner: -
--

CREATE INDEX idx_citation_scope_document ON rag_app.citation_record USING btree (security_scope_id, document_id, created_at);


--
-- Name: idx_config_profile_enabled; Type: INDEX; Schema: rag_app; Owner: -
--

CREATE INDEX idx_config_profile_enabled ON rag_app.config_profile USING btree (profile_type, enabled);


--
-- Name: idx_document_entity_scope_kb_name; Type: INDEX; Schema: rag_app; Owner: -
--

CREATE INDEX idx_document_entity_scope_kb_name ON rag_app.document_entity USING btree (security_scope_id, knowledge_base_id, normalized_name);


--
-- Name: idx_document_entity_source_chunks_gin; Type: INDEX; Schema: rag_app; Owner: -
--

CREATE INDEX idx_document_entity_source_chunks_gin ON rag_app.document_entity USING gin (source_chunk_ids_json);


--
-- Name: idx_document_metadata_gin; Type: INDEX; Schema: rag_app; Owner: -
--

CREATE INDEX idx_document_metadata_gin ON rag_app.document_record USING gin (metadata_json);


--
-- Name: idx_document_scope_filename; Type: INDEX; Schema: rag_app; Owner: -
--

CREATE INDEX idx_document_scope_filename ON rag_app.document_record USING btree (security_scope_id, knowledge_base_id, filename);


--
-- Name: idx_document_scope_hash; Type: INDEX; Schema: rag_app; Owner: -
--

CREATE INDEX idx_document_scope_hash ON rag_app.document_record USING btree (security_scope_id, knowledge_base_id, content_sha256);


--
-- Name: idx_document_scope_kb_status; Type: INDEX; Schema: rag_app; Owner: -
--

CREATE INDEX idx_document_scope_kb_status ON rag_app.document_record USING btree (security_scope_id, knowledge_base_id, status, updated_at);


--
-- Name: idx_document_version_scope_document; Type: INDEX; Schema: rag_app; Owner: -
--

CREATE INDEX idx_document_version_scope_document ON rag_app.document_version USING btree (security_scope_id, document_id, version_number);


--
-- Name: idx_document_version_scope_status; Type: INDEX; Schema: rag_app; Owner: -
--

CREATE INDEX idx_document_version_scope_status ON rag_app.document_version USING btree (security_scope_id, status, updated_at);


--
-- Name: idx_entity_relation_scope_kb_object; Type: INDEX; Schema: rag_app; Owner: -
--

CREATE INDEX idx_entity_relation_scope_kb_object ON rag_app.entity_relation USING btree (security_scope_id, knowledge_base_id, object_normalized);


--
-- Name: idx_entity_relation_scope_kb_subject; Type: INDEX; Schema: rag_app; Owner: -
--

CREATE INDEX idx_entity_relation_scope_kb_subject ON rag_app.entity_relation USING btree (security_scope_id, knowledge_base_id, subject_normalized);


--
-- Name: idx_entity_relation_source_chunks_gin; Type: INDEX; Schema: rag_app; Owner: -
--

CREATE INDEX idx_entity_relation_source_chunks_gin ON rag_app.entity_relation USING gin (source_chunk_ids_json);


--
-- Name: idx_feedback_scope_request; Type: INDEX; Schema: rag_app; Owner: -
--

CREATE INDEX idx_feedback_scope_request ON rag_app.feedback_record USING btree (security_scope_id, request_id, created_at);


--
-- Name: idx_feedback_scope_type; Type: INDEX; Schema: rag_app; Owner: -
--

CREATE INDEX idx_feedback_scope_type ON rag_app.feedback_record USING btree (security_scope_id, feedback_type, created_at);


--
-- Name: idx_idempotency_scope_expiry; Type: INDEX; Schema: rag_app; Owner: -
--

CREATE INDEX idx_idempotency_scope_expiry ON rag_app.idempotency_record USING btree (security_scope_id, expires_at);


--
-- Name: idx_index_node_scope_kb_type; Type: INDEX; Schema: rag_app; Owner: -
--

CREATE INDEX idx_index_node_scope_kb_type ON rag_app.document_index_node USING btree (security_scope_id, knowledge_base_id, node_type);


--
-- Name: idx_index_node_source_chunks_gin; Type: INDEX; Schema: rag_app; Owner: -
--

CREATE INDEX idx_index_node_source_chunks_gin ON rag_app.document_index_node USING gin (source_chunk_ids_json);


--
-- Name: idx_job_scope_claim; Type: INDEX; Schema: rag_app; Owner: -
--

CREATE INDEX idx_job_scope_claim ON rag_app.ingestion_job USING btree (security_scope_id, status, next_retry_at, locked_until, created_at);


--
-- Name: idx_job_scope_document; Type: INDEX; Schema: rag_app; Owner: -
--

CREATE INDEX idx_job_scope_document ON rag_app.ingestion_job USING btree (security_scope_id, document_id, created_at);


--
-- Name: idx_job_scope_kb; Type: INDEX; Schema: rag_app; Owner: -
--

CREATE INDEX idx_job_scope_kb ON rag_app.ingestion_job USING btree (security_scope_id, knowledge_base_id, created_at);


--
-- Name: idx_knowledge_base_scope_name; Type: INDEX; Schema: rag_app; Owner: -
--

CREATE INDEX idx_knowledge_base_scope_name ON rag_app.knowledge_base USING btree (security_scope_id, name);


--
-- Name: idx_knowledge_base_scope_status; Type: INDEX; Schema: rag_app; Owner: -
--

CREATE INDEX idx_knowledge_base_scope_status ON rag_app.knowledge_base USING btree (security_scope_id, status, updated_at);


--
-- Name: idx_query_expiry; Type: INDEX; Schema: rag_app; Owner: -
--

CREATE INDEX idx_query_expiry ON rag_app.query_record USING btree (expires_at);


--
-- Name: idx_query_kb_ids_gin; Type: INDEX; Schema: rag_app; Owner: -
--

CREATE INDEX idx_query_kb_ids_gin ON rag_app.query_record USING gin (knowledge_base_ids_json);


--
-- Name: idx_query_scope_created; Type: INDEX; Schema: rag_app; Owner: -
--

CREATE INDEX idx_query_scope_created ON rag_app.query_record USING btree (security_scope_id, created_at);


--
-- Name: idx_query_scope_outcome; Type: INDEX; Schema: rag_app; Owner: -
--

CREATE INDEX idx_query_scope_outcome ON rag_app.query_record USING btree (security_scope_id, outcome, created_at);


--
-- Name: uk_document_external; Type: INDEX; Schema: rag_app; Owner: -
--

CREATE UNIQUE INDEX uk_document_external ON rag_app.document_record USING btree (security_scope_id, knowledge_base_id, external_id) WHERE ((external_id IS NOT NULL) AND (deleted_at IS NULL));


--
-- Name: uk_document_version_active; Type: INDEX; Schema: rag_app; Owner: -
--

CREATE UNIQUE INDEX uk_document_version_active ON rag_app.document_version USING btree (security_scope_id, document_id) WHERE ((status)::text = 'ACTIVE'::text);


--
-- Name: config_profile trg_config_profile_updated_at; Type: TRIGGER; Schema: rag_app; Owner: -
--

CREATE TRIGGER trg_config_profile_updated_at BEFORE UPDATE ON rag_app.config_profile FOR EACH ROW EXECUTE FUNCTION rag_app.set_updated_at();


--
-- Name: document_record trg_document_record_updated_at; Type: TRIGGER; Schema: rag_app; Owner: -
--

CREATE TRIGGER trg_document_record_updated_at BEFORE UPDATE ON rag_app.document_record FOR EACH ROW EXECUTE FUNCTION rag_app.set_updated_at();


--
-- Name: document_version trg_document_version_updated_at; Type: TRIGGER; Schema: rag_app; Owner: -
--

CREATE TRIGGER trg_document_version_updated_at BEFORE UPDATE ON rag_app.document_version FOR EACH ROW EXECUTE FUNCTION rag_app.set_updated_at();


--
-- Name: ingestion_job trg_ingestion_job_updated_at; Type: TRIGGER; Schema: rag_app; Owner: -
--

CREATE TRIGGER trg_ingestion_job_updated_at BEFORE UPDATE ON rag_app.ingestion_job FOR EACH ROW EXECUTE FUNCTION rag_app.set_updated_at();


--
-- Name: knowledge_base trg_knowledge_base_updated_at; Type: TRIGGER; Schema: rag_app; Owner: -
--

CREATE TRIGGER trg_knowledge_base_updated_at BEFORE UPDATE ON rag_app.knowledge_base FOR EACH ROW EXECUTE FUNCTION rag_app.set_updated_at();


--
-- Name: citation_record fk_citation_document; Type: FK CONSTRAINT; Schema: rag_app; Owner: -
--

ALTER TABLE ONLY rag_app.citation_record
    ADD CONSTRAINT fk_citation_document FOREIGN KEY (security_scope_id, document_id) REFERENCES rag_app.document_record(security_scope_id, id) ON DELETE RESTRICT;


--
-- Name: citation_record fk_citation_knowledge_base; Type: FK CONSTRAINT; Schema: rag_app; Owner: -
--

ALTER TABLE ONLY rag_app.citation_record
    ADD CONSTRAINT fk_citation_knowledge_base FOREIGN KEY (security_scope_id, knowledge_base_id) REFERENCES rag_app.knowledge_base(security_scope_id, id) ON DELETE RESTRICT;


--
-- Name: citation_record fk_citation_query; Type: FK CONSTRAINT; Schema: rag_app; Owner: -
--

ALTER TABLE ONLY rag_app.citation_record
    ADD CONSTRAINT fk_citation_query FOREIGN KEY (security_scope_id, request_id) REFERENCES rag_app.query_record(security_scope_id, request_id) ON DELETE CASCADE;


--
-- Name: document_entity fk_document_entity_document; Type: FK CONSTRAINT; Schema: rag_app; Owner: -
--

ALTER TABLE ONLY rag_app.document_entity
    ADD CONSTRAINT fk_document_entity_document FOREIGN KEY (security_scope_id, document_id) REFERENCES rag_app.document_record(security_scope_id, id) ON DELETE CASCADE;


--
-- Name: document_entity fk_document_entity_knowledge_base; Type: FK CONSTRAINT; Schema: rag_app; Owner: -
--

ALTER TABLE ONLY rag_app.document_entity
    ADD CONSTRAINT fk_document_entity_knowledge_base FOREIGN KEY (security_scope_id, knowledge_base_id) REFERENCES rag_app.knowledge_base(security_scope_id, id) ON DELETE CASCADE;


--
-- Name: document_record fk_document_knowledge_base; Type: FK CONSTRAINT; Schema: rag_app; Owner: -
--

ALTER TABLE ONLY rag_app.document_record
    ADD CONSTRAINT fk_document_knowledge_base FOREIGN KEY (security_scope_id, knowledge_base_id) REFERENCES rag_app.knowledge_base(security_scope_id, id) ON DELETE RESTRICT;


--
-- Name: document_version fk_document_version_document; Type: FK CONSTRAINT; Schema: rag_app; Owner: -
--

ALTER TABLE ONLY rag_app.document_version
    ADD CONSTRAINT fk_document_version_document FOREIGN KEY (security_scope_id, document_id) REFERENCES rag_app.document_record(security_scope_id, id) ON DELETE CASCADE;


--
-- Name: document_version fk_document_version_knowledge_base; Type: FK CONSTRAINT; Schema: rag_app; Owner: -
--

ALTER TABLE ONLY rag_app.document_version
    ADD CONSTRAINT fk_document_version_knowledge_base FOREIGN KEY (security_scope_id, knowledge_base_id) REFERENCES rag_app.knowledge_base(security_scope_id, id) ON DELETE RESTRICT;


--
-- Name: entity_relation fk_entity_relation_document; Type: FK CONSTRAINT; Schema: rag_app; Owner: -
--

ALTER TABLE ONLY rag_app.entity_relation
    ADD CONSTRAINT fk_entity_relation_document FOREIGN KEY (security_scope_id, document_id) REFERENCES rag_app.document_record(security_scope_id, id) ON DELETE CASCADE;


--
-- Name: entity_relation fk_entity_relation_knowledge_base; Type: FK CONSTRAINT; Schema: rag_app; Owner: -
--

ALTER TABLE ONLY rag_app.entity_relation
    ADD CONSTRAINT fk_entity_relation_knowledge_base FOREIGN KEY (security_scope_id, knowledge_base_id) REFERENCES rag_app.knowledge_base(security_scope_id, id) ON DELETE CASCADE;


--
-- Name: feedback_record fk_feedback_query; Type: FK CONSTRAINT; Schema: rag_app; Owner: -
--

ALTER TABLE ONLY rag_app.feedback_record
    ADD CONSTRAINT fk_feedback_query FOREIGN KEY (security_scope_id, request_id) REFERENCES rag_app.query_record(security_scope_id, request_id) ON DELETE CASCADE;


--
-- Name: document_index_node fk_index_node_document; Type: FK CONSTRAINT; Schema: rag_app; Owner: -
--

ALTER TABLE ONLY rag_app.document_index_node
    ADD CONSTRAINT fk_index_node_document FOREIGN KEY (security_scope_id, document_id) REFERENCES rag_app.document_record(security_scope_id, id) ON DELETE CASCADE;


--
-- Name: document_index_node fk_index_node_knowledge_base; Type: FK CONSTRAINT; Schema: rag_app; Owner: -
--

ALTER TABLE ONLY rag_app.document_index_node
    ADD CONSTRAINT fk_index_node_knowledge_base FOREIGN KEY (security_scope_id, knowledge_base_id) REFERENCES rag_app.knowledge_base(security_scope_id, id) ON DELETE CASCADE;


--
-- Name: ingestion_job fk_job_document; Type: FK CONSTRAINT; Schema: rag_app; Owner: -
--

ALTER TABLE ONLY rag_app.ingestion_job
    ADD CONSTRAINT fk_job_document FOREIGN KEY (security_scope_id, document_id) REFERENCES rag_app.document_record(security_scope_id, id) ON DELETE RESTRICT;


--
-- Name: ingestion_job fk_job_knowledge_base; Type: FK CONSTRAINT; Schema: rag_app; Owner: -
--

ALTER TABLE ONLY rag_app.ingestion_job
    ADD CONSTRAINT fk_job_knowledge_base FOREIGN KEY (security_scope_id, knowledge_base_id) REFERENCES rag_app.knowledge_base(security_scope_id, id) ON DELETE RESTRICT;


--
-- PostgreSQL database dump complete
--

-- 同时写入历史 Alembic 头和人工发布基线。V1.0.0 之后只允许 DBA 执行经过
-- 审核的增量 SQL；应用启动门禁只读校验这三个值，不会自动修改数据库。
INSERT INTO rag_app.alembic_version (
    version_num,
    release_version,
    schema_checksum
)
VALUES (
    '0006_document_version_updates',
    'V1.0.0',
    '427e6747cf6054d521ad3b292b81a4721612e43494e1560794a821f8bfed83d3'
);

COMMIT;
