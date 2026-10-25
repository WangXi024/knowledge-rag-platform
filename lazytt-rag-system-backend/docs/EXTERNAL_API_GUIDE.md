# LazyTT RAG 外部调用与接口接入指南

更新时间：2026-08-24

适用 API 版本：`v1`

当前 RAGFlow 契约版本：`v0.26.4`

本文面向需要把 LazyTT RAG 接入业务系统、Agent、聊天机器人、内部平台或测试工具的
开发者。内容以当前 FastAPI 路由、Pydantic Schema、运行时 Profile 和 OpenAPI 契约
为准，说明系统能够对外提供什么能力、怎样标识调用用户、支持哪些文件、怎样上传与解析、
怎样检索和流式问答，以及接入时必须遵守的安全边界。

机器可读的完整字段契约位于：

- [OpenAPI 契约](../rag-development-spec/openapi/rag-api-v1.yaml)
- [稳定错误码](../rag-development-spec/docs/04-错误码说明.md)
- [API 语义说明](../rag-development-spec/docs/02-API接口说明.md)

如果本文与机器可读契约的字段约束发生冲突，应以 OpenAPI 和当前运行时代码为准，
并把文档漂移作为缺陷修复。

## 1. 外部系统应该连接哪里

本地完整环境启动后，外部调用方连接的是自研 RAG Gateway：

```text
http://127.0.0.1:8080
```

默认只允许本机访问。需要从同一受信 Wi-Fi/局域网的其他主机调用时，
在 Git 忽略的 `deploy/local/.env.ragflow` 中设置：

```text
RAG_APP_BIND_ADDRESS=0.0.0.0
```

然后重新运行 `./deploy/local/start-full.sh`。在 macOS 上，脚本会优先显示由
Bonjour/mDNS 提供的稳定 `.local` 地址，同时保留当前局域网 IP 作为备用。例如
本机 `LocalHostName` 已设置为 `wx` 时：

```text
http://wx.local:8080
```

其他主机先访问 `GET http://wx.local:8080/health/live` 验证网络，再使用
`X-Tenant-ID + X-User-ID` 调用业务接口。这只会开放 Gateway 8080，不会开放 RAGFlow 8081/
9380、数据库或 Redis；Admin Server 9381 已在运行 Profile 中关闭。局域网模式不提供
TLS，不应通过路由器端口转发
暴露到公网。如果调用方不支持 mDNS、无线网络开启了客户端隔离，或者出现同名
设备冲突，可以改用启动脚本同时显示的 `http://<局域网IP>:8080`。

同一个 Docker Compose 网络中的服务可以使用：

```text
http://rag-gateway:8080
```

生产环境应通过 HTTPS 反向代理提供类似下面的地址：

```text
https://<rag-host>
```

需要明确区分下面几个入口：

| 入口 | 用途 | 是否属于外部业务 API |
| --- | --- | --- |
| Gateway `:8080/v1/*` | 知识库、文档、检索、问答、引用与反馈 | 是 |
| Gateway `:8080/health/*` | 容器和依赖健康检查 | 是 |
| Gateway `:8080/docs` | FastAPI Swagger 调试页面 | 仅建议开发环境使用 |
| Gateway `:8080/openapi.json` | FastAPI 运行时生成的接口描述 | 仅建议开发和生成客户端使用 |
| RAGFlow `:8081` | RAGFlow 本地管理界面 | 否 |
| 开发控制台 `:3000` | 人工测试全部 Gateway 接口 | 否，属于测试前端 |
| 普通用户端 `:3001` | Tenant/User 联调身份、共享知识库只读浏览与问答 | 否，浏览器通过 BFF 间接调用 Gateway |

访问 `http://127.0.0.1:8080/` 返回 `404 Not Found` 是正常现象，因为根路径不是业务
接口。外部系统应调用 `/v1/*` 或 `/health/*`。

外部调用方不应直接访问 RAGFlow Server、PostgreSQL、Elasticsearch、MinIO、Redis
或 Valkey，也不应持有 `RAGFLOW_API_KEY`。这些都是 Gateway 后面的内部实现。

## 2. 系统对外提供的主要能力

当前 Gateway 对外提供 34 个主要接口，能够完成以下完整链路：

1. 查询服务版本、运行状态、支持格式和 Profile；
2. 创建、查询、修改和删除按安全 Scope 隔离的知识库；
3. 上传单个文件，进行完整性校验、精确重复文件检测和异步处理；
4. 解析 PDF、Office、文本、Markdown、HTML、图片、音频和视频；
5. 查询文档、Job、解析进度、Chunk 内容及原文位置；
6. 手动解析、重新解析、失败重试、安全文件版本更新、回滚、取消任务和异步删除；
7. 执行不调用 LLM 的纯检索，获得候选 Chunk、得分和 QueryPlan；
8. 执行普通 JSON RAG 问答或 SSE 流式问答；
9. 自动进行中英文 Query Rewrite、多路线召回、加权 RRF 和可选真实 Rerank；
10. 使用文档摘要、章节、动态 Parent、表格 Parent 和轻量实体关系侧车辅助导航；
11. 返回可追溯引用、页码、坐标、原文摘录和相关分数；
12. 使用精确缓存和语义缓存，并在响应中说明缓存类型；
13. 查询问答请求状态、取消请求、读取引用和提交反馈。

侧车摘要、Parent、章节和轻量实体关系只用于找到真实证据。最终候选和引用仍会回读
RAGFlow 原始 Chunk，不会把自动生成的摘要或关系描述冒充原文证据。

## 3. Direct Tenant/User 与数据隔离

### 3.1 哪些接口需要 Tenant ID 与 User ID

健康、版本和能力接口公开：

```text
GET /health/live
GET /health/ready
GET /v1/version
GET /v1/capabilities
```

其余业务接口必须携带：

```http
X-Tenant-ID: stable-tenant-id
X-User-ID: stable-user-id
```

