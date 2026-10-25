# LazyTT RAG 另一台电脑外部部署与已有存储复用手册

> 历史参考：本文保留改造前的流程与审计说明。2026-09-07 起，部署与 SQL 执行以[当前 ECS 操作入口](../deploy/ecs/README.md)为准；本文旧 `migrations/releases`、Alembic 或一体化安装命令不再执行。旧文件可在 Git 检查点 `335d418` 查阅。

更新时间：2026-08-25

本文面向需要在另一台电脑部署 LazyTT RAG 后端的维护人员。目标是从 GitHub 拉取与
现有环境一致的 `lazytt-rag-system` 版本，创建本机私有配置，连接外部存储，启动
RAGFlow、Gateway、Worker 和 Scheduler，并完成可重复的验收。

本文只描述后端，不包含用户前端或内部开发前端。

> [!IMPORTANT]
> 当前后端使用 Direct Tenant/User 联调身份：业务请求携带 `X-Tenant-ID` 和
> `X-User-ID`，不使用注册、密码、Session、JWT 或 `issuer`。这两个 Header 不是认证
> 凭据，Gateway 和 `/v1/management/*` 只能运行在受控网络，不能直接暴露到公网。

> [!CAUTION]
> 文中的 `<REDACTED>`、`<GITHUB_REPOSITORY_URL>`、`<COMMIT_OR_TAG>`、主机名、账号和
> 端口都是占位符。不得把真实密码、DSN、Token 或 API Key 写入 Git、文档、聊天、
> 工单、截图或共享终端历史。

## 1. 当前版本和系统边界

本手册以以下版本为准：

| 项目 | 当前值 |
|---|---|
| App PostgreSQL Schema | `V1.1.0` |
| Alembic 历史头 | `0006_document_version_updates` |
| V1.1.0 Schema 摘要 | `6f61654d651c73816a9dd85db3a55bfbd8f5ab5f1296908de60d93aa873f3109` |
| RAGFlow | `v0.26.4` |
| 知识库类型 | `TENANT`、`SYSTEM` |
| 身份字段 | `X-Tenant-ID`、`X-User-ID` |

当前不存在个人知识库：

- 同一 Tenant 下的全部 User 可以访问该 Tenant 的全部 `TENANT` 知识库；
- 所有 Tenant 都可以访问全部 `SYSTEM` 知识库；
- 当前没有知识库成员 ACL；
- 创建、上传、重解析和删除使用 `/v1/management/*`；
- 普通用户读取使用非 management 接口。

### 1.1 Application 组件

| 组件 | 职责 |
|---|---|
| RAGFlow Server | RAGFlow API、Dataset 和文档控制面 |
| RAGFlow Task Executor | RAGFlow 解析、切块和索引任务 |
| RAG Gateway | HTTP API、共享范围、检索、问答和缓存 |
| RAG Worker | 上传、解析、重试、删除等异步业务任务 |
| RAG Scheduler | 定时任务和调度 |
| `rag-app-schema-check` | 启动前只读检查数据库版本和结构 |
| `rag-app-bootstrap` | 验证或幂等准备 Profile、缓存索引和运行依赖 |

### 1.2 外部存储组件

RAG 外部存储不是三项，而是四类物理服务；PostgreSQL 中又包含两个逻辑数据库：

| 存储 | 数据职责 | 主要使用者 |
|---|---|---|
| PostgreSQL `rag_app` | 知识库、文档、任务、引用、反馈和 Profile | Gateway、Worker、Scheduler |
| PostgreSQL `rag_flow` | RAGFlow 元数据 | RAGFlow Server、Task Executor |
| Redis Stack DB 0 | 精确缓存、语义缓存和 RediSearch 索引 | Gateway |
| Redis Stack DB 1 | RAGFlow 队列、状态和临时数据 | RAGFlow |
| Redis Stack DB 2 | Celery Broker、结果和调度数据 | Worker、Scheduler |
| Elasticsearch | Chunk 正文、BM25 和向量索引 | RAGFlow |
| MinIO | 上传原文件和解析对象 | RAGFlow |

PostgreSQL 是应用控制面的权威存储。MinIO 保存文件本体，Elasticsearch 保存检索索引，
Redis Stack 保存缓存和运行状态。只复制其中一种存储，不能得到完整可用的知识库。

## 2. 先选择部署路径

必须先选定路径 A 或路径 B，不要混用两条路径中的数据库初始化命令。

### 2.1 路径 B：复用已有远程存储（推荐）

适用于把 Application 从旧电脑迁移到新电脑，同时保留服务器上已有知识库和文件。

```text
新电脑拉取相同版本代码
→ 创建新电脑自己的 deploy/local/.env.ragflow
→ 填入同一套远程存储连接和凭据
→ 停止旧电脑的 RAG Application
→ 新电脑运行 ./deploy/local/start-full.sh
→ 只读检查 rag_app V1.1.0
→ 复用已有知识库、文档、Chunk、索引和对象文件
→ 验收通过后由新电脑正式接管
```

路径 B 不需要再次：

- 创建 PostgreSQL 角色；
- 创建 `rag_app` 或 `rag_flow`；
- 执行 `V1.0.0__fresh_install.sql`；
- 执行 V1.0.0 盖章或 V1.1.0 增量 SQL；
- 清空或转换已有业务数据；
- 创建已有 MinIO Bucket；
- 删除或重建已有 Elasticsearch 索引；
- 清空 Redis；
- 重新上传、解析已有文件；
- 重新创建已有共享知识库；
- 手工重建已有 Profile。

