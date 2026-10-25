# RAG 后端 ECS 开发部署

本页是当前 ECS 操作入口。部署到阿里云 ECS 上的 Docker Compose，复用已有共享
PostgreSQL、Redis Stack、Elasticsearch 和 MinIO。这里只维护后端，不部署前端。
规范参考用户提供的 `DEPLOYMENT_STANDARD.md`；按照本次确认，数据库迁移采用纯 SQL。

按项填写 GitHub Secrets、Variables、Compose 环境变量，以及在 ECS 手动执行镜像内
数据库迁移的完整命令，见 [部署配置清单](部署配置清单.md)。

当前交付是仓库改造，目标 ECS 的地址、容量、共享 Store 兼容性、真实模型和网络尚待验收。
当前 Direct Tenant/User Header 不提供认证，默认仅允许宿主机回环访问。管理员可明确选择开发公网 Gateway；该选择不构成认证接入或生产上线。

## 1. 文件与职责

```text
compose.dev.yml
.github/workflows/
└── ecs.yml
migrations/
├── rag_app/
│   ├── V1.0.0__init_schema.sql
│   ├── V1.0.0__stamp_existing_schema.sql
│   └── V1.0.0__to__V1.1.0__shared_knowledge_bases.sql
└── rag_flow/
    └── V0.26.4__init_schema.sql
deploy/
├── local/                         本地启停、存储和 Smoke
│   └── ragflow/                    本地独立配置模板
│       └── service_conf.postgres.yaml.template
└── ecs/
    ├── ragflow/                    ECS 独立配置模板
    │   └── service_conf.postgres.yaml.template
    ├── README.md
    ├── .env.dev.example
    ├── 环境变量.md
    ├── 部署配置清单.md
    ├── 数据库字典.md
    ├── release.py
    └── database/
        ├── 01_roles_databases.pgadmin.sql
        ├── 02_database_permissions.pgadmin.sql
        ├── 03_rag_app_comments.sql
        ├── 03_rag_flow_comments.sql
        ├── 04_verify_rag_app.sql
        ├── 04_verify_rag_flow.sql
        └── 05_verify_database_access.sql
tests/ecs/
├── test_database_sql.py
└── test_release.py
```

`migrations/` 只保存 `.sql`。初始化文件只负责目标库内结构；角色、建库、权限、中文注释
和结构验收由 `database/` 配合完成。迁移只携带结构和版本标记，不含账号、模型或业务数据。
数据库账号/权限阶段与应用发布阶段独立，普通 release 不运行迁移或注释 SQL。

`rag_app.alembic_version` 保留原名称和历史头 `0006_document_version_updates`，避免破坏
已有数据库。它现在是 SQL 发布版本表，运行期已不依赖 Alembic，不能手改版本伪造升级。
原 Python 迁移历史保存在改造前 Git 检查点 `335d418ae86671a7617d21017d10feb8d323c22b`。

## 2. 运行拓扑和前置条件

| 对象 | 当前行为 |
|---|---|
| Compose 项目 | `lazytt-rag-ecs-dev`；必须使用独立部署目录 |
| 自研最终镜像 | 一个 GHCR `linux/amd64` digest，Gateway/Worker/Scheduler/管理任务共用 |
| RAGFlow 镜像 | `ghcr.io/lazyaix/ragflow` 中与官方 v0.26.4 对应的经核实 amd64 digest，两个服务共用 |
| 常驻服务 | `ragflow-server`、`ragflow-task-executor`、`rag-gateway`、`rag-worker`、`rag-scheduler` |
| 一次性任务 | `rag-app-schema-check` 只读；`rag-app-bootstrap` 初始化 Profile/缓存；`rag-app-migrate` 显式 SQL |
| 网络 | 独立 bridge、自动 IPAM；不加入或创建共享 Store 网络 |
| 持久卷 | 项目自己的 upload_staging_data、ragflow_logs、scheduler_data；启停不删卷 |
| 入口 | Gateway `127.0.0.1:19380`；RAGFlow API `127.0.0.1:19381`；不发布 UI/Admin 端口 |
| 资源 | 默认常驻内存上限合计 16 GiB；启用 MinerU 再增加 2 GiB，另预留一次性任务、Docker 和宿主余量；按实际机器验收 |
| 日志 | json-file，每容器 10 MiB × 3；容器错误原文不进入发布回执 |

