# LazyTT RAG 本地部署与运维

本目录把“有状态存储”和“无状态运行时”拆成两个独立 Compose。完整的新手步骤见
[`docs/USER_MANUAL.md`](../../docs/USER_MANUAL.md)；本文只保留运维人员需要的配置、启停、
验证和恢复边界。

可选的 [MinerU 云端解析](#mineru-云端解析) 由独立适配服务调用官网 API，
无需本机运行 MinerU 模型；启用步骤与 Token 配置见该文档。

本地 RAGFlow 模板位于 `ragflow/service_conf.postgres.yaml.template`；ECS 在
`deploy/ecs/ragflow/` 独立维护另一份，修改配置时按目标环境选择文件。

本次目录改造将原 `deploy/*.sh` 与私有 `.env` 移到本目录，根目录不保留旧启动包装脚本。
运行命令仍从仓库根目录执行；已有本地项目名、网络名和数据卷名沿用。
本地完整一键流程会调用真实模型 API，通常产生探测调用；当前改造的自动测试未运行真实业务栈。

本地 Schema 自动准备支持空库、完整旧 0006、V1.0.0、V1.1.0。早于 0006 或部分结构会
停止；V1.0.0 存在旧业务数据时升级拒绝且保留数据。外部库先按
[当前数据库流程](../ecs/README.md#4-数据库执行顺序)准备，再启动本地客户端运行时。

## 1. 唯一启动入口

首次初始化和以后重启都使用：

```bash
./deploy/local/start-full.sh
```

脚本会依次完成：

1. 为七个本地基础设施密码占位符生成互不相同的随机值；
2. 生成 Git 忽略的 `deploy/local/.env.full`；
3. 静态检查运行时 Compose、平台、端口、模型字段和数据卷；`local` 模式还会检查
   storage Compose，`external` 模式不读取本地存储镜像；
4. `local` 模式先启动独立存储栈，幂等初始化/升级 App Schema 到 V1.1.0；
   `external` 模式只使用外部地址，不执行项目的本地迁移命令；
5. 通过 RAGFlow 官方账号 API 注册/登录管理员并创建稳定 API Key；
6. 真实探测配置的 Chat、Embedding、文档 Vision 和媒体 Vision 服务，自动识别 Embedding 维度及
   Chat 的有限协议差异；
7. 通过 RAGFlow 官方 Provider API 注册通用 Embedding/Vision 模型；
8. 对最终 App/RAGFlow Schema 执行只读校验，执行 Profile、缓存索引 Bootstrap，再启动
   Gateway、Worker 和 Scheduler。local 空库执行两库冻结 SQL；external 的正式迁移
   由管理员按 ECS 操作入口执行。RAGFlow 官方启动仍可能执行兼容性 DDL。

任何一步失败都会停止后续启动，不会删除 Volume，也不会伪造 RAGFlow 账号、模型或业务数据。

本地地址：

- Gateway：<http://127.0.0.1:8080>
- RAGFlow UI：<http://127.0.0.1:8081>
- RAGFlow 主 API：只绑定 `127.0.0.1:9380`

RAGFlow Admin Server（`admin/server/admin_server.py`，默认端口 `9381`）已永久关闭。
Gateway、文档解析、检索、管理 UI 和 Task Executor 都不依赖它；关闭后可减少
`ragflow-server` 的常驻内存。需要临时恢复时，在 `compose.local.yml` 的
`ragflow-server.command` 中重新加入 `--enable-adminserver`，并在 `ports` 中加入
`127.0.0.1:9381:9381`，再运行 `./deploy/local/start-full.sh` 重建运行时。恢复关闭时删除
这两项后重新运行同一启动脚本。不要修改或 Fork RAGFlow 源码。

业务接口当前使用 `X-Tenant-ID + X-User-ID` 派生数据隔离范围，不提供账号、密码、
Session 或 JWT。两个 Header 都不是认证凭据，只能用于受控开发和联调网络；生产必须
接入公司 Identity。

## 2. 存储与运行时边界

| 文件 | 服务 | 生命周期 |
|---|---|---|
| `docker-compose.storage.yml` | Compose Project `lazytt-rag-storage`；共享 PostgreSQL（两个逻辑数据库/账号）、统一 Redis Stack、Elasticsearch、MinIO | 可独立启停，持有 4 个命名卷 |
| `compose.local.yml` | Compose Project `lazytt-rag-runtime`；local App Schema Prepare、RAGFlow Server/Task Executor、只读 Schema Check、Bootstrap、Gateway、Worker、Scheduler | 可反复重建；仅 local 一次性命令可维护 App Schema，不负责数据库容器 |

两个 Compose 是 Docker Desktop 中可分别查看和启停的独立项目，并连接同一个不归
任一项目所有的外部共享网络。启动脚本会在网络不存在时幂等创建，停止其中一层不会
删除另一层的网络或数据卷。`start-full.sh` 在本地模式会自动按“存储 → 运行时”顺序
启动，因此普通用户仍只需一个命令。需要单独维护存储时使用：

```bash
./deploy/local/start-storage.sh
./deploy/local/stop-storage.sh
```

`stop-full.sh` 只停止运行时，存储保持运行。以上脚本都不删除命名卷。

在 Docker Desktop 中应看到两个项目：

```text
lazytt-rag-storage   # 数据库、缓存、对象存储、检索引擎
lazytt-rag-runtime   # RAGFlow 与自研应用运行时
```

## 3. 通用模型配置

先复制私有环境模板：

```bash
cp deploy/local/.env.ragflow.example deploy/local/.env.ragflow
chmod 600 deploy/local/.env.ragflow
```

每项能力只填写 Base URL、API Key 和模型名，不再使用百炼、Moonshot、硅基流动或
Ollama 专属变量：

```dotenv
LLM_BASE_URL=https://provider.example/v1
LLM_API_KEY=<REDACTED>
LLM_MODEL=chat-model-name
# 留空时复用 LLM_MODEL；只有需要独立 Rewrite 模型时才填写。
REWRITE_MODEL=

EMBEDDING_BASE_URL=https://provider.example/v1
EMBEDDING_API_KEY=<REDACTED>
EMBEDDING_MODEL=embedding-model-name
EMBEDDING_DIMENSION=pending-auto-detection

VISION_BASE_URL=https://provider.example/v1
VISION_API_KEY=<REDACTED>
VISION_MODEL=vision-model-name

# 视频抽帧理解使用独立模型；即使复用供应商和 Key，也要显式填写。
MEDIA_VISION_BASE_URL=https://provider.example/v1
MEDIA_VISION_API_KEY=<REDACTED>
MEDIA_VISION_MODEL=media-vision-model-name
```

LLM、文档 Vision 和媒体 Vision 使用 OpenAI-compatible `POST /chat/completions`；
Embedding 使用 OpenAI-compatible `POST /embeddings`。同一个 Key 可以显式填到多个
职责，但脚本不会按供应商名称复制 Key，也不会把真实值写入示例、日志或 Git。

下面这些字段由 Bootstrap 自动探测或生成，不需要用户手工拼接：

```dotenv
LLM_OMIT_SAMPLING_PARAMETERS=false
LLM_MAX_OUTPUT_TOKENS_FIELD=max_tokens
LLM_EXTRA_BODY_JSON={}
RAGFLOW_EMBEDDING_MODEL=
RAGFLOW_VISION_RUNTIME_MODEL=
RAGFLOW_VISION_MODEL=
```

若环境中已经写入数值 `EMBEDDING_DIMENSION`，实际探测结果不一致会阻止启动。模型或
维度改变时必须创建新索引世代并重建知识库/语义缓存，不能把不同向量混写。

### 3.1 可选 Rerank

Rerank 不是 OpenAI 官方统一接口。本项目约定 Base URL 下存在 `POST /rerank`，请求
包含 `model/query/documents`，响应返回带 `index` 和 `relevance_score` 的结果：

```dotenv
RERANK_ENABLED=true
RERANK_BASE_URL=https://provider.example/v1
RERANK_API_KEY=<REDACTED>
RERANK_MODEL=rerank-model-name
```

关闭或在线暂时失败时，系统继续使用向量、BM25 和加权 RRF，并返回可观察 Warning；
不会用余弦相似度伪装 Cross-Encoder 分数。

### 3.2 可选音视频 ASR

ASR 也不是 OpenAI 官方统一接口。本项目约定服务接受 Chat Completions
`input_audio`：

```dotenv
MEDIA_PROCESSING_ENABLED=true
ASR_BASE_URL=https://provider.example/v1
ASR_API_KEY=<REDACTED>
ASR_MODEL=asr-model-name

MEDIA_VISION_BASE_URL=https://provider.example/v1
MEDIA_VISION_API_KEY=<REDACTED>
MEDIA_VISION_MODEL=media-vision-model-name
```

Worker 使用 FFmpeg/ffprobe 将音轨按 60 秒、重叠 2 秒分段；视频以 10 秒为基础间隔
抽帧并补充结尾画面，长视频会在最多 20 帧内自动均匀放宽间隔。转写与图片描述转换
为带时间戳 Markdown 后再交给 RAGFlow 建索引。文档图片继续使用 `VISION_*` 对应的
OCR；视频帧只使用 `MEDIA_VISION_*`。关闭媒体处理时，两组三元组都可以留空。

## 4. RAGFlow 账号与数据安全

`RAGFLOW_ADMIN_EMAIL` 默认是 `admin@lazytt.io`，七个密码保持模板占位符即可。首次启动
会生成一次并保存到权限为 `0600`、Git 忽略的私有环境；后续启动不会轮换密码。
`RAGFLOW_API_KEY` 初次留空，脚本使用官方 API 创建后原子写回。

RAGFlow 相关不支持 ARM64 的两个容器单独使用 `linux/amd64`。不要设置全局
`DOCKER_DEFAULT_PLATFORM`，不要修改或 Fork `ragflow-main`，也不要执行：

```text
docker compose down -v
```

该命令会删除账号、文档、对象、索引和缓存数据卷。

## 5. 本地存储与外部存储切换

默认配置是：

```dotenv
RAG_STORAGE_MODE=local
```

此时连接目标是独立存储 Compose 内的服务名。联调局域网数据库时，把私有
`deploy/local/.env.ragflow` 改为 `external`，并填写 App PostgreSQL DSN、统一 Redis Stack
主机/端口/密码，以及 RAGFlow PostgreSQL、Elasticsearch、MinIO 的地址、端口和凭据。
随后重新运行 `start-full.sh` 即可；业务代码、镜像和 RAGFlow 源码都不修改。

`local` 启动会先识别 App 数据库状态：空库执行现有 0001～0006，合法旧 Revision 升级
后登记 `V1.0.0` 并执行 V1.1.0 共享知识库增量 SQL，已经升级的库直接跳过。旧业务
数据未按批准流程清空，或表集合、Revision、发布字段互相矛盾时脚本
会停止并保留现有数据，绝不通过删除 Volume 重建。最终仍运行相同的只读 Schema 门禁。

外部模式不会启动、停止或删除本机存储容器，也不会迁移旧数据。共享数据库服务器必须
为本项目分配独立 Database/用户、Redis Stack 实例、Elasticsearch 索引命名空间和
MinIO Bucket，不能与其他业务复用同一逻辑命名空间。

`external` 模式只要求连接地址和凭据，私有配置可以不包含 `POSTGRES_IMAGE`、
`REDIS_STACK_IMAGE`、`ELASTICSEARCH_IMAGE`、`MINIO_IMAGE`。如果还没有真实外部
服务器，可以使用独立的空存储模拟环境完成切换演练；镜像只写在模拟服务器自己的
Compose 中，具体步骤见
[`docs/LOCAL_EXTERNAL_STORAGE_REHEARSAL.md`](../../docs/LOCAL_EXTERNAL_STORAGE_REHEARSAL.md)。

统一 Redis Stack 的逻辑 DB 固定为：DB 0=精确/语义缓存、DB 1=RAGFlow 内部状态、
DB 2=Celery Broker。三者共享内存、AOF 和故障域，不属于强安全隔离；完整的
合并边界、回退方法和后续调度优化见
[`docs/REDIS_UNIFICATION_AND_NEXT_STEPS.md`](../../docs/REDIS_UNIFICATION_AND_NEXT_STEPS.md)。

统一 Redis 的脱敏运行报告可以随时执行：

```bash
./deploy/local/redis-runtime-report.sh
```

报告只读取 DB0/1/2 Key 数、摄取/维护队列长度、未确认消息、内存、AOF 和拒绝连接
统计，不显示密码，也不会删除或修改任何 Key。

## 6. 启停、状态和实时日志

```bash
./deploy/local/preflight-full.sh
./deploy/local/start-full.sh
docker compose --env-file deploy/local/.env.full \
  -f compose.local.yml ps
docker compose --env-file deploy/local/.env.full \
  -f deploy/local/docker-compose.storage.yml ps
docker compose --env-file deploy/local/.env.full \
  -f compose.local.yml logs -f --since 10m \
  rag-gateway rag-worker ragflow-server ragflow-task-executor
./deploy/local/stop-full.sh
```

`stop-full.sh` 只停止运行时容器；存储容器和数据卷保持不变。

## 7. 健康检查和失败策略

- `/health/live`：Gateway 进程存活；
- `/health/ready`：应用数据库、Redis、Profile、暂存卷、RAGFlow、模型端点和缓存索引；
- 启动预检：真实 Chat、单条/批量 Embedding、实际维度、合成图片 OCR，以及已启用的
  Rerank/ASR；
- `GET /models` 不是强制能力：端点返回 404/405 时以启动真实调用为事实来源；若端点
  提供标准清单则持续校验配置模型，401/403/5xx 仍判为不可用；
- 统一 Redis Stack 故障：缓存会绕过，但 Celery 与 RAGFlow 内部状态也会同时不可用，
  因此 Ready 必须报告不可用，不能再把它当成“仅缓存降级”；
- RAGFlow/索引故障：Ready 为 unavailable，不能返回未经边界验证的旧缓存；
- Rerank 故障：保留 RRF 排名并输出 Warning，不返回 500。

## 8. Smoke 与质量门禁

所有验证只使用非业务 Fixture：

```bash
./deploy/local/run-gateway-model-smoke.sh
./deploy/local/run-ragflow-smoke.sh --parse-timeout 600
uv run python tools/gateway_e2e_smoke.py \
  --output smoke-results/gateway-e2e.json
uv run python tools/gateway_image_smoke.py \
  --output smoke-results/gateway-image.json
uv run python tools/gateway_resilience_smoke.py \
  --output smoke-results/gateway-resilience.json
```

最后确认 Fixture、临时 Dataset、文档和缓存已清理。`ragflow-smoke-embedding` 只属于
独立 `smoke` Compose Profile，不是正常开发模型。

## 9. 备份和恢复

PostgreSQL 恢复 Smoke：

```bash
uv run python tools/postgres_backup_restore_smoke.py
```

完整恢复必须对齐同一恢复点：共享实例中的 App/RAGFlow 两个 PostgreSQL 数据库、
MinIO、Elasticsearch、统一 Redis Stack AOF，然后执行 Migration/Bootstrap、映射
对账、RAGFlow/Gateway Smoke 和 Scope 隔离
复测。只恢复 PostgreSQL 不能恢复原文对象和搜索索引。

## 10. 本地与生产边界

本地环境文件只是开发手段；生产应使用 Secret Manager、TLS 入口、受控 RAGFlow 运维
通道、容量/配额、监控告警、联合备份和数据合规审查。兼容端点是否允许发送业务文本、
图片、音频或候选 Chunk，必须在真实数据接入前单独确认。

## 可选 PDF 整页视觉解析

`vision-pdf@1` 使用 RAGFlow 官方 VisionParser：将 PDF 页面渲染成图片，再逐页调用
当前 `RAGFLOW_VISION_MODEL` 注册引用。该引用来自已验证的环境配置，Profile 中的
`${RAGFLOW_VISION_MODEL}` 仅由后端 Adapter 定点替换，不支持任意环境变量展开。
DeepDOC 默认策略和已有文档保持原样；选择新策略不会自动重新解析旧文件。

代码更新后按正常本地启动流程构建镜像并导入新增 Profile。创建测试知识库时，将
`parser_profile` 设为 `vision-pdf@1`；其余分块、Embedding 和检索 Profile 保持原值。
也可以在上传 PDF 的 multipart 表单中显式提交 `parser_profile=vision-pdf@1`，仅影响
该次文档选择。前端若未提供这个选项，可使用 Apifox 调用现有管理 API，无需修改前端。

保持 `task_page_size: 12`：24 页仍拆成两个页面任务，每个任务内逐页调用视觉模型。
当前解析容器保持 4 核、8 GiB 和最多 8 路核心解析；API 配额与响应速度也会影响耗时。
输出位置是整页范围，不能承诺与 DeepDOC 一样精细的段落高亮；文字遗漏、表格结构、
检索结果、耗时和调用费用需要用同一文件作对照测试。测试文件页面会发送至配置的
视觉模型服务。切回 `deepdoc-default@1` 可用于新的上传或显式重解析。

## MinerU 云端解析

`mineru-cloud@1` 是可选 PDF 策略，不替换 DeepDOC、Vision 或已有 MinerU Profile。
适配服务提供 RAGFlow 的 `/file_parse`，内部使用官网申请上传 URL → PUT 文件 →
轮询任务 → 下载 ZIP。仅调整 ZIP 根目录与内容列表名称，保留官方内容和图片字节。
无需修改 RAGFlow 官方源码，也无需安装本地 MinerU/OCR 模型。

### 本地启用

在仓库根目录执行。将 `MINERU_CLOUD_API_TOKEN` 填入统一私有配置
`deploy/local/.env.ragflow`，保留引号，不保留占位符尖括号。不要把真实值提交 Git。
启动流程会将这两项同步到 `.env.full`，Compose 仅向适配服务注入。
`MINERU_CLOUD_MODEL` 可选 `vlm` 或 `pipeline`，默认 `vlm`。

```bash
# 正常构建镜像并导入策略；Token 已填写时自动启动 MinerU 并注册模型。
./deploy/local/start-full.sh

```

在现有上传接口 multipart 中提交 `parser_profile=mineru-cloud@1`；不需要新增 Gateway
路由。前端本次未改动，尚未显示该选项时可以用 Apifox。旧文档不会自动重新解析。
更新 Token 后重新运行 `./deploy/local/start-full.sh`，自动刷新配置并应用到容器。
未填写 Token 时跳过 MinerU；填写后若健康检查或注册失败，启动脚本会报错停止。
停止可选服务时使用同一个根 Compose 执行 `stop mineru-cloud-adapter`。
`./deploy/local/stop-full.sh` 也会停止该服务；无需维护第二个 Compose 文件。

### 行为与限制

- 每个请求提交完整 PDF。该策略使用大 `task_page_size` 避免 RAGFlow 把同一整份
  PDF 当作多个页面任务重复发往官网；不支持页范围解析。官网限制仍是最多 200 页。
- 适配层输入最多 100 MiB，ZIP 下载最多 128 MiB，解压内容最多 256 MiB。
  最多两份文档同时处理，满载返回明确忙碌错误；不代表保证两份文档的完成时间。
- 等待官网最多 1500 秒，创建任务不自动重试。进度查询的超时、429/5xx 最多连续
  重试两次；文件或任务失败不会静默回退 DeepDOC。上层重试可能新建官网任务。
- 健康检查只验证配置存在，不验证 Token 权限、余额或真实解析质量。
- ZIP 目录、内容列表及图片不兼容时明确报错，不把仅有 Markdown 的结果冒充完整结果。
  内容列表使用官方 v1 数组格式，暂不支持只有 content_list_v2 的结果。
- RAGFlow v0.26.4 后续可能调用租户视觉模型补充图片描述。适配服务自身不调用其他模型；
  不能承诺最终 Chunk 与官网原始输出字节一致，RAGFlow 仍负责分块和索引。
- Token 只发往 `https://mineru.net/api/v4`；仅允许已核实的官网上传/CDN 主机，禁止跳转。
  官网更换下载主机时应核实后更新白名单。服务只开放容器内网，不可直接暴露公网。
- ECS 已同步可选服务及自动注册流程，启用方式见 [ECS 操作入口](../ecs/README.md#可选-mineru-官网-vlm-解析)。

### 验证

先跑 `tests/unit/test_mineru_cloud.py`，使用模拟官网响应验证请求、异常和 ZIP 转换。
配置真实 Token 后使用合成 PDF 做端到端验证；没有真实调用证据时不能称为已联调成功。
如需恢复原策略，新上传选择 `deepdoc-default@1` 或 `vision-pdf@1` 即可。

接口依据：[MinerU 官网 API](https://mineru.net/apiManage/docs)、
[RAGFlow v0.26.4 MinerUParser](https://github.com/infiniflow/ragflow/blob/v0.26.4/deepdoc/parser/mineru_parser.py)。

### 本机 PDF 默认策略

`compose.local.yml` 的应用公共环境设置 `RAG_PDF_DEFAULT_PARSER: mineru-cloud@1`。
自动上传 PDF（含新版本）默认使用官网 VLM，手动传入 Parser 仍优先，其他格式沿用原规则。
删除该配置并重建应用容器可恢复知识库默认路由；不会重新解析已有文档。
此设置仅在本地 Compose 启用，ECS 默认值不变。MinerU Token 必须已配置。

### 本机 10 并发测试配置

MinerU 适配容器限制为 1 CPU、2 GiB，`MINERU_CLOUD_MAX_CONCURRENCY=10`；
RAGFlow 执行器保持 4 CPU、8 GiB，任务和切块构建并发均为 10。
本地 Celery Worker 也设为 10 个进程，避免仅两个文件能进入解析；这些进程另占内存。
适配器未设置该变量时仍为 2，因此 ECS 并发不变。修改后运行 `./deploy/local/start-full.sh`。
同时上传 10 份 PDF，检查最终切块、失败率及内存峰值；这只是测试上限，
不保证官网同时执行 10 个任务，超过适配器上限的请求仍返回忙碌。

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

### 并发配置与取消保护

在统一私有配置 `deploy/local/.env.ragflow` 中设置以下参数，再运行 `start-full.sh`：

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `WORKER_CONCURRENCY` | 10 | Worker 处理进程数 |
| `RAGFLOW_MAX_CONCURRENT_TASKS` | 10 | RAGFlow 任务数 |
| `RAGFLOW_MAX_CONCURRENT_CHUNK_BUILDERS` | 10 | 切块构建数 |
| `MINERU_CLOUD_MAX_CONCURRENCY` | 10 | 官网在途请求数 |
| `MINERU_CLOUD_RESULT_CONCURRENCY` | 2 | 同时下载与转换结果数 |

配置会生成到 `.env.full`；Worker 命令不再写死 10。请求过多仍可能返回忙碌，
官网额度和全栈内存容量需独立压测。Docker 总内存与各容器内存上限本次保持不变。
API 取消不覆盖 Worker 租约；进度和成功写回不能覆盖已提交的取消请求。
心跳异常或所有权丢失会取消当前执行协程，让任务保留到租约恢复。
取消本机 HTTP 等待不代表官网已接受的任务会停止执行或停止计费。


### 手动选择 MinerU 解析图片

本地管理端上传 PNG、JPG/JPEG 时选择「MinerU 云端解析」，前端为图片提交
`mineru-image@1`，PDF 仍提交 `mineru-cloud@1`。图片默认自动路由保持原策略。
Worker 在上传 RAGFlow 前把静态图片无损包装为单页 PDF，不缩放；限制为 1600 万像素，
转换后的 PDF（含附件）不超过 100 MiB。原图字节保存在 PDF 的 `original-image` 附件中，
本地文档记录继续保留原图名称、大小和摘要；现有图片预览接口能力不因此扩大。
此方案不能保证信息图的跨列阅读顺序和箭头关系识别效果，应以解析后的切块为准。

重新构建本地应用镜像并运行 `bootstrap all` 导入新策略后，重建 Gateway/Worker/Scheduler
使其读取新配置；使用正常 `./deploy/local/start-full.sh` 也会完成这些步骤。
已有图片切换到该策略需要重新上传；已使用该策略的图片可以正常重解析。
