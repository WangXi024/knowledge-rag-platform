# LazyTT RAG 本机外部存储迁移演练

## 1. 目标与边界

本演练在同一台电脑上启动一个独立 Compose Project，模拟未来局域网数据库服务器。
目标是验证 RAG 运行时只修改连接配置，就能从本地 storage Compose 切到一套全新的
空外部存储。

演练最终目标包含：

- PostgreSQL 18.4：一个实例、一个运行期业务账号、`rag_app` 与 `rag_flow` 两个库；
- Redis Stack：DB 0 缓存、DB 1 RAGFlow、DB 2 Celery；
- Elasticsearch 8.11.3：RAGFlow Chunk、向量和全文索引；
- MinIO：原文件与解析对象。

Compose 启动时四种存储都是空白服务；上述 PostgreSQL 角色、数据库和表必须由 DBA
手工安装。演练不迁移旧数据、不删除正式环境卷、不修改 RAGFlow 源码，也不要求在
`deploy/local/.env.ragflow` 中填写四个存储镜像。演练 Compose 自己固定软件镜像，这与
未来公司服务器由运维团队安装数据库是同一责任边界。

## 2. 内存与安全断点

本机 Docker 当前还可能运行正式 RAG 存储或其他 Elasticsearch。24 GB 主机不适合
同时运行两套检索引擎。开始前先保存私有配置并停止当前运行时/存储，保留所有卷：

演练 Compose 还把 PostgreSQL、Redis Stack、Elasticsearch、MinIO 的容器内存上限
分别限制为 768 MiB、768 MiB、1536 MiB、512 MiB，总上限约 3.5 GiB。Docker Desktop
中的“limit”表示允许使用的上限，不是容器启动后立即占用的内存。

```bash
cd /Users/wx/Lazyai_Project/Lazyai_RAG_Project/lazytt-rag-system
cp deploy/local/.env.ragflow deploy/local/.env.ragflow.before-external-lab
chmod 600 deploy/local/.env.ragflow.before-external-lab
./deploy/local/stop-full.sh
./deploy/local/stop-storage.sh
```

以上命令不会执行 `down -v`。Docker 中其他不属于本项目的 MySQL、Elasticsearch、
MinIO 或 Valkey 不会由本项目脚本停止；如需释放它们的内存，必须先确认所属项目再
由其负责人停止。

## 3. 创建独立空存储环境

复制演练私有配置：

```bash
cp deploy/local/external-storage-lab/.env.example deploy/local/external-storage-lab/.env
chmod 600 deploy/local/external-storage-lab/.env
```

编辑 `.env`，把每个 `change-me` 替换成不同的随机密码。真实值不得写入本文、日志、
聊天或 Git。

启动：

```bash
docker compose \
  --env-file deploy/local/external-storage-lab/.env \
  -f deploy/local/external-storage-lab/docker-compose.yml \
  up -d --wait
```

查看状态：

```bash
docker compose \
  --env-file deploy/local/external-storage-lab/.env \
  -f deploy/local/external-storage-lab/docker-compose.yml \
  ps
```

Docker Desktop 中应出现单独项目 `lazytt-rag-external-storage-lab`，四个服务均为
healthy。它使用独立卷，不会看到旧知识库或旧文档。此时 PostgreSQL 只有镜像启动
必需的 DBA 管理账号和 `postgres` 管理库；Compose 不会创建 LazyTT 业务对象。

## 4. 由 DBA 手工安装 PostgreSQL

先使用演练 `.env` 中的管理员账号验证目标仍为空。下面命令只读取角色和数据库名称，
不会创建对象：

```bash
docker compose \
  --env-file deploy/local/external-storage-lab/.env \
  -f deploy/local/external-storage-lab/docker-compose.yml \
  exec postgres bash -lc \
  'PGPASSWORD="$POSTGRES_PASSWORD" psql -U "$POSTGRES_USER" -d postgres -c "SELECT rolname FROM pg_roles WHERE rolname = '\''lazytt_rag'\'';" -c "SELECT datname FROM pg_database WHERE datname IN ('\''rag_app'\'', '\''rag_flow'\'');"'
```

两项查询都必须返回 0 行。随后由 DBA 审计安装 SQL，并在可交互终端中手工执行。

脚本执行到 `\password lazytt_rag` 时需要交互输入业务密码。DBA 应使用本机 `psql` 按
[`database-deployment-package/DBA_MIGRATION_SIMULATION_RUNBOOK.md`](./database-deployment-package/DBA_MIGRATION_SIMULATION_RUNBOOK.md)
的命令连接 `127.0.0.1:15432` 并执行安装文件。Compose 本身永远不会运行该 SQL。

## 5. 验证 Redis Stack、Elasticsearch 与 MinIO

Redis 必须同时支持普通命令和 RediSearch：

