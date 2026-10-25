# 外部存储迁移演练 Compose

本目录只模拟一台全新的外部存储服务器，包含空 PostgreSQL、Redis Stack、
Elasticsearch 和 MinIO。它不复用 `lazytt-rag-storage` 的容器或命名卷，也不包含
RAGFlow 和自研 RAG 服务。

本环境不执行任何数据库迁移自动化：

- Compose 只启动四种空白存储服务；
- PostgreSQL 只创建镜像启动所必需的 DBA 管理账号和 `postgres` 管理库；
- 不会自动创建 `lazytt_rag`、`rag_app`、`rag_flow` 或任何业务表；
- DBA 必须手工审计并执行
  [当前两库 SQL 安装流程](../../ecs/README.md#4-数据库执行顺序)；
- RAG Runtime 启动时只读校验 Schema，不会替 DBA 建表或升级表。

完整操作、配置切换、验收与回退步骤见：

- [`docs/LOCAL_EXTERNAL_STORAGE_REHEARSAL.md`](../../../docs/LOCAL_EXTERNAL_STORAGE_REHEARSAL.md)

最小启动命令：

```bash
cp deploy/local/external-storage-lab/.env.example deploy/local/external-storage-lab/.env
# 将所有 change-me 替换为仅保存在本机的随机密码
docker compose \
  --env-file deploy/local/external-storage-lab/.env \
  -f deploy/local/external-storage-lab/docker-compose.yml \
  up -d --wait
```

四个容器均为 `healthy` 只代表存储服务可连接，不代表 PostgreSQL 业务结构已经安装。
下一步请由 DBA 按
[当前 ECS 数据库与候选迁移入口](../../ecs/README.md)
手工执行数据库安装和权限验收。

本目录中的 `image:` 只决定模拟服务器安装什么软件版本。RAG 运行时切到
`RAG_STORAGE_MODE=external` 后不读取这些镜像字段，只读取地址、端口和凭据。

本目录 `.env` 的变量名与系统连接配置统一：管理员使用 `POSTGRES_ADMIN_USER` 和
`POSTGRES_ADMIN_PASSWORD`，Redis 使用 `REDIS_STACK_PASSWORD` 和 `REDIS_STACK_PORT`，
Elasticsearch 使用 `ELASTIC_PASSWORD` 和 `RAGFLOW_ES_PORT`，MinIO 使用 `MINIO_USER`、
`MINIO_PASSWORD` 和 `RAGFLOW_MINIO_PORT`。PostgreSQL 宿主机映射端口使用
`RAGFLOW_POSTGRES_PORT`；`MINIO_CONSOLE_PORT` 仅供演练控制台使用。

本目录 `.env` 与 `../.env.ragflow` 仍由不同入口独立读取，同名不会自动同步值。
RAG 运行时连接该演练环境时，需要将共享的端口和凭据在两份文件中对齐；PostgreSQL
管理员凭据只供存储管理使用，业务连接继续使用 DBA 配置的业务账号。已有私有配置
升级时应更名旧变量并保留值，不要用示例覆盖现有 `.env`。

本机只有 24 GB 内存时，不要让该演练环境与原 `lazytt-rag-storage` 或其他
Elasticsearch 同时运行。停止演练只使用 `docker compose stop` 或 `down`，不要使用
`down -v`；只有在明确放弃整套模拟数据时才可删除演练卷。