当前 Gateway 和两套前端都不提供注册、邮箱密码登录或 JWT。普通用户端只用
HttpOnly Cookie 记住联调人员填写的两个 ID，它不是可信 Session。两个 Header
只用于受控开发联调，不证明调用方属于该租户或拥有这个身份；生产接入公司 Identity
后，必须由可信令牌或内部网关提供 Tenant/User。

### 3.2 Scope 怎样派生

Gateway 使用请求中的 Tenant/User：

```text
X-Tenant-ID + "user:" + X-User-ID
```

两项仍用于生成调用主体 Scope，供查询审计、缓存和幂等隔离。知识资源不再按个人
User ID 归属：租户共享知识库使用 Tenant 资源 Scope，系统共享知识库使用固定的全局
资源 Scope。当前同一 Tenant 的全部用户可读租户共享库，所有 Tenant 均可读系统库。

Tenant ID 与 User ID 都必须为 1～128 字符且不含控制字符。客户端不能直接提交 Scope
摘要；访问另一个租户资源的 UUID 与资源不存在统一返回 `404`。但调用方仍能伪造
两个 Header，所以当前能力是数据隔离，不是认证。

### 3.3 调用环境变量

后续示例使用：

```bash
export RAG_BASE_URL=http://127.0.0.1:8080
export RAG_TENANT_ID=external-dev-tenant
export RAG_USER_ID=external-dev-user
```

请求示例：

```bash
curl -H "X-Tenant-ID: $RAG_TENANT_ID" \
  -H "X-User-ID: $RAG_USER_ID" \
  "$RAG_BASE_URL/v1/knowledge-bases"
```

不要把生产 Identity Token 或模型密钥混入该 Header。

## 4. 通用 HTTP 约定

### 4.1 请求 ID

客户端可以传入一个 UUID：

```http
X-Request-ID: 7bfb752c-8617-409f-a1ea-b4b2b608026b
```

不传时服务器自动生成。成功和失败响应都会包含 `request_id`，响应 Header 也会返回
`X-Request-ID`。排障时应记录该 ID，不要只记录中文错误信息。

### 4.2 幂等键

以下写操作必须携带：

```http
Idempotency-Key: <1-128个可打印字符>
```

需要幂等键的接口包括：

- 创建知识库；
- 上传文档；
- 上传文档新版本或回滚到退役版本；
- 删除知识库或文档；
- Parse、Reparse、Retry；
- 取消 Job 或问答请求；
- 提交反馈。

当前幂等记录默认保留 24 小时。相同 Scope、Method、Path、幂等键和请求内容会返回
首次结果，不会重复创建资源。相同幂等键对应不同请求内容时返回 `409`。

上传失败后重试必须复用原来的幂等键；创建一项全新的业务操作时必须使用新键。

### 4.3 成功响应外壳

普通成功响应统一包含：

```json
{
  "request_id": "7bfb752c-8617-409f-a1ea-b4b2b608026b",
  "data": {}
}
```

分页响应额外包含：

```json
{
  "request_id": "7bfb752c-8617-409f-a1ea-b4b2b608026b",
  "data": [],
  "meta": {
    "page": 1,
    "page_size": 20,
    "total": 0
  }
}
```

知识库、文档和 Chunk 列表的 `page_size` 最大为 100；纯检索返回数量最大为 50。

### 4.4 错误响应

所有普通 HTTP 错误采用：

```json
{
  "request_id": "7bfb752c-8617-409f-a1ea-b4b2b608026b",
  "error": {
    "code": "RAG-DOC-002",
    "message": "文件类型不支持",
    "retryable": false,
    "details": []
  }
}
```

客户端应根据稳定的 `error.code` 和 `retryable` 处理，不应依赖中文 `message` 做程序
分支。只有 `retryable=true` 才建议有限重试，默认最多重试两次，并加入退避和抖动。

### 4.5 异步资源约定

上传、解析、版本更新、版本回滚、重解析、重试和删除等耗时操作通常返回 HTTP `202`：

```json
{
  "request_id": "...",
  "data": {
    "job_id": "2e515ffe-12e5-4e96-967d-335b4c5e5fd7",
    "status": "PENDING"
  }
}
```

`202` 只表示任务已经安全接收，不表示解析已经成功。客户端必须轮询
`GET /v1/management/jobs/{job_id}`，并在完成后再次查询文档状态。

## 5. 全部公开接口清单

### 5.1 系统接口：4 个

| Method | Path | 身份 Header | 功能 |
| --- | --- | --- | --- |
| `GET` | `/health/live` | 否 | 只检查 Gateway 进程是否存活 |
| `GET` | `/health/ready` | 否 | 检查数据库、Redis、RAGFlow 等依赖是否就绪 |
| `GET` | `/v1/version` | 否 | 返回 API、构建和 RAGFlow 契约版本 |
| `GET` | `/v1/capabilities` | 否 | 返回实际支持格式、大小上限、Profile 和功能开关 |

接入程序应优先调用 `/v1/capabilities` 动态获得能力，不要在客户端永久硬编码文件
格式或 Profile 列表。

### 5.2 知识库接口：6 个

| Method | Path | 幂等键 | 功能 |
| --- | --- | --- | --- |
| `GET` | `/v1/knowledge-bases` | 不需要 | 普通用户只读分页查询可访问共享库；可用 `scope_type=TENANT\|SYSTEM` 筛选归属 |
| `POST` | `/v1/management/knowledge-bases` | 必需 | 创建租户共享或系统共享知识库和对应 RAGFlow Dataset |
| `GET` | `/v1/management/knowledge-bases` | 不需要 | 分页查询当前租户与系统共享知识库 |
| `GET` | `/v1/management/knowledge-bases/{knowledge_base_id}` | 不需要 | 查询知识库详情 |
| `PATCH` | `/v1/management/knowledge-bases/{knowledge_base_id}` | 不需要 | 修改名称、描述和未来任务使用的 Profile |
| `DELETE` | `/v1/management/knowledge-bases/{knowledge_base_id}` | 必需 | 异步删除知识库；`force=true` 可连同文档删除 |

