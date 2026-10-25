# PDF 原文件与切块位置映射

管理端在同一个阅读面板显示 PDF 原文件和分页切块。点击“查看原文”跳到首个来源页，
跨页切块可在“对应页”中切换。页面只绘制当前页，不执行 PDF 脚本或暴露内嵌附件入口。

## 接口与安全边界

- `GET /v1/management/documents/{document_id}/source?document_revision=N`
  返回 `application/pdf` 字节流，只支持 READY 的原 PDF，最大 100 MiB。
- `GET /v1/management/documents/{document_id}/chunks?page=1&page_size=20&document_revision=N`
  使用相同修订读取切块。旧调用方可省略修订号，保持原有分页行为。
- 修订取自文档列表的 `document_revision`。文档更版或重新解析后固定修订查看返回
  `RAG-DOC-019` / 409，必须刷新列表后重新打开，不静默把旧切块映射到新文件。
- 先检查管理策略，再验证文档及知识库资源 Scope；跨租户与不存在统一 404。
  当前管理策略仍依赖可信内网，Tenant/User Header 不等于生产管理员认证。
- 仅经服务端映射调用 RAGFlow 官方
  `GET /api/v1/datasets/{dataset_id}/documents/{document_id}`；不接受任意 URL/磁盘路径，
  不绕过 RAGFlow 直接读 MinIO，不向浏览器返回引擎 ID、对象地址或 API Key。
- 拒绝重定向、错误 MIME、非 PDF 文件头及长度异常；流结束检查原文件 SHA-256。
  失败中断流，不完整文件不会交给前端 PDF.js。完成或取消后关闭下游连接。
- 文件响应使用 `Cache-Control: no-store` 与 `X-Content-Type-Options: nosniff`；
  BFF 流式转发，浏览器仅在当前阅读会话内存持有文件，关闭即销毁阅读器。

## 坐标契约与限制

`positions` 中的 `page` 为一基页码；`bbox` 顺序是 `[left, right, top, bottom]`，
不是 `[x1, y1, x2, y2]`。普通 PDF 坐标以左上角为原点，使用 72 DPI 页面单位。
缩放阅读时按页面原始宽高转换为百分比，支持一块对应同页多个矩形或跨多页。

- 页码存在但坐标缺失：跳页，提示无法精确高亮。
- 没有页码：不猜测对应页面。
- 倒置、越界、非有限坐标：不绘制。
- 旋转或非零裁切原点页面：暂只跳页，明确提示需要特殊转换。
- 源文件已丢失或内容不符：显示失败与重试入口，不触发重新解析。
- Word、Excel、图片和音视频暂不提供本功能，也不会将派生 Markdown 当作原文件。

阅读器采用 [Mozilla PDF.js](https://mozilla.github.io/pdf.js/examples/)，依赖、Worker、
字体和解码器由同源静态目录提供，不请求第三方 CDN。坐标次序对照
[RAGFlow 位置写入实现](https://github.com/infiniflow/ragflow/blob/main/rag/nlp/__init__.py)
及原文下载官方接口核对；部署的解析器可能没有返回位置，不能承诺所有旧文件可高亮。

## 验证和本地使用

后端必须先加载新增接口；前端安装锁文件依赖后运行正常的 dev/build 流程。
本次不改变数据库结构、存储卷、文档或解析任务，也不重新上传已有文件。

1. 使用非敏感、已解析的普通 PDF 打开“查看切块”，确认左侧原文件可加载。
2. 点击不同页的“查看原文”，核对正文对应页及高亮范围；跨页块逐页检查。
3. 检查无坐标/页码块和非标准页面的提示，而不是误画高亮。
4. 切换身份后重新进入；其他租户的私有 PDF 不应可访问。
5. 更版后再次访问旧修订链接，应为 409；后端不支持的新接口应明确失败。

自动验证入口：`tests/unit/test_document_source.py`、OpenAPI 契约测试，以及管理端的
`document-chunks.test.mjs`、`pdf-positions.test.mjs`、`pdf-source-preview.test.mjs`。
这些测试不上传或解析真实业务文件；画布视觉与实际解析器坐标仍需按上述步骤人工核对。
