# 文档安全版本更新与回滚

更新时间：2026-08-10

## 1. 功能目标

当知识库中的文件需要更新时，系统不再要求调用方先删除旧文档。调用方可以对同一个
逻辑 `document_id` 上传新文件，系统会在后台创建独立候选版本：

```text
上传候选文件
  → 独立上传到 RAGFlow
  → 应用 Parser / Chunk Profile
  → 解析与 Embedding
  → 建立 Metadata、层级索引和实体侧车
  → 候选物理文档暂时关闭
  → 打开候选物理文档并关闭旧物理文档
  → PostgreSQL 原子切换活动版本
  → Knowledge Base Revision +1
  → 旧答案缓存自然失效
```

候选版本成功前，旧版本始终保持 `ACTIVE` 和可检索。候选上传、解析、Embedding、
Metadata 或发布任一步失败，都只会把候选标记为 `FAILED`，不会把稳定逻辑文档改成
失败状态。

## 2. 逻辑文档和物理版本

- `DocumentRecord` 是上游长期使用的逻辑文档，`id` 在更新前后不变；
- `DocumentVersion` 是一次不可变文件快照，保存版本号、文件 Hash、Profile、状态和
  对应的 RAGFlow 物理文档 ID；
- `active_version_id` 指向当前在线版本；
- `document_revision` 每次发布或回滚成功后增加；
- 退役版本的 RAGFlow 物理文档会关闭但暂时保留；其全部 Chunk 会由 RAGFlow 同步
  设为不可检索，以支持快速回滚；
- 删除逻辑文档时，Worker 会删除它的全部 RAGFlow 物理版本，避免历史索引成为孤儿。

同一逻辑文档同一时刻只允许一个 `ACTIVE` 版本，也只允许一个执行中的更新 Job。
数据库唯一约束、文档级事务锁和 Job 检查共同阻止并发更新产生两个活动版本。

## 3. 更新模式

接口接受：

- `AUTO`：请求 Gateway 选择可用模式；
- `SAFE_REPLACE`：明确要求安全整版替换。

当前实现中，`AUTO` 的 `effective_mode` 也是 `SAFE_REPLACE`。这代表系统会重新解析并
构建新文件的索引，不会把整版重建伪装成 Chunk 级增量更新。

如果新文件只在末尾增加几段，当前仍会建立一套独立候选索引。这样会多消耗一些解析
资源，但不需要修改 RAGFlow 源码，并能保证失败时旧文件完整可用。真正的 Chunk Diff、
向量复用和局部索引更新属于后续独立能力。

## 4. API

### 4.1 上传新版本

```http
POST /v1/documents/{document_id}/versions
Authorization: Bearer <JWT>
Idempotency-Key: <UUID>
Content-Type: multipart/form-data
```

表单字段：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `file` | 是 | 新版本原文件 |
| `metadata` | 否 | JSON 对象；覆盖继承的业务 Metadata |
| `parser_profile` | 否 | 新版本 Parser；留空先沿用兼容的当前值，不兼容时按配置规则选择 |
| `chunk_profile` | 否 | 新版本 Chunk Profile；留空按文件族推荐 |
| `mode` | 否 | `AUTO` 或 `SAFE_REPLACE`，默认 `AUTO` |

返回 HTTP 202，包含 `version_id`、`version_number`、`job_id`、`status` 和
`effective_mode`。Job 成功只表示新版本已经完成发布，而不只是文件已上传。

如果新文件的 `content_sha256` 与当前活动版本完全相同，接口返回 409、
`RAG-DOC-014`，并给出 `existing_document_id`，避免没有意义的版本。

最终 Parser/Chunk 会固化到候选版本。任务重试沿用候选版本，不会重新执行自动选择；
因此上传被接受后，即使知识库默认值或路由规则变化，本次版本任务也不会发生策略漂移。

### 4.2 查询版本历史

```http
GET /v1/documents/{document_id}/versions?page=1&page_size=20
Authorization: Bearer <JWT>
```

版本状态包括：`PENDING`、`UPLOADING`、`PARSING`、`INDEXING`、`ACTIVE`、
`RETIRED` 和 `FAILED`。响应不会暴露内部 RAGFlow 物理 ID 或安全 Scope。

