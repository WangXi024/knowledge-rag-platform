# LazyTT RAG 后端部署与使用手册

更新时间：2026-08-24

本手册只描述 LazyTT RAG 后端：存储、RAGFlow、Gateway、Worker、Scheduler、数据库
升级和 HTTP API。接口详细字段以
[`rag-service-openapi.json`](rag-service-openapi.json) 为准。

> [!IMPORTANT]
> 当前是 **Direct Tenant/User 开发联调模式**。请求携带 `X-Tenant-ID` 和
> `X-User-ID`，不使用注册、密码、Session、JWT 或 `issuer`。两个 Header 不是认证
> 凭据，只允许在受控网络中使用；生产环境必须由可信 Identity 提供并验证身份。

## 1. 当前系统状态

- 只存在 `TENANT` 和 `SYSTEM` 两类共享知识库，不存在个人知识库。
- 同一 Tenant 下的全部 User 当前都能读取该 Tenant 的全部 `TENANT` 知识库。
- 所有 Tenant 都能读取全部 `SYSTEM` 知识库。
- 当前没有知识库成员 ACL；后续可以在共享访问策略后增加知识库—成员多对多权限表。
- 创建、上传、重解析和删除使用 `/v1/management/*`；普通读取使用非 management 接口。
- 当前没有租户管理员或平台管理员账号/角色校验。Management 接口依赖内部网络边界，
  不能直接暴露到公网。
- 问答请求未指定知识库时，当前默认检索调用方可访问的全部 TENANT 和 SYSTEM 知识库；
  后续可以再增加默认库或显式选择规则做范围收窄。
- 当前 App PostgreSQL Schema 发布版本是 `V1.1.0`。
- 当前适配的 RAGFlow 固定版本是 `v0.26.4`。

## 2. 后端服务地址

| 地址 | 用途 | 暴露建议 |
|---|---|---|
| <http://127.0.0.1:8080> | RAG Gateway HTTP API | 受控网络；按配置可开放局域网 |
| <http://127.0.0.1:8080/docs> | Gateway Swagger UI | 受控网络 |
| <http://127.0.0.1:8080/openapi.json> | Gateway 运行时 OpenAPI | 受控网络 |
| <http://127.0.0.1:8081> | RAGFlow 本地运维界面 | 只绑定本机 |
| <http://127.0.0.1:9380> | RAGFlow API | 只供 Gateway、Worker 和运维检查使用 |

PostgreSQL、Redis Stack、Elasticsearch 和 MinIO 默认不发布宿主机端口。Gateway、
Worker、Scheduler 和 RAGFlow 通过共享 Docker 网络访问存储。

## 3. 最低准备

安装 Git、Docker Desktop 和 `uv`：

```bash
git --version
docker version
docker compose version
uv --version
```

Docker Desktop 必须已经启动。建议给 Docker 至少分配 12 GB 内存。不要执行
`docker compose down -v`，否则可能删除命名卷中的数据库和对象数据。

## 4. 获取代码

```bash
git clone <BACKEND_REPOSITORY_URL> lazytt-rag-system
cd lazytt-rag-system
```

## 5. 创建本机私有配置

```bash
cp deploy/local/.env.ragflow.example deploy/local/.env.ragflow
chmod 600 deploy/local/.env.ragflow
```

只编辑 Git 忽略的 `deploy/local/.env.ragflow`。不要把真实密码、API Key 或 Token 写入示例
文件、文档、日志、聊天或 Git。

### 5.1 本地服务密码

以下字段首次运行可以保持 `change-me` 或空值，启动脚本会生成随机密码，后续启动会
保留已有值：

```dotenv
POSTGRES_ADMIN_PASSWORD=change-me
APP_POSTGRES_PASSWORD=change-me
RAGFLOW_POSTGRES_PASSWORD=change-me
ELASTIC_PASSWORD=change-me
MINIO_PASSWORD=change-me
REDIS_STACK_PASSWORD=change-me
RAGFLOW_ADMIN_PASSWORD=change-me
```