### 2.2 路径 A：搭建全新远程存储

适用于没有可复用存储，需要从空环境部署一套新的 RAG 后端。

```text
准备空 PostgreSQL、Redis Stack、Elasticsearch、MinIO
→ DBA 创建业务角色及 rag_app、rag_flow
→ DBA 安装 V1.0.0 基线
→ DBA 增量升级到 V1.1.0
→ 配置四类存储的账号、权限和持久化
→ 新电脑拉取代码并修改 deploy/local/.env.ragflow
→ 运行 ./deploy/local/start-full.sh
→ 初始化 RAGFlow 账号、API Key、模型和 Profile
→ 创建共享知识库并完成端到端验收
```

## 3. 部署角色和责任

### 3.1 DBA

- 创建和审计 PostgreSQL 角色、数据库与 Schema；
- 审核并手工运行发布 SQL；
- 验证对象 Owner、最小权限、发布号和 Schema 摘要；
- 负责 PostgreSQL 备份、恢复和升级回退。

### 3.2 存储管理员

- 部署和维护 Redis Stack、Elasticsearch、MinIO；
- 配置持久化、备份、TLS、防火墙和访问控制；
- 提供经批准的服务地址和运行凭据。

### 3.3 Application 维护人员

- 获取正确代码版本；
- 创建和保护 `deploy/local/.env.ragflow`；
- 配置存储和模型连接；
- 启停 Application、查看日志并完成验收；
- 不使用运行账号代替 DBA 执行未审核的结构修改。

## 4. 新电脑前置条件

下面命令均在新电脑执行。

### 4.1 Git

```bash
git --version
```

### 4.2 Docker

macOS 安装并启动 Docker Desktop；Linux 安装组织批准的 Docker Engine 和 Compose
Plugin。等待 Docker Engine 就绪后验证：

```bash
docker version
docker compose version
```

建议给 Docker 至少分配 12 GB 内存。实际资源还取决于并发、文件大小和模型部署方式。

### 4.3 uv

启动脚本会使用 `uv` 安装锁定依赖并运行准备工具：

```bash
uv --version
```

### 4.4 网络和主机名

新电脑必须能够访问：

| 服务 | 常见端口 |
|---|---:|
| PostgreSQL | 5432 |
| Redis Stack | 6379 |
| Elasticsearch | 9200 |
| MinIO API | 9000 |
| LLM/Embedding/Vision/Rerank | 以 Provider 为准 |

如果需要让同一受信局域网中的其他主机访问 Gateway，应准备固定内网 IP、内网 DNS 或
macOS `.local` 主机名。`0.0.0.0` 只能用于监听，不能作为客户端访问地址。

macOS 可查看本机 mDNS 名称：

```bash
scutil --get LocalHostName
```

例如输出 `NewMac` 时，同一局域网内通常使用：

```text
newmac.local
```

## 5. 确认旧环境的代码和数据状态

### 5.1 记录旧电脑代码版本

在旧电脑项目根目录执行：

```bash
git rev-parse HEAD
git status --short
```

记录 Commit SHA 或正式 Tag。未提交的本地修改不会通过 GitHub 自动出现在新电脑，必须
先经过正常代码审查并提交，不能依赖手工复制零散源码。

### 5.2 路径 B 记录切换基线

在停止旧 Application 之前记录：

- `rag_app` 发布版本和摘要；
- 知识库数量；
- 文档数量；
- 仍在运行的 Job；
- RAGFlow Dataset 和文档数量；
- MinIO Bucket；
- Elasticsearch 集群和索引状态；
- 当前使用的 Embedding 模型及维度。

只读查询 `rag_app`：

```sql
SELECT version_num, release_version, schema_checksum, applied_at
FROM rag_app.alembic_version;

SELECT
    (SELECT count(*) FROM rag_app.knowledge_base) AS knowledge_bases,
    (SELECT count(*) FROM rag_app.document_record) AS documents,
    (SELECT count(*) FROM rag_app.ingestion_job) AS jobs;
```

路径 B 必须得到：

```text
version_num     = 0006_document_version_updates
release_version = V1.1.0
schema_checksum = 6f61654d651c73816a9dd85db3a55bfbd8f5ab5f1296908de60d93aa873f3109
```

如果已有数据库不是 V1.1.0，停止 Application 主机迁移。先由 DBA 按
`migrations/releases/README.md` 和发布说明完成备份、升级及验收；新电脑启动不会替
DBA 自动升级 external 数据库。

## 6. 从 GitHub 拉取代码

下面路径只是示例，可以替换为组织批准的目录。

### 6.1 克隆仓库

```bash
mkdir -p "$HOME/LazyTT"
cd "$HOME/LazyTT"

git clone <GITHUB_REPOSITORY_URL> lazytt-rag-system
cd lazytt-rag-system
```

### 6.2 切换到指定版本

```bash
git checkout <COMMIT_OR_TAG>
git rev-parse HEAD
git status --short
```

`git rev-parse HEAD` 必须与第 5.1 节记录的版本一致。不要默认使用后来出现的其他分支、
提交或镜像。

## 7. 创建私有配置文件

### 7.1 从模板创建

在新电脑项目根目录执行：

```bash
cp deploy/local/.env.ragflow.example deploy/local/.env.ragflow
chmod 600 deploy/local/.env.ragflow
```

如果目标文件已经存在，不要直接覆盖；先确认它是否属于本次部署。