知识库列表支持：

```text
page
page_size
name
status
```

知识库有文档时，普通删除返回 `RAG-KB-005`。确实要连同文档删除时使用：

```http
DELETE /v1/management/knowledge-bases/{id}?force=true
```

`/v1/knowledge-bases` 不经过管理授权，只应用同租户、系统共享和未来成员 ACL 访问
策略。普通用户端不应调用 `/v1/management/*`；management 接口保留给受控内部管理端。

### 5.3 文档、版本和 Chunk 接口：14 个

| Method | Path | 幂等键 | 功能 |
| --- | --- | --- | --- |
| `GET` | `/v1/knowledge-bases/{knowledge_base_id}/documents` | 不需要 | 普通用户只读分页查询可访问共享知识库中的文档 |
| `GET` | `/v1/documents/{document_id}` | 不需要 | 普通用户只读查询可访问共享文档详情 |
| `GET` | `/v1/documents/{document_id}/chunks` | 不需要 | 普通用户只读分页查询可访问共享文档切块 |
| `POST` | `/v1/management/knowledge-bases/{knowledge_base_id}/documents` | 必需 | 上传一个文件，可自动创建解析任务 |
| `GET` | `/v1/management/knowledge-bases/{knowledge_base_id}/documents` | 不需要 | 分页查询知识库中的文档 |
| `GET` | `/v1/management/documents/{document_id}` | 不需要 | 查询单个文档状态、进度和统计 |
| `DELETE` | `/v1/management/documents/{document_id}` | 必需 | 异步删除文档及索引 |
| `POST` | `/v1/management/documents/{document_id}/parse` | 必需 | 对已上传但未解析的文档启动首次解析 |
| `POST` | `/v1/management/documents/{document_id}/reparse` | 必需 | 清除旧 Chunk 后使用可选新 Profile 重建 |
| `POST` | `/v1/management/documents/{document_id}/retry` | 必需 | 根据最近失败阶段创建有限重试任务 |
| `GET` | `/v1/management/documents/{document_id}/chunks` | 不需要 | 分页读取 Chunk、页码、坐标和 Metadata |
| `POST` | `/v1/management/documents/{document_id}/versions` | 必需 | 上传候选文件并创建安全版本更新 Job |
| `GET` | `/v1/management/documents/{document_id}/versions` | 不需要 | 分页查询活动、退役、候选和失败版本 |
| `POST` | `/v1/management/documents/{document_id}/versions/{version_id}/rollback` | 必需 | 异步回滚到一个可用退役版本 |

文档列表支持：

```text
page
page_size
status
filename
external_id
```

删除文档时，如果希望先请求取消活动任务，可以传：

```http
DELETE /v1/management/documents/{id}?cancel_active=true
```

版本更新不会原地覆盖当前 RAGFlow 文档。候选版本在独立物理文档中完成解析、Embedding、
Metadata 和侧车索引，发布成功前旧版本一直可检索。当前 `AUTO` 模式明确返回
`effective_mode=SAFE_REPLACE`：即使新文件只追加少量内容，也会安全重建候选索引，
而不是宣称已经完成 Chunk 级增量更新。完整状态、故障补偿和验收步骤见
[DOCUMENT_VERSION_UPDATES.md](DOCUMENT_VERSION_UPDATES.md)。

### 5.4 Job 接口：2 个

| Method | Path | 幂等键 | 功能 |
| --- | --- | --- | --- |
| `GET` | `/v1/management/jobs/{job_id}` | 不需要 | 查询任务类型、状态、进度、次数和错误 |
| `POST` | `/v1/management/jobs/{job_id}/cancel` | 必需 | 请求取消尚未结束的任务 |

Job 状态包括：

```text
PENDING
RUNNING
RETRY_WAIT
SUCCEEDED
FAILED
CANCEL_REQUESTED
CANCELLED
```

已经 `SUCCEEDED`、`FAILED` 或 `CANCELLED` 的 Job 不能再取消；这类请求返回冲突而不是
把历史状态改写。

### 5.5 检索接口：1 个

| Method | Path | 功能 |
| --- | --- | --- |
| `POST` | `/v1/retrieval` | 执行纯检索，返回 QueryPlan 和候选 Chunk，不调用答案 LLM |

适用于搜索页、调试召回、Agent 自己组织 Context、检索质量评估和引用预览。

### 5.6 问答与请求接口：4 个

| Method | Path | 幂等键 | 功能 |
| --- | --- | --- | --- |
| `POST` | `/v1/answers` | 不需要 | 等待完整 RAG 答案后返回 JSON |
| `POST` | `/v1/answers/stream` | 不需要 | 使用 SSE 增量返回答案、引用和完成状态 |
| `GET` | `/v1/requests/{request_id}` | 不需要 | 查询问答请求状态和脱敏结果 |
| `POST` | `/v1/requests/{request_id}/cancel` | 必需 | 请求取消正在执行的问答 |

### 5.7 引用接口：2 个

| Method | Path | 功能 |
| --- | --- | --- |
| `GET` | `/v1/requests/{request_id}/citations` | 查询一次回答最终使用的全部引用 |
| `GET` | `/v1/citations/{citation_id}` | 查询单个引用的来源、摘录、页码和位置 |

### 5.8 反馈接口：1 个

| Method | Path | 幂等键 | 功能 |
| --- | --- | --- | --- |
| `POST` | `/v1/feedback` | 必需 | 对回答提交点赞、点踩、问题标签或修正答案 |

允许的 `feedback_type`：

```text
helpful
not_helpful
answer_incorrect
citation_incorrect
should_answer
should_refuse
other
```

`rating` 允许 `1`、`-1` 或 `null`。

## 6. 支持解析的文件

### 6.1 当前运行时白名单

单个文件默认最大 `100 MiB`，外部 API 当前支持：

