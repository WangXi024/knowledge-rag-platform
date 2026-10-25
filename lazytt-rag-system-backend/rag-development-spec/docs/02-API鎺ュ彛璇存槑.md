# RAG API 接口说明

> 机器可读契约：`../openapi/rag-api-v1.yaml`  
> 本文负责语义和流程；字段冲突以 OpenAPI 为准。

## 1. 通用约定

### 1.1 地址和临时身份

- Base URL：`https://<rag-host>/v1`。
- 健康接口位于 `/health`，不使用 `/v1`。
- 健康、版本和能力接口无需身份 Header。
- 其他业务接口在当前联调阶段必须同时提交 `X-Tenant-ID` 与 `X-User-ID`。Gateway
  把请求 Tenant ID 与 User ID 组合后计算内部
  `security_scope_id`，并用它隔离知识库、文档、任务、查询、引用、反馈、幂等记录和缓存。
- 本项目不提供用户注册、登录、密码、Session、角色、Workspace 成员或复杂 ACL。
- 两个 Header 只是临时隔离输入，不验证调用者是否属于该租户或拥有该身份；因此该模式只允许在
  受控开发联调网络使用，不能当作生产认证方案。
- 请求 Query 或 Body 中的 `user_id`、`tenant_id`、`rag_scope_id`、
  `security_scope_id` 均不能覆盖服务端构造的 `SecurityContext`。

### 1.2 请求头

| 请求头 | 必须 | 说明 |
|---|---|---|
| `X-Tenant-ID` | 业务接口必填 | 1-128 字符的稳定上游 Tenant ID；不是认证凭据 |
| `X-User-ID` | 业务接口必填 | 1-128 字符的稳定上游 User ID；不是认证凭据 |
| `X-Request-ID` | 否 | UUID；未传由服务器生成 |
| `Idempotency-Key` | 创建、上传、触发任务、删除和反馈必传 | 1-128 个可打印字符，默认保留 24 小时 |
| `Accept` | 否 | 流式接口固定 `text/event-stream` |

### 1.3 成功响应

```json
{
  "request_id": "19d1f2e7-99cd-43b9-a25f-0ee8eec5706d",
  "data": {},
  "meta": {
    "page": 1,
    "page_size": 20,
    "total": 1
  }
}
```

### 1.4 错误响应

```json
{
  "request_id": "19d1f2e7-99cd-43b9-a25f-0ee8eec5706d",
  "error": {
    "code": "RAG-DOC-004",
    "message": "文档解析失败",
    "retryable": true,
    "details": [
      {"field": "document_id", "reason": "engine_parse_failed"}
    ]
  }
}
```

`message` 面向调用方，不包含内部堆栈和 RAGFlow 原文。错误码见 `04-错误码说明.md`。

### 1.5 幂等

以下操作必须传 `Idempotency-Key`：创建知识库、上传文档、解析/Reparse/Retry、删除、提交反馈。幂等键的实际命名空间是 `(security_scope_id, method, path, idempotency_key)`：同一 Scope 下相同 Key 和相同请求返回首次结果；同一 Scope 下相同 Key 和不同请求返回 HTTP 409；不同 Scope 可以使用相同 Key，且结果互不可见。

文件请求 Hash 由 Method、Path、非文件字段、文件 SHA-256 组成。幂等记录不保存文件正文。

## 2. 能力和健康

### 2.1 `GET /v1/capabilities`

返回支持的文件类型、Parser Profile、Chunk Profile、Retrieval Profile、最大文件大小和功能开关。上游应用应使用此接口控制前端选项，不得硬编码 RAGFlow 解析器列表。

### 2.2 `GET /v1/version`

返回 RAG API 版本、构建 Commit、RAGFlow 契约版本；不得返回密钥或完整依赖配置。

### 2.3 `GET /health/live`

只表示 Gateway 进程存活，不探测下游，适合容器 liveness。

### 2.4 `GET /health/ready`

探测应用数据库、Redis 和 RAGFlow Health。LLM 可配置为必需或降级状态。依赖不可用时返回 503 和每项依赖的安全状态。

