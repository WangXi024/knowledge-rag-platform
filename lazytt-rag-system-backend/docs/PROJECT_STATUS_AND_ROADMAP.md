# 项目完成度、未完成项与后续路线

更新时间：2026-08-11

评估基线：`feature/xi-wang_lazyx/6/rag-system-development-testing`，源码基线
`ab8eba5`

正式普通用户端按同级 `lazytt-frontend-console` 仓库的 `54409d2` 提交评估；后端与
前端使用各自仓库独立版本，部署时通过同源 BFF 和 Gateway API 连接。

规格基线：`rag-development-spec/` 1.4.0

## 1. 文档目的

本文从代码、自动化测试、真实联调和生产交付四个层次说明当前项目状态。它回答
以下问题：

1. M0～M4 已经实现了什么；
2. 哪些内容经过自动化测试；
3. 哪些内容已经与真实 RAGFlow、模型服务和数据组件联调；
4. 哪些内容尚未完成或只完成了部分；
5. 下一阶段应按什么优先级继续改进。

“代码存在”“测试通过”“真实联调通过”和“生产可用”是四种不同状态，本文不把
它们混为一谈。

## 2. 总体结论

当前项目已经形成可独立运行的第一期 RAG Server。若只看本地演示和核心 RAG 主链路，
完成度约为 **90%～95%**；若以包含身份接入、高可用、监控、灾备、容量和合规的完整
企业级 RAG 平台为目标，当前综合完成度约为 **75%**。本地开发和技术集成环境已经
可用，但尚不能宣称完整生产就绪。

| 评估维度 | 当前估算 | 结论 |
|---|---:|---|
| M0～M4 核心功能代码 | 约 95% | 主链路已经实现 |
| 自动化工程验收 | 约 95% | 当前收集 229 项后端测试；本次 218 通过、11 项环境跳过 |
| 真实技术环境联调 | 约 85% | 历史模型基线已验证；通用模型配置待 Docker 恢复后复验 |
| 第一期严格验收门禁 | 约 80%～90% | 使用方真实数据手动验收尚未完成 |
| 生产上线准备 | 约 65%～75% | 仍需安全、端口、镜像、灾备、容量和监控收敛 |

当前可以用于：

- 上游系统通过 JWT 调用独立 `/v1` API；
- 本地或受控集成环境的知识库、文档、检索和问答联调；
- 通过 3001 用户端注册本地测试账号、上传文档/图片/音视频并进行带引用问答；
- 使用最小非业务 Fixture 进行回归测试；
- 使用方拿自有、已批准文档执行正式业务验收。

当前不应直接视为：

- 已完成真实业务质量验收的系统；
- 已完成生产安全加固的部署包；
- 已完成高可用、容量、完整灾备和长期压测的生产平台。

## 3. 当前运行基线

### 3.1 自研服务

| 服务 | 职责 | 当前状态 |
|---|---|---|
| RAG Gateway | Direct Tenant/User、TENANT/SYSTEM 共享知识库、检索、问答、缓存、引用 | 已实现；本轮 Docker 未启动 |
| RAG Worker | 上传、解析、删除、重试、取消、补偿、Revision 更新 | 已实现并运行 |
| RAG Scheduler | 周期派发待处理 Job、清理过期暂存文件 | 已实现并运行 |
| Migration | 纯 SQL 初始化/升级两个数据库 | 已实现，一次性容器 |
| Bootstrap | 模型预检、Profile 导入、语义缓存索引校验 | 已实现，一次性容器 |
| 开发控制台 | 34 个 Gateway 接口、Profile、文档版本和原始响应验收 | 已实现，端口 3000 |
| 普通用户端 | Tenant/User 联调身份、共享知识只读浏览、并行流式问答 | 已实现参照快照，端口 3001 |

### 3.2 外部和基础设施

