# Redis 统一部署、Celery 运行优化与后续策略

更新时间：2026-08-20

## 1. 本次已经完成什么

本项目原来使用三个物理 Redis-compatible 服务：

| 原服务 | 原用途 | 合并后的逻辑 DB |
|---|---|---:|
| `cache-redis` | 精确缓存、语义缓存、RediSearch 向量索引 | DB 0 |
| `ragflow-redis` | RAGFlow 内部队列、锁和短状态 | DB 1 |
| `app-redis` | Celery Broker 和短期投递状态 | DB 2 |

现在改为一个物理 `redis-stack` 服务，固定镜像为
`redis/redis-stack-server:7.4.0-v8`。Redis Stack 完整包含普通 Redis 命令，并额外
提供 RediSearch，因此三个职责都可以使用同一个服务。

物理合并后又完成了 DB 2 的 Celery 运行链路收敛：关闭无人读取的 Result Backend，
使用 `rag:celery:` 全局 Key 前缀，将摄取与维护任务分队列，给周期唤醒消息设置寿命，
并补充连接恢复、未确认消息可见性和脱敏诊断。限流、自动扩缩容和高可用仍属于后续项。

## 2. 为什么使用 DB 0、1、2

```text
redis-stack:6379
├── DB 0：Gateway 精确缓存 + 语义缓存 HNSW
├── DB 1：RAGFlow 内部状态
└── DB 2：Celery Broker
    ├── rag:celery:rag-ingestion
    └── rag:celery:rag-maintenance
```

逻辑 DB 的作用只是防止三个组件直接使用相同 Key 名。它们仍然共享：

- 同一个进程、CPU 和网络连接入口；
- 同一份内存上限；
- 同一份 AOF 持久化文件；
- 同一个密码和服务故障域；
- 同一个重启、备份和恢复时间点。

因此 DB 0、1、2 不是租户安全隔离，也不是资源隔离。业务租户隔离继续由 Gateway 的
`security_scope_id`、缓存 Key 和缓存索引字段完成。

Celery Result Backend 已关闭。API 从不读取 Celery 返回值，业务 Job 的状态、进度、
错误、重试次数和租约都在 App PostgreSQL 的 `ingestion_jobs` 中。Redis 消息只负责
唤醒 Worker，因此 Redis 中一条过期的周期消息不会让业务 Job 消失。

## 3. 当前持久化和内存策略

存储 Compose 使用以下关键参数：

```text
appendonly yes
appendfsync everysec
maxmemory 1gb（可通过 REDIS_STACK_MAXMEMORY 调整）
maxmemory-policy noeviction
```

使用 `noeviction` 是因为同一实例里包含 Celery 队列和 RAGFlow 状态。若使用
`allkeys-lru`，缓存压力可能静默驱逐尚未消费的任务或 RAGFlow 状态，造成比“写入失败”
更难排查的数据丢失。`noeviction` 会在内存耗尽时明确拒绝新写入，便于健康检查、告警
和扩容介入。

## 4. 配置入口

私有配置 `deploy/local/.env.ragflow` 使用：

```dotenv
REDIS_STACK_IMAGE=redis/redis-stack-server:7.4.0-v8
REDIS_STACK_PASSWORD=<REDACTED>
REDIS_STACK_MAXMEMORY=1gb
REDIS_STACK_HOST=redis-stack
REDIS_STACK_PORT=6379
```

`tools/prepare_local_env.py` 根据这组配置生成 Git 忽略的 `deploy/local/.env.full`：

```dotenv
CACHE_REDIS_URL=redis://:<REDACTED>@redis-stack:6379/0
APP_REDIS_URL=redis://:<REDACTED>@redis-stack:6379/2
```

RAGFlow 的模板把 DB 固定为 1。外部存储模式只需把 Host、Port 和密码改为外部 Redis
Stack，三个逻辑 DB 的分工不变。

## 5. 从旧拓扑切换

运行：

```bash
./deploy/local/start-full.sh
```

启动流程会：

1. 将旧私有配置中的 `RAGFLOW_REDIS_PASSWORD` 原值迁移为
   `REDIS_STACK_PASSWORD`，不打印或轮换密码；
2. 生成统一的三条逻辑连接；
3. 创建新卷 `lazytt-ragflow-poc_redis_stack_data`；
4. 停止并移除三个已经退出 Compose 拓扑的旧 Redis 容器；
5. 保留三个旧命名卷，不执行 `down -v`；
6. 启动一个 Redis Stack，再启动运行时和 Bootstrap。