## 3. 知识库接口

### 3.1 创建知识库

`POST /v1/knowledge-bases`

```json
{
  "external_id": "product-docs",
  "name": "产品文档",
  "description": "产品手册和操作说明",
  "parser_profile": "deepdoc-default@1",
  "chunk_profile": "general-256@2",
  "retrieval_profile": "hybrid-structure-aware@6",
  "embedding_profile": "embedding-openai-compatible@3"
}
```

成功返回 201。`external_id` 可选，仅用于上游关联；项目不解释其业务含义，但只要求在当前 `security_scope_id` 内唯一。Embedding Profile 在已有 Chunk 后不可直接修改，必须新建知识库或执行受控全量重建。

`general-256@2` 是新建知识库必须持有的安全兜底：目标块大小为 256 Token，项目层的
`overlap_ratio: 0.10` 会在调用 RAGFlow 时转换为 `overlapped_percent: 10`。上传时若
调用方没有显式覆盖，Gateway 会按文件族选择 `prose-384@1`、`table-192@1`、
`visual-192@1` 或 `media-320@1`，并在解析前通过官方文档接口真正应用。旧 Profile
继续保留给历史知识库使用；仅修改 Profile 不会自动重建已有 Chunk。

知识库的 Parser 是文档自动选择时优先尝试的默认值：若支持当前扩展名则保留；否则按
`parser_profiles.example.yaml` 的有序 `selection_rules` 自动选择 PDF、Office、图片或
媒体 Parser。上传字段 `parser_profile` 可显式覆盖，但仍必须兼容文件。Embedding
Profile 与知识库向量空间绑定，不支持按文档混用。

`hybrid-structure-aware@6` 是当前新请求默认 Retrieval Profile：按问题类型动态选择
Top-K，并允许 Document Summary、Section、标题/长度动态 Parent、Table Parent、
实体/显式关系侧车路线参与导航；向量侧车节点必须先达到 0.40 余弦相似度。侧车命中不会直接返回合成节点，而是逐级回读
RAGFlow 原始 Chunk；`hybrid-default@1/@2`、`hybrid-adaptive@3` 与
`hybrid-multigranular@4`、`hybrid-structure-aware@5` 保留兼容和回滚。

### 3.2 查询和修改

- `GET /v1/knowledge-bases`：分页和名称搜索。
- `GET /v1/knowledge-bases/{id}`：返回配置、状态、文档数和 revision。
- `PATCH /v1/knowledge-bases/{id}`：可修改名称、描述和未来任务使用的 Profile。

修改 Parser/Chunk Profile 不自动重建已有文档。响应返回 `reindex_required=true` 作为提示。

所有列表只返回当前 Scope 的资源。详情、修改和删除操作遇到其他 Scope 的 ID 时统一返回 404，避免通过 403 差异探测资源是否存在。

### 3.3 删除

`DELETE /v1/knowledge-bases/{id}` 返回 202 和 Job。删除默认要求知识库为空；请求 `force=true` 时先删除其文档再删除 Dataset。删除操作可重复执行。

## 4. 文档接口

上传接口接受 Profile 白名单内的 PDF、Office、JSON/JSONL/LDJSON、图片和常见音视频
格式。JSON 文件族使用 `json-default@1` 调用 RAGFlow 内置结构化 Parser。音视频不是
直接交给 RAGFlow：Worker 先用 FFmpeg/ffprobe 校验、分段和有限抽帧，再通过受控
OpenAI-compatible ASR/视觉服务生成带时间戳 Markdown，最后使用 `media-default@1`
进入 RAGFlow 通用切块与索引。外部 `Document` 的文件名、大小、MIME 和 SHA-256
始终代表用户上传的原媒体，派生文件仅作为 Job 恢复点，不改变资源身份。

### 4.1 上传

`POST /v1/knowledge-bases/{knowledge_base_id}/documents`

使用 `multipart/form-data`：