共享 PostgreSQL 是物理服务合并；`rag_app` 与 `rag_flow` 仍是两个独立数据库和两个
最小权限账号。运行时服务不会获得 PostgreSQL 初始化管理员密码。

### 5.2 OpenAI-compatible 模型

```dotenv
LLM_BASE_URL=https://provider.example/v1
LLM_API_KEY=<REDACTED>
LLM_MODEL=chat-model-name
REWRITE_MODEL=

EMBEDDING_BASE_URL=https://provider.example/v1
EMBEDDING_API_KEY=<REDACTED>
EMBEDDING_MODEL=embedding-model-name
EMBEDDING_DIMENSION=pending-auto-detection

VISION_BASE_URL=https://provider.example/v1
VISION_API_KEY=<REDACTED>
VISION_MODEL=vision-model-name

MEDIA_VISION_BASE_URL=https://provider.example/v1
MEDIA_VISION_API_KEY=<REDACTED>
MEDIA_VISION_MODEL=media-vision-model-name
```

要求：

- LLM、文档 Vision 和媒体 Vision 支持 `POST /chat/completions`；
- Embedding 支持 `POST /embeddings`；
- Base URL 是否包含 `/v1` 以供应商契约为准；
- `EMBEDDING_DIMENSION` 可以自动探测，固定值与实际不一致会阻止服务就绪。

### 5.3 可选 Rerank

```dotenv
RERANK_ENABLED=false
RERANK_BASE_URL=
RERANK_API_KEY=
RERANK_MODEL=
```

启用时，兼容服务需要提供 `POST /rerank`。在线 Rerank 暂时失败时，检索会保留 RRF
排序并返回降级 Warning，不会用向量余弦分数冒充 Cross-Encoder。

### 5.4 可选媒体和 MinerU

普通图片和文档 OCR 使用 `VISION_*`；视频抽帧描述独立使用 `MEDIA_VISION_*`。
音视频需要项目配置的 FFmpeg/ffprobe、ASR 和媒体 Vision 能力。音轨默认按 60 秒、
重叠 2 秒分段；视频以 10 秒为基础间隔抽帧并补充结尾画面，长视频会在最多 20 帧内
自动均匀放宽间隔。复杂 PDF 使用
`mineru-default@1` 时，还必须配置可用的 MinerU Server/Backend；未配置时不要选择该
Parser。

## 6. 存储模式

### 6.1 local

默认配置：

```dotenv
RAG_STORAGE_MODE=local
```

`start-full.sh` 会启动本地 PostgreSQL、Redis Stack、Elasticsearch、MinIO、RAGFlow 和
应用运行时，并对本地 `rag_app` 执行幂等 Schema Prepare。

### 6.2 external

```dotenv
RAG_STORAGE_MODE=external
```

external 模式不会启动本地存储，也不会执行数据库 DDL。必须由 DBA 提前准备 App
PostgreSQL、RAGFlow PostgreSQL、Redis Stack、Elasticsearch 和 MinIO，并将连接信息填入
私有配置。

## 7. 启动完整后端

```bash
./deploy/local/start-full.sh
```

脚本依次执行：

1. 安装锁定依赖并准备脱敏运行配置；
2. 校验存储模式、镜像、模型和 Compose 拓扑；
3. local 模式启动存储并把 App Schema 幂等准备到 `V1.1.0`；
4. 启动 RAGFlow Server 和 Task Executor；
5. 创建或复用 RAGFlow 运维账号/API Key；
6. 探测并注册模型；
7. 构建并启动 Gateway、Worker 和 Scheduler。

该脚本不会删除 PostgreSQL、Redis、Elasticsearch 或 MinIO 数据卷。任一步失败都会停止
后续流程，并保留已有数据供排查。

启动后检查：

```bash
curl http://127.0.0.1:8080/health/live
curl http://127.0.0.1:8080/health/ready
curl http://127.0.0.1:8080/v1/version
curl http://127.0.0.1:8080/v1/capabilities
```

## 8. Direct Tenant/User 与共享访问

除健康、版本和能力接口外，请求必须同时携带：