### 7.2 编辑文件

```bash
nano deploy/local/.env.ragflow
```

也可以使用其他本地编辑器。

只维护：

```text
deploy/local/.env.ragflow
```

不要手工修改：

```text
deploy/local/.env.full
```

`deploy/local/.env.full` 是 `start-full.sh` 根据 `.env.ragflow` 生成的运行环境，每次启动都
可能重新生成。

### 7.3 Secret 管理

- 真实环境文件不得进入 Git；
- `git status --short` 不应显示包含真实 Secret 的新文件；
- 优先通过组织密码管理器或 Secret Manager 交接凭据；
- DSN 中的密码如果包含 `@`、`:`、`/`、`#`、`%` 等字符，必须做 URL 编码；
- URL 编码后的密码仍然是 Secret；
- 不要使用 `cat deploy/local/.env.ragflow` 把整份配置打印到共享终端。

## 8. 修改 `deploy/local/.env.ragflow`

### 8.1 切换到 external 模式

```dotenv
RAG_STORAGE_MODE=external
```

external 模式不会启动本机 `docker-compose.storage.yml`，也不会自动对 `rag_app` 执行
DDL。

### 8.2 App PostgreSQL

```dotenv
APP_DATABASE_DSN=postgresql+psycopg://lazytt_rag:<URL_ENCODED_PASSWORD>@<POSTGRES_HOST>:5432/rag_app
```

### 8.3 RAGFlow PostgreSQL

```dotenv
RAGFLOW_POSTGRES_HOST=<POSTGRES_HOST>
RAGFLOW_POSTGRES_PORT=5432
RAGFLOW_POSTGRES_DATABASE=rag_flow
RAGFLOW_POSTGRES_USER=lazytt_rag
RAGFLOW_POSTGRES_PASSWORD=<REDACTED>
```

`APP_DATABASE_DSN` 必须指向 `rag_app`，`RAGFLOW_POSTGRES_DATABASE` 必须是
`rag_flow`。不能把两个服务配置到同一个逻辑数据库。

### 8.4 Redis Stack

```dotenv
REDIS_STACK_HOST=<REDIS_HOST>
REDIS_STACK_PORT=6379
REDIS_STACK_PASSWORD=<REDACTED>
REDIS_STACK_MAXMEMORY=1gb
```

启动脚本会从这些字段派生不同逻辑 DB 的 URL。不要手工把 Gateway缓存、RAGFlow和
Celery全部指到DB 0。

### 8.5 Elasticsearch

```dotenv
RAGFLOW_ES_SCHEME=http
RAGFLOW_ES_HOST=<ELASTICSEARCH_HOST>
RAGFLOW_ES_PORT=9200
ELASTIC_PASSWORD=<REDACTED>
```

如果远端启用了 HTTPS，应按实际证书、代理和组织安全策略调整 Scheme 及信任配置，
不要通过长期关闭证书校验绕过问题。

### 8.6 MinIO

```dotenv
RAGFLOW_MINIO_HOST=<MINIO_HOST>
RAGFLOW_MINIO_PORT=9000
MINIO_USER=<MINIO_ACCESS_KEY>
MINIO_PASSWORD=<REDACTED>
```

路径 B 必须使用已有 RAGFlow 数据对应的 MinIO 服务和凭据。只连接 PostgreSQL 而指向
另一个空 MinIO，会导致已有文档元数据存在但文件本体不可用。

### 8.7 Gateway 监听地址

仅供本机调用时：

```dotenv
RAG_APP_BIND_ADDRESS=127.0.0.1
RAG_APP_PORT=8080
```

确实需要受信局域网访问时：

```dotenv
RAG_APP_BIND_ADDRESS=0.0.0.0
RAG_APP_PORT=8080
```

绑定 `0.0.0.0` 后还必须通过主机防火墙、VLAN 或反向代理限制访问来源。RAGFlow管理
界面仍应保持只绑定本机。

### 8.8 RAGFlow 运维身份

路径 A 使用新 `rag_flow` 时：

```dotenv
RAGFLOW_ADMIN_EMAIL=<RAGFLOW_ADMIN_EMAIL>
RAGFLOW_ADMIN_PASSWORD=<REDACTED>
RAGFLOW_API_KEY=
```

首次启动会通过 RAGFlow 官方 API 注册或登录，并把验证后的 API Key 写回私有文件。

路径 B 复用已有 `rag_flow` 时，应通过安全渠道复用已经验收的管理员身份和有效 API
Key。也可以把 `RAGFLOW_API_KEY` 留空，但前提是新电脑持有同一账号的有效登录凭据，
启动脚本能够通过官方 API 重新获取或创建 Token。禁止直接修改 `rag_flow` 数据表伪造
账号或 Token。

### 8.9 模型 Provider

至少配置：

```dotenv
LLM_BASE_URL=<OPENAI_COMPATIBLE_BASE_URL>
LLM_API_KEY=<REDACTED>
LLM_MODEL=<CHAT_MODEL>
REWRITE_MODEL=

EMBEDDING_BASE_URL=<OPENAI_COMPATIBLE_BASE_URL>
EMBEDDING_API_KEY=<REDACTED>
EMBEDDING_MODEL=<EMBEDDING_MODEL>
EMBEDDING_DIMENSION=<EXPECTED_DIMENSION_OR_PENDING_AUTO_DETECTION>

VISION_BASE_URL=<OPENAI_COMPATIBLE_BASE_URL>
VISION_API_KEY=<REDACTED>
VISION_MODEL=<VISION_MODEL>
```

