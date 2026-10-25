# 第一期 M0～M4 验收报告

验证日期：2026-07-27

分支：`feature/xi-wang_lazyx/6/rag-system-development-testing`

状态：本地开发 Profile 已完成自动测试与真实联调；使用方业务数据手动验收待办。

> 历史快照：本报告记录 2026-07-27 当时的模型和 Profile，不是当前新部署配置。
> 当前模型接入以 [ADR-0004](ADR-0004-OPENAI-COMPATIBLE-MODELS.md) 和
> [用户手册](USER_MANUAL.md) 为准。

## 1. 已实现

- M0：Python/FastAPI/SQLAlchemy/Alembic/Celery 工程底座、统一错误、JWT、
  `SecurityContext`、Scoped Repository、Profile/Cache Bootstrap。
- M1：知识库、文档、Job、共享暂存卷、Worker、租约/有限重试/取消/补偿、
  RAGFlow Dataset/Document/Parse/Chunk Adapter。
- M2：Query Rewrite、向量/BM25/RRF、Context、Ollama LLM、引用、拒答、
  非流式与 SSE。
- M3：查询/引用/反馈记录、精确缓存、Redis Stack 语义缓存、Scope/Revision/
  Profile/模型/语言/历史边界、引用重验和故障绕过。
- M4：非 root 自研镜像、统一 Compose、Migration、自动 Bootstrap、完整 Ready、
  静态预检、E2E/恢复/备份 Smoke、安全检查和运维文档。

数据库 Migration Revision：`0001_initial_rag_app`，应用 Schema 共 8 张业务表。

## 2. 已自动测试

- 规格解析：OpenAPI 27 个操作、SQL 8 张表，全部通过。
- OpenAPI Diff：运行时 Path/Method 集合与规格完全一致。
- Unit/API/Contract/Integration 共 105 项通过；PostgreSQL 18.4、Redis Stack
  和 Fake/Mock 外部端口均纳入测试。
- Domain/Application 核心分支覆盖率 81.25%，通过 80% 门禁。
- JWT 错误、两个 Scope、Scoped Repository、幂等、状态机、Profile checksum、
  RAGFlow Adapter、缓存污染/维度/TTL/并发、请求总时限与取消传播均有断言。
- SSE 事件序号、空闲心跳、客户端断开取消和终止 `error` 事件均有自动化断言。
- `tools/check_tracked_secrets.py` 比较本机敏感值与全部 Git 跟踪文件，并检查
  忽略规则和本地权限。

## 3. 已真实联调

- 官方 RAGFlow v0.26.4 + PostgreSQL 18.4 + Elasticsearch + MinIO + Valkey
  在 Apple Silicon 上运行；只有两个 RAGFlow 容器使用 amd64。
- RAGFlow 容器访问宿主机 Ollama；`qwen3:8b` 普通/结构化/SSE 调用通过。
- `bge-m3:latest` 单条与批量 Embedding 均真实返回 1024 维。
- 通过官方 Provider/Instance/Model API 持久注册 RAGFlow Ollama Embedding；
  没有直接改数据库。
- 真实 `bge-m3` 完成 Dataset -> Upload -> Parse -> Chunk -> Retrieve ->
  Delete；重启后再次通过。
- Gateway 最小 Fixture 完成建库、上传、解析、检索、问答、SSE、Exact/
  Semantic Cache、Scope 隔离、Revision 失效、Feedback 和删除。
- 旧三实例拓扑中曾验证独立 Cache Redis 故障时可以绕过缓存；当前统一 Redis Stack
  同时承载缓存、RAGFlow 状态和 Celery 队列，整个实例不可用时 Ready 必须返回 503，
  不能继续把它描述成仅缓存降级。单条缓存命令或 Embedding 失败仍可安全绕过缓存。
- RAGFlow Server 停止时 Ready 返回 503，已缓存问题也返回稳定
  `RAG-RF-001`，不返回旧答案。
- RAGFlow/Gateway/Worker/Scheduler 非破坏性重启后，模型、映射、索引和缓存
  保留。
- App/RAGFlow PostgreSQL Dump 分别恢复到临时数据库并核对 8/42 张表；临时
  数据库全部删除。
- 最近 30 分钟关键日志扫描未发现 MySQL 连接、Traceback、Fatal、密码、API
  Key 或 Authorization 泄露。

