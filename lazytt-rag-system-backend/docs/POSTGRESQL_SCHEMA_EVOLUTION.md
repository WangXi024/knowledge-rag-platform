# PostgreSQL 空库初始化、Schema 演进与验收手册

> 历史参考：本文保留改造前的流程与审计说明。2026-09-07 起，部署与 SQL 执行以[当前 ECS 操作入口](../deploy/ecs/README.md)为准；本文旧 `migrations/releases`、Alembic 或一体化安装命令不再执行。旧文件可在 Git 检查点 `335d418` 查阅。

## 1. 文档目标

本文说明 LazyTT RAG 在一台新的 PostgreSQL 服务器上如何建立可运行的空数据库，
以及自研应用数据库如何从 Alembic `0001` 演进到 `0006`。

本文同时记录当前本地默认部署和外部 PostgreSQL 空库初始化方案。两种模式共同遵守：

- 只使用一个 PostgreSQL 实例；
- 创建两个逻辑数据库：`rag_app` 和 `rag_flow`；
- `rag_app` 的六个 Alembic 版本仍是开发期 Schema 演进事实来源，但生产安装只能由 DBA
  执行评审后的全新安装 SQL；
- `rag_flow` 的结构来自固定版本 RAGFlow 官方 Schema，生产安装同样由 DBA 预先完成；
- 只创建新账号、新数据库和新表，不迁移任何旧业务数据；
- PostgreSQL 管理员只参与首次建库，不进入应用运行配置。

账号形态按部署模式区分：

- 当前本地 `lazytt-rag-storage` 默认创建 `rag_app`、`rag_flow` 两个运行账号，分别访问
  两个数据库；
- 外部数据库服务器可按本文方案创建一个非管理员业务账号 `lazytt_rag`，让它访问两个
  数据库；
- 账号可以合并，数据库不能合并。无论采用哪种账号形态，都必须保留 `rag_app`、
  `rag_flow` 两个数据库。

字段级数据字典请查看
[POSTGRESQL_SCHEMA_AND_DATA_MIGRATION.md](./POSTGRESQL_SCHEMA_AND_DATA_MIGRATION.md)，
当前 App PostgreSQL 的汇总 DDL 请查看
[`rag-development-spec/database/schema.sql`](../rag-development-spec/database/schema.sql)。

> 本文不授权删除现有数据库、Docker 数据卷或业务记录。对已有环境执行初始化前，
> 必须确认目标确实是空环境。

## 2. 本次初始化范围

### 2.1 包含的内容

1. 在目标 PostgreSQL 实例中创建唯一业务账号/对象所有者 `lazytt_rag`；
3. 创建应用数据库 `rag_app`；
4. 创建 RAGFlow 数据库 `rag_flow`；
5. 由 DBA 使用评审后的全新安装 SQL 一次性安装两个数据库的 Schema；
6. 收紧实例级权限：`lazytt_rag` 持有两个项目数据库和目标 Schema/对象并保留
   Schema CREATE，但不能创建角色、第三个数据库或取得超级用户能力；
7. 配置 Gateway、Worker、Scheduler 和 RAGFlow 的连接信息；
8. 由启动门禁只读验证表集合、应用 Revision、两个目标 Schema 的对象所有权和
   CREATE 权限；
9. 验证两个数据库、表结构和完整应用链路。

### 2.2 不包含的内容

本策略明确不执行：

- `pg_dump` 或 `pg_restore`；
- 导入旧 PostgreSQL 数据；
- 导入旧 Elasticsearch 索引；
- 复制旧 MinIO 对象；
- 恢复 Redis Stack 数据；
- 复制 Docker 数据卷；
- 直接用 `schema.sql` 覆盖已有数据库；
- 修改或 Fork RAGFlow 源码。

## 3. 当前 PostgreSQL 架构

### 3.1 本地开发模式

```text
PostgreSQL 实例（lazytt-rag-storage）
├── 开发账号 rag_app  -> 数据库 rag_app
└── 开发账号 rag_flow -> 数据库 rag_flow
```

