# GraphRAG 一期验收记录

记录日期：2026-11-13。对应 GitHub Issue #1，分支
`feature/WangXi024/1/native-graphrag`。本记录覆盖本轮工作区检查和隔离环境真实联调；
原生修复检查点为 `acf1010`；最终交付提交见本分支 Git 历史和 Issue #1 关联 PR。

**第一阶段实现与受控验收通过，合成资源已清理，具备提交 PR 的条件。**
原生构图、结构导航、删除后重建、活动版本 JSON/缓存、回滚/重解析、真实 TCP SSE
及取消未确认的安全行为均已验证。GitHub 的最终 PR、合并提交和 Issue 状态以远端记录为准。
合并目标按项目所有者指示使用 `main`，GitHub Actions 保持关闭，二期尚未开始。

## 1. 自动检查与执行轮次

不同轮次和测试子集不累加为总用例数；较早通过的项目明确分列，不冒充本轮复验。

| 本轮检查 | 结果与边界 |
| --- | --- |
| 后端单元与 HTTP/OpenAPI 契约 | **629 passed、1 skipped、4 deselected**；Windows 跳过 POSIX ECS 检查，四项 POSIX 0600 模式断言显式排除 |
| 领域/应用覆盖率 | 行与分支合并覆盖率 **82.05%**，达到当前 80% 门禁；不代表每个模块均达到 80% |
| 图谱针对性回归 | **177 passed**；与全量属于不同执行轮次，不在 629 上另加 177 |
| 取消 Fixture 补充适配器回归 | **123 passed**；真实取消响应脱敏导出后执行，属于后续独立轮次，不与 629 或 177 相加 |
| PostgreSQL 18.4 复验 | **23 passed**；真实测试库中的迁移、Scope/FK、CAS、单在途约束及任务恢复相关检查 |
| 静态门禁 | Ruff、**71 个源文件 Mypy**、规格路径/引用与 **39 项 API 操作**校验通过 |
| 管理前端最终复验 | TypeScript、ESLint、**52 项**组件/代理回归通过，文件并发为 1；生产构建通过，未部署 |

| 较早已通过的检查 | 结果与范围 |
| --- | --- |
| canonical SQL 部署验收 | **1 passed**；V1.1→V1.2 带合成业务行升级无损、重复执行拒绝、注释与权限门禁 |
| Redis Stack 7.4.0-v8 | **2 passed**；真实向量索引、TTL、同键原子覆盖、过滤摘要/图谱版本/模式隔离 |
| 管理前端 | TypeScript、ESLint、**51 项**组件/代理模拟回归和生产构建通过；文案调整后的 **6 项**图谱用例以并发 1 复验通过 |
| 凭据与文件权限 | 私有文件 Git 忽略、Windows 原生 DACL 与跟踪文件敏感值检查通过，不输出密钥；最终另以两个私有环境中的 **11 个唯一真实敏感值** 对照 **385 个可发布文件**，无匹配（公开生成占位符不计作凭据） |
| 四类真实模型端点 | Chat、Embedding、Graph Chat、Vision 最小合成请求分别成功；Embedding 返回 1024 维，Vision 识别合成图片文字；不替代原生调用链验收 |

检查按阶段串行执行，没有同时运行前端构建、后端全量测试和原生构图。数据库并发
正确性用例仅使用必要的两个事务，Redis 同键用例仅使用两个小写入。不同阶段通过只
证明各自受测检查点，后续源码变化仍须按影响范围复验。

## 2. 真实构图与官方 KG 检索限制

使用官方 **RAGFlow v0.26.4**、独立合成知识库和两份微小 TXT，不使用业务数据，
不修改或 Fork 上游源码，不直接写 RAGFlow 数据库，不为临时模型改变 Token 参数。

| 实测项目 | 结果 |
| --- | --- |
| 初次两文档原生构图 | 约 **333 秒**完成；官方 Graph Read 返回 **9 个节点、15 条边** |
| 构图 Graph Chat 调用 | **6 次串行请求**；代理所见实际 usage 合计约 **22,486 tokens** |
| 原生任务日志计量 | 日志记录 **21,807 tokens**；这是上游日志口径，与代理实际 usage 分列，不叠加、不冒充全流程费用 |
| 官方 KG 冷诊断 | `POST /api/v1/retrieval`、`use_kg=true`，耗时 **71.015 秒**，HTTP 200 仅返回 **2 个普通 Chunk**，没有 KG 聚合项 |