需先核实：ECS 为 amd64，有 Python 3、Docker Engine 与支持 `up --wait` / `run
--interactive=false` 的 Compose v2；部署账号能执行 Docker、读受限配置；GHCR 可达；
Store 地址能从应用容器到达。PostgreSQL 基线在 18.4 验证，Redis 必须具备 RediSearch。
ECS 不创建 Store、专用 Bucket 或共享服务权限，也不安装软件或改防火墙。

同名网络/卷若不带本项目归属标签，发布会停止，不接管已有资源。迁移前保存受影响数据库
备份与 ACL，验证可恢复。连接池与进程数的预算见 [环境变量](环境变量.md)。

## 3. 首次配置与制品准备

1. 在已审阅提交构建镜像。GitHub `ECS backend` 的 `build` 执行制品构建或候选核验。
   已移除独立 CI 工作流，规格、静态检查、密钥扫描和测试由开发者按根 README 手动执行。
   记录完整 SHA 与 GHCR digest。RAGFlow digest 必须与固定 v0.26.4 平台对应。
2. ECS 管理员按 [.env.dev.example](.env.dev.example) 建立 `/etc/lazytt/rag.env`，归属
   `root:root`、权限 `0600`。它是 Bash `export` 文件，不是本地 `.env.full`。先填数据库
   配置和 RAGFlow digest，其余未消费字段可空；真实秘密不进入 GitHub Variables。
   模板已预填本机运行中的公开模型配置，并启用媒体处理和重排；对应 API Key 仍需填写，
   模型引用与维度仍须经过首次 `bootstrap-models` 注册和验证，不能仅因已预填而跳过。
3. 用相同 digest 和 SHA 执行 Actions `prepare`。它拉取并核验候选镜像，检查 Compose、
   网络/卷归属，保存非敏感模板和回执；首次写入 `compose.yml`，已有环境保留当前文件。
   prepare 不启动服务，不迁移数据库，也不等同于部署成功。

配置项、GitHub Variables/Secrets 和完整消费者清单都在 [环境变量](环境变量.md)。
只有首次流程及验收完成后才把 `ECS_DEPLOY_ENABLED` 设为 `true`，启用 `dev` push 自动发布。
本次改造没有推送 Git、开启开关或创建真实 ECS 配置。

## 4. 数据库执行顺序

使用已审阅同一 SHA 的 SQL，在管理员工作站 pgAdmin 的 Query Tool 操作。第一步选择
`postgres` 管理库；以后逐步确认目标库和 `current_user`。每个文件单独执行，出错先
`ROLLBACK` 并检查原因，不继续运行下一份，不删库重建。

| 顺序 | 连接库 / 有效角色 | 文件与预期结果 |
|---|---|---|
| 1 | postgres / 实例管理员 | `01_roles_databases.pgadmin.sql`：A、B、C 分别选中运行；已有库核对 owner 后跳过 CREATE DATABASE；角色密码通过 pgAdmin 属性设置 |
| 2 | rag_app 与 rag_flow / 实例管理员 | 两库各执行 `02_database_permissions.pgadmin.sql`；只修改本系统库权限 |
| 3 | rag_app / lazytt_rag | `migrations/rag_app/V1.0.0__init_schema.sql`：仅全新空库，创建 13 张表及 V1.0.0 版本 |
| 4 | rag_app / lazytt_rag | `migrations/rag_app/V1.0.0__to__V1.1.0__shared_knowledge_bases.sql`：升级共享知识库结构，登记 V1.1.0 |
| 5 | rag_flow / lazytt_rag | `migrations/rag_flow/V0.26.4__init_schema.sql`：仅全新空库，创建 42 张表及相关结构 |
| 6 | 对应业务库 / lazytt_rag | `03_rag_app_comments.sql`、`03_rag_flow_comments.sql`：补充中文注释 |
| 7 | 对应业务库 / lazytt_rag | `04_verify_rag_app.sql`、`04_verify_rag_flow.sql`：只读验收，返回 verified |
| 8 | postgres / 实例管理员 | `05_verify_database_access.sql`：检查自身两库授权和其他业务库 CONNECT 隔离 |