```bash
docker compose --env-file deploy/local/external-storage-lab/.env \
  -f deploy/local/external-storage-lab/docker-compose.yml \
  exec redis-stack sh -lc 'redis-cli -a "$REDIS_STACK_PASSWORD" --no-auth-warning PING'

docker compose --env-file deploy/local/external-storage-lab/.env \
  -f deploy/local/external-storage-lab/docker-compose.yml \
  exec redis-stack sh -lc 'redis-cli -a "$REDIS_STACK_PASSWORD" --no-auth-warning MODULE LIST'
```

Elasticsearch 与 MinIO：

```bash
curl -fsS -u 'elastic:<REDACTED>' http://127.0.0.1:19200/_cluster/health
curl -fsS http://127.0.0.1:19000/minio/health/live
```

`<REDACTED>` 只表示从私有演练 `.env` 读取实际值，不要把密码直接写进共享 Shell
历史。也可以只依赖 Compose health 状态完成本机验收。

## 6. 切换 RAG 私有连接配置

只编辑 Git 忽略的 `deploy/local/.env.ragflow`，不要编辑自动生成的 `.env.full`。保留 Chat、
Embedding、Vision、Rerank、ASR 和 RAGFlow 管理账号配置，只替换存储部分：

```dotenv
RAG_STORAGE_MODE=external

APP_DATABASE_DSN=postgresql+psycopg://lazytt_rag:<URL_ENCODED_PASSWORD>@host.docker.internal:15432/rag_app

REDIS_STACK_HOST=host.docker.internal
REDIS_STACK_PORT=16379
REDIS_STACK_PASSWORD=<REDACTED>

RAGFLOW_POSTGRES_HOST=host.docker.internal
RAGFLOW_POSTGRES_PORT=15432
RAGFLOW_POSTGRES_DATABASE=rag_flow
RAGFLOW_POSTGRES_USER=lazytt_rag
RAGFLOW_POSTGRES_PASSWORD=<REDACTED>

RAGFLOW_ES_SCHEME=http
RAGFLOW_ES_HOST=host.docker.internal
RAGFLOW_ES_PORT=19200
ELASTIC_PASSWORD=<REDACTED>

RAGFLOW_MINIO_HOST=host.docker.internal
RAGFLOW_MINIO_PORT=19000
MINIO_USER=lazytt_rag
MINIO_PASSWORD=<REDACTED>

RAGFLOW_API_KEY=
```

密码进入 `APP_DATABASE_DSN` 时必须进行 URL 编码；其他独立密码字段填写原值。可以
删除 `POSTGRES_IMAGE`、`REDIS_STACK_IMAGE`、`ELASTICSEARCH_IMAGE`、`MINIO_IMAGE`
四行。外部模式生成的 `.env.full` 不再包含它们。

容器内不能用 `127.0.0.1` 访问宿主机映射端口，因此演练连接使用
`host.docker.internal`。未来迁移到局域网服务器时，只把它改成实际服务器地址，
例如 `192.168.31.2`，端口改成运维分配值。

## 7. 启动与预期结果

只有 DBA 已经完成 PostgreSQL 安装和权限验收后，才允许启动 Runtime：

```bash
./deploy/local/start-full.sh
```

预期静态检查输出包含：

```text
runtime and external storage static preflight passed
external storage mode selected; local storage containers were not started
```

启动程序随后会：

1. 使用业务账号只读检查 `rag_app` 和 `rag_flow` 的 Schema、版本、对象所有权和
   Schema CREATE 权限；
2. Schema 不完整时立即停止，不会自动修复或迁移；
3. 通过官方 API 创建/登录本地 RAGFlow 管理员并生成新的 API Key；
4. 以正常 DML 初始化 Profile 和 Redis 语义缓存索引；
5. 启动 Gateway、Worker、Scheduler。

## 8. 业务 Smoke

至少验证：

1. `GET /health/ready` 返回 ready；
2. 使用同一 `X-Tenant-ID + X-User-ID` 创建知识库；
3. 上传一个最小非业务 DOCX/PDF；
4. 等待解析到 READY；
5. 执行混合检索并确认返回真实 Chunk；
6. 执行普通与 SSE 问答；
7. 删除文档和知识库；
8. 重启 runtime 后再次检查 ready 和模型配置。

## 9. 回退到原本地存储

先停止 runtime，不删除卷：

```bash
./deploy/local/stop-full.sh
cp deploy/local/.env.ragflow.before-external-lab deploy/local/.env.ragflow
chmod 600 deploy/local/.env.ragflow
./deploy/local/start-full.sh
```

原 `lazytt-rag-storage` 命名卷没有被修改，恢复源配置后会重新连接原数据。

演练环境暂时不用时执行：

```bash
docker compose \
  --env-file deploy/local/external-storage-lab/.env \
  -f deploy/local/external-storage-lab/docker-compose.yml \
  stop
```

不要执行 `docker compose down -v`。待确认演练数据确实不需要后，再单独制定卷清理
操作；这不属于本迁移验收流程。