对实际固定镜像只读核对发现，KG 候选方法调用异步 `Dealer.get_vector` 缺少
`await`，且 ES 字段回读缺少命中顶层 `_score`，导致候选向量查询/分数过滤不符合
预期。普通 Chunk 和 HTTP 200 不能证明 KG 聚合可用，不能通过更换模型或放宽引用
边界掩盖这两个版本缺陷。

一期在线使用 **`native_graph_read_v1`**：通过官方 Graph Read 读取受限原生图，以
问题中完整实体词/短语作为种子并扩展一跳，再在同库回读原始 Chunk。路线为
`ragflow:graph`，警告为 `native_graph_structural_navigation`。结构描述、关系摘要
和社区内容不作为原文证据；旧 `use_kg=true` 适配仅保留显式 `probe_kg_navigation`，
不进入在线问答。这符合一期“图谱导航辅助原文召回”的范围，不表示上游聚合实现已修复。

## 3. 删除、重建与活动版本边界

早期真实删除试验发现，上游直接重建会复用已保存/合并子图，使已删除 Doc2 的来源
留在图中。在线严格来源检查阻断该图并回退普通检索，没有将失效来源用于回答。
修复后，重建准备先持久化已确认终态的旧任务身份，通过公开 `wipe=true` 清理派生图，
再提交构图；发布前验证全图、节点、边的来源，不把进度 1 当作来源有效的证明。

| 实测项目 | 结果与边界 |
| --- | --- |
| 删除 Doc2 后清理重建 | 新图为 **generation 3 / content revision 3**，含 **6 个节点、10 条边**，来源仅剩 Doc1 |
| 同问题关闭图谱检索 | **5,031 ms**，返回 **1 个候选** |
| 同问题开启结构导航 | **6,235 ms**，返回 **1 个候选**；图谱阶段 **1,815 ms**，原文仍只来自 Doc1 |
| Noah 候选未发布 | 真实隔离检查通过；当前原版仍可普通检索，未发布 Noah 正文没有泄漏 |
| 发布 Noah 后尝试构图 | 历史物理版本保留导致活动/物理来源集合不一致，返回 **FAILED / RAG-GRAPH-004**；在收费构图 POST 之前拒绝 |
| 发布 Noah 后普通检索 | 返回内容逐字匹配当前活动 Noah 版本的独立原文 Chunk，没有以原版 Mira 正文冒充活动版本 |
| 公开 API 回滚 | 回滚 Job **SUCCEEDED**，恢复原版活动指针 `4a798…` 与 Mira，知识库内容版本为 **revision 5** |
| 公开 API 重解析 | REPARSE Job **SUCCEEDED**，内容版本推进至 **revision 6**，活动指针仍为原版；图谱为 **STALE / built_for_revision 3 / generation 3** |
| 重解析后活动版本检索 | `verify-active` 真实返回 **1 个当前 Mira 候选**，正文逐字来自活动原文，没有 Noah 泄漏 |

两次检索仅为同一微小合成场景中的单次观察，返回数量相同，不能宣称召回质量提升、
延迟稳定或业务效果改善。图谱阶段耗时也不能简单等同于两次总耗时之差。

保留历史版本是既有能力；不会为通过图谱验收而删除历史、只改 disabled 标记或放宽
来源规则。历史物理版本尚在的知识库可继续使用活动版本普通检索，但原生图谱可能
保持过期/失败并安全降级。以上回滚、重解析结果正是保留历史情况下的安全降级，
没有把旧图直接恢复为当前 READY。

首次活动版本验收工具误将在线 SHA256 稳定候选 ID 与管理 API 原生 Chunk ID 直接
比较而失败；更正为正式映射及合法侧车原文核对后通过。该失败属于验收工具断言错误，
不计为生产检索失败，也没有通过放宽活动来源或逐字原文规则来通过检查。

## 4. 真实问答、缓存、SSE 与取消

### 4.1 活动版本 JSON 与缓存

回滚后、重解析前，真实 `verify-answer` 已通过：当前 Mira 版本的 JSON 回答成功，
每条 quote 均逐字属于实际上传原文，并通过独立管理 Chunk API 回读核验；引用和
答案均没有 Noah，TXT 页码为 `null`、`positions=[]`。随后同题请求命中 **exact**，
仅改变问题首字母大小写的请求命中 **semantic**，答案与原文引用保持一致。