本地模式保留历史开发账号形态，便于旧数据卷继续启动。它不代表生产权限基线；新部署、
外部服务器和本次 DBA 验收一律使用下一节的所有者/运行账号分离模型。

### 3.2 外部服务器单业务账号模式

```text
PostgreSQL 实例
├── 业务账号/对象所有者：lazytt_rag（LOGIN）
│   ├── 连接 rag_app
│   ├── 连接 rag_flow
│   └── 仅在两个项目 Schema 内拥有 DDL/对象所有权
├── 数据库：rag_app
│   └── 自研 RAG Gateway、Worker、Scheduler 数据
└── 数据库：rag_flow
    └── RAGFlow v0.26.4 内部数据
```

| 项目 | 最终值 | 用途 |
|---|---|---|
| PostgreSQL 实例 | 1 个 | 统一提供关系数据库服务 |
| 运行时业务账号 | 外部模式可使用 `lazytt_rag` | 同一账号访问两个逻辑数据库 |
| 应用数据库 | `rag_app` | 自研 RAG 业务数据和侧车索引 |
| RAGFlow 数据库 | `rag_flow` | RAGFlow 内部元数据和任务数据 |
| 业务账号/对象所有者 | `lazytt_rag`（可登录） | 持有两个项目数据库、Schema 和对象 |
| 管理员账号 | 仅首次初始化使用 | 创建角色、数据库、Schema 和授权 |

两个数据库共用同一个 PostgreSQL 进程和业务账号，但表、事务、
备份边界和数据库名称仍然独立。`rag_app` 与 `rag_flow` 不能合并为同一个数据库。

## 4. 管理员与业务账号边界

### 4.1 PostgreSQL 管理员

管理员或 DBA 只负责：

- 创建 `lazytt_rag`；
- 创建 `rag_app` 和 `rag_flow`；
- 设置数据库 Owner 和连接权限；
- 必要时修改服务器防火墙、`pg_hba.conf` 和监听地址。

管理员账号不得：

- 写入 `deploy/local/.env.ragflow`；
- 写入 `APP_DATABASE_DSN`；
- 作为 Gateway 或 RAGFlow 的长期连接账号；
- 提交到 Git 仓库。

### 4.2 外部模式业务账号 `lazytt_rag`

`lazytt_rag` 是本文外部服务器方案使用的统一运行时数据库账号。它应具备：

- 登录 PostgreSQL 的权限；
- 连接 `rag_app` 和 `rag_flow` 的权限；
- 在这两个数据库中创建和维护 Schema、表、索引、约束及触发器的权限。

它不应具备：

- PostgreSQL 超级管理员权限；
- 创建其他账号的权限；
- 创建第三个数据库的权限；
- 复制、流复制或绕过行级安全策略的权限。

### 4.3 单业务账号的安全取舍

统一账号可以减少密码数量和部署配置，但意味着：

- 该账号泄露时，`rag_app` 和 `rag_flow` 都可能受影响；
- 无法仅通过数据库账号区分 Gateway 和 RAGFlow 的操作来源；
- 两个数据库必须使用同一份业务密码。

该取舍是外部服务器方案的明确选择；本地默认模式仍使用两个运行账号。两种模式都通过
两个逻辑数据库保持结构和业务边界。

## 5. 两个数据库分别存储什么

### 5.1 `rag_app`

由 LazyTT RAG 自研代码使用，主要保存：

- 配置 Profile；
- 知识库业务记录；
- 逻辑文档和文档版本；
- 上传、解析、更新、删除任务；
- API 幂等记录；
- Query、Citation 和 Feedback 审计记录；
- Parent、Section、Summary 层级侧车索引；
- 实体和轻量关系侧车索引；
- Alembic 当前 Revision。

### 5.2 `rag_flow`

由 RAGFlow v0.26.4 使用，主要保存：

- RAGFlow 用户、Tenant 和 API Key 信息；
- Dataset、Document、Chunk 的关系元数据；
- 模型 Provider 和模型实例配置；
- 解析任务及运行状态；
- RAGFlow 权限和内部系统配置。