管理员连接业务库后，步骤 3～7 先 `SET ROLE lazytt_rag;`，完成后 `RESET ROLE;`。
本系统按既有契约共用一个业务所有者 `lazytt_rag`，两库之间允许该身份访问；不得让它有
SUPERUSER/CREATEDB/CREATEROLE/REPLICATION/BYPASSRLS。跨其他库的 PUBLIC CONNECT
若仍存在，第 8 步会报错；由管理员另行审阅共享 ACL，不由本脚本批量撤销其他系统权限。

已有库按实际版本分支执行：

- 已是 V1.1.0：跳过初始化和增量迁移，复核结构、注释及权限。
- 0006 完整旧结构、缺少全部发布元数据：只执行 `V1.0.0__stamp_existing_schema.sql`，
  之后按 V1.0.0 处理。不能给部分元数据、未知结构或 V1.1.0 重新盖章。
- V1.0.0：升级脚本要求知识资源、Job、查询、反馈和幂等表为空。有旧业务数据时会拒绝，
  保留现场；必须另行审阅数据迁移方案，不能为通过门禁清空真实业务数据。
- 早于 0006 的历史开发库：当前纯 SQL 链拒绝自动处理。先在检查点版本及隔离恢复库中
  完成历史升级验证，再单独审阅迁移；不要把这类库当作空库。
- RAGFlow 已有完整 v0.26.4 库：跳过初始化；部分结构停止人工核查。官方 RAGFlow 启动
  仍可能执行兼容性 DDL，因此其角色保留对象所有权；自研发布脚本不会替它承诺完全只读。

字段、对象说明和正式 SQL 链见 [数据库字典](数据库字典.md)。

### 已有 rag_app 的 PUBLIC CONNECT 收紧

若其他系统的隔离门禁发现可通过 PUBLIC 连接 `rag_app`，先只读确认该库 owner、
PUBLIC ACL，以及 `lazytt_rag` 的显式 CONNECT。由 `rag_app` 对象所有者在该库执行：

```sql
REVOKE CONNECT ON DATABASE rag_app FROM PUBLIC;
```

此操作仅移除该库的公共连接授权，保留已显式授予的权限，不修改其他数据库或终止
既有会话。执行前记录 ACL，执行后核实 PUBLIC CONNECT 已移除、`lazytt_rag` 新连接
成功，并由受阻调用方重新执行自身权限门禁。若确有其他获准调用方，应先核查其独立
显式授权，不能把恢复 PUBLIC CONNECT 当作绕过隔离门禁的解决办法。

## 5. ECS 上的显式管理命令

SQL 也可由管理员通过同一候选镜像执行，适合已有环境的候选迁移。工作站先从已审阅
SHA 提取 `compose.dev.yml`、`deploy/ecs/release.py` 和
`deploy/ecs/ragflow/service_conf.postgres.yaml.template`，仅将这三个文件传到 ECS 上
`mktemp -d /tmp/rag-admin.XXXXXXXX` 创建的临时目录。文件名保留，暂存目录不用作服务目录。
不要传源码、真实本地 env 或前端。Actions 自动交付会清理自己的临时文件，管理员任务
需单独暂存。下面在 ECS 的 root Bash 会话执行，填已核实的非敏感候选参数：