```http
X-Tenant-ID: stable-tenant-id
X-User-ID: stable-user-id
```

当前服务端维护三类 Scope：

- 主体 Scope：由 Tenant ID + User ID 派生，用于调用审计、缓存、请求和幂等边界；
- Tenant 资源 Scope：由 Tenant ID 派生，保存该租户的 `TENANT` 知识库及其文档；
- System 资源 Scope：系统固定范围，保存所有租户可读的 `SYSTEM` 知识库及其文档。

因此，同一个 Tenant 下更换 User ID 不会改变可见的共享知识库；更换 Tenant ID 后，只能
看到新 Tenant 的共享库和所有 SYSTEM 库。`X-User-ID` 仍然必传，但不创建个人知识库。

当前没有 `issuer` 字段，也没有任何 `DIRECT_USER_ISSUER` 配置。

## 9. 管理接口与普通读取接口

### 9.1 内部管理接口

以下操作使用 `/v1/management/*`：

- 创建、查询、修改和删除知识库；
- 上传、删除、解析、重解析、重试和更新文档版本；
- 查询和取消异步 Job；
- 查询管理侧文档和 Chunk。

Swagger 按 `KnowledgeBases`、`Documents`、`Jobs` 分组，不会单独显示一个 `Admin` 标签。
路径中的 `/management` 才是内部管理入口标识。

### 9.2 普通只读接口

```text
GET /v1/knowledge-bases
GET /v1/knowledge-bases/{knowledge_base_id}/documents
GET /v1/documents/{document_id}
GET /v1/documents/{document_id}/chunks
```

这些接口执行 TENANT/SYSTEM 共享访问过滤，不提供创建、上传、修改或删除能力。

## 10. 创建两类共享知识库

创建接口：

```text
POST /v1/management/knowledge-bases
```

Headers：

```http
Content-Type: application/json
X-Tenant-ID: tenant-a
X-User-ID: kb-operator
Idempotency-Key: create-tenant-a-kb-001
```

创建 Tenant 共享知识库：

```json
{
  "scope_type": "TENANT",
  "external_id": "tenant-a-main-kb",
  "name": "租户A共享知识库",
  "description": "tenant-a 下所有用户共享访问",
  "parser_profile": "office-default@1",
  "chunk_profile": "general-256@2",
  "embedding_profile": "embedding-openai-compatible@3",
  "retrieval_profile": "hybrid-structure-aware@6"
}
```

创建 System 共享知识库时，把 `scope_type` 改为 `SYSTEM`。建议内部平台操作统一使用固定
的 Header，例如：

```http
X-Tenant-ID: platform-management
X-User-ID: kb-operator
```

SYSTEM 知识库最终写入系统资源 Scope，不属于 `platform-management` Tenant。上述 Header
只是当前联调约定，不是管理员凭证。

`external_id` 可选，用于调用方关联自己的业务对象；在同一资源 Scope 内必须唯一。

## 11. 上传文件

上传接口：

```text
POST /v1/management/knowledge-bases/{knowledge_base_id}/documents
```

每个请求只支持一个文件，默认最大 `100 MiB`。批量上传需要发送多个请求，并为每个
不同文件使用不同的 `Idempotency-Key`。

`multipart/form-data` 字段：

| 字段 | 必填 | 说明 |
|---|---|---|
| `file` | 是 | 单个文件 |
| `external_id` | 否 | 调用方文档标识；不需要时不要传 |
| `metadata` | 否 | JSON 对象序列化后的字符串 |
| `parser_profile` | 否 | 显式 Parser；留空时由后端按兼容默认值和配置规则选择 |
| `chunk_profile` | 否 | 覆盖自动选择的 Chunk Profile |
| `auto_parse` | 否 | 默认 `true`，上传后自动创建解析任务 |

不要手工固定 multipart 的 `Content-Type` boundary，应由 HTTP 客户端生成。

### 11.1 Parser 自动选择

当前优先级：

```text
上传显式 parser_profile
→ 兼容当前扩展名的知识库默认 Parser
→ parser_profiles.example.yaml 中首条匹配的 selection_rules
→ 无匹配时返回 422 / RAG-KB-004
```