需要注意：RAGFlow 的全部文档数据并不只存在 PostgreSQL 中。原始文件通常存入 MinIO，
Chunk、向量和全文检索数据通常进入 Elasticsearch；RAGFlow 的队列和运行状态使用统一
Redis Stack 的 DB 1。统一实例的 DB 0 供 Gateway 缓存使用，DB 2 仅作为 Celery Broker。

## 6. 为什么仍要创建两个数据库

即使两个数据库都由 `lazytt_rag` 访问，也不应将所有表混在一个数据库中：

1. `rag_app` 由自研 Alembic 迁移管理，`rag_flow` 由 RAGFlow 官方逻辑管理；
2. 两边可能存在同名 Schema、扩展、表或迁移规则；
3. 可以分别检查、备份、重建和设置连接上限；
4. RAGFlow 升级不会直接进入自研业务 Schema；
5. 自研迁移不会误修改 RAGFlow 内部表；
6. 出现故障时可以快速判断属于 Gateway 还是 RAGFlow。

因此外部方案是“一个实例、一个业务账号、两个逻辑数据库”，本地默认方案是“一个实例、
两个业务账号、两个逻辑数据库”；两者都不是“一个数据库存全部表”。

## 7. 外部新服务器初始化前提

开始前确认：

- PostgreSQL 服务已启动；
- 项目主机能够访问 `<POSTGRES_HOST>:5432`；
- PostgreSQL 版本满足项目和 RAGFlow v0.26.4 的要求；
- 当前操作人员具备一次性的 DBA 权限；
- `lazytt_rag`、`rag_app`、`rag_flow` 不存在，或已确认可以安全复用；
- 目标服务器的防火墙只允许可信主机访问 PostgreSQL；
- 已准备一个不会提交到 Git 的高强度业务密码。

可先验证网络连通性：

```bash
nc -vz <POSTGRES_HOST> 5432
```

该命令只检查端口，不代表账号、数据库和权限已经正确。

## 8. 创建外部业务账号

以下 SQL 必须由 PostgreSQL 管理员执行，密码应替换为真实随机值，但不得写入文档或
Git 仓库：

```sql
CREATE ROLE lazytt_rag
    LOGIN
    PASSWORD '<REDACTED>'
    NOSUPERUSER
    NOCREATEDB
    NOCREATEROLE
    NOREPLICATION;
```

权限含义：

| 设置 | 含义 |
|---|---|
| `LOGIN` | 允许使用该账号连接 PostgreSQL |
| `NOSUPERUSER` | 不能绕过数据库权限体系 |
| `NOCREATEDB` | 不能自行创建其他数据库 |
| `NOCREATEROLE` | 不能创建或修改其他账号 |
| `NOREPLICATION` | 不能使用数据库复制能力 |

如果账号已经存在，不要再次直接执行 `CREATE ROLE`。应先核对账号属性，再由 DBA 决定
是否使用 `ALTER ROLE` 更新密码。

## 9. 创建两个数据库

由管理员执行：

```sql
CREATE DATABASE rag_app
    OWNER lazytt_rag
    ENCODING 'UTF8';

CREATE DATABASE rag_flow
    OWNER lazytt_rag
    ENCODING 'UTF8';
```

两个数据库都由 `lazytt_rag` 所有，因此它能在数据库中建立运行所需的 Schema 和表，
但仍不能创建其他数据库。

## 10. 收紧数据库连接权限

PostgreSQL 默认可能允许 `PUBLIC` 连接新数据库。由管理员执行：

```sql
REVOKE CONNECT ON DATABASE rag_app FROM PUBLIC;
GRANT CONNECT ON DATABASE rag_app TO lazytt_rag;

REVOKE CONNECT ON DATABASE rag_flow FROM PUBLIC;
GRANT CONNECT ON DATABASE rag_flow TO lazytt_rag;
```

分别验证业务账号连接：

```bash
PGPASSWORD='<REDACTED>' psql \
  -h <POSTGRES_HOST> -p 5432 -U lazytt_rag -d rag_app \
  -c 'SELECT current_user, current_database();'

PGPASSWORD='<REDACTED>' psql \
  -h <POSTGRES_HOST> -p 5432 -U lazytt_rag -d rag_flow \
  -c 'SELECT current_user, current_database();'
```