| 组件 | 当前用途 | 当前配置 |
|---|---|---|
| RAGFlow | Dataset、Document、Parse、Chunk、Retrieval | 官方 `v0.26.4` |
| RAGFlow PostgreSQL | RAGFlow 内部关系数据 | PostgreSQL 18.4 |
| Elasticsearch | RAGFlow 全文与向量索引 | 8.11.3 |
| MinIO | RAGFlow 对象存储 | 本地持久卷 |
| App PostgreSQL | 自研业务状态、映射、Job、查询、引用和反馈 | PostgreSQL 18.4 |
| 统一 Redis Stack DB 0 | 精确缓存、语义向量缓存 | Redis Stack 7.4 |
| 统一 Redis Stack DB 1 | RAGFlow 内部缓存/队列 | Redis Stack 7.4 |
| 统一 Redis Stack DB 2 | Celery Broker、摄取/维护队列和健康探测 | Redis Stack 7.4 |
| 通用 Chat/Vision API | Query Rewrite、答案、文档图片 OCR、视频抽帧理解 | OpenAI-compatible HTTPS |
| 通用 Embedding API | RAGFlow、查询和语义缓存向量 | OpenAI-compatible HTTPS |
| 可选扩展 API | Cross-Encoder Rerank、音视频 ASR | 项目约定 `/rerank` / `input_audio` |

模型职责固定、供应商可配置：

- Chat/Rewrite、Embedding、文档 Vision、媒体 Vision 分别使用三元组；
- Bootstrap 真实探测 Chat 协议覆盖、Embedding 维度、文档 OCR 和媒体图片理解；
- 音视频使用配置的 ASR，视频有限抽帧只调用媒体 Vision；
- Rerank 使用通用真实 Cross-Encoder 协议；故障时明确告警并保留向量、BM25 和
  加权 RRF 顺序。

上述通用配置已经完成代码、Profile、Compose、Bootstrap 和 Mock 回归；但本轮 Docker
守护进程不可用，尚未重新执行完整云端/RAGFlow 生命周期，因此不能仅凭本机已有私有
配置描述为当前代码基线的全链路真实联调通过。历史数据库 Profile 行只为已有引用保留，
新种子不再包含厂商专属 Profile；不同模型或维度不能写入同一缓存/知识库索引。

## 4. M0～M4 完成情况

### 4.1 M0：契约、骨架和安全底座

状态：**已完成**

已实现：

- Python 3.12、FastAPI、Pydantic、SQLAlchemy 2、psycopg 3、版本 SQL、Celery；
- `uv` 依赖锁定、Ruff、Mypy、pytest、coverage 和 GitHub Actions；
- 统一领域错误、统一 API 错误响应、Request ID；
- OpenAPI DTO、Domain Model 和 Ports；
- JWT 离线公钥/JWKS 验证；
- 只允许配置的非对称算法，拒绝 `none`；
- 从 `iss + tenant_id + rag_scope_id` 派生不可伪造的
  `security_scope_id`；
- 所有 Repository 方法显式接收 Scope；
- 8 张应用表的初始 Migration，以及后续层级索引、实体关系和文档版本迁移；当前共
  12 张应用表；
- Profile 幂等 Bootstrap 和 checksum 冲突阻断；
- Redis 语义缓存索引 Bootstrap 和模型/维度一致性检查；
- Live、Ready、Version、Capabilities 接口。

验证证据：

- OpenAPI 运行时 Path/Method 与规格完全一致；
- PostgreSQL Scope Repository 集成测试通过；
- JWT 缺失、过期、算法、Claim 和生命周期测试通过；
- Profile 幂等导入、冲突和必需引用测试通过。

### 4.2 M1：离线知识入库

状态：**已完成**

已实现：

- 知识库创建、列表、详情、修改和异步删除；
- 文档流式上传、扩展名校验、双扩展名防护；
- `.uploading -> .ready` 原子发布；
- 文件大小、SHA-256 和安全文件名；
- PDF、Office、图片和音视频文件族校验，图片/媒体魔数与 MIME 一致性检查；
- Gateway/Worker 共享暂存 Volume；
- 文档上传、解析、重新解析、重试、删除；
- 文档不可变版本历史、候选独立解析、安全发布、退役版本回滚和逻辑文档 Revision；
- 发布/回滚通过 RAGFlow 官方文档状态接口切换整份物理文件及其全部 Chunk；发布
  失败时重新打开旧物理文档并关闭候选，删除逻辑文档时清理全部物理版本；
- Job 查询和取消；
- Job 状态机、有限重试、指数退避、租约和租约续期；
- Worker 异常恢复、失败状态记录和过期暂存清理；
- RAGFlow Dataset/Document/Parse/Chunk Adapter；
- PDF 默认 DeepDoc、Office 使用内置 Office Parser、图片使用 Picture Parser；
- 音视频由 FFmpeg/ffprobe 校验、分段和有限抽帧，配置的 ASR 转写后生成
  带时间戳 Markdown，再由 `media-default@1` 交给 RAGFlow 通用解析；