| 字段 | 必须 | 说明 |
|---|---|---|
| `file` | 是 | 单个文件；批量由上游并发调用或后续批量接口完成 |
| `external_id` | 否 | 上游关联标识 |
| `metadata` | 否 | JSON 字符串，白名单业务检索元数据 |
| `parser_profile` | 否 | 显式解析 Profile；留空时先尝试兼容默认值，再按配置规则选择 |
| `chunk_profile` | 否 | 显式指定文档 Chunk；留空时按文件族自动选择，未知格式回退知识库默认值 |
| `auto_parse` | 否 | 默认 `true` |

响应 202：

```json
{
  "request_id": "...",
  "data": {
    "document_id": "8fc0f1a4-5dd5-4c94-a9d2-1dd8def3f265",
    "job_id": "d93d89d7-40ef-4a45-b5fc-a46e9427dbcd",
    "status": "RECEIVED"
  }
}
```

只有当文件已完整写入共享暂存卷、完成大小/Hash 校验、原子发布为 `.ready`，
并且 Document/UPLOAD Job 已提交到应用 PostgreSQL 后才能返回 202。返回 202
不表示已经上传到 RAGFlow；上游通过 `job_id` 查询后续状态。暂存卷不可写或
容量不足时返回 `RAG-DOC-010`，不得创建一个 Worker 永远无法读取的 Job。

### 4.2 文档查询

- `GET /v1/knowledge-bases/{id}/documents`：按状态、文件名和 external_id 过滤。
- `GET /v1/documents/{document_id}`：返回处理状态、进度、Chunk 数和安全错误摘要。
- `GET /v1/documents/{document_id}/chunks`：分页返回 Chunk 和来源位置，用于验收与调试。

### 4.3 解析、重新解析和重试

- `POST /v1/documents/{id}/parse`：只允许 `UPLOADED` 或可恢复状态。
- `POST /v1/documents/{id}/reparse`：清除旧 Chunk 后使用指定 Profile 重建。
- `POST /v1/documents/{id}/retry`：对 `FAILED` 文档沿用失败 Job 的输入并增加
  attempt；自动重试已经处于活动状态时幂等返回当前 Job。自动重试额度耗尽后，
  用户显式重试会获得一轮新的有限重试额度。

三个接口均返回 202。Parse/Reparse 重复触发同一执行中文档返回 409；Retry
在已有自动重试时返回当前 Job，避免前端状态刷新与后台重试之间的竞态。

### 4.4 安全版本更新与回滚

- `POST /v1/documents/{id}/versions`：使用 multipart 上传候选文件，返回版本和
  `UPDATE_DOCUMENT` Job；候选成功前当前文档继续 READY；
- `GET /v1/documents/{id}/versions`：分页查看不可变版本、状态、Hash、Profile 和
  解析统计，不暴露 RAGFlow 物理 ID；
- `POST /v1/documents/{id}/versions/{version_id}/rollback`：把可用的 `RETIRED`
  版本作为目标创建回滚 Job。

版本发布在独立 RAGFlow 物理文档中完成解析和索引，随后切换 Chunk 可用性并在应用
PostgreSQL 中原子更新活动版本、逻辑 Document 和 Knowledge Base Revision。当前
`AUTO` 明确协商为 `SAFE_REPLACE`，不把整版重建描述成真正 Chunk 增量更新。候选
失败不得把当前活动文档标记为失败。

### 4.5 删除

`DELETE /v1/documents/{id}` 返回 202。删除 Job 会清理逻辑文档的全部 RAGFlow 物理
版本和索引；成功后资源详情返回 404，对应 Job 仍可查询。上游如需保留业务记录，应
在自身系统保存。

## 5. Job 接口

### 5.1 查询

`GET /v1/jobs/{job_id}` 返回：类型、状态、进度、attempt、创建/开始/结束时间、错误码和关联资源 ID。

### 5.2 取消

`POST /v1/jobs/{job_id}/cancel` 只表示请求取消。对于 RAGFlow 已在执行的任务，Worker 需要调用对应停止接口并再次核对状态。无法中断时返回 202 且状态保持 `CANCEL_REQUESTED`，直到核对完成。

