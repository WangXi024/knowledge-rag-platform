# RAGFlow 前置环境验证记录

验证日期：2026-07-29
状态：前置运行环境、真实 Ollama 文本/视觉模型接入及生命周期 Smoke 已完成。

> 历史快照：本报告只证明 2026-07-29 当时的本地模型环境。当前配置已经迁移到
> 供应商无关的 `BASE_URL/API_KEY/MODEL` 契约，新部署请使用
> [`deploy/README.md`](README.md)，不要照抄下文旧模型值或旧命令。

## 已验证

- 宿主机：Apple Silicon `arm64`，Docker Desktop 29.6.1，当前分配内存约
  12 GB。
- RAGFlow：官方 `infiniflow/ragflow:v0.26.4`。
- 仅 `ragflow-server` 和 `ragflow-task-executor` 使用 `linux/amd64`；
  PostgreSQL、Elasticsearch、MinIO、Redis 和 Smoke Fixture 使用原生
  平台。未设置全局 `DOCKER_DEFAULT_PLATFORM`。
- RAGFlow Server、Task Executor、PostgreSQL、Elasticsearch、MinIO 和
  Redis 全部通过 Compose 健康检查。
- 历史验证时管理界面返回 HTTP 200，且 8081、9380、9381 均只绑定
  `127.0.0.1`。当前运行 Profile 已关闭未被 Gateway 使用的 Admin Server 9381，
  仅保留管理 UI 8081 和主 API 9380；所有依赖服务仍无宿主机端口。
- 健康接口返回：

```json
{"db":"ok","doc_engine":"ok","redis":"ok","status":"ok","storage":"ok"}
```

- PostgreSQL 18.4 运行在原生 `aarch64`，RAGFlow 初始化出 42 张 public
  表；`tenant_model_provider`、`tenant_model_instance`、`tenant_model`、
  `document` 和 `task` 均存在。
- 整套服务执行非破坏性重启后，六个必需服务再次全部健康；PostgreSQL
  仍为 42 张表，关键表均保留。
- Test-only OpenAI-compatible embedding Fixture 容器已启动并健康，无
  宿主机端口；向量维度为 128，输出归一化且对测试查询有正相似度。
- Compose 默认 profile、Smoke profile、Shell 预检脚本及 Python Smoke
  工具均通过静态校验。
- 生命周期 Smoke 通过真实 RAGFlow API 完成：创建 1 个 Dataset、上传 1
  个合成文本 Fixture、启动 Task Executor 解析、状态到达 `DONE`、生成
  1 个 Chunk，并在检索接口返回 1 个候选。
- Smoke 的清理接口成功删除 1 个文档和 1 个 Dataset。清理后 PostgreSQL
  中 `lazytt-smoke-*` Dataset、Fixture 文档和孤立任务的数量均为 0；
  Elasticsearch 对应租户索引和元数据索引的文档数均为 0。
- 在完成账号和 Smoke 模型初始化后，再次非破坏性重启整套服务；所有
  容器恢复健康，PostgreSQL 仍为 42 张表。重启后的完整生命周期 Smoke
  再次通过，且清理后的 PostgreSQL 与 Elasticsearch 残留计数仍为 0。
- RAGFlow Server 容器通过 `host.docker.internal:11434` 看到了宿主机五个
  Ollama 模型；宿主机 Ollama 没有发布到局域网或公网。
- 真实调用 `bge-m3:latest` 的原生单条接口与 OpenAI 兼容批量接口分别
  返回 1 个和 2 个向量，所有向量维度均为 1024。
- 通过官方 Provider/Instance/Model API 持久注册
  `bge-m3:latest@ollama-local@Ollama`，并设为租户默认 Embedding；没有
  直接修改 RAGFlow 数据库。
- 通过同一官方模型 API 持久注册
  `qwen3-vl:4b@ollama-local@Ollama` 并设为租户默认 Vision 模型。使用内存
  合成 PNG 的原生 Ollama 视觉调用通过；非破坏性重启 RAGFlow Server 和
  Task Executor 后再次 Bootstrap，默认 Vision 配置仍然有效。
- 图片生命周期 Smoke 通过 Gateway 创建临时 `picture-default@1` 知识库，
  上传最小合成 PNG，RAGFlow Picture Parser 调用 `qwen3-vl:4b` 生成视觉
  描述，再以真实 `bge-m3` 建立索引。最近一次结果为 1 个 Chunk、301 Tokens，
  文档限定检索返回 1 个候选，最后删除文档和知识库。
- 使用真实 `bge-m3` 的 Dataset -> Upload -> Parse -> Chunk -> Retrieval
  -> Delete 两次通过，其中一次在非破坏性重启 Server 和 Task Executor
  之后执行。Task 日志明确记录任务的 `embd_id` 为上述 Ollama 模型。