可选 Rerank：

```dotenv
RERANK_ENABLED=false
RERANK_BASE_URL=
RERANK_API_KEY=
RERANK_MODEL=
```

路径 B 必须保持与已有向量索引兼容的 Embedding 模型和维度。直接更换模型或维度不能
只靠修改环境变量完成，通常需要新的 Profile、索引代际和重建流程。

## 9. 外部存储连通性检查

所有检查都应在新电脑执行。端口可达只证明 TCP 连通，不能代替协议、认证和权限检查。

### 9.1 TCP

```bash
nc -vz <POSTGRES_HOST> 5432
nc -vz <REDIS_HOST> 6379
nc -vz <ELASTICSEARCH_HOST> 9200
nc -vz <MINIO_HOST> 9000
```

如果宿主机可访问但容器不可访问，检查 Docker 到公司网络的路由、DNS、防火墙允许的
源地址，以及配置中是否误用了 `127.0.0.1`。容器中的 `127.0.0.1` 只代表容器自己。

### 9.2 PostgreSQL

使用密码提示、`.pgpass` 或受控凭据注入，不要把真实密码写在命令中：

```bash
psql -X -W -h <POSTGRES_HOST> -p 5432 -U lazytt_rag -d rag_app \
  -c 'SELECT version_num, release_version, schema_checksum, applied_at FROM rag_app.alembic_version;'

psql -X -W -h <POSTGRES_HOST> -p 5432 -U lazytt_rag -d rag_flow \
  -c "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';"
```

### 9.3 Redis Stack

通过受控方式提供密码后验证：

```bash
REDISCLI_AUTH='<REDACTED>' redis-cli -h <REDIS_HOST> -p 6379 PING
REDISCLI_AUTH='<REDACTED>' redis-cli -h <REDIS_HOST> -p 6379 MODULE LIST
```

预期 `PING` 返回 `PONG`，模块列表至少包含 `search`。普通 Redis 不满足语义缓存需求。

### 9.4 Elasticsearch

```bash
curl --fail-with-body \
  -u '<ELASTIC_USER>:<REDACTED>' \
  'http://<ELASTICSEARCH_HOST>:9200/_cluster/health?pretty'
```

不要把带真实密码的命令保存在共享Shell历史。生产环境优先通过Secret Manager、临时
环境注入或运维平台执行。

### 9.5 MinIO

```bash
curl -fsS http://<MINIO_HOST>:9000/minio/health/live
```

如已安装 MinIO Client，再使用受控凭据检查账号、Policy 和 Bucket，不在文档或终端
历史中保存真实 Access Key/Secret Key。

## 10. 路径 B：复用已有远程存储

### 10.1 切换前检查

- 已完成 PostgreSQL、MinIO 和 Elasticsearch 备份；
- `rag_app` 为 V1.1.0 且摘要正确；
- `rag_flow` 与 RAGFlow v0.26.4 兼容；
- 四类存储健康；
- 新电脑已通过第 9 节连通性检查；
- 新旧电脑代码 Commit 一致；
- 新电脑已配置相同的存储和模型凭据；
- 没有正在上传、解析、删除或重建索引的关键任务。

### 10.2 停止新写入并等待任务收敛

在切换窗口开始后：

1. 停止新的上传、重解析和删除请求；
2. 等待正在运行的 Job 成功或进入明确失败状态；
3. 记录仍未完成的 Job ID；
4. 再停止旧 Application。

### 10.3 停止旧电脑 Application

在旧电脑项目根目录执行：

```bash
./deploy/local/stop-full.sh
```

该命令只停止 RAGFlow 和 Application 运行时，不会停止远程 PostgreSQL、Redis Stack、
Elasticsearch、MinIO，也不会删除其中的数据。

确认旧端以下组件已经停止：

- `rag-gateway`；
- `rag-worker`；
- `rag-scheduler`；
- `ragflow-server`；
- `ragflow-task-executor`。

### 10.4 为什么默认不同时运行两套 Application

两台电脑同时连接同一套存储会形成多实例部署：

- 两个 Gateway 同时提供写接口；
- 两个 Worker 可能并行消费任务；
- 两个 Scheduler 可能重复调度；
- 两个 RAGFlow Task Executor 可能并行处理；
- 两个 `start-full.sh` 可能同时验证或准备账号、模型和缓存索引。

这些组件理论上可以向多实例演进，但在完成多实例并发、锁、队列、幂等和调度验收前，
迁移主机时采用“先停旧端，再启新端”的单活方式。

### 10.5 新电脑启动

在新电脑项目根目录执行：

```bash
./deploy/local/start-full.sh
```

external 模式应出现：

```text
external storage mode selected; local storage containers were not started
```

如果 Schema Check 报告版本、摘要、对象Owner或表清单不一致，停止启动并交给DBA处理。
不要运行Fresh Install、手工补列或伪造 `alembic_version`。

## 11. 路径 A：全新远程存储

路径 A 包含数据库和存储的高影响初始化，只能由对应管理员在确认目标为空后执行。

### 11.1 准备四类空存储

存储管理员准备并持久化：

- PostgreSQL；
- Redis Stack，必须包含 RediSearch；
- Elasticsearch；
- MinIO。

为每项配置固定版本、数据目录、备份、访问控制、防火墙和监控。不要用临时容器文件系统
承载正式数据。

### 11.2 审核数据库发布文件

DBA 在执行前确认：

