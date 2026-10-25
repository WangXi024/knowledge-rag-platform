# LazyTT RAG 存储连接配置手册

> 历史参考：本文保留改造前的流程与审计说明。2026-09-07 起，部署与 SQL 执行以[当前 ECS 操作入口](../../deploy/ecs/README.md)为准；本文旧 `migrations/releases`、Alembic 或一体化安装命令不再执行。旧文件可在 Git 检查点 `335d418` 查阅。

## 1. 文档目的

本文说明数据库管理员完成空库初始化后，应用维护人员如何让 LazyTT RAG 连接到
PostgreSQL、Redis Stack、Elasticsearch 和 MinIO。本文只修改 Git 忽略的本机配置，
不要求修改 Python 代码或 RAGFlow 源码。

## 2. 配置入口

私有配置文件：

```text
deploy/local/.env.ragflow
```

模板文件：

```text
deploy/local/.env.ragflow.example
```

`deploy/local/.env.full` 是 `start-full.sh` 根据 `.env.ragflow` 生成的运行时文件，不应作为
人工维护入口。每次启动都可能重新生成它，因此应始终修改 `.env.ragflow`。

## 3. 存储模式

### 3.1 本地独立存储 Compose

```dotenv
RAG_STORAGE_MODE=local
```

此模式由 `deploy/local/docker-compose.storage.yml` 启动 PostgreSQL、Redis Stack、
Elasticsearch 和 MinIO。对应的 `*_IMAGE` 变量只在这个模式下用于选择容器镜像。

### 3.2 外部数据库服务器

```dotenv
RAG_STORAGE_MODE=external
```

此模式不会启动本地存储 Compose。运行时容器只连接外部地址，`POSTGRES_IMAGE`、
`REDIS_STACK_IMAGE`、`ELASTICSEARCH_IMAGE`、`MINIO_IMAGE` 不参与连接，可以从私有
配置中删除。远端服务版本由数据库服务器自己的运维配置管理。

## 4. 外部存储配置模板

下面只有占位符，不能填入真实密钥后提交 Git：

```dotenv
RAG_STORAGE_MODE=external

# 自研应用数据库。密码必须做 URL 编码；例如 @、:、/ 等字符不能原样放进 DSN。
APP_DATABASE_DSN=postgresql+psycopg://lazytt_rag:<URL_ENCODED_PASSWORD>@<POSTGRES_HOST>:5432/rag_app

# RAGFlow 使用同一个 PostgreSQL 实例、同一个业务账号，但连接另一个逻辑数据库。
RAGFLOW_POSTGRES_HOST=<POSTGRES_HOST>
RAGFLOW_POSTGRES_PORT=5432
RAGFLOW_POSTGRES_DATABASE=rag_flow
RAGFLOW_POSTGRES_USER=lazytt_rag
RAGFLOW_POSTGRES_PASSWORD=<POSTGRES_PASSWORD>

# 统一 Redis Stack：DB 0=缓存，DB 1=RAGFlow，DB 2=Celery。
REDIS_STACK_HOST=<REDIS_HOST>
REDIS_STACK_PORT=6379
REDIS_STACK_PASSWORD=<REDIS_PASSWORD>

# RAGFlow 全文检索/向量索引。
RAGFLOW_ES_SCHEME=http
RAGFLOW_ES_HOST=<ELASTICSEARCH_HOST>
RAGFLOW_ES_PORT=9200
ELASTIC_PASSWORD=<ELASTIC_PASSWORD>

# RAGFlow 原始文件和解析对象。
RAGFLOW_MINIO_HOST=<MINIO_HOST>
RAGFLOW_MINIO_PORT=9000
MINIO_USER=<MINIO_ACCESS_KEY>
MINIO_PASSWORD=<MINIO_SECRET_KEY>
```

地址选择规则：

- 存储服务运行在同一台 macOS 宿主机、应用运行在 Docker 中：可用
  `host.docker.internal`；