- Gateway OpenAI 兼容 Adapter 对 `qwen3:8b` 的非流式回答、JSON
  结构化回答和 SSE 流式回答均通过；流式响应包含增量和 usage。
- Gateway/语义缓存 Adapter 的单条和批量 Embedding 均通过 1024 维强
  校验。错误维度的单元测试会以启动配置错误失败，不会继续写缓存索引。
- 本地管理员默认密码已通过官方用户 API 随机化，原临时 Smoke Key 已由
  官方 Token API 替换为稳定本地开发 Key并删除。凭据只存在 Git 忽略且
  权限为 `0600` 的本地环境文件，验证输出未包含完整凭据。
- 本报告首次验收当时 Rerank 未启用；2026-07-29 后续版本已由 Gateway 接入
  硅基流动真实 Cross-Encoder，RAGFlow 内仍未重复注册 Reranker。
- 两次真实模型 Smoke 清理后 PostgreSQL 中临时 Dataset、Fixture 文档、
  孤立 Task 均为 0；Elasticsearch 对两个临时 Dataset ID 的命中数为 0。
- 图片 Smoke 的临时文档和知识库也由 `finally` 清理，未使用真实业务图片，
  未删除或重建任何 Docker Volume。

## 发现并处理的问题

### PostgreSQL 与旧模型迁移

RAGFlow 通用初始化成功在 PostgreSQL 创建表，但可选
`--init-model-provider-tables` 参数调用 MySQL 专用迁移脚本，并尝试连接
`localhost:3306`，导致 Server 重启。

处理：移除该可选参数，保持 `DB_TYPE=postgres`，没有切换数据库。全新库
所需模型表已由通用初始化创建。Smoke 模型将使用 v0.26.4 的
Provider/Instance/Model API 注册。

### 8 GiB Docker 内存

Elasticsearch 最初使用 2 GiB JVM 堆和 512 MiB `/tmp` 内存盘，两次以
退出码 137 被终止。处理后使用 1 GiB 堆并移除 `/tmp` 内存盘，服务恢复
健康并通过整套重启验证。此设置仅适用于最小 Fixture，不代表生产容量
建议。

### 重复列日志

Server 与 Task Executor 每次启动都会执行通用初始化。已有 PostgreSQL
列会产生 `column already exists` 的 CRITICAL 文本，但初始化继续完成，
进程保持健康。该行为来自官方 v0.26.4 初始化实现，当前判断为幂等噪声，
后续需在 M4 的日志基线中保留观察。

### 首次账号初始化

本地验证卷中预置管理员的 `access_token` 为空。v0.26.4 的 API Token
鉴权除校验 `api_token` 表外，还要求其关联用户的 `access_token` 非空，
因此首次请求返回 401。通过 RAGFlow 官方 `UserService` 补齐随机
`access_token` 后，同一个 API Token 鉴权成功。

正常首次初始化应在管理界面创建账号或至少完成一次账号登录，再创建临时 API
Key；官方登录流程会生成该字段。当前环境随后通过官方用户和 Token API 完成
随机密码、稳定 Key、登录验证和旧 Key 清理。自动初始化工具显式验证该前置
条件，不向 `api_token` 表直接写入记录。

### Rerank 本地限制与后续状态

RAGFlow v0.26.4 不支持把 Ollama 作为 Rerank Provider，Ollama 也没有标准
`/rerank` 接口。现有 `qllama/bge-reranker-v2-m3:latest` 经当前 Ollama
Embedding 接口调用会失败，因此没有把它注册或标记为可用。后续只能通过
`RerankPort` 接入真正的独立 Cross-Encoder 服务。该后续工作已于 2026-07-29
完成：Gateway 使用硅基流动模型，Bootstrap 与 E2E 已验证真实分数；此变化不修改
RAGFlow 源码，也不改变本报告中的 RAGFlow 前置环境结论。

### 运行日志基线

- Task Executor 明确记录 Fixture 解析、128 维 Embedding、Elasticsearch
  写入和任务完成，耗时约 6 秒；删除接口随后执行
  `_delete_by_query`。
- PostgreSQL 健康探测偶发记录 `connection already closed`，官方重试逻辑
  在下一次尝试恢复，健康接口持续返回 `db=ok`。当前不阻塞前置环境，但
  M4 长时间稳定性验证需继续观察。
- 使用普通 API Token 时，鉴权会先尝试 JWT 并记录
  `No b'.' found in value`，随后 API Token 校验成功；这是 v0.26.4 的鉴权
  顺序日志。
- 最小英文 Fixture 会记录 `Load term.freq FAIL`，但解析状态为 `DONE`，
  Chunk 和检索结果均正常。该 Fixture 只验证技术链路，不用于质量判断。
