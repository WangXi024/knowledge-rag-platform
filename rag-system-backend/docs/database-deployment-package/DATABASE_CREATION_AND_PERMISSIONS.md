# LazyTT RAG 数据库创建与权限说明

> 历史参考：本文保留改造前的流程与审计说明。2026-09-07 起，部署与 SQL 执行以[当前 ECS 操作入口](../../deploy/ecs/README.md)为准；本文旧 `migrations/releases`、Alembic 或一体化安装命令不再执行。旧文件可在 Git 检查点 `335d418` 查阅。

## 1. 文档目的

本文面向数据库管理员（DBA），说明如何为 LazyTT RAG 在一个**全新的 PostgreSQL
实例**中创建数据库、运行账号和权限。本文不迁移旧数据，也不要求运行时服务持有
PostgreSQL 管理员账号。

适用基线：

- LazyTT RAG 当前数据库迁移头：`0006_document_version_updates`；
- RAGFlow：`infiniflow/ragflow:v0.26.4`；
- PostgreSQL：当前本地基线为 `18.4`；
- 初始化脚本：[`V1.0.0__fresh_install.sql`](../../migrations/releases/V1.0.0__fresh_install.sql)。

> 该 SQL 是“全新空库初始化脚本”，不是旧数据导入脚本。目标实例中已经存在同名
> 角色、数据库或业务表时，应先由 DBA 审计，不能直接覆盖执行。

## 2. 最终数据库规划

一个 PostgreSQL 实例承载两个逻辑数据库，并由同一个非管理员业务账号访问：

| 对象 | 固定名称 | 用途 |
|---|---|---|
| 业务账号与对象所有者 | `lazytt_rag` | 唯一项目角色；Gateway、Worker、RAGFlow 用它连接 PostgreSQL，并由它持有两个数据库及其对象 |
| 应用数据库 | `rag_app` | 自研 Gateway 控制面数据、任务、索引侧车、引用、反馈等 |
| RAGFlow 数据库 | `rag_flow` | RAGFlow 的账号、租户、知识库、文档、任务、模型配置等元数据 |
| 应用 Schema | `rag_app` | `rag_app` 数据库中的自研应用表 |
| RAGFlow Schema | `public` | `rag_flow` 数据库中的 RAGFlow 官方表 |

`lazytt_rag` 是两个项目数据库、业务 Schema 和业务对象的所有者，但不是 PostgreSQL
超级用户，也不具有 `CREATEDB`、`CREATEROLE`、`REPLICATION` 或 `BYPASSRLS`。
RAGFlow v0.26.4 会在启动兼容检查和任务入队阶段执行受限 DDL，因此该账号必须拥有
`rag_flow.public`；为保持一个账号、一套一致权限模型，它也拥有 `rag_app.rag_app`。
结构变更仍只能由 DBA 审核 SQL 后人工执行，应用启动流程不会自动运行迁移。

## 3. 权限模型

| 权限 | `rag_app` | `rag_flow` | 说明 |
|---|---:|---:|---|
| `CONNECT` | 是 | 是 | 允许业务服务连接 |
| `TEMPORARY` | 是 | 是 | 允许查询或迁移期间使用临时对象 |
| 数据库所有者 | 是 | 是 | 两个数据库均由唯一业务账号 `lazytt_rag` 持有 |
| Schema `USAGE` | 是 | 是 | 可以访问 DBA 已创建的对象 |
| Schema `CREATE` | 是 | 是 | 单角色模型要求账号能维护两个项目 Schema；应用启动仍不自动执行 DDL |
| Schema/表所有者 | 是 | 是 | 两套 Schema 与基础表都由 `lazytt_rag` 直接持有 |
| 表 DML | 是 | 是 | 仅允许 `SELECT/INSERT/UPDATE/DELETE` |
| 序列使用 | 是 | 是 | 仅允许业务写入所需的 `USAGE/SELECT/UPDATE` |
| 超级用户 | 否 | 否 | 禁止运行时获得实例管理权限 |
| 创建角色 | 否 | 否 | 账号与权限仍由 DBA 管理 |
| 创建其他数据库 | 否 | 否 | 不能越界创建新业务数据库 |