不传 `parser_profile` 时，后端自动选择：

| 文件类型 | Parser |
|---|---|
| PDF | `deepdoc-default@1` |
| 复杂公式/排版 PDF | 知识库默认值设为 `mineru-default@1` 时保留；需要 MinerU 已配置 |
| DOC/DOCX/PPT/PPTX/XLS/XLSX/TXT/MD/HTML/CSV | `office-default@1` |
| JSON/JSONL/LDJSON | `json-default@1` |
| PNG/JPG/JPEG/WEBP | `picture-default@1` |
| MP3/WAV/M4A/MP4/MOV 等媒体 | `media-default@1` |

例如，知识库默认 Parser 是 `office-default@1` 时，上传 PDF 会自动选择
`deepdoc-default@1`；如果知识库默认值是支持 PDF 的 `mineru-default@1`，则继续使用
MinerU，不会被通用 PDF 规则覆盖。显式传入的 Parser 具有最高优先级，但不兼容文件时
仍返回 `422 / RAG-KB-004`。最终 Parser 会保存到 Document/DocumentVersion，重试和
重解析不会再次自动路由；修改知识库默认值只影响后续新上传。

### 11.2 Chunk 当前行为

上传时不传 `chunk_profile`，Gateway 会按文件族自动选择：

| 文件族 | Chunk Profile |
|---|---|
| PDF、Word、长文本、JSON/JSONL/LDJSON | `prose-384@1` |
| XLS/XLSX/CSV | `table-192@1` |
| PPT/PPTX、图片 | `visual-192@1` |
| 音视频转写 | `media-320@1` |
| 未识别格式 | 回退知识库默认 Chunk Profile |

显式 `chunk_profile` 始终拥有更高优先级。修改知识库默认 Parser/Chunk 不会自动重建已
存在文档；需要调用文档重解析接口。

## 12. 幂等键

创建、上传、解析、重解析、重试、删除和反馈等写操作要求 `Idempotency-Key`。

- 重试完全相同的 Method、Path、文件和参数：复用原 Key；
- 更换文件或修改任一业务参数：使用新 Key；
- 同一个 Key 对应不同请求会返回 `409 / RAG-COMMON-009`；
- 幂等记录不保存文件正文，上传指纹包含文件 SHA-256。

示例：

```text
upload-tenant-a-image-005-001
upload-tenant-a-image-006-001
```

## 13. 查询任务和文档状态

上传成功返回 `202`，其中包含 `document_id` 和 `job_id`。轮询：

```text
GET /v1/management/jobs/{job_id}
```

Job 最终成功状态是 `SUCCEEDED`；文档最终可检索状态是 `READY`。文档列表：

```text
GET /v1/management/knowledge-bases/{knowledge_base_id}/documents
```

如果上传时 `auto_parse=false`，需要显式调用：

```text
POST /v1/management/documents/{document_id}/parse
```

## 14. 检索策略

当前推荐 Retrieval Profile 是：

```text
hybrid-structure-aware@6
```

它支持问题分类、动态 Top-K、双语 QueryPlan、向量/BM25、加权 RRF、可选 Rerank、
Summary/Section/Parent/Table/Entity 侧车导航、0.40 侧车余弦相似度门槛、去重和相邻 Chunk 上下文补全。侧车命中
不会直接作为最终证据，最终引用仍回读 RAGFlow 原始 Chunk。

一次请求对多个知识库使用统一 Retrieval Profile，不会按单个文件分别切换检索策略。

## 15. 数据库发布与升级

### 15.1 当前 V1.1.0 结构变化

V1.1.0 增加：

- `knowledge_base.scope_type`：只允许 `TENANT` 或 `SYSTEM`；
- `idx_knowledge_base_type_scope_status`；
- `citation_record.resource_scope_id`；
- 引用到知识库/文档的外键改为使用资源 Scope。

### 15.2 local 模式

