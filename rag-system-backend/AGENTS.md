# LazyTT RAG System 智能体协作说明

本文件面向 Codex、Claude Code、GitHub Copilot Agent 等编程智能体，也供新加入
项目的开发人员参考。它适用于本仓库根目录及全部子目录；如果未来某个子目录
存在更具体的 `AGENTS.md`，则该子目录优先遵循更具体的规则。

用户当前指令、正式 Issue 及其最新讨论优先于本文件。发现用户要求、技术规格、
代码现状和本文互相冲突时，不得静默选择或扩大需求，应保留现场、给出证据并请求
确认。

## 1. 项目定位与固定技术基线

LazyTT RAG System 是独立的、按 `security_scope_id` 强隔离的 RAG Gateway。
它提供知识库、文档摄取、异步 Job、双语混合检索、流式问答、引用、缓存、反馈
和健康检查；底层复用 RAGFlow 解析及索引，模型能力通过受控云端 API 提供。

当前本地开发基线：

- Python 3.12，FastAPI、SQLAlchemy、Pydantic、Celery、Redis、PostgreSQL；
- Node.js 22+，Next.js/React/vinext，包含开发控制台和普通用户端；
- 官方 `infiniflow/ragflow:v0.26.4`；
- RAGFlow Embedding 与语义缓存：通过通用 OpenAI-compatible Embeddings 端点接入，
  模型名和实际维度由启动 Bootstrap 验证；
- Gateway Chat/Rewrite：通过通用 OpenAI-compatible Chat Completions 接入，端点需要的
  Token 字段、采样省略和额外 JSON 由启动 Bootstrap 探测，不按供应商写分支；
- 图片 OCR：通过通用 OpenAI-compatible 多模态 Chat Completions 接入，并由
  RAGFlow Picture Parser 调用；
- 音视频预处理：容器内 FFmpeg/ffprobe 负责分段与有限抽帧，`ASR_*` 配置的模型
  负责语音转写；Worker 把结果转换成带时间戳 Markdown 后再交给
  RAGFlow，原媒体不会绕过 Gateway 直接进入解析器；
- 检索：向量路线、关键词路线、双语 QueryPlan、加权 RRF；
- Gateway Rerank：使用项目约定的通用 `POST /rerank` 协议；只接受真实
  Cross-Encoder 分数，故障时明确降级到 RRF，不伪造 Rerank。

固定边界：

- 不修改、不复制、不 Fork 同级目录 `../ragflow-main`；该目录只允许作为只读
  参考。RAGFlow 能力通过官方镜像、官方 API 和 Adapter 接入。
- 不直接修改 RAGFlow 数据库伪造账号、模型、Provider、Dataset 或任务状态。
- 不使用余弦相似度冒充 Cross-Encoder Rerank，不把任何 Embedding 模型注册为
  已经验证的 Rerank 服务。
- 不将不同 Embedding 模型、版本或维度写入同一个知识库索引或语义缓存索引。
- 模型或向量维度变化时必须创建新索引版本，禁止向旧索引混写。

## 2. 第一事实来源与阅读顺序

开始修改前，按任务范围阅读以下资料：

1. `README.md`：项目入口、启动方法和基础质量门禁；
2. `rag-development-spec/`：技术规格、OpenAPI、数据库 Schema、契约和 Profile；
3. `docs/PROJECT_STATUS_AND_ROADMAP.md`：完成度、限制和路线；
4. `docs/CODEBASE_STRUCTURE.md`：目录、模块、接口和数据流；
5. `docs/NEWCOMER_LEARNING_GUIDE.md`：由浅入深的当前行为说明；
6. `deploy/local/README.md`：本地部署、安全、Smoke、备份与恢复；
7. `frontend/README.md` 和 `frontend/MANUAL_ACCEPTANCE_TEST.md`：开发控制台；
8. 同级 `../lazytt-frontend-console/README.md`：正式普通用户端；仓库内
   `user-frontend/README.md` 仅作为参照快照说明。

规格、OpenAPI、Profile、实现和文档必须保持一致。发现不一致时先确认哪一项代表
当前批准行为；不要只改一处让其他契约继续漂移，也不要为了让测试通过而降低安全
或数据隔离要求。

## 3. 关键目录与职责