```bash
set -euo pipefail
set +x
test "$(stat -c '%U:%G:%a' /etc/lazytt/rag.env)" = root:root:600
bash -n /etc/lazytt/rag.env
source /etc/lazytt/rag.env
stage='<本次临时暂存目录>'
revision='<完整已审阅 Git SHA>'
image='<ghcr.io/...@sha256:...>'
directory='/opt/lazytt/lazytt-rag-system'
candidate=(python3 "$stage/release.py" --directory "$directory"
  --source-compose "$stage/compose.dev.yml"
  --source-template "$stage/service_conf.postgres.yaml.template"
  --image "$image" --revision "$revision")
```

GHCR 私有包需本次受限会话的有效登录。可临时设置 `DOCKER_CONFIG` 到专用权限 0700
目录并交互执行 `docker login ghcr.io`；勿覆盖共享 Docker 登录配置。结束后 logout 并
清理本次临时认证目录。数据库迁移使用业务角色配置，不在主机持久文件放实例管理员密码。

在步骤 1～2 建库授权完成后，全新安装可依次执行下列明确操作；已有库只选升级卡要求的
命令，禁止机械重跑初始化。每次命令成功后记录文件名和版本，再进入下一步。

```bash
"${candidate[@]}" --mode migrate --database rag_app --sql V1.0.0__init_schema.sql
"${candidate[@]}" --mode migrate --database rag_app --sql V1.0.0__to__V1.1.0__shared_knowledge_bases.sql
"${candidate[@]}" --mode migrate --database rag_flow --sql V0.26.4__init_schema.sql
```

这些命令只使用内存中的候选 Compose，保留已有运行 Compose，不启动常驻服务。注释与
验收继续在 pgAdmin 使用上节步骤 6～8 完成，不另复制进应用镜像。

## 6. 首次账号、模型与发布

1. 两库验收通过，补齐 Redis Stack、ES、MinIO 配置。新建账号时将
   `RAGFLOW_REGISTER_ENABLED=1`，重新 source 配置，执行
   `"${candidate[@]}" --mode init-ragflow`，只启动 RAGFlow 两个服务。该模式拒绝已有运行服务。
2. 首次管理员账号走官方账号 API。下面从终端隐藏读取输入，输出直接进入父工具，
   API Key 捕获后原子写回 0600 配置；不在命令行、Shell 历史或终端打印密码/Key：

   ```bash
   python3 -c 'import getpass,json; print(json.dumps({"email":getpass.getpass("邮箱（隐藏输入）："),"password":getpass.getpass("管理员密码：")}))' \
     | "${candidate[@]}" --mode bootstrap-account --config-file /etc/lazytt/rag.env
   source /etc/lazytt/rag.env
   ```

3. 补齐 LLM/Embedding/Vision 三元组后，执行
   `"${candidate[@]}" --mode bootstrap-models --config-file /etc/lazytt/rag.env`。
   它会实际调用模型端点并产生少量探测费用；只按明确配置接入，不猜供应商。
   成功后重新 source 文件；设回 `RAGFLOW_REGISTER_ENABLED=0`。
4. 使用同一 SHA/digest 的 Actions `release`，或执行
   `"${candidate[@]}" --mode release </dev/null`。顺序为只读结构检查 → RAGFlow Server →
   Executor → Profile/缓存 Bootstrap → Gateway/Worker/Scheduler，逐步等待健康。
5. 按下节验收。管理员操作结束只清理自己暂存的三个辅助文件及空暂存目录；保留
   `$directory/compose.yml`、配置模板、回执、受限环境文件及项目数据卷。

账号和模型登记不是普通发布动作。已有应用运行时，这两个首次入口会拒绝修改；变更模型
必须单独审阅索引版本、维度、数据兼容性与维护窗口。