- 媒体派生文件写入 Job 恢复点；RAGFlow 上传重试不会重复调用 ASR，外部 Document
  身份仍是用户上传的原媒体；
- 新建知识库保留 `general-256@2` 安全默认，新文件按正文、表格、
  视觉和媒体文件族自动选择 384/192/192/320 Token 的不可变 Profile，
  并在解析前向 RAGFlow 文档写入对应重叠百分比；
- 知识库 Parser 是上传默认值而非不可改变的文档锁定值；通用 API 可按文档显式覆盖，
  未显式选择时先保留兼容默认 Parser，再按配置规则自动路由 PDF、Office、图片和媒体；
- 上传到 RAGFlow 后通过官方下载接口回读并核对大小与 SHA-256；
- Office 响应出现已知的名称/大小规范化差异时按受控规则兼容，损坏副本安全删除并
  有限重传；
- 本地 ID 与 RAGFlow ID 双向映射；
- 解析/删除成功后递增知识库 Revision；
- 删除审计记录和不可访问状态。

验证证据：

- Worker 跨容器读取暂存文件并校验 Hash；
- RAGFlow Dataset、上传、解析、Chunk、检索、删除真实 Smoke 通过；
- Job 抢占、租约恢复、取消和失败路径测试通过；
- 越权资源在调用 RAGFlow 前返回 404。

### 4.3 M2：在线检索和生成

状态：**核心能力已完成，主动取消控制仍可加强**

已实现：

- Query 规范化和结构化双语 QueryPlan；
- Rewrite 失败后回退原问题；
- 对全部知识库和可选文档执行批量 Scope 校验；
- 从文档 Metadata 聚合知识库语言，不把全部文档加载进应用内存；
- 文档解析完成后抽样原始 Chunk，自动补全标题、文件类型、语种、页数和表格迹象；
- 根据问题语言和中文/英文/混合/未知知识库生成多条向量与 BM25 路线；
- 各路线并发调用 RAGFlow，新请求按 `hybrid-structure-aware@6` 和问题类型
  动态选择 Top-K，并在侧车向量导航取 Top-N 前应用 0.40 相似度门槛，再执行
  加权 RRF；旧 `@1`～`@5` 保留用于回滚；
- 解析后按标题/长度动态构建 Parent-Child，为表格重复侧车表头，并为
  Parent/Section/Summary 增加仅供 Embedding 的文档上下文前缀；同时保留
  抽取式摘要、实体和显式关系侧车索引；命中侧车后仍回读
  RAGFlow 原始 Chunk，避免合成摘要或图节点成为不可核验引用；
- RRF 后执行无效候选过滤、正文 Hash 去重、Embedding 语义近重复删除、相邻 Chunk
  合并和单文档数量限制；
- 按问题类型选择 8～16 个候选进入可选真实 Cross-Encoder Rerank；
- Context 数量和估算 Token 预算；
- OpenAI-compatible LLM 非流式和流式适配；
- 有证据回答、无证据拒答；
- Citation 构建、标签校验和一次修复；
- REST Answer 和 SSE Answer；
- SSE sequence、heartbeat、error、usage、citation 和 completed；
- 请求总时限、模型有限重试、并发 Semaphore；
- 客户端断开时取消正在等待的流式 LLM 调用。

普通用户端的历史会话只用于回看，不传入下一次问答。每个问题独立完成 QueryPlan、
缓存边界、检索和生成，因此不会因为切换会话改变检索结果。Gateway 契约仍保留历史
字段，供其他已授权上游按契约使用。

部分完成：

- `POST /v1/requests/{request_id}/cancel` 会把 QueryRecord 标记为
  `CANCELLED`，流式循环在下一业务事件边界检查并结束；
- 预留的 `redis.control_db` 尚未用于请求取消标记；
- 对正在长时间等待首 Token 的请求，主动取消还不能像客户端断开一样立即取消
  底层 HTTP 请求；
- 非流式请求也没有进程内活动请求注册表来即时中断底层调用。

这不影响普通问答和超时保护，但属于下一步应优先补齐的控制面能力。

### 4.4 M3：第一期增强

状态：**已完成**

已实现：

