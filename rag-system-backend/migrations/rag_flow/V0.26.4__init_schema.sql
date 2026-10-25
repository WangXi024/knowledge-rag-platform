-- rag_flow V0.26.4 冻结结构基线，来自改造前已审阅的结构快照。
-- 只在已创建的空数据库中以对象所有者执行；账号、授权和注释由 deploy/ecs/database 管理。
-- 不写入账号、模型、知识库或文档；失败回滚本文件全部结构，禁止删库重装。
-- rag_app 的 alembic_version 名称仅为兼容已有数据库，不再依赖 Python/Alembic 迁移。
BEGIN;
SET LOCAL lock_timeout = '10s';
DO $$
BEGIN
    IF current_database() <> 'rag_flow' THEN
        RAISE EXCEPTION '初始化连接的数据库不正确，应为 rag_flow';
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.tables
               WHERE table_schema NOT IN ('pg_catalog', 'information_schema')) THEN
        RAISE EXCEPTION '数据库不是空库，拒绝覆盖现有结构';
    END IF;
END
$$;

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
COMMIT;
