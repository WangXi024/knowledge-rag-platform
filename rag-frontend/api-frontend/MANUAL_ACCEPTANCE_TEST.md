# 3000 开发控制台人工验收

更新时间：2026-08-19

只使用最小非业务 Fixture。不要在截图、日志或缺陷单中粘贴密码、API Key 或模型凭据。

## 1. 准备

```bash
./deploy/start-full.sh
cd frontend
npm install
npm run prepare:local
npm run dev
```

打开 <http://127.0.0.1:3000>。确认页面没有登录/注册入口，左侧显示可编辑的 Direct
Tenant ID 和 User ID，可使用 `manual-tenant-a / manual-user-a`。

## 2. 系统接口

1. 打开“系统状态”；
2. 检查 `/health/live`、`/health/ready`、`/v1/version`、`/v1/capabilities`；
3. 预期公开接口无需身份 Header，业务接口由 BFF 自动发送 Tenant/User 两个 Header；
4. Rerank 关闭时必须明确显示降级能力，不能报 500。

## 3. 知识库

1. 创建 `manual-kb-a`；
2. 使用页面当前提供的 Parser、Chunk、Embedding 和 Retrieval Profile；
3. 保存返回的 `knowledge_base_id`；
4. 查询列表、详情并修改名称/描述；
5. 确认 `document_count` 初始为 0。

写操作需要幂等键；页面自动生成。使用同一幂等键和同一请求重试时不能创建第二份资源。

## 4. 文档与 Job

### 4.1 手动解析

1. 上传 `deploy/fixtures/ragflow-smoke.txt`；
2. 关闭“自动解析”；
3. Parser 覆盖填写 `office-default@1`；该 `.txt` Fixture 不支持 DeepDoc，选错时应返回
   `RAG-KB-004`，用于验证文件类型与 Parser Profile 的兼容性校验；
4. Metadata 使用 `{"source":"manual-acceptance","language":"zh-CN"}`；
5. 保存 `document_id`、上传 `job_id`；
6. 上传 Job 成功后，文档应为 `UPLOADED`；
7. 点击“解析”，轮询新 Job；
8. 最终文档应为 `READY`，Chunk/Token 大于 0。

### 4.2 自动解析和重复文件

1. 重新选择同一个文件、相同知识库并开启自动解析；
2. 预期返回“文件内容已经上传”和 `existing_document_id`；
3. 不应保存第二份完全相同内容。

### 4.3 Chunk

1. 打开文档 Chunk；
2. 确认能读取真实文本和位置；
3. 解析器/Chunk Profile 应与文件类型自动路由结果一致；
4. 不应把侧车摘要或 Parent 文本伪装为原文 Chunk。

## 5. 混合检索

对上述知识库执行：

```text
What color is the synthetic acceptance marker?
```

检查：

- `original_query`、`effective_query` 和 `query_plan`；
- 向量与 BM25 路线、加权 RRF；
- `score`、`vector_score`、`term_score`；
- Rerank 启用时才出现真实 `rerank_score`；
- 引用候选属于当前知识库和文档。

再验证 Document ID 和 Metadata Filter。过滤条件是强约束：不满足时返回 0 候选，不得
回退到其他文档。

## 6. JSON 与 SSE 问答

1. 在“问答实验室”选择当前知识库；
2. 提出能由 Fixture 回答的问题；
3. 先执行普通 JSON，再执行 SSE；
4. 检查答案引用标签、引用详情、响应语言、改写问题、Warnings 和耗时；
5. SSE 必须以 `completed` 结束，不能把中断的 Token 当最终答案；
6. 提出无证据问题，应返回 `no_evidence`/拒答，不应编造。

连续两次提交完全相同请求，第二次应允许命中 `exact`；使用语义近似问题时可能命中
`semantic`，未达到阈值返回 `none` 不算接口失败。缓存命中后仍必须重新校验引用和
知识库边界。

## 7. 文档版本更新和回滚

1. 准备内容增加一行的新版本文件；
2. 调用 `POST /v1/documents/{document_id}/versions`；
3. 轮询 Job，确认新版本解析成功后才切换；
4. 查询版本列表，确认一个 `ACTIVE`、旧版本为 `RETIRED`；
5. RAGFlow 中旧版本文档应禁用，新版本启用；
6. 检索只返回活动版本；
7. 对旧版本执行 rollback，轮询 Job 后确认状态反转；
8. 整个过程 `document_id` 不变，知识库 Revision 增加，旧缓存不再复用。

新版本解析失败时，旧活动版本必须继续可检索。

## 8. 删除与取消

1. 尝试取消已完成 Job，预期返回“已结束 Job 不可取消”；
2. 删除有运行中 Job 的文档应被拒绝；
3. Job 结束后删除文档；
4. 非空知识库不使用 `force=true` 时返回冲突；
5. 文档清空后删除知识库。

## 9. Direct Tenant/User 隔离

1. 记录 `manual-tenant-a / manual-user-a` 的知识库 UUID；
2. 保持 Tenant 不变，把 User 切换为 `manual-user-b`；
3. 最近资源 UUID 应自动清空；
4. 列表中不能看到 A 的知识库；
5. 在原始请求控制台直接查询 A 的 UUID，应返回 404；
6. 切回 A 后资源仍可见；
7. 再保持 User 不变，把 Tenant 改为 `manual-tenant-b`，A 的资源同样必须返回 404。

这验证组合 Scope 隔离，不代表身份认证。当前调用方仍能主动填写其他 Tenant/User。

## 10. 自动测试与密钥检查

```bash
cd ..
.venv/bin/python -m pytest -q
.venv/bin/ruff check src migrations tests tools
.venv/bin/python -m mypy src
.venv/bin/python rag-development-spec/tools/validate_spec.py
.venv/bin/python tools/check_tracked_secrets.py

cd frontend
npm test
npm run build
```

验收记录至少包含：时间、分支/Commit、非敏感 Tenant/User ID、资源 UUID、request_id、预期、
实际和日志摘要。不得记录真实密码或 Key。