所有自动 Fixture 都是最小非业务文本。清理后应用侧活跃 Fixture 知识库为 0、
RAGFlow Fixture Dataset 为 0、共享暂存文件为 0，临时恢复库和独立 Redis
测试容器也已删除；没有执行 `down -v`。应用 PostgreSQL 的 `DELETED` 审计记录
和 Query/Feedback 按配置的保留期保存，失效缓存通过 revision 立即不可命中并
按 TTL 物理回收，这些不再是可访问的活动资源。

## 4. 镜像与架构基线

验收时实际运行的关键镜像：

| 镜像 | 本地 Image ID | 架构 |
|---|---|---|
| `local/lazytt-rag-service:dev` | `sha256:c2f2e4d5830ad607a16b743806ce0f4aea1e337d51df6110a2d86f2bcfa2c440` | arm64 |
| `infiniflow/ragflow:v0.26.4` | `sha256:16d24d1968ab59e2715a85d2590f1569c9539e0362344a42f3a23e8be06a655b` | amd64 |
| `postgres:18.4-bookworm` | `sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296` | arm64 |
| `redis/redis-stack-server:7.4.0-v8` | `sha256:798ab84d9f266936b034ab11c4d04a2b8e4b441884c5aa7d17ac951eefdf742a` | arm64 |
| `elasticsearch:8.11.3` | `sha256:58a3a280935d830215802322e9a0373faaacdfd646477aa7e718939c2f29292a` | arm64 |

生产发布必须把已审核 Tag 固定为 Registry Digest，并完成许可证/SBOM 审核；
本地 Image ID 不是可跨机器复现的 Registry Digest。

## 5. 尚未验证

- 使用方尚未提供真实业务文档、目标问题和质量标注，因此 `MAN-001`～
  `MAN-006` 的解析质量、答案正确性、拒答效果、Citation 业务准确性和阈值校准
  尚未验证。
- 当时配置的真实 Cross-Encoder 已通过合成请求、容器 Bootstrap 和 Gateway
  E2E 联调；当前通用端点仍需按所选供应商重新完成生产数据合规、长期 SLA、容量和
  费用评估。
- 未执行长期压测、容量测试、完整混沌测试、跨主机灾备、生产 TLS/Secret
  Manager/告警集成。这些属于生产上线前工作或规格明确的后续阶段。

## 6. 非生产限制

- 当前是单机 8 GiB 最小 Fixture Profile，不是生产资源建议。
- RAGFlow UI/API/Admin 虽只绑定 `127.0.0.1`，生产默认仍应完全取消发布。
- 本地 Ollama、环境文件 Secret 和开发 JWT 只适用于受控开发机。
- RAGFlow 官方启动会打印已有列初始化噪声；PostgreSQL 健康探测偶发
  `connection already closed` 后重试成功；最小英文 Fixture 可能打印
  `Load term.freq FAIL`，但实际 Parse/Chunk/Retrieval 均成功。
- App Worker 当前为单机 Celery/数据库租约恢复模型；正式容量、HA 和滚动升级
  需要部署环境验证。

## 7. 使用方下一步

1. 在 <http://127.0.0.1:8081> 登录时，从 Git 忽略的本地环境文件读取邮箱和
   随机密码；当前环境不需要再次初始化或创建一次性 Key。
2. 使用自有、已批准的测试文档完成 `rag-development-spec/docs/11` 中
   `MAN-001`～`MAN-006`，记录 Parser/Profile、问题、答案、引用和缓存阈值。
3. 生产上线前按 `deploy/local/README.md` 的目标生产 Profile 收敛端口、Secret、
   Registry Digest、备份位置、TLS、容量和告警。

## 8. 验收基线后的增量说明

更新时间：2026-08-04

本节记录 2026-07-27 验收之后合入的能力，**不会把后来的代码倒填成原验收日期已经
验证**。上面第 1～7 节仍是原 M0～M4 验收快照。

### 8.1 已实现的增量

- 普通用户端：本地注册登录、PDF/Office/图片/音视频四个隔离知识空间、文件轮询/重试/
  删除和只读 Chunk 浏览；
- 图片摄取：`picture-default@1`、RAGFlow Picture Parser、当时配置的视觉模型
  启动预检和合成图片 Smoke 工具；
- 音视频摄取：`media-default@1`、FFmpeg/ffprobe 分段与有限抽帧、当时配置的 ASR、
  时间戳 Markdown 和可恢复派生文件；