- Query、Citation、Feedback 持久化；
- 精确缓存；
- Redis Stack 语义缓存；
- FLOAT32、当前已验证维度的 HNSW 向量索引；
- 模型、维度和索引版本绑定；
- Scope、知识库 Revision、Retrieval Profile、Prompt Profile、
  Model Profile、语言和历史摘要边界；
- TTL、原子 Pipeline Upsert 和命中计数；
- 缓存命中后重新检查 RAGFlow 健康；
- 缓存 Citation 所属知识库、文档和 READY 状态重验；
- 缓存读写故障时问答链路可绕过缓存；统一 Redis Stack 整体故障还会影响 Celery 与
  RAGFlow，Ready 必须报告不可用；
- 删除/Reparse 后通过 Revision 立即逻辑失效旧缓存；
- 基础结构化日志和阶段耗时记录；
- RerankPort、通用真实 Cross-Encoder Adapter、显式关闭和服务故障降级结果；
- 用户端显示精确缓存、语义缓存或直接问答来源，命中后仍执行引用和 Scope 重验。

验证证据：

- 精确和语义缓存真实写入、命中、TTL 和 Scope 隔离通过；
- 不同索引版本/模型边界不混用；
- 单纯缓存操作失败时正常 RAG 可继续；统一 Redis Stack 停止时 Ready 不可用；
- RAGFlow 不健康时不返回旧缓存答案；
- 引用重验和 Revision 失效测试通过。

### 4.5 M4：集成和部署

状态：**本地开发 Profile 已完成；生产 Profile 尚未完全收敛**

已实现：

- 单一 `compose.local.yml`；
- 15 个服务定义和 9 个命名 Volume；
- 自研 ARM64 非 root 镜像；
- 只有 RAGFlow Server/Task Executor 使用 `linux/amd64`；
- 禁止全局 `DOCKER_DEFAULT_PLATFORM`；
- Migration -> Bootstrap -> Gateway/Worker/Scheduler 启动依赖；
- PostgreSQL、Redis、Elasticsearch、MinIO、RAGFlow 健康检查；
- 通用 Chat/OCR/Embedding、可选 Rerank/ASR、Embedding 真实维度以及 RAGFlow/API
  Key 启动预检；
- Profile 和 Redis 索引幂等 Bootstrap；
- Compose 静态预检；
- Git 忽略环境文件、随机本地密码和稳定本地 API Key；
- RAGFlow UI/API 仅绑定 `127.0.0.1`；
- 本地普通用户端：邮箱密码注册、四个隔离知识空间、文件轮询/重试/删除；
- 用户端两个会话并行流式回答、安全 Markdown 渲染、默认收起引用栏；
- 非破坏性启停脚本；
- Gateway E2E、图片生命周期、RAGFlow 生命周期、模型、韧性和 PostgreSQL
  Backup/Restore Smoke；
- 镜像来源、Tag、Image ID 和许可证风险清单；
- 部署、恢复和验收文档。

尚未达到生产 Profile 的部分：

- 生产默认应只发布 Gateway，当前本地 Profile 为调试保留了 RAGFlow
  `127.0.0.1` UI/API；
- Compose 仍使用本地可读 Tag，生产应固定审核后的 Registry Digest；
- Secret 仍来自本地忽略文件，生产应接入 Secret Manager；
- TLS、正式域名、正式 JWKS、告警和生产备份位置尚未接入。

## 5. 第一期功能需求覆盖矩阵

| 需求 | 当前状态 | 说明 |
|---|---|---|
| FR-KB-001 | 已完成 | KB 创建、列表、详情、修改、异步删除 |
| FR-DOC-001 | 已完成 | 流式上传、暂存、Hash、解析、重解析、安全版本更新、回滚、重试、删除 |
| FR-RET-001 | 已完成 | 当前 Scope 内多知识库混合检索 |
| FR-ANS-001 | 已完成 | REST 和 SSE RAG 问答 |
| FR-CIT-001 | 已完成 | 文档、Chunk、页码/坐标、摘录引用 |
| FR-REF-001 | 已完成 | 无证据和引用失败受控拒答 |
| FR-CACHE-001 | 已完成 | 精确/语义缓存和故障绕过 |
| FR-IDEM-001 | 已完成 | 创建、上传、任务、删除、反馈幂等 |
| FR-SEC-001 | 已完成 | JWT 和 SecurityContext |
| FR-SEC-002 | 已完成 | 所有可寻址资源 Scope 过滤 |
| FR-SEC-003 | 已完成 | 越权 404 且不调用业务下游 |
| FR-SEC-004 | 已完成 | 缓存与幂等不跨 Scope |
| FR-DEP-001 | 部分完成 | 单一 Compose 已有；本地调试仍发布 loopback RAGFlow 端口 |
| FR-CFG-001 | 已完成 | Profile 幂等导入，checksum 冲突阻止启动 |
| FR-DEP-002 | 已完成 | 仅 RAGFlow amd64，自研镜像原生 ARM64 |