| 文件族 | 扩展名 | Parser Profile | 自动推荐 Chunk Profile | 处理方式 |
| --- | --- | --- | --- | --- |
| PDF | `.pdf` | `deepdoc-default@1` | `prose-384@1` | DeepDoc OCR、版面和表格解析 |
| 复杂 PDF 可选 | `.pdf` | `mineru-default@1` | `prose-384@1` | 需要另行配置 MinerU 服务 |
| Word | `.doc` `.docx` | `office-default@1` | `prose-384@1` | RAGFlow Office/Naive 解析 |
| PowerPoint | `.ppt` `.pptx` | `office-default@1` | `visual-192@1` | 按视觉/页面型短块处理 |
| Excel/表格 | `.xls` `.xlsx` `.csv` | `office-default@1` | `table-192@1` | 按行组切块并使用表头侧车 |
| 纯文本 | `.txt` | `office-default@1` | `prose-384@1` | 通用文本解析 |
| Markdown | `.md` | `office-default@1` | `prose-384@1` | 保留标题结构的通用文本解析 |
| HTML | `.html` `.htm` | `office-default@1` | `prose-384@1` | HTML/通用文本解析 |
| JSON | `.json` `.jsonl` `.ldjson` | `json-default@1` | `prose-384@1` | RAGFlow 结构化 JSON Parser；保留嵌套层级后拆分 |
| 图片 | `.png` `.jpg` `.jpeg` `.webp` | `picture-default@1` | `visual-192@1` | Picture Parser、OCR 和必要的视觉描述 |
| 音频 | `.mp3` `.wav` `.m4a` `.aac` `.flac` `.ogg` | `media-default@1` | `media-320@1` | FFmpeg 分段、ASR、时间戳 Markdown |
| 视频 | `.mp4` `.mov` `.mkv` `.webm` `.avi` | `media-default@1` | `media-320@1` | 音轨 ASR、有限抽帧、时间戳 Markdown |

运行环境可能调整白名单或文件上限，因此最终应查询：

```http
GET /v1/capabilities
```

当前不支持把 `.zip`、`.rar`、`.rtf`、`.tiff`、`.bmp` 或可执行文件直接作为文档上传。
`.json`、`.jsonl` 和 `.ldjson` 会自动路由到 `json-default@1`；显式选择其他不兼容
Parser 时仍返回 `422 / RAG-KB-004`。

当前也没有“输入 URL 让服务器下载网页或文件”的接口，`url_ingestion_enabled=false`。

### 6.2 Parser 与 Chunk 的自动选择边界

外部调用方只需理解两条统一规则：

1. **Chunk Profile**：上传时未显式指定 `chunk_profile`，Gateway 会根据扩展名自动
   选择正文、表格、视觉或媒体策略；
2. **Parser Profile**：显式值优先；留空时先保留支持当前扩展名的知识库默认 Parser，
   默认值不兼容时再按 `parser_profiles.example.yaml` 中有序 `selection_rules` 选择。

因此，一个直接调用 API 的上游系统可以：

- 默认不传 Parser/Chunk，让后端处理同一知识库中的 PDF、Office、图片和音视频；
- 需要对照实验或指定 MinerU 时显式传 Profile，或把兼容 Parser 设为知识库默认值。

如果把图片上传到默认 `office-default@1` 的知识库且不覆盖 Parser，系统会自动选择
`picture-default@1`。显式传入不兼容 Parser 或上传没有任何规则支持的扩展名时返回
`RAG-KB-004`，不会静默选错解析器。

### 6.3 当前分块策略

| Chunk Profile | 目标大小 | 重叠 | 主要用途 |
| --- | ---: | ---: | --- |
| `general-256@2` | 256 Token | 10% | 知识库安全默认值 |
| `prose-384@1` | 384 Token | 12% | PDF、Word、TXT、Markdown、HTML |
| `table-192@1` | 192 Token | 5% | Excel 和 CSV |
| `visual-192@1` | 192 Token | 8% | PPT 和图片 OCR |
| `media-320@1` | 320 Token | 12% | 音视频时间戳转写 |

解析完成后 Gateway 还会构建不改变原始 Chunk 的动态 Parent、Section、Summary、
Table Parent、结构化 Metadata 和轻量实体/关系导航。已有文档不会因为 Profile 改变
自动重切；需要调用 Reparse。

### 6.4 Markdown 的特别说明

**明确结论：当前 Gateway 后端可以直接上传、解析、切块、向量化并检索
Markdown `.md` 文件。** 不需要先手工转换为 PDF 或 Word。

Markdown 默认使用：

| 项目 | 当前值 |
| --- | --- |
| 文件扩展名 | `.md` |
| 建议 MIME Type | `text/markdown` |
| Parser Profile | `office-default@1` |
| Chunk Profile | `prose-384@1` |
| 目标 Chunk 大小 | 384 Token |
| Chunk 重叠 | 12% |
| 解析成功标志 | Document `status=READY` |

Markdown 标题、正文、列表和普通表格可以参与结构感知切块、Embedding、
BM25 与混合检索。上传时如果知识库的默认 Parser 不是 Office Parser，建议显式
覆盖：

```bash
curl --fail-with-body \
  -X POST "$RAG_BASE_URL/v1/management/knowledge-bases/$KB_ID/documents" \
  -H "X-Tenant-ID: $RAG_TENANT_ID" \
  -H "X-User-ID: $RAG_USER_ID" \
  -H "Idempotency-Key: upload-markdown-001" \
  -F "file=@./example.md;type=text/markdown" \
  -F "parser_profile=office-default@1" \
  -F "chunk_profile=prose-384@1" \
  -F "auto_parse=true"
```

返回 `202` 后要轮询 Job，并等待文档变为 `READY`；之后可通过
`GET /v1/management/documents/{document_id}/chunks` 检查切块，或直接调用
`POST /v1/retrieval` 验证检索。

Markdown 中引用的远程图片或本地相对图片不会自动作为新的图片文档执行视觉解析；
如果图片内容也要进入知识库，应把图片作为独立文件上传。

普通用户网页是否允许选择 `.md` 是前端产品白名单问题，不代表 Gateway 后端能力。
外部 API 和 3000 开发控制台可以按本节规则上传 `.md`。