```text
lazytt-rag-system/
├── src/rag_service/          Python 后端
│   ├── api/                  HTTP、认证依赖、Schema、SSE 和错误映射
│   ├── application/          用例编排：离线摄取、检索、问答、缓存
│   ├── domain/               领域模型、状态机、错误和抽象 Port
│   ├── infrastructure/       PostgreSQL、Redis、RAGFlow、模型和文件适配器
│   ├── bootstrap/            Profile、模型、RAGFlow、缓存索引启动预检
│   ├── workers/              Worker、Scheduler 和异步任务入口
│   ├── security/             JWT 与 SecurityContext
│   └── observability/        结构化日志、脱敏和 Request ID
├── migrations/               rag_app/rag_flow 纯 SQL Migration
├── tests/                    unit、contract、integration 测试
├── rag-development-spec/     技术规格、配置种子、OpenAPI 和契约
├── deploy/                   local/ecs 分开的启动发布工具，共用 ragflow 模板
├── tools/                    预检、Bootstrap、密钥、Smoke、备份恢复工具
├── frontend/                 开发测试控制台，默认端口 3000
├── user-frontend/            普通用户端参照快照，不是正式部署事实来源
└── docs/                     状态、结构、ADR、验收和学习文档
```

分层约束：

- `domain/` 不依赖 FastAPI、SQLAlchemy、Redis、RAGFlow SDK 或具体模型厂商；
- `application/` 依赖 Domain Port，不直接操作具体数据库、HTTP Client 或 Docker；
- `api/` 只做协议转换、认证、校验、状态码、Header、分页和 SSE 封装，不放核心
  检索算法或数据库业务逻辑；
- `infrastructure/` 实现 Port，并在进入领域层前标准化外部协议和错误；
- `bootstrap/` 负责启动失败前置发现，不能通过静默降级掩盖模型、维度、Profile
  或 API Key 错误；
- Worker 负责耗时摄取任务，Gateway 请求线程不得同步执行长时间 OCR/解析。

## 4. 开始工作前

每次开始任务必须先执行只读检查：

```bash
git status --short --branch
git diff --stat
git branch --show-current
```

工作区可能包含用户或其他任务尚未提交的修改。必须遵守：

- 现有修改默认属于用户，不能丢弃、覆盖、回退或顺手格式化；
- 不执行 `git reset --hard`、`git checkout -- <path>`、`git clean` 等破坏性命令；
- 修改前检查目标文件 Diff；若与既有修改重叠且无法安全合并，停止并说明冲突；
- 只暂存当前任务自己的文件或 Hunk，不使用未经逐项确认的 `git add .`；
- 不把其他任务、其他 Issue 或自动生成的无关修改带入本次 Commit；
- 不因工作区不干净而删除 Docker Volume、重建数据库或覆盖本地 Secret。

变更必须严格限定在用户请求或正式 Issue 范围内。诊断任务默认只读；只有明确要求
修复或实现时才修改代码。

## 5. 代码风格与命名

### 5.1 Python

- 使用 Python 3.12 语法和完整类型标注；Mypy 必须保持 strict；
- Ruff 目标行宽 100，遵循 `E、F、I、B、UP、ASYNC、S`；
- 包、模块、函数和变量使用清晰英文命名，不使用无意义缩写；
- 领域对象优先使用显式类型、`Enum`、不可变元组和 Pydantic/Dataclass 契约；
- 异步 I/O 必须设置合理超时、有限重试并传播取消；禁止无上限重试和阻塞睡眠；
- 捕获异常时只捕获能够处理的范围；对外转换为稳定错误码，日志保留 Request ID，
  但不得记录密码、Token、私钥、完整 JWT 或未脱敏业务正文；
- 所有 Repository 查询必须带 `security_scope_id`，资源不存在与跨 Scope 访问对外
  保持一致，禁止通过错误差异泄漏资源存在性；
- 写操作考虑幂等、事务、并发版本和失败恢复，不能只处理理想路径。

### 5.2 TypeScript、React 和前端

- TypeScript 保持严格类型，避免无理由使用 `any`、非空断言和静默吞错；
- 浏览器只调用同源 BFF；Gateway JWT、JWT 私钥和下游 API Key 只留在服务端；
- `frontend/` 是开发验收工具，可以展示 UUID、Profile、原始 JSON 和调试信息；
- 正式普通用户产品位于同级 `../lazytt-frontend-console`；仓库内 `user-frontend/`
  只作参照。普通用户产品不暴露知识库选择、Parser、Job ID、RAGFlow 或外部
  Rerank Provider 的运维细节；
- 用户端四个隐藏知识库分别处理 PDF、Office、图片和音视频，并始终按用户
  Tenant/Scope 隔离；