## 7. 日常一键发布与失败恢复

### 2026-09-08 首次账号输入交接

本次应用候选固定为 `58153ebc9cafcfdbc0668dffb70b476b24697e0e`，镜像为
`ghcr.io/lazyaix/lazytt-rag-system@sha256:ad43eca40dbb6607492ae3df171507405d307fb1d957a42492921459a00e65bd`。
两库正式迁移、03 注释与 04 结构验收均已通过；不再重复初始化。
`19380/19381` 已由 Identity 使用，因此本次 RAG 配置持久采用宿主回环
`19580/19581`，不修改 Identity。端口仍须按各环境实际占用核实。

部署执行人已向 ECS 交付候选原版 `release.py`、Compose 源文件、RAGFlow 模板及
非敏感 `manifest.json`，位置为
`/opt/lazytt/lazytt-rag-system/administrator/58153ebc9caf/`。
同目录 `account.sh` 只隐藏读取密码，并调用第 6 节的原有 `bootstrap-account`；
不另行实现注册或直接写数据库。目录仅供 root 访问，候选清单包含文件 SHA256。

先确认 RAGFlow 两个服务健康，再在 ECS 的交互终端执行：

```bash
sudo bash /opt/lazytt/lazytt-rag-system/administrator/58153ebc9caf/account.sh '<管理员邮箱>'
```

密码提示不回显，输入仅经进程 stdin 传递；不要把密码加在命令参数中或发到聊天。
成功时正式发布工具输出 `bootstrap-account` 的 `status: passed` 回执，并将 API Key
写回 `/etc/lazytt/rag.env`；操作者无需复制 Key。失败时停止并保留错误阶段，不打印配置、
不清库，也不绕过官方账号 API。本次该账号入口及后续 `bootstrap-models` 已通过；已完成环境不要重复首次注册。

本次已关闭注册并完成同候选 release，GitHub Actions run `34233785796` 成功，
五个常驻服务均健康。应用仍为上文固定的 `58153ebc`，SSH 保活修复只改变发布控制。
其他首次环境完成账号后，由部署执行人继续正式 `bootstrap-models`、关闭注册及同候选 release。
模型调用、五个服务健康、业务和恢复验收分别记录，不能以账号成功代替部署完成。

本次运行验收：Gateway `19580` readiness 与 RAGFlow `19581` healthz 均为 200，
缺失或空 Tenant/User Header 返回 `400 / RAG-COMMON-001`。空闲状态下重启五个服务后，
两个健康接口恢复，30 条配置记录快照不变；不代表带业务任务的恢复。RAGFlow API Key
经官方用户接口确认归属本次管理员，运行中的注册开关为 0。

随后按用户指定执行 `REVOKE CONNECT ON DATABASE rag_app FROM PUBLIC`；新连接确认
`lazytt_rag` 可访问，两个 Memory 角色不可访问；`rag_flow` 本身无 PUBLIC CONNECT。
该步骤只收紧 RAG 库 ACL，不代表其他系统的公网发布已经完成。

当前仍未接入真实 Identity；Direct Header 并非认证凭据。因此真实身份/授权、
上传到检索问答的完整业务链和负载恢复保持未验收，RAG 入口保持回环。

完成首次流程后，日常发布仍可自动化：获准的 `dev` push 触发单个应用镜像
构建/推送 → SSH 交付 → 只读 Schema 检查 → 更新 → 健康检查。也可手动 release 已有
digest 和完整 SHA，失败重试沿用同一候选。workflow 和主机文件锁均按本环境串行执行。

需要结构升级时顺序是：build/prepare 候选 → 管理员备份及执行明确 SQL → 注释/验收 →
release 同一候选。prepare 成功不意味着已执行数据库变更。迁移失败保留已提交旧版本，
修复后从确认的阶段继续。每份 SQL 独立事务，不把多个文件成功声明成一个总事务。