两个结果都应显示 `current_user = lazytt_rag`，数据库分别为 `rag_app` 和 `rag_flow`。

## 11. 外部 PostgreSQL 项目配置

真实值只填写在 Git 忽略的 `deploy/local/.env.ragflow`，不要直接修改自动生成的
`deploy/local/.env.full`。外部 PostgreSQL 示例：

```text
# 自研应用数据库。外部模式只以 DSN 作为运行时连接来源。
APP_DATABASE_DSN=postgresql+psycopg://lazytt_rag:<URL编码后的密码>@<POSTGRES_HOST>:5432/rag_app

# RAGFlow 数据库
RAGFLOW_POSTGRES_HOST=<POSTGRES_HOST>
RAGFLOW_POSTGRES_PORT=5432
RAGFLOW_POSTGRES_DATABASE=rag_flow
RAGFLOW_POSTGRES_USER=lazytt_rag
RAGFLOW_POSTGRES_PASSWORD=<REDACTED>
```

配置要求：

1. `APP_DATABASE_DSN` 与 `RAGFLOW_POSTGRES_*` 必须使用同一个 `lazytt_rag` 业务账号，
   但分别连接 `rag_app` 和 `rag_flow`；
2. `APP_DATABASE_DSN` 中的密码必须与 `RAGFLOW_POSTGRES_PASSWORD` 一致；
3. 密码含 `@`、`:`、`/`、`#`、`%` 等字符时，DSN 中必须使用 URL 编码；
4. `APP_POSTGRES_DATABASE`、`APP_POSTGRES_USER`、`APP_POSTGRES_PASSWORD` 是本地 storage
   Compose 的初始化变量，不是外部模式的应用连接入口；外部模式不要依赖它们覆盖 DSN；
5. 管理员账号和管理员密码不得写入应用配置；
6. `<POSTGRES_HOST>` 应填写局域网数据库服务器地址或可解析主机名。

### 11.1 本地模式与外部模式的区别

当前本地 `lazytt-rag-storage` 由 `deploy/local/postgres/init-databases.sh` 初始化，默认使用：

```text
APP_POSTGRES_USER=rag_app
RAGFLOW_POSTGRES_USER=rag_flow
```

外部模式不会启动本地 storage Compose，也不会依赖该脚本在远端建账号。外部 DBA 先创建
`lazytt_rag` 和两个数据库，再将 `APP_DATABASE_DSN`、`RAGFLOW_POSTGRES_*` 都指向同一
外部账号。不要因为外部方案使用单账号，就把本地默认变量误改成同一个账号。

### 11.2 配置源和生成文件

只修改 Git 忽略的 `deploy/local/.env.ragflow`。`deploy/local/.env.full` 由 `start-full.sh` 自动生成，
不应手工维护。外部模式还必须设置：

```text
RAG_STORAGE_MODE=external
```

管理员账号和真实密码不能写入文档、示例配置或 Git。

## 12. 身份边界如何落到 PostgreSQL

当前 Gateway 不在 PostgreSQL 保存登录用户、密码或 Session。请求通过 `X-Tenant-ID`、
`X-User-ID` 提交 Identity 系统提供的租户和用户标识；服务端使用 Tenant ID 以及
`user:<user_id>` 计算 SHA-256 `security_scope_id`。

```text
tenant_id + user:<user_id>
  -> SHA-256
  -> 64 位 security_scope_id
  -> 作为知识库、文档、Job、查询、引用和反馈的隔离条件
```

数据库只保存派生后的 Scope，不把原始 Tenant ID 和 User ID 当成业务表主键。Repository
查询必须同时包含 `security_scope_id`，所以即使知道另一租户的知识库 UUID，也不能通过
正常 Gateway API 访问其记录。当前 `direct_user` 模式仅用于非生产联调，生产环境仍需
由 Identity 提供可信鉴权并防止客户端伪造这两个 Header。

## 13. 自研数据库的 Alembic 演进记录

迁移目录：