- 用户端对话记录目前只供回看，每个问题独立检索，不能未经需求确认引入上下文；
- SSE 必须真正流式转发，支持取消、错误态和结束态，不能在 BFF 中把整个响应缓冲；
- 新交互同时考虑加载、空数据、失败、重试、禁用、键盘操作和窄屏布局；
- 不把本地开发账号系统描述或实现成生产身份系统。

### 5.3 SQL、Migration、Shell、YAML 和 Docker

- 数据库结构变化优先新增向前 Migration；已经应用的 Migration 不得随意改写；
- Migration 必须说明数据兼容、索引、约束、回滚或恢复策略；
- Shell 脚本使用严格错误处理，变量加引号，不打印 Secret，不依赖全局隐式状态；
- 示例环境文件只放非敏感示例值，真实值只进入 Git 忽略文件；
- 配置字段变化同步更新 Settings、示例 YAML、Compose 环境注入、预检、文档和测试；
- 不手工编辑生成目录、`node_modules/`、`dist/`、`build/` 或缓存目录；
- 锁文件只在依赖确实变化时更新，不做无关的全量重写。

## 6. 强制中文注释规范

**每次修改或新增代码之后，都必须同步新增或修改对应注释。注释必须使用中文，
并详细到即使读者不懂 Python、TypeScript 或当前框架，也能理解这个文件、类、
函数以及关键代码片段正在做什么。此项是代码完成标准，不是可选建议。**

具体要求：

1. 新增源码文件必须在文件顶部写中文模块说明，解释文件职责、调用方、依赖对象、
   输入输出和不负责的边界。
2. 新增或修改类时，必须更新中文类注释或 Docstring，解释类在架构中的位置、状态、
   生命周期、线程/协程安全和主要协作者。
3. 新增或修改函数、方法时，必须更新中文注释或 Docstring，至少说明：
   - 为什么需要这个函数；
   - 参数分别代表什么；
   - 返回值代表什么；
   - 会写入哪些数据库、缓存、文件或外部服务；
   - 可能产生哪些错误、重试、降级或副作用；
   - 涉及异步、事务、并发、幂等、安全 Scope 时的关键保证。
4. 状态转换、检索路线、RRF、缓存边界、引用校验、协议转换、异常恢复、并发控制、
   权限过滤和数据清理等关键代码片段之前，必须写中文块注释解释流程和设计原因。
5. 修改已有逻辑时必须同时检查附近注释；旧注释不再准确时先更新，禁止留下与代码
   行为冲突的说明。
6. 测试代码也要用中文注释说明测试场景、输入、预期结果和它防止的回归问题。
7. Python 使用中文模块 Docstring、类/函数 Docstring 和必要的行前注释；TypeScript
   使用中文文件注释、TSDoc/JSDoc 和关键逻辑块注释；Shell、SQL、YAML、Dockerfile
   使用各自语法的中文注释。
8. 注释重点解释“业务含义、数据流和为什么这样做”，不能只是把代码逐字翻译成中文，
   例如“给变量加一”不属于合格注释。
9. 注释不得包含真实密码、API Key、Token、私钥、生产地址、客户数据或大段业务正文。
10. 自动生成文件、第三方代码、依赖锁文件和纯构建产物不强制补注释；不要为了满足
    注释要求手工修改这些文件。配置或契约文件如果承载关键策略，仍需中文说明。

代码审查时，如果功能正确但关键中文注释缺失、过时或只有语言层面的复述，应视为
任务未完成。

## 7. Profile、模型、检索和缓存规则

- Profile 引用是不可变版本，例如 `hybrid-default@2`。已导入的 `@1`、`@2` 不得
  原地改变语义；行为变化应新增 `@3`，同步默认值、OpenAPI、前端和测试。
- Bootstrap 出现 checksum 冲突时不得修改数据库或跳过校验，应恢复旧版本内容，
  再新增 Profile 版本。
- LLM、Embedding 与 Vision 分别使用通用 Base URL、API Key、模型名三元组；同一
  Key 可以显式填入多个职责，但代码不得隐式复制或按供应商名称推断。
- Embedding 实际维度必须启动时真实调用验证；与既有配置不一致时 Ready 失败。
- 语义缓存只写当前已验证维度的 FLOAT32，索引名、模型、维度变化必须升版本。
- 缓存必须隔离 Scope、知识库 Revision、Retrieval/Prompt/Model Profile、回答语言
  和历史哈希；缓存命中后仍验证引用和知识库边界。
- 缓存故障应安全绕过并继续正常问答；RAGFlow 或知识索引不可用时不能返回未经
  验证的旧缓存答案。
