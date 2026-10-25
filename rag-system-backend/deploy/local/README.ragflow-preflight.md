# RAGFlow 前置环境专项说明

更新时间：2026-08-10

本文件只说明 RAGFlow 前置环境边界。完整部署、模型配置、Smoke、备份和恢复命令见
[部署文档](README.md)。

## 固定边界

- 使用官方 `infiniflow/ragflow:v0.26.4`；
- RAGFlow Server 与 Task Executor 在 Apple Silicon 上单独使用 `linux/amd64`；
- 不修改、不 Fork、不挂载 `ragflow-main` 源码；
- RAGFlow PostgreSQL、Elasticsearch、MinIO 和 Valkey 使用独立持久卷；
- RAGFlow UI 只绑定 `127.0.0.1:8081`，依赖数据库不发布宿主机端口；
- 禁止执行 `docker compose down -v`，除非用户明确要求销毁全部本地数据。

## 通用模型配置

复制示例文件：

```bash
cp deploy/local/.env.ragflow.example deploy/local/.env.ragflow
```

然后只按能力填写以下字段：

```text
LLM_BASE_URL=
LLM_API_KEY=
LLM_MODEL=
REWRITE_MODEL=

EMBEDDING_BASE_URL=
EMBEDDING_API_KEY=
EMBEDDING_MODEL=

VISION_BASE_URL=
VISION_API_KEY=
VISION_MODEL=

MEDIA_VISION_BASE_URL=
MEDIA_VISION_API_KEY=
MEDIA_VISION_MODEL=
```

可选 Rerank 使用项目约定的 `POST /rerank`：

```text
RERANK_ENABLED=true
RERANK_BASE_URL=
RERANK_API_KEY=
RERANK_MODEL=
```

可选音视频 ASR 使用项目约定的 Chat Completions `input_audio`：

```text
MEDIA_PROCESSING_ENABLED=true
ASR_BASE_URL=
ASR_API_KEY=
ASR_MODEL=
```

Rerank 和 ASR 不是 OpenAI 官方统一协议。更换供应商前必须确认响应结构符合项目契约；
Chat、Embedding、文档 Vision 和媒体 Vision 分别使用 Chat Completions、Embeddings
与带 `image_url` 的 Chat Completions。媒体处理开启时 ASR 和媒体 Vision 都必须配置。

`EMBEDDING_DIMENSION` 可以保持 `pending-auto-detection`。启动脚本会真实发送单条和
批量 Embedding 请求，探测维度并写回 Git 忽略的私有环境文件。

## 启动与账号初始化

保持七个本地密码为 `change-me-auto-generated`，保持：

```text
RAGFLOW_ADMIN_EMAIL=admin@lazytt.io
RAGFLOW_API_KEY=
```

然后执行：

```bash
./deploy/local/start-full.sh
```

脚本会分别生成七个随机密码，通过 RAGFlow 官方用户与 Token API 注册或登录管理员，
创建/复用稳定 API Key，通过官方 Provider API 注册 Embedding 与 Vision，并把非敏感
模型引用和 Key 写回权限为 `0600` 的忽略文件。它不会直接改 RAGFlow 数据库，也不会
在终端打印密码或 Key。

管理界面：<http://127.0.0.1:8081>

Gateway：<http://127.0.0.1:8080>

## 验证和 Smoke

```bash
./deploy/local/run-gateway-model-smoke.sh
./deploy/local/run-ragflow-smoke.sh --parse-timeout 600
```

图片能力可使用合成非业务图片验证：

```bash
uv run python tools/gateway_image_smoke.py \
  --output smoke-results/gateway-image.json
```

`ragflow-smoke-embedding` 只属于显式 `smoke` Compose Profile，不是正常开发运行的
默认 Embedding 服务，也不能用于业务质量评估。

## 安全说明

- API Key、密码和 JWT 只能出现在 Git 忽略文件或 Secret Manager；
- Gateway 与 RAGFlow 在 Docker 内部网络通信，但内部网络不替代应用层身份认证；
- 模型或维度变化时必须创建新的知识索引和语义缓存索引世代；
- 真实业务数据接入前必须单独完成供应商合规、费用、限流和故障演练。