`start-full.sh` 自动执行 `schema prepare-local`。空库会依次创建历史结构、登记 V1.0.0，
再升级到 V1.1.0；已经是 V1.1.0 时幂等跳过。它不会自动删除旧业务数据。

### 15.3 external 模式

external 模式不自动执行 DDL。DBA 使用：

```text
migrations/rag_app/V1.0.0__to__V1.1.0__shared_knowledge_bases.sql
```

旧个人资源不能安全推断为租户或系统共享资源，因此增量 SQL 要求知识资源、Job、问答、
反馈和幂等表为空。检查失败会整体回滚，脚本本身不会删除任何数据。执行前必须完成备份
和经批准的数据清理。

## 16. 启停和日志

```bash
# 停止运行时，保留存储容器和命名卷
./deploy/local/stop-full.sh

# 重新构建并启动完整后端
./deploy/local/start-full.sh

# 单独管理 local 存储
./deploy/local/start-storage.sh
./deploy/local/stop-storage.sh
```

查看运行时日志：

```bash
docker compose --env-file deploy/local/.env.full \
  -f compose.local.yml logs -f --since 10m \
  rag-gateway rag-worker rag-scheduler ragflow-server ragflow-task-executor
```

查看容器状态：

```bash
docker compose --env-file deploy/local/.env.full \
  -f compose.local.yml ps

docker compose --env-file deploy/local/.env.full \
  -f deploy/local/docker-compose.storage.yml ps
```

## 17. 常见故障

### `/v1/knowledge-bases` 返回 404

运行中的 Gateway 镜像可能早于普通只读接口。重新运行 `./deploy/local/start-full.sh` 构建并
重建无状态应用容器。该操作不删除知识库、文档或数据卷。

### `RAG-COMMON-009` 幂等冲突

当前 `Idempotency-Key` 已用于不同文件或参数。不同业务请求换新 Key；只有完全相同的
网络重试才复用原 Key。

### `RAG-KB-004` Parser 不兼容

检查上传文件扩展名和显式 `parser_profile`。自动模式会执行配置中的 `selection_rules`；
如果显式 Parser 不兼容或扩展名没有任何规则，仍会返回该错误。

### 同租户用户看不到共享库

先直接请求 `GET /v1/knowledge-bases`。正常无数据应返回 `200` 和空数组，不应返回 404。
确认请求使用正确 `X-Tenant-ID`；同一 Tenant 下修改 `X-User-ID` 不应改变共享知识库列表。

### RAGFlow 解析进度长时间不变

同时检查 Gateway Worker 和 RAGFlow Task Executor 日志。RAGFlow 某些阶段只在批次完成
后更新进度；应以 Job、Document 状态和错误对象共同判断。

### 模型返回 401

API Key、Base URL 或模型名不匹配。只修改私有 `deploy/local/.env.ragflow`，不要直接修改自动
生成的 `.env.full`；修正后重新执行 `start-full.sh`。

### Docker daemon unavailable

启动 Docker Desktop，等待 Engine Running 后重试。修改 Docker 资源后也要等待 Engine
完全恢复。

## 18. 安全边界

- 不提交 `.env.ragflow`、`.env.full`、API Key、管理员密码或模型凭据；
- 不在日志、文档或聊天中输出完整秘密；
- 不对公网开放 RAGFlow 管理界面、数据库、Redis、Elasticsearch 或 MinIO；
- Direct Tenant/User 和 Management 接口仅限受控内部网络；
- 不允许调用方提交最终 `security_scope_id`，只能由服务端派生；
- 不执行 `docker compose down -v`；
- 不直接修改 RAGFlow 数据库，也不修改/Fork RAGFlow 源码；
- 生产前必须接入可信 Identity、TLS、管理员授权、成员 ACL、限流、审计、配额和备份恢复。

完整后端链路：

```text
准备存储与模型 → 启动 Gateway/Worker/RAGFlow → 创建 TENANT 或 SYSTEM 知识库
→ 单文件上传并选择 Parser → 自动选择 Chunk → RAGFlow 解析/索引
→ 结构感知混合检索 → LLM 回答 → 引用回读与缓存
```
