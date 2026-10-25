# Codex 单机实施指令

> 当前实现覆盖说明（2026-08-19）：JWT/JWKS 步骤属于未来生产 Identity 目标。当前
> 代码已选择 Direct Tenant/User 临时模式并拆分独立存储 Compose。

## 1. 任务

在公司 RAG 仓库的一台开发电脑上实现本规格包定义的第一期 RAG Server：
自研 RAG Gateway、RAG Worker、应用 PostgreSQL 和语义缓存，并通过公开 API
集成固定版本 RAGFlow。整个目录作为一个完整输入交给同一个 Codex 任务。不得
只生成代码骨架；每个里程碑必须有可运行测试和验收证据。

## 2. 开始前

1. 完整阅读 `README.md`、`docs/00` 至 `docs/12`。
2. 运行 `python tools/validate_spec.py`。
3. 以 OpenAPI、SQL、错误码和 RAGFlow 映射为实现契约。
4. 确认 Docker、PostgreSQL 18.4、Redis Stack、模型服务和 RAGFlow v0.26.4
   环境可用。
5. 先建立 ADR/Issue 记录影响兼容性的规格偏差，不得静默猜测。

事实冲突优先级：

```text
OpenAPI > database/schema.sql > 错误码 > RAGFlow 接口映射
        > 详细设计 > 其他说明
```

## 3. 强制架构约束

- 路由只做协议适配；Use Case 负责编排；Domain 不依赖 FastAPI/Celery/RAGFlow。
- RAGFlow 只能通过 `RagEnginePort`/Adapter 访问，不读写其数据库、Redis、MinIO
  或 Elasticsearch。
- 除健康接口外全部验证上游 JWT。仅接受配置中的算法、Audience 和
  Key，禁止 `alg=none` 和根据 Token Header 任意取远程 Key。
- `SecurityContext` 只能由认证中间件构造；请求参数不能构造或覆盖。
- 所有外部可寻址 Repository 方法必须把 `security_scope_id` 作为必需参数。
- 访问其他 Scope 的 ID 返回 404，且在 RAGFlow、缓存正文、Embedding 或 LLM
  调用之前终止。
- 精确/语义缓存都按 Scope 隔离；语义命中后重验 revision 和 Citation。
- Gateway、Worker 和 Scheduler 使用同一个 `UploadStagingPort`/命名卷；
  Gateway 只原子发布 `.ready` 文件，Worker 只消费已校验的 `.ready` 文件。
- Profile YAML 是种子，数据库是运行时事实来源；Bootstrap 不得覆盖同版本不同
  checksum 的 Profile。
- 不实现用户注册、登录、组织、角色、Workspace 成员、复杂 ACL、Source、
  DocumentVersion、IndexGeneration 或 Tombstone。
- 第一阶段不实现自动质量评测平台和完整可观测平台，但必须有基础结构化日志、
  `request_id`、`job_id`、稳定错误码和关键阶段耗时。

## 4. 推荐项目结构

```text
rag-server/
├── src/rag_service/
│   ├── api/                 # FastAPI 路由、DTO、错误处理、SSE
│   ├── security/            # JWT Verifier、SecurityContext、Scope 派生
│   ├── application/         # Use Cases
│   ├── domain/              # 实体、状态机、错误、Ports
│   ├── infrastructure/
│   │   ├── postgres/        # Scoped Repositories
│   │   ├── ragflow/         # Client、Adapter、DTO
│   │   ├── cache/           # Exact/Semantic Cache
│   │   ├── staging/         # Local Volume Upload Staging
│   │   └── llm/
│   ├── bootstrap/           # Profile 导入和 Redis 索引初始化
│   └── workers/
├── migrations/
├── tests/
│   ├── unit/
│   ├── repository/
│   ├── api/
│   ├── contract/
│   └── integration/
├── deploy/
├── Dockerfile
└── pyproject.toml
```

## 5. 里程碑

### M0：契约、骨架和安全底座

- Python 3.12、FastAPI、Pydantic、SQLAlchemy 2/psycopg 3、版本 SQL、Celery、
  Ruff、MyPy、pytest 和 CI。
- DTO、Domain Error、Ports、Fake Adapter 和初始迁移。
- `rag_service bootstrap all`：幂等导入 Profile 并创建/校验语义缓存索引。
- JWT/JWKS 验证、`SecurityContext`、Scope 派生、统一响应、Request ID、
  Health/Version。
- Scoped Repository 与两个 Scope 的数据库自动测试。

### M1：离线知识入库

- RAGFlow Client/Adapter、KnowledgeBase/Document/Job API 和 Worker。
- 共享 Volume `UploadStagingPort`、流式写入、原子 ready、Hash、幂等、状态机、
  轮询、取消、有限重试、租约、过期清理和补偿。