## 6. 自动化测试现状

当前后端测试集合共 228 项。本次供应商无关模型迁移与完整物理文档状态切换回归结果为
**218 通过、11 项跳过**；
跳过项是需要显式提供 `TEST_DATABASE_DSN` 或 `TEST_CACHE_REDIS_URL` 的真实
PostgreSQL/Redis Integration 测试。2026-07-27 M0～M4 验收基线的
Domain/Application 核心分支覆盖率为 81.25%，满足当时 80% 门禁；本次实现没有
重新生成完整 coverage 报告。

| 测试层 | 覆盖内容 |
|---|---|
| Unit | Domain 状态机、Application 编排、Adapter、缓存、JWT、暂存 |
| API | 请求/响应、认证、Scope、未知字段、SSE、系统接口 |
| Repository | PostgreSQL 真实约束、Scope、租约、幂等、查询记录 |
| Redis Integration | Redis Stack 索引、原子写入、TTL、版本和 Scope |
| Contract | 运行时 OpenAPI 与规格 Path/Method 完全一致 |
| External Smoke | RAGFlow、云端模型、Gateway E2E、依赖降级和恢复 |
| User Frontend | 构建、18 项渲染/行为测试、TypeScript、ESLint |

质量门禁：

- `rag-development-spec/tools/validate_spec.py`：通过；
- Ruff：通过；
- Mypy strict：通过；
- pytest：本次 218 通过、11 项环境跳过；静态测试检查领域枚举与 ORM 约束完全一致，
  另有一项真实 PostgreSQL Integration 专门验证数据库允许
  `PARENT/SECTION/SUMMARY` 三类节点；
- coverage：81.25% 是 2026-07-27 历史验收值，本次未重算；
- 用户端：构建、18/18 测试、`tsc --noEmit` 和 ESLint 通过；
- 跟踪文件密钥扫描：通过；
- Compose 静态架构和端口检查：通过。

## 7. 已真实联调内容

以下是旧 Ollama 运行基线已经完成的本机真实验证，用于保留历史证据；它不能替代
当前通用模型配置的真实联调：

- RAGFlow v0.26.4 使用 PostgreSQL 启动；
- RAGFlow Server 和 Task Executor 健康；
- RAGFlow 容器通过 `host.docker.internal:11434` 访问 Ollama；
- `qwen3:8b` 普通 Chat、结构化输出和 SSE；
- `bge-m3:latest` 单条和批量 Embedding；
- Embedding 实际维度为 1024；
- 通过 RAGFlow 官方 Provider/Instance/Model API 注册 Ollama Embedding；
- RAGFlow Picture Parser、`qwen3-vl:4b` 配置和合成图片生命周期；
- 真实 `bge-m3` Dataset 生命周期；
- Gateway 建库、上传、解析、Chunk、检索、答案、SSE、引用和删除；
- Exact/Semantic Cache、Scope 隔离、Revision 失效和 Feedback；
- DB 0 缓存故障绕过，以及统一 Redis 整体故障的明确不可用状态；
- RAGFlow 故障时 Ready 失败并拒绝返回旧缓存；
- RAGFlow/Gateway/Worker/Scheduler 重启后状态保留；
- App/RAGFlow PostgreSQL Dump 恢复到临时库并核对表数量；
- Fixture、临时数据库、临时测试容器和共享暂存文件清理。

历史默认 Rerank 曾使用合成候选完成真实 `/v1/rerank` 协议、下标映射和 Gateway
E2E 联调。当前通用 Adapter 保留同一协议，但更换服务后仍需重新真实验证；历史结果
不等于当前供应商的生产数据合规、长期 SLA 或效果评估已经完成。

当前迁移已经自动验证请求协议、流式解析、结构化返回、维度拒绝、RAGFlow Provider
Bootstrap 编排和密钥不落库。仍待 Docker 恢复后，对本机实际配置重新执行普通/
结构化/SSE、合成图片、可选最小音频、单条/批量 Embedding 及完整 RAGFlow Dataset
生命周期。