```text
migrations/releases/V1.0.0__fresh_install.sql
migrations/releases/V1.0.0__to__V1.1.0__shared_knowledge_bases.sql
```

同时记录 Git Commit 和文件 SHA-256：

```bash
shasum -a 256 migrations/releases/V1.0.0__fresh_install.sql
shasum -a 256 migrations/releases/V1.0.0__to__V1.1.0__shared_knowledge_bases.sql
```

### 11.3 安装 V1.0.0 基线

`V1.0.0__fresh_install.sql` 只允许用于不存在目标角色、`rag_app` 和 `rag_flow` 的全新
环境。它会安全提示输入业务账号密码，不应把密码写入SQL文件。

```bash
psql -X -W \
  -v ON_ERROR_STOP=1 \
  -h <POSTGRES_HOST> \
  -p 5432 \
  -U <DBA_USER> \
  -d postgres \
  -f migrations/releases/V1.0.0__fresh_install.sql
```

任一步失败后保留现场并审计，不要直接重复运行。该脚本包含 `CREATE DATABASE`，无法用
一个普通事务覆盖跨数据库的全部步骤。

### 11.4 增量升级到 V1.1.0

V1.0.0 安装成功后立即执行：

```bash
psql -X -W \
  -v ON_ERROR_STOP=1 \
  -h <POSTGRES_HOST> \
  -p 5432 \
  -U lazytt_rag \
  -d rag_app \
  -f migrations/releases/V1.0.0__to__V1.1.0__shared_knowledge_bases.sql
```

这个增量会：

- 增加 `knowledge_base.scope_type`；
- 增加 `idx_knowledge_base_type_scope_status`；
- 增加 `citation_record.resource_scope_id`；
- 调整引用外键和索引；
- 将发布号和摘要更新为 V1.1.0。

它要求知识资源、Job、问答、反馈和幂等表为空，脚本本身不会删除任何数据。Fresh
Install后的空库满足这一条件。

### 11.5 验证数据库版本

```sql
SELECT version_num, release_version, schema_checksum, applied_at
FROM rag_app.alembic_version;

SELECT table_name, column_name, is_nullable
FROM information_schema.columns
WHERE table_schema = 'rag_app'
  AND (
      (table_name = 'knowledge_base' AND column_name = 'scope_type')
      OR
      (table_name = 'citation_record' AND column_name = 'resource_scope_id')
  )
ORDER BY table_name;

SELECT
    to_regclass('rag_app.idx_knowledge_base_type_scope_status') AS kb_index,
    to_regclass('rag_app.idx_citation_resource_document') AS citation_index,
    to_regclass('rag_app.idx_citation_scope_document') AS removed_old_index;
```

预期：

- `release_version = V1.1.0`；
- 摘要等于第 1 节固定值；
- 两个新增字段都是 `is_nullable = NO`；
- 两个新索引存在；
- `removed_old_index` 为 `NULL`。

### 11.6 Redis Stack、Elasticsearch 和 MinIO

存储管理员完成：

- Redis认证、RediSearch、持久化和DB 0/1/2边界；
- Elasticsearch认证、持久化和集群健康；
- MinIO运行账号、最小Policy、Bucket和持久化；
- 四类存储的备份和监控。

不要手工向 `rag_flow` 插入账号、Token、模型或业务数据。RAGFlow首次启动通过官方流程
完成必要的业务初始化和兼容性操作。

### 11.7 新电脑启动

完成第 6 至第 9 节配置和连通性检查后执行：

```bash
./deploy/local/start-full.sh
```

## 12. `start-full.sh` 实际启动顺序

`./deploy/local/start-full.sh` 会：

1. 读取 `deploy/local/.env.ragflow`；
2. 使用 `uv` 安装锁定依赖；
3. 生成Git忽略的 `deploy/local/.env.full`；
4. 执行存储、镜像、模型和Compose静态门禁；
5. external模式跳过本地Storage Compose；
6. 构建与当前代码一致的 `rag-app-schema-check` 镜像；
7. 只读验证 `rag_app` 和 `rag_flow`；
8. 启动RAGFlow Server和Task Executor；
9. 通过官方API验证或准备RAGFlow账号和API Key；
10. 探测并注册模型Provider；
11. 幂等验证或准备Profile和语义缓存索引；
12. 构建并启动Gateway、Worker和Scheduler；
13. 输出本机、mDNS或局域网Gateway地址。

external模式不会对 `rag_app` 执行 `alembic upgrade head`、`CREATE TABLE`、
`ALTER TABLE` 或发布迁移。RAGFlow官方进程可能按固定版本在 `rag_flow` 中执行自身幂等
兼容操作，因此路径B必须使用与原环境相同的RAGFlow版本。

## 13. 启动结果检查

### 13.1 容器状态

```bash
docker compose \
  --env-file deploy/local/.env.full \
  -f compose.local.yml \
  ps
```

查看一次性门禁：

```bash
docker compose \
  --env-file deploy/local/.env.full \
  -f compose.local.yml \
  ps -a rag-app-schema-check rag-app-bootstrap
```

两个一次性容器都应成功结束。失败时查看：

```bash
docker compose \
  --env-file deploy/local/.env.full \
  -f compose.local.yml \
  logs --no-log-prefix rag-app-schema-check rag-app-bootstrap
```

不要复制包含完整DSN、密码或API Key的日志到聊天或工单。

### 13.2 Gateway健康检查