## 6. 纯检索接口

`POST /v1/retrieval`

纯检索仅使用认证上下文中 `tenant_id` 对应的租户知识库（`TENANT`），暂不检索系统共享知识库（`SYSTEM`）。不传 `knowledge_base_ids` 或传空数组时，检索当前租户全部 READY 知识库；显式传入系统库、其他租户库或不存在的 ID，统一返回 404。此限制不改变问答接口的知识库范围。

```json
{
  "knowledge_base_ids": ["4b5d82ac-b50a-4757-b71d-f6f17e1a30fa"],
  "query": "如何切换 MinerU 解析器？",
  "metadata_filter": {
    "logic": "and",
    "conditions": [
      {"field": "product", "operator": "eq", "value": "ragflow"}
    ]
  },
  "retrieval_profile": "hybrid-structure-aware@6",
  "options": {
    "page_size": 10,
    "include_content": true,
    "include_positions": true
  }
}
```

上游传入的参数只能在 Profile 允许范围内覆盖；不得允许任意 `top_k` 或阈值导致资源滥用。Gateway 必须先确认所有 `knowledge_base_ids` 均属于当前 Scope 且可用；任一 ID 不存在或越界时返回 404，并且不得调用缓存、RAGFlow、Embedding、Reranker 或 LLM。响应候选包含稳定知识库、文档和 Chunk ID、正文、页码/坐标、分数和白名单元数据。

## 7. 问答接口

### 7.1 非流式

`POST /v1/answers`

```json
{
  "knowledge_base_ids": ["4b5d82ac-b50a-4757-b71d-f6f17e1a30fa"],
  "query": "RAGFlow 如何切换 MinerU？",
  "messages": [
    {"role": "user", "content": "我们正在使用 RAGFlow。"},
    {"role": "assistant", "content": "请问具体想了解哪部分？"}
  ],
  "retrieval_profile": "hybrid-structure-aware@6",
  "generation_profile": "answer-openai-compatible@4",
  "response_language": "zh-CN"
}
```

成功响应：

```json
{
  "request_id": "...",
  "data": {
    "answer": "可以在知识库配置中选择 MinerU。[C1]",
    "outcome": "success",
    "response_language": "zh-CN",
    "original_query": "RAGFlow 如何切换 MinerU？",
    "effective_query": "How can RAGFlow switch to MinerU?",
    "query_rewritten": true,
    "citations": [
      {
        "citation_id": "...",
        "label": "C1",
        "document_id": "...",
        "document_name": "解析器说明.pdf",
        "page_start": 6,
        "quote": "在 PDF parser 中选择 MinerU。"
      }
    ],
    "warnings": [],
    "usage": {
      "prompt_tokens": 1800,
      "completion_tokens": 150,
      "total_tokens": 1950
    },
    "meta": {
      "cache_type": "none"
    }
  }
}
```

`outcome`：`success`、`no_evidence`、`refused`、`cancelled`。系统性失败使用非 2xx 错误，而不是 `outcome=error`。

`response_language` 目前只接受 `zh-CN` 和 `en-US`。Gateway 会把该值映射为
受信任的语言名称并注入答案生成和引用修复 Prompt；它只控制最终回答语言，不会关闭
中文原问题向量召回、英文改写问题向量召回或英文 BM25 召回。不同响应语言使用不同的
缓存边界，不能互相命中。

`meta.cache_type`：`none`、`exact` 或 `semantic`。缓存只允许在同一 `security_scope_id` 内命中，命中后仍需验证知识库 revision 和引用资源有效性。

### 7.2 流式

`POST /v1/answers/stream` 请求体与非流式相同，响应 `text/event-stream`。事件数据统一包含 `request_id`、`sequence`、`timestamp` 和事件特有字段。

完整事件见 `contracts/sse-events.json`。结束事件示例：

```text
event: completed
id: 12
data: {"request_id":"...","sequence":12,"outcome":"success"}
```