## 7. 创建知识库

建议先调用 `/v1/capabilities` 获得当前可用 Profile，然后创建知识库。

下面创建一个适合 Office、TXT、Markdown、HTML 和 CSV 的知识库：

```bash
curl --fail-with-body \
  -X POST "$RAG_BASE_URL/v1/management/knowledge-bases" \
  -H "X-Tenant-ID: $RAG_TENANT_ID" \
  -H "X-User-ID: $RAG_USER_ID" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: create-office-kb-001" \
  -d '{
    "scope_type": "TENANT",
    "external_id": "external-office-kb",
    "name": "外部接入示例知识库",
    "description": "仅使用合成或已批准数据",
    "parser_profile": "office-default@1",
    "chunk_profile": "general-256@2",
    "embedding_profile": "embedding-openai-compatible@3",
    "retrieval_profile": "hybrid-structure-aware@6"
  }'
```

创建成功返回 HTTP `201`。保存响应中的 `data.id`，后续所有上传和检索都使用这个
Gateway UUID，不要使用 RAGFlow 页面里的 Dataset ID。

`scope_type` 只能是 `TENANT` 或 `SYSTEM`。`external_id` 用于上游系统把自己的业务
ID 与 Gateway UUID 关联，在同一资源 Scope 内必须唯一，但它不会改变权限边界。

更新知识库 Profile 只影响未来任务。已有 Chunk 不会自动重建，响应中的
`reindex_required=true` 用于提醒调用方安排 Reparse。

## 8. 上传、解析和轮询文档

### 8.1 自动解析上传

上传使用 `multipart/form-data`，一次请求只接受一个文件：

```bash
curl --fail-with-body \
  -X POST "$RAG_BASE_URL/v1/management/knowledge-bases/$KB_ID/documents" \
  -H "X-Tenant-ID: $RAG_TENANT_ID" \
  -H "X-User-ID: $RAG_USER_ID" \
  -H "Idempotency-Key: upload-markdown-001" \
  -F "file=@./example.md;type=text/markdown" \
  -F "external_id=manual-example-md" \
  -F 'metadata={"source":"external-api-demo","language":"zh-CN","version":"1"}' \
  -F "auto_parse=true"
```

可用 multipart 字段：

| 字段 | 必需 | 说明 |
| --- | --- | --- |
| `file` | 是 | 单个文件 |
| `external_id` | 否 | 当前知识库内唯一的上游文档 ID |
| `metadata` | 否 | JSON Object 字符串 |
| `parser_profile` | 否 | 覆盖知识库默认 Parser |
| `chunk_profile` | 否 | 覆盖自动文件族 Chunk Profile |
| `auto_parse` | 否 | 默认 `true`；上传结束后自动解析 |

返回 HTTP `202`，包含：

```json
{
  "request_id": "...",
  "data": {
    "document_id": "5393cbaf-f147-4229-8be1-8bed4a79eaea",
    "job_id": "9dc0df13-82b1-4cb0-9715-81329e3ea191",
    "status": "RECEIVED"
  }
}
```

同 Scope、同知识库、相同 `content_sha256` 的未删除文件只保存一次。重复上传返回
`RAG-DOC-012` 和 `existing_document_id`，调用方应复用已有文档，不要更换文件名后
反复重传。

### 8.2 Metadata 约束

上传 Metadata 最大 16 KiB：

- 上传时键名只能包含小写字母、数字和下划线，最长 64 字符；
- 如果该字段之后要用于 `metadata_filter`，键名还必须以小写字母开头；
- 值可以是字符串、整数、浮点数、布尔值或字符串数组；
- 不接受嵌套 Object、任意数组或 `null`；
- 系统解析完成后可能增加 `title`、`language`、文件类型、日期、版本、技术实体以及
  `lazytt_*` 内部处理字段。

业务字段建议使用稳定名称，例如：

```json
{
  "source": "product-manual",
  "language": "zh-CN",
  "product": "lazytt",
  "version": "2026.08",
  "department": "engineering"
}
```

### 8.3 不自动解析

如果只想先上传：

```text
auto_parse=false
```

等待上传 Job `SUCCEEDED`、文档状态为 `UPLOADED` 后，再调用：

```bash
curl --fail-with-body \
  -X POST "$RAG_BASE_URL/v1/management/documents/$DOCUMENT_ID/parse" \
  -H "X-Tenant-ID: $RAG_TENANT_ID" \
  -H "X-User-ID: $RAG_USER_ID" \
  -H "Idempotency-Key: parse-document-001"
```

### 8.4 查询 Job

```bash
curl --fail-with-body \
  -H "X-Tenant-ID: $RAG_TENANT_ID" \
  -H "X-User-ID: $RAG_USER_ID" \
  "$RAG_BASE_URL/v1/management/jobs/$JOB_ID"
```

推荐每 2～5 秒轮询一次，不要毫秒级高频请求。Job `SUCCEEDED` 后再查询文档：

```bash
curl --fail-with-body \
  -H "X-Tenant-ID: $RAG_TENANT_ID" \
  -H "X-User-ID: $RAG_USER_ID" \
  "$RAG_BASE_URL/v1/management/documents/$DOCUMENT_ID"
```

文档最终可检索状态是：

```text
READY
```

文档状态可能包括：

```text
RECEIVED → UPLOADING → UPLOADED → QUEUED → PARSING → INDEXING → READY
```

失败时为 `FAILED`，删除流程可能出现 `DELETING`、`DELETE_FAILED` 或 `DELETED`。

### 8.5 Reparse

使用新的 Parser/Chunk 重建已有文档：

```bash
curl --fail-with-body \
  -X POST "$RAG_BASE_URL/v1/management/documents/$DOCUMENT_ID/reparse" \
  -H "X-Tenant-ID: $RAG_TENANT_ID" \
  -H "X-User-ID: $RAG_USER_ID" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: reparse-document-001" \
  -d '{
    "parser_profile": "office-default@1",
    "chunk_profile": "prose-384@1"
  }'
```