## 8. 尚未完成：第一期正式验收项

### 8.1 使用方真实数据手动验收

这是当前无法自动替代的主要验收缺口。需要由使用方提供至少一份可公开或已批准
用于测试的业务文档，完成：

| ID | 待验收内容 |
|---|---|
| MAN-001 | 至少一种真实目标格式的解析质量和 Chunk 抽样 |
| MAN-002 | 可回答问题的答案和 Citation 正确性 |
| MAN-003 | 无依据问题是否正确拒答 |
| MAN-004 | 相似问法语义缓存阈值和引用 |
| MAN-005 | Reparse/删除后的 Revision 与缓存失效 |
| MAN-006 | 第二 Scope 对第一 Scope 全部资源的交叉访问 |

在这组结果记录并签字前，不应声称“业务验收 100% 完成”。

### 8.2 主动请求取消的低延迟控制

当前代码已经处理：

- 客户端断开导致的协程取消；
- 总请求超时；
- 流式事件之间检查数据库取消状态；
- 离线解析 Job 取消并调用 RAGFlow Stop Parse。

仍需补齐：

- 使用 Redis Stack DB 2 写入短 TTL 取消标记；
- Gateway 内活动请求注册和跨实例取消通知；
- `POST /v1/requests/{id}/cancel` 直接取消底层 LLM HTTP 请求；
- 非流式 Answer 的取消传播；
- 首 Token 长等待期间的取消延迟测试。

### 8.3 Rerank 生产评估

状态：**已实现并完成最小真实联调**。

- 新增 `OpenAICompatibleRerankAdapter`，调用项目约定的真实 `/rerank`；
- 根据响应 `index` 把乱序结果恢复为与输入候选严格对齐的分数；
- Bootstrap 使用两条合成文本验证 Key、配置模型、完整下标和有限数值；
- `/v1/capabilities` 返回 `rerank=true`，检索候选包含数值型 `rerank_score`；
- 401、429、临时 5xx、超时或无效协议都稳定降级到原加权 RRF，不返回 500；
- 没有把 Ollama Embedding 误装为 Reranker，也没有用余弦相似度伪造分数。

仍未完成的是生产供应商评估、敏感数据合规、预算/配额告警、长期延迟和可用性压测，
以及生产 Secret Manager 接入；当前真实 Key 只保存在 Git 忽略的本地配置中。

## 9. 尚未完成：生产化工作

以下多数属于规格明确的后续生产建设，不是当前本地 Profile 的缺陷修补：

### 9.1 部署与供应链

- 新增不发布 RAGFlow UI/API 的生产 Compose Override；
- 所有生产镜像固定 Registry Digest；
- 生成和归档 SBOM、镜像签名、漏洞扫描；
- 完成 Redis Stack、Elasticsearch、MinIO 分发镜像许可证审核；
- 正式 Registry、发布版本和回滚制品；
- 用生产域名和 TLS 终止替代本地 loopback。

### 9.2 Secret 和身份

- 接入 Secret Manager/KMS；
- 接入正式 JWKS 并验证轮换；
- 区分开发、测试、预发和生产配置；
- RAGFlow 管理员和 API Key 定期轮换；
- 增加管理面网络策略和审计；
- 增加高风险 Token 重放检测时，再使用 `jti` 控制能力。

### 9.3 灾备

已完成 PostgreSQL Dump/Restore，尚需：

- MinIO Bucket Version/Snapshot 恢复演练；
- Elasticsearch Snapshot Repository 恢复演练；
- Redis AOF 恢复验证；
- 按 PostgreSQL -> MinIO/Elasticsearch -> RAGFlow -> Redis -> App 的
  完整顺序恢复；
- 恢复后映射对账、Smoke 和两 Scope 复测；
- 明确目标环境 RPO/RTO。

### 9.4 容量与高可用

- 使用真实文档规模进行解析吞吐和索引容量测量；
- Gateway、Worker 并发和队列堆积压测；
- 云端模型/RAGFlow/Elasticsearch 的瓶颈、配额和限流分析；
- 多 Worker、进程重启、滚动升级和重复 Job 验证；
- 跨主机部署和故障域设计；
- 正式容量、P95、RPO 和 RTO 基线。