- 存储服务运行在局域网服务器：使用固定 IP 或内网 DNS，例如
  `db.internal.example`；
- 不要在容器配置中使用 `127.0.0.1` 指向宿主机或远端服务器；容器内的
  `127.0.0.1` 只代表容器自己。

同一个变量在 `.env.ragflow` 中只能定义一次。dotenv 对重复键通常采用最后一个值，
重复的 Redis 密码或地址会造成“配置看似正确、运行时却认证失败”的隐蔽问题。

## 5. 各存储的连接职责

| 存储 | 应用连接方式 | 主要使用者 | 数据职责 |
|---|---|---|---|
| PostgreSQL `rag_app` | `APP_DATABASE_DSN` | Gateway、Worker、Scheduler | 控制面、任务、侧车索引、引用、反馈 |
| PostgreSQL `rag_flow` | `RAGFLOW_POSTGRES_*` | RAGFlow Server、Task Executor | RAGFlow 元数据 |
| Redis Stack DB 0 | 启动脚本派生 `CACHE_REDIS_URL` | Gateway | 精确缓存、语义缓存与 RediSearch 索引 |
| Redis Stack DB 1 | RAGFlow 配置模板固定 | RAGFlow | RAGFlow 队列、状态与临时数据 |
| Redis Stack DB 2 | 启动脚本派生 `APP_REDIS_URL` | Celery Worker/Scheduler | 任务 Broker/结果与调度运行数据 |
| Elasticsearch | `RAGFLOW_ES_*` | RAGFlow | Chunk 正文索引、BM25 与向量检索 |
| MinIO | `RAGFLOW_MINIO_*` | RAGFlow | 上传原文件和对象数据 |

三个 Redis 逻辑 DB 共用一个密码，但必须保持不同 DB 编号和 Key Namespace。不能把
三个 URL 都改到 DB 0，否则缓存清理、Celery 队列和 RAGFlow Key 可能相互影响。

## 6. 启动流程

完成 `.env.ragflow` 后运行：

```bash
./deploy/local/start-full.sh
```

外部模式的预期提示包括：

```text
runtime and external storage static preflight passed
external storage mode selected; local storage containers were not started
```

启动脚本随后会：

1. 生成 Git 忽略的 `.env.full`；
2. 检查外部存储配置；
3. 运行 `rag-app-schema-check`，以只读事务核对两个数据库的表集合、自研应用
   `V1.0.0` 发布号、Schema 摘要、Alembic 历史头，以及单一业务账号对两个目标
   Schema 的对象所有权和 `CREATE` 权限；
4. Schema 校验成功后才启动 RAGFlow Server 与 Task Executor；
5. 通过官方 API 完成 RAGFlow 账号/API Key 与模型 Bootstrap；
6. 启动 Gateway、Worker 和 Scheduler。

自研启动流程不会对 `rag_app` 执行 `alembic upgrade head`、`CREATE TABLE`、
`ALTER TABLE` 或其他 DDL；RAGFlow 官方进程会在 `rag_flow` 中执行自身幂等兼容性
DDL。如果 DBA 没有执行安装 SQL、现有 0006 数据库没有完成一次性 V1.0.0 登记、
结构版本或摘要不匹配，或任一目标 Schema 缺少 `lazytt_rag` 的 CREATE/对象所有权，
门禁都会明确失败。

全新数据库执行 `migrations/releases/V1.0.0__fresh_install.sql` 后已经是 V1.0.0，
不需要再次登记。仅对
升级本功能前已经处于 `0006_document_version_updates` 的现有 `rag_app`，由 DBA 审核并
执行 `migrations/releases/V1.0.0__stamp_existing_schema.sql`。该脚本只在 13 张表和
0006 历史头
完全符合预期时补充正式发布信息，不改业务数据。

## 7. 连通性验证

### 7.1 PostgreSQL

