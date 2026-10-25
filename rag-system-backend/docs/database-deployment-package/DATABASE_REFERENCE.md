# LazyTT RAG 全部存储数据字典

> 本文是 V1.0.0 历史字段说明；当前 V1.1.0 与 RAGFlow 完整字典见 [ECS 数据库字典](../../deploy/ecs/数据库字典.md)。

## 1. 范围与读取方法

本文描述当前 LazyTT RAG 使用的四类持久化/运行态存储：PostgreSQL、Redis Stack、
Elasticsearch 和 MinIO。PostgreSQL 有固定表结构，因此逐表逐字段列出；另外三类不是
关系型数据库，没有“表 + 列”的统一 Schema，本文按逻辑 DB、Key、索引文档或对象
路径列出当前项目实际依赖的结构。

本文只描述结构，不包含任何真实用户数据、密码、Token、API Key、文档正文或向量值。
PostgreSQL 清单来自当前正式发布基线：

- `rag_app`：Schema 发布版本 `V1.0.0`，开发期 Alembic 历史头
  `0006_document_version_updates`；
- `rag_flow`：`infiniflow/ragflow:v0.26.4`；
- `rag_app` 共 13 张表（含 `alembic_version`）；
- `rag_flow` 共 42 张基础表。

`V1.0.0` 是自研 `rag_app` 数据库的正式部署基线。0001～0006 只保留为开发期演进
记录，后续生产升级由 DBA 执行版本化增量 SQL。发布记录和执行规则见
[`RAG_APP_SCHEMA_RELEASES.md`](./RAG_APP_SCHEMA_RELEASES.md) 与
[`migrations/README.md`](./migrations/README.md)。

RAGFlow 内部表与 Elasticsearch 文档映射属于上游实现细节。升级 RAGFlow 版本前必须
重新导出和审计，不能把本文当成跨版本稳定 API。

## 2. 存储总览

| 存储 | 逻辑隔离 | 主要数据 | 是否由 SQL 初始化 |
|---|---|---|:---:|
| PostgreSQL | `rag_app`、`rag_flow` 两个数据库 | 控制面、任务、侧车索引、RAGFlow 元数据 | 是 |
| Redis Stack | DB 0、DB 1、DB 2 | 缓存、RAGFlow 临时数据、Celery 队列 | 否 |
| Elasticsearch | RAGFlow 动态索引 | Chunk 正文、关键词字段、向量与检索元数据 | 否 |
| MinIO | RAGFlow Bucket/Prefix | 原始上传文件、解析中间对象 | 否 |

### 2.1 数据库名称、运行角色与权限基线

当前确认的 PostgreSQL 部署模型是“一个实例、一个可登录业务账号、两个逻辑数据库”。
业务账号同时持有两个数据库及其中的 Schema/对象。完整的 DBA 操作与验收命令见
[`DATABASE_CREATION_AND_PERMISSIONS.md`](./DATABASE_CREATION_AND_PERMISSIONS.md)。

| 对象 | 固定名称 | 用途或权限 |
|---|---|---|
| 业务账号/对象所有者 | `lazytt_rag` | Gateway、Worker、Scheduler、RAGFlow Server 和 Task Executor 共用；同时持有两个项目数据库及其中对象 |
| 自研应用数据库 | `rag_app` | 由 `lazytt_rag` 持有；保存 Gateway 控制面、任务、引用和侧车索引 |
| RAGFlow 数据库 | `rag_flow` | 由 `lazytt_rag` 持有；保存固定版 RAGFlow 元数据 |
| 应用 Schema | `rag_app` | 位于 `rag_app` 数据库，由本交付 SQL 创建 |
| RAGFlow Schema | `public` | 位于 `rag_flow` 数据库，由本交付 SQL 创建固定版基础结构 |

`lazytt_rag` 不授予 `SUPERUSER`、`CREATEDB`、`CREATEROLE`、`REPLICATION` 或
`BYPASSRLS`，但因为它负责固定版本 RAGFlow 的兼容性 DDL，并持有两个项目数据库，
所以在 `rag_app` 与 `rag_flow` 的目标 Schema 内保留 `CREATE`。数据库管理员账号不得
写入运行时配置。该简化方案减少了角色切换，但账号泄露时两个数据库都会受影响。

## 3. `rag_app` 六个开发期演进与正式发布基线

| Revision | 主要变化 | 数据处理 |
|---|---|---|
| `0001_initial_rag_app` | 创建应用基础表、约束、索引、`set_updated_at()` 与更新时间触发器 | 全新建库 |
| `0002_document_external_reuse` | 将文档 `external_id` 全表唯一约束改成“未删除记录”的部分唯一索引 | 不删除历史数据；允许软删除后复用 |
| `0003_document_hierarchical_index` | 新增 `document_index_node`，支持 Parent/Summary 侧车索引 | 不改 RAGFlow 原文；侧车可重建 |
| `0004_entity_graph_index` | 新增 `document_entity`、`entity_relation` | 只保存导航与来源 Chunk ID |
| `0005_allow_section_index_nodes` | `document_index_node.node_type` 增加 `SECTION` | 只替换 CHECK 约束 |
| `0006_document_version_updates` | 新增 `document_version`、激活版本字段、`UPDATE_DOCUMENT` Job 类型与触发器 | 旧库升级时生成 v1 快照；本空库 SQL 直接安装最终结构 |

以上六个 Revision 共同组成 `V1.0.0`。`0006_document_version_updates` 回答“开发期
最后执行到哪一步”，`V1.0.0` 回答“当前可以部署哪一个正式数据库契约”。二者不能
互相替代，也不能通过修改旧 Revision 文件发布新结构。

## 4. PostgreSQL `rag_app` 完整字段字典

### `alembic_version`

记录自研应用数据库的开发期历史头和正式 Schema 发布基线。应用启动会只读校验本表、
13 张业务表清单以及预期摘要；校验不一致时拒绝启动，不会自动执行 DDL。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `version_num` | `character varying(32)` | 否 | `—` | Alembic 当前迁移 Revision。 |
| 2 | `release_version` | `character varying(32)` | 否 | `—` | 面向部署和 DBA 的正式 Schema 版本；当前固定为 `V1.0.0`。 |
| 3 | `schema_checksum` | `character(64)` | 否 | `—` | 当前正式基线的 SHA-256 契约摘要，用于阻止应用连接到错误结构。 |
| 4 | `applied_at` | `timestamp with time zone` | 否 | `CURRENT_TIMESTAMP` | DBA 安装或登记当前正式基线的时间。 |

### `citation_record`

保存回答引用、原始 Chunk 映射、页码位置、分数与引用验证状态。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `id` | `uuid` | 否 | `—` | 记录主标识；具体长度/格式由所属表定义。 |
| 2 | `security_scope_id` | `character(64)` | 否 | `—` | Gateway 隔离键；由当前租户 ID 与用户 ID 组合推导，所有业务查询必须带此边界。 |
| 3 | `request_id` | `uuid` | 否 | `—` | 一次 API/问答请求的 UUID，用于审计和关联日志。 |
| 4 | `ordinal` | `integer` | 否 | `—` | 自研应用 `citation_record` 记录中的“ordinal”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 5 | `label` | `character varying(16)` | 否 | `—` | 自研应用 `citation_record` 记录中的“label”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 6 | `knowledge_base_id` | `uuid` | 否 | `—` | 自研应用知识库 UUID。 |
| 7 | `document_id` | `uuid` | 否 | `—` | 文档标识或文档外键。 |
| 8 | `engine_chunk_id` | `character varying(128)` | 否 | `—` | RAGFlow/检索引擎中的真实 Chunk 标识。 |
| 9 | `document_name` | `character varying(255)` | 否 | `—` | 自研应用 `citation_record` 记录中的“document name”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 10 | `heading_path_json` | `jsonb` | 是 | `—` | heading path 的 JSON 结构；子字段由对应业务对象定义。 |
| 11 | `page_start` | `integer` | 是 | `—` | 自研应用 `citation_record` 记录中的“page start”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 12 | `page_end` | `integer` | 是 | `—` | 自研应用 `citation_record` 记录中的“page end”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 13 | `positions_json` | `jsonb` | 是 | `—` | positions 的 JSON 结构；子字段由对应业务对象定义。 |
| 14 | `quote_text` | `text` | 否 | `—` | 自研应用 `citation_record` 记录中的“quote text”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 15 | `retrieval_score` | `numeric(10,8)` | 是 | `—` | 自研应用 `citation_record` 记录中的“retrieval score”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 16 | `rerank_score` | `numeric(10,8)` | 是 | `—` | 自研应用 `citation_record` 记录中的“rerank score”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 17 | `verification_status` | `character varying(32)` | 否 | `—` | 自研应用 `citation_record` 记录中的“verification status”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 18 | `created_at` | `timestamp with time zone` | 否 | `CURRENT_TIMESTAMP` | 自研应用记录创建时间（带时区）。 |

### `config_profile`

保存不可原地修改的 Parser、Chunk、Embedding、Retrieval、Prompt、Model Profile。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `id` | `uuid` | 否 | `—` | 记录主标识；具体长度/格式由所属表定义。 |
| 2 | `profile_type` | `character varying(32)` | 否 | `—` | 自研应用 `config_profile` 记录中的“profile type”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 3 | `name` | `character varying(64)` | 否 | `—` | 面向用户或系统显示的名称。 |
| 4 | `version` | `integer` | 否 | `—` | 自研应用 `config_profile` 记录中的“version”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 5 | `description` | `character varying(512)` | 是 | `—` | 说明文本。 |
| 6 | `config_json` | `jsonb` | 否 | `—` | config 的 JSON 结构；子字段由对应业务对象定义。 |
| 7 | `checksum_sha256` | `character(64)` | 否 | `—` | 自研应用 `config_profile` 记录中的“checksum sha256”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 8 | `enabled` | `boolean` | 否 | `true` | 控制或记录“enabled”状态。 |
| 9 | `created_at` | `timestamp with time zone` | 否 | `CURRENT_TIMESTAMP` | 自研应用记录创建时间（带时区）。 |
| 10 | `updated_at` | `timestamp with time zone` | 否 | `CURRENT_TIMESTAMP` | 自研应用记录更新时间（带时区，部分表由触发器维护）。 |

### `document_entity`

保存从文档 Chunk 抽取的实体及其来源 Chunk，用于实体导航检索。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `id` | `uuid` | 否 | `—` | 记录主标识；具体长度/格式由所属表定义。 |
| 2 | `security_scope_id` | `character(64)` | 否 | `—` | Gateway 隔离键；由当前租户 ID 与用户 ID 组合推导，所有业务查询必须带此边界。 |
| 3 | `knowledge_base_id` | `uuid` | 否 | `—` | 自研应用知识库 UUID。 |
| 4 | `document_id` | `uuid` | 否 | `—` | 文档标识或文档外键。 |
| 5 | `canonical_name` | `character varying(128)` | 否 | `—` | 自研应用 `document_entity` 记录中的“canonical name”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 6 | `normalized_name` | `character varying(128)` | 否 | `—` | 自研应用 `document_entity` 记录中的“normalized name”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 7 | `entity_type` | `character varying(32)` | 否 | `—` | 自研应用 `document_entity` 记录中的“entity type”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 8 | `aliases_json` | `jsonb` | 否 | `—` | aliases 的 JSON 结构；子字段由对应业务对象定义。 |
| 9 | `source_chunk_ids_json` | `jsonb` | 否 | `—` | 侧车节点对应的真实 RAGFlow Chunk ID 列表；引用必须回读原文。 |
| 10 | `mention_count` | `integer` | 否 | `1` | mention count 的计数或度量值。 |
| 11 | `metadata_json` | `jsonb` | 否 | `'{}'::jsonb` | 业务 Metadata JSON；结构随文档/请求类型变化，不允许存放明文密钥。 |
| 12 | `index_version` | `integer` | 否 | `1` | 自研应用 `document_entity` 记录中的“index version”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 13 | `created_at` | `timestamp with time zone` | 否 | `CURRENT_TIMESTAMP` | 自研应用记录创建时间（带时区）。 |
| 14 | `updated_at` | `timestamp with time zone` | 否 | `CURRENT_TIMESTAMP` | 自研应用记录更新时间（带时区，部分表由触发器维护）。 |

### `document_index_node`

保存 Parent、Section、Summary、Table Parent 等可重建的多粒度侧车索引。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `id` | `uuid` | 否 | `—` | 记录主标识；具体长度/格式由所属表定义。 |
| 2 | `security_scope_id` | `character(64)` | 否 | `—` | Gateway 隔离键；由当前租户 ID 与用户 ID 组合推导，所有业务查询必须带此边界。 |
| 3 | `knowledge_base_id` | `uuid` | 否 | `—` | 自研应用知识库 UUID。 |
| 4 | `document_id` | `uuid` | 否 | `—` | 文档标识或文档外键。 |
| 5 | `node_type` | `character varying(16)` | 否 | `—` | 自研应用 `document_index_node` 记录中的“node type”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 6 | `ordinal` | `integer` | 否 | `—` | 自研应用 `document_index_node` 记录中的“ordinal”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 7 | `content` | `text` | 否 | `—` | 正文或内容载荷。 |
| 8 | `source_chunk_ids_json` | `jsonb` | 否 | `—` | 侧车节点对应的真实 RAGFlow Chunk ID 列表；引用必须回读原文。 |
| 9 | `embedding_bytes` | `bytea` | 是 | `—` | 按固定顺序保存的 FLOAT32 向量原始字节，不依赖 pgvector。 |
| 10 | `embedding_dimension` | `integer` | 否 | `0` | 自研应用 `document_index_node` 记录中的“embedding dimension”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 11 | `metadata_json` | `jsonb` | 否 | `'{}'::jsonb` | 业务 Metadata JSON；结构随文档/请求类型变化，不允许存放明文密钥。 |
| 12 | `index_version` | `integer` | 否 | `1` | 自研应用 `document_index_node` 记录中的“index version”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 13 | `created_at` | `timestamp with time zone` | 否 | `CURRENT_TIMESTAMP` | 自研应用记录创建时间（带时区）。 |
| 14 | `updated_at` | `timestamp with time zone` | 否 | `CURRENT_TIMESTAMP` | 自研应用记录更新时间（带时区，部分表由触发器维护）。 |