### 9.5 可观测性

当前有 JSON 日志、Request ID、Job ID、稳定错误码和阶段耗时。后续仍需：

- OpenTelemetry Trace；
- Prometheus 指标；
- Dashboard 和告警；
- 队列堆积、解析失败率、RAGFlow 延迟、LLM 首 Token、缓存命中率指标；
- 日志采集、脱敏验证和保留策略。

## 10. 已识别的代码级改进项

### 10.1 P0：请求取消控制面

实现 `CancellationPort`：

1. Redis Stack DB 2 写入 `cancel:{security_scope_id}:{request_id}`；
2. 设置短 TTL，避免永久垃圾键；
3. Streaming/Non-streaming LLM 调用同时监听取消信号；
4. 活动 `httpx` 请求收到取消后立即关闭；
5. 多 Gateway 实例之间通过 Redis Pub/Sub 或轮询同步；
6. 保留 QueryRecord 作为最终事实和审计记录。

### 10.2 P1：缩短数据库事务边界

当前 FastAPI 依赖在一个 `session.begin()` 内构造 Service，并可能跨越 RAGFlow、
Embedding 和 LLM 网络调用。虽然当前测试通过，但长外部调用会占用数据库连接和
事务资源。

建议：

- Scope/映射读取使用短事务；
- 外部网络调用在事务之外执行；
- 最终状态和查询记录使用独立短事务提交；
- 保持乐观锁/row_version 处理并发冲突；
- 对离线 Worker 同样拆分“领取 Job”“外部执行”“提交状态”事务。

### 10.3 P1：复用长生命周期 HTTP/Redis 客户端

当前每个请求会创建并关闭 RAGFlow、LLM、Embedding 和 Redis Stack 缓存客户端。
建议放入 FastAPI lifespan：

- 统一连接池；
- 减少 TLS/TCP 建连；
- 为并发、超时和关闭顺序提供统一管理；
- 使 Ready 探测与业务 Adapter 复用相同连接策略。

### 10.4 P1：提高薄弱模块覆盖率

总覆盖率达标，但 `application/ingestion_worker.py` 分支覆盖率约 64%。优先补充：

- 删除知识库部分失败和补偿；
- 租约丢失；
- 解析轮询超时；
- Retry Wait 达到最大次数；
- Worker 进程在外部成功、本地提交前退出；
- 乐观锁冲突后的恢复。

### 10.5 P1：扩大真实格式验证

当前已有最小文本和合成图片自动 Fixture。下一步至少选择经过批准的：

- PDF；
- DOCX；
- PPTX；
- XLSX/CSV；
- 中文 Markdown/TXT。

分别记录 Parser、页码/坐标、Chunk、耗时和答案引用。

大型 Excel 还需要单独记录工作表规模、生成 Chunk 数、Embedding 时间和内存峰值，
避免只用文件 MB 判断处理成本。

### 10.6 已完成：精确重复文件去重

`content_sha256` 既用于暂存/RAGFlow 上传副本完整性验证，也已经用于下面的业务规则：

```text
同一 security_scope_id + 同一知识库 + 相同 content_sha256 + 文档未删除
= 拒绝新的重复上传，并在 RAG-DOC-012 错误详情中返回 existing_document_id
```

Repository 使用当前事务的 PostgreSQL advisory lock 关闭并发上传竞争；文档软删除后
允许重新上传相同内容。该规则只做原始字节完全相同的精确去重，不用向量相似度把
不同版本合同或说明书误判成同一文档。在线“语义候选去重”是另一项功能，它只减少
一次检索结果中的近重复 Chunk，不删除用户文件。

### 10.7 已完成：Gateway 侧车索引优化

在不修改 RAGFlow 源码的前提下，应用 PostgreSQL 现保存 Parent/Summary、实体与显式
关系导航索引。新文档解析后自动建立；既有 READY 文档需要手动重解析一次才会获得
这些侧车节点。每个节点都保存真实 RAGFlow Chunk ID，在线路线命中后回读原文再进入
Rerank、上下文预算和引用校验。

当前是轻量实现而不是完整 GraphRAG：实体与关系由确定性中英文规则抽取，不执行
跨文档实体消歧、隐含关系推理、图路径查询或 Neo4j 存储。超大文档侧车构建最多读取
前 1,000 个 Chunk；基础 RAGFlow 全文检索仍覆盖全部 Chunk。