```bash
PGPASSWORD='<REDACTED>' psql \
  -h <POSTGRES_HOST> -p 5432 -U lazytt_rag -d rag_app \
  -c 'SELECT version_num, release_version, schema_checksum, applied_at FROM rag_app.alembic_version;'

PGPASSWORD='<REDACTED>' psql \
  -h <POSTGRES_HOST> -p 5432 -U lazytt_rag -d rag_flow \
  -c "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';"
```

避免把真实密码直接写进共享终端历史；生产验收优先使用 `.pgpass`、Secret Manager 或
运维平台的临时凭据注入。

### 7.2 Redis Stack

```bash
REDISCLI_AUTH='<REDACTED>' redis-cli \
  -h <REDIS_HOST> -p 6379 PING

REDISCLI_AUTH='<REDACTED>' redis-cli \
  -h <REDIS_HOST> -p 6379 MODULE LIST
```

预期 `PING` 返回 `PONG`，模块列表至少包含 `search`。普通 Redis 没有 RediSearch，
不能满足当前语义缓存索引要求。

### 7.3 Elasticsearch

```bash
curl -fsS -u 'elastic:<REDACTED>' \
  'http://<ELASTICSEARCH_HOST>:9200/_cluster/health'
```

单节点初始化时可接受 `yellow`，但不能为 `red`；生产集群应按运维目标达到 `green`。

### 7.4 MinIO

```bash
curl -fsS 'http://<MINIO_HOST>:9000/minio/health/live'
```

### 7.5 应用

```bash
curl -fsS http://127.0.0.1:8080/health/live
curl -fsS http://127.0.0.1:8080/health/ready
```

`live` 只说明进程存活；`ready` 才代表关键依赖已通过就绪检查。

### 7.6 查看只读 Schema 门禁结果

```bash
docker compose --env-file deploy/local/.env.full \
  -f compose.local.yml ps -a rag-app-schema-check

docker compose --env-file deploy/local/.env.full \
  -f compose.local.yml logs --no-log-prefix rag-app-schema-check
```

成功日志会包含 `"status": "passed"`、`"ddl_executed": false`、
`"schema_release": "V1.0.0"`，但不会输出 DSN、密码或管理员凭据。失败时先由 DBA
核对发布记录和目标表结构；不要给应用容器临时增加 DDL 权限绕过门禁。

## 8. 切回本地存储

1. 先执行 `./deploy/local/stop-full.sh`，只停止运行时，不删除卷；
2. 将 `.env.ragflow` 的 `RAG_STORAGE_MODE` 改为 `local`；
3. 恢复模板中的本地镜像与本地服务名配置；
4. 重新执行 `./deploy/local/start-full.sh`；
5. 验证 Docker Desktop 中出现独立的 `lazytt-rag-storage` 与
   `lazytt-rag-runtime` 两个 Compose 项目。

切换连接地址不会自动复制数据。本交付包的目标是初始化全新空库，因此本地与外部
环境的数据彼此独立。

## 9. 常见错误

| 现象 | 常见原因 | 处理 |
|---|---|---|
| PostgreSQL 密码失败 | DSN 密码未 URL 编码或账号密码不一致 | 重新编码 DSN；由 DBA核对角色密码 |
| RAGFlow Server unhealthy | 任一 PostgreSQL/ES/MinIO/Redis 地址或凭据错误 | 查看容器日志，逐项执行本章连通性检查 |
| Redis `NOAUTH` | `.env.ragflow` 有重复密码键，或外部实例密码不同 | 保留一套变量并重新生成 `.env.full` |
| `unknown command FT.*` | 使用了普通 Redis 而非 Redis Stack | 部署带 RediSearch 的 Redis Stack |
| ES 返回 401 | 使用了占位密码或错误账号 | 使用实际 Secret；不要照抄 `<REDACTED>` |
| MinIO 可连但文档失败 | Access Key/Secret 不匹配或 Bucket 权限不足 | 检查 RAGFlow 日志与 MinIO 权限 |