项目只创建一个业务账号 `lazytt_rag`，不再创建 `lazytt_rag_owner`。这简化了部署和
RAGFlow 的所有权兼容性，但也意味着该账号泄露时两个项目数据库的结构与数据都会受
影响。因此必须通过网络白名单、Secret 管理、连接审计和备份保护该账号；数据库实例
管理员账号绝不能写入应用配置。

## 4. 执行前提

DBA 需要准备：

1. 一个全新的或已确认无同名业务对象的 PostgreSQL 实例；
2. 能创建角色和数据库的管理员连接；
3. `psql` 客户端，建议与目标 PostgreSQL 主版本一致；
4. 一个只通过安全渠道传递的 `lazytt_rag` 随机密码；
5. 已核对 SQL 文件的 Git 提交与 RAGFlow v0.26.4 基线一致。

不要把密码写进 SQL、Markdown、Git、聊天记录或命令历史。初始化脚本会使用 psql 的
交互式 `\password lazytt_rag` 提示，让 DBA 在终端中输入密码。

## 5. DBA 执行方法

### 5.1 全新空实例

在仓库根目录执行：

```bash
psql \
  --host=<POSTGRES_HOST> \
  --port=5432 \
  --username=<DBA_ADMIN_USER> \
  --dbname=postgres \
  --file=migrations/releases/V1.0.0__fresh_install.sql
```

脚本的执行顺序是：

1. 检查 `lazytt_rag`、`rag_app`、`rag_flow` 不存在；
2. 创建无管理员权限的登录角色 `lazytt_rag`，并交互式设置该账号密码；
3. 创建 `rag_app` 和 `rag_flow`，所有者均为 `lazytt_rag`；
4. 撤销 `PUBLIC` 对两个数据库的默认连接权限；
5. 以 `lazytt_rag` 身份创建完整 `rag_app` Schema，并记录 Alembic 头版本；
6. 以同一账号创建与 RAGFlow v0.26.4 对应的完整 `rag_flow` Schema，使
   官方运行时能执行自己的兼容性 DDL；
7. 输出单角色所有权、表数量、Revision 和 Schema CREATE 权限校验信息。

SQL 使用 `ON_ERROR_STOP`，任一步失败都会停止，不会静默跳过错误。但 PostgreSQL 的
`CREATE DATABASE` 不能放入同一个事务，因此脚本不是全局原子操作；若中途失败，必须
由 DBA 检查已创建对象，再决定继续或清理，不能盲目重复执行。

### 5.2 已使用旧双角色方案的实例

如果目标实例已经采用 `lazytt_rag_owner`（对象所有者）和 `lazytt_rag`（运行账号）
双角色方案，不得执行空库安装 SQL。应先停止运行服务并完成备份，再由 DBA 审计并执行：

```bash
psql \
  --host=<POSTGRES_HOST> \
  --port=5432 \
  --username=<DBA_ADMIN_USER> \
  --dbname=postgres \
  --file=docs/database-deployment-package/lazytt_rag_single_role_migration.sql
```

该脚本只转移两个数据库、Schema 和对象的所有权与权限，最后删除旧的
`lazytt_rag_owner`；它不创建新表，也不修改业务行。执行后必须完成第 6 节的全部校验，
再启动系统并让只读 Schema 门禁复核结果。

## 6. 初始化后的验证

### 6.1 验证账号属性

```sql
SELECT rolname, rolsuper, rolcreaterole, rolcreatedb, rolreplication
FROM pg_roles
WHERE rolname = 'lazytt_rag'
ORDER BY rolname;
```

预期仅一行：`lazytt_rag` 可以登录，所有管理属性均为 `false`。

### 6.2 验证两个数据库

