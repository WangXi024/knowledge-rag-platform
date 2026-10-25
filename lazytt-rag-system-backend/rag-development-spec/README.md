# RAG 开发规格包

> 版本：1.4.0  
> 基线日期：2026-07-27  
> 实现目标：将整个规格包交给一个 Codex 任务，在一台开发电脑上按阶段生成、运行和验证完整 RAG 项目

> [!IMPORTANT]
> 2026-08-19 运行时临时采用 Direct Identity 模式：业务接口同时接收
> `X-Tenant-ID` 与 `X-User-ID`，不提供
> 注册、登录、密码、Session 或 JWT。原规格中的 JWT + JWKS 是未来生产 Identity
> 目标，不代表当前实现。当前机器可读第一事实来源为 `openapi/rag-api-v1.yaml`。

## 1. 规格包定位

本规格包定义一个独立的 RAG 系统：上游系统通过稳定 API 调用自研 RAG Gateway，Gateway 与 RAG Worker 通过 RAGFlow 完成文档解析、索引和检索，并自行完成 Security Scope 隔离、Context、LLM 生成、引用校验、精确缓存和语义缓存。

本项目不实现用户注册、登录、密码、Session、角色或 Workspace 成员。当前联调调用方
提供稳定 Tenant ID 与 User ID，Gateway 将两者组合后派生 Scope，并
强制所有资源、幂等记录和缓存按 `security_scope_id` 隔离。两个 Header 都不是认证凭据。

## 2. 固定技术决策

| 项目 | 决策 |
|---|---|
| RAG 引擎 | RAGFlow `v0.26.4`，独立本地部署 |
| 接入方式 | 只通过公开 HTTP API；禁止访问 RAGFlow 内部数据库和索引 |
| 文档引擎 | Elasticsearch，版本跟随固定的 RAGFlow Compose |
| RAGFlow 数据库 | 目标 PostgreSQL `18.4`，独立 `rag_flow` Database/账号；空库全链路 PoC 是启用门禁 |
| 解析器 | DeepDoc 默认；MinerU 作为可切换远程解析器 |
| 自研服务 | Python 3.12、FastAPI、Pydantic v2、SQLAlchemy 2、版本 SQL |
| 身份与隔离 | 当前 Tenant/User Header；未来生产接入 Identity |
| 异步任务 | Celery + Redis；PostgreSQL 任务记录为状态事实来源 |
| 应用数据库 | PostgreSQL `18.4`，独立 `rag_app` Database/Schema 和 `rag_app` 账号 |
| LLM | OpenAI-compatible Chat Completions |
| 缓存 | 第一期实现精确缓存和语义缓存；语义缓存使用独立的向量检索缓存实例，命中后必须重验版本范围与引用 |
| 上传交接 | Gateway/Worker/Scheduler 共享命名 Volume；Gateway 原子发布 ready 文件，Worker 异步上传 RAGFlow |
| Profile | YAML 作为不可变种子，Bootstrap 幂等导入 `config_profile`，运行时以数据库为准 |
| Apple Silicon | 仅 RAGFlow 容器使用 `linux/amd64` 兼容模式；自研镜像保持 ARM64 原生构建 |
| 对外协议 | REST/JSON + SSE |
| 版本策略 | 固定稳定 Tag 和镜像 Digest；初期不 Fork |

说明：目标部署统一使用 PostgreSQL，但必须保持逻辑隔离。RAG Gateway/Worker 只访问 `rag_app`，RAGFlow 只访问 `rag_flow`；两个数据库使用不同账号、迁移和备份边界，任何一侧都不得直接读写另一侧的数据表。本项目尚无历史数据，因此 RAGFlow 只执行空 PostgreSQL 初始化和完整 PoC，不执行 MySQL 数据迁移；PoC 失败时按选型报告中的回退策略处理。

## 3. 文件清单

