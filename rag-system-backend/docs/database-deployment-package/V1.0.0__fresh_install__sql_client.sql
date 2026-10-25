-- ============================================================================
-- LazyTT RAG V1.0.0 全新安装：数据库客户端纯 SQL 单文件版
-- ============================================================================
-- 用途：由 DBA 在数据库客户端的 SQL 编辑器中分三个区块执行，替代原版的 psql 专用命令。
-- 输入：同一 PostgreSQL 实例上的 DBA 连接；目标角色、两个数据库必须全部不存在。
-- 输出：lazytt_rag 业务角色、rag_app 与 rag_flow 数据库及各自的 V1.0.0 基础结构。
-- 原始发布文件：../../migrations/releases/V1.0.0__fresh_install.sql
-- 原始文件 SHA-256：28bcab57c4b3147cb50d06e414fbf685b75e4f00dd266812e02e4fc3d3048437
-- 本文件是客户端执行副本，不替换原始发布文件；不插入 RAGFlow 账号、Token、模型等业务数据。
--
-- 执行顺序（三个区块必须连接同一实例，均使用有权限的 DBA 账号）：
--   第一部分：连接 postgres，按 1A -> 设置业务密码 -> 1B -> 1C -> 1D 分步执行。
--   第二部分：新建/切换连接到 rag_app，选中整个第二部分执行。
--   第三部分：新建/切换连接到 rag_flow，选中整个第三部分执行。
-- PostgreSQL 没有用于切换 Database 的 USE 语句；只修改 Schema/search_path 不等于切库。
--
-- 必须先在客户端开启“遇到错误立即停止”，关闭“忽略错误/出错继续”。
-- 第一部分必须处于自动提交模式，且两条 CREATE DATABASE 各自单独执行。
-- 禁止“执行整个文件”、禁止把整个文件放进单一事务或作为一个批量请求发送。
-- 第二、三部分各自使用 BEGIN/COMMIT，任何检查或 DDL 错误均不得提交部分结构；
-- 如客户端报错后仍显示事务未结束，请在该连接中执行 ROLLBACK 并交由 DBA 审计。
-- 跨数据库不能整体回滚；任一步失败均应停止后续区块，不得重跑或删除已有对象。
--
-- 密码：纯 SQL 无法交互式安全提示。本文件不含密码语句，也不会读取任何 .env。
-- 1A 创建角色后，DBA 必须通过客户端的角色管理界面/批准的安全渠道为 lazytt_rag
-- 设置强密码，再继续后续步骤；密码不能写入本文件、命令历史、日志或聊天。
-- 未设置密码时该角色不能通过密码认证；本文件不会配置 pg_hba.conf 或放宽认证策略。
--
-- 三部分成功后仅完成 V1.0.0；应用启动前还须按发布流程在 rag_app 中执行原增量文件：
-- ../../migrations/releases/V1.0.0__to__V1.1.0__shared_knowledge_bases.sql
-- 增量必须满足 V1.0.0 版本、摘要及业务空表检查；已升级的库不得重复执行。
-- RAGFlow 业务初始化仍由固定 v0.26.4 的官方启动流程完成，禁止手工插入业务数据。
-- ============================================================================


-- ============================================================================
-- 第一部分 START：连接 postgres（DBA，自动提交，按 1A～1D 分步执行）
-- ============================================================================

-- 1A：单独执行整个 DO 语句。空环境检查和创建角色在同一个原子语句中完成；
-- 检查不通过时不会创建角色。必须看到成功结果，才能进行密码设置及 1B。
DO $fresh_preflight$
BEGIN
    IF current_database() <> 'postgres' THEN
        RAISE EXCEPTION '当前连接数据库为 %，请切换到 postgres 后执行第一部分', current_database();
    END IF;

    IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'lazytt_rag') THEN
        RAISE EXCEPTION '角色 lazytt_rag 已存在；本文件仅允许全新初始化';
    END IF;

    IF EXISTS (SELECT 1 FROM pg_catalog.pg_database WHERE datname = 'rag_app') THEN
        RAISE EXCEPTION '数据库 rag_app 已存在；本文件不会覆盖现有数据库';
    END IF;

    IF EXISTS (SELECT 1 FROM pg_catalog.pg_database WHERE datname = 'rag_flow') THEN
        RAISE EXCEPTION '数据库 rag_flow 已存在；本文件不会覆盖现有数据库';
    END IF;

    CREATE ROLE lazytt_rag
        LOGIN
        NOSUPERUSER
        NOCREATEDB
        NOCREATEROLE
        NOINHERIT
        NOREPLICATION
        NOBYPASSRLS;
END
$fresh_preflight$;

-- 暂停：现在通过客户端的角色管理界面为 lazytt_rag 设置强密码，不要写入本文件。
-- 密码设置完成后继续；第二、三部分仍使用 DBA 连接，不是切换成业务账号。

-- 1B：只选中下面这一条 CREATE DATABASE 执行；自动提交必须开启，不能在事务中。
CREATE DATABASE rag_app
    WITH OWNER = lazytt_rag
         ENCODING = 'UTF8'
         TEMPLATE = template0;

-- 1C：确认 1B 成功后，只选中下面这一条 CREATE DATABASE 执行。
CREATE DATABASE rag_flow
    WITH OWNER = lazytt_rag
         ENCODING = 'UTF8'
         TEMPLATE = template0;

-- 1D：两库均创建成功后执行权限收紧和结果核验，禁止跳过。
-- 撤销 PUBLIC 的默认访问，只向唯一业务账号授予数据库连接及临时对象权限。
REVOKE ALL ON DATABASE rag_app FROM PUBLIC;
REVOKE ALL ON DATABASE rag_flow FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE rag_app TO lazytt_rag;
GRANT CONNECT, TEMPORARY ON DATABASE rag_flow TO lazytt_rag;

SELECT rolname, rolsuper, rolcreaterole, rolcreatedb, rolreplication
FROM pg_catalog.pg_roles
WHERE rolname = 'lazytt_rag'
ORDER BY rolname;

SELECT datname, pg_catalog.pg_get_userbyid(datdba) AS owner
FROM pg_catalog.pg_database
WHERE datname IN ('rag_app', 'rag_flow')
ORDER BY datname;

-- 第一部分 END：角色应为非超级用户、不能创建角色/数据库/复制；两库 owner 为 lazytt_rag。
-- 请在客户端新建/切换 Database=rag_app 的 DBA 连接，再执行第二部分。


-- ============================================================================
-- 第二部分 START：连接 rag_app（DBA，必须连同 BEGIN/COMMIT 一起选中执行）
-- ============================================================================
-- 本区块保留原始应用 DDL、版本摘要、权限和空库检查，不含 V1.1.0 增量。
-- 显式事务保证目标错误、对象冲突或任一 DDL 失败时，本区块的修改整体回滚。
BEGIN;

-- 防御性检查：即使数据库被并发写入，也拒绝覆盖任何已有业务表。
DO $$
BEGIN
    -- 先确认连接的 Database，防止只切换了 Schema 却在错误数据库创建对象。
    IF current_database() <> 'rag_app' THEN
        RAISE EXCEPTION '当前连接数据库为 %，请切换到 rag_app 后执行本区块', current_database();
    END IF;

    IF EXISTS (
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
    ) THEN
        RAISE EXCEPTION 'rag_app 不是空数据库，已拒绝执行 Schema 初始化';
    END IF;
END
$$;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO lazytt_rag;
GRANT USAGE, CREATE ON SCHEMA public TO lazytt_rag;
SET ROLE lazytt_rag;

-- --------------------------------------------------------------------------
-- 以下 DDL 是从当前 Alembic 头 0006_document_version_updates 的空 Schema
-- 以 pg_dump --schema-only --no-owner --no-privileges 生成。
-- --------------------------------------------------------------------------
--
-- PostgreSQL database dump
--


