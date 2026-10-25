# lazytt-rag-system

第一期独立 RAG Server，实现了 M0～M4 的 Gateway、Worker、Scheduler、应用
PostgreSQL、Redis Stack 语义缓存，以及固定版本 RAGFlow 的完整本地编排。

> [!IMPORTANT]
> 第一次使用本项目，请先阅读
> **[《LazyTT RAG 本地部署与使用手册》](docs/USER_MANUAL.md)**。
> 手册面向新手，完整说明了软件安装、Docker 与云端模型配置、首次初始化、API Key、
> 前后端启动、Direct Tenant/User、文件上传、问答使用、停止重启和常见故障排查。不要直接跳到
> 启动脚本前，必须先按手册填写 Git 忽略的 `deploy/local/.env.ragflow`；此后首次初始化和
> 日常重启都使用同一条命令，RAGFlow API Key 会自动创建和保存。

当前模型接入基线：

- RAGFlow：官方 `infiniflow/ragflow:v0.26.4`，内部数据库为 PostgreSQL。
- Chat/Rewrite、Embedding 和 Vision 均通过供应商无关的 OpenAI-compatible Adapter
  接入；每项能力只配置 Base URL、API Key 和模型名。
- 启动 Bootstrap 会真实探测普通/结构化回答、单条/批量 Embedding、实际向量维度和
  合成图片 OCR，并自动写入不含密钥的协议兼容字段及 RAGFlow 模型引用。
- 音视频入库：容器内 FFmpeg/ffprobe 分段与抽帧，配置的 ASR/媒体 Vision 服务处理内容，
  再以带时间戳 Markdown 交给 RAGFlow 建索引。
- 检索：向量 + BM25 + 加权 RRF；可选 Rerank 使用项目约定的真实 Cross-Encoder
  `POST /rerank` 协议，故障时自动保留 RRF 排名。
- 切块与索引优化：新文件按正文、表格、视觉和媒体文件族自动选择不可变 Chunk
  Profile；`hybrid-structure-aware@6` 按问题类型动态选择 Top-K，并在 0.40 侧车
  相似度门槛后提供文档摘要、章节、标题/长度动态 Parent、表格行组及实体/显式关系导航。
  检索上下文会携带文档、章节和表头前缀，但最终引用始终回读 RAGFlow 原始 Chunk，
  不修改 RAGFlow 源码，也不会把生成前缀伪装成原文。
- 质量控制：同 Scope、同知识库的 SHA-256 完全重复文件会被拒绝；检索候选还会执行
  语义近重复删除和相邻 Chunk 上下文合并。这里的“语义去重”只处理召回候选，不会
  把内容相近但版本不同的上传文件自动删除。

本地地址：

- 普通用户网页：<http://127.0.0.1:3001>（由独立 `lazytt-frontend-console` 提供）
- RAG 测试控制台：<http://localhost:3000>
- Gateway：<http://127.0.0.1:8080>
- RAGFlow 管理界面：<http://127.0.0.1:8081>

当前开发联调版不提供账号、密码、Session 或 JWT：业务 API 使用 `X-Tenant-ID` 与
`X-User-ID` 派生隔离范围。两个 Header 都不是认证凭据，禁止直接用于生产；后续由
公司 Identity 提供可信 Tenant/User。RAGFlow 管理端口只绑定本机，PostgreSQL、Redis、
Elasticsearch 和 MinIO 不发布宿主机端口。

## 后端部署

本地部署见 [deploy/local/README.md](deploy/local/README.md)，ECS 开发部署见
[deploy/ecs/README.md](deploy/ecs/README.md)。迁移 SQL 位于 `migrations/rag_app/` 和
`migrations/rag_flow/`；ECS 角色/权限/注释/验收位于 `deploy/ecs/database/`。
本地保留一键启动；ECS 完成首次管理员配置后由 `dev` 流水线自动发布。

## 快速开始

新电脑首次部署先按 [`docs/USER_MANUAL.md`](docs/USER_MANUAL.md) 创建
`deploy/local/.env.ragflow`，按能力填写 Base URL、API Key 和模型名；七个本地密码由启动
脚本首次自动生成。填好后，无论首次部署还是以后重启都执行。脚本会先启动独立
存储 Compose，再启动 RAGFlow 与应用运行时 Compose：

```bash
./deploy/local/start-full.sh
```

首次初始化、环境变量、云端模型 Key、测试、备份恢复和故障处理见
[`deploy/local/README.md`](deploy/local/README.md)。只停止运行时并保留独立存储和全部数据：

```bash
./deploy/local/stop-full.sh
```

禁止在普通操作中使用 `docker compose down -v`。

存储栈也可单独管理；停止存储容器仍不会删除命名卷：

```bash
./deploy/local/start-storage.sh
./deploy/local/stop-storage.sh
```

将 `RAG_STORAGE_MODE` 改为 `external` 并配置连接地址后，运行时会跳过本地存储栈；
业务代码不需要修改。

本地后端启动后，可另开终端启动全接口测试控制台：

```bash
cd frontend
npm install
npm run prepare:local
npm run dev
```

测试控制台允许直接编辑 Tenant/User 来验证隔离，不再创建本地账号或签发 JWT。
切换任一 ID 时会清空浏览器记录的最近资源 UUID，但不会删除任一范围的数据。
具体使用和安全边界见 [`frontend/README.md`](frontend/README.md)。

普通用户上传和流式问答界面已经拆分到同级独立仓库
`../lazytt-frontend-console`，正式源码基线为 `54409d2`。请按
[`docs/USER_MANUAL.md`](docs/USER_MANUAL.md) 第 5、9 章克隆并通过该仓库的
`docker compose up --build -d` 启动 3001 端口；后端仓库内 `user-frontend/`
只是历史参照快照，不作为正式部署入口。

## 质量检查

```bash
uv sync --frozen --extra dev
uv run python rag-development-spec/tools/validate_spec.py
uv run ruff check src migrations tests tools
uv run mypy src
uv run pytest --cov --cov-report=term-missing
uv run python tools/check_tracked_secrets.py
```

Domain/Application 核心分支覆盖率门禁为 80%，并在 CI 中使用 PostgreSQL 18.4
与 Redis Stack 执行真实 Repository/Cache Integration 测试。

实际验收结果、已知限制与未完成的使用方业务数据验证见
[`docs/ACCEPTANCE_REPORT.md`](docs/ACCEPTANCE_REPORT.md)。

进一步阅读：

- [`docs/PROJECT_STATUS_AND_ROADMAP.md`](docs/PROJECT_STATUS_AND_ROADMAP.md)：
  当前完成内容、未完成项、技术债和分阶段改进路线。
- [`docs/CODEBASE_STRUCTURE.md`](docs/CODEBASE_STRUCTURE.md)：
  项目架构、逐目录/逐文件职责、API、数据表、运行流程和修改定位指南。