- 双语检索：`hybrid-default@2`、中英文 QueryPlan、知识库语言感知路线和加权 RRF；
- 真实 Rerank：当时配置的 Cross-Encoder 服务、启动探测、分数映射和
  故障回退；
- 用户端问答：问题无历史上下文、最多两个会话并行、SSE 隔离、Markdown 安全渲染、
  缓存来源和中性 Rerank 结果提示；
- Chunk：新知识库默认 `general-256@2`，约 256 Token，并向 RAGFlow 写入
  `overlapped_percent=10`；
- Office 上传恢复：兼容已知名称/大小规范化，复用同一次失败流程留下的精确远端文档，
  成功后清除旧错误；
- 上传完整性：通过 RAGFlow 官方下载接口回读文件并核对大小与 SHA-256，损坏时安全
  删除并有限重传；
- 小白用户本地部署手册和文档导航。

### 8.2 本次文档同步执行的自动检查

```text
后端：197 项已收集，186 passed，11 skipped
用户端：构建通过，16/16 测试通过，TypeScript 通过，ESLint 通过
```

11 项后端跳过项需要独立配置 `TEST_DATABASE_DSN` 或
`TEST_CACHE_REDIS_URL`。其中新增一项会在真实 PostgreSQL 中写入并回读
`PARENT/SECTION/SUMMARY`，验证 Migration 和 ORM 约束没有漂移。因此本次结果不能
描述为“197 项全部通过”，也不能替代
2026-07-27 的真实 PostgreSQL/Redis Integration 证据。

### 8.3 本次没有重新执行的真实联调

本次只是文档同步，没有重新启动或修改现有 Docker 数据卷，也没有重新执行：

- 当前通用模型端点/RAGFlow 全生命周期 Smoke；
- 合成图片真实 Parse/Chunk/Retrieval Smoke；
- 最小音视频真实 ASR/派生 Markdown/Parse/Chunk/Retrieval Smoke；
- 当前远端服务的真实付费 Rerank 请求；
- PostgreSQL Backup/Restore；
- Cache/RAGFlow 故障与恢复演练。

这些能力的最近一次真实结果仍以各自带日期的 `deploy/` 报告和 Smoke 输出为准。
生产业务数据的效果、容量、合规和长期稳定性仍未因此得到验证。

### 8.4 2026-08-01 检索与索引优化增量

本次在不修改 RAGFlow 源码、不删除 Docker 数据卷的前提下实现并自动验证：

- 同 Scope、同知识库、相同 SHA-256 的未删除文件精确去重，并关闭并发上传竞争；
- 检索候选 Embedding 语义近重复删除和相邻 Chunk 上下文合并；
- `hybrid-multigranular@4` 问题分类、动态 Top-K/Rerank/Context 规模；
- 解析成功后自动补全标题、文档种类、语种、页数、表格迹象、日期/年份、版本、组织
  和技术词等确定性结构化 Metadata；
- PostgreSQL Document Summary、Section、Parent-Child 多粒度侧车索引；既有轻量
  实体与显式关系导航保持不变，本次没有扩展成完整知识图谱；
- 所有侧车命中只导航并回读 RAGFlow 原始 Chunk，继续执行真实 Rerank 和引用校验。

同批还实现音视频链路：Worker 对常见音视频执行 FFmpeg/ffprobe 校验、60 秒带重叠
分段和有限抽帧，调用当时配置的 ASR 生成带时间戳 Markdown，并把派生文件作为
重试恢复点后再上传 RAGFlow。用户原媒体的文件名、大小、MIME 与 SHA-256 不被派生
文件覆盖。

自动回归结果为 `186 passed, 11 skipped`；Ruff、Mypy、技术规格和追踪密钥检查通过，
规格识别 27 个 API operation 和 11 张应用表。11 个跳过项仍是当前终端没有配置真实
`TEST_DATABASE_DSN`/`TEST_CACHE_REDIS_URL`，因此本增量尚未在本轮重新执行真实
PostgreSQL Migration 和 Redis Integration，也未使用业务文档校准动态 Top-K、实体
抽取覆盖率、语义去重阈值或真实音视频质量。既有 READY 文档需要重解析才会获得新的
Section/结构化 Metadata 侧车索引；本轮因 Docker daemon 不可用，尚未执行真实媒体
生命周期联调。

### 8.5 2026-08-04 文档级 Profile 手工联调

在当前本地 Docker 环境、同一个测试知识库中完成两份非业务验收文档解析：