### `document_record`

保存文档逻辑主记录、当前激活版本、解析状态、统计和 RAGFlow 映射。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `id` | `uuid` | 否 | `—` | 记录主标识；具体长度/格式由所属表定义。 |
| 2 | `security_scope_id` | `character(64)` | 否 | `—` | Gateway 隔离键；由当前租户 ID 与用户 ID 组合推导，所有业务查询必须带此边界。 |
| 3 | `knowledge_base_id` | `uuid` | 否 | `—` | 自研应用知识库 UUID。 |
| 4 | `external_id` | `character varying(128)` | 是 | `—` | 调用方提供的稳定外部幂等/映射标识；软删除后可按约束复用。 |
| 5 | `ragflow_document_id` | `character varying(64)` | 是 | `—` | 对应 RAGFlow 文档的内部标识。 |
| 6 | `active_version_id` | `uuid` | 是 | `—` | 当前对外生效的文档版本 UUID。 |
| 7 | `active_version_number` | `integer` | 否 | `1` | 当前激活的文档版本号。 |
| 8 | `document_revision` | `bigint` | 否 | `'1'::bigint` | 逻辑文档 Revision；版本切换时递增。 |
| 9 | `filename` | `character varying(255)` | 否 | `—` | 上传时保存的原始文件名。 |
| 10 | `safe_filename` | `character varying(255)` | 否 | `—` | 经安全净化、可用于本地暂存的文件名。 |
| 11 | `mime_type` | `character varying(128)` | 否 | `—` | 自研应用 `document_record` 记录中的“mime type”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 12 | `file_size_bytes` | `bigint` | 否 | `—` | 上传文件字节数。 |
| 13 | `content_sha256` | `character(64)` | 否 | `—` | 文件内容 SHA-256，用于完整性校验和完全重复文件去重。 |
| 14 | `metadata_json` | `jsonb` | 是 | `—` | 业务 Metadata JSON；结构随文档/请求类型变化，不允许存放明文密钥。 |
| 15 | `parser_profile_ref` | `character varying(128)` | 否 | `—` | 绑定的 parser profile Profile/版本引用。 |
| 16 | `chunk_profile_ref` | `character varying(128)` | 否 | `—` | 绑定的 chunk profile Profile/版本引用。 |
| 17 | `status` | `character varying(32)` | 否 | `—` | 当前业务状态；允许值由表上的 CHECK 约束或上游枚举控制。 |
| 18 | `progress` | `numeric(5,4)` | 否 | `'0'::numeric` | 任务或解析进度，通常为 0 到 1 的比例。 |
| 19 | `chunk_count` | `integer` | 否 | `0` | 解析产生的 Chunk 数量。 |
| 20 | `token_count` | `bigint` | 否 | `'0'::bigint` | 文档或任务累计 Token 数。 |
| 21 | `row_version` | `bigint` | 否 | `'0'::bigint` | 自研应用 `document_record` 记录中的“row version”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 22 | `last_error_code` | `character varying(64)` | 是 | `—` | 自研应用 `document_record` 记录中的“last error code”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 23 | `last_error_message` | `character varying(1024)` | 是 | `—` | 自研应用 `document_record` 记录中的“last error message”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 24 | `engine_progress_message` | `character varying(2048)` | 是 | `—` | 自研应用 `document_record` 记录中的“engine progress message”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 25 | `indexed_at` | `timestamp with time zone` | 是 | `—` | 文档完成索引的时间。 |
| 26 | `deleted_at` | `timestamp with time zone` | 是 | `—` | 软删除时间；为空表示记录仍处于活动生命周期。 |
| 27 | `created_at` | `timestamp with time zone` | 否 | `CURRENT_TIMESTAMP` | 自研应用记录创建时间（带时区）。 |
| 28 | `updated_at` | `timestamp with time zone` | 否 | `CURRENT_TIMESTAMP` | 自研应用记录更新时间（带时区，部分表由触发器维护）。 |

### `document_version`

保存文档每个版本的文件指纹、RAGFlow 文档映射、激活状态和更新模式。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `id` | `uuid` | 否 | `—` | 记录主标识；具体长度/格式由所属表定义。 |
| 2 | `security_scope_id` | `character(64)` | 否 | `—` | Gateway 隔离键；由当前租户 ID 与用户 ID 组合推导，所有业务查询必须带此边界。 |
| 3 | `knowledge_base_id` | `uuid` | 否 | `—` | 自研应用知识库 UUID。 |
| 4 | `document_id` | `uuid` | 否 | `—` | 文档标识或文档外键。 |
| 5 | `version_number` | `integer` | 否 | `—` | 文档版本序号。 |
| 6 | `ragflow_document_id` | `character varying(64)` | 是 | `—` | 对应 RAGFlow 文档的内部标识。 |
| 7 | `filename` | `character varying(255)` | 否 | `—` | 上传时保存的原始文件名。 |
| 8 | `safe_filename` | `character varying(255)` | 否 | `—` | 经安全净化、可用于本地暂存的文件名。 |
| 9 | `mime_type` | `character varying(128)` | 否 | `—` | 自研应用 `document_version` 记录中的“mime type”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 10 | `file_size_bytes` | `bigint` | 否 | `—` | 上传文件字节数。 |
| 11 | `content_sha256` | `character(64)` | 否 | `—` | 文件内容 SHA-256，用于完整性校验和完全重复文件去重。 |
| 12 | `metadata_json` | `jsonb` | 否 | `'{}'::jsonb` | 业务 Metadata JSON；结构随文档/请求类型变化，不允许存放明文密钥。 |
| 13 | `parser_profile_ref` | `character varying(128)` | 否 | `—` | 绑定的 parser profile Profile/版本引用。 |
| 14 | `chunk_profile_ref` | `character varying(128)` | 否 | `—` | 绑定的 chunk profile Profile/版本引用。 |
| 15 | `status` | `character varying(32)` | 否 | `—` | 当前业务状态；允许值由表上的 CHECK 约束或上游枚举控制。 |
| 16 | `requested_mode` | `character varying(32)` | 否 | `—` | 自研应用 `document_version` 记录中的“requested mode”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 17 | `effective_mode` | `character varying(32)` | 否 | `—` | 自研应用 `document_version` 记录中的“effective mode”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 18 | `chunk_count` | `integer` | 否 | `0` | 解析产生的 Chunk 数量。 |
| 19 | `token_count` | `bigint` | 否 | `'0'::bigint` | 文档或任务累计 Token 数。 |
| 20 | `last_error_code` | `character varying(64)` | 是 | `—` | 自研应用 `document_version` 记录中的“last error code”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 21 | `last_error_message` | `character varying(1024)` | 是 | `—` | 自研应用 `document_version` 记录中的“last error message”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 22 | `activated_at` | `timestamp with time zone` | 是 | `—` | activated 的时间戳。 |
| 23 | `retired_at` | `timestamp with time zone` | 是 | `—` | retired 的时间戳。 |
| 24 | `created_at` | `timestamp with time zone` | 否 | `CURRENT_TIMESTAMP` | 自研应用记录创建时间（带时区）。 |
| 25 | `updated_at` | `timestamp with time zone` | 否 | `CURRENT_TIMESTAMP` | 自研应用记录更新时间（带时区，部分表由触发器维护）。 |

### `entity_relation`

保存同一知识库内实体之间的轻量关系及来源 Chunk。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `id` | `uuid` | 否 | `—` | 记录主标识；具体长度/格式由所属表定义。 |
| 2 | `security_scope_id` | `character(64)` | 否 | `—` | Gateway 隔离键；由当前租户 ID 与用户 ID 组合推导，所有业务查询必须带此边界。 |
| 3 | `knowledge_base_id` | `uuid` | 否 | `—` | 自研应用知识库 UUID。 |
| 4 | `document_id` | `uuid` | 否 | `—` | 文档标识或文档外键。 |
| 5 | `subject_name` | `character varying(128)` | 否 | `—` | 自研应用 `entity_relation` 记录中的“subject name”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 6 | `subject_normalized` | `character varying(128)` | 否 | `—` | 自研应用 `entity_relation` 记录中的“subject normalized”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 7 | `predicate` | `character varying(64)` | 否 | `—` | 自研应用 `entity_relation` 记录中的“predicate”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 8 | `object_name` | `character varying(128)` | 否 | `—` | 自研应用 `entity_relation` 记录中的“object name”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 9 | `object_normalized` | `character varying(128)` | 否 | `—` | 自研应用 `entity_relation` 记录中的“object normalized”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 10 | `source_chunk_ids_json` | `jsonb` | 否 | `—` | 侧车节点对应的真实 RAGFlow Chunk ID 列表；引用必须回读原文。 |
| 11 | `confidence` | `numeric(5,4)` | 否 | `'1'::numeric` | 自研应用 `entity_relation` 记录中的“confidence”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 12 | `metadata_json` | `jsonb` | 否 | `'{}'::jsonb` | 业务 Metadata JSON；结构随文档/请求类型变化，不允许存放明文密钥。 |
| 13 | `index_version` | `integer` | 否 | `1` | 自研应用 `entity_relation` 记录中的“index version”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 14 | `created_at` | `timestamp with time zone` | 否 | `CURRENT_TIMESTAMP` | 自研应用记录创建时间（带时区）。 |
| 15 | `updated_at` | `timestamp with time zone` | 否 | `CURRENT_TIMESTAMP` | 自研应用记录更新时间（带时区，部分表由触发器维护）。 |

### `feedback_record`

保存用户对指定请求或回答的反馈与附加元数据。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `id` | `uuid` | 否 | `—` | 记录主标识；具体长度/格式由所属表定义。 |
| 2 | `security_scope_id` | `character(64)` | 否 | `—` | Gateway 隔离键；由当前租户 ID 与用户 ID 组合推导，所有业务查询必须带此边界。 |
| 3 | `request_id` | `uuid` | 否 | `—` | 一次 API/问答请求的 UUID，用于审计和关联日志。 |
| 4 | `rating` | `smallint` | 是 | `—` | 自研应用 `feedback_record` 记录中的“rating”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 5 | `feedback_type` | `character varying(32)` | 否 | `—` | 自研应用 `feedback_record` 记录中的“feedback type”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 6 | `comment_text` | `text` | 是 | `—` | 自研应用 `feedback_record` 记录中的“comment text”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 7 | `corrected_answer` | `text` | 是 | `—` | 自研应用 `feedback_record` 记录中的“corrected answer”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 8 | `created_at` | `timestamp with time zone` | 否 | `CURRENT_TIMESTAMP` | 自研应用记录创建时间（带时区）。 |

### `idempotency_record`

保存写接口幂等键、请求指纹与重放响应，避免重复写入。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `security_scope_id` | `character(64)` | 否 | `—` | Gateway 隔离键；由当前租户 ID 与用户 ID 组合推导，所有业务查询必须带此边界。 |
| 2 | `idempotency_key` | `character varying(128)` | 否 | `—` | 自研应用 `idempotency_record` 记录中的“idempotency key”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 3 | `http_method` | `character varying(8)` | 否 | `—` | 自研应用 `idempotency_record` 记录中的“http method”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 4 | `request_path` | `character varying(255)` | 否 | `—` | 自研应用 `idempotency_record` 记录中的“request path”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 5 | `request_sha256` | `character(64)` | 否 | `—` | 自研应用 `idempotency_record` 记录中的“request sha256”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 6 | `processing_status` | `character varying(16)` | 否 | `—` | 自研应用 `idempotency_record` 记录中的“processing status”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 7 | `response_status` | `smallint` | 是 | `—` | 自研应用 `idempotency_record` 记录中的“response status”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 8 | `response_json` | `jsonb` | 是 | `—` | 可重放的响应或结果快照。 |
| 9 | `resource_id` | `uuid` | 是 | `—` | resource 的关联标识；外键约束以实际 Schema 为准。 |
| 10 | `created_at` | `timestamp with time zone` | 否 | `CURRENT_TIMESTAMP` | 自研应用记录创建时间（带时区）。 |
| 11 | `expires_at` | `timestamp with time zone` | 否 | `—` | 记录过期时间。 |

### `ingestion_job`

保存上传、解析、重解析、更新、删除、清理等异步 Job 的状态与重试信息。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `id` | `uuid` | 否 | `—` | 记录主标识；具体长度/格式由所属表定义。 |
| 2 | `security_scope_id` | `character(64)` | 否 | `—` | Gateway 隔离键；由当前租户 ID 与用户 ID 组合推导，所有业务查询必须带此边界。 |
| 3 | `job_type` | `character varying(32)` | 否 | `—` | 自研应用 `ingestion_job` 记录中的“job type”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 4 | `status` | `character varying(32)` | 否 | `—` | 当前业务状态；允许值由表上的 CHECK 约束或上游枚举控制。 |
| 5 | `knowledge_base_id` | `uuid` | 是 | `—` | 自研应用知识库 UUID。 |
| 6 | `document_id` | `uuid` | 是 | `—` | 文档标识或文档外键。 |
| 7 | `progress` | `numeric(5,4)` | 否 | `'0'::numeric` | 任务或解析进度，通常为 0 到 1 的比例。 |
| 8 | `attempt` | `integer` | 否 | `0` | 自研应用 `ingestion_job` 记录中的“attempt”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 9 | `max_attempts` | `integer` | 否 | `2` | 自研应用 `ingestion_job` 记录中的“max attempts”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 10 | `payload_json` | `jsonb` | 是 | `—` | payload 的 JSON 结构；子字段由对应业务对象定义。 |
| 11 | `result_json` | `jsonb` | 是 | `—` | result 的 JSON 结构；子字段由对应业务对象定义。 |
| 12 | `error_code` | `character varying(64)` | 是 | `—` | 稳定错误码。 |
| 13 | `error_message` | `character varying(1024)` | 是 | `—` | 面向日志或调用方的错误说明。 |
| 14 | `next_retry_at` | `timestamp with time zone` | 是 | `—` | next retry 的时间戳。 |
| 15 | `locked_by` | `character varying(128)` | 是 | `—` | 自研应用 `ingestion_job` 记录中的“locked by”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 16 | `locked_until` | `timestamp with time zone` | 是 | `—` | 自研应用 `ingestion_job` 记录中的“locked until”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 17 | `started_at` | `timestamp with time zone` | 是 | `—` | 任务实际开始时间。 |
| 18 | `finished_at` | `timestamp with time zone` | 是 | `—` | 任务结束时间。 |
| 19 | `created_at` | `timestamp with time zone` | 否 | `CURRENT_TIMESTAMP` | 自研应用记录创建时间（带时区）。 |
| 20 | `updated_at` | `timestamp with time zone` | 否 | `CURRENT_TIMESTAMP` | 自研应用记录更新时间（带时区，部分表由触发器维护）。 |

