# DBA 主导的空数据库迁移模拟验收手册

> 历史参考：本文保留改造前的流程与审计说明。2026-09-07 起，部署与 SQL 执行以[当前 ECS 操作入口](../../deploy/ecs/README.md)为准；本文旧 `migrations/releases`、Alembic 或一体化安装命令不再执行。旧文件可在 Git 检查点 `335d418` 查阅。

## 1. 目标与边界

本文指导数据库管理员在一个**全新的、可丢弃的 PostgreSQL 测试实例**中完成 LazyTT
RAG 数据库初始化，再由应用维护人员把系统切到该实例进行验收。

本次“迁移”指把数据库结构部署到新服务器，不复制旧业务数据：

- 创建唯一项目角色 `lazytt_rag`；该角色可登录，并直接持有两个项目数据库及对象；
- 创建 `rag_app`、`rag_flow` 两个逻辑数据库；
- 创建当前应用 Alembic `0006` 和 RAGFlow v0.26.4 所需的完整表结构；
- 给业务账号授予两个项目数据库内的所有权、连接、DML、序列和 Schema CREATE 权限；
- 应用启动时只读校验结构，绝不自动建表或升级表。

本文不迁移旧 PostgreSQL 数据，也不迁移 Redis、Elasticsearch 或 MinIO 数据。若以后
需要保留旧业务数据，必须另行设计停机、备份、一致性和回滚方案，不能套用本文。

## 2. 责任边界

| 角色 | 可以做什么 | 明确不能做什么 |
|---|---|---|
| DBA 管理员 | 创建角色、数据库、Schema、表、索引、函数、触发器和权限 | 不把管理员密码交给应用，不伪造 RAGFlow 业务数据 |
| `lazytt_rag` | 唯一项目账号；持有并连接两个数据库，可在两个项目 Schema 内维护对象 | 不能创建其他数据库或角色，不具备超级用户、复制或绕过 RLS 权限 |
| 应用维护人员 | 配置连接地址，启动系统，查看只读 Schema 门禁和健康状态 | 不运行 Alembic，不使用账号权限绕过受审计的 DBA 发布流程 |

## 3. 使用的交付文件

| 文件 | 用途 |
|---|---|
| [`V1.0.0__fresh_install.sql`](../../migrations/releases/V1.0.0__fresh_install.sql) | DBA 在全新 PostgreSQL 实例上执行的一次性完整安装 SQL |
| [`lazytt_rag_single_role_migration.sql`](./lazytt_rag_single_role_migration.sql) | DBA 将已经存在的旧双角色实例迁移为单角色模型；空库演练不执行 |
| [`DATABASE_CREATION_AND_PERMISSIONS.md`](./DATABASE_CREATION_AND_PERMISSIONS.md) | 角色、数据库、权限和验收依据 |
| [`DATABASE_REFERENCE.md`](./DATABASE_REFERENCE.md) | 全部存储结构和字段字典 |
| [`DATABASE_CONNECTION_CONFIGURATION.md`](./DATABASE_CONNECTION_CONFIGURATION.md) | 应用连接本地或外部存储的方法 |

SQL 文件同时包含 `rag_app` 和 `rag_flow` 的 PostgreSQL 表结构。它不会创建 RAGFlow
管理员、API Key、模型实例、Dataset 或文档业务数据。

## 4. 模拟环境准备

仓库已经提供独立的四存储模拟环境：

- Compose：`deploy/local/external-storage-lab/docker-compose.yml`；
- 私有配置模板：`deploy/local/external-storage-lab/.env.example`；
- Compose Project：`lazytt-rag-external-storage-lab`；
- PostgreSQL DBA 端口：默认 `127.0.0.1:15432`。

先复制私有配置并替换全部 `change-me`，真实值不能提交：

```bash
cp deploy/local/external-storage-lab/.env.example deploy/local/external-storage-lab/.env
chmod 600 deploy/local/external-storage-lab/.env
```

然后只启动空白存储服务：

```bash
docker compose \
  --env-file deploy/local/external-storage-lab/.env \
  -f deploy/local/external-storage-lab/docker-compose.yml \
  up -d --wait
```

该 Compose 没有 `docker-entrypoint-initdb.d` 挂载，也不包含任何迁移容器或初始化
脚本。`healthy` 只证明 PostgreSQL、Redis Stack、Elasticsearch 和 MinIO 可连接，
不代表业务角色、数据库或表已经创建。实际公司数据库仍由 DBA 按公司的备份、审计、
网络和 Secret 规范提供，不要求使用 Docker。

## 5. 第一阶段：DBA 执行数据库安装

### 5.1 确认目标是空环境

DBA 使用 `deploy/local/external-storage-lab/.env` 中仅供本次演练的管理员账号连接 `postgres`
管理库：

```bash
psql \
  --host=127.0.0.1 \
  --port=15432 \
  --username=<POSTGRES_ADMIN_USER> \
  --dbname=postgres
```

执行只读检查：

```sql
SELECT rolname
FROM pg_roles
WHERE rolname = 'lazytt_rag';

SELECT datname
FROM pg_database
WHERE datname IN ('rag_app', 'rag_flow');
```