发布前只读门禁失败时不更新旧服务。更新阶段失败时停止候选服务，恢复旧 Compose，并用
旧镜像检查当前结构；兼容时恢复发布前正在运行的本系统服务，不兼容则保持停止，等待
管理员恢复决策。不会 `down -v`、回滚数据库或恢复共享 Store。旧模板按 SHA 独立保留。
若停止/恢复操作本身失败会返回失败，不能写恢复成功。

该恢复依赖同一发布进程存活；断电、强制终止、主机环境文件被外部修改不在自动恢复保证内。
环境秘密不会版本化备份；配置回退由管理员按配置修订记录恢复。Bootstrap 已提交的
Profile/缓存状态不会自动回退。RAGFlow 升级产生的结构变化也不会被脚本逆向执行。

ECS 上的常驻定义只有 `compose.yml`；`config/<SHA>/` 为非敏感模板，`candidate.json`
为最后成功阶段回执。镜像 digest、SHA 和数据库版本以本次 Actions/回执为准，不只看标签。

## 8. 分层验收

在 ECS 管理会话先加载受限环境，再只读检查本项目：

```bash
docker compose --project-name lazytt-rag-ecs-dev -f /opt/lazytt/lazytt-rag-system/compose.yml ps
curl -fsS http://127.0.0.1:19380/health/ready
curl -fsS http://127.0.0.1:19381/api/v1/system/healthz
docker ps --filter label=com.docker.compose.project=lazytt-rag-ecs-dev \
  --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
```

默认使用经授权 SSH 隧道访问 Gateway 回环端口。用户明确要求开发公网入口时，
执行下文“开发 Gateway 公网入口”；当前尚未交付真实身份认证。

| 验收层 | 应记录的证据 |
|---|---|
| 制品/配置 | 完整 SHA、应用/RAGFlow digest、平台、Actions 链接、配置修订号（不含秘密） |
| 数据库 | 两库/角色、V1.1.0 历史、注释/字段/对象验收、跨库访问拒绝、备份位置 |
| 运行就绪 | 五个常驻服务健康；无新增 OOM/持续重启；两项健康 API 通过 |
| 业务 | 合成文档上传→解析→检索/引用→删除；租户隔离与拒绝路径；开启的可选能力单独验证 |
| 恢复 | 重建后数据持久性、失败候选、兼容/不兼容旧镜像恢复；共享 Store 未受影响 |
| 入口 | 默认验证本机与获准隧道；显式开放公网时补充外部 HTTP 验证；不写生产可用结论 |

本地代码验证使用 `tests/ecs/test_release.py` 注入故障；`test_database_sql.py` 仅在
显式 `TEST_SQL_ADMIN_DSN` 指向空的可丢弃本地 PostgreSQL 集群时运行，测试会创建并清理
canonical 数据库。它拒绝目标库/角色已存在的实例，不能将测试 DSN 指向现有 Store。
代码测试不代替真实 ECS 的业务、容量和恢复验收。

## 可选 MinerU 官网 VLM 解析

在 ECS `/etc/lazytt/rag.env` 中设置以下四项，真实 Token 不提交 Git：

```bash
export MINERU_CLOUD_API_TOKEN='<REDACTED>'
export MINERU_CLOUD_MODEL=vlm
# 1 核、2 GiB 适配器；不代表整套系统具有 10 文件解析并发。
export MINERU_CLOUD_MAX_CONCURRENCY=10
export MINERU_CLOUD_RESULT_CONCURRENCY=2
```

Token 留空时跳过此服务；启用时用真实 Token 替换整个占位符。无需新增 GitHub
Variables 或 Secrets，也无需新增流水线步骤。现有流水线交付的应用镜像已包含适配服务，
Compose 与 release.py 一同交付，管理员仍只按原流程执行数据库变更。