### `knowledge_base`

保存安全 Scope 内知识库的主记录、Profile 绑定和 Revision。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `id` | `uuid` | 否 | `—` | 记录主标识；具体长度/格式由所属表定义。 |
| 2 | `security_scope_id` | `character(64)` | 否 | `—` | Gateway 隔离键；由当前租户 ID 与用户 ID 组合推导，所有业务查询必须带此边界。 |
| 3 | `external_id` | `character varying(128)` | 是 | `—` | 调用方提供的稳定外部幂等/映射标识；软删除后可按约束复用。 |
| 4 | `name` | `character varying(128)` | 否 | `—` | 面向用户或系统显示的名称。 |
| 5 | `description` | `character varying(2048)` | 是 | `—` | 说明文本。 |
| 6 | `ragflow_dataset_id` | `character varying(64)` | 是 | `—` | 对应 RAGFlow 知识库/数据集的内部标识。 |
| 7 | `parser_profile_ref` | `character varying(128)` | 否 | `—` | 绑定的 parser profile Profile/版本引用。 |
| 8 | `chunk_profile_ref` | `character varying(128)` | 否 | `—` | 绑定的 chunk profile Profile/版本引用。 |
| 9 | `embedding_profile_ref` | `character varying(128)` | 否 | `—` | 绑定的 embedding profile Profile/版本引用。 |
| 10 | `retrieval_profile_ref` | `character varying(128)` | 否 | `—` | 绑定的 retrieval profile Profile/版本引用。 |
| 11 | `status` | `character varying(32)` | 否 | `—` | 当前业务状态；允许值由表上的 CHECK 约束或上游枚举控制。 |
| 12 | `revision` | `bigint` | 否 | `'0'::bigint` | 知识库 Revision；变更时用于缓存边界失效。 |
| 13 | `row_version` | `bigint` | 否 | `'0'::bigint` | 自研应用 `knowledge_base` 记录中的“row version”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 14 | `last_error_code` | `character varying(64)` | 是 | `—` | 自研应用 `knowledge_base` 记录中的“last error code”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 15 | `last_error_message` | `character varying(512)` | 是 | `—` | 自研应用 `knowledge_base` 记录中的“last error message”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 16 | `deleted_at` | `timestamp with time zone` | 是 | `—` | 软删除时间；为空表示记录仍处于活动生命周期。 |
| 17 | `created_at` | `timestamp with time zone` | 否 | `CURRENT_TIMESTAMP` | 自研应用记录创建时间（带时区）。 |
| 18 | `updated_at` | `timestamp with time zone` | 否 | `CURRENT_TIMESTAMP` | 自研应用记录更新时间（带时区，部分表由触发器维护）。 |

### `query_record`

保存问答/检索请求的审计记录、Profile、知识库 Revision、结果和耗时。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `request_id` | `uuid` | 否 | `—` | 一次 API/问答请求的 UUID，用于审计和关联日志。 |
| 2 | `security_scope_id` | `character(64)` | 否 | `—` | Gateway 隔离键；由当前租户 ID 与用户 ID 组合推导，所有业务查询必须带此边界。 |
| 3 | `request_status` | `character varying(32)` | 否 | `—` | 自研应用 `query_record` 记录中的“request status”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 4 | `outcome` | `character varying(32)` | 是 | `—` | 自研应用 `query_record` 记录中的“outcome”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 5 | `query_sha256` | `character(64)` | 否 | `—` | 自研应用 `query_record` 记录中的“query sha256”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 6 | `query_redacted` | `text` | 是 | `—` | 自研应用 `query_record` 记录中的“query redacted”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 7 | `effective_query_redacted` | `text` | 是 | `—` | 自研应用 `query_record` 记录中的“effective query redacted”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 8 | `knowledge_base_ids_json` | `jsonb` | 否 | `—` | 本次查询允许访问的知识库 UUID 列表。 |
| 9 | `retrieval_profile_ref` | `character varying(128)` | 否 | `—` | 绑定的 retrieval profile Profile/版本引用。 |
| 10 | `prompt_profile_ref` | `character varying(128)` | 是 | `—` | 绑定的 prompt profile Profile/版本引用。 |
| 11 | `model_profile_ref` | `character varying(128)` | 是 | `—` | 绑定的 model profile Profile/版本引用。 |
| 12 | `cache_status` | `character varying(32)` | 否 | `'BYPASS'::character varying` | 自研应用 `query_record` 记录中的“cache status”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 13 | `candidate_count` | `integer` | 否 | `0` | candidate count 的计数或度量值。 |
| 14 | `context_count` | `integer` | 否 | `0` | context count 的计数或度量值。 |
| 15 | `answer_text` | `text` | 是 | `—` | 自研应用 `query_record` 记录中的“answer text”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 16 | `timings_json` | `jsonb` | 是 | `—` | timings 的 JSON 结构；子字段由对应业务对象定义。 |
| 17 | `warnings_json` | `jsonb` | 是 | `—` | warnings 的 JSON 结构；子字段由对应业务对象定义。 |
| 18 | `prompt_tokens` | `integer` | 是 | `—` | 自研应用 `query_record` 记录中的“prompt tokens”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 19 | `completion_tokens` | `integer` | 是 | `—` | 自研应用 `query_record` 记录中的“completion tokens”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 20 | `total_tokens` | `integer` | 是 | `—` | 自研应用 `query_record` 记录中的“total tokens”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 21 | `error_code` | `character varying(64)` | 是 | `—` | 稳定错误码。 |
| 22 | `created_at` | `timestamp with time zone` | 否 | `CURRENT_TIMESTAMP` | 自研应用记录创建时间（带时区）。 |
| 23 | `completed_at` | `timestamp with time zone` | 是 | `—` | completed 的时间戳。 |
| 24 | `expires_at` | `timestamp with time zone` | 是 | `—` | 记录过期时间。 |

## 5. PostgreSQL `rag_flow` 完整字段字典

以下 42 张表由 RAGFlow v0.26.4 使用。带密码、Token、模型连接或系统设置的表属于
敏感区：只能由官方 API 和受控运维流程访问，禁止把表数据导出到文档或 Git。

### `api_4_conversation`

RAGFlow 对外会话 API 的对话轮次与引用数据。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `id` | `character varying(32)` | 否 | `—` | 记录主标识；具体长度/格式由所属表定义。 |
| 2 | `create_time` | `bigint` | 是 | `—` | RAGFlow 使用的创建时间 Unix 毫秒值。 |
| 3 | `create_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的创建时间戳。 |
| 4 | `update_time` | `bigint` | 是 | `—` | RAGFlow 使用的更新时间 Unix 毫秒值。 |
| 5 | `update_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的更新时间戳。 |
| 6 | `name` | `character varying(255)` | 是 | `—` | 面向用户或系统显示的名称。 |
| 7 | `dialog_id` | `character varying(32)` | 否 | `—` | dialog 的关联标识；外键约束以实际 Schema 为准。 |
| 8 | `user_id` | `character varying(255)` | 否 | `—` | 所属用户标识。 |
| 9 | `exp_user_id` | `character varying(255)` | 是 | `—` | exp user 的关联标识；外键约束以实际 Schema 为准。 |
| 10 | `message` | `text` | 是 | `—` | RAGFlow v0.26.4 `api_4_conversation` 记录中的“message”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 11 | `reference` | `text` | 是 | `—` | RAGFlow v0.26.4 `api_4_conversation` 记录中的“reference”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 12 | `tokens` | `integer` | 否 | `—` | tokens 的计数或度量值。 |
| 13 | `source` | `character varying(16)` | 是 | `—` | RAGFlow v0.26.4 `api_4_conversation` 记录中的“source”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 14 | `dsl` | `text` | 是 | `—` | RAGFlow v0.26.4 `api_4_conversation` 记录中的“dsl”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 15 | `duration` | `real` | 否 | `—` | duration 的计数或度量值。 |
| 16 | `round` | `integer` | 否 | `—` | round 的计数或度量值。 |
| 17 | `thumb_up` | `integer` | 否 | `—` | RAGFlow v0.26.4 `api_4_conversation` 记录中的“thumb up”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 18 | `errors` | `text` | 是 | `—` | RAGFlow v0.26.4 `api_4_conversation` 记录中的“errors”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 19 | `version_title` | `character varying(255)` | 是 | `—` | RAGFlow v0.26.4 `api_4_conversation` 记录中的“version title”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |

### `api_token`

RAGFlow 租户 API Token；属于敏感认证数据。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `create_time` | `bigint` | 是 | `—` | RAGFlow 使用的创建时间 Unix 毫秒值。 |
| 2 | `create_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的创建时间戳。 |
| 3 | `update_time` | `bigint` | 是 | `—` | RAGFlow 使用的更新时间 Unix 毫秒值。 |
| 4 | `update_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的更新时间戳。 |
| 5 | `tenant_id` | `character varying(32)` | 否 | `—` | 所属租户标识；在 rag_flow 中指 RAGFlow 内部租户。 |
| 6 | `token` | `character varying(255)` | 否 | `—` | API Token；属于敏感认证数据，禁止导出实际值。 |
| 7 | `dialog_id` | `character varying(32)` | 是 | `—` | dialog 的关联标识；外键约束以实际 Schema 为准。 |
| 8 | `source` | `character varying(16)` | 是 | `—` | RAGFlow v0.26.4 `api_token` 记录中的“source”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 9 | `beta` | `character varying(255)` | 是 | `—` | beta 敏感认证/连接字段；禁止导出真实数据。 |

### `canvas_template`

RAGFlow Canvas/Agent 编排模板。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `id` | `character varying(32)` | 否 | `—` | 记录主标识；具体长度/格式由所属表定义。 |
| 2 | `create_time` | `bigint` | 是 | `—` | RAGFlow 使用的创建时间 Unix 毫秒值。 |
| 3 | `create_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的创建时间戳。 |
| 4 | `update_time` | `bigint` | 是 | `—` | RAGFlow 使用的更新时间 Unix 毫秒值。 |
| 5 | `update_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的更新时间戳。 |
| 6 | `avatar` | `text` | 是 | `—` | RAGFlow v0.26.4 `canvas_template` 记录中的“avatar”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 7 | `title` | `text` | 是 | `—` | RAGFlow v0.26.4 `canvas_template` 记录中的“title”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 8 | `description` | `text` | 是 | `—` | 说明文本。 |
| 9 | `canvas_type` | `character varying(32)` | 是 | `—` | RAGFlow v0.26.4 `canvas_template` 记录中的“canvas type”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 10 | `canvas_types` | `text` | 是 | `—` | RAGFlow v0.26.4 `canvas_template` 记录中的“canvas types”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 11 | `canvas_category` | `character varying(32)` | 否 | `—` | RAGFlow v0.26.4 `canvas_template` 记录中的“canvas category”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 12 | `dsl` | `text` | 是 | `—` | RAGFlow v0.26.4 `canvas_template` 记录中的“dsl”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |

### `chat_channel`

聊天渠道与渠道接入配置。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `id` | `character varying(32)` | 否 | `—` | 记录主标识；具体长度/格式由所属表定义。 |
| 2 | `create_time` | `bigint` | 是 | `—` | RAGFlow 使用的创建时间 Unix 毫秒值。 |
| 3 | `create_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的创建时间戳。 |
| 4 | `update_time` | `bigint` | 是 | `—` | RAGFlow 使用的更新时间 Unix 毫秒值。 |
| 5 | `update_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的更新时间戳。 |
| 6 | `tenant_id` | `character varying(32)` | 否 | `—` | 所属租户标识；在 rag_flow 中指 RAGFlow 内部租户。 |
| 7 | `name` | `character varying(128)` | 否 | `—` | 面向用户或系统显示的名称。 |
| 8 | `channel` | `character varying(128)` | 否 | `—` | RAGFlow v0.26.4 `chat_channel` 记录中的“channel”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 9 | `config` | `text` | 否 | `—` | RAGFlow v0.26.4 `chat_channel` 记录中的“config”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 10 | `chat_id` | `character varying(32)` | 是 | `—` | chat 的关联标识；外键约束以实际 Schema 为准。 |
| 11 | `status` | `integer` | 否 | `—` | 当前业务状态；允许值由表上的 CHECK 约束或上游枚举控制。 |
| 12 | `dialog_id` | `character varying(32)` | 是 | `—` | dialog 的关联标识；外键约束以实际 Schema 为准。 |

### `compilation_template`

编译/生成模板定义。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `id` | `character varying(32)` | 否 | `—` | 记录主标识；具体长度/格式由所属表定义。 |
| 2 | `create_time` | `bigint` | 是 | `—` | RAGFlow 使用的创建时间 Unix 毫秒值。 |
| 3 | `create_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的创建时间戳。 |
| 4 | `update_time` | `bigint` | 是 | `—` | RAGFlow 使用的更新时间 Unix 毫秒值。 |
| 5 | `update_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的更新时间戳。 |
| 6 | `tenant_id` | `character varying(32)` | 是 | `—` | 所属租户标识；在 rag_flow 中指 RAGFlow 内部租户。 |
| 7 | `group_id` | `character varying(32)` | 是 | `—` | group 的关联标识；外键约束以实际 Schema 为准。 |
| 8 | `name` | `character varying(128)` | 否 | `—` | 面向用户或系统显示的名称。 |
| 9 | `description` | `text` | 是 | `—` | 说明文本。 |
| 10 | `kind` | `character varying(64)` | 否 | `—` | RAGFlow v0.26.4 `compilation_template` 记录中的“kind”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 11 | `config` | `text` | 否 | `—` | RAGFlow v0.26.4 `compilation_template` 记录中的“config”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 12 | `is_builtin` | `boolean` | 否 | `—` | 控制或记录“is builtin”状态。 |
| 13 | `status` | `character varying(1)` | 是 | `—` | 当前业务状态；允许值由表上的 CHECK 约束或上游枚举控制。 |