全新模拟环境中两条查询都应返回 0 行。若存在任何同名对象，停止操作并更换空实例；
安装 SQL 不会覆盖或删除它们。

### 5.2 审计 SQL 文件

执行前至少确认：

```bash
git status --short
git rev-parse --short HEAD
shasum -a 256 migrations/releases/V1.0.0__fresh_install.sql
```

将提交号和 SHA-256 记录进本次变更单。SQL 中不能出现真实密码、`DROP DATABASE`、
`DROP ROLE` 或业务数据插入语句。

### 5.3 运行完整安装 SQL

在仓库根目录执行：

```bash
psql \
  --host=127.0.0.1 \
  --port=15432 \
  --username=<POSTGRES_ADMIN_USER> \
  --dbname=postgres \
  --file=migrations/releases/V1.0.0__fresh_install.sql
```

脚本执行到 `\password lazytt_rag` 时，终端会要求输入并确认运行账号密码。该密码只通过
公司的 Secret 渠道交给应用维护人员，不记录在 SQL、工单正文或共享终端输出中。

脚本成功结束时会输出：

```text
SUCCESS: PostgreSQL 空库结构和最小权限初始化完成。
```

`CREATE DATABASE` 不能处于普通事务中，因此整个脚本无法跨两个数据库做全局事务回滚。
中途失败时不要直接重跑；先由 DBA 核对已经创建的角色、数据库和表，再决定在这个
可丢弃实验实例上重建，或使用单独的修复 SQL。

## 6. 第二阶段：DBA 验收结构与最小权限

### 6.1 角色和数据库所有权

```sql
SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole,
       rolreplication, rolbypassrls
FROM pg_roles
WHERE rolname = 'lazytt_rag'
ORDER BY rolname;

SELECT datname, pg_get_userbyid(datdba) AS owner
FROM pg_database
WHERE datname IN ('rag_app', 'rag_flow')
ORDER BY datname;
```

预期：

- `lazytt_rag.rolcanlogin = true`；
- 该角色的实例级管理权限全部是 `false`；
- 两个数据库的所有者都是 `lazytt_rag`。

### 6.2 表数量与应用版本

```bash
psql -h <POSTGRES_HOST> -p <POSTGRES_PORT> -U lazytt_rag -d rag_app \
  -c 'SELECT version_num FROM rag_app.alembic_version;'

psql -h <POSTGRES_HOST> -p <POSTGRES_PORT> -U lazytt_rag -d rag_app \
  -c "SELECT count(*) FROM information_schema.tables WHERE table_schema='rag_app';"

psql -h <POSTGRES_HOST> -p <POSTGRES_PORT> -U lazytt_rag -d rag_flow \
  -c "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_type='BASE TABLE';"
```

当前预期：

- Alembic Revision：`0006_document_version_updates`；
- `rag_app`：13 张表（含 `alembic_version`）；
- `rag_flow`：42 张基础表。

### 6.3 证明两个项目 Schema 使用统一所有权

```bash
psql -h <POSTGRES_HOST> -p <POSTGRES_PORT> -U lazytt_rag -d rag_app \
  -c "SELECT has_schema_privilege(current_user, 'rag_app', 'CREATE');"

psql -h <POSTGRES_HOST> -p <POSTGRES_PORT> -U lazytt_rag -d rag_flow \
  -c "SELECT has_schema_privilege(current_user, 'public', 'CREATE');"
```

两条都必须为 `true`。这是单角色所有权契约，不等于把运行账号提升为 PostgreSQL
管理员；它仍不能创建其他数据库或角色。还必须按
[`DATABASE_CREATION_AND_PERMISSIONS.md`](./DATABASE_CREATION_AND_PERMISSIONS.md)
确认 `rag_app.rag_app`、`rag_flow.public` 和全部基础表都由 `lazytt_rag` 直接持有。

### 6.4 验证业务 DML 权限

可以在**可丢弃的模拟环境**中验证事务内写入并回滚：

```bash
psql -h <POSTGRES_HOST> -p <POSTGRES_PORT> -U lazytt_rag -d rag_app <<'SQL'
BEGIN;
INSERT INTO rag_app.config_profile (
    id, profile_type, name, version, description,
    config_json, checksum_sha256, enabled
) VALUES (
    gen_random_uuid(), 'CACHE', 'dba-permission-smoke', 1,
    'transaction rollback fixture', '{}'::jsonb,
    repeat('0', 64), false
);
ROLLBACK;
SQL
```

这一步只允许在明确的验收实例执行。事务回滚后不保留测试记录。

## 7. 第三阶段：应用维护人员配置连接

只编辑 Git 忽略文件：

```text
deploy/local/.env.ragflow
```

不要直接编辑自动生成的 `deploy/local/.env.full`。使用脱敏结构：