| 文件 | 用途 |
|---|---|
| `docs/00-需求与范围说明.md` | 需求、范围、责任边界和可追踪完成定义 |
| `docs/01-RAG系统详细设计与开发规范.md` | 总体架构、模块、流程、安全范围和实现约束 |
| `docs/02-API接口说明.md` | 上游 API 语义、示例和 SSE 协议 |
| `docs/03-RAGFlow接口映射.md` | 内部 Adapter 与 RAGFlow v0.26.4 的映射 |
| `docs/04-错误码说明.md` | 稳定错误码、HTTP 映射和重试语义 |
| `docs/05-数据库设计.md` | 数据对象、状态、索引和保留策略 |
| `docs/06-部署运维手册.md` | 环境、部署、备份、恢复和升级 |
| `docs/07-测试与验收规范.md` | 第一期自动测试、手动验证和验收门禁 |
| `docs/08-Codex实施指令.md` | 交给 Codex 后的阶段化实施约束和交付报告要求 |
| `docs/09-技术分析与选型报告.md` | 框架、存储、解析、成本、风险和退路的选型依据 |
| `docs/10-安全与威胁模型.md` | 信任边界、JWT、越权、缓存、上传和部署威胁 |
| `docs/11-第一期测试用例与验收清单.md` | 可直接执行的第一期验收场景和证据要求 |
| `docs/12-Redis语义缓存物理设计.md` | Redis Hash、RediSearch Vector、TTL、并发和索引升级契约 |
| `openapi/rag-api-v1.yaml` | 对外 API 的机器可读契约，API 实现的第一事实来源 |
| `database/schema.sql` | PostgreSQL 初始 DDL |
| `configs/*.example.yaml` | 应用、解析、检索、模型和 Prompt 配置基线 |
| `deploy/docker-compose.app.yml` | 自研服务本地部署示例 |
| `compose.local.yml` | RAGFlow 与自研应用运行时 |
| `deploy/local/docker-compose.storage.yml` | PostgreSQL、统一 Redis Stack、Elasticsearch、MinIO 独立存储栈 |
| `deploy/.env.example` | 环境变量清单 |
| `deploy/local/.env.full.example` | 完整部署环境变量模板 |
| `deploy/ragflow-postgres.env.example` | RAGFlow 使用独立 PostgreSQL 数据库的环境变量基线 |
| `contracts/*` | RAGFlow 响应、Fixture 覆盖矩阵和 SSE 事件契约样例 |
| `tools/validate_spec.py` | 校验必需文件、YAML/JSON、OpenAPI 引用与核心接口、数据库核心表 |

## 4. 冲突时的优先级

实现过程中出现描述冲突时，按以下顺序处理：

1. `openapi/rag-api-v1.yaml`：对外 API 字段、类型和状态码。
2. `database/schema.sql`：数据库字段、索引和约束。
3. `docs/04-错误码说明.md`：错误码及重试语义。
4. `docs/03-RAGFlow接口映射.md`：RAGFlow Adapter 契约。
5. `docs/01-RAG系统详细设计与开发规范.md`：总体设计。
6. 其他说明和示例。

发现冲突时不得静默选择；应先修正规格，再继续实现。

## 5. Codex 推荐执行顺序

将整个 `rag-development-spec` 目录提供给 Codex。Codex 在同一仓库、同一工作区
和同一台电脑上顺序实施，不应一次性生成所有代码，也不应拆成互不一致的两套
实现。按以下阶段开发并在每阶段运行测试：

1. M0：建立仓库、SecurityContext、ScopedRepository、配置、Profile/Cache Bootstrap、领域模型、数据库迁移、错误协议和 OpenAPI 骨架。
2. M1：部署 RAGFlow，完成共享上传暂存、知识库、文档、任务和 Adapter 契约测试。
3. M2：完成纯检索、Context、LLM、非流式回答和 SSE。
4. M3：完成引用校验、拒答、精确缓存、语义缓存、基础查询记录和反馈。
5. M4：完成补偿、超时/有限重试、依赖降级、`up --build` 统一 Compose、架构/依赖预检和第一期验收。

每个阶段均要求：代码格式检查通过、静态检查通过、自动化测试通过、数据库迁移可前进、服务能启动、阶段验收用例有证据。完整压测、混沌、自动质量评测和灾备演练属于后续阶段。

## 6. 实现前必须替换的占位内容

- 当前 Direct Identity 的 Tenant/User 由每次请求显式提交；所有服务密钥必须由安全
  配置或环境变量提供。生产 Identity 尚未接入。
- 所有生产镜像必须从普通 Tag 固定为镜像 Digest。
- Embedding、Reranker、LLM 的具体模型名称需要在 PoC 后写入模型配置。
- 解析、检索和语义缓存相似度阈值必须由使用方使用真实文档和问题手动校准；第一期先提供可配置的保守默认值、开关、Mock/Fake 测试和命中明细。
- 生产域名、证书、资源限制、备份位置和告警接收人由部署环境补充。
- macOS/Apple Silicon 使用 RAGFlow x86 镜像时必须记录模拟运行 Smoke；正式
  部署优先使用已验证的 x86_64 Linux 或自行构建并验证的 ARM64 镜像。

## 7. 完成定义

“代码已生成”不等于项目完成。只有功能闭环、Security Scope 隔离、工程测试、完整 Compose Smoke、故障绕过和手动验收均符合 `docs/07-测试与验收规范.md` 与 `docs/11-第一期测试用例与验收清单.md`，才可宣布第一期完成。自动质量评测、完整性能和混沌门禁属于后续阶段。