### `compilation_template_group`

编译模板分组。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `id` | `character varying(32)` | 否 | `—` | 记录主标识；具体长度/格式由所属表定义。 |
| 2 | `create_time` | `bigint` | 是 | `—` | RAGFlow 使用的创建时间 Unix 毫秒值。 |
| 3 | `create_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的创建时间戳。 |
| 4 | `update_time` | `bigint` | 是 | `—` | RAGFlow 使用的更新时间 Unix 毫秒值。 |
| 5 | `update_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的更新时间戳。 |
| 6 | `tenant_id` | `character varying(32)` | 否 | `—` | 所属租户标识；在 rag_flow 中指 RAGFlow 内部租户。 |
| 7 | `name` | `character varying(128)` | 否 | `—` | 面向用户或系统显示的名称。 |
| 8 | `description` | `text` | 是 | `—` | 说明文本。 |
| 9 | `scope` | `character varying(16)` | 否 | `—` | RAGFlow v0.26.4 `compilation_template_group` 记录中的“scope”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 10 | `status` | `character varying(1)` | 是 | `—` | 当前业务状态；允许值由表上的 CHECK 约束或上游枚举控制。 |

### `connector`

外部数据连接器定义。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `id` | `character varying(32)` | 否 | `—` | 记录主标识；具体长度/格式由所属表定义。 |
| 2 | `create_time` | `bigint` | 是 | `—` | RAGFlow 使用的创建时间 Unix 毫秒值。 |
| 3 | `create_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的创建时间戳。 |
| 4 | `update_time` | `bigint` | 是 | `—` | RAGFlow 使用的更新时间 Unix 毫秒值。 |
| 5 | `update_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的更新时间戳。 |
| 6 | `tenant_id` | `character varying(32)` | 否 | `—` | 所属租户标识；在 rag_flow 中指 RAGFlow 内部租户。 |
| 7 | `name` | `character varying(128)` | 否 | `—` | 面向用户或系统显示的名称。 |
| 8 | `source` | `character varying(128)` | 否 | `—` | RAGFlow v0.26.4 `connector` 记录中的“source”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 9 | `input_type` | `character varying(128)` | 否 | `—` | RAGFlow v0.26.4 `connector` 记录中的“input type”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 10 | `config` | `text` | 否 | `—` | RAGFlow v0.26.4 `connector` 记录中的“config”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 11 | `refresh_freq` | `integer` | 否 | `—` | RAGFlow v0.26.4 `connector` 记录中的“refresh freq”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 12 | `prune_freq` | `integer` | 否 | `—` | RAGFlow v0.26.4 `connector` 记录中的“prune freq”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 13 | `timeout_secs` | `integer` | 否 | `—` | RAGFlow v0.26.4 `connector` 记录中的“timeout secs”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 14 | `indexing_start` | `timestamp without time zone` | 是 | `—` | RAGFlow v0.26.4 `connector` 记录中的“indexing start”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 15 | `status` | `character varying(16)` | 是 | `—` | 当前业务状态；允许值由表上的 CHECK 约束或上游枚举控制。 |

### `connector2kb`

连接器与知识库的关联关系。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `id` | `character varying(32)` | 否 | `—` | 记录主标识；具体长度/格式由所属表定义。 |
| 2 | `create_time` | `bigint` | 是 | `—` | RAGFlow 使用的创建时间 Unix 毫秒值。 |
| 3 | `create_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的创建时间戳。 |
| 4 | `update_time` | `bigint` | 是 | `—` | RAGFlow 使用的更新时间 Unix 毫秒值。 |
| 5 | `update_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的更新时间戳。 |
| 6 | `connector_id` | `character varying(32)` | 否 | `—` | connector 的关联标识；外键约束以实际 Schema 为准。 |
| 7 | `kb_id` | `character varying(32)` | 否 | `—` | RAGFlow 知识库/数据集标识。 |
| 8 | `auto_parse` | `character varying(1)` | 否 | `—` | RAGFlow v0.26.4 `connector2kb` 记录中的“auto parse”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |

### `conversation`

RAGFlow 内部会话及消息历史。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `id` | `character varying(32)` | 否 | `—` | 记录主标识；具体长度/格式由所属表定义。 |
| 2 | `create_time` | `bigint` | 是 | `—` | RAGFlow 使用的创建时间 Unix 毫秒值。 |
| 3 | `create_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的创建时间戳。 |
| 4 | `update_time` | `bigint` | 是 | `—` | RAGFlow 使用的更新时间 Unix 毫秒值。 |
| 5 | `update_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的更新时间戳。 |
| 6 | `dialog_id` | `character varying(32)` | 否 | `—` | dialog 的关联标识；外键约束以实际 Schema 为准。 |
| 7 | `name` | `character varying(255)` | 是 | `—` | 面向用户或系统显示的名称。 |
| 8 | `message` | `text` | 是 | `—` | RAGFlow v0.26.4 `conversation` 记录中的“message”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 9 | `reference` | `text` | 是 | `—` | RAGFlow v0.26.4 `conversation` 记录中的“reference”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 10 | `user_id` | `character varying(255)` | 是 | `—` | 所属用户标识。 |

### `dialog`

RAGFlow Chat Assistant/Dialog 配置。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `id` | `character varying(32)` | 否 | `—` | 记录主标识；具体长度/格式由所属表定义。 |
| 2 | `create_time` | `bigint` | 是 | `—` | RAGFlow 使用的创建时间 Unix 毫秒值。 |
| 3 | `create_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的创建时间戳。 |
| 4 | `update_time` | `bigint` | 是 | `—` | RAGFlow 使用的更新时间 Unix 毫秒值。 |
| 5 | `update_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的更新时间戳。 |
| 6 | `tenant_id` | `character varying(32)` | 否 | `—` | 所属租户标识；在 rag_flow 中指 RAGFlow 内部租户。 |
| 7 | `name` | `character varying(255)` | 是 | `—` | 面向用户或系统显示的名称。 |
| 8 | `description` | `text` | 是 | `—` | 说明文本。 |
| 9 | `icon` | `text` | 是 | `—` | RAGFlow v0.26.4 `dialog` 记录中的“icon”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 10 | `language` | `character varying(32)` | 是 | `—` | RAGFlow v0.26.4 `dialog` 记录中的“language”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 11 | `llm_id` | `character varying(128)` | 否 | `—` | llm 的关联标识；外键约束以实际 Schema 为准。 |
| 12 | `tenant_llm_id` | `integer` | 是 | `—` | tenant llm 的关联标识；外键约束以实际 Schema 为准。 |
| 13 | `llm_setting` | `text` | 否 | `—` | RAGFlow v0.26.4 `dialog` 记录中的“llm setting”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 14 | `prompt_type` | `character varying(16)` | 否 | `—` | RAGFlow v0.26.4 `dialog` 记录中的“prompt type”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 15 | `prompt_config` | `text` | 否 | `—` | RAGFlow v0.26.4 `dialog` 记录中的“prompt config”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 16 | `meta_data_filter` | `text` | 是 | `—` | RAGFlow v0.26.4 `dialog` 记录中的“meta data filter”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 17 | `similarity_threshold` | `real` | 否 | `—` | RAGFlow v0.26.4 `dialog` 记录中的“similarity threshold”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 18 | `vector_similarity_weight` | `real` | 否 | `—` | RAGFlow v0.26.4 `dialog` 记录中的“vector similarity weight”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 19 | `top_n` | `integer` | 否 | `—` | RAGFlow v0.26.4 `dialog` 记录中的“top n”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 20 | `top_k` | `integer` | 否 | `—` | RAGFlow v0.26.4 `dialog` 记录中的“top k”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 21 | `do_refer` | `character varying(1)` | 否 | `—` | RAGFlow v0.26.4 `dialog` 记录中的“do refer”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 22 | `rerank_id` | `character varying(128)` | 否 | `—` | rerank 的关联标识；外键约束以实际 Schema 为准。 |
| 23 | `tenant_rerank_id` | `integer` | 是 | `—` | tenant rerank 的关联标识；外键约束以实际 Schema 为准。 |
| 24 | `kb_ids` | `text` | 否 | `—` | kb ids 标识集合。 |
| 25 | `status` | `character varying(1)` | 是 | `—` | 当前业务状态；允许值由表上的 CHECK 约束或上游枚举控制。 |

### `document`

RAGFlow 文档元数据、解析配置、进度、Chunk 统计和对象存储位置。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `id` | `character varying(32)` | 否 | `—` | 记录主标识；具体长度/格式由所属表定义。 |
| 2 | `create_time` | `bigint` | 是 | `—` | RAGFlow 使用的创建时间 Unix 毫秒值。 |
| 3 | `create_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的创建时间戳。 |
| 4 | `update_time` | `bigint` | 是 | `—` | RAGFlow 使用的更新时间 Unix 毫秒值。 |
| 5 | `update_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的更新时间戳。 |
| 6 | `thumbnail` | `text` | 是 | `—` | RAGFlow v0.26.4 `document` 记录中的“thumbnail”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 7 | `kb_id` | `character varying(256)` | 否 | `—` | RAGFlow 知识库/数据集标识。 |
| 8 | `parser_id` | `character varying(32)` | 否 | `—` | RAGFlow 解析模板/解析方法标识。 |
| 9 | `pipeline_id` | `character varying(32)` | 是 | `—` | pipeline 的关联标识；外键约束以实际 Schema 为准。 |
| 10 | `parser_config` | `text` | 否 | `—` | RAGFlow Parser/Chunk 参数 JSON。 |
| 11 | `source_type` | `character varying(128)` | 否 | `—` | RAGFlow v0.26.4 `document` 记录中的“source type”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 12 | `type` | `character varying(32)` | 否 | `—` | RAGFlow v0.26.4 `document` 记录中的“type”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 13 | `created_by` | `character varying(32)` | 否 | `—` | RAGFlow v0.26.4 `document` 记录中的“created by”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 14 | `name` | `character varying(255)` | 是 | `—` | 面向用户或系统显示的名称。 |
| 15 | `location` | `character varying(255)` | 是 | `—` | 文件或对象的存储位置。 |
| 16 | `size` | `bigint` | 否 | `—` | 对象或文件大小。 |
| 17 | `token_num` | `integer` | 否 | `—` | RAGFlow v0.26.4 `document` 记录中的“token num”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 18 | `chunk_num` | `integer` | 否 | `—` | RAGFlow v0.26.4 `document` 记录中的“chunk num”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 19 | `progress` | `real` | 否 | `—` | 任务或解析进度，通常为 0 到 1 的比例。 |
| 20 | `progress_msg` | `text` | 是 | `—` | RAGFlow v0.26.4 `document` 记录中的“progress msg”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 21 | `process_begin_at` | `timestamp without time zone` | 是 | `—` | process begin 的时间戳。 |
| 22 | `process_duration` | `real` | 否 | `—` | RAGFlow v0.26.4 `document` 记录中的“process duration”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 23 | `suffix` | `character varying(32)` | 否 | `—` | RAGFlow v0.26.4 `document` 记录中的“suffix”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 24 | `content_hash` | `character varying(32)` | 是 | `—` | RAGFlow v0.26.4 `document` 记录中的“content hash”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 25 | `run` | `character varying(1)` | 是 | `—` | RAGFlow v0.26.4 `document` 记录中的“run”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 26 | `status` | `character varying(1)` | 是 | `—` | 当前业务状态；允许值由表上的 CHECK 约束或上游枚举控制。 |

### `evaluation_cases`

评测数据集中的单条问题/期望结果。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `id` | `character varying(32)` | 否 | `—` | 记录主标识；具体长度/格式由所属表定义。 |
| 2 | `create_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的创建时间戳。 |
| 3 | `update_time` | `bigint` | 是 | `—` | RAGFlow 使用的更新时间 Unix 毫秒值。 |
| 4 | `update_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的更新时间戳。 |
| 5 | `dataset_id` | `character varying(32)` | 否 | `—` | RAGFlow 数据集/知识库标识。 |
| 6 | `question` | `text` | 否 | `—` | RAGFlow v0.26.4 `evaluation_cases` 记录中的“question”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 7 | `reference_answer` | `text` | 是 | `—` | RAGFlow v0.26.4 `evaluation_cases` 记录中的“reference answer”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 8 | `relevant_doc_ids` | `text` | 是 | `—` | relevant doc ids 标识集合。 |
| 9 | `relevant_chunk_ids` | `text` | 是 | `—` | relevant chunk ids 标识集合。 |
| 10 | `metadata` | `text` | 是 | `—` | 上游 Metadata 或配置数据。 |
| 11 | `create_time` | `bigint` | 否 | `—` | RAGFlow 使用的创建时间 Unix 毫秒值。 |

### `evaluation_datasets`

评测数据集定义。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `id` | `character varying(32)` | 否 | `—` | 记录主标识；具体长度/格式由所属表定义。 |
| 2 | `create_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的创建时间戳。 |
| 3 | `update_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的更新时间戳。 |
| 4 | `tenant_id` | `character varying(32)` | 否 | `—` | 所属租户标识；在 rag_flow 中指 RAGFlow 内部租户。 |
| 5 | `name` | `character varying(255)` | 否 | `—` | 面向用户或系统显示的名称。 |
| 6 | `description` | `text` | 是 | `—` | 说明文本。 |
| 7 | `kb_ids` | `text` | 否 | `—` | kb ids 标识集合。 |
| 8 | `created_by` | `character varying(32)` | 否 | `—` | RAGFlow v0.26.4 `evaluation_datasets` 记录中的“created by”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 9 | `create_time` | `bigint` | 否 | `—` | RAGFlow 使用的创建时间 Unix 毫秒值。 |
| 10 | `update_time` | `bigint` | 否 | `—` | RAGFlow 使用的更新时间 Unix 毫秒值。 |
| 11 | `status` | `integer` | 否 | `—` | 当前业务状态；允许值由表上的 CHECK 约束或上游枚举控制。 |

### `evaluation_results`