Reparse 适合对同一源文件改变 Parser/Chunk Profile；它会原地替换旧 Chunk。文件正文
发生变化时优先使用下面的安全版本接口。

### 8.6 上传文件新版本与回滚

```bash
curl --fail-with-body \
  -X POST "$RAG_BASE_URL/v1/management/documents/$DOCUMENT_ID/versions" \
  -H "X-Tenant-ID: $RAG_TENANT_ID" \
  -H "X-User-ID: $RAG_USER_ID" \
  -H "Idempotency-Key: update-document-v2-001" \
  -F "file=@./example-v2.md;type=text/markdown" \
  -F 'metadata={"version":"2"}' \
  -F "mode=AUTO"
```

保存响应中的 `version_id` 和 `job_id`。Job 成功后查询：

```bash
curl --fail-with-body \
  -H "X-Tenant-ID: $RAG_TENANT_ID" \
  -H "X-User-ID: $RAG_USER_ID" \
  "$RAG_BASE_URL/v1/management/documents/$DOCUMENT_ID/versions?page=1&page_size=20"
```

回滚到一个 `RETIRED` 版本：

```bash
curl --fail-with-body \
  -X POST "$RAG_BASE_URL/v1/management/documents/$DOCUMENT_ID/versions/$VERSION_ID/rollback" \
  -H "X-Tenant-ID: $RAG_TENANT_ID" \
  -H "X-User-ID: $RAG_USER_ID" \
  -H "Idempotency-Key: rollback-document-v1-001"
```

上传、发布和回滚都不改变 `$DOCUMENT_ID`。应轮询返回的 Job，不能只根据 HTTP 202
判断文件已经切换。

## 9. 纯检索

### 9.1 最小请求

```bash
curl --fail-with-body \
  -X POST "$RAG_BASE_URL/v1/retrieval" \
  -H "X-Tenant-ID: $RAG_TENANT_ID" \
  -H "X-User-ID: $RAG_USER_ID" \
  -H "Content-Type: application/json" \
  -d "{
    \"knowledge_base_ids\": [],
    \"query\": \"系统支持哪些文件格式？\",
    \"retrieval_profile\": \"hybrid-structure-aware@6\",
    \"options\": {
      \"page_size\": 10,
      \"include_content\": true,
      \"include_positions\": true
    }
  }"
```

请求支持：

| 字段 | 约束 | 说明 |
| --- | --- | --- |
| `knowledge_base_ids` | 可选，最多 20 个唯一 UUID | 默认空数组，自动检索当前租户全部 READY 库和全部系统 READY 库；保留显式缩小范围能力 |
| `document_ids` | 最多 100 个唯一 UUID | 可选，只检索指定文档 |
| `query` | 1～8000 字符 | 用户问题 |
| `metadata_filter` | 最多 20 个条件 | 可选 Metadata 过滤 |
| `retrieval_profile` | 版本化 Profile | 默认 `hybrid-structure-aware@6` |
| `options.page_size` | 1～50 | 返回候选数量 |
| `include_content` | 布尔值 | 是否返回 Chunk 正文 |
| `include_positions` | 布尔值 | 是否返回页码和坐标 |

### 9.2 Metadata Filter

```json
{
  "logic": "and",
  "conditions": [
    {"field": "language", "operator": "eq", "value": "zh-CN"},
    {"field": "version", "operator": "starts_with", "value": "2026"}
  ]
}
```

支持的运算符：

```text
eq
ne
contains
not_contains
starts_with
empty
not_empty
gt
gte
lt
lte
```

使用 Metadata Filter 时，为了保持过滤边界正确，部分 Gateway 侧车路线会被绕过，
响应 `warnings` 可能包含 `sidecar_index_bypassed_metadata_filter`。这不是 500 错误。

### 9.3 检索响应

主要字段：

- `original_query`：调用方原始问题；
- `effective_query`：本次主要生效查询；
- `query_plan.detected_language`：检测到的中英文；
- `query_plan.variants`：中英文查询变体；
- `query_plan.retrieval_routes`：实际走过的向量、BM25 和侧车路线；
- `query_plan.query_type`：`fact/list/comparison/summary/multi_hop`；
- `retrieval_top_k/rerank_top_n/context_top_n`：本次动态预算；
- `candidates`：候选 Chunk；
- `warnings`：Rerank、缓存、侧车或模型降级信息。

每个候选包含：

```json
{
  "chunk_id": "engine-chunk-id",
  "knowledge_base_id": "...",
  "document_id": "...",
  "document_name": "example.md",
  "heading_path": [],
  "content": "候选正文",
  "positions": [{"page": 1, "bbox": null}],
  "scores": {
    "score": 0.0315,
    "vector_score": 0.7896,
    "term_score": 0.1579,
    "rerank_score": null
  },
  "metadata": {}
}
```

`score` 可能是 RRF 综合排序分，不是百分制，也不应简单解释为“31.5% 相似度”。
`vector_score`、`term_score` 和真实 `rerank_score` 来自不同阶段，不能互相当作同一个
量纲相加。外部应用应以服务返回顺序为最终排名，以分项分数用于解释和调试。

没有证据时，纯检索仍返回 HTTP `200`，但 `candidates=[]`。

## 10. 非流式问答

```bash
curl --fail-with-body \
  -X POST "$RAG_BASE_URL/v1/answers" \
  -H "X-Tenant-ID: $RAG_TENANT_ID" \
  -H "X-User-ID: $RAG_USER_ID" \
  -H "Content-Type: application/json" \
  -d "{
    \"knowledge_base_ids\": [],
    \"query\": \"请根据上传文档说明系统支持哪些文件格式。\",
    \"messages\": [],
    \"retrieval_profile\": \"hybrid-structure-aware@6\",
    \"generation_profile\": \"answer-openai-compatible@4\",
    \"response_language\": \"zh-CN\"
  }"
```