```dotenv
RAG_STORAGE_MODE=external

APP_DATABASE_DSN=postgresql+psycopg://lazytt_rag:<URL_ENCODED_PASSWORD>@<POSTGRES_HOST>:<POSTGRES_PORT>/rag_app

RAGFLOW_POSTGRES_HOST=<POSTGRES_HOST>
RAGFLOW_POSTGRES_PORT=<POSTGRES_PORT>
RAGFLOW_POSTGRES_DATABASE=rag_flow
RAGFLOW_POSTGRES_USER=lazytt_rag
RAGFLOW_POSTGRES_PASSWORD=<POSTGRES_PASSWORD>
```

DSN 中的密码必须做 URL 编码；独立的 `RAGFLOW_POSTGRES_PASSWORD` 使用原始密码。
运行配置中绝不能出现 DBA 管理员账号。

Redis Stack、Elasticsearch、MinIO 仍按
[`DATABASE_CONNECTION_CONFIGURATION.md`](./DATABASE_CONNECTION_CONFIGURATION.md) 配置。
本文不要求迁移旧数据，所以这些存储可以使用新的空实例。

## 8. 第四阶段：启动并验证应用不会执行迁移

### 8.1 正常启动

```bash
./deploy/local/start-full.sh
```

关键启动顺序：

```text
读取私有配置
  -> 生成 deploy/local/.env.full
  -> rag-app-schema-check 只读校验两个 PostgreSQL 数据库
  -> RAGFlow Server / Task Executor
  -> RAGFlow 官方账号、API Key、模型 Bootstrap
  -> rag-app-bootstrap 导入业务 Profile、创建 Redis 语义缓存索引
  -> Gateway / Worker / Scheduler
```

`rag-app-bootstrap` 会写入业务 Profile 和 Redis 索引，这属于正常 DML/缓存初始化；它不
创建或修改 PostgreSQL 表结构。

### 8.2 检查 Schema 门禁

```bash
docker compose --env-file deploy/local/.env.full \
  -f compose.local.yml ps -a rag-app-schema-check

docker compose --env-file deploy/local/.env.full \
  -f compose.local.yml logs --no-log-prefix rag-app-schema-check
```

成功结果必须包含：

```json
{"ddl_executed": false, "status": "passed"}
```

输出中不应出现密码、完整 DSN 或管理员账号。

### 8.3 验证运行服务

```bash
curl -fsS http://127.0.0.1:8080/health/live
curl -fsS http://127.0.0.1:8080/health/ready
```

随后使用非业务 Fixture 创建知识库、上传文档、等待解析、检索并删除测试资源。该
Smoke 证明业务读写链路正常；DDL/对象所有权仍由前面的系统目录查询单独验收。

## 9. 失败路径验收

以下项目只在可丢弃实验环境进行：

| 场景 | 预期结果 | 正确处理 |
|---|---|---|
| 未执行安装 SQL | `rag-app-schema-check` 报缺表，Runtime 不继续启动 | DBA 执行受审计安装 SQL |
| `rag_app` Revision 不是 `0006` | 门禁报版本不一致 | DBA 提供单独的升级 SQL；应用不得执行 Alembic |
| 少一张 RAGFlow 表 | 门禁报缺表 | DBA 按固定镜像基线修复 Schema |
| `lazytt_rag` 在任一项目 Schema 没有 CREATE 或不是对象 owner | Schema 门禁或 RAGFlow 启动失败 | DBA 执行受审计的单角色所有权修复 SQL |
| 密码或网络错误 | PostgreSQL 连接失败 | 修复 Secret、URL 编码、网络或 `pg_hba.conf` |

不要通过下列方式“修复”门禁：

- 不把 DBA 密码写入 `.env.ragflow`；
- 不给 `lazytt_rag` 授予超级用户、创建角色或创建其他数据库权限；
- 不撤销 `lazytt_rag` 在两个项目数据库中的所有权和 Schema CREATE；
- 不删除版本表或手工伪造 Revision；
- 不修改代码跳过 Schema 检查；
- 不直接修改 RAGFlow 表伪造账号、Key、模型或 Dataset。

## 10. 后续 Schema 版本发布流程

未来出现 `0007` 或新的 RAGFlow 固定版本时，应采用以下流程：

1. 开发团队提交 Migration 源码和对应的、可审计的 DBA SQL；
2. 在临时数据库从当前生产基线演练升级；
3. DBA 评估锁表、索引耗时、回滚/恢复和容量；
4. 变更窗口内先停止写流量，再由 DBA 执行 SQL；
5. DBA 验证结构与权限；
6. 部署包含新 Schema 指纹的应用版本；
7. 应用只读门禁通过后恢复流量。

应用镜像永远不携带可用于生产 DDL 的数据库管理员凭据。

## 11. 模拟环境清理

仅当目标明确是本次可丢弃实验实例时，按实验环境自己的 Compose 或平台流程删除。
不要对正常 LazyTT RAG 环境执行 `docker compose down -v`，也不要删除已有生产卷。

最终验收证据应保留：SQL 文件 SHA-256、Git 提交、DBA 执行时间、角色属性、数据库
Owner、表数量、Revision、Schema 权限查询和 `rag-app-schema-check` 脱敏结果；不得保留
密码、Token、完整 DSN 或业务正文。