```sql
SELECT datname, pg_get_userbyid(datdba) AS owner
FROM pg_database
WHERE datname IN ('rag_app', 'rag_flow')
ORDER BY datname;
```

预期两行的 `owner` 都是 `lazytt_rag`。

### 6.3 验证统一的 Schema 所有权边界

```bash
psql -h <POSTGRES_HOST> -U lazytt_rag -d rag_app \
  -c "SELECT has_schema_privilege(current_user, 'rag_app', 'CREATE');"

psql -h <POSTGRES_HOST> -U lazytt_rag -d rag_flow \
  -c "SELECT has_schema_privilege(current_user, 'public', 'CREATE');"
```

预期两个结果都为 `true`。这表示 `lazytt_rag` 是两个项目 Schema 的所有者，并不表示
它具有实例级管理权限。继续确认两套 Schema 和表都由该账号直接持有：

```bash
psql -h <POSTGRES_HOST> -U lazytt_rag -d rag_app -c "
SELECT pg_get_userbyid(nspowner) AS schema_owner
FROM pg_namespace WHERE nspname='rag_app';
SELECT count(*) AS tables_not_owned_by_runtime
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname='rag_app' AND c.relkind IN ('r','p')
  AND pg_get_userbyid(c.relowner) <> current_user;"

psql -h <POSTGRES_HOST> -U lazytt_rag -d rag_flow -c "
SELECT pg_get_userbyid(nspowner) AS schema_owner
FROM pg_namespace WHERE nspname='public';
SELECT count(*) AS tables_not_owned_by_runtime
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname='public' AND c.relkind IN ('r','p')
  AND pg_get_userbyid(c.relowner) <> current_user;"
```

两次都应得到 `schema_owner=lazytt_rag`、`tables_not_owned_by_runtime=0`。

### 6.4 验证应用 Schema 版本

```bash
psql -h <POSTGRES_HOST> -U lazytt_rag -d rag_app \
  -c 'TABLE rag_app.alembic_version;'
```

预期为：

```text
0006_document_version_updates
```

### 6.5 验证表数量

```bash
psql -h <POSTGRES_HOST> -U lazytt_rag -d rag_app \
  -c "SELECT count(*) FROM information_schema.tables WHERE table_schema='rag_app';"

psql -h <POSTGRES_HOST> -U lazytt_rag -d rag_flow \
  -c "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE';"
```

当前基线预期：

- `rag_app`：13 张表（含 `alembic_version`）；
- `rag_flow`：42 张 RAGFlow 基础表。

## 7. RAGFlow 首次启动还会做什么

本 SQL 只创建表结构，不伪造 RAGFlow 管理员、API Key、模型实例或业务数据。第一次
启动固定版本的 RAGFlow 时，官方程序仍会：

- 校验自己的表结构，并在需要时执行官方兼容性 DDL；
- 写入官方内置的系统设置与模板；
- 由本项目启动流程通过官方 API 完成管理员/API Key Bootstrap；
- 通过官方模型配置接口注册启用的模型。

因此，执行 SQL 后不能直接向 `rag_flow` 手工插入账号或 API Key，也不能把“表已创建”
误认为 RAGFlow 已完成全部初始化。

## 8. 明确禁止事项

- 禁止使用本 SQL 覆盖正在使用的数据库；
- 禁止把 DBA 管理员密码交给 Gateway 或 RAGFlow；
- 禁止撤销 `lazytt_rag` 对 `rag_app`、`rag_flow` 目标 Schema 的所有权或 `CREATE`
  权限；
- 禁止把 `rag_flow` 所需权限错误扩展成 PostgreSQL 超级用户、`CREATEDB` 或
  `CREATEROLE`；
- 禁止把真实密码写入仓库；
- 禁止直接修改 RAGFlow 表来伪造管理员、API Key 或模型配置；
- 禁止把此文件用于不同 RAGFlow 版本而不先重新做 Schema 兼容审计；
- 禁止在未确认目标的情况下执行 `DROP DATABASE`、`DROP ROLE` 或清理数据卷。