切换前应确认没有正在执行的上传、解析、版本更新和回答任务。缓存、队列和短状态不从
三份旧 AOF/RDB 拼接进新实例；持久业务事实仍以 PostgreSQL、Elasticsearch 和 MinIO
为准。切换后需要重新执行 Smoke，缓存会自然重新建立。

## 6. 验证方法

### 6.1 服务和逻辑 DB

```bash
docker compose --env-file deploy/local/.env.full \
  -f deploy/local/docker-compose.storage.yml ps

docker compose --env-file deploy/local/.env.full \
  -f deploy/local/docker-compose.storage.yml exec -T redis-stack sh -c \
  'for db in 0 1 2; do redis-cli -a "$REDIS_STACK_PASSWORD" --no-auth-warning -n "$db" PING; done'
```

应只看到一个 `redis-stack` 服务，并得到三个 `PONG`。

### 6.2 RediSearch 和缓存索引

```bash
docker compose --env-file deploy/local/.env.full \
  -f deploy/local/docker-compose.storage.yml exec -T redis-stack sh -c \
  'redis-cli -a "$REDIS_STACK_PASSWORD" --no-auth-warning MODULE LIST'
```

输出应包含 Search/RediSearch。应用 Bootstrap 还必须通过语义缓存索引的模型、维度和
Schema 校验。

### 6.3 端到端行为

至少验证：

1. 上传一个非业务 Fixture 并完成解析；
2. Worker 能消费 Celery 任务；
3. RAGFlow 解析任务能推进到完成；
4. 相同问题第二次请求能命中精确缓存；
5. 语义相近问题不会跨 Tenant/User Scope 命中；
6. 重启 Redis Stack 后 AOF 能恢复必要状态；
7. 所有 Fixture 清理完成。

## 7. 回退边界

合并前本地检查点：

```text
commit: ae13737
tag: checkpoint/redis-before-unification-20260820
```

旧 Redis 命名卷被保留，但回退前必须先停止当前运行时，避免新旧拓扑同时消费任务。
建议通过新分支检出检查点或使用 `git revert` 回退合并提交，不要执行
`git reset --hard`，也不要删除任何卷。由于切换后的新任务不会写入旧卷，回退后的队列
状态只代表切换前时间点；PostgreSQL 中的 Job 事实应由恢复任务重新对账。

## 8. 已实施的运行优化与后续边界

### 8.1 当前已经生效的 Celery 策略

| 能力 | 当前配置 | 解决的问题 |
|---|---|---|
| 关闭 Result Backend | Celery 不再配置 `backend`，全局 `task_ignore_result=true` | 避免 DB 2 产生无人读取的 `celery-task-meta-*` |
| 队列分责 | `rag-ingestion`、`rag-maintenance` | 摄取唤醒与暂存清理可以分别观察和路由 |
| Key 命名空间 | `rag:celery:` | 队列、绑定和未确认消息易于识别，不与其他 DB 2 Key 混名 |
| 周期消息过期 | 摄取 15 秒、维护 840 秒 | Worker 忙于长解析时，历史轮询不会无限积压 |
| 轮询周期 | 摄取每 5 秒、维护每 900 秒 | 兼顾 Job 恢复速度与空查询开销 |
| 未确认消息可见性 | 14400 秒 | 长文档仍在执行时不会因默认窗口过短被重复投递 |
| Redis 连接恢复 | 启动/运行期重试、读写超时、30 秒健康检查 | 短暂网络抖动不会立即让 Worker 永久退出 |
| 连接丢失处理 | 取消尚未确认的长任务 | 避免失联任务继续写外部系统；租约到期后由 PostgreSQL 恢复 |
| Worker 公平性 | `prefetch=1`、队列轮询 `round_robin` | 单个 Worker 不预取一批长任务，也不会固定只检查一条队列 |

这里的“摄取队列”装的不是完整文档数据，而是“尝试认领一个 PostgreSQL Job”的短
唤醒消息。Worker 消费后使用数据库行锁认领 Job；即使消息过期或 Redis 重启，Beat
下一轮仍会再次唤醒 Worker。因此消息防积压和业务不丢失可以同时成立。