- 双语检索保留原问题和翻译变体，根据文档 `language/lang` 路由向量及关键词
  检索，再执行加权 RRF；不能用单向中文翻译逻辑替代对称 QueryPlan。
- Rerank 关闭或外部服务故障时必须明确返回降级 warning，并稳定使用向量、关键词
  和 RRF，不能返回 500；配置开启时必须先完成真实启动探测才能标记为已验证。

## 8. Docker、数据和外部服务安全

- 正常启动使用 `./deploy/local/start-full.sh`，停止使用 `./deploy/local/stop-full.sh`；
- 禁止执行 `docker compose down -v`，禁止删除或重建既有 Volume；
- 不设置全局 `DOCKER_DEFAULT_PLATFORM`；只有 RAGFlow 不支持 ARM64 的服务可以
  在 Compose 服务级使用 `linux/amd64`；
- RAGFlow UI 和临时管理接口只能绑定 `127.0.0.1`，不得开放到局域网或公网；
- 云端模型调用只使用配置批准的 HTTPS 端点；不得把 Key 写入镜像、日志、测试证据
  或可追踪文件，真实业务接入前还需确认第三方数据处理与合规边界；
- 不停止、删除或修改同一 Docker 守护进程中的其他项目资源；
- 不修改或清空现有 PostgreSQL、Elasticsearch、MinIO、Redis/Valkey 数据，除非
  用户明确授权且已有可验证备份；
- 技术验证只使用最小非业务 Fixture，禁止引入真实客户数据、内部业务文档或生产
  数据；
- 备份文件可能包含账号、Token Hash 和业务元数据，必须放在 Git 忽略的受限目录；
- 删除测试资源优先调用公开 API 并等待异步 Job 完成，不绕过状态机直接删数据库。

## 9. 构建、测试和格式化命令

根据变更范围先运行针对性测试，交付前按风险运行完整门禁。不得声称未执行的测试
已经通过。

### 9.1 后端完整质量门禁

```bash
uv sync --frozen --extra dev
uv run python rag-development-spec/tools/validate_spec.py
uv run ruff check src migrations tests tools deploy/ecs
uv run mypy src
uv run python tools/check_tracked_secrets.py
uv run pytest --cov --cov-report=term-missing
```

Domain/Application 分支覆盖率门禁为 80%。小范围修改可以先执行相关测试，例如：

```bash
uv run pytest -q tests/unit/test_online_service.py
uv run ruff check src/rag_service/application/online.py tests/unit/test_online_service.py
```

### 9.2 开发控制台

```bash
cd frontend
npx tsc --noEmit
npm run lint
npm test
```

### 9.3 普通用户端

```bash
cd user-frontend
npx tsc --noEmit
npm run lint
npm test
```

### 9.4 Compose、模型和真实联调

涉及 Compose、环境注入、模型、RAGFlow Adapter、摄取、缓存或恢复时，按范围执行：

```bash
./deploy/local/preflight-full.sh
./deploy/local/start-full.sh
./deploy/local/run-gateway-model-smoke.sh
./deploy/local/run-ragflow-smoke.sh --parse-timeout 600
uv run python tools/gateway_e2e_smoke.py --output smoke-results/gateway-e2e.json
uv run python tools/gateway_image_smoke.py --output smoke-results/gateway-image.json
```

真实联调前确认所配置兼容端点的模型权限、配额与 Key 有效，并只使用仓库 Fixture
或运行时生成的合成数据。音视频链路还要确认 FFmpeg/ffprobe 可执行、配置的 ASR
模型可访问，并避免把业务录音或视频当作 Smoke Fixture。
Smoke 后确认临时 Dataset、文档、缓存和 Fixture 已清理；不要通过删除 Volume 清理。

### 9.5 仅文档或注释变更

至少执行：

```bash
git diff --check
```

同时检查 Markdown 链接、命令、端口、Profile 版本和描述是否仍与当前代码一致。

## 10. 测试编写要求

- 修复 Bug 必须增加能够先失败、修复后通过的回归测试；
- 新行为覆盖成功、空结果、非法输入、权限隔离、下游超时和可恢复失败；
- Scope、幂等、状态机、缓存边界和引用校验属于安全关键路径，修改时必须测试；
- 单元测试不得依赖公网或真实业务数据；外部调用使用 Adapter Fixture/Mock；
- Integration 测试使用明确的测试数据库和 Redis，不连接生产或未知本地实例；
- 测试不能通过降低断言、删除用例、扩大重试或吞掉异常来“修复”；
- 时间、随机数和 UUID 在需要确定性时使用可注入或固定 Fixture；
- SSE 测试必须覆盖事件顺序、完成事件、错误事件和取消传播；
- 前端变更同时验证数据状态与用户可见状态，不能只检查组件能够编译。