```bash
curl -fsS http://127.0.0.1:8080/health/live
curl -fsS http://127.0.0.1:8080/health/ready
curl -fsS http://127.0.0.1:8080/v1/version
curl -fsS http://127.0.0.1:8080/v1/capabilities
```

`live`成功只说明进程存活；最终验收必须以 `ready` 为准。`ready` 应确认PostgreSQL、
Redis、RAGFlow、模型和缓存索引已经就绪。

### 13.3 确认external模式没有误启本地存储

```bash
docker compose \
  --env-file deploy/local/.env.full \
  -f deploy/local/docker-compose.storage.yml \
  ps
```

本次启动不应新增本地PostgreSQL、Redis Stack、Elasticsearch或MinIO容器。已有旧容器
可能仍然显示；实际连接目标应结合私有配置中的非敏感主机字段和运行时健康检查判断。

只查看非敏感目标字段：

```bash
awk -F= '/^RAG_STORAGE_MODE=|^RAGFLOW_POSTGRES_HOST=|^RAGFLOW_ES_HOST=|^RAGFLOW_MINIO_HOST=|^REDIS_STACK_HOST=/ {print}' deploy/local/.env.full
```

不要打印 `APP_DATABASE_DSN`、Redis URL或任何带密码的字段。

## 14. 路径 B 已有数据验收

路径 B 先证明旧数据被新电脑正确读取，再创建新的测试数据。

### 14.1 数据库数量对比

重新运行第 5.2 节只读统计，与切换前记录对比。切换期间没有新请求时，知识库和文档
数量应一致。

### 14.2 Gateway共享知识库

使用已知的测试Tenant/User读取：

```bash
curl --fail-with-body \
  -H 'X-Tenant-ID: tenant-a' \
  -H 'X-User-ID: user-001' \
  'http://127.0.0.1:8080/v1/knowledge-bases?page=1&page_size=100'
```

应看到该Tenant可访问的 `TENANT` 知识库和全部 `SYSTEM` 知识库。不要使用未知Tenant
看不到业务数据就判断迁移失败。

### 14.3 已有文档和Chunk

从返回中选择一个已知非敏感知识库和文档，通过以下接口检查：

```text
GET /v1/knowledge-bases/{knowledge_base_id}/documents
GET /v1/documents/{document_id}
GET /v1/documents/{document_id}/chunks
```

已有文档应保持 `READY`，Chunk可以读取，MinIO文件和Elasticsearch索引没有丢失。

## 15. 最小端到端功能验收

所有验收只使用获批的非业务Fixture。写操作需要唯一 `Idempotency-Key`；更换文件、
Path或参数时必须换新Key。

### 15.1 能力检查

```text
GET /v1/capabilities
```

确认当前部署实际提供所需Parser、Chunk、Embedding和Retrieval Profile。

使用 Apifox 验收时，可以导入：

```text
docs/rag-service-openapi.json
```

然后创建一个环境变量：

```text
baseUrl=http://<NEW_RAG_HOST>:8080
```

普通业务和management请求统一设置：

```text
X-Tenant-ID: <TEST_TENANT_ID>
X-User-ID: <TEST_USER_ID>
```

当前不配置JWT、Authorization或 `issuer`。创建、上传、重解析、重试和删除等写操作还
必须分别设置唯一的 `Idempotency-Key`。上传请求使用 `multipart/form-data`，不要手工
填写 boundary，应由Apifox自动生成。

### 15.2 创建两类共享知识库

```text
POST /v1/management/knowledge-bases
```

分别创建：

- 一个 `SYSTEM`共享知识库；
- 一个 `TENANT`共享知识库。

创建请求必须携带：

```text
X-Tenant-ID
X-User-ID
Idempotency-Key
Content-Type: application/json
```

Profile以 `/v1/capabilities` 实际返回值为准。当前推荐使用供应商无关Embedding Profile
和结构感知Retrieval Profile。

### 15.3 上传解析矩阵

```text
POST /v1/management/knowledge-bases/{knowledge_base_id}/documents
```

每个请求只上传一个文件，使用 `multipart/form-data`：

| 文件族 | Parser | 推荐Chunk |
|---|---|---|
| PDF | `deepdoc-default@1` | `prose-384@1` |
| DOC/DOCX | `office-default@1` | `prose-384@1` |
| PPT/PPTX | `office-default@1` | `visual-192@1` |
| XLS/XLSX/CSV | `office-default@1` | `table-192@1` |
| PNG/JPG/JPEG/WEBP | `picture-default@1` | `visual-192@1` |

建议验收：

1. SYSTEM知识库上传一个PDF；
2. TENANT知识库上传一个PDF；
3. TENANT知识库上传图片；
4. TENANT知识库上传Word、PPT或Excel。

上传时可以不传 `parser_profile` 和 `chunk_profile`：Gateway 先保留兼容的知识库默认
Parser，不兼容时按配置规则自动切换 PDF、Office、图片或媒体 Parser，并按文件族选择
Chunk。需要指定 MinerU 或做对照实验时再显式传入。

### 15.4 轮询任务和文档

上传返回 `document_id` 和 `job_id` 后轮询：

```text
GET /v1/management/jobs/{job_id}
GET /v1/management/documents/{document_id}
GET /v1/management/documents/{document_id}/chunks
```

完成条件：

```text
Job status       = SUCCEEDED
Job progress     = 1
Document status  = READY
Document chunks  > 0
```

### 15.5 检索和问答