### 7.3 查询和取消

- `GET /v1/requests/{request_id}`：查询已结束请求的结果摘要和状态。
- `POST /v1/requests/{request_id}/cancel`：设置 Redis 取消标记并取消当前 LLM HTTP 请求。

Request ID 不是访问凭据。查询和取消前必须同时匹配当前 `security_scope_id`；其他 Scope 的 Request 返回 404。

## 8. 引用和反馈

- `GET /v1/requests/{request_id}/citations`：列出本次回答的引用。
- `GET /v1/citations/{citation_id}`：返回引用定位和摘录，不返回整个未裁剪文档。
- `POST /v1/feedback`：提交 rating、错误类型、评论和可选修正答案。

Feedback 必须关联已存在 Request，不能自动改变 Prompt、配置或索引，只进入评测样本候选池。

Citation 和 Feedback 的读取、创建同样受当前 `security_scope_id` 约束。引用定位前必须重新确认其知识库和文档仍属于当前 Scope。

## 9. SSE 事件定义

| 事件 | 说明 |
|---|---|
| `accepted` | 请求已接受，返回 request_id |
| `retrieval.started` | 开始检索 |
| `retrieval.completed` | 返回候选/Context 数量，不返回敏感全文 |
| `generation.started` | 模型调用开始，包含 `model_profile` 和 `response_language` |
| `answer.delta` | 增量文本 |
| `citation` | 已验证引用 |
| `usage` | Token 和模型别名 |
| `completed` | 最终 outcome 和 warnings |
| `heartbeat` | 连接心跳 |
| `error` | 稳定错误对象，之后关闭连接 |

客户端必须按 `sequence` 去重和排序，不应解析自然语言日志推断状态。

## 10. Security Scope 行为

| 场景 | 对外结果 | 下游调用要求 |
|---|---|---|
| 任一身份 Header 缺失、空白、过长或包含控制字符 | 400 | 不调用 RAGFlow、缓存或 LLM |
| 当前 Scope 查询列表 | 200 | SQL 必须以 `security_scope_id` 为首要过滤条件 |
| 当前 Scope 访问自己的资源 | 按正常业务结果 | 下游映射只能来自当前 Scope |
| 使用其他 Scope 的资源 ID | 404 | 不调用 RAGFlow、Embedding、Reranker 或 LLM |
| 一次请求混用本 Scope 和其他 Scope 的知识库 ID | 404，整次失败 | 禁止返回部分结果 |
| 不同 Scope 使用相同 `external_id` 或 `Idempotency-Key` | 允许 | 分别存储、分别返回 |
| 精确或语义缓存候选来自其他 Scope | 视为未命中并记录安全事件 | 禁止读取候选正文或引用 |

错误响应和日志不得暴露目标资源属于其他 Scope。`security_scope_id` 只作为内部派生字段，不返回给上游。

## 11. 接口兼容策略

- `/v1` 内只允许增加可选字段和枚举；删除、改名、改变必填性需要 `/v2`。
- 客户端必须忽略未知响应字段。
- 服务器拒绝未知请求字段，防止拼写错误静默生效。
- OpenAPI 变更必须经过 diff 审核和契约测试。
- RAGFlow 升级不得直接改变本项目 OpenAPI。

| 类别  | 接口                                                     |
| --- | ------------------------------------------------------ |
| 能力  | `GET /v1/capabilities`、`GET /v1/version`               |
| 健康  | `GET /health/live`、`GET /health/ready`                 |
| 知识库 | Create/List/Get/Patch/Delete                           |
| 文档  | Upload/List/Get/Delete/Parse/Reparse/Retry/List Chunks |
| 任务  | Get/Cancel                                             |
| 检索  | `POST /v1/retrieval`                                   |
| 回答  | `POST /v1/answers`、`POST /v1/answers/stream`           |
| 请求  | Get/Cancel                                             |
| 引用  | 按 Request 查询、按 Citation 查询                             |
| 反馈  | `POST /v1/feedback`                                    |