随后 release 自动启动 `mineru-cloud-adapter`，等待健康，通过 RAGFlow 官方 API
幂等注册 `mineru-cloud@mineru-cloud@MinerU`，再导入策略并启动后端。注册失败则发布失败，
按既有结构兼容检查恢复此前运行服务；已注册的模型记录会保留，重试可复用。

上传 PDF 时选择 `mineru-cloud@1`。文件发送到 MinerU 官网，使用 VLM 模式；
适配容器只负责上传、轮询和结果转换，限制为 1 CPU、2 GiB，默认最多同时处理 10 个请求，结果下载与转换并发为 2，
不发布主机端口，Token 仅注入该容器。健康检查只检查配置，不证明官网配额或 Token 有效；
发布后需用小型测试 PDF 验证切块非空。超过适配服务并发限制的请求可能返回忙碌。

停用前应先等待 MinerU 任务完成；清空 Token 后重新 release 会停止适配服务，
已导入的策略仍存在，此时不要再选择该策略。已有切块检索不受影响。

## 任务及时派发与恢复

新增任务或手动重试在数据库提交后立即向 Celery 发出唤醒通知；通知不包含文件内容。
Worker 在同一个执行名额内连续领取积压任务，最多领取 10 个，或达到 60 秒预算后
在当前任务完成时交还 Celery，并发送续接通知。不会中断尚未完成的解析。
每 5 秒扫描保留为通知失败、过期和到期重试的兜底，不再是正常上传的唯一入口。
Redis 不可用时已提交任务保留在 PostgreSQL，恢复后由扫描继续领取。

Worker 的进程数仍决定该容器同时执行任务的上限；批次大小不是额外并发。
进度更新保留心跳租约，阶段提交会验证当前任务所有权；丢失所有权时回滚。
数据库行锁不保证外部 API 恰好调用一次：外部调用已成功但检查点尚未提交就崩溃的
情况仍需按现有幂等和任务恢复机制处理，压测应检查重复文件及重复计费。
本次无需数据库迁移或新增环境变量，重建应用镜像并重启 Gateway、Worker、Scheduler 生效。

## 开发 Gateway 公网入口

默认 `RAG_GATEWAY_BIND_HOST=127.0.0.1`。用户明确要求开放开发 Gateway 时，由管理员在
既有 `/etc/lazytt/rag.env` 持久设置 `RAG_GATEWAY_BIND_HOST=0.0.0.0`，并将
`LAZYTT_RAG_PUBLIC_BASE_URL` 设置为实际公网 IP 与 `RAG_APP_PORT` 对应的地址。
沿正常候选 prepare、只读数据库检查、release 和失败恢复路径发布；不重做迁移或账号初始化。

本次用户要求不限制来源 IP，目标 Gateway 端口为已有配置的 `19580`。防火墙和安全组
仅为这个 Gateway 端口配置相应入站范围；RAGFlow `19581` 始终回环，不开放管理入口。
公网联调必须实测外部 HTTP 响应，并核对真实监听、缺失身份头拒绝以及 RAGFlow 未公开。

当前 Header 仅派生数据范围，不验证调用者拥有该身份；开放公网会允许外部调用者自填
Tenant/User 并访问对应范围及消耗模型资源。不得把 Header 格式拒绝称为认证保护。
关闭公网时将绑定改回 `127.0.0.1` 并重新 release，同时撤销对应入站规则。


### 本次安全组操作

在目标 ECS 所属安全组添加入方向允许规则：协议 TCP，端口 `19580`，来源
`0.0.0.0/0`（用户已明确要求不限制公网来源），描述为 RAG Gateway 开发访问。
保留所有既有规则，不添加 `19581`，不扩大到全部端口。该操作只允许流量到达宿主，
仍须新候选实际监听后从 ECS 之外取得 HTTP 响应。回收入口时删除本次专用规则并将
Gateway 绑定恢复回环。若已有等效规则，只记录并复用，不重复添加。