- 执行混合检索并确认返回候选；
- 执行普通问答；
- 执行SSE流式问答；
- 验证引用可以回溯到真实Chunk；
- 重复相同问题验证精确缓存；
- 使用近义问题记录语义缓存命中或阈值未命中。

可结合项目已有脚本：

```bash
./deploy/local/run-gateway-model-smoke.sh
./deploy/local/run-ragflow-smoke.sh --parse-timeout 600
```

执行脚本前检查它使用的是测试Tenant、测试知识库和非业务Fixture。删除测试资源前再次
确认资源ID，不得清理已有业务知识库。

### 15.6 共享范围

使用 `tenant-a/user-001`：

- 应看到tenant-a的TENANT知识库；
- 应看到全部SYSTEM知识库。

使用 `tenant-a/user-002`：

- 应看到与user-001相同的tenant-a TENANT知识库；
- 应看到全部SYSTEM知识库。

使用 `tenant-b/user-001`：

- 不应看到tenant-a TENANT知识库；
- 应看到SYSTEM知识库。

### 15.7 重启持久性

```bash
./deploy/local/stop-full.sh
./deploy/local/start-full.sh
```

重启后再次验证知识库、文档、Chunk、检索和问答。停止Full运行时不会删除远程存储。

## 16. 日常启停和日志

### 16.1 启动或重建

```bash
./deploy/local/start-full.sh
```

### 16.2 停止Application并保留存储

```bash
./deploy/local/stop-full.sh
```

### 16.3 查看状态

```bash
docker compose \
  --env-file deploy/local/.env.full \
  -f compose.local.yml \
  ps
```

### 16.4 查看日志

```bash
docker compose \
  --env-file deploy/local/.env.full \
  -f compose.local.yml \
  logs -f --since 10m \
  rag-gateway rag-worker rag-scheduler ragflow-server ragflow-task-executor
```

禁止执行：

```text
docker compose down -v
```

`-v`可能删除命名卷。虽然external模式的权威存储在远端，但本机仍可能保存上传暂存、
日志或其他运行数据，不能把破坏性命令作为普通启停方式。

## 17. 备份和恢复要求

### 17.1 PostgreSQL

至少分别备份 `rag_app` 和 `rag_flow`：

```bash
pg_dump -W -h <POSTGRES_HOST> -p 5432 -U <BACKUP_USER> \
  -d rag_app -Fc -f <SECURE_BACKUP_DIR>/rag_app.dump

pg_dump -W -h <POSTGRES_HOST> -p 5432 -U <BACKUP_USER> \
  -d rag_flow -Fc -f <SECURE_BACKUP_DIR>/rag_flow.dump
```

备份完成不等于可恢复，必须定期执行隔离恢复演练。

### 17.2 MinIO

- 对Bucket做版本化、镜像或组织批准的备份；
- 定期验证对象能够恢复；
- 备份时间点应尽量与PostgreSQL一致。

### 17.3 Elasticsearch

- 配置 Snapshot Repository；
- 记录与PostgreSQL/MinIO一致的快照时间点；
- 索引可以从权威数据重建，但重建时间和模型兼容性必须评估。

### 17.4 Redis Stack

- 配置符合业务要求的RDB/AOF；
- 明确缓存可重建数据和任务运行数据的区别；
- 不把Redis当作知识库唯一权威存储。

## 18. 主机迁移回退

路径 B 的最小回退链路：

```text
停止新电脑Application
→ 保持远程存储运行
→ 确认旧电脑代码和私有配置未被破坏
→ 启动旧电脑Application
→ 检查Schema门禁和Gateway Ready
→ 验证已有知识库、文档和检索
```

回退时仍然只能保持一套未经多实例验收的Application处于活动状态。

路径 A 如果启动失败：

1. 停止Application；
2. 保留存储现场和日志；
3. 不直接删除数据库、Bucket或索引；
4. 根据DBA和存储管理员审核结果决定修复或从备份恢复。

Schema升级失败时：

- 查找第一条数据库错误，而不是只处理后续 `25P02`；
- 执行 `ROLLBACK`；
- 不手工伪造 `alembic_version`；
- 不修改迁移SQL绕过版本或空数据门禁；
- 根据备份和发布说明恢复。

## 19. 常见故障

### 19.1 `deploy/local/.env.ragflow`缺失

从示例复制并填写私有配置，不要创建或手工维护 `.env.full`。

### 19.2 `connection refused`或timeout

检查地址、端口、服务监听范围、公司网络、防火墙、Docker路由和DNS。不要通过长期关闭
全部防火墙解决问题。

### 19.3 PostgreSQL认证失败

检查账号、数据库、密码URL编码、`pg_hba.conf`和来源地址。确认 `APP_DATABASE_DSN`
连接 `rag_app`，RAGFlow连接 `rag_flow`。

### 19.4 Schema Check失败

常见原因：

- 数据库未安装；
- `rag_app`不是V1.1.0；
- Schema摘要不匹配；
- 表或索引缺失；
- 对象Owner或CREATE权限不符合；
- 连接到了错误数据库。

external模式不会自动修复这些问题。交给DBA审核，不要给运行账号超级用户权限绕过门禁。

### 19.5 Redis `unknown command FT.CREATE`或`FT.INFO`

远端是普通Redis或没有加载RediSearch。切换到Redis Stack并重新验收模块。

### 19.6 Elasticsearch 401/403

检查Scheme、账号、密码、角色、代理和TLS。不要把错误的Elasticsearch地址误配成
Gateway或MinIO地址。