- Dataset/Document/Job 的 Scope 映射和契约测试。
- `RagEnginePort` 的 Dataset/Document/Parse/Chunk/Retrieval/Health 方法与
  `contracts/ragflow-fixture-manifest.yaml` 全部实现。

### M2：在线检索和生成

- Retrieval、Query Rewrite、Candidate 标准化、Context Pack。
- LLM OpenAI-compatible Client、Citation Builder、拒答、REST Answer 和 SSE。
- 在所有下游调用前完成知识库 Scope 批量校验。

### M3：第一期增强

- 引用校验、精确缓存、Redis Stack 语义缓存、Feedback。
- 按 `docs/12` 实现 Redis Hash、`FT.CREATE`、FLOAT32 编码、Scope Filter、
  配置/Revision 边界、阈值、TTL、原子幂等 Upsert、引用重验和故障绕过。
- 基础结构化日志、关键阶段耗时和 Query 记录。

### M4：集成和部署

- 完整 Compose `up -d --build`、PostgreSQL RAGFlow PoC、Migration、
  Bootstrap、Preflight 和 Smoke。
- 提供 `tools/generate_dev_keys.py`、`tools/issue_dev_token.py` 和
  `tools/ragflow_lifecycle_smoke.py`；私钥只生成到 Git 忽略目录。
- 超时、有限重试、依赖降级和进程恢复。
- 使用 Mock/Fake/最小 Fixture 完成自动测试；使用方用自有数据手动 E2E。

## 6. 单个 Codex 的执行方式

### 6.1 单工作区、单实现

- 在一个仓库和一个工作区中实现，不创建两套脚手架或重复 Domain Model。
- 默认使用一个实现分支；每完成一个里程碑建立可回退 Commit。
- 同一时间只允许一个里程碑处于进行中，先修复当前阶段测试再继续。
- 可以使用 Fake/Mock 解耦外部依赖，但真实 Adapter 必须实现同一个 Port。
- 不因本机暂时缺少模型或 RAGFlow 镜像而删除功能；应保留配置、Fake 测试，并
  把真实 Smoke 标记为待环境验证。

### 6.2 推荐编码顺序

1. 建立项目、配置加载、统一错误、OpenAPI 模型和基础测试。
2. 实现 JWT、`SecurityContext`、Scoped Repository 和初始 Migration。
3. 实现 Profile/Cache Bootstrap、共享 Upload Staging、知识库、文档、Job、
   幂等、完整 RAGFlow Adapter 和 Worker。
4. 实现 Retrieval、Query Rewrite、Context、LLM、Citation 和 SSE。
5. 实现精确缓存、语义缓存、反馈和基础查询记录。
6. 完成 Dockerfile、统一 Compose、Migration、Bootstrap、Preflight 和 Smoke。
7. 执行全量自动测试，再由使用方使用自有数据做手动 E2E。

### 6.3 本机资源策略

- 默认使用 CPU Profile，不要求本机启动 GPU 模型。
- Embedding、Reranker 和 LLM 通过 OpenAI-compatible 地址配置；macOS 本地
  Ollama 可运行在宿主机并由容器通过
  `http://host.docker.internal:11434/v1` 调用。
- RAGFlow、PostgreSQL、Redis Stack、MinIO 和 Elasticsearch 由统一 Compose
  启动。
- Apple Silicon 默认只对 RAGFlow 服务设置 `linux/amd64`；禁止全局强制所有
  镜像使用 amd64。自研镜像原生构建为 ARM64。
- 启动前检查内存、磁盘和 `vm.max_map_count`；资源不足时先完成 Fake/Contract
  测试，不得通过移除 Elasticsearch、Scope 或语义缓存改变设计。
- 只有 `rag-gateway` 对宿主机暴露业务端口。

## 7. 测试和完成定义

- 自动测试不使用真实业务数据；只使用最小 Fixture 和测试 JWT。
- Fake 与真实 Adapter 遵循同一 Port 契约。
- 每个里程碑必须运行受影响的 Unit/API/Repository/Contract 测试。
- 最终集成前运行规格校验、全量测试和 OpenAPI Diff。
- 未运行的测试、RAGFlow PostgreSQL PoC 或手动 E2E 必须明确标为“未验证”，
  不得声称完成。
- RAGFlow PostgreSQL PoC 失败必须分类为数据库、RAGFlow API、模型/存储依赖或
  架构模拟问题；不能用 MySQL 回退掩盖未定位的 ARM64 模拟错误。

每个里程碑报告：

1. 已完成 Issue 和主要文件；
2. 测试命令、结果和失败项；
3. 规格偏差与 ADR；
4. 安全边界验证；
5. 下一阶段依赖。

最终报告包括 API 地址、Migration Revision、镜像 Digest、Compose 启动方式、
RAGFlow Smoke、两个 Scope 隔离证据、语义缓存证据、已知限制和回滚步骤。