评测运行的逐条结果。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `id` | `character varying(32)` | 否 | `—` | 记录主标识；具体长度/格式由所属表定义。 |
| 2 | `create_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的创建时间戳。 |
| 3 | `update_time` | `bigint` | 是 | `—` | RAGFlow 使用的更新时间 Unix 毫秒值。 |
| 4 | `update_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的更新时间戳。 |
| 5 | `run_id` | `character varying(32)` | 否 | `—` | run 的关联标识；外键约束以实际 Schema 为准。 |
| 6 | `case_id` | `character varying(32)` | 否 | `—` | case 的关联标识；外键约束以实际 Schema 为准。 |
| 7 | `generated_answer` | `text` | 否 | `—` | RAGFlow v0.26.4 `evaluation_results` 记录中的“generated answer”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 8 | `retrieved_chunks` | `text` | 否 | `—` | RAGFlow v0.26.4 `evaluation_results` 记录中的“retrieved chunks”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 9 | `metrics` | `text` | 否 | `—` | RAGFlow v0.26.4 `evaluation_results` 记录中的“metrics”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 10 | `execution_time` | `real` | 否 | `—` | execution 的时间值。 |
| 11 | `token_usage` | `text` | 是 | `—` | RAGFlow v0.26.4 `evaluation_results` 记录中的“token usage”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 12 | `create_time` | `bigint` | 否 | `—` | RAGFlow 使用的创建时间 Unix 毫秒值。 |

### `evaluation_runs`

评测运行批次与状态。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `id` | `character varying(32)` | 否 | `—` | 记录主标识；具体长度/格式由所属表定义。 |
| 2 | `create_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的创建时间戳。 |
| 3 | `update_time` | `bigint` | 是 | `—` | RAGFlow 使用的更新时间 Unix 毫秒值。 |
| 4 | `update_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的更新时间戳。 |
| 5 | `dataset_id` | `character varying(32)` | 否 | `—` | RAGFlow 数据集/知识库标识。 |
| 6 | `dialog_id` | `character varying(32)` | 否 | `—` | dialog 的关联标识；外键约束以实际 Schema 为准。 |
| 7 | `name` | `character varying(255)` | 否 | `—` | 面向用户或系统显示的名称。 |
| 8 | `config_snapshot` | `text` | 否 | `—` | RAGFlow v0.26.4 `evaluation_runs` 记录中的“config snapshot”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 9 | `metrics_summary` | `text` | 是 | `—` | RAGFlow v0.26.4 `evaluation_runs` 记录中的“metrics summary”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 10 | `status` | `character varying(32)` | 否 | `—` | 当前业务状态；允许值由表上的 CHECK 约束或上游枚举控制。 |
| 11 | `created_by` | `character varying(32)` | 否 | `—` | RAGFlow v0.26.4 `evaluation_runs` 记录中的“created by”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 12 | `create_time` | `bigint` | 否 | `—` | RAGFlow 使用的创建时间 Unix 毫秒值。 |
| 13 | `complete_time` | `bigint` | 是 | `—` | complete 的时间值。 |

### `file`

RAGFlow 文件树中的文件/目录元数据。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `id` | `character varying(32)` | 否 | `—` | 记录主标识；具体长度/格式由所属表定义。 |
| 2 | `create_time` | `bigint` | 是 | `—` | RAGFlow 使用的创建时间 Unix 毫秒值。 |
| 3 | `create_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的创建时间戳。 |
| 4 | `update_time` | `bigint` | 是 | `—` | RAGFlow 使用的更新时间 Unix 毫秒值。 |
| 5 | `update_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的更新时间戳。 |
| 6 | `parent_id` | `character varying(32)` | 否 | `—` | parent 的关联标识；外键约束以实际 Schema 为准。 |
| 7 | `tenant_id` | `character varying(32)` | 否 | `—` | 所属租户标识；在 rag_flow 中指 RAGFlow 内部租户。 |
| 8 | `created_by` | `character varying(32)` | 否 | `—` | RAGFlow v0.26.4 `file` 记录中的“created by”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 9 | `name` | `character varying(255)` | 否 | `—` | 面向用户或系统显示的名称。 |
| 10 | `location` | `character varying(255)` | 是 | `—` | 文件或对象的存储位置。 |
| 11 | `size` | `bigint` | 否 | `—` | 对象或文件大小。 |
| 12 | `type` | `character varying(32)` | 否 | `—` | RAGFlow v0.26.4 `file` 记录中的“type”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 13 | `source_type` | `character varying(128)` | 否 | `—` | RAGFlow v0.26.4 `file` 记录中的“source type”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |

### `file2document`

文件记录与文档记录的映射。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `id` | `character varying(32)` | 否 | `—` | 记录主标识；具体长度/格式由所属表定义。 |
| 2 | `create_time` | `bigint` | 是 | `—` | RAGFlow 使用的创建时间 Unix 毫秒值。 |
| 3 | `create_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的创建时间戳。 |
| 4 | `update_time` | `bigint` | 是 | `—` | RAGFlow 使用的更新时间 Unix 毫秒值。 |
| 5 | `update_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的更新时间戳。 |
| 6 | `file_id` | `character varying(32)` | 是 | `—` | file 的关联标识；外键约束以实际 Schema 为准。 |
| 7 | `document_id` | `character varying(32)` | 是 | `—` | 文档标识或文档外键。 |

### `file_commit`

文件同步或提交批次。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `id` | `character varying(32)` | 否 | `—` | 记录主标识；具体长度/格式由所属表定义。 |
| 2 | `create_time` | `bigint` | 是 | `—` | RAGFlow 使用的创建时间 Unix 毫秒值。 |
| 3 | `create_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的创建时间戳。 |
| 4 | `update_time` | `bigint` | 是 | `—` | RAGFlow 使用的更新时间 Unix 毫秒值。 |
| 5 | `update_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的更新时间戳。 |
| 6 | `folder_id` | `character varying(32)` | 否 | `—` | folder 的关联标识；外键约束以实际 Schema 为准。 |
| 7 | `parent_id` | `character varying(32)` | 是 | `—` | parent 的关联标识；外键约束以实际 Schema 为准。 |
| 8 | `message` | `character varying(512)` | 否 | `—` | RAGFlow v0.26.4 `file_commit` 记录中的“message”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 9 | `author_id` | `character varying(32)` | 否 | `—` | author 的关联标识；外键约束以实际 Schema 为准。 |
| 10 | `file_count` | `integer` | 否 | `—` | file count 的计数或度量值。 |
| 11 | `tree_state` | `text` | 是 | `—` | RAGFlow v0.26.4 `file_commit` 记录中的“tree state”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 12 | `title` | `character varying(255)` | 是 | `—` | RAGFlow v0.26.4 `file_commit` 记录中的“title”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 13 | `comments` | `text` | 是 | `—` | RAGFlow v0.26.4 `file_commit` 记录中的“comments”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |

### `file_commit_item`

文件提交批次中的单项明细。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `id` | `character varying(32)` | 否 | `—` | 记录主标识；具体长度/格式由所属表定义。 |
| 2 | `create_time` | `bigint` | 是 | `—` | RAGFlow 使用的创建时间 Unix 毫秒值。 |
| 3 | `create_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的创建时间戳。 |
| 4 | `update_time` | `bigint` | 是 | `—` | RAGFlow 使用的更新时间 Unix 毫秒值。 |
| 5 | `update_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的更新时间戳。 |
| 6 | `commit_id` | `character varying(32)` | 否 | `—` | commit 的关联标识；外键约束以实际 Schema 为准。 |
| 7 | `file_id` | `character varying(32)` | 否 | `—` | file 的关联标识；外键约束以实际 Schema 为准。 |
| 8 | `operation` | `character varying(16)` | 否 | `—` | RAGFlow v0.26.4 `file_commit_item` 记录中的“operation”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 9 | `old_hash` | `character varying(64)` | 是 | `—` | RAGFlow v0.26.4 `file_commit_item` 记录中的“old hash”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 10 | `new_hash` | `character varying(64)` | 是 | `—` | RAGFlow v0.26.4 `file_commit_item` 记录中的“new hash”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 11 | `old_location` | `character varying(255)` | 是 | `—` | RAGFlow v0.26.4 `file_commit_item` 记录中的“old location”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 12 | `new_location` | `character varying(255)` | 是 | `—` | RAGFlow v0.26.4 `file_commit_item` 记录中的“new location”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 13 | `old_name` | `character varying(255)` | 是 | `—` | RAGFlow v0.26.4 `file_commit_item` 记录中的“old name”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 14 | `new_name` | `character varying(255)` | 是 | `—` | RAGFlow v0.26.4 `file_commit_item` 记录中的“new name”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 15 | `diff` | `text` | 是 | `—` | RAGFlow v0.26.4 `file_commit_item` 记录中的“diff”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 16 | `content_after_storage` | `character varying(16)` | 是 | `—` | RAGFlow v0.26.4 `file_commit_item` 记录中的“content after storage”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 17 | `content_after_location` | `character varying(512)` | 是 | `—` | RAGFlow v0.26.4 `file_commit_item` 记录中的“content after location”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 18 | `slug_kwd` | `character varying(512)` | 是 | `—` | RAGFlow v0.26.4 `file_commit_item` 记录中的“slug kwd”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 19 | `page_type_kwd` | `character varying(32)` | 是 | `—` | RAGFlow v0.26.4 `file_commit_item` 记录中的“page type kwd”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |

### `invitation_code`

租户邀请注册信息。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `id` | `character varying(32)` | 否 | `—` | 记录主标识；具体长度/格式由所属表定义。 |
| 2 | `create_time` | `bigint` | 是 | `—` | RAGFlow 使用的创建时间 Unix 毫秒值。 |
| 3 | `create_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的创建时间戳。 |
| 4 | `update_time` | `bigint` | 是 | `—` | RAGFlow 使用的更新时间 Unix 毫秒值。 |
| 5 | `update_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的更新时间戳。 |
| 6 | `code` | `character varying(32)` | 否 | `—` | RAGFlow v0.26.4 `invitation_code` 记录中的“code”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 7 | `visit_time` | `timestamp without time zone` | 是 | `—` | visit 的时间值。 |
| 8 | `user_id` | `character varying(32)` | 是 | `—` | 所属用户标识。 |
| 9 | `tenant_id` | `character varying(32)` | 是 | `—` | 所属租户标识；在 rag_flow 中指 RAGFlow 内部租户。 |
| 10 | `status` | `character varying(1)` | 是 | `—` | 当前业务状态；允许值由表上的 CHECK 约束或上游枚举控制。 |

### `knowledgebase`

RAGFlow 知识库元数据、Embedding、Parser 和权限配置。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `id` | `character varying(32)` | 否 | `—` | 记录主标识；具体长度/格式由所属表定义。 |
| 2 | `create_time` | `bigint` | 是 | `—` | RAGFlow 使用的创建时间 Unix 毫秒值。 |
| 3 | `create_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的创建时间戳。 |
| 4 | `update_time` | `bigint` | 是 | `—` | RAGFlow 使用的更新时间 Unix 毫秒值。 |
| 5 | `update_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的更新时间戳。 |
| 6 | `avatar` | `text` | 是 | `—` | RAGFlow v0.26.4 `knowledgebase` 记录中的“avatar”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 7 | `tenant_id` | `character varying(32)` | 否 | `—` | 所属租户标识；在 rag_flow 中指 RAGFlow 内部租户。 |
| 8 | `name` | `character varying(128)` | 否 | `—` | 面向用户或系统显示的名称。 |
| 9 | `language` | `character varying(32)` | 是 | `—` | RAGFlow v0.26.4 `knowledgebase` 记录中的“language”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 10 | `description` | `text` | 是 | `—` | 说明文本。 |
| 11 | `embd_id` | `character varying(128)` | 否 | `—` | embd 的关联标识；外键约束以实际 Schema 为准。 |
| 12 | `tenant_embd_id` | `integer` | 是 | `—` | tenant embd 的关联标识；外键约束以实际 Schema 为准。 |
| 13 | `permission` | `character varying(16)` | 否 | `—` | RAGFlow v0.26.4 `knowledgebase` 记录中的“permission”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 14 | `created_by` | `character varying(32)` | 否 | `—` | RAGFlow v0.26.4 `knowledgebase` 记录中的“created by”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 15 | `doc_num` | `integer` | 否 | `—` | RAGFlow v0.26.4 `knowledgebase` 记录中的“doc num”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 16 | `token_num` | `integer` | 否 | `—` | RAGFlow v0.26.4 `knowledgebase` 记录中的“token num”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 17 | `chunk_num` | `integer` | 否 | `—` | RAGFlow v0.26.4 `knowledgebase` 记录中的“chunk num”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 18 | `similarity_threshold` | `real` | 否 | `—` | RAGFlow v0.26.4 `knowledgebase` 记录中的“similarity threshold”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 19 | `vector_similarity_weight` | `real` | 否 | `—` | RAGFlow v0.26.4 `knowledgebase` 记录中的“vector similarity weight”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 20 | `parser_id` | `character varying(32)` | 否 | `—` | RAGFlow 解析模板/解析方法标识。 |
| 21 | `pipeline_id` | `character varying(32)` | 是 | `—` | pipeline 的关联标识；外键约束以实际 Schema 为准。 |
| 22 | `parser_config` | `text` | 否 | `—` | RAGFlow Parser/Chunk 参数 JSON。 |
| 23 | `pagerank` | `integer` | 否 | `—` | RAGFlow v0.26.4 `knowledgebase` 记录中的“pagerank”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 24 | `graphrag_task_id` | `character varying(32)` | 是 | `—` | graphrag task 的关联标识；外键约束以实际 Schema 为准。 |
| 25 | `graphrag_task_finish_at` | `timestamp without time zone` | 是 | `—` | graphrag task finish 的时间戳。 |
| 26 | `raptor_task_id` | `character varying(32)` | 是 | `—` | raptor task 的关联标识；外键约束以实际 Schema 为准。 |
| 27 | `raptor_task_finish_at` | `timestamp without time zone` | 是 | `—` | raptor task finish 的时间戳。 |
| 28 | `mindmap_task_id` | `character varying(32)` | 是 | `—` | mindmap task 的关联标识；外键约束以实际 Schema 为准。 |
| 29 | `mindmap_task_finish_at` | `timestamp without time zone` | 是 | `—` | mindmap task finish 的时间戳。 |
| 30 | `artifact_task_id` | `character varying(32)` | 是 | `—` | artifact task 的关联标识；外键约束以实际 Schema 为准。 |
| 31 | `artifact_task_finish_at` | `timestamp without time zone` | 是 | `—` | artifact task finish 的时间戳。 |
| 32 | `skill_task_id` | `character varying(32)` | 是 | `—` | skill task 的关联标识；外键约束以实际 Schema 为准。 |
| 33 | `skill_task_finish_at` | `timestamp without time zone` | 是 | `—` | skill task finish 的时间戳。 |
| 34 | `status` | `character varying(1)` | 是 | `—` | 当前业务状态；允许值由表上的 CHECK 约束或上游枚举控制。 |