问答请求支持 1～20 个知识库、可选 Metadata Filter、最多 12 条有限历史消息，以及
`zh-CN` 或 `en-US` 响应语言。普通用户端当前不会把历史会话传给下一个问题，但其他
经过授权的外部调用方可以按 API 契约传入 `messages`。

成功响应的重要字段：

- `answer`：包含 `[C1]`、`[C2]` 等标签的答案；
- `outcome`：`success`、`no_evidence`、`refused` 或 `cancelled`；
- `response_language`：实际目标语言；
- `original_query`、`effective_query`、`query_rewritten`；
- `query_plan`：中英文查询、路线和动态预算；
- `citations`：已验证引用；
- `warnings`：明确的降级信息；
- `usage`：模型 Token 用量，供应商提供时返回；
- `meta.cache_type`：`none`、`exact` 或 `semantic`。

无证据不是系统异常。服务返回 HTTP `200` 和 `outcome=no_evidence`，而不是伪造答案。
模型输出无法通过引用校验时可能返回 `outcome=refused`。

## 11. SSE 流式问答

### 11.1 请求

```bash
curl -N --fail-with-body \
  -X POST "$RAG_BASE_URL/v1/answers/stream" \
  -H "X-Tenant-ID: $RAG_TENANT_ID" \
  -H "X-User-ID: $RAG_USER_ID" \
  -H "Accept: text/event-stream" \
  -H "Content-Type: application/json" \
  -d "{
    \"knowledge_base_ids\": [],
    \"query\": \"总结文档的主要内容。\",
    \"messages\": [],
    \"retrieval_profile\": \"hybrid-structure-aware@6\",
    \"generation_profile\": \"answer-openai-compatible@4\",
    \"response_language\": \"zh-CN\"
  }"
```

代理层必须关闭响应缓冲，并保持 `text/event-stream`。Gateway 当前设置：

- SSE 心跳默认 15 秒；
- 问答总 Deadline 默认 150 秒；
- LLM 首 Token 超时默认 20 秒。

### 11.2 事件顺序

客户端应按事件名处理，不要假设每次都有全部事件：

| 事件 | 含义 |
| --- | --- |
| `accepted` | 请求已接受 |
| `retrieval.started` | 开始检索 |
| `retrieval.completed` | 检索完成，携带数量、改写和 QueryPlan |
| `generation.started` | 开始模型生成；缓存命中时 `source=cache` |
| `answer.delta` | 一段增量答案文本 |
| `citation` | 一个最终使用且验证通过的引用 |
| `usage` | Token 用量 |
| `heartbeat` | 暂无业务内容但连接仍然存活 |
| `completed` | 正常结束，携带 outcome、warning、cache_type 和耗时 |
| `error` | 流已经建立后的稳定错误；发送后连接关闭 |

示例：

```text
event: answer.delta
id: 5
data: {"request_id":"...","sequence":5,"timestamp":"...","delta":"系统支持"}

event: completed
id: 12
data: {"request_id":"...","sequence":12,"timestamp":"...","outcome":"success","warnings":[],"cache_type":"none","timings":{"total_ms":8532.1}}
```

客户端只有收到 `completed` 才能把回答标记为完整。收到 `error` 或网络断开时，不应把
已经显示的部分 Token 当作经过完整引用校验的最终答案。

## 12. 引用、请求记录和反馈

每条引用包含：

- `citation_id` 和显示标签 `C1/C2/...`；
- 知识库、文档 UUID 和文档名；
- 标题路径；
- 起止页码和可选坐标框；
- `quote` 原文摘录；
- 排序或 Rerank 相关分数。

回答结束后可以再次查询引用：

```bash
curl --fail-with-body \
  -H "X-Tenant-ID: $RAG_TENANT_ID" \
  -H "X-User-ID: $RAG_USER_ID" \
  "$RAG_BASE_URL/v1/requests/$REQUEST_ID/citations"
```

读取单条引用：

```bash
curl --fail-with-body \
  -H "X-Tenant-ID: $RAG_TENANT_ID" \
  -H "X-User-ID: $RAG_USER_ID" \
  "$RAG_BASE_URL/v1/citations/$CITATION_ID"
```

提交反馈：

```bash
curl --fail-with-body \
  -X POST "$RAG_BASE_URL/v1/feedback" \
  -H "X-Tenant-ID: $RAG_TENANT_ID" \
  -H "X-User-ID: $RAG_USER_ID" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: feedback-$REQUEST_ID-001" \
  -d "{
    \"request_id\": \"$REQUEST_ID\",
    \"rating\": 1,
    \"feedback_type\": \"helpful\",
    \"comment\": \"答案和引用一致\"
  }"
```

请求审计接口只返回经过控制的查询、结果和警告，不返回完整 Prompt、模型私密配置或
未受控内部上下文。

## 13. 当前检索和生成能力说明

默认 `hybrid-structure-aware@6` 会根据问题类型动态选择预算，并在侧车向量导航取 Top-N 前应用 0.40 余弦相似度门槛：

| 问题类型 | Retrieval Top-K | Rerank Top-N | Context Top-N |
| --- | ---: | ---: | ---: |
| 事实 `fact` | 30 | 8 | 5 |
| 列举 `list` | 50 | 10 | 6 |
| 对比 `comparison` | 60 | 12 | 7 |
| 总结 `summary` | 80 | 15 | 8 |
| 多跳 `multi_hop` | 80 | 16 | 8 |

主要处理流程：

```text
原问题
  → 中英文检测与 Query Rewrite
  → 根据知识库语言生成向量/BM25查询路线
  → 并行召回
  → 加权 RRF
  → 摘要/章节/Parent/Table Parent/实体关系侧车导航
  → 精确候选去重、语义近重复删除、相邻 Chunk 合并
  → 可选真实 Cross-Encoder Rerank
  → Context 预算
  → LLM 生成
  → 引用校验
  → JSON 或 SSE 返回
```