本次使用当前图谱模式和保留历史后的 STALE 降级边界，没有要求图谱 READY，也未把
普通检索成功冒充图谱命中。旧缓存中含假页码/坐标的响应不作为本轮通过证据。

### 4.2 真实 TCP SSE

通过实际回环 TCP/HTTP 服务验证流式路径，单进程、请求串行；这份证据不同于会缓冲
完整响应的 ASGITransport 验证。完整请求观测到 **17 个线路读取批次**：

| 从请求开始计时 | 实测 |
| --- | --- |
| 收到 `accepted` | **1.515 秒** |
| 收到首个 `answer.delta` | **8.656 秒** |
| 收到 `completed` | **9.546 秒** |

完整响应完成后关闭连接，审计仍为 **COMPLETED**；另一请求收到 `accepted` 后直接
断开 TCP，最终审计为 **CANCELLED**，没有额外调用取消 API。该实验验证了真实传输、
断连清理与审计收尾，不能证明云端已经停止执行或停止计费。

新 TXT 引用的 `page_start/page_end` 为 `null`、`positions=[]`，未沿用上游占位
坐标。该 TCP 场景出现图谱不可用的降级警告，回答仅依据当时可见的 Doc1 原文，没有
虚构已删除 Doc2 的关系；它不替代新图代数的结构导航正向证据。旧缓存中含假页码/
坐标的响应不能计作最终引用通过，JSON/exact/semantic 已另按 4.1 完成真实验证。

### 4.3 已提交原生构图的取消与恢复

使用独立单 TXT 合成知识库，通过真实 `POST .../index?type=graph` 取得原生 task ID
后请求取消，并经 CAS 恢复同一个本地 Job。公开
`DELETE .../index?type=graph&wipe=false` 返回 `data={}`，随后公开 GET index 也返回
空对象；脱敏取消 Fixture 已导出，没有伪造原生终态。

短预算验收仅在 Git 忽略的验收进程构造器中使用 **3 秒超时、0.5 秒轮询间隔**，
采用真实时钟，未 Mock 时间，也未修改产品配置或模型参数。实际本地状态保持
**CANCELLING**，最近错误为 **RAG-GRAPH-011**。再次请求本地 build 返回 **202**
并复用原 Job，没有新发原生构图 POST；这里的安全行为是合并在途任务，不是返回 409。

空任务响应仍不能证明执行器或收费已停止，因此该结果验收的是“取消待确认且不重复
提交”的语义。独立取消库随后通过公开 force DELETE 正常清理，清理 Job **SUCCEEDED**、
知识库查询 **404**；资源清理成功不用于将此前未确认的原生取消改记为已停止。

## 5. 内存、容量与已知限制

本机资源有限，原生执行器、检索依赖和测试按阶段运行。原生执行器限额 **1280 MiB**、
ES **512 MiB**、验收代理 **64 MiB**，其他依赖按隔离配置单独限额；较早 PostgreSQL/
Redis 集成测试使用独立临时环境，各限制 1 核、384 MB。没有高并发执行收费模型测试。

早期组合运行出现过 **OOM**，该失败保留为资源边界事实；分段安排后的最终受测阶段
没有 OOM。不能据此声称整套服务在该内存预算下长期稳定，也未完成容量、并发或负载验收。

- 官方 Graph Read 在 **256 节点/128 边**处截取，未返回完整数量或截断标记。客户端
  拒绝节点数 ≥256 或边数 ≥128，仅接受最多 **255 节点/127 边**且来源/结构校验通过
  的响应；流式正文上限为 **4 MiB**。超限降级，不截断后冒充完整图。
- 导航只使用完整实体词/短语与一跳邻居，不等于语义推理；没有种子时回退普通检索。
- 公开 wipe 没有预期任务 ID、CAS 或图谱锁，依靠本地唯一在途任务、租约及持久恢复
  记录保护；不得清理未知或运行中的旧任务，也不能消除上游外部操作者的竞争。
- wipe 清派生图及两个 KB 阶段完成标记，保留普通文档、Chunk、通用模型缓存；独立
  Redis 阶段 checkpoint 和 `ty2ents` 不在清理范围。当前 light 策略关闭 resolution/community。
- 原生取消会删除任务记录；受理或空响应均不能证明执行器退出，无法确认时保持
  `CANCELLING`、阻断新收费提交，不保证已停止调用或计费。