- DOCX `01_结构化实验报告_主文档.docx` 使用 `office-default@1` 和
  `prose-384@1`，状态 `READY`，生成 7 个 RAGFlow 原始 Chunk；
- PDF `04_锂离子电池SOH估计.pdf` 使用 `deepdoc-default@1` 和
  `prose-384@1`，状态 `READY`，生成 61 个 RAGFlow 原始 Chunk；
- 两份文档均完成 `heading-length-v3` Parent、Section、Document Summary 和版本 2
  结构化 Metadata，`lazytt_hierarchical_index=ready`。

这次证据确认正文文件族的自动 Chunk Profile、文档级 Parser 覆盖和侧车索引真实
生效，但不能替代 XLSX、PPTX、图片和音视频文件族的独立验收。PDF 的确定性版本号和
技术词抽取仍出现普通小数、论文编号等噪声，属于后续精度优化项，不影响解析和索引
状态判定。

### 8.6 2026-08-09 文档安全版本更新

本次新增稳定逻辑文档 ID 下的不可变版本历史。新文件会作为独立 RAGFlow 物理文档
完成解析、Chunk、Embedding、Metadata 和侧车索引；所有产物就绪后才切换活动版本并
推进知识库 Revision。候选失败时旧版继续检索，退役版本可以通过同一发布流程回滚，
删除逻辑文档时会清理全部已知物理版本。当前 `AUTO` 明确落为 `SAFE_REPLACE`，尚未
实现只重建变化 Chunk 的真实增量索引。

2026-08-10 进一步把主切换从“只修改 Chunk 可用性”改为 RAGFlow 官方完整文档状态
接口。发布后，退役旧文件在 RAGFlow 管理界面中明确显示为关闭，活动新文件显示为
启用；该接口同时同步文档全部 Chunk。数据库激活失败测试确认会按顺序重新打开旧版、
关闭候选，既有显式 Chunk ID 修复接口仍被保留但不再用于正常版本切换。

本轮自动验证结果：

```text
后端：218 passed，11 skipped
Ruff：通过
Mypy：通过
规格：OpenAPI 30 个操作、SQL 12 张应用表，通过
开发控制台：构建与 12 项界面测试通过
仓库内用户端参照快照：TypeScript、ESLint、构建与 14 项界面测试通过
```

11 项跳过仍是当前终端未配置真实 `TEST_DATABASE_DSN` 或
`TEST_CACHE_REDIS_URL`。本轮 Docker daemon 未运行，因此没有把 `0006` 迁移应用到
真实 PostgreSQL，也没有重新执行 RAGFlow 新版本发布/失败补偿/重启后回滚联调；这些
属于合入后的真实环境验收项，不能用 Fake Adapter 单元测试替代。

### 8.7 2026-08-11 通用模型配置迁移

当前运行链路已经移除供应商专属环境变量、活动 Profile 和 Rerank Adapter。新部署按
职责填写 Chat、Embedding、Vision 的 `BASE_URL/API_KEY/MODEL`；可选 Rerank 和 ASR
也使用各自独立三元组，但分别遵守项目约定的 `/rerank` 和 `input_audio` 扩展协议。
Query Rewrite 默认复用回答模型，只有填写 `REWRITE_MODEL` 时才使用同一端点上的独立
模型。

自动验证覆盖：

- 单条/批量 Embedding 与真实维度读取；
- 普通回答、JSON Object、独立 Rewrite 模型和 SSE 请求体兼容；
- 合成图片 OCR；
- RAGFlow `OpenAI-API-Compatible` Provider 的实例创建、默认模型回读，以及通过官方
  `PUT` 原地轮换 API Key；
- Rerank 响应下标/分数校验与在线 RRF 降级；
- ASR/Rerank 关闭时不要求填写对应三元组，开启时缺项立即失败；
- 旧厂商字段不再进入当前环境生成、Compose、Profile 或运行 Adapter；
- 本地敏感值没有出现在 Git 跟踪文件。

本轮结果仍为后端 `218 passed, 11 skipped`，两个仓库内前端参照实现的类型检查、
Lint、构建和测试通过，Compose 静态拓扑验证通过。Docker Desktop/daemon 当时不可用，
所以没有调用当前用户配置的真实模型，也没有重新执行 RAGFlow Dataset 生命周期、
容器重启持久化、Rerank/ASR 付费端点和语义缓存真实联调。上述项目仍应在 Docker 与
已批准模型 Key 可用后通过最小非业务 Fixture 复验。