Rerank 关闭或暂时失败时，系统会保留向量、BM25 和 RRF 结果，并在 `warnings` 中明确
说明降级，不应因为 Rerank 不可用直接返回 500。

精确缓存命中时 `cache_type=exact`，语义缓存命中时为 `semantic`，未命中为 `none`。
缓存边界包含 Scope、知识库 Revision、Retrieval/Profile、模型、语言和历史摘要；知识
库内容变化后旧缓存不会跨 Revision 错用。

## 14. 常见错误和调用方处理

| 场景 | 常见错误码 | 建议处理 |
| --- | --- | --- |
| Tenant/User 任一缺失、过长或含控制字符 | `RAG-COMMON-001` | 修正两个 Header，不要把 Token 填入其中 |
| 文件格式不支持 | `RAG-DOC-002` | 查询 capabilities，转换为受支持格式 |
| 文件损坏、空文件或文件名非法 | `RAG-DOC-003` | 修复源文件，不重试相同坏文件 |
| 完全重复文件 | `RAG-DOC-012` | 使用 `existing_document_id` 定位已有文件 |
| 知识库 Parser 不支持文件 | `RAG-KB-004` | 上传时传正确 Parser，或路由到正确知识库 |
| 文档正在处理 | `RAG-DOC-005` | 查询当前 Job，等待结束 |
| 文档解析超时 | `RAG-DOC-008` | 检查 RAGFlow 日志和资源，再有限 Retry |
| 知识库非空 | `RAG-KB-005` | 先删文档，或明确使用 `force=true` |
| 检索不可用/超时 | `RAG-RETRIEVAL-002/003` | 仅在 `retryable=true` 时有限重试 |
| LLM 不可用/超时 | `RAG-LLM-001/002` | 保留 request_id，有限重试或提示稍后再试 |
| RAGFlow 协议/连接失败 | `RAG-RF-*` | 检查 `/health/ready` 和服务日志 |
| 服务未就绪 | `RAG-COMMON-012` | 等待依赖恢复，不继续提交批量任务 |

HTTP `429` 如果由上游代理或模型服务透传，应遵守 `Retry-After`。客户端重试和服务端
内部重试都有限制，不能叠加成无限重试。

## 15. 外部调用方不应假设的能力

当前公开 API 没有提供以下能力：

- 生产用户注册、登录、Token 验证、角色或组织成员管理；
- 一次 multipart 请求批量上传多个文件；
- 通过 URL 自动抓取网页或下载远程文件；
- 直接上传 JSON/JSONL、压缩包或任意二进制格式；
- 在线编辑 Chunk 正文或直接修改向量；
- 原始文件下载接口；
- 原地替换文档内容；更新内容应删除旧文档后上传新文件，或按业务版本并存；
- 直接管理 RAGFlow 账号、API Key、Provider 或内部 Dataset；
- 完整 GraphRAG、跨文档实体消歧、隐含关系推理或外部图数据库查询；
- 生产级身份系统、TLS、Secret Manager、高可用和多机灾备。

外部调用方也不应依赖 RAGFlow 内部 ID、数据库表、Elasticsearch 索引名或 Redis Key。
只有 Gateway 返回的 UUID、稳定错误码和版本化 Profile 属于公开契约。

## 16. 推荐的接入顺序

一个新的上游系统建议按以下顺序接入：

1. 调用 `/health/live` 和 `/health/ready`；
2. 在受控联调环境为调用方分配稳定 Tenant/User；
3. 调用 `/v1/version` 和 `/v1/capabilities`；
4. 为不同 Parser 文件族创建知识库，保存 Gateway UUID；
5. 使用唯一 `Idempotency-Key` 上传最小非业务文件；
6. 轮询 Job 和 Document，直到 `READY`；
7. 调用 `/v1/retrieval` 验证召回和 Scope；
8. 调用 `/v1/answers` 验证完整 JSON；
9. 调用 `/v1/answers/stream` 验证 SSE、心跳、取消和断线处理；
10. 查询引用并提交反馈；
11. 分别更换 Tenant ID、User ID 做跨范围反向测试；
12. 验证重复请求、重复文件、下游超时和可恢复失败；
13. 生产发布前再完成 HTTPS、身份系统、Secret、限流、监控、备份和容量基线。

## 17. 最小端到端流程

```text
调用方提供稳定 X-Tenant-ID + X-User-ID
        ↓
POST /v1/management/knowledge-bases
        ↓
POST /v1/management/knowledge-bases/{kb_id}/documents
        ↓
GET /v1/management/jobs/{job_id}
        ↓
GET /v1/management/documents/{document_id} 直到 READY
        ↓
POST /v1/retrieval
        ↓
POST /v1/answers/stream
        ↓
读取 answer.delta、citation、completed
        ↓
GET /v1/requests/{request_id}/citations
        ↓
POST /v1/feedback
```

## 18. 维护和兼容性要求

- 客户端在启动或定期刷新时读取 `/v1/capabilities`；
- Profile 必须使用完整版本引用，例如 `hybrid-structure-aware@6`；
- 不把 Profile 名称、文件白名单、模型名或最大文件大小散落硬编码在多个前端；
- 保存 Gateway `request_id`、资源 UUID 和 Job ID，不保存或展示内部 Secret；
- 处理未知 `warnings` 时保持前向兼容，不应因为出现新告警字段而解析失败；
- JSON 客户端应拒绝依赖未声明字段，SSE 客户端应忽略暂时不认识的事件；
- 调用方升级前对比 `/v1/version` 与 OpenAPI 契约；
- 生产系统必须通过 Gateway 接入，不能绕过 Scope 直接访问 RAGFlow。

更完整的部署、内部架构和检索算法说明见：

- [本地部署与使用手册](USER_MANUAL.md)
- [代码结构详解](CODEBASE_STRUCTURE.md)
- [双语检索与加权 RRF](MULTILINGUAL_RETRIEVAL.md)
- [结构感知切块](STRUCTURE_AWARE_CHUNKING.md)
- [项目状态与限制](PROJECT_STATUS_AND_ROADMAP.md)