### `llm`

历史/兼容模型定义。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `create_time` | `bigint` | 是 | `—` | RAGFlow 使用的创建时间 Unix 毫秒值。 |
| 2 | `create_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的创建时间戳。 |
| 3 | `update_time` | `bigint` | 是 | `—` | RAGFlow 使用的更新时间 Unix 毫秒值。 |
| 4 | `update_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的更新时间戳。 |
| 5 | `llm_name` | `character varying(128)` | 否 | `—` | RAGFlow v0.26.4 `llm` 记录中的“llm name”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 6 | `model_type` | `character varying(128)` | 否 | `—` | RAGFlow v0.26.4 `llm` 记录中的“model type”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 7 | `fid` | `character varying(128)` | 否 | `—` | RAGFlow v0.26.4 `llm` 记录中的“fid”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 8 | `max_tokens` | `integer` | 否 | `—` | RAGFlow v0.26.4 `llm` 记录中的“max tokens”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 9 | `tags` | `character varying(255)` | 否 | `—` | RAGFlow v0.26.4 `llm` 记录中的“tags”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 10 | `is_tools` | `boolean` | 否 | `—` | 控制或记录“is tools”状态。 |
| 11 | `status` | `character varying(1)` | 是 | `—` | 当前业务状态；允许值由表上的 CHECK 约束或上游枚举控制。 |

### `llm_factories`

模型供应商工厂元数据。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `name` | `character varying(128)` | 否 | `—` | 面向用户或系统显示的名称。 |
| 2 | `create_time` | `bigint` | 是 | `—` | RAGFlow 使用的创建时间 Unix 毫秒值。 |
| 3 | `create_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的创建时间戳。 |
| 4 | `update_time` | `bigint` | 是 | `—` | RAGFlow 使用的更新时间 Unix 毫秒值。 |
| 5 | `update_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的更新时间戳。 |
| 6 | `logo` | `text` | 是 | `—` | RAGFlow v0.26.4 `llm_factories` 记录中的“logo”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 7 | `tags` | `character varying(255)` | 否 | `—` | RAGFlow v0.26.4 `llm_factories` 记录中的“tags”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 8 | `rank` | `integer` | 否 | `—` | RAGFlow v0.26.4 `llm_factories` 记录中的“rank”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 9 | `status` | `character varying(1)` | 是 | `—` | 当前业务状态；允许值由表上的 CHECK 约束或上游枚举控制。 |

### `mcp_server`

RAGFlow 内部 MCP Server 配置。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `id` | `character varying(32)` | 否 | `—` | 记录主标识；具体长度/格式由所属表定义。 |
| 2 | `create_time` | `bigint` | 是 | `—` | RAGFlow 使用的创建时间 Unix 毫秒值。 |
| 3 | `create_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的创建时间戳。 |
| 4 | `update_time` | `bigint` | 是 | `—` | RAGFlow 使用的更新时间 Unix 毫秒值。 |
| 5 | `update_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的更新时间戳。 |
| 6 | `name` | `character varying(255)` | 否 | `—` | 面向用户或系统显示的名称。 |
| 7 | `tenant_id` | `character varying(32)` | 否 | `—` | 所属租户标识；在 rag_flow 中指 RAGFlow 内部租户。 |
| 8 | `url` | `character varying(2048)` | 否 | `—` | url 地址。 |
| 9 | `server_type` | `character varying(32)` | 否 | `—` | RAGFlow v0.26.4 `mcp_server` 记录中的“server type”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 10 | `description` | `text` | 是 | `—` | 说明文本。 |
| 11 | `variables` | `text` | 是 | `—` | RAGFlow v0.26.4 `mcp_server` 记录中的“variables”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 12 | `headers` | `text` | 是 | `—` | RAGFlow v0.26.4 `mcp_server` 记录中的“headers”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |

### `memory`

RAGFlow 自带 Memory 功能的元数据；不等同于当前 Gateway 的问答历史。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `id` | `character varying(32)` | 否 | `—` | 记录主标识；具体长度/格式由所属表定义。 |
| 2 | `create_time` | `bigint` | 是 | `—` | RAGFlow 使用的创建时间 Unix 毫秒值。 |
| 3 | `create_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的创建时间戳。 |
| 4 | `update_time` | `bigint` | 是 | `—` | RAGFlow 使用的更新时间 Unix 毫秒值。 |
| 5 | `update_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的更新时间戳。 |
| 6 | `name` | `character varying(128)` | 否 | `—` | 面向用户或系统显示的名称。 |
| 7 | `avatar` | `text` | 是 | `—` | RAGFlow v0.26.4 `memory` 记录中的“avatar”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 8 | `tenant_id` | `character varying(32)` | 否 | `—` | 所属租户标识；在 rag_flow 中指 RAGFlow 内部租户。 |
| 9 | `memory_type` | `integer` | 否 | `—` | RAGFlow v0.26.4 `memory` 记录中的“memory type”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 10 | `storage_type` | `character varying(32)` | 否 | `—` | RAGFlow v0.26.4 `memory` 记录中的“storage type”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 11 | `embd_id` | `character varying(128)` | 否 | `—` | embd 的关联标识；外键约束以实际 Schema 为准。 |
| 12 | `tenant_embd_id` | `integer` | 是 | `—` | tenant embd 的关联标识；外键约束以实际 Schema 为准。 |
| 13 | `llm_id` | `character varying(128)` | 否 | `—` | llm 的关联标识；外键约束以实际 Schema 为准。 |
| 14 | `tenant_llm_id` | `integer` | 是 | `—` | tenant llm 的关联标识；外键约束以实际 Schema 为准。 |
| 15 | `permissions` | `character varying(16)` | 否 | `—` | RAGFlow v0.26.4 `memory` 记录中的“permissions”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 16 | `description` | `text` | 是 | `—` | 说明文本。 |
| 17 | `memory_size` | `integer` | 否 | `—` | RAGFlow v0.26.4 `memory` 记录中的“memory size”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 18 | `forgetting_policy` | `character varying(32)` | 否 | `—` | RAGFlow v0.26.4 `memory` 记录中的“forgetting policy”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 19 | `temperature` | `real` | 否 | `—` | RAGFlow v0.26.4 `memory` 记录中的“temperature”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 20 | `system_prompt` | `text` | 是 | `—` | RAGFlow v0.26.4 `memory` 记录中的“system prompt”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 21 | `user_prompt` | `text` | 是 | `—` | RAGFlow v0.26.4 `memory` 记录中的“user prompt”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |

### `pipeline_operation_log`

Pipeline 操作审计日志。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `id` | `character varying(32)` | 否 | `—` | 记录主标识；具体长度/格式由所属表定义。 |
| 2 | `create_time` | `bigint` | 是 | `—` | RAGFlow 使用的创建时间 Unix 毫秒值。 |
| 3 | `create_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的创建时间戳。 |
| 4 | `update_time` | `bigint` | 是 | `—` | RAGFlow 使用的更新时间 Unix 毫秒值。 |
| 5 | `update_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的更新时间戳。 |
| 6 | `document_id` | `character varying(32)` | 否 | `—` | 文档标识或文档外键。 |
| 7 | `tenant_id` | `character varying(32)` | 否 | `—` | 所属租户标识；在 rag_flow 中指 RAGFlow 内部租户。 |
| 8 | `kb_id` | `character varying(32)` | 否 | `—` | RAGFlow 知识库/数据集标识。 |
| 9 | `pipeline_id` | `character varying(32)` | 是 | `—` | pipeline 的关联标识；外键约束以实际 Schema 为准。 |
| 10 | `pipeline_title` | `character varying(32)` | 是 | `—` | RAGFlow v0.26.4 `pipeline_operation_log` 记录中的“pipeline title”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 11 | `parser_id` | `character varying(32)` | 否 | `—` | RAGFlow 解析模板/解析方法标识。 |
| 12 | `document_name` | `character varying(255)` | 否 | `—` | RAGFlow v0.26.4 `pipeline_operation_log` 记录中的“document name”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 13 | `document_suffix` | `character varying(255)` | 否 | `—` | RAGFlow v0.26.4 `pipeline_operation_log` 记录中的“document suffix”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 14 | `document_type` | `character varying(255)` | 否 | `—` | RAGFlow v0.26.4 `pipeline_operation_log` 记录中的“document type”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 15 | `source_from` | `character varying(255)` | 否 | `—` | RAGFlow v0.26.4 `pipeline_operation_log` 记录中的“source from”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 16 | `progress` | `real` | 否 | `—` | 任务或解析进度，通常为 0 到 1 的比例。 |
| 17 | `progress_msg` | `text` | 是 | `—` | RAGFlow v0.26.4 `pipeline_operation_log` 记录中的“progress msg”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 18 | `process_begin_at` | `timestamp without time zone` | 是 | `—` | process begin 的时间戳。 |
| 19 | `process_duration` | `real` | 否 | `—` | RAGFlow v0.26.4 `pipeline_operation_log` 记录中的“process duration”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 20 | `dsl` | `text` | 是 | `—` | RAGFlow v0.26.4 `pipeline_operation_log` 记录中的“dsl”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 21 | `task_type` | `character varying(32)` | 否 | `—` | RAGFlow v0.26.4 `pipeline_operation_log` 记录中的“task type”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 22 | `operation_status` | `character varying(32)` | 否 | `—` | RAGFlow v0.26.4 `pipeline_operation_log` 记录中的“operation status”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 23 | `avatar` | `text` | 是 | `—` | RAGFlow v0.26.4 `pipeline_operation_log` 记录中的“avatar”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 24 | `status` | `character varying(1)` | 是 | `—` | 当前业务状态；允许值由表上的 CHECK 约束或上游枚举控制。 |

### `search`

RAGFlow Search 配置。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `id` | `character varying(32)` | 否 | `—` | 记录主标识；具体长度/格式由所属表定义。 |
| 2 | `create_time` | `bigint` | 是 | `—` | RAGFlow 使用的创建时间 Unix 毫秒值。 |
| 3 | `create_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的创建时间戳。 |
| 4 | `update_time` | `bigint` | 是 | `—` | RAGFlow 使用的更新时间 Unix 毫秒值。 |
| 5 | `update_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的更新时间戳。 |
| 6 | `avatar` | `text` | 是 | `—` | RAGFlow v0.26.4 `search` 记录中的“avatar”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 7 | `tenant_id` | `character varying(32)` | 否 | `—` | 所属租户标识；在 rag_flow 中指 RAGFlow 内部租户。 |
| 8 | `name` | `character varying(128)` | 否 | `—` | 面向用户或系统显示的名称。 |
| 9 | `description` | `text` | 是 | `—` | 说明文本。 |
| 10 | `created_by` | `character varying(32)` | 否 | `—` | RAGFlow v0.26.4 `search` 记录中的“created by”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 11 | `search_config` | `text` | 否 | `—` | RAGFlow v0.26.4 `search` 记录中的“search config”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 12 | `status` | `character varying(1)` | 是 | `—` | 当前业务状态；允许值由表上的 CHECK 约束或上游枚举控制。 |

### `sync_logs`

连接器/文件同步日志。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `id` | `character varying(32)` | 否 | `—` | 记录主标识；具体长度/格式由所属表定义。 |
| 2 | `create_time` | `bigint` | 是 | `—` | RAGFlow 使用的创建时间 Unix 毫秒值。 |
| 3 | `create_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的创建时间戳。 |
| 4 | `update_time` | `bigint` | 是 | `—` | RAGFlow 使用的更新时间 Unix 毫秒值。 |
| 5 | `update_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的更新时间戳。 |
| 6 | `connector_id` | `character varying(32)` | 否 | `—` | connector 的关联标识；外键约束以实际 Schema 为准。 |
| 7 | `task_type` | `character varying(32)` | 否 | `—` | RAGFlow v0.26.4 `sync_logs` 记录中的“task type”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 8 | `status` | `character varying(128)` | 否 | `—` | 当前业务状态；允许值由表上的 CHECK 约束或上游枚举控制。 |
| 9 | `from_beginning` | `character varying(1)` | 是 | `—` | RAGFlow v0.26.4 `sync_logs` 记录中的“from beginning”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 10 | `new_docs_indexed` | `integer` | 否 | `—` | RAGFlow v0.26.4 `sync_logs` 记录中的“new docs indexed”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 11 | `total_docs_indexed` | `integer` | 否 | `—` | RAGFlow v0.26.4 `sync_logs` 记录中的“total docs indexed”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 12 | `docs_removed_from_index` | `integer` | 否 | `—` | RAGFlow v0.26.4 `sync_logs` 记录中的“docs removed from index”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 13 | `error_msg` | `text` | 否 | `—` | RAGFlow v0.26.4 `sync_logs` 记录中的“error msg”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 14 | `error_count` | `integer` | 否 | `—` | error count 的计数或度量值。 |
| 15 | `full_exception_trace` | `text` | 是 | `—` | RAGFlow v0.26.4 `sync_logs` 记录中的“full exception trace”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 16 | `time_started` | `timestamp without time zone` | 是 | `—` | RAGFlow v0.26.4 `sync_logs` 记录中的“time started”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 17 | `poll_range_start` | `character varying(255)` | 是 | `—` | RAGFlow v0.26.4 `sync_logs` 记录中的“poll range start”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 18 | `poll_range_end` | `character varying(255)` | 是 | `—` | RAGFlow v0.26.4 `sync_logs` 记录中的“poll range end”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 19 | `kb_id` | `character varying(32)` | 否 | `—` | RAGFlow 知识库/数据集标识。 |

### `system_settings`