目前一个 Worker 进程同时消费两条队列。队列已经逻辑分开，但还没有按队列部署独立
Worker 池或资源配额；这是小规模部署的简洁基线，不等同于生产级资源隔离。

### 8.2 脱敏诊断

```bash
./deploy/local/redis-runtime-report.sh
```

该命令只读输出 DB0/1/2 Key 数、两条 Celery 队列长度、未确认消息数、内存占用比例、
淘汰/拒绝连接和 AOF 状态。它不显示密码、不扫描业务值，也不修改或删除 Key。

### 8.3 仍需补齐监控和容量基线

监控以下指标并记录一周真实负载：

| 指标 | 建议告警起点 | 目的 |
|---|---:|---|
| `used_memory / maxmemory` | 持续超过 70% 预警，80% 告警 | 避免触发 `noeviction` 写入失败 |
| `evicted_keys` | 必须为 0 | 发现策略被错误改为可淘汰 |
| `rejected_connections` | 大于 0 | 发现连接上限或资源耗尽 |
| Redis 命令 P95 | 持续高于 20ms 调查 | 发现缓存搜索影响队列 |
| Celery 队列长度 | 结合吞吐基线告警 | 判断任务是否积压 |
| 最老任务等待时间 | 超过业务 SLA 告警 | 比单看队列长度更准确 |
| RAGFlow 解析状态停留时间 | 超过解析 SLA 告警 | 识别 DB 1 被共享负载拖慢 |

不能只用“并发量多少”定义积压。积压取决于到达速率与处理速率：当单位时间新增任务数
长期大于 Worker 单位时间完成数时，队列必然增长。应先测得不同文件类型的平均/95 分位
处理时间，再确定本机或服务器的并发上限。

### 8.4 队列资源隔离和背压仍未实施

当前已经分出两条逻辑队列：

- `rag-ingestion`：唤醒 Worker 认领上传、解析、重解析、版本更新和回滚 Job；
- `rag-maintenance`：清理暂存文件。

如果真实负载证明维护任务被长解析阻塞，再为两条队列启动独立 Worker 池，并配置不同
并发和资源限制。后续还可以按 Tenant/User 设置上传并发、排队额度和大文件低并发
通道。当前没有实现自动扩缩容、死信队列或入口限流，不能把逻辑分队列描述成完整背压。

### 8.5 Key 与危险命令保护

- DB 0 的缓存 Key 继续带模型、Profile、Revision、语言和 `security_scope_id`；
- DB 2 的 Celery Key 保持明确前缀；
- 禁止应用执行 `FLUSHALL`；维护 DB 0 时只能使用受审计的索引级/前缀级清理；
- 外部部署使用网络 ACL、TLS 和 Secret Manager；一个共享密码只是本地开发基线。

Redis ACL 很难把同一用户严格限制到某个逻辑 DB，因此生产若需要强权限隔离，应拆回
多个实例，而不是把逻辑 DB 当成安全边界。

### 8.6 背压和准入控制

- 上传入口按 Tenant/User 设置并发与队列配额；
- 大文件进入独立低并发队列；
- 队列最老等待时间超过阈值时拒绝或延后新的重任务；
- 把“已接收、排队、运行、解析、索引”分阶段返回，不让前端误判卡死；
- 所有重试必须幂等，并继续以 PostgreSQL Job 为事实来源。

### 8.7 高可用、备份和恢复

生产环境需要规划 Redis Sentinel/托管高可用、AOF 备份、恢复演练和与 PostgreSQL/
Elasticsearch/MinIO 对齐的恢复点。Redis 恢复不能代替 PostgreSQL Job 对账。

### 8.8 何时应该重新拆分

满足任一条件时，优先把 Celery DB 2 拆成独立 Redis，再评估 RAGFlow DB 1：

- 语义缓存/向量搜索造成 Redis P95 明显升高并影响任务出队；
- 内存长期超过 70%，且缓存与队列无法分别扩容；
- 一个组件重启或故障不允许影响另外两个组件；
- 生产安全要求不同账号、不同备份周期或强 ACL；
- Celery 积压导致 RAGFlow 解析状态同步延迟，或反之；
- 需要为队列和缓存采用不同的淘汰、持久化或高可用策略。

物理合并降低了本地和小规模部署成本，但不是所有生产规模下的最终形态。是否拆分应由
真实监控数据和故障隔离要求决定。