-- Dumped from database version 18.4 (Debian 18.4-1.pgdg12+1)
-- Dumped by pg_dump version 18.4 (Debian 18.4-1.pgdg12+1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: rag_app; Type: SCHEMA; Schema: -; Owner: -
--

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

RESET ROLE;

-- 唯一业务账号持有应用 Schema。日常代码仍不自动迁移；结构变更继续由 DBA 审核
-- SQL 后执行，启动门禁只负责验证 Schema，不会执行 DDL。
REVOKE CREATE ON SCHEMA rag_app FROM PUBLIC;
ALTER SCHEMA rag_app OWNER TO lazytt_rag;
GRANT USAGE, CREATE ON SCHEMA rag_app TO lazytt_rag;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA rag_app TO lazytt_rag;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA rag_app TO lazytt_rag;
ALTER DEFAULT PRIVILEGES FOR ROLE lazytt_rag IN SCHEMA rag_app
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO lazytt_rag;
ALTER DEFAULT PRIVILEGES FOR ROLE lazytt_rag IN SCHEMA rag_app
    GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO lazytt_rag;

SELECT
    version_num AS rag_app_alembic_head,
    release_version AS rag_app_schema_release,
    schema_checksum AS rag_app_schema_checksum
FROM rag_app.alembic_version;

SELECT count(*) AS rag_app_table_count
FROM information_schema.tables
WHERE table_schema = 'rag_app' AND table_type = 'BASE TABLE';

SELECT
    has_schema_privilege('lazytt_rag', 'rag_app', 'USAGE') AS app_schema_usage,
    has_schema_privilege('lazytt_rag', 'rag_app', 'CREATE') AS app_schema_create;

COMMIT;

-- 第二部分 END：必须成功 COMMIT；发布号应为 V1.0.0，基础表数量应为 13，
-- app_schema_usage 与 app_schema_create 均为 true。不要仅凭中间查询判定事务成功。
-- 请在客户端新建/切换 Database=rag_flow 的 DBA 连接，再执行第三部分。


-- ============================================================================
-- 第三部分 START：连接 rag_flow（DBA，必须连同 BEGIN/COMMIT 一起选中执行）
-- ============================================================================
-- 本区块只建立固定 RAGFlow v0.26.4 的基础结构，不手工插入业务账号或配置。
-- 显式事务保证本区块内的空库检查、建表及授权全部成功后才会提交。
BEGIN;

DO $$
BEGIN
    -- 先确认连接的 Database，防止只切换了 Schema 却在错误数据库创建对象。
    IF current_database() <> 'rag_flow' THEN
        RAISE EXCEPTION '当前连接数据库为 %，请切换到 rag_flow 后执行本区块', current_database();
    END IF;

    IF EXISTS (
        SELECT 1
        FROM information_schema.tables
        WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
    ) THEN
        RAISE EXCEPTION 'rag_flow 不是空数据库，已拒绝执行 Schema 初始化';
    END IF;
END
$$;

-- 官方 RAGFlow v0.26.4 会在启动时 ALTER 兼容字段，并在任务入队时执行
-- CREATE TABLE IF NOT EXISTS task，因此统一业务账号必须直接拥有 rag_flow.public。
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO lazytt_rag;
GRANT USAGE, CREATE ON SCHEMA public TO lazytt_rag;
SET ROLE lazytt_rag;

-- --------------------------------------------------------------------------
-- 以下 DDL 固定对应 infiniflow/ragflow:v0.26.4 的 42 张基础表。
-- 它只包含 Schema，不包含现有账号、租户、知识库、文档、Token 或模型配置。
-- --------------------------------------------------------------------------
--
-- PostgreSQL database dump
--


-- Dumped from database version 18.4 (Debian 18.4-1.pgdg12+1)
-- Dumped by pg_dump version 18.4 (Debian 18.4-1.pgdg12+1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: api_4_conversation; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.api_4_conversation (
    id character varying(32) NOT NULL,
    create_time bigint,
    create_date timestamp without time zone,
    update_time bigint,
    update_date timestamp without time zone,
    name character varying(255),
    dialog_id character varying(32) NOT NULL,
    user_id character varying(255) NOT NULL,
    exp_user_id character varying(255),
    message text,
    reference text,
    tokens integer NOT NULL,
    source character varying(16),
    dsl text,
    duration real NOT NULL,
    round integer NOT NULL,
    thumb_up integer NOT NULL,
    errors text,
    version_title character varying(255)
);


--
-- Name: api_token; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.api_token (
    create_time bigint,
    create_date timestamp without time zone,
    update_time bigint,
    update_date timestamp without time zone,
    tenant_id character varying(32) NOT NULL,
    token character varying(255) NOT NULL,
    dialog_id character varying(32),
    source character varying(16),
    beta character varying(255)
);


--
-- Name: canvas_template; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.canvas_template (
    id character varying(32) NOT NULL,
    create_time bigint,
    create_date timestamp without time zone,
    update_time bigint,
    update_date timestamp without time zone,
    avatar text,
    title text,
    description text,
    canvas_type character varying(32),
    canvas_types text,
    canvas_category character varying(32) NOT NULL,
    dsl text
);


--
-- Name: chat_channel; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chat_channel (
    id character varying(32) NOT NULL,
    create_time bigint,
    create_date timestamp without time zone,
    update_time bigint,
    update_date timestamp without time zone,
    tenant_id character varying(32) NOT NULL,
    name character varying(128) NOT NULL,
    channel character varying(128) NOT NULL,
    config text NOT NULL,
    chat_id character varying(32),
    status integer NOT NULL,
    dialog_id character varying(32)
);


--
-- Name: compilation_template; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.compilation_template (
    id character varying(32) NOT NULL,
    create_time bigint,
    create_date timestamp without time zone,
    update_time bigint,
    update_date timestamp without time zone,
    tenant_id character varying(32),
    group_id character varying(32),
    name character varying(128) NOT NULL,
    description text,
    kind character varying(64) NOT NULL,
    config text NOT NULL,
    is_builtin boolean NOT NULL,
    status character varying(1)
);


--
-- Name: compilation_template_group; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.compilation_template_group (
    id character varying(32) NOT NULL,
    create_time bigint,
    create_date timestamp without time zone,
    update_time bigint,
    update_date timestamp without time zone,
    tenant_id character varying(32) NOT NULL,
    name character varying(128) NOT NULL,
    description text,
    scope character varying(16) NOT NULL,
    status character varying(1)
);


--
-- Name: connector; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.connector (
    id character varying(32) NOT NULL,
    create_time bigint,
    create_date timestamp without time zone,
    update_time bigint,
    update_date timestamp without time zone,
    tenant_id character varying(32) NOT NULL,
    name character varying(128) NOT NULL,
    source character varying(128) NOT NULL,
    input_type character varying(128) NOT NULL,
    config text NOT NULL,
    refresh_freq integer NOT NULL,
    prune_freq integer NOT NULL,
    timeout_secs integer NOT NULL,
    indexing_start timestamp without time zone,
    status character varying(16)
);


--
-- Name: connector2kb; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.connector2kb (
    id character varying(32) NOT NULL,
    create_time bigint,
    create_date timestamp without time zone,
    update_time bigint,
    update_date timestamp without time zone,
    connector_id character varying(32) NOT NULL,
    kb_id character varying(32) NOT NULL,
    auto_parse character varying(1) NOT NULL
);


--
-- Name: conversation; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.conversation (
    id character varying(32) NOT NULL,
    create_time bigint,
    create_date timestamp without time zone,
    update_time bigint,
    update_date timestamp without time zone,
    dialog_id character varying(32) NOT NULL,
    name character varying(255),
    message text,
    reference text,
    user_id character varying(255)
);


--
-- Name: dialog; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.dialog (
    id character varying(32) NOT NULL,
    create_time bigint,
    create_date timestamp without time zone,
    update_time bigint,
    update_date timestamp without time zone,
    tenant_id character varying(32) NOT NULL,
    name character varying(255),
    description text,
    icon text,
    language character varying(32),
    llm_id character varying(128) NOT NULL,
    tenant_llm_id integer,
    llm_setting text NOT NULL,
    prompt_type character varying(16) NOT NULL,
    prompt_config text NOT NULL,
    meta_data_filter text,
    similarity_threshold real NOT NULL,
    vector_similarity_weight real NOT NULL,
    top_n integer NOT NULL,
    top_k integer NOT NULL,
    do_refer character varying(1) NOT NULL,
    rerank_id character varying(128) NOT NULL,
    tenant_rerank_id integer,
    kb_ids text NOT NULL,
    status character varying(1)
);


--
-- Name: document; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.document (
    id character varying(32) NOT NULL,
    create_time bigint,
    create_date timestamp without time zone,
    update_time bigint,
    update_date timestamp without time zone,
    thumbnail text,
    kb_id character varying(256) NOT NULL,
    parser_id character varying(32) NOT NULL,
    pipeline_id character varying(32),
    parser_config text NOT NULL,
    source_type character varying(128) NOT NULL,
    type character varying(32) NOT NULL,
    created_by character varying(32) NOT NULL,
    name character varying(255),
    location character varying(255),
    size bigint NOT NULL,
    token_num integer NOT NULL,
    chunk_num integer NOT NULL,
    progress real NOT NULL,
    progress_msg text,
    process_begin_at timestamp without time zone,
    process_duration real NOT NULL,
    suffix character varying(32) NOT NULL,
    content_hash character varying(32),
    run character varying(1),
    status character varying(1)
);


--
-- Name: evaluation_cases; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.evaluation_cases (
    id character varying(32) NOT NULL,
    create_date timestamp without time zone,
    update_time bigint,
    update_date timestamp without time zone,
    dataset_id character varying(32) NOT NULL,
    question text NOT NULL,
    reference_answer text,
    relevant_doc_ids text,
    relevant_chunk_ids text,
    metadata text,
    create_time bigint NOT NULL
);


--
-- Name: evaluation_datasets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.evaluation_datasets (
    id character varying(32) NOT NULL,
    create_date timestamp without time zone,
    update_date timestamp without time zone,
    tenant_id character varying(32) NOT NULL,
    name character varying(255) NOT NULL,
    description text,
    kb_ids text NOT NULL,
    created_by character varying(32) NOT NULL,
    create_time bigint NOT NULL,
    update_time bigint NOT NULL,
    status integer NOT NULL
);


--
-- Name: evaluation_results; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.evaluation_results (
    id character varying(32) NOT NULL,
    create_date timestamp without time zone,
    update_time bigint,
    update_date timestamp without time zone,
    run_id character varying(32) NOT NULL,
    case_id character varying(32) NOT NULL,
    generated_answer text NOT NULL,
    retrieved_chunks text NOT NULL,
    metrics text NOT NULL,
    execution_time real NOT NULL,
    token_usage text,
    create_time bigint NOT NULL
);


--
-- Name: evaluation_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.evaluation_runs (
    id character varying(32) NOT NULL,
    create_date timestamp without time zone,
    update_time bigint,
    update_date timestamp without time zone,
    dataset_id character varying(32) NOT NULL,
    dialog_id character varying(32) NOT NULL,
    name character varying(255) NOT NULL,
    config_snapshot text NOT NULL,
    metrics_summary text,
    status character varying(32) NOT NULL,
    created_by character varying(32) NOT NULL,
    create_time bigint NOT NULL,
    complete_time bigint
);


--
-- Name: file; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.file (
    id character varying(32) NOT NULL,
    create_time bigint,
    create_date timestamp without time zone,
    update_time bigint,
    update_date timestamp without time zone,
    parent_id character varying(32) NOT NULL,
    tenant_id character varying(32) NOT NULL,
    created_by character varying(32) NOT NULL,
    name character varying(255) NOT NULL,
    location character varying(255),
    size bigint NOT NULL,
    type character varying(32) NOT NULL,
    source_type character varying(128) NOT NULL
);


--
-- Name: file2document; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.file2document (
    id character varying(32) NOT NULL,
    create_time bigint,
    create_date timestamp without time zone,
    update_time bigint,
    update_date timestamp without time zone,
    file_id character varying(32),
    document_id character varying(32)
);


--
-- Name: file_commit; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.file_commit (
    id character varying(32) NOT NULL,
    create_time bigint,
    create_date timestamp without time zone,
    update_time bigint,
    update_date timestamp without time zone,
    folder_id character varying(32) NOT NULL,
    parent_id character varying(32),
    message character varying(512) NOT NULL,
    author_id character varying(32) NOT NULL,
    file_count integer NOT NULL,
    tree_state text,
    title character varying(255),
    comments text
);


--
-- Name: file_commit_item; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.file_commit_item (
    id character varying(32) NOT NULL,
    create_time bigint,
    create_date timestamp without time zone,
    update_time bigint,
    update_date timestamp without time zone,
    commit_id character varying(32) NOT NULL,
    file_id character varying(32) NOT NULL,
    operation character varying(16) NOT NULL,
    old_hash character varying(64),
    new_hash character varying(64),
    old_location character varying(255),
    new_location character varying(255),
    old_name character varying(255),
    new_name character varying(255),
    diff text,
    content_after_storage character varying(16),
    content_after_location character varying(512),
    slug_kwd character varying(512),
    page_type_kwd character varying(32)
);


--
-- Name: invitation_code; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.invitation_code (
    id character varying(32) NOT NULL,
    create_time bigint,
    create_date timestamp without time zone,
    update_time bigint,
    update_date timestamp without time zone,
    code character varying(32) NOT NULL,
    visit_time timestamp without time zone,
    user_id character varying(32),
    tenant_id character varying(32),
    status character varying(1)
);


--
-- Name: knowledgebase; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.knowledgebase (
    id character varying(32) NOT NULL,
    create_time bigint,
    create_date timestamp without time zone,
    update_time bigint,
    update_date timestamp without time zone,
    avatar text,
    tenant_id character varying(32) NOT NULL,
    name character varying(128) NOT NULL,
    language character varying(32),
    description text,
    embd_id character varying(128) NOT NULL,
    tenant_embd_id integer,
    permission character varying(16) NOT NULL,
    created_by character varying(32) NOT NULL,
    doc_num integer NOT NULL,
    token_num integer NOT NULL,
    chunk_num integer NOT NULL,
    similarity_threshold real NOT NULL,
    vector_similarity_weight real NOT NULL,
    parser_id character varying(32) NOT NULL,
    pipeline_id character varying(32),
    parser_config text NOT NULL,
    pagerank integer NOT NULL,
    graphrag_task_id character varying(32),
    graphrag_task_finish_at timestamp without time zone,
    raptor_task_id character varying(32),
    raptor_task_finish_at timestamp without time zone,
    mindmap_task_id character varying(32),
    mindmap_task_finish_at timestamp without time zone,
    artifact_task_id character varying(32),
    artifact_task_finish_at timestamp without time zone,
    skill_task_id character varying(32),
    skill_task_finish_at timestamp without time zone,
    status character varying(1)
);


--
-- Name: llm; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.llm (
    create_time bigint,
    create_date timestamp without time zone,
    update_time bigint,
    update_date timestamp without time zone,
    llm_name character varying(128) NOT NULL,
    model_type character varying(128) NOT NULL,
    fid character varying(128) NOT NULL,
    max_tokens integer NOT NULL,
    tags character varying(255) NOT NULL,
    is_tools boolean NOT NULL,
    status character varying(1)
);


--
-- Name: llm_factories; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.llm_factories (
    name character varying(128) NOT NULL,
    create_time bigint,
    create_date timestamp without time zone,
    update_time bigint,
    update_date timestamp without time zone,
    logo text,
    tags character varying(255) NOT NULL,
    rank integer NOT NULL,
    status character varying(1)
);


--
-- Name: mcp_server; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.mcp_server (
    id character varying(32) NOT NULL,
    create_time bigint,
    create_date timestamp without time zone,
    update_time bigint,
    update_date timestamp without time zone,
    name character varying(255) NOT NULL,
    tenant_id character varying(32) NOT NULL,
    url character varying(2048) NOT NULL,
    server_type character varying(32) NOT NULL,
    description text,
    variables text,
    headers text
);


--
-- Name: memory; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.memory (
    id character varying(32) NOT NULL,
    create_time bigint,
    create_date timestamp without time zone,
    update_time bigint,
    update_date timestamp without time zone,
    name character varying(128) NOT NULL,
    avatar text,
    tenant_id character varying(32) NOT NULL,
    memory_type integer NOT NULL,
    storage_type character varying(32) NOT NULL,
    embd_id character varying(128) NOT NULL,
    tenant_embd_id integer,
    llm_id character varying(128) NOT NULL,
    tenant_llm_id integer,
    permissions character varying(16) NOT NULL,
    description text,
    memory_size integer NOT NULL,
    forgetting_policy character varying(32) NOT NULL,
    temperature real NOT NULL,
    system_prompt text,
    user_prompt text
);


--
-- Name: pipeline_operation_log; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pipeline_operation_log (
    id character varying(32) NOT NULL,
    create_time bigint,
    create_date timestamp without time zone,
    update_time bigint,
    update_date timestamp without time zone,
    document_id character varying(32) NOT NULL,
    tenant_id character varying(32) NOT NULL,
    kb_id character varying(32) NOT NULL,
    pipeline_id character varying(32),
    pipeline_title character varying(32),
    parser_id character varying(32) NOT NULL,
    document_name character varying(255) NOT NULL,
    document_suffix character varying(255) NOT NULL,
    document_type character varying(255) NOT NULL,
    source_from character varying(255) NOT NULL,
    progress real NOT NULL,
    progress_msg text,
    process_begin_at timestamp without time zone,
    process_duration real NOT NULL,
    dsl text,
    task_type character varying(32) NOT NULL,
    operation_status character varying(32) NOT NULL,
    avatar text,
    status character varying(1)
);


--
-- Name: search; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.search (
    id character varying(32) NOT NULL,
    create_time bigint,
    create_date timestamp without time zone,
    update_time bigint,
    update_date timestamp without time zone,
    avatar text,
    tenant_id character varying(32) NOT NULL,
    name character varying(128) NOT NULL,
    description text,
    created_by character varying(32) NOT NULL,
    search_config text NOT NULL,
    status character varying(1)
);


--
-- Name: sync_logs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sync_logs (
    id character varying(32) NOT NULL,
    create_time bigint,
    create_date timestamp without time zone,
    update_time bigint,
    update_date timestamp without time zone,
    connector_id character varying(32) NOT NULL,
    task_type character varying(32) NOT NULL,
    status character varying(128) NOT NULL,
    from_beginning character varying(1),
    new_docs_indexed integer NOT NULL,
    total_docs_indexed integer NOT NULL,
    docs_removed_from_index integer NOT NULL,
    error_msg text NOT NULL,
    error_count integer NOT NULL,
    full_exception_trace text,
    time_started timestamp without time zone,
    poll_range_start character varying(255),
    poll_range_end character varying(255),
    kb_id character varying(32) NOT NULL
);


--
-- Name: system_settings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.system_settings (
    name character varying(128) NOT NULL,
    create_time bigint,
    create_date timestamp without time zone,
    update_time bigint,
    update_date timestamp without time zone,
    source character varying(32) NOT NULL,
    data_type character varying(32) NOT NULL,
    value text NOT NULL
);


--
-- Name: task; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.task (
    id character varying(32) NOT NULL,
    create_time bigint,
    create_date timestamp without time zone,
    update_time bigint,
    update_date timestamp without time zone,
    doc_id character varying(32) NOT NULL,
    from_page integer NOT NULL,
    to_page integer NOT NULL,
    task_type character varying(32) NOT NULL,
    priority integer NOT NULL,
    begin_at timestamp without time zone,
    process_duration real NOT NULL,
    progress real NOT NULL,
    progress_msg text,
    retry_count integer NOT NULL,
    digest text,
    chunk_ids text
);


--
-- Name: tenant; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tenant (
    id character varying(32) NOT NULL,
    create_time bigint,
    create_date timestamp without time zone,
    update_time bigint,
    update_date timestamp without time zone,
    name character varying(100),
    public_key character varying(255),
    llm_id character varying(128) NOT NULL,
    tenant_llm_id integer,
    embd_id character varying(128) NOT NULL,
    tenant_embd_id integer,
    asr_id character varying(128) NOT NULL,
    tenant_asr_id integer,
    img2txt_id character varying(128) NOT NULL,
    tenant_img2txt_id integer,
    rerank_id character varying(128) NOT NULL,
    tenant_rerank_id integer,
    tts_id character varying(256),
    tenant_tts_id integer,
    ocr_id character varying(256),
    parser_ids character varying(256) NOT NULL,
    credit integer NOT NULL,
    status character varying(1)
);


--
-- Name: tenant_langfuse; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tenant_langfuse (
    tenant_id character varying(32) NOT NULL,
    create_time bigint,
    create_date timestamp without time zone,
    update_time bigint,
    update_date timestamp without time zone,
    secret_key character varying(2048) NOT NULL,
    public_key character varying(2048) NOT NULL,
    host character varying(128) NOT NULL
);


--
-- Name: tenant_llm; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tenant_llm (
    id integer CONSTRAINT tenant_llm_id_not_null1 NOT NULL,
    create_time bigint,
    create_date timestamp without time zone,
    update_time bigint,
    update_date timestamp without time zone,
    tenant_id character varying(32) NOT NULL,
    llm_factory character varying(128) NOT NULL,
    model_type character varying(128),
    llm_name character varying(128),
    api_key text,
    api_base character varying(255),
    max_tokens integer NOT NULL,
    used_tokens integer NOT NULL,
    status character varying(1) NOT NULL
);


--
-- Name: tenant_llm_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.tenant_llm_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: tenant_llm_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.tenant_llm_id_seq OWNED BY public.tenant_llm.id;


--
-- Name: tenant_model; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tenant_model (
    id character varying(32) NOT NULL,
    create_time bigint,
    create_date timestamp without time zone,
    update_time bigint,
    update_date timestamp without time zone,
    model_name character varying(128),
    provider_id character varying(32) NOT NULL,
    instance_id character varying(32) NOT NULL,
    model_type character varying(32) NOT NULL,
    status character varying(32) NOT NULL,
    extra character varying(1024) NOT NULL
);


--
-- Name: tenant_model_group; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tenant_model_group (
    id character varying(32) NOT NULL,
    create_time bigint,
    create_date timestamp without time zone,
    update_time bigint,
    update_date timestamp without time zone,
    group_type character varying(32) NOT NULL,
    model_name character varying(128),
    strategy character varying(32) NOT NULL
);


--
-- Name: tenant_model_group_mapping; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tenant_model_group_mapping (
    create_time bigint,
    create_date timestamp without time zone,
    update_time bigint,
    update_date timestamp without time zone,
    group_id character varying(32) NOT NULL,
    provider_id character varying(32) NOT NULL,
    instance_id character varying(32) NOT NULL,
    model_id character varying(32) NOT NULL,
    weight integer NOT NULL,
    status character varying(32) NOT NULL
);


--
-- Name: tenant_model_instance; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tenant_model_instance (
    id character varying(32) CONSTRAINT tenant_model_instance_id_not_null1 NOT NULL,
    create_time bigint,
    create_date timestamp without time zone,
    update_time bigint,
    update_date timestamp without time zone,
    instance_name character varying(128) NOT NULL,
    provider_id character varying(32) NOT NULL,
    api_key character varying(512) NOT NULL,
    status character varying(32) NOT NULL,
    extra character varying(512) NOT NULL
);


--
-- Name: tenant_model_provider; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tenant_model_provider (
    id character varying(32) CONSTRAINT tenant_model_provider_id_not_null1 NOT NULL,
    create_time bigint,
    create_date timestamp without time zone,
    update_time bigint,
    update_date timestamp without time zone,
    provider_name character varying(128) NOT NULL,
    tenant_id character varying(32) NOT NULL
);


--
-- Name: user; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public."user" (
    id character varying(32) NOT NULL,
    create_time bigint,
    create_date timestamp without time zone,
    update_time bigint,
    update_date timestamp without time zone,
    access_token character varying(255),
    nickname character varying(100) NOT NULL,
    password character varying(255),
    email character varying(255) NOT NULL,
    avatar text,
    language character varying(32),
    color_schema character varying(32),
    timezone character varying(64),
    last_login_time timestamp without time zone,
    is_authenticated character varying(1) NOT NULL,
    is_active character varying(1) NOT NULL,
    is_anonymous character varying(1) NOT NULL,
    login_channel character varying(255),
    status character varying(1),
    is_superuser boolean
);


--
-- Name: user_canvas; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_canvas (
    id character varying(32) NOT NULL,
    create_time bigint,
    create_date timestamp without time zone,
    update_time bigint,
    update_date timestamp without time zone,
    avatar text,
    user_id character varying(255) NOT NULL,
    title character varying(255),
    permission character varying(16) NOT NULL,
    release boolean NOT NULL,
    description text,
    canvas_type character varying(32),
    canvas_category character varying(32) NOT NULL,
    tags character varying(512) NOT NULL,
    dsl text
);


--
-- Name: user_canvas_version; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_canvas_version (
    id character varying(32) NOT NULL,
    create_time bigint,
    create_date timestamp without time zone,
    update_time bigint,
    update_date timestamp without time zone,
    user_canvas_id character varying(255) NOT NULL,
    title character varying(255),
    description text,
    release boolean NOT NULL,
    dsl text
);


--
-- Name: user_tenant; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_tenant (
    id character varying(32) NOT NULL,
    create_time bigint,
    create_date timestamp without time zone,
    update_time bigint,
    update_date timestamp without time zone,
    user_id character varying(32) NOT NULL,
    tenant_id character varying(32) NOT NULL,
    role character varying(32) NOT NULL,
    invited_by character varying(32) NOT NULL,
    status character varying(1)
);


--
-- Name: tenant_llm id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant_llm ALTER COLUMN id SET DEFAULT nextval('public.tenant_llm_id_seq'::regclass);


--
-- Name: api_4_conversation api_4_conversation_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_4_conversation
    ADD CONSTRAINT api_4_conversation_pkey PRIMARY KEY (id);


--
-- Name: api_token api_token_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.api_token
    ADD CONSTRAINT api_token_pkey PRIMARY KEY (tenant_id, token);


--
-- Name: canvas_template canvas_template_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.canvas_template
    ADD CONSTRAINT canvas_template_pkey PRIMARY KEY (id);


--
-- Name: chat_channel chat_channel_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chat_channel
    ADD CONSTRAINT chat_channel_pkey PRIMARY KEY (id);


--
-- Name: compilation_template_group compilation_template_group_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.compilation_template_group
    ADD CONSTRAINT compilation_template_group_pkey PRIMARY KEY (id);


--
-- Name: compilation_template compilation_template_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.compilation_template
    ADD CONSTRAINT compilation_template_pkey PRIMARY KEY (id);


--
-- Name: connector2kb connector2kb_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.connector2kb
    ADD CONSTRAINT connector2kb_pkey PRIMARY KEY (id);


--
-- Name: connector connector_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.connector
    ADD CONSTRAINT connector_pkey PRIMARY KEY (id);


--
-- Name: conversation conversation_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.conversation
    ADD CONSTRAINT conversation_pkey PRIMARY KEY (id);


--
-- Name: dialog dialog_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dialog
    ADD CONSTRAINT dialog_pkey PRIMARY KEY (id);


--
-- Name: document document_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.document
    ADD CONSTRAINT document_pkey PRIMARY KEY (id);


--
-- Name: evaluation_cases evaluation_cases_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.evaluation_cases
    ADD CONSTRAINT evaluation_cases_pkey PRIMARY KEY (id);


--
-- Name: evaluation_datasets evaluation_datasets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.evaluation_datasets
    ADD CONSTRAINT evaluation_datasets_pkey PRIMARY KEY (id);


--
-- Name: evaluation_results evaluation_results_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.evaluation_results
    ADD CONSTRAINT evaluation_results_pkey PRIMARY KEY (id);


--
-- Name: evaluation_runs evaluation_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.evaluation_runs
    ADD CONSTRAINT evaluation_runs_pkey PRIMARY KEY (id);


--
-- Name: file2document file2document_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.file2document
    ADD CONSTRAINT file2document_pkey PRIMARY KEY (id);


--
-- Name: file_commit_item file_commit_item_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.file_commit_item
    ADD CONSTRAINT file_commit_item_pkey PRIMARY KEY (id);


--
-- Name: file_commit file_commit_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.file_commit
    ADD CONSTRAINT file_commit_pkey PRIMARY KEY (id);


--
-- Name: file file_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.file
    ADD CONSTRAINT file_pkey PRIMARY KEY (id);


--
-- Name: invitation_code invitation_code_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invitation_code
    ADD CONSTRAINT invitation_code_pkey PRIMARY KEY (id);


--
-- Name: knowledgebase knowledgebase_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.knowledgebase
    ADD CONSTRAINT knowledgebase_pkey PRIMARY KEY (id);


--
-- Name: llm_factories llm_factories_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm_factories
    ADD CONSTRAINT llm_factories_pkey PRIMARY KEY (name);


--
-- Name: llm llm_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.llm
    ADD CONSTRAINT llm_pkey PRIMARY KEY (fid, llm_name);


--
-- Name: mcp_server mcp_server_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mcp_server
    ADD CONSTRAINT mcp_server_pkey PRIMARY KEY (id);


--
-- Name: memory memory_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.memory
    ADD CONSTRAINT memory_pkey PRIMARY KEY (id);


--
-- Name: pipeline_operation_log pipeline_operation_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pipeline_operation_log
    ADD CONSTRAINT pipeline_operation_log_pkey PRIMARY KEY (id);


--
-- Name: search search_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.search
    ADD CONSTRAINT search_pkey PRIMARY KEY (id);


--
-- Name: sync_logs sync_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sync_logs
    ADD CONSTRAINT sync_logs_pkey PRIMARY KEY (id);


--
-- Name: system_settings system_settings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.system_settings
    ADD CONSTRAINT system_settings_pkey PRIMARY KEY (name);


--
-- Name: task task_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.task
    ADD CONSTRAINT task_pkey PRIMARY KEY (id);


--
-- Name: tenant_langfuse tenant_langfuse_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant_langfuse
    ADD CONSTRAINT tenant_langfuse_pkey PRIMARY KEY (tenant_id);


--
-- Name: tenant_llm tenant_llm_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant_llm
    ADD CONSTRAINT tenant_llm_pkey PRIMARY KEY (id);


--
-- Name: tenant_model_group_mapping tenant_model_group_mapping_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant_model_group_mapping
    ADD CONSTRAINT tenant_model_group_mapping_pkey PRIMARY KEY (group_id, provider_id, instance_id, model_id);


--
-- Name: tenant_model_group tenant_model_group_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant_model_group
    ADD CONSTRAINT tenant_model_group_pkey PRIMARY KEY (id);


--
-- Name: tenant_model_instance tenant_model_instance_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant_model_instance
    ADD CONSTRAINT tenant_model_instance_pkey PRIMARY KEY (id);


--
-- Name: tenant_model tenant_model_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant_model
    ADD CONSTRAINT tenant_model_pkey PRIMARY KEY (id);


--
-- Name: tenant_model_provider tenant_model_provider_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant_model_provider
    ADD CONSTRAINT tenant_model_provider_pkey PRIMARY KEY (id);


--
-- Name: tenant tenant_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tenant
    ADD CONSTRAINT tenant_pkey PRIMARY KEY (id);


--
-- Name: user_canvas user_canvas_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_canvas
    ADD CONSTRAINT user_canvas_pkey PRIMARY KEY (id);


--
-- Name: user_canvas_version user_canvas_version_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_canvas_version
    ADD CONSTRAINT user_canvas_version_pkey PRIMARY KEY (id);


--
-- Name: user user_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public."user"
    ADD CONSTRAINT user_pkey PRIMARY KEY (id);


--
-- Name: user_tenant user_tenant_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_tenant
    ADD CONSTRAINT user_tenant_pkey PRIMARY KEY (id);


--
-- Name: api4conversation_create_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX api4conversation_create_date ON public.api_4_conversation USING btree (create_date);


--
-- Name: api4conversation_create_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX api4conversation_create_time ON public.api_4_conversation USING btree (create_time);


--
-- Name: api4conversation_dialog_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX api4conversation_dialog_id ON public.api_4_conversation USING btree (dialog_id);


--
-- Name: api4conversation_duration; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX api4conversation_duration ON public.api_4_conversation USING btree (duration);


--
-- Name: api4conversation_exp_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX api4conversation_exp_user_id ON public.api_4_conversation USING btree (exp_user_id);


--
-- Name: api4conversation_round; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX api4conversation_round ON public.api_4_conversation USING btree (round);


--
-- Name: api4conversation_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX api4conversation_source ON public.api_4_conversation USING btree (source);


--
-- Name: api4conversation_thumb_up; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX api4conversation_thumb_up ON public.api_4_conversation USING btree (thumb_up);


--
-- Name: api4conversation_update_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX api4conversation_update_date ON public.api_4_conversation USING btree (update_date);


--
-- Name: api4conversation_update_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX api4conversation_update_time ON public.api_4_conversation USING btree (update_time);


--
-- Name: api4conversation_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX api4conversation_user_id ON public.api_4_conversation USING btree (user_id);


--
-- Name: apitoken_beta; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX apitoken_beta ON public.api_token USING btree (beta);


--
-- Name: apitoken_create_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX apitoken_create_date ON public.api_token USING btree (create_date);


--
-- Name: apitoken_create_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX apitoken_create_time ON public.api_token USING btree (create_time);


--
-- Name: apitoken_dialog_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX apitoken_dialog_id ON public.api_token USING btree (dialog_id);


--
-- Name: apitoken_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX apitoken_source ON public.api_token USING btree (source);


--
-- Name: apitoken_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX apitoken_tenant_id ON public.api_token USING btree (tenant_id);


--
-- Name: apitoken_token; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX apitoken_token ON public.api_token USING btree (token);


--
-- Name: apitoken_update_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX apitoken_update_date ON public.api_token USING btree (update_date);


--
-- Name: apitoken_update_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX apitoken_update_time ON public.api_token USING btree (update_time);


--
-- Name: canvastemplate_canvas_category; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX canvastemplate_canvas_category ON public.canvas_template USING btree (canvas_category);


--
-- Name: canvastemplate_canvas_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX canvastemplate_canvas_type ON public.canvas_template USING btree (canvas_type);


--
-- Name: canvastemplate_create_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX canvastemplate_create_date ON public.canvas_template USING btree (create_date);


--
-- Name: canvastemplate_create_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX canvastemplate_create_time ON public.canvas_template USING btree (create_time);


--
-- Name: canvastemplate_update_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX canvastemplate_update_date ON public.canvas_template USING btree (update_date);


--
-- Name: canvastemplate_update_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX canvastemplate_update_time ON public.canvas_template USING btree (update_time);


--
-- Name: chat_channel_dialog_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chat_channel_dialog_id ON public.chat_channel USING btree (dialog_id);


--
-- Name: chatchannel_channel; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chatchannel_channel ON public.chat_channel USING btree (channel);


--
-- Name: chatchannel_chat_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chatchannel_chat_id ON public.chat_channel USING btree (chat_id);


--
-- Name: chatchannel_create_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chatchannel_create_date ON public.chat_channel USING btree (create_date);


--
-- Name: chatchannel_create_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chatchannel_create_time ON public.chat_channel USING btree (create_time);


--
-- Name: chatchannel_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chatchannel_status ON public.chat_channel USING btree (status);


--
-- Name: chatchannel_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chatchannel_tenant_id ON public.chat_channel USING btree (tenant_id);


--
-- Name: chatchannel_update_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chatchannel_update_date ON public.chat_channel USING btree (update_date);


--
-- Name: chatchannel_update_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX chatchannel_update_time ON public.chat_channel USING btree (update_time);


--
-- Name: compilationtemplate_create_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX compilationtemplate_create_date ON public.compilation_template USING btree (create_date);


--
-- Name: compilationtemplate_create_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX compilationtemplate_create_time ON public.compilation_template USING btree (create_time);


--
-- Name: compilationtemplate_group_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX compilationtemplate_group_id ON public.compilation_template USING btree (group_id);


--
-- Name: compilationtemplate_is_builtin; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX compilationtemplate_is_builtin ON public.compilation_template USING btree (is_builtin);


--
-- Name: compilationtemplate_kind; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX compilationtemplate_kind ON public.compilation_template USING btree (kind);


--
-- Name: compilationtemplate_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX compilationtemplate_name ON public.compilation_template USING btree (name);


--
-- Name: compilationtemplate_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX compilationtemplate_status ON public.compilation_template USING btree (status);


--
-- Name: compilationtemplate_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX compilationtemplate_tenant_id ON public.compilation_template USING btree (tenant_id);


--
-- Name: compilationtemplate_tenant_id_name_is_builtin_status; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX compilationtemplate_tenant_id_name_is_builtin_status ON public.compilation_template USING btree (tenant_id, name, is_builtin, status);


--
-- Name: compilationtemplate_update_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX compilationtemplate_update_date ON public.compilation_template USING btree (update_date);


--
-- Name: compilationtemplate_update_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX compilationtemplate_update_time ON public.compilation_template USING btree (update_time);


--
-- Name: compilationtemplategroup_create_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX compilationtemplategroup_create_date ON public.compilation_template_group USING btree (create_date);


--
-- Name: compilationtemplategroup_create_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX compilationtemplategroup_create_time ON public.compilation_template_group USING btree (create_time);


--
-- Name: compilationtemplategroup_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX compilationtemplategroup_name ON public.compilation_template_group USING btree (name);


--
-- Name: compilationtemplategroup_scope; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX compilationtemplategroup_scope ON public.compilation_template_group USING btree (scope);


--
-- Name: compilationtemplategroup_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX compilationtemplategroup_status ON public.compilation_template_group USING btree (status);


--
-- Name: compilationtemplategroup_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX compilationtemplategroup_tenant_id ON public.compilation_template_group USING btree (tenant_id);


--
-- Name: compilationtemplategroup_tenant_id_name_status; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX compilationtemplategroup_tenant_id_name_status ON public.compilation_template_group USING btree (tenant_id, name, status);


--
-- Name: compilationtemplategroup_update_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX compilationtemplategroup_update_date ON public.compilation_template_group USING btree (update_date);


--
-- Name: compilationtemplategroup_update_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX compilationtemplategroup_update_time ON public.compilation_template_group USING btree (update_time);


--
-- Name: connector2kb_connector_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX connector2kb_connector_id ON public.connector2kb USING btree (connector_id);


--
-- Name: connector2kb_create_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX connector2kb_create_date ON public.connector2kb USING btree (create_date);


--
-- Name: connector2kb_create_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX connector2kb_create_time ON public.connector2kb USING btree (create_time);


--
-- Name: connector2kb_kb_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX connector2kb_kb_id ON public.connector2kb USING btree (kb_id);


--
-- Name: connector2kb_update_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX connector2kb_update_date ON public.connector2kb USING btree (update_date);


--
-- Name: connector2kb_update_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX connector2kb_update_time ON public.connector2kb USING btree (update_time);


--
-- Name: connector_create_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX connector_create_date ON public.connector USING btree (create_date);


--
-- Name: connector_create_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX connector_create_time ON public.connector USING btree (create_time);


--
-- Name: connector_indexing_start; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX connector_indexing_start ON public.connector USING btree (indexing_start);


--
-- Name: connector_input_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX connector_input_type ON public.connector USING btree (input_type);


--
-- Name: connector_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX connector_source ON public.connector USING btree (source);


--
-- Name: connector_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX connector_status ON public.connector USING btree (status);


--
-- Name: connector_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX connector_tenant_id ON public.connector USING btree (tenant_id);


--
-- Name: connector_update_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX connector_update_date ON public.connector USING btree (update_date);


--
-- Name: connector_update_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX connector_update_time ON public.connector USING btree (update_time);


--
-- Name: conversation_create_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX conversation_create_date ON public.conversation USING btree (create_date);


--
-- Name: conversation_create_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX conversation_create_time ON public.conversation USING btree (create_time);


--
-- Name: conversation_dialog_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX conversation_dialog_id ON public.conversation USING btree (dialog_id);


--
-- Name: conversation_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX conversation_name ON public.conversation USING btree (name);


--
-- Name: conversation_update_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX conversation_update_date ON public.conversation USING btree (update_date);


--
-- Name: conversation_update_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX conversation_update_time ON public.conversation USING btree (update_time);


--
-- Name: conversation_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX conversation_user_id ON public.conversation USING btree (user_id);


--
-- Name: dialog_create_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX dialog_create_date ON public.dialog USING btree (create_date);


--
-- Name: dialog_create_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX dialog_create_time ON public.dialog USING btree (create_time);


--
-- Name: dialog_language; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX dialog_language ON public.dialog USING btree (language);


--
-- Name: dialog_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX dialog_name ON public.dialog USING btree (name);


--
-- Name: dialog_prompt_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX dialog_prompt_type ON public.dialog USING btree (prompt_type);


--
-- Name: dialog_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX dialog_status ON public.dialog USING btree (status);


--
-- Name: dialog_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX dialog_tenant_id ON public.dialog USING btree (tenant_id);


--
-- Name: dialog_tenant_llm_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX dialog_tenant_llm_id ON public.dialog USING btree (tenant_llm_id);


--
-- Name: dialog_tenant_rerank_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX dialog_tenant_rerank_id ON public.dialog USING btree (tenant_rerank_id);


--
-- Name: dialog_update_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX dialog_update_date ON public.dialog USING btree (update_date);


--
-- Name: dialog_update_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX dialog_update_time ON public.dialog USING btree (update_time);


--
-- Name: document_chunk_num; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX document_chunk_num ON public.document USING btree (chunk_num);


--
-- Name: document_content_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX document_content_hash ON public.document USING btree (content_hash);


--
-- Name: document_create_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX document_create_date ON public.document USING btree (create_date);


--
-- Name: document_create_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX document_create_time ON public.document USING btree (create_time);


--
-- Name: document_created_by; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX document_created_by ON public.document USING btree (created_by);


--
-- Name: document_kb_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX document_kb_id ON public.document USING btree (kb_id);


--
-- Name: document_location; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX document_location ON public.document USING btree (location);


--
-- Name: document_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX document_name ON public.document USING btree (name);


--
-- Name: document_parser_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX document_parser_id ON public.document USING btree (parser_id);


--
-- Name: document_pipeline_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX document_pipeline_id ON public.document USING btree (pipeline_id);


--
-- Name: document_process_begin_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX document_process_begin_at ON public.document USING btree (process_begin_at);


--
-- Name: document_progress; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX document_progress ON public.document USING btree (progress);


--
-- Name: document_run; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX document_run ON public.document USING btree (run);


--
-- Name: document_size; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX document_size ON public.document USING btree (size);


--
-- Name: document_source_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX document_source_type ON public.document USING btree (source_type);


--
-- Name: document_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX document_status ON public.document USING btree (status);


--
-- Name: document_suffix; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX document_suffix ON public.document USING btree (suffix);


--
-- Name: document_token_num; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX document_token_num ON public.document USING btree (token_num);


--
-- Name: document_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX document_type ON public.document USING btree (type);


--
-- Name: document_update_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX document_update_date ON public.document USING btree (update_date);


--
-- Name: document_update_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX document_update_time ON public.document USING btree (update_time);


--
-- Name: evaluationcase_create_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX evaluationcase_create_date ON public.evaluation_cases USING btree (create_date);


--
-- Name: evaluationcase_dataset_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX evaluationcase_dataset_id ON public.evaluation_cases USING btree (dataset_id);


--
-- Name: evaluationcase_update_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX evaluationcase_update_date ON public.evaluation_cases USING btree (update_date);


--
-- Name: evaluationcase_update_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX evaluationcase_update_time ON public.evaluation_cases USING btree (update_time);


--
-- Name: evaluationdataset_create_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX evaluationdataset_create_date ON public.evaluation_datasets USING btree (create_date);


--
-- Name: evaluationdataset_create_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX evaluationdataset_create_time ON public.evaluation_datasets USING btree (create_time);


--
-- Name: evaluationdataset_created_by; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX evaluationdataset_created_by ON public.evaluation_datasets USING btree (created_by);


--
-- Name: evaluationdataset_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX evaluationdataset_name ON public.evaluation_datasets USING btree (name);


--
-- Name: evaluationdataset_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX evaluationdataset_tenant_id ON public.evaluation_datasets USING btree (tenant_id);


--
-- Name: evaluationdataset_update_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX evaluationdataset_update_date ON public.evaluation_datasets USING btree (update_date);


--
-- Name: evaluationresult_case_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX evaluationresult_case_id ON public.evaluation_results USING btree (case_id);


--
-- Name: evaluationresult_create_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX evaluationresult_create_date ON public.evaluation_results USING btree (create_date);


--
-- Name: evaluationresult_run_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX evaluationresult_run_id ON public.evaluation_results USING btree (run_id);


--
-- Name: evaluationresult_update_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX evaluationresult_update_date ON public.evaluation_results USING btree (update_date);


--
-- Name: evaluationresult_update_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX evaluationresult_update_time ON public.evaluation_results USING btree (update_time);


--
-- Name: evaluationrun_create_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX evaluationrun_create_date ON public.evaluation_runs USING btree (create_date);


--
-- Name: evaluationrun_create_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX evaluationrun_create_time ON public.evaluation_runs USING btree (create_time);


--
-- Name: evaluationrun_created_by; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX evaluationrun_created_by ON public.evaluation_runs USING btree (created_by);


--
-- Name: evaluationrun_dataset_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX evaluationrun_dataset_id ON public.evaluation_runs USING btree (dataset_id);


--
-- Name: evaluationrun_dialog_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX evaluationrun_dialog_id ON public.evaluation_runs USING btree (dialog_id);


--
-- Name: evaluationrun_update_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX evaluationrun_update_date ON public.evaluation_runs USING btree (update_date);


--
-- Name: evaluationrun_update_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX evaluationrun_update_time ON public.evaluation_runs USING btree (update_time);


--
-- Name: file2document_create_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX file2document_create_date ON public.file2document USING btree (create_date);


--
-- Name: file2document_create_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX file2document_create_time ON public.file2document USING btree (create_time);


--
-- Name: file2document_document_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX file2document_document_id ON public.file2document USING btree (document_id);


--
-- Name: file2document_file_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX file2document_file_id ON public.file2document USING btree (file_id);


--
-- Name: file2document_update_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX file2document_update_date ON public.file2document USING btree (update_date);


--
-- Name: file2document_update_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX file2document_update_time ON public.file2document USING btree (update_time);


--
-- Name: file_create_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX file_create_date ON public.file USING btree (create_date);


--
-- Name: file_create_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX file_create_time ON public.file USING btree (create_time);


--
-- Name: file_created_by; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX file_created_by ON public.file USING btree (created_by);


--
-- Name: file_location; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX file_location ON public.file USING btree (location);


--
-- Name: file_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX file_name ON public.file USING btree (name);


--
-- Name: file_parent_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX file_parent_id ON public.file USING btree (parent_id);


--
-- Name: file_size; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX file_size ON public.file USING btree (size);


--
-- Name: file_source_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX file_source_type ON public.file USING btree (source_type);


--
-- Name: file_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX file_tenant_id ON public.file USING btree (tenant_id);


--
-- Name: file_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX file_type ON public.file USING btree (type);


--
-- Name: file_update_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX file_update_date ON public.file USING btree (update_date);


--
-- Name: file_update_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX file_update_time ON public.file USING btree (update_time);


--
-- Name: filecommit_author_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX filecommit_author_id ON public.file_commit USING btree (author_id);


--
-- Name: filecommit_create_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX filecommit_create_date ON public.file_commit USING btree (create_date);


--
-- Name: filecommit_create_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX filecommit_create_time ON public.file_commit USING btree (create_time);


--
-- Name: filecommit_folder_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX filecommit_folder_id ON public.file_commit USING btree (folder_id);


--
-- Name: filecommit_parent_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX filecommit_parent_id ON public.file_commit USING btree (parent_id);


--
-- Name: filecommit_update_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX filecommit_update_date ON public.file_commit USING btree (update_date);


--
-- Name: filecommit_update_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX filecommit_update_time ON public.file_commit USING btree (update_time);


--
-- Name: filecommititem_commit_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX filecommititem_commit_id ON public.file_commit_item USING btree (commit_id);


--
-- Name: filecommititem_commit_id_file_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX filecommititem_commit_id_file_id ON public.file_commit_item USING btree (commit_id, file_id);


--
-- Name: filecommititem_content_after_storage; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX filecommititem_content_after_storage ON public.file_commit_item USING btree (content_after_storage);


--
-- Name: filecommititem_create_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX filecommititem_create_date ON public.file_commit_item USING btree (create_date);


--
-- Name: filecommititem_create_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX filecommititem_create_time ON public.file_commit_item USING btree (create_time);


--
-- Name: filecommititem_file_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX filecommititem_file_id ON public.file_commit_item USING btree (file_id);


--
-- Name: filecommititem_new_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX filecommititem_new_hash ON public.file_commit_item USING btree (new_hash);


--
-- Name: filecommititem_old_hash; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX filecommititem_old_hash ON public.file_commit_item USING btree (old_hash);


--
-- Name: filecommititem_operation; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX filecommititem_operation ON public.file_commit_item USING btree (operation);


--
-- Name: filecommititem_page_type_kwd; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX filecommititem_page_type_kwd ON public.file_commit_item USING btree (page_type_kwd);


--
-- Name: filecommititem_slug_kwd; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX filecommititem_slug_kwd ON public.file_commit_item USING btree (slug_kwd);


--
-- Name: filecommititem_update_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX filecommititem_update_date ON public.file_commit_item USING btree (update_date);


--
-- Name: filecommititem_update_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX filecommititem_update_time ON public.file_commit_item USING btree (update_time);


--
-- Name: invitationcode_code; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX invitationcode_code ON public.invitation_code USING btree (code);


--
-- Name: invitationcode_create_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX invitationcode_create_date ON public.invitation_code USING btree (create_date);


--
-- Name: invitationcode_create_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX invitationcode_create_time ON public.invitation_code USING btree (create_time);


--
-- Name: invitationcode_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX invitationcode_status ON public.invitation_code USING btree (status);


--
-- Name: invitationcode_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX invitationcode_tenant_id ON public.invitation_code USING btree (tenant_id);


--
-- Name: invitationcode_update_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX invitationcode_update_date ON public.invitation_code USING btree (update_date);


--
-- Name: invitationcode_update_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX invitationcode_update_time ON public.invitation_code USING btree (update_time);


--
-- Name: invitationcode_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX invitationcode_user_id ON public.invitation_code USING btree (user_id);


--
-- Name: invitationcode_visit_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX invitationcode_visit_time ON public.invitation_code USING btree (visit_time);


--
-- Name: knowledgebase_artifact_task_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX knowledgebase_artifact_task_id ON public.knowledgebase USING btree (artifact_task_id);


--
-- Name: knowledgebase_chunk_num; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX knowledgebase_chunk_num ON public.knowledgebase USING btree (chunk_num);


--
-- Name: knowledgebase_create_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX knowledgebase_create_date ON public.knowledgebase USING btree (create_date);


--
-- Name: knowledgebase_create_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX knowledgebase_create_time ON public.knowledgebase USING btree (create_time);


--
-- Name: knowledgebase_created_by; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX knowledgebase_created_by ON public.knowledgebase USING btree (created_by);


--
-- Name: knowledgebase_doc_num; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX knowledgebase_doc_num ON public.knowledgebase USING btree (doc_num);


--
-- Name: knowledgebase_embd_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX knowledgebase_embd_id ON public.knowledgebase USING btree (embd_id);


--
-- Name: knowledgebase_graphrag_task_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX knowledgebase_graphrag_task_id ON public.knowledgebase USING btree (graphrag_task_id);


--
-- Name: knowledgebase_language; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX knowledgebase_language ON public.knowledgebase USING btree (language);


--
-- Name: knowledgebase_mindmap_task_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX knowledgebase_mindmap_task_id ON public.knowledgebase USING btree (mindmap_task_id);


--
-- Name: knowledgebase_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX knowledgebase_name ON public.knowledgebase USING btree (name);


--
-- Name: knowledgebase_parser_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX knowledgebase_parser_id ON public.knowledgebase USING btree (parser_id);


--
-- Name: knowledgebase_permission; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX knowledgebase_permission ON public.knowledgebase USING btree (permission);


--
-- Name: knowledgebase_pipeline_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX knowledgebase_pipeline_id ON public.knowledgebase USING btree (pipeline_id);


--
-- Name: knowledgebase_raptor_task_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX knowledgebase_raptor_task_id ON public.knowledgebase USING btree (raptor_task_id);


--
-- Name: knowledgebase_similarity_threshold; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX knowledgebase_similarity_threshold ON public.knowledgebase USING btree (similarity_threshold);


--
-- Name: knowledgebase_skill_task_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX knowledgebase_skill_task_id ON public.knowledgebase USING btree (skill_task_id);


--
-- Name: knowledgebase_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX knowledgebase_status ON public.knowledgebase USING btree (status);


--
-- Name: knowledgebase_tenant_embd_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX knowledgebase_tenant_embd_id ON public.knowledgebase USING btree (tenant_embd_id);


--
-- Name: knowledgebase_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX knowledgebase_tenant_id ON public.knowledgebase USING btree (tenant_id);


--
-- Name: knowledgebase_token_num; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX knowledgebase_token_num ON public.knowledgebase USING btree (token_num);


--
-- Name: knowledgebase_update_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX knowledgebase_update_date ON public.knowledgebase USING btree (update_date);


--
-- Name: knowledgebase_update_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX knowledgebase_update_time ON public.knowledgebase USING btree (update_time);


--
-- Name: knowledgebase_vector_similarity_weight; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX knowledgebase_vector_similarity_weight ON public.knowledgebase USING btree (vector_similarity_weight);


--
-- Name: llm_create_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX llm_create_date ON public.llm USING btree (create_date);


--
-- Name: llm_create_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX llm_create_time ON public.llm USING btree (create_time);


--
-- Name: llm_fid; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX llm_fid ON public.llm USING btree (fid);


--
-- Name: llm_llm_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX llm_llm_name ON public.llm USING btree (llm_name);


--
-- Name: llm_model_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX llm_model_type ON public.llm USING btree (model_type);


--
-- Name: llm_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX llm_status ON public.llm USING btree (status);


--
-- Name: llm_tags; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX llm_tags ON public.llm USING btree (tags);


--
-- Name: llm_update_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX llm_update_date ON public.llm USING btree (update_date);


--
-- Name: llm_update_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX llm_update_time ON public.llm USING btree (update_time);


--
-- Name: llmfactories_create_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX llmfactories_create_date ON public.llm_factories USING btree (create_date);


--
-- Name: llmfactories_create_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX llmfactories_create_time ON public.llm_factories USING btree (create_time);


--
-- Name: llmfactories_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX llmfactories_status ON public.llm_factories USING btree (status);


--
-- Name: llmfactories_tags; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX llmfactories_tags ON public.llm_factories USING btree (tags);


--
-- Name: llmfactories_update_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX llmfactories_update_date ON public.llm_factories USING btree (update_date);


--
-- Name: llmfactories_update_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX llmfactories_update_time ON public.llm_factories USING btree (update_time);


--
-- Name: mcpserver_create_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX mcpserver_create_date ON public.mcp_server USING btree (create_date);


--
-- Name: mcpserver_create_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX mcpserver_create_time ON public.mcp_server USING btree (create_time);


--
-- Name: mcpserver_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX mcpserver_tenant_id ON public.mcp_server USING btree (tenant_id);


--
-- Name: mcpserver_update_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX mcpserver_update_date ON public.mcp_server USING btree (update_date);


--
-- Name: mcpserver_update_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX mcpserver_update_time ON public.mcp_server USING btree (update_time);


--
-- Name: memory_create_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_create_date ON public.memory USING btree (create_date);


--
-- Name: memory_create_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_create_time ON public.memory USING btree (create_time);


--
-- Name: memory_memory_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_memory_type ON public.memory USING btree (memory_type);


--
-- Name: memory_permissions; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_permissions ON public.memory USING btree (permissions);


--
-- Name: memory_storage_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_storage_type ON public.memory USING btree (storage_type);


--
-- Name: memory_tenant_embd_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_tenant_embd_id ON public.memory USING btree (tenant_embd_id);


--
-- Name: memory_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_tenant_id ON public.memory USING btree (tenant_id);


--
-- Name: memory_tenant_llm_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_tenant_llm_id ON public.memory USING btree (tenant_llm_id);


--
-- Name: memory_update_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_update_date ON public.memory USING btree (update_date);


--
-- Name: memory_update_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX memory_update_time ON public.memory USING btree (update_time);


--
-- Name: pipelineoperationlog_create_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pipelineoperationlog_create_date ON public.pipeline_operation_log USING btree (create_date);


--
-- Name: pipelineoperationlog_create_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pipelineoperationlog_create_time ON public.pipeline_operation_log USING btree (create_time);


--
-- Name: pipelineoperationlog_document_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pipelineoperationlog_document_id ON public.pipeline_operation_log USING btree (document_id);


--
-- Name: pipelineoperationlog_kb_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pipelineoperationlog_kb_id ON public.pipeline_operation_log USING btree (kb_id);


--
-- Name: pipelineoperationlog_parser_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pipelineoperationlog_parser_id ON public.pipeline_operation_log USING btree (parser_id);


--
-- Name: pipelineoperationlog_pipeline_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pipelineoperationlog_pipeline_id ON public.pipeline_operation_log USING btree (pipeline_id);


--
-- Name: pipelineoperationlog_pipeline_title; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pipelineoperationlog_pipeline_title ON public.pipeline_operation_log USING btree (pipeline_title);


--
-- Name: pipelineoperationlog_process_begin_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pipelineoperationlog_process_begin_at ON public.pipeline_operation_log USING btree (process_begin_at);


--
-- Name: pipelineoperationlog_progress; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pipelineoperationlog_progress ON public.pipeline_operation_log USING btree (progress);


--
-- Name: pipelineoperationlog_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pipelineoperationlog_status ON public.pipeline_operation_log USING btree (status);


--
-- Name: pipelineoperationlog_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pipelineoperationlog_tenant_id ON public.pipeline_operation_log USING btree (tenant_id);


--
-- Name: pipelineoperationlog_update_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pipelineoperationlog_update_date ON public.pipeline_operation_log USING btree (update_date);


--
-- Name: pipelineoperationlog_update_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX pipelineoperationlog_update_time ON public.pipeline_operation_log USING btree (update_time);


--
-- Name: search_create_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX search_create_date ON public.search USING btree (create_date);


--
-- Name: search_create_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX search_create_time ON public.search USING btree (create_time);


--
-- Name: search_created_by; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX search_created_by ON public.search USING btree (created_by);


--
-- Name: search_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX search_name ON public.search USING btree (name);


--
-- Name: search_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX search_status ON public.search USING btree (status);


--
-- Name: search_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX search_tenant_id ON public.search USING btree (tenant_id);


--
-- Name: search_update_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX search_update_date ON public.search USING btree (update_date);


--
-- Name: search_update_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX search_update_time ON public.search USING btree (update_time);


--
-- Name: synclogs_connector_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX synclogs_connector_id ON public.sync_logs USING btree (connector_id);


--
-- Name: synclogs_create_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX synclogs_create_date ON public.sync_logs USING btree (create_date);


--
-- Name: synclogs_create_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX synclogs_create_time ON public.sync_logs USING btree (create_time);


--
-- Name: synclogs_kb_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX synclogs_kb_id ON public.sync_logs USING btree (kb_id);


--
-- Name: synclogs_poll_range_end; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX synclogs_poll_range_end ON public.sync_logs USING btree (poll_range_end);


--
-- Name: synclogs_poll_range_start; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX synclogs_poll_range_start ON public.sync_logs USING btree (poll_range_start);


--
-- Name: synclogs_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX synclogs_status ON public.sync_logs USING btree (status);


--
-- Name: synclogs_task_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX synclogs_task_type ON public.sync_logs USING btree (task_type);


--
-- Name: synclogs_time_started; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX synclogs_time_started ON public.sync_logs USING btree (time_started);


--
-- Name: synclogs_update_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX synclogs_update_date ON public.sync_logs USING btree (update_date);


--
-- Name: synclogs_update_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX synclogs_update_time ON public.sync_logs USING btree (update_time);


--
-- Name: systemsettings_create_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX systemsettings_create_date ON public.system_settings USING btree (create_date);


--
-- Name: systemsettings_create_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX systemsettings_create_time ON public.system_settings USING btree (create_time);


--
-- Name: systemsettings_update_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX systemsettings_update_date ON public.system_settings USING btree (update_date);


--
-- Name: systemsettings_update_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX systemsettings_update_time ON public.system_settings USING btree (update_time);


--
-- Name: task_begin_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX task_begin_at ON public.task USING btree (begin_at);


--
-- Name: task_create_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX task_create_date ON public.task USING btree (create_date);


--
-- Name: task_create_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX task_create_time ON public.task USING btree (create_time);


--
-- Name: task_doc_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX task_doc_id ON public.task USING btree (doc_id);


--
-- Name: task_progress; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX task_progress ON public.task USING btree (progress);


--
-- Name: task_update_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX task_update_date ON public.task USING btree (update_date);


--
-- Name: task_update_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX task_update_time ON public.task USING btree (update_time);


--
-- Name: tenant_asr_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenant_asr_id ON public.tenant USING btree (asr_id);


--
-- Name: tenant_create_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenant_create_date ON public.tenant USING btree (create_date);


--
-- Name: tenant_create_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenant_create_time ON public.tenant USING btree (create_time);


--
-- Name: tenant_credit; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenant_credit ON public.tenant USING btree (credit);


--
-- Name: tenant_embd_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenant_embd_id ON public.tenant USING btree (embd_id);


--
-- Name: tenant_img2txt_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenant_img2txt_id ON public.tenant USING btree (img2txt_id);


--
-- Name: tenant_llm_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenant_llm_id ON public.tenant USING btree (llm_id);


--
-- Name: tenant_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenant_name ON public.tenant USING btree (name);


--
-- Name: tenant_ocr_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenant_ocr_id ON public.tenant USING btree (ocr_id);


--
-- Name: tenant_parser_ids; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenant_parser_ids ON public.tenant USING btree (parser_ids);


--
-- Name: tenant_public_key; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenant_public_key ON public.tenant USING btree (public_key);


--
-- Name: tenant_rerank_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenant_rerank_id ON public.tenant USING btree (rerank_id);


--
-- Name: tenant_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenant_status ON public.tenant USING btree (status);


--
-- Name: tenant_tenant_asr_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenant_tenant_asr_id ON public.tenant USING btree (tenant_asr_id);


--
-- Name: tenant_tenant_embd_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenant_tenant_embd_id ON public.tenant USING btree (tenant_embd_id);


--
-- Name: tenant_tenant_img2txt_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenant_tenant_img2txt_id ON public.tenant USING btree (tenant_img2txt_id);


--
-- Name: tenant_tenant_llm_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenant_tenant_llm_id ON public.tenant USING btree (tenant_llm_id);


--
-- Name: tenant_tenant_rerank_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenant_tenant_rerank_id ON public.tenant USING btree (tenant_rerank_id);


--
-- Name: tenant_tenant_tts_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenant_tenant_tts_id ON public.tenant USING btree (tenant_tts_id);


--
-- Name: tenant_tts_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenant_tts_id ON public.tenant USING btree (tts_id);


--
-- Name: tenant_update_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenant_update_date ON public.tenant USING btree (update_date);


--
-- Name: tenant_update_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenant_update_time ON public.tenant USING btree (update_time);


--
-- Name: tenantlangfuse_create_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenantlangfuse_create_date ON public.tenant_langfuse USING btree (create_date);


--
-- Name: tenantlangfuse_create_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenantlangfuse_create_time ON public.tenant_langfuse USING btree (create_time);


--
-- Name: tenantlangfuse_host; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenantlangfuse_host ON public.tenant_langfuse USING btree (host);


--
-- Name: tenantlangfuse_public_key; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenantlangfuse_public_key ON public.tenant_langfuse USING btree (public_key);


--
-- Name: tenantlangfuse_secret_key; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenantlangfuse_secret_key ON public.tenant_langfuse USING btree (secret_key);


--
-- Name: tenantlangfuse_update_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenantlangfuse_update_date ON public.tenant_langfuse USING btree (update_date);


--
-- Name: tenantlangfuse_update_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenantlangfuse_update_time ON public.tenant_langfuse USING btree (update_time);


--
-- Name: tenantllm_create_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenantllm_create_date ON public.tenant_llm USING btree (create_date);


--
-- Name: tenantllm_create_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenantllm_create_time ON public.tenant_llm USING btree (create_time);


--
-- Name: tenantllm_llm_factory; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenantllm_llm_factory ON public.tenant_llm USING btree (llm_factory);


--
-- Name: tenantllm_llm_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenantllm_llm_name ON public.tenant_llm USING btree (llm_name);


--
-- Name: tenantllm_max_tokens; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenantllm_max_tokens ON public.tenant_llm USING btree (max_tokens);


--
-- Name: tenantllm_model_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenantllm_model_type ON public.tenant_llm USING btree (model_type);


--
-- Name: tenantllm_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenantllm_status ON public.tenant_llm USING btree (status);


--
-- Name: tenantllm_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenantllm_tenant_id ON public.tenant_llm USING btree (tenant_id);


--
-- Name: tenantllm_tenant_id_llm_factory_llm_name; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX tenantllm_tenant_id_llm_factory_llm_name ON public.tenant_llm USING btree (tenant_id, llm_factory, llm_name);


--
-- Name: tenantllm_update_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenantllm_update_date ON public.tenant_llm USING btree (update_date);


--
-- Name: tenantllm_update_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenantllm_update_time ON public.tenant_llm USING btree (update_time);


--
-- Name: tenantllm_used_tokens; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenantllm_used_tokens ON public.tenant_llm USING btree (used_tokens);


--
-- Name: tenantmodel_create_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenantmodel_create_date ON public.tenant_model USING btree (create_date);


--
-- Name: tenantmodel_create_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenantmodel_create_time ON public.tenant_model USING btree (create_time);


--
-- Name: tenantmodel_instance_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenantmodel_instance_id ON public.tenant_model USING btree (instance_id);


--
-- Name: tenantmodel_update_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenantmodel_update_date ON public.tenant_model USING btree (update_date);


--
-- Name: tenantmodel_update_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenantmodel_update_time ON public.tenant_model USING btree (update_time);


--
-- Name: tenantmodelgroup_create_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenantmodelgroup_create_date ON public.tenant_model_group USING btree (create_date);


--
-- Name: tenantmodelgroup_create_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenantmodelgroup_create_time ON public.tenant_model_group USING btree (create_time);


--
-- Name: tenantmodelgroup_update_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenantmodelgroup_update_date ON public.tenant_model_group USING btree (update_date);


--
-- Name: tenantmodelgroup_update_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenantmodelgroup_update_time ON public.tenant_model_group USING btree (update_time);


--
-- Name: tenantmodelgroupmapping_create_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenantmodelgroupmapping_create_date ON public.tenant_model_group_mapping USING btree (create_date);


--
-- Name: tenantmodelgroupmapping_create_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenantmodelgroupmapping_create_time ON public.tenant_model_group_mapping USING btree (create_time);


--
-- Name: tenantmodelgroupmapping_group_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenantmodelgroupmapping_group_id ON public.tenant_model_group_mapping USING btree (group_id);


--
-- Name: tenantmodelgroupmapping_model_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenantmodelgroupmapping_model_id ON public.tenant_model_group_mapping USING btree (model_id);


--
-- Name: tenantmodelgroupmapping_update_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenantmodelgroupmapping_update_date ON public.tenant_model_group_mapping USING btree (update_date);


--
-- Name: tenantmodelgroupmapping_update_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenantmodelgroupmapping_update_time ON public.tenant_model_group_mapping USING btree (update_time);


--
-- Name: tenantmodelinstance_create_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenantmodelinstance_create_date ON public.tenant_model_instance USING btree (create_date);


--
-- Name: tenantmodelinstance_create_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenantmodelinstance_create_time ON public.tenant_model_instance USING btree (create_time);


--
-- Name: tenantmodelinstance_update_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenantmodelinstance_update_date ON public.tenant_model_instance USING btree (update_date);


--
-- Name: tenantmodelinstance_update_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenantmodelinstance_update_time ON public.tenant_model_instance USING btree (update_time);


--
-- Name: tenantmodelprovider_create_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenantmodelprovider_create_date ON public.tenant_model_provider USING btree (create_date);


--
-- Name: tenantmodelprovider_create_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenantmodelprovider_create_time ON public.tenant_model_provider USING btree (create_time);


--
-- Name: tenantmodelprovider_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenantmodelprovider_tenant_id ON public.tenant_model_provider USING btree (tenant_id);


--
-- Name: tenantmodelprovider_tenant_id_provider_name; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX tenantmodelprovider_tenant_id_provider_name ON public.tenant_model_provider USING btree (tenant_id, provider_name);


--
-- Name: tenantmodelprovider_update_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenantmodelprovider_update_date ON public.tenant_model_provider USING btree (update_date);


--
-- Name: tenantmodelprovider_update_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX tenantmodelprovider_update_time ON public.tenant_model_provider USING btree (update_time);


--
-- Name: user_access_token; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX user_access_token ON public."user" USING btree (access_token);


--
-- Name: user_color_schema; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX user_color_schema ON public."user" USING btree (color_schema);


--
-- Name: user_create_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX user_create_date ON public."user" USING btree (create_date);


--
-- Name: user_create_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX user_create_time ON public."user" USING btree (create_time);


--
-- Name: user_email; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX user_email ON public."user" USING btree (email);


--
-- Name: user_is_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX user_is_active ON public."user" USING btree (is_active);


--
-- Name: user_is_anonymous; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX user_is_anonymous ON public."user" USING btree (is_anonymous);


--
-- Name: user_is_authenticated; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX user_is_authenticated ON public."user" USING btree (is_authenticated);


--
-- Name: user_is_superuser; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX user_is_superuser ON public."user" USING btree (is_superuser);


--
-- Name: user_language; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX user_language ON public."user" USING btree (language);


--
-- Name: user_last_login_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX user_last_login_time ON public."user" USING btree (last_login_time);


--
-- Name: user_login_channel; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX user_login_channel ON public."user" USING btree (login_channel);


--
-- Name: user_nickname; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX user_nickname ON public."user" USING btree (nickname);


--
-- Name: user_password; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX user_password ON public."user" USING btree (password);


--
-- Name: user_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX user_status ON public."user" USING btree (status);


--
-- Name: user_timezone; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX user_timezone ON public."user" USING btree (timezone);


--
-- Name: user_update_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX user_update_date ON public."user" USING btree (update_date);


--
-- Name: user_update_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX user_update_time ON public."user" USING btree (update_time);


--
-- Name: usercanvas_canvas_category; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usercanvas_canvas_category ON public.user_canvas USING btree (canvas_category);


--
-- Name: usercanvas_canvas_type; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usercanvas_canvas_type ON public.user_canvas USING btree (canvas_type);


--
-- Name: usercanvas_create_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usercanvas_create_date ON public.user_canvas USING btree (create_date);


--
-- Name: usercanvas_create_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usercanvas_create_time ON public.user_canvas USING btree (create_time);


--
-- Name: usercanvas_permission; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usercanvas_permission ON public.user_canvas USING btree (permission);


--
-- Name: usercanvas_release; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usercanvas_release ON public.user_canvas USING btree (release);


--
-- Name: usercanvas_tags; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usercanvas_tags ON public.user_canvas USING btree (tags);


--
-- Name: usercanvas_update_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usercanvas_update_date ON public.user_canvas USING btree (update_date);


--
-- Name: usercanvas_update_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usercanvas_update_time ON public.user_canvas USING btree (update_time);


--
-- Name: usercanvas_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usercanvas_user_id ON public.user_canvas USING btree (user_id);


--
-- Name: usercanvasversion_create_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usercanvasversion_create_date ON public.user_canvas_version USING btree (create_date);


--
-- Name: usercanvasversion_create_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usercanvasversion_create_time ON public.user_canvas_version USING btree (create_time);


--
-- Name: usercanvasversion_release; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usercanvasversion_release ON public.user_canvas_version USING btree (release);


--
-- Name: usercanvasversion_update_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usercanvasversion_update_date ON public.user_canvas_version USING btree (update_date);


--
-- Name: usercanvasversion_update_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usercanvasversion_update_time ON public.user_canvas_version USING btree (update_time);


--
-- Name: usercanvasversion_user_canvas_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usercanvasversion_user_canvas_id ON public.user_canvas_version USING btree (user_canvas_id);


--
-- Name: usertenant_create_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usertenant_create_date ON public.user_tenant USING btree (create_date);


--
-- Name: usertenant_create_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usertenant_create_time ON public.user_tenant USING btree (create_time);


--
-- Name: usertenant_invited_by; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usertenant_invited_by ON public.user_tenant USING btree (invited_by);


--
-- Name: usertenant_role; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usertenant_role ON public.user_tenant USING btree (role);


--
-- Name: usertenant_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usertenant_status ON public.user_tenant USING btree (status);


--
-- Name: usertenant_tenant_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usertenant_tenant_id ON public.user_tenant USING btree (tenant_id);


--
-- Name: usertenant_update_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usertenant_update_date ON public.user_tenant USING btree (update_date);


--
-- Name: usertenant_update_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usertenant_update_time ON public.user_tenant USING btree (update_time);


--
-- Name: usertenant_user_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX usertenant_user_id ON public.user_tenant USING btree (user_id);


--
-- PostgreSQL database dump complete
--

RESET ROLE;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO lazytt_rag;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO lazytt_rag;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO lazytt_rag;
ALTER DEFAULT PRIVILEGES FOR ROLE lazytt_rag IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO lazytt_rag;
ALTER DEFAULT PRIVILEGES FOR ROLE lazytt_rag IN SCHEMA public
    GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO lazytt_rag;

SELECT count(*) AS rag_flow_table_count
FROM information_schema.tables
WHERE table_schema = 'public' AND table_type = 'BASE TABLE';

SELECT
    has_schema_privilege('lazytt_rag', 'public', 'USAGE') AS ragflow_schema_usage,
    has_schema_privilege('lazytt_rag', 'public', 'CREATE') AS ragflow_schema_create;

COMMIT;

-- 第三部分 END：必须成功 COMMIT；基础表数量应为 42，
-- ragflow_schema_usage 与 ragflow_schema_create 均为 true。
-- V1.0.0 安装完成后，重新连接 rag_app 执行原 V1.1.0 增量并核验发布号、字段和索引。