RAGFlow 系统级设置；可能包含加密或敏感配置，禁止导出数据到 Git。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `name` | `character varying(128)` | 否 | `—` | 面向用户或系统显示的名称。 |
| 2 | `create_time` | `bigint` | 是 | `—` | RAGFlow 使用的创建时间 Unix 毫秒值。 |
| 3 | `create_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的创建时间戳。 |
| 4 | `update_time` | `bigint` | 是 | `—` | RAGFlow 使用的更新时间 Unix 毫秒值。 |
| 5 | `update_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的更新时间戳。 |
| 6 | `source` | `character varying(32)` | 否 | `—` | RAGFlow v0.26.4 `system_settings` 记录中的“source”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 7 | `data_type` | `character varying(32)` | 否 | `—` | RAGFlow v0.26.4 `system_settings` 记录中的“data type”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 8 | `value` | `text` | 否 | `—` | RAGFlow v0.26.4 `system_settings` 记录中的“value”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |

### `task`

RAGFlow 文档解析/Embedding 任务及进度。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `id` | `character varying(32)` | 否 | `—` | 记录主标识；具体长度/格式由所属表定义。 |
| 2 | `create_time` | `bigint` | 是 | `—` | RAGFlow 使用的创建时间 Unix 毫秒值。 |
| 3 | `create_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的创建时间戳。 |
| 4 | `update_time` | `bigint` | 是 | `—` | RAGFlow 使用的更新时间 Unix 毫秒值。 |
| 5 | `update_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的更新时间戳。 |
| 6 | `doc_id` | `character varying(32)` | 否 | `—` | RAGFlow 文档标识。 |
| 7 | `from_page` | `integer` | 否 | `—` | RAGFlow v0.26.4 `task` 记录中的“from page”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 8 | `to_page` | `integer` | 否 | `—` | RAGFlow v0.26.4 `task` 记录中的“to page”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 9 | `task_type` | `character varying(32)` | 否 | `—` | RAGFlow v0.26.4 `task` 记录中的“task type”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 10 | `priority` | `integer` | 否 | `—` | RAGFlow v0.26.4 `task` 记录中的“priority”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 11 | `begin_at` | `timestamp without time zone` | 是 | `—` | begin 的时间戳。 |
| 12 | `process_duration` | `real` | 否 | `—` | RAGFlow v0.26.4 `task` 记录中的“process duration”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 13 | `progress` | `real` | 否 | `—` | 任务或解析进度，通常为 0 到 1 的比例。 |
| 14 | `progress_msg` | `text` | 是 | `—` | RAGFlow v0.26.4 `task` 记录中的“progress msg”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 15 | `retry_count` | `integer` | 否 | `—` | retry count 的计数或度量值。 |
| 16 | `digest` | `text` | 是 | `—` | RAGFlow v0.26.4 `task` 记录中的“digest”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 17 | `chunk_ids` | `text` | 是 | `—` | chunk ids 标识集合。 |

### `tenant`

RAGFlow 内部租户。它与 Gateway 的 X-Tenant-ID 隔离键不是同一概念。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `id` | `character varying(32)` | 否 | `—` | 记录主标识；具体长度/格式由所属表定义。 |
| 2 | `create_time` | `bigint` | 是 | `—` | RAGFlow 使用的创建时间 Unix 毫秒值。 |
| 3 | `create_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的创建时间戳。 |
| 4 | `update_time` | `bigint` | 是 | `—` | RAGFlow 使用的更新时间 Unix 毫秒值。 |
| 5 | `update_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的更新时间戳。 |
| 6 | `name` | `character varying(100)` | 是 | `—` | 面向用户或系统显示的名称。 |
| 7 | `public_key` | `character varying(255)` | 是 | `—` | RAGFlow v0.26.4 `tenant` 记录中的“public key”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 8 | `llm_id` | `character varying(128)` | 否 | `—` | llm 的关联标识；外键约束以实际 Schema 为准。 |
| 9 | `tenant_llm_id` | `integer` | 是 | `—` | tenant llm 的关联标识；外键约束以实际 Schema 为准。 |
| 10 | `embd_id` | `character varying(128)` | 否 | `—` | embd 的关联标识；外键约束以实际 Schema 为准。 |
| 11 | `tenant_embd_id` | `integer` | 是 | `—` | tenant embd 的关联标识；外键约束以实际 Schema 为准。 |
| 12 | `asr_id` | `character varying(128)` | 否 | `—` | asr 的关联标识；外键约束以实际 Schema 为准。 |
| 13 | `tenant_asr_id` | `integer` | 是 | `—` | tenant asr 的关联标识；外键约束以实际 Schema 为准。 |
| 14 | `img2txt_id` | `character varying(128)` | 否 | `—` | img2txt 的关联标识；外键约束以实际 Schema 为准。 |
| 15 | `tenant_img2txt_id` | `integer` | 是 | `—` | tenant img2txt 的关联标识；外键约束以实际 Schema 为准。 |
| 16 | `rerank_id` | `character varying(128)` | 否 | `—` | rerank 的关联标识；外键约束以实际 Schema 为准。 |
| 17 | `tenant_rerank_id` | `integer` | 是 | `—` | tenant rerank 的关联标识；外键约束以实际 Schema 为准。 |
| 18 | `tts_id` | `character varying(256)` | 是 | `—` | tts 的关联标识；外键约束以实际 Schema 为准。 |
| 19 | `tenant_tts_id` | `integer` | 是 | `—` | tenant tts 的关联标识；外键约束以实际 Schema 为准。 |
| 20 | `ocr_id` | `character varying(256)` | 是 | `—` | ocr 的关联标识；外键约束以实际 Schema 为准。 |
| 21 | `parser_ids` | `character varying(256)` | 否 | `—` | parser ids 标识集合。 |
| 22 | `credit` | `integer` | 否 | `—` | RAGFlow v0.26.4 `tenant` 记录中的“credit”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 23 | `status` | `character varying(1)` | 是 | `—` | 当前业务状态；允许值由表上的 CHECK 约束或上游枚举控制。 |

### `tenant_langfuse`

租户 Langfuse 可观测性配置。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `tenant_id` | `character varying(32)` | 否 | `—` | 所属租户标识；在 rag_flow 中指 RAGFlow 内部租户。 |
| 2 | `create_time` | `bigint` | 是 | `—` | RAGFlow 使用的创建时间 Unix 毫秒值。 |
| 3 | `create_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的创建时间戳。 |
| 4 | `update_time` | `bigint` | 是 | `—` | RAGFlow 使用的更新时间 Unix 毫秒值。 |
| 5 | `update_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的更新时间戳。 |
| 6 | `secret_key` | `character varying(2048)` | 否 | `—` | Secret Key；属于敏感认证数据。 |
| 7 | `public_key` | `character varying(2048)` | 否 | `—` | RAGFlow v0.26.4 `tenant_langfuse` 记录中的“public key”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 8 | `host` | `character varying(128)` | 否 | `—` | RAGFlow v0.26.4 `tenant_langfuse` 记录中的“host”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |

### `tenant_llm`

历史租户模型关联配置。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `id` | `integer` | 否 | `nextval('tenant_llm_id_seq'::regclass)` | 记录主标识；具体长度/格式由所属表定义。 |
| 2 | `create_time` | `bigint` | 是 | `—` | RAGFlow 使用的创建时间 Unix 毫秒值。 |
| 3 | `create_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的创建时间戳。 |
| 4 | `update_time` | `bigint` | 是 | `—` | RAGFlow 使用的更新时间 Unix 毫秒值。 |
| 5 | `update_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的更新时间戳。 |
| 6 | `tenant_id` | `character varying(32)` | 否 | `—` | 所属租户标识；在 rag_flow 中指 RAGFlow 内部租户。 |
| 7 | `llm_factory` | `character varying(128)` | 否 | `—` | RAGFlow v0.26.4 `tenant_llm` 记录中的“llm factory”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 8 | `model_type` | `character varying(128)` | 是 | `—` | RAGFlow v0.26.4 `tenant_llm` 记录中的“model type”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 9 | `llm_name` | `character varying(128)` | 是 | `—` | RAGFlow v0.26.4 `tenant_llm` 记录中的“llm name”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 10 | `api_key` | `text` | 是 | `—` | 外部 API Key；属于敏感认证数据。 |
| 11 | `api_base` | `character varying(255)` | 是 | `—` | RAGFlow v0.26.4 `tenant_llm` 记录中的“api base”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 12 | `max_tokens` | `integer` | 否 | `—` | RAGFlow v0.26.4 `tenant_llm` 记录中的“max tokens”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 13 | `used_tokens` | `integer` | 否 | `—` | RAGFlow v0.26.4 `tenant_llm` 记录中的“used tokens”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 14 | `status` | `character varying(1)` | 否 | `—` | 当前业务状态；允许值由表上的 CHECK 约束或上游枚举控制。 |

### `tenant_model`

租户可用模型目录。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `id` | `character varying(32)` | 否 | `—` | 记录主标识；具体长度/格式由所属表定义。 |
| 2 | `create_time` | `bigint` | 是 | `—` | RAGFlow 使用的创建时间 Unix 毫秒值。 |
| 3 | `create_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的创建时间戳。 |
| 4 | `update_time` | `bigint` | 是 | `—` | RAGFlow 使用的更新时间 Unix 毫秒值。 |
| 5 | `update_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的更新时间戳。 |
| 6 | `model_name` | `character varying(128)` | 是 | `—` | RAGFlow v0.26.4 `tenant_model` 记录中的“model name”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 7 | `provider_id` | `character varying(32)` | 否 | `—` | provider 的关联标识；外键约束以实际 Schema 为准。 |
| 8 | `instance_id` | `character varying(32)` | 否 | `—` | instance 的关联标识；外键约束以实际 Schema 为准。 |
| 9 | `model_type` | `character varying(32)` | 否 | `—` | RAGFlow v0.26.4 `tenant_model` 记录中的“model type”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 10 | `status` | `character varying(32)` | 否 | `—` | 当前业务状态；允许值由表上的 CHECK 约束或上游枚举控制。 |
| 11 | `extra` | `character varying(1024)` | 否 | `—` | RAGFlow v0.26.4 `tenant_model` 记录中的“extra”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |

### `tenant_model_group`

租户模型分组。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `id` | `character varying(32)` | 否 | `—` | 记录主标识；具体长度/格式由所属表定义。 |
| 2 | `create_time` | `bigint` | 是 | `—` | RAGFlow 使用的创建时间 Unix 毫秒值。 |
| 3 | `create_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的创建时间戳。 |
| 4 | `update_time` | `bigint` | 是 | `—` | RAGFlow 使用的更新时间 Unix 毫秒值。 |
| 5 | `update_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的更新时间戳。 |
| 6 | `group_type` | `character varying(32)` | 否 | `—` | RAGFlow v0.26.4 `tenant_model_group` 记录中的“group type”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 7 | `model_name` | `character varying(128)` | 是 | `—` | RAGFlow v0.26.4 `tenant_model_group` 记录中的“model name”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 8 | `strategy` | `character varying(32)` | 否 | `—` | RAGFlow v0.26.4 `tenant_model_group` 记录中的“strategy”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |

### `tenant_model_group_mapping`

模型分组映射。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `create_time` | `bigint` | 是 | `—` | RAGFlow 使用的创建时间 Unix 毫秒值。 |
| 2 | `create_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的创建时间戳。 |
| 3 | `update_time` | `bigint` | 是 | `—` | RAGFlow 使用的更新时间 Unix 毫秒值。 |
| 4 | `update_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的更新时间戳。 |
| 5 | `group_id` | `character varying(32)` | 否 | `—` | group 的关联标识；外键约束以实际 Schema 为准。 |
| 6 | `provider_id` | `character varying(32)` | 否 | `—` | provider 的关联标识；外键约束以实际 Schema 为准。 |
| 7 | `instance_id` | `character varying(32)` | 否 | `—` | instance 的关联标识；外键约束以实际 Schema 为准。 |
| 8 | `model_id` | `character varying(32)` | 否 | `—` | model 的关联标识；外键约束以实际 Schema 为准。 |
| 9 | `weight` | `integer` | 否 | `—` | RAGFlow v0.26.4 `tenant_model_group_mapping` 记录中的“weight”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 10 | `status` | `character varying(32)` | 否 | `—` | 当前业务状态；允许值由表上的 CHECK 约束或上游枚举控制。 |

### `tenant_model_instance`

通过 RAGFlow 官方 API 创建的模型实例；可能包含敏感连接配置。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `id` | `character varying(32)` | 否 | `—` | 记录主标识；具体长度/格式由所属表定义。 |
| 2 | `create_time` | `bigint` | 是 | `—` | RAGFlow 使用的创建时间 Unix 毫秒值。 |
| 3 | `create_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的创建时间戳。 |
| 4 | `update_time` | `bigint` | 是 | `—` | RAGFlow 使用的更新时间 Unix 毫秒值。 |
| 5 | `update_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的更新时间戳。 |
| 6 | `instance_name` | `character varying(128)` | 否 | `—` | RAGFlow v0.26.4 `tenant_model_instance` 记录中的“instance name”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 7 | `provider_id` | `character varying(32)` | 否 | `—` | provider 的关联标识；外键约束以实际 Schema 为准。 |
| 8 | `api_key` | `character varying(512)` | 否 | `—` | 外部 API Key；属于敏感认证数据。 |
| 9 | `status` | `character varying(32)` | 否 | `—` | 当前业务状态；允许值由表上的 CHECK 约束或上游枚举控制。 |
| 10 | `extra` | `character varying(512)` | 否 | `—` | RAGFlow v0.26.4 `tenant_model_instance` 记录中的“extra”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |

### `tenant_model_provider`

租户模型供应商配置。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `id` | `character varying(32)` | 否 | `—` | 记录主标识；具体长度/格式由所属表定义。 |
| 2 | `create_time` | `bigint` | 是 | `—` | RAGFlow 使用的创建时间 Unix 毫秒值。 |
| 3 | `create_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的创建时间戳。 |
| 4 | `update_time` | `bigint` | 是 | `—` | RAGFlow 使用的更新时间 Unix 毫秒值。 |
| 5 | `update_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的更新时间戳。 |
| 6 | `provider_name` | `character varying(128)` | 否 | `—` | RAGFlow v0.26.4 `tenant_model_provider` 记录中的“provider name”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 7 | `tenant_id` | `character varying(32)` | 否 | `—` | 所属租户标识；在 rag_flow 中指 RAGFlow 内部租户。 |