```text
migrations/
├── env.py
└── versions/
    ├── 0001_initial_rag_app.py
    ├── 0002_reuse_deleted_document_external_id.py
    ├── 0003_document_hierarchical_index.py
    ├── 0004_entity_graph_index.py
    ├── 0005_allow_section_index_nodes.py
    └── 0006_document_version_updates.py
```

在开发阶段重建 Schema 或生成新安装包时，Alembic 会：

1. 读取 `APP_DATABASE_DSN`；
2. 连接 `rag_app`；
3. 创建 `rag_app` Schema；
4. 从 `0001` 按依赖顺序执行到 `0006`；
5. 将当前 Revision 写入 `rag_app.alembic_version`。

RAGFlow 数据库不使用这组 Alembic 脚本。`local` 启动的一次性准备命令会先验证旧库
Revision 与表集合一致，再执行 `alembic upgrade head` 并登记 `V1.0.0`；异常半成品会
停止且不覆盖数据。`external`/生产数据库不执行该命令，DBA 应用评审 SQL 后，应用只读
检查 `rag_app.alembic_version`、发布号和摘要。

## 14. 六个迁移版本演进

### 14.1 总览

```text
0001_initial_rag_app
  -> 0002_document_external_reuse
  -> 0003_document_hierarchical_index
  -> 0004_entity_graph_index
  -> 0005_allow_section_index_nodes
  -> 0006_document_version_updates
```

| 版本 | 主要变化 | 对应能力 |
|---|---|---|
| `0001` | 创建基础表、索引、更新时间函数和触发器 | 基础 RAG 业务存储 |
| `0002` | 修改文档 External ID 唯一规则 | 软删除后复用 External ID |
| `0003` | 新增 `document_index_node` | Parent、Summary 层级索引 |
| `0004` | 新增实体和关系表 | 实体索引、轻量关系检索 |
| `0005` | 允许 `SECTION` 节点类型 | 章节级多粒度索引 |
| `0006` | 新增文档版本表和活动版本字段 | 文档安全更新与回滚 |

### 14.2 `0001_initial_rag_app`

创建 `rag_app` Schema、初始业务表、索引、检查约束、`set_updated_at()` 函数，以及
`config_profile`、`knowledge_base`、`document_record`、`ingestion_job` 的更新时间触发器。

它提供 Profile、知识库、文档、任务、幂等、查询、引用和反馈等基础存储能力。

注意：`0001` 调用当前 `Base.metadata.create_all()`，不是完全冻结的历史 DDL。今天在新空库
执行时，当前 ORM 可能直接创建已经包含后续能力的部分表；后续迁移使用存在性检查或
幂等方式补齐。已经发布的迁移不应再修改，新变化必须新增下一号迁移。

### 14.3 `0002_document_external_reuse`

它删除旧的文档 `external_id` 全表唯一约束，改为只约束未删除记录的部分唯一索引，
唯一范围仍包含安全 Scope 和知识库。

对应能力：文档软删除后，可以在同一安全范围和知识库中复用原 External ID。

如果已经发生 ID 复用，降级恢复旧唯一约束会因冲突失败。迁移会保留历史数据，而不是
静默删除冲突记录。

### 14.4 `0003_document_hierarchical_index`

新增 `document_index_node`，支持 Parent、Summary、Parent-Child 导航，并为后续 Section
多粒度索引提供结构。

该表是 Gateway 侧车导航索引，不复制 RAGFlow 原始正文。删除该表会损失结构感知检索，
但不删除 RAGFlow 中的原始文档和 Chunk。

### 14.5 `0004_entity_graph_index`

新增 `document_entity` 和 `entity_relation`，用于保存文档实体、实体到真实 Chunk 的映射、
轻量实体关系及关系证据。

迁移不回填旧文档。旧文档需要重新解析后才会生成对应侧车数据。

### 14.6 `0005_allow_section_index_nodes`

将节点类型从 `PARENT/SUMMARY` 扩展为 `PARENT/SECTION/SUMMARY`，支持按章节建立中间层
索引，使检索可以在文档摘要、章节、Parent 和真实 Chunk 之间进行多粒度路由。