- 四项 POSIX 0600 断言仍须在 Linux 复验，Windows DACL 不替代 POSIX 文件模式；
  本轮没有完成真实 FFmpeg/ASR、音视频链路或生产 OCR 容量验收。
- 没有业务黄金集、统计显著的质量对照、全流程费用或容量结论。上述 Token 数只是
  对应原生构图观察值，不含后续问答、Embedding、诊断等全部调用，不换算总账单。

## 6. 收尾与交付边界

- 全局关闭原生能力和知识库关闭分别经过真实检索复验：都只返回当前 Mira 活动原文，
  图谱导航调用数为 0；全局关闭的验收配置不要求 Graph Chat 引用，不修改用户环境文件。
- 两个独立合成知识库均经公开删除 API 和正式 Worker 清理，删除任务为 `SUCCEEDED`，
  知识库回读为 404。Worker 的自动 inspect 在库已删除后返回 404 属预期；独立删除任务
  状态回读确认成功，不能把该验收工具的后置读取异常误记为删除失败。
- 最终六个隔离容器的 OOM 标记均为 false，随后只停止本次 Compose 项目。保留所有
  Docker 卷，没有清理其他项目、历史数据库或用户数据。
- 最终审查修复了管理页持有旧知识库列表版本时误报“待重建”的问题。就绪判断使用同次
  后台图状态中的构建/目标内容版本；后台真实 STALE 和 QUEUED 不被浏览器掩盖。
- 真实图谱与取消响应分别保存在两个脱敏 Fixture；完整私有配置和合成验收响应仍在 Git
  忽略目录。普通原文、图谱描述和上游空任务的语义没有混用。
- 本次只交付 Issue #1；目标 `main` 由项目所有者明确指定，覆盖 AGENTS 原 `dev` 约定。
  GitHub Actions 预检为关闭且运行数 0；推送、PR 和合并后需继续核对，最终结果见远端。
  用户要求提交 GitHub 后停止，因此不开始 Issue #2。

受控验收通过不等于生产负载、业务召回效果或完整云端费用已经验收。启用时仍须遵守
第 5 节的容量、历史版本、模型协议和取消限制；一期可独立关闭并回退基础混合检索。

## 7. 可复验命令

Windows 本机使用 Git 忽略的隔离测试环境；以下以 `python` 表示该环境解释器，工作
目录为 `rag-system-backend`。命令仅列复验方式，不表示本文编辑时又执行了一轮。
真实密钥和连接串不写入本文。

```powershell
python -m pytest tests/unit tests/contract --cov --cov-report=term-missing -q `
  --deselect=tests/unit/test_prepare_ragflow_env.py::test_first_run_generates_seven_distinct_passwords_and_default_email `
  --deselect=tests/unit/test_prepare_ragflow_env.py::test_repeat_run_preserves_all_existing_values `
  --deselect=tests/unit/test_ragflow_account_bootstrap.py::test_api_key_update_preserves_passwords_and_forces_private_permissions `
  --deselect=tests/unit/test_ragflow_cloud_model_bootstrap.py::test_probe_results_are_written_without_changing_secrets
python -m ruff check src migrations tests tools deploy/ecs rag-development-spec/tools
python -m mypy src
python rag-development-spec/tools/validate_spec.py
python tools/check_tracked_secrets.py
```

PostgreSQL 必须指向独立 `rag_app_test`，禁止使用开发或业务库。数据库和缓存分开运行，
通过私有进程环境分别提供 `TEST_DATABASE_DSN`、`TEST_CACHE_REDIS_URL`：

```powershell
python -u -c "import asyncio,pytest; asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy()); raise SystemExit(pytest.main(['tests/integration/test_postgres_scope.py','-q']))"
python -m pytest tests/integration/test_redis_stack_cache.py -q
```

本次忽略目录中的真实生命周期工具从仓库根目录执行，只操作 state 标识的合成知识库/
Doc1；`verify-active` 仅检索，`verify-answer` 才显式请求问答及缓存验证。公开问答 API
没有 cache-only 模式，缓存未命中可能再次生成；首个失败立即停止，不自动重试。

```powershell
python .publish-tools/native-acceptance/lifecycle.py verify-active
python .publish-tools/native-acceptance/lifecycle.py verify-answer
```

工具和完整私有响应保持 Git 忽略；只提交审核后的脱敏 Fixture，不把临时端口、合成
凭据或隔离镜像配置变为生产默认值。完整操作边界见
[NATIVE_GRAPHRAG.md](NATIVE_GRAPHRAG.md)。