### 10.8 P2：外部集成测试自动化

当前 GitHub Actions 运行 PostgreSQL、Redis Stack、Unit/API/Contract/
Integration；RAGFlow/云端模型真实 Smoke 主要在受控本机执行。

建议增加：

- 自托管 Runner 的夜间 RAGFlow/云端模型 Smoke；
- 失败保留脱敏日志和报告；
- 不使用真实业务数据；
- 将 RAGFlow API 兼容性测试和升级验证独立成 Job。

### 10.9 P2：环境配置简化

当前保留 `.env.ragflow` 和 `.env.full` 两套本地文件。它们都被 Git 忽略且工具
可以生成，但长期可能发生配置漂移。

建议：

- 明确 `.env.full` 为唯一完整运行来源；
- `.env.ragflow` 只作为前置环境调试兼容入口；
- 增加两者关键模型、密码引用和镜像 Tag 一致性校验；
- 生产改为分层配置和 Secret 引用。

## 11. 推荐下一步路线

### 阶段 A：完成正式第一期验收

优先级：最高

1. 使用方准备已批准业务 Fixture；
2. 执行 MAN-001～MAN-006；
3. 记录 Parser、问题、答案、引用、阈值和耗时；
4. 修复业务验收发现的问题；
5. 填写验收清单和签字记录。

完成标志：技术闭环和业务闭环都通过。

### 阶段 B：补齐取消控制并优化事务/连接

优先级：高

1. 实现 Redis CancellationPort；
2. 中断非流式和流式底层 LLM 请求；
3. 缩短数据库事务；
4. 生命周期复用 HTTP/Redis 客户端；
5. 增加并发、取消和连接池测试。

完成标志：主动取消在首 Token 等待期也能快速生效，且数据库连接不被长调用占用。

### 阶段 C：生产 Profile 收敛

优先级：高

1. 关闭 RAGFlow 宿主机发布端口；
2. 固定 Registry Digest；
3. 接入 TLS、正式 JWKS、Secret Manager；
4. 补充 SBOM、签名和漏洞扫描；
5. 明确生产备份位置、RPO/RTO 和告警。

完成标志：生产配置不依赖开发机文件，云端 Key、配额、网络和合规策略全部受控。

### 阶段 D：灾备、容量和观测

优先级：中

1. 完整存储恢复演练；
2. 性能和容量测试；
3. 多 Worker/滚动升级验证；
4. OpenTelemetry、指标、Dashboard 和告警；
5. 基于真实运行数据确定扩容策略。

### 阶段 E：可选能力

优先级：按业务需求

- Rerank 供应商多路选择、效果评测和生产合规；
- 自动化质量评测；
- A/B Prompt/Profile；
- 更复杂的数据源同步；
- 完整 ACL、共享授权或多 Scope 并集；
- 独立管理前端。

这些能力在第一期规格中属于延期或明确不负责项，不应为了“看起来完整”而提前
引入。

## 12. 下一轮完成定义建议

建议下一轮只有同时满足以下条件才标记为完成：

- MAN-001～MAN-006 有真实记录和验收结论；
- 主动取消可中断流式与非流式底层 HTTP 调用；
- 生产 Compose 默认只暴露 Gateway；
- 生产镜像使用审核后的 Digest；
- Secret/JWKS/TLS 使用目标环境能力；
- MinIO、Elasticsearch 和 PostgreSQL 完整恢复演练通过；
- 容量、P95、RPO、RTO 有目标环境测量值；
- 新增测试后保持自动化全绿和核心覆盖率不低于 80%。

## 13. 相关文档

- [文档导航与本次同步说明](README.md)
- [本地部署与使用手册](USER_MANUAL.md)
- [新人学习指南](NEWCOMER_LEARNING_GUIDE.md)
- [双语 QueryPlan 与语言感知检索](MULTILINGUAL_RETRIEVAL.md)
- [第一期验收报告](ACCEPTANCE_REPORT.md)
- [代码结构详解](CODEBASE_STRUCTURE.md)
- [部署与运维说明](../deploy/local/README.md)
- [规格需求与范围](../rag-development-spec/docs/00-需求与范围说明.md)
- [详细设计与开发规范](../rag-development-spec/docs/01-RAG系统详细设计与开发规范.md)
- [测试用例与验收清单](../rag-development-spec/docs/11-第一期测试用例与验收清单.md)