降级时会先删除可重建的 `SECTION` 节点，再恢复旧检查约束；原始文档不受影响。

### 14.7 `0006_document_version_updates`

为 `document_record` 增加 `active_version_id`、`active_version_number`、`document_revision`，
并新增 `document_version`、版本表更新时间触发器和 `UPDATE_DOCUMENT` Job 类型。

对应能力包括上传新版本、解析成功后原子切换、查询版本历史、回滚旧版本，以及使用
Revision 让旧缓存失效。

从旧数据库升级时会为已有逻辑文档建立本地版本 1 快照。新空数据库没有历史行，因此
不会产生业务数据。

## 15. `rag_app` 当前完整表清单

当前包含 12 张业务表和 1 张 Alembic 内部表：

| 表名 | 分类 | 主要用途 | 是否可直接删除 |
|---|---|---|---|
| `config_profile` | 配置 | 保存 Parser、Chunk、Embedding、Retrieval、Prompt、Model 等版本化 Profile | 否 |
| `knowledge_base` | 知识库 | 保存知识库状态、Revision、Profile 引用和隔离范围 | 否 |
| `document_record` | 文档 | 保存逻辑文档、活动版本、哈希、状态、统计和元数据 | 否 |
| `document_version` | 文档版本 | 保存每次更新产生的物理版本和激活状态 | 否 |
| `document_index_node` | 侧车索引 | 保存 Parent、Section、Summary 节点和真实 Chunk 引用 | 否 |
| `document_entity` | 侧车索引 | 保存实体及其来源 Chunk | 否 |
| `entity_relation` | 侧车索引 | 保存实体关系及证据 Chunk | 否 |
| `ingestion_job` | 异步任务 | 保存上传、解析、更新、删除、清理任务及状态事实 | 否 |
| `idempotency_record` | API 安全 | 防止同一写请求被重复执行 | 否 |
| `query_record` | 查询审计 | 保存检索和问答请求的脱敏信息及性能数据 | 否 |
| `citation_record` | 引用审计 | 保存引用 Chunk、排序、分数和验证状态 | 否 |
| `feedback_record` | 质量反馈 | 保存结果评分、问题类型和脱敏评论 | 否 |
| `alembic_version` | 迁移内部表 | 记录当前 Alembic Revision | 否 |

## 16. 冗余表审计结论

当前结构中没有可以仅凭名称或行数直接判定为多余的表：

- 没有独立的前端测试表；
- 没有登录用户表或 Session 表；
- 没有 Demo 专用表；
- 空的 `query_record`、`citation_record`、`feedback_record` 仍是正式功能表；
- 层级、实体和关系表虽然可重建，但属于当前检索能力；
- `alembic_version` 是迁移基线，不能删除；
- `rag_flow` 的空表也可能属于 RAGFlow 官方功能，不能由本仓库擅自清理。

“空表”不等于“冗余表”，“包含测试记录”也不等于“测试表”。清理测试数据时，应通过
明确的 Tenant、User、Scope、知识库 ID 或 External ID 精确选择，并优先走应用 API。

## 17. RAGFlow 数据库初始化

`rag_flow` 不使用本仓库 `migrations/versions` 下的 Alembic 脚本：

1. DBA 创建空数据库 `rag_flow`，Owner 为业务账号 `lazytt_rag`；
2. DBA 从固定版本 RAGFlow v0.26.4 的干净 Schema 生成物安装全部表；
3. DBA 确认 `lazytt_rag` 持有 `public` Schema、表、序列和函数，并保留 Schema
   CREATE，以满足固定版 RAGFlow 的官方兼容性 DDL；
4. 将 `RAGFLOW_POSTGRES_*` 指向该数据库；
5. 启动固定镜像 `infiniflow/ragflow:v0.26.4`；其内置初始化检查只能做幂等确认，
   不能依赖它在生产运行账号下建表；
6. 验证 RAGFlow Server、Task Executor、管理界面和 API。

禁止使用自研 `schema.sql` 初始化 `rag_flow`，也禁止直接修改 RAGFlow 数据库来伪造
模型、用户、API Key 或 Dataset 配置。