## 11. Git、分支和本地提交

团队强制“一项 Issue = 一个开发分支 = 一个 Pull Request”。分支格式：

```text
type/developer/issue-id/short-description
```

允许的类型及 Commit type：`feature→feat`、`fix→fix`、`refactor→refactor`、
`perf→perf`、`test→test`、`docs→docs`、`chore→chore`、`ci→ci`、
`build→build`。

每完成一个可独立验证和回滚的代码版本，都要创建一次本地 Commit。提交前：

```bash
git status --short
git diff
git diff --cached
```

只暂存本任务文件或 Hunk。Commit 标题格式：

```text
type(scope): concise English description
```

Commit 正文第一行必须列出实际开发成员：

```text
开发成员：@github-enterprise-username
```

智能体不得猜测参与人；优先从当前符合规范的分支名称确认，无法确认时提交前询问。

除非用户另行授权：

- 只创建本地 Commit，不 Push，不创建或更新 PR；
- 不直接 Push `dev`、`staging`、`main`；
- 不执行 `git push --force`；
- 不自行 Merge 环境分支；
- 不修改 Branch Protection、Ruleset、CODEOWNERS 或仓库权限；
- 不 Rebase 或改写已经共享的历史；
- Code Owner Approval 后分支冻结，不再 Push、Rebase 或追加功能；
- PR Merge/Close 后原分支终止，不能复用于新需求。

需要创建 PR 时，目标分支固定为 `dev`，并按团队标准记录 Issue、设计依赖、变更、
验证、风险和回滚。测试未执行或未通过必须如实写明。

## 12. Secret、隐私和日志

严禁提交或输出：

- `.env`、`deploy/local/.env.full`、`deploy/local/.env.ragflow` 的真实内容；
- 管理员密码、RAGFlow API Key、JWT 私钥、完整 JWT、Access Token；
- SSH Key、云凭据、生产连接串；
- 真实客户数据、生产数据、敏感业务 Fixture；
- 包含上述内容的日志、截图、Smoke 结果或备份。

示例文件使用明显的非敏感占位值。排查 Secret 时只报告变量名、是否存在、长度或
脱敏指纹，不把完整值复制到终端输出、聊天、Issue 或 Commit。

## 13. 文档、契约和交付同步

以下变更必须同步对应资料：

- API 路径、字段、错误码：OpenAPI、Schema、契约测试、前端调用和接口文档；
- 数据库结构：版本 SQL、技术 Schema、Repository、恢复说明和测试；
- Profile：新版本种子、默认引用、Bootstrap、前端选项、Smoke 和文档；
- 环境变量：Settings、示例环境文件、Compose、预检、部署文档和 Secret 检查；
- 模型或维度：启动预检、缓存索引版本、RAGFlow Bootstrap、健康检查和恢复说明；
- 用户端行为：用户端 README、必要验收步骤和新人指南；
- 部署行为：Compose 静态校验、启停脚本、健康检查、备份恢复和已知限制。

最终报告必须明确区分：

1. 已实现；
2. 已自动测试；
3. 已真实联调；
4. 尚未验证；
5. 非生产限制。

同时列出实际执行的验证命令及结果、本地 Commit SHA、未提交的其他工作区修改、
是否 Push。禁止把“代码存在”“Mock 通过”描述成“真实联调通过”。

## 14. 完成检查清单

提交或交付前逐项确认：

- [ ] 变更只属于当前请求/Issue，没有覆盖用户既有修改；
- [ ] 新增或修改代码已经同步维护详细中文注释；
- [ ] 注释与真实行为一致，非技术读者能够理解关键数据流；
- [ ] 分层、Scope、幂等、状态机和错误契约没有被绕过；
- [ ] Profile/缓存索引需要升级版本时没有原地修改旧版本；
- [ ] 没有修改 `../ragflow-main`，没有直接修改 RAGFlow 数据库；
- [ ] 没有删除 Docker Volume，没有改变全局 Docker 平台；
- [ ] 已执行与风险匹配的 Lint、类型检查、测试、构建或 Smoke；
- [ ] 未执行的验证已明确说明；
- [ ] `git diff --check` 通过；
- [ ] Secret 扫描通过或已按范围确认无敏感数据；
- [ ] 暂存区只包含本任务修改；
- [ ] 已创建可回滚的本地 Commit，没有未经授权 Push；
- [ ] 最终报告区分实现、自动测试、真实联调、未验证和非生产限制。