### `user`

RAGFlow 管理界面用户及认证信息。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `id` | `character varying(32)` | 否 | `—` | 记录主标识；具体长度/格式由所属表定义。 |
| 2 | `create_time` | `bigint` | 是 | `—` | RAGFlow 使用的创建时间 Unix 毫秒值。 |
| 3 | `create_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的创建时间戳。 |
| 4 | `update_time` | `bigint` | 是 | `—` | RAGFlow 使用的更新时间 Unix 毫秒值。 |
| 5 | `update_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的更新时间戳。 |
| 6 | `access_token` | `character varying(255)` | 是 | `—` | RAGFlow v0.26.4 `user` 记录中的“access token”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 7 | `nickname` | `character varying(100)` | 否 | `—` | RAGFlow v0.26.4 `user` 记录中的“nickname”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 8 | `password` | `character varying(255)` | 是 | `—` | 认证密码摘要或受保护凭据；禁止导出表数据到文档/Git。 |
| 9 | `email` | `character varying(255)` | 否 | `—` | RAGFlow v0.26.4 `user` 记录中的“email”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 10 | `avatar` | `text` | 是 | `—` | RAGFlow v0.26.4 `user` 记录中的“avatar”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 11 | `language` | `character varying(32)` | 是 | `—` | RAGFlow v0.26.4 `user` 记录中的“language”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 12 | `color_schema` | `character varying(32)` | 是 | `—` | RAGFlow v0.26.4 `user` 记录中的“color schema”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 13 | `timezone` | `character varying(64)` | 是 | `—` | RAGFlow v0.26.4 `user` 记录中的“timezone”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 14 | `last_login_time` | `timestamp without time zone` | 是 | `—` | last login 的时间值。 |
| 15 | `is_authenticated` | `character varying(1)` | 否 | `—` | 控制或记录“is authenticated”状态。 |
| 16 | `is_active` | `character varying(1)` | 否 | `—` | 控制或记录“is active”状态。 |
| 17 | `is_anonymous` | `character varying(1)` | 否 | `—` | 控制或记录“is anonymous”状态。 |
| 18 | `login_channel` | `character varying(255)` | 是 | `—` | RAGFlow v0.26.4 `user` 记录中的“login channel”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 19 | `status` | `character varying(1)` | 是 | `—` | 当前业务状态；允许值由表上的 CHECK 约束或上游枚举控制。 |
| 20 | `is_superuser` | `boolean` | 是 | `—` | 控制或记录“is superuser”状态。 |

### `user_canvas`

用户 Canvas/Agent 编排。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `id` | `character varying(32)` | 否 | `—` | 记录主标识；具体长度/格式由所属表定义。 |
| 2 | `create_time` | `bigint` | 是 | `—` | RAGFlow 使用的创建时间 Unix 毫秒值。 |
| 3 | `create_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的创建时间戳。 |
| 4 | `update_time` | `bigint` | 是 | `—` | RAGFlow 使用的更新时间 Unix 毫秒值。 |
| 5 | `update_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的更新时间戳。 |
| 6 | `avatar` | `text` | 是 | `—` | RAGFlow v0.26.4 `user_canvas` 记录中的“avatar”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 7 | `user_id` | `character varying(255)` | 否 | `—` | 所属用户标识。 |
| 8 | `title` | `character varying(255)` | 是 | `—` | RAGFlow v0.26.4 `user_canvas` 记录中的“title”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 9 | `permission` | `character varying(16)` | 否 | `—` | RAGFlow v0.26.4 `user_canvas` 记录中的“permission”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 10 | `release` | `boolean` | 否 | `—` | RAGFlow v0.26.4 `user_canvas` 记录中的“release”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 11 | `description` | `text` | 是 | `—` | 说明文本。 |
| 12 | `canvas_type` | `character varying(32)` | 是 | `—` | RAGFlow v0.26.4 `user_canvas` 记录中的“canvas type”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 13 | `canvas_category` | `character varying(32)` | 否 | `—` | RAGFlow v0.26.4 `user_canvas` 记录中的“canvas category”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 14 | `tags` | `character varying(512)` | 否 | `—` | RAGFlow v0.26.4 `user_canvas` 记录中的“tags”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 15 | `dsl` | `text` | 是 | `—` | RAGFlow v0.26.4 `user_canvas` 记录中的“dsl”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |

### `user_canvas_version`

用户 Canvas 历史版本。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `id` | `character varying(32)` | 否 | `—` | 记录主标识；具体长度/格式由所属表定义。 |
| 2 | `create_time` | `bigint` | 是 | `—` | RAGFlow 使用的创建时间 Unix 毫秒值。 |
| 3 | `create_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的创建时间戳。 |
| 4 | `update_time` | `bigint` | 是 | `—` | RAGFlow 使用的更新时间 Unix 毫秒值。 |
| 5 | `update_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的更新时间戳。 |
| 6 | `user_canvas_id` | `character varying(255)` | 否 | `—` | user canvas 的关联标识；外键约束以实际 Schema 为准。 |
| 7 | `title` | `character varying(255)` | 是 | `—` | RAGFlow v0.26.4 `user_canvas_version` 记录中的“title”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 8 | `description` | `text` | 是 | `—` | 说明文本。 |
| 9 | `release` | `boolean` | 否 | `—` | RAGFlow v0.26.4 `user_canvas_version` 记录中的“release”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 10 | `dsl` | `text` | 是 | `—` | RAGFlow v0.26.4 `user_canvas_version` 记录中的“dsl”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |

### `user_tenant`

RAGFlow 用户与其内部租户的成员关系。

| 序号 | 字段 | PostgreSQL 类型 | 可空 | 默认值 | 字段含义 |
|---:|---|---|:---:|---|---|
| 1 | `id` | `character varying(32)` | 否 | `—` | 记录主标识；具体长度/格式由所属表定义。 |
| 2 | `create_time` | `bigint` | 是 | `—` | RAGFlow 使用的创建时间 Unix 毫秒值。 |
| 3 | `create_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的创建时间戳。 |
| 4 | `update_time` | `bigint` | 是 | `—` | RAGFlow 使用的更新时间 Unix 毫秒值。 |
| 5 | `update_date` | `timestamp without time zone` | 是 | `—` | RAGFlow 使用的更新时间戳。 |
| 6 | `user_id` | `character varying(32)` | 否 | `—` | 所属用户标识。 |
| 7 | `tenant_id` | `character varying(32)` | 否 | `—` | 所属租户标识；在 rag_flow 中指 RAGFlow 内部租户。 |
| 8 | `role` | `character varying(32)` | 否 | `—` | RAGFlow v0.26.4 `user_tenant` 记录中的“role”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 9 | `invited_by` | `character varying(32)` | 否 | `—` | RAGFlow v0.26.4 `user_tenant` 记录中的“invited by”字段；精确枚举/JSON 子结构由该版本业务模型约束。 |
| 10 | `status` | `character varying(1)` | 是 | `—` | 当前业务状态；允许值由表上的 CHECK 约束或上游枚举控制。 |

## 6. Redis Stack 数据字典

统一 Redis Stack 是一个物理实例，使用同一网络端点和密码，通过逻辑 DB 与 Key
Namespace 分离职责。

### 6.1 DB 0：Gateway 缓存与语义缓存

| Key/索引 | Redis 类型 | 字段/值 | 用途 |
|---|---|---|---|
| `rag-cache:exact:<identifier>` | String + TTL | 完整 CacheEntry JSON | 精确问题缓存 |
| `rag-cache:semantic:v2:<identifier>` | Hash + TTL | 见下表 | 语义缓存候选 |
| `rag-cache:index-metadata:rag_semantic_cache_v2` | Hash | Schema/模型信息 | 启动时验证索引版本、模型与维度 |
| `rag_semantic_cache_v2` | RediSearch HNSW Index | 见下表 | 对语义缓存 Hash 做边界过滤和向量 KNN |

语义缓存 Hash/Index 字段：

| 字段 | 类型 | 含义 |
|---|---|---|
| `cache_id` | TAG/String | 缓存记录标识 |
| `security_scope_id` | TAG | 租户 + 用户派生的隔离边界 |
| `knowledge_base_revisions_hash` | TAG | 所有知识库 Revision 的稳定摘要 |
| `retrieval_profile` | TAG | 检索 Profile 版本 |
| `prompt_profile` | TAG | Prompt Profile 版本 |
| `model_profile` | TAG | Model Profile 版本 |
| `language` | TAG | 响应/问题语言边界 |
| `history_hash` | TAG | 历史摘要边界；当前无上下文时为稳定空历史值 |
| `normalized_question` | String | 归一化后的问题 |
| `answer_json` | JSON String | 答案、QueryPlan、Usage 和 Warning |
| `citations_json` | JSON String | 引用数组 |
| `created_at_ms` | NUMERIC | 创建时间 Unix 毫秒 |
| `expires_at_ms` | NUMERIC | 过期时间 Unix 毫秒 |
| `last_hit_at_ms` | String/Numeric | 最近命中时间 Unix 毫秒 |
| `embedding` | FLOAT32 Vector | 当前固定维度的语义缓存向量 |

### 6.2 DB 1：RAGFlow

DB 1 由 RAGFlow v0.26.4 内部使用，保存任务队列、锁、进度和临时状态。Key 名称及值
结构属于上游内部实现，不是本项目对外契约；不能由 Gateway 清理，也不能通过
`FLUSHDB` 作为普通故障处理手段。

### 6.3 DB 2：Celery

| Key Namespace | 类型 | 用途 |
|---|---|---|
| `rag:celery:rag-ingestion` | List | 文档上传、解析、重解析、版本更新任务队列 |
| `rag:celery:rag-maintenance` | List | 删除、清理等维护任务队列 |
| `rag:celery:unacked` | Hash | 已领取但尚未确认的消息 |
| `rag:celery:unacked_index` | Sorted Set | 未确认消息的可见性超时索引 |
| `rag:celery:*` | Kombu/Celery 内部类型 | Binding、锁、结果或调度辅助 Key |

Redis 是运行态存储，不由 `migrations/releases/V1.0.0__fresh_install.sql` 初始化。
Gateway Bootstrap 会
创建并验证 RediSearch 索引；Celery/RAGFlow 在运行时按需创建自己的 Key。

## 7. Elasticsearch 数据字典

Elasticsearch 索引由 RAGFlow 根据知识库/Embedding 模型动态创建。当前 Gateway 依赖
RAGFlow 检索 API 返回以下 Chunk 逻辑字段；真实索引还可能含上游内部字段：

| 逻辑字段 | 含义 |
|---|---|
| `id` | RAGFlow Chunk ID |
| `dataset_id` / `kb_id` | RAGFlow 知识库 ID |
| `document_id` / `doc_id` | RAGFlow 文档 ID |
| `content` / `content_with_weight` | Chunk 正文及加权检索正文 |
| `question_kwd` | 问题关键词 |
| `important_kwd` | 重要关键词 |
| `positions` | 页码、坐标或文本位置 |
| `metadata` | 文档/Chunk Metadata |
| `vector` / 模型相关向量字段 | Embedding 向量；字段名和维度随模型/上游映射变化 |
| `similarity` | RAGFlow 汇总相似度 |
| `vector_similarity` | 向量相似度 |
| `term_similarity` | BM25/关键词相似度 |

Elasticsearch 不是事实控制面：知识库权限和文档归属仍以 `rag_app` PostgreSQL 为准。
Gateway 会把引擎 ID 反查到当前安全 Scope 的本地记录，无法映射的 Chunk 会被丢弃。

## 8. MinIO 对象字典

MinIO 由 RAGFlow 的 Storage 模块管理，保存上传原文件和解析流程产生的对象。Bucket、
Prefix 和对象键可由 RAGFlow 配置/版本决定，不是 Gateway 的稳定 API。

| 对象属性 | 含义 |
|---|---|
| Bucket | RAGFlow 使用的对象存储桶；可由 `MINIO_BUCKET` 指定或由官方默认流程管理 |
| Prefix | 可选 `MINIO_PREFIX_PATH`，用于同一 MinIO 实例中的业务路径隔离 |
| Object Key | RAGFlow 生成的内部对象名；不保证等于用户原文件名 |
| Object Body | 原始文件或解析中间对象 |
| Content Type/Metadata | 文件 MIME 和对象元数据 |

`rag_app.document_record.filename` 才是 Gateway 面向用户保存的稳定原文件名。不得根据
MinIO 内部对象名向用户暴露文件身份，也不得在数据库初始化 SQL 中手工创建 MinIO
对象。

## 9. 结构初始化责任边界

| 组件 | 创建方式 | 本交付 SQL 是否负责 |
|---|---|:---:|
| `rag_app` PostgreSQL Schema | DBA 执行 SQL；应用 Alembic 校验 | 是 |
| `rag_flow` PostgreSQL Schema | DBA 执行固定版本 Schema；RAGFlow 启动补内置数据 | 是（仅结构） |
| Redis 缓存索引 | Gateway Bootstrap 执行 `FT.CREATE`/校验 | 否 |
| RAGFlow Redis Key | RAGFlow 运行时创建 | 否 |
| Celery Redis Key | Worker/Scheduler 运行时创建 | 否 |
| Elasticsearch 索引与 Mapping | RAGFlow 创建 | 否 |
| MinIO Bucket/对象 | RAGFlow Storage 初始化与上传流程创建 | 否 |

## 10. 安全注意事项

- `api_token`、`user`、`system_settings`、模型实例等表可能包含认证或连接敏感数据；
- Redis 缓存正文与引用可能包含用户问题和知识库片段，不能当作无敏感数据；
- Elasticsearch 保存 Chunk 正文和向量，MinIO 保存完整原文件，二者都属于业务数据；
- 本文可以进入 Git，因为只描述 Schema；任何 `pg_dump` 数据文件、Redis RDB/AOF、
  Elasticsearch Snapshot、MinIO 对象备份都不能直接提交仓库；
- 生产环境的备份、保留期、加密、审计和删除策略必须由基础设施运维单独制定。