## 18. 自动启动顺序

```text
读取 deploy/local/.env.ragflow
  -> 生成 Git 忽略的 deploy/local/.env.full
  -> 分别检查 lazytt-rag-storage 与 lazytt-rag-runtime
  -> 本地模式启动 storage；外部模式跳过本地 storage
  -> 本地模式幂等准备 rag_app 并登记 V1.0.0；外部模式跳过全部 Schema DDL
  -> rag-app-schema-check 只读检查 rag_app 与 rag_flow
  -> 启动 RAGFlow Server 与 Task Executor
  -> 验证 RAGFlow 账号、API Key 和模型
  -> rag-app-bootstrap 执行应用启动预检
  -> 启动 Gateway、Worker、Scheduler
  -> Gateway 健康检查通过
```

`rag-app-schema-check` 的默认命令是一次性只读门禁，成功后显示 `Exited (0)` 是正常
现象。`start-full.sh` 只在 local 模式先用同一镜像运行显式 `schema prepare-local`；该
命令只维护 `rag_app`，不接触 `rag_flow`，且表集合、Revision 或发布字段异常时拒绝
自动修复。external 模式不会调用写命令，缺表、版本、权限或所有权不符时仍要求 DBA
修复。

本地部署在 Docker Desktop 中显示为两个独立 Compose Project：

- `lazytt-rag-storage`：PostgreSQL、Redis Stack、Elasticsearch、MinIO；
- `lazytt-rag-runtime`：Gateway、Worker、Scheduler、RAGFlow 和一次性初始化容器。

两者通过外部共享 Docker Network 通信。拆成两个 Project 不会把 PostgreSQL 拆成两个
实例；storage Project 内仍只有一个 PostgreSQL 服务和两个逻辑数据库。

异步任务状态以 `rag_app.ingestion_job` 为事实来源。统一 Redis Stack 的 DB 2 只作为
Celery Broker，Celery Result Backend 已关闭；不能通过 Redis 结果 Key 替代 PostgreSQL
中的 Job 状态。

## 19. 手动初始化与检查命令

### 19.1 查看迁移链

```bash
uv run alembic history
uv run alembic heads
```

预期只有一个 Head：`0006_document_version_updates`。

### 19.2 生成与评审 App 迁移（仅开发/DBA 流程）

以下命令只用于开发者在隔离环境验证六个演进版本，不能放进生产启动脚本，也不能使用
生产运行账号执行：

```bash
APP_DATABASE_DSN='<隔离验证数据库 DSN>' uv run alembic upgrade head
```

生产/验收安装应由 DBA 直接执行：

```bash
psql -X -v ON_ERROR_STOP=1 -f \
  migrations/releases/V1.0.0__fresh_install.sql
```

### 19.3 只读检查当前版本

```bash
APP_DATABASE_DSN='<外部 rag_app DSN>' uv run alembic current
```

或查询：

```sql
SELECT version_num FROM rag_app.alembic_version;
```

预期为 `0006_document_version_updates`。

### 19.4 查看 App 表

连接 `rag_app` 后执行：

```sql
SELECT table_schema, table_name
FROM information_schema.tables
WHERE table_schema = 'rag_app'
ORDER BY table_name;
```

### 19.5 查看 RAGFlow 表

连接 `rag_flow` 后执行：

```sql
SELECT table_schema, table_name
FROM information_schema.tables
WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
ORDER BY table_schema, table_name;
```

这里只检查初始化结果，不应手工修改 RAGFlow 表。

## 20. 初始化完成验收

### 20.1 外部单账号模式 PostgreSQL 权限

- [ ] `lazytt_rag` 能连接 `rag_app`；
- [ ] `lazytt_rag` 能连接 `rag_flow`；
- [ ] `lazytt_rag` 不是超级管理员；
- [ ] `lazytt_rag` 不能创建账号；
- [ ] `lazytt_rag` 不能创建第三个数据库；
- [ ] 非授权普通账号不能连接两个数据库。

单一业务账号模式还应验证两个数据库和目标 Schema 的 Owner 均为 `lazytt_rag`，且该
账号不是管理员、不能创建角色或第三个数据库。