### 19.7 MinIO健康但上传失败

健康接口不验证业务账号权限。检查Access Key、Secret Key、Bucket、Policy、端口和
RAGFlow配置。

### 19.8 RAGFlow账号或API Key失败

路径 B 检查新电脑是否持有与已有 `rag_flow` 对应的账号和Key。不要直接向数据库写入
Token。必要时在受控本地管理入口通过官方流程重新获取。

### 19.9 模型返回401或维度不一致

分别检查LLM、Embedding、Vision和Rerank三元组。路径 B 不得在没有重建方案时切换到
不同Embedding模型或维度。

### 19.10 Parser不兼容

PDF、Office和图片混合上传时，显式指定文档级Parser。Parser与扩展名不兼容会返回
`RAG-KB-004`。

### 19.11 幂等冲突

同一个 `Idempotency-Key` 对应不同Path、文件或参数会返回
`RAG-COMMON-009`。完全相同请求重试可复用原Key；业务参数变化必须换新Key。

### 19.12 复用存储后看不到旧数据

依次检查：

1. Gateway是否连接正确的 `rag_app`；
2. RAGFlow是否连接正确的 `rag_flow`；
3. MinIO和Elasticsearch是否为原环境对应服务；
4. 请求是否使用正确 `X-Tenant-ID`；
5. 知识库是否为TENANT或SYSTEM；
6. 旧文档是否仍为READY。

### 19.13 两台电脑同时运行

如果出现重复调度、并行任务或切换状态不确定，先停止新旧两端中的一端，保留远程存储，
再从日志和任务状态恢复单活。

## 20. 安全要求

- Gateway和Management接口不得直接暴露公网；
- RAGFlow管理界面只允许本机或受控运维网络访问；
- PostgreSQL、Redis、Elasticsearch和MinIO只允许获批主机访问；
- 不使用PostgreSQL超级用户作为Application运行账号；
- 不把真实 `.env.ragflow`、`.env.full`、DSN或API Key提交到Git；
- 不把完整Secret打印到日志、截图、聊天或工单；
- 不执行未经确认的数据删除、数据库重建、索引删除或Bucket清理；
- 删除、迁移和升级前必须备份；
- 验收使用非业务Fixture；
- 不执行 `docker compose down -v`；
- 定期轮换已暴露或来源不明的凭据。

## 21. 最终完成判定清单

### 21.1 通用

- [ ] 新电脑代码Commit或Tag正确；
- [ ] Docker和uv可用；
- [ ] `deploy/local/.env.ragflow`已创建，权限为 `0600`；
- [ ] `RAG_STORAGE_MODE=external`；
- [ ] 私有配置和日志没有泄露Secret；
- [ ] 四类远程存储协议检查成功；
- [ ] `rag_app`为V1.1.0且摘要正确；
- [ ] RAGFlow版本与 `rag_flow`兼容；
- [ ] `rag-app-schema-check`成功；
- [ ] `rag-app-bootstrap`成功；
- [ ] Gateway `/health/ready`成功；
- [ ] Worker和Scheduler正常；
- [ ] 本次启动没有误启本地Storage Compose；
- [ ] 创建SYSTEM和TENANT知识库成功；
- [ ] 文件上传、解析、Chunk和检索成功；
- [ ] TENANT/SYSTEM共享范围正确；
- [ ] Application重启后数据仍存在。

### 21.2 路径 B 额外检查

- [ ] 切换前已记录代码、数据库和业务数量基线；
- [ ] 已停止或收敛旧端正在运行的Job；
- [ ] 旧Application已停止；
- [ ] 新旧电脑未处于未经验收的长期多实例状态；
- [ ] 已有知识库、文档、Chunk和检索结果可用；
- [ ] 已有MinIO对象和Elasticsearch索引可用；
- [ ] 新旧切换回退步骤已经记录。

### 21.3 路径 A 额外检查

- [ ] DBA确认目标为全新环境；
- [ ] V1.0.0 Fresh Install成功；
- [ ] V1.1.0增量成功；
- [ ] PostgreSQL角色、数据库、Owner和权限验收成功；
- [ ] Redis Stack包含RediSearch；
- [ ] Elasticsearch持久化和认证正常；
- [ ] MinIO账号、Policy、Bucket和持久化正常；
- [ ] RAGFlow账号、API Key和模型Provider初始化成功；
- [ ] 四类存储备份方案已经建立。

## 22. 相关文件和文档

- `deploy/local/.env.ragflow.example`：私有配置模板；
- `deploy/local/start-full.sh`：完整后端启动入口；
- `deploy/local/stop-full.sh`：停止运行时并保留存储；
- `compose.local.yml`：RAGFlow和Application运行时；
- `deploy/local/docker-compose.storage.yml`：仅local模式使用的本地存储；
- `migrations/releases/README.md`：数据库发布规则；
- `migrations/releases/V1.0.0__fresh_install.sql`：全新安装；
- `migrations/releases/V1.0.0__to__V1.1.0__shared_knowledge_bases.sql`：V1.1.0增量；
- `docs/USER_MANUAL.md`：当前后端使用手册；
- `docs/EXTERNAL_STORAGE_DEPLOYMENT_RUNBOOK.md`：外部存储迁移细节；
- `docs/STORAGE_MODE_SWITCH_RUNBOOK.md`：local/external切换演练；
- `docs/database-deployment-package/`：DBA交付与数据库参考；
- `docs/rag-service-openapi.json`：当前HTTP接口契约。