### 4.3 回滚到旧版本

```http
POST /v1/documents/{document_id}/versions/{version_id}/rollback
Authorization: Bearer <JWT>
Idempotency-Key: <UUID>
```

只有具有物理索引的 `RETIRED` 版本可以回滚。接口返回 HTTP 202 和 Job ID；Worker
复用与发布相同的安全切换流程，回滚成功后 Revision 增加、当前版本变为 `RETIRED`，
以后仍可再次切换回来。

## 5. 音视频版本

音视频更新沿用首次上传的媒体处理能力。系统先把候选媒体转换为 Markdown，再把派生
内容交给 RAGFlow；版本历史保留原始文件名、MIME、大小和 Hash。派生文件恢复点写入
Job 后，Worker 重试不会重复调用云端 ASR 或视觉模型。

## 6. 一致性和故障边界

RAGFlow 物理文档状态和 PostgreSQL 不能组成一个分布式数据库事务，因此发布采用
“外部可用性切换 + 数据库事务 + 补偿”方式：

1. 解析完成后先关闭候选物理文档，避免候选提前进入检索 Top-K；
2. 发布时打开候选物理文档、关闭旧物理文档；RAGFlow 官方状态接口会同步文档
   `status` 和该文档全部 Chunk 的 `available_int`；
3. 在 PostgreSQL 同一事务中切换 `DocumentVersion`、`DocumentRecord`、侧车索引和
   Knowledge Base Revision；
4. 数据库提交失败时，重新打开旧物理文档并关闭候选物理文档；
5. 可重试错误保留候选进度；重试耗尽才标记 `FAILED`。

因此，版本切换完成后不仅旧版 Chunk 不参与检索，RAGFlow 管理界面的旧文件“启用”
开关也会显示为关闭；活动新版则显示为启用。系统仍保留显式 Chunk ID 可用性接口，
但它只用于细粒度修复和运维，不再承担正常发布与回滚。

外部补偿存在毫秒到秒级窗口，因此该方案是可靠的 Saga/补偿发布，不宣称跨系统强
事务。生产环境应监控 UPDATE_DOCUMENT Job、失败候选和可用性补偿日志。

## 7. 开发控制台验收

打开 <http://127.0.0.1:3000> 的“文档与解析”：

1. 先上传并解析一个非业务 Fixture，确认文档 `READY`；
2. 点击该文档的“版本历史”；
3. 在“安全更新文档”上传有明显新增内容的新版本；
4. 记录 `version_id`、`job_id`，查询 Job 直到 `SUCCEEDED`；
5. 刷新版本历史，确认 v1 为 `RETIRED`、v2 为 `ACTIVE`；
6. 确认逻辑 `document_id` 未变化，`active_version_number` 和
   `document_revision` 增加；
7. 检索新增内容，确认引用仍指向同一逻辑文档；
8. 对 v1 点击回滚并等待 Job 成功，再验证旧内容恢复；
9. 上传与当前活动版完全相同的文件，确认返回 `RAG-DOC-014`；
10. 打开 <http://127.0.0.1:8081> 的对应 Dataset，确认活动版文件开关为开启、退役版
    文件开关为关闭；回滚后两个开关应互换；
11. 使用无法解析的候选文件验证失败后旧版本仍能检索和保持启用。

所有验收只使用最小非业务 Fixture，不直接修改数据库，不删除 Docker 数据卷。

## 8. 当前验证状态

截至 2026-08-10，版本更新已覆盖完整物理文档关闭/开启、既有 Chunk 级修复接口、
发布、回滚和 PostgreSQL 激活失败后的 Saga 补偿。最近一次验证结果见
[ACCEPTANCE_REPORT.md](ACCEPTANCE_REPORT.md)。

11 项跳过需要显式的真实 PostgreSQL 或 Redis 测试地址。最近回归时 Docker daemon
不可用，因此 `0006_document_version_updates` 尚未在该轮重新应用到真实 PostgreSQL，
也没有用真实 RAGFlow 重跑候选发布、失败补偿、重启恢复和回滚生命周期。自动化通过
表示代码与契约一致，不等于这些真实环境验收已经完成。