### 20.2 App Schema

- [ ] `rag_app` Schema 存在；
- [ ] 12 张业务表存在；
- [ ] `alembic_version` 存在；
- [ ] 当前 Revision 为 `0006_document_version_updates`；
- [ ] 必要索引、约束和更新时间触发器存在。

### 20.3 RAGFlow

- [ ] RAGFlow Server 和 Task Executor 健康；
- [ ] RAGFlow 可以登录且 API Key 有效；
- [ ] Embedding 和 Vision 模型配置有效；
- [ ] 可以创建 Dataset 并上传非业务 Fixture。

### 20.4 端到端业务

- [ ] Gateway `/health/ready` 返回健康；
- [ ] 可以创建知识库；
- [ ] 可以上传文档并完成解析和元数据增强；
- [ ] 可以执行混合检索；
- [ ] 可以执行普通和 SSE 流式问答；
- [ ] 引用可以回溯真实文档 Chunk；
- [ ] 容器重启后结构和配置仍然有效。

## 21. 常见故障与处理

### 21.1 密码认证失败

外部单账号模式应检查两个密码变量是否完全相同、DSN 是否使用同一密码、特殊字符是否
已经 URL 编码，以及 `lazytt_rag` 是否使用预期密码创建。本地默认双账号模式则应分别
核对 `rag_app`、`rag_flow` 的密码，不能要求二者相同。

### 21.2 数据库不存在

检查 `rag_app` 和 `rag_flow` 是否分别创建，以及名称是否与配置完全一致。

### 21.3 Alembic 权限不足

确认 `rag_app` 的 Owner 是 `lazytt_rag`，且该账号能创建 Schema、表、索引、函数和
触发器。

### 21.4 `rag_app` 处于半初始化状态

不要手工填写 `alembic_version`。先检查已存在表和迁移日志，再决定继续升级，或在确认
空库无数据后由 DBA 重建目标数据库。

### 21.5 RAGFlow 无法初始化

检查 `RAGFLOW_POSTGRES_DATABASE=rag_flow`、业务账号、网络连接、Owner、连接权限，
以及 RAGFlow Server 和 Task Executor 日志。

### 21.6 两个服务误连同一数据库

必须保证：

```text
APP_DATABASE_DSN          -> rag_app
RAGFLOW_POSTGRES_DATABASE -> rag_flow
```

虽然账号相同，但数据库名必须不同。

### 21.7 修改地址后仍连接旧数据库

不要只改自动生成的 `.env.full`。应修改 `deploy/local/.env.ragflow`，重新执行启动流程，让
Compose 重新创建使用新环境变量的容器。

## 22. 明确禁止的操作

- 不使用 PostgreSQL 管理员作为运行账号；
- 不把管理员密码写入项目配置；
- 不把 `rag_app` 和 `rag_flow` 合并为同一数据库；
- 不给 `lazytt_rag` 超级管理员、创建账号或创建数据库权限；
- 不手工伪造 `rag_app.alembic_version`；
- 不使用 `schema.sql` 覆盖已有数据库；
- 不导入旧 PostgreSQL Dump；
- 不导入旧 Elasticsearch、MinIO 或 Redis Stack 数据；
- 不修改 RAGFlow 源码或直接伪造其数据库配置；
- 不提交真实数据库密码、管理员凭据或 DSN；
- 不为清理提示而执行 `docker compose down -v`。

## 23. 最终结论

```text
外部服务器方案：管理员只负责首次创建，不参与应用运行

lazytt_rag
├── rag_app  -> Alembic 0001～0006 -> Gateway/Worker/Scheduler
└── rag_flow -> RAGFlow v0.26.4 官方初始化 -> RAGFlow Server/Task Executor
```

本地默认部署使用 `rag_app`、`rag_flow` 两个运行账号；外部数据库服务器可以使用上面的
统一账号。对于新的数据库服务器，正确做法是创建空账号和两个空数据库，然后分别让
Alembic 与 RAGFlow 官方流程初始化结构。旧环境业务数据以及其他存储系统的数据均不在
本策略范围内。
