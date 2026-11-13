# RAGFlow 原生 GraphRAG 一期接入与验收

本期保持官方 RAGFlow `v0.26.4`，通过公开 HTTP API 接入原生轻量构图，新增知识库级
配置、持久化构建任务、管理界面和可选检索路线。全局与知识库默认均关闭。
既有 Gateway 侧车实体/显式关系导航继续独立工作，两者的策略、状态和检索路线不同。

本文说明当前实现及其边界，不代表原生图谱完整流程已经真实验收。

## 1. 已实现范围与验证状态

一期包括受控 `native-graph-light@1` 策略、配置和状态管理、`GRAPH_BUILD` 异步任务、
失效与取消处理，以及基于图谱导航词补召回原文的问答路径。策略固定为 `light`，实体
类型为 organization、person、geo、event、category；社区聚合与实体消歧默认关闭。
改变这些语义必须新增不可变策略版本，不能原地修改已导入的旧 Profile。

本期不提供图谱节点/边的浏览编辑、外部图数据库、社区报告问答或任意引擎参数入口。
模型抽取出的关系仍需以原文证据核验，不能据此承诺隐含关系推理的正确率。

当前应分开记录以下证据：

| 层次 | 当前结论 |
| --- | --- |
| 实现与隔离测试 | 管理、任务、适配、检索及管理端代码已有对应 Mock/契约或组件用例；它们不调用真实模型 |
| 四组模型端点 | Chat、Embedding、Vision、Graph Chat 的最小真实 API 请求已分别通过，使用合成输入；只证明所测请求形态可调用、凭据可用 |
| 模型登记与原生执行 | 隔离合成知识库已完成真实原生构图，官方图谱接口返回 9 个节点、15 条边；该小样本不代表全部 Bootstrap、内容生命周期或业务质量验收通过 |
| 官方 KG 聚合检索 | 一次冷诊断调用耗时约 71 秒，`use_kg=true` 返回 HTTP 200，但只有 2 个普通 Chunk，没有 KG 聚合项；不宣称该聚合路线可用 |
| 原生完整流程 | 配置→真实构建→可用图谱→检索→原文引用→内容变更/取消，尚未完成当前环境的全链路验收 |
| 质量与容量 | 尚无业务黄金集、收益对照、容量或并发稳定性结论 |

临时使用的 Kimi K3 不构成本项目的模型基线，不为它修改业务或 RAGFlow 源码，也不
为它追加或写入专用 Token 参数。某组最小请求成功，不能替代原生调用路径的协议验收。

## 2. 开关、模型与部署准备

| 配置 | 默认/来源 | 消费方 |
| --- | --- | --- |
| `RAGFLOW_GRAPH_ENABLED` | `false` | Gateway、Worker、Scheduler、运行时预检及模型 Bootstrap |
| `RAGFLOW_GRAPH_CHAT_BASE_URL` | 由操作者显式填写 | 仅模型 Bootstrap |
| `RAGFLOW_GRAPH_CHAT_API_KEY` | 由操作者显式填写 | 仅模型 Bootstrap |
| `RAGFLOW_GRAPH_CHAT_MODEL` | 由操作者显式填写 | 仅模型 Bootstrap |
| `RAGFLOW_GRAPH_CHAT_MODEL_REF` | Bootstrap 成功后写回的非敏感引用 | 应用运行时 |

开启全局能力时，Graph Chat 的三元组必须独立显式提供。可以自行填入与 Gateway
相同的值，程序不会隐式复制 Gateway 凭据。关闭时不要求这组三元组，也不额外调用
Graph Chat。真实值保存在 Git 忽略的私有配置或管理员环境中，不写入 Profile、API
响应或浏览器。普通业务 Compose 只注入图谱开关和登记引用；ECS 的三元组仅传给
显式执行的模型 Bootstrap 进程。

Graph Chat 使用通用 Chat Completions 文本协议。模型 Bootstrap 会发送合成显式关系
并核对 JSON 提取结果，随后登记独立的 RAGFlow Chat 实例和模型，设置租户默认
`llm_id`，回读成功后保存引用。这会实际调用模型并产生费用。Gateway 的 Chat 协议
探测结果不会自动转成原生 Graph Chat 的适配参数；模型必须通过其实际调用路径验收。

运行时只校验引用与 RAGFlow 租户默认 Chat 一致。预检返回
`graph=enabled_model_registered` 表示登记匹配，不表示某个知识库已经构图或图谱可用。
RAGFlow 共用租户的其他应用也可能使用默认 `llm_id`，启用前须核对这一影响。

应用数据库需到 `V1.2.0 / 0007_native_graphrag`，并通过既有 Bootstrap 导入新增
`GRAPH` 种子。迁移不会为旧知识库自动启用图谱或创建收费任务。数据库安装、保留数据
及外部 DBA 执行边界见[Schema 发布记录](database-deployment-package/RAG_APP_SCHEMA_RELEASES.md)。
模型操作入口分别见[本地部署](../deploy/local/README.md#原生-graphrag-的模型准备)和
[ECS 部署](../deploy/ecs/README.md)，不要以直接修改 RAGFlow 数据表替代模型登记。

## 3. 知识库管理接口

所有路径以应用知识库 UUID 为目标，并经过既有管理权限与资源范围校验；调用方不能
提交任意 RAGFlow Dataset ID。身份仍遵守项目当前 Direct Tenant/User 契约，Header
本身不提供生产身份认证。四个接口均返回 `{request_id, data}`，其中 `data` 为图谱状态。

| 方法与路径 | HTTP 成功码 | 行为 |
| --- | --- | --- |
| `GET /v1/management/knowledge-bases/{id}/graph` | `200` | 只读配置与状态；没有图谱记录的旧知识库返回默认关闭，不触发模型 |
| `PUT /v1/management/knowledge-bases/{id}/graph/configuration` | `200` | 保存完整受控配置；开启自动构建时可能同时登记构建任务 |
| `POST /v1/management/knowledge-bases/{id}/graph/build` | `202` | 构建、重建或失败重试；同库在途请求合并，不并排创建第二项构图 |
| `POST /v1/management/knowledge-bases/{id}/graph/cancel` | `202` | 登记取消意图；等待 Worker 与上游核查，不代表任务已经停止 |

配置请求示例：

```json
{
  "enabled": true,
  "profile_ref": "native-graph-light@1",
  "auto_build": false,
  "retrieval_enabled": false
}
```

`enabled` 默认 `false`；`auto_build` 与 `retrieval_enabled` 默认 `true`，但只有知识库
启用后才有作用。首次手动验收可先保持后二者关闭，构建成功后单独开启检索。
`auto_build=true` 时，首次启用、构图策略变化及内容失效可以安排任务，不承诺上传尚未完成时
立即构图成功。文档实际解析配置会继承知识库图谱子配置，普通文件解析仍不是构图
完成的信号；真正构图通过独立 Job 调用 RAGFlow 原生索引接口。

管理端在“共享知识库”选中目标后展示配置与状态，支持保存、构建/重试、取消和刷新。
轮询不会覆盖未保存的草稿，界面也不会将 `202` 当成“可用”。普通用户端没有图谱
编辑入口。配置与管理界面说明见[管理端 README](../../rag-frontend/admin-frontend/README.md#知识库图谱管理)。

## 4. 状态、版本和任务恢复

| 状态 | 含义 |
| --- | --- |
| `DISABLED` | 知识库图谱未启用 |
| `NOT_BUILT` | 已启用，尚无已发布构建 |
| `QUEUED` | 本地任务已登记，等待执行 |
| `BUILDING` | Worker 正在提交或跟踪原生任务 |
| `READY` | 构建通过发布检查；在线使用仍会核对内容版本与来源 |
| `STALE` | 内容/配置已变化或构建结果不能用于当前内容，需重建 |
| `FAILED` | 本次构建失败，查看稳定错误码与任务记录 |
| `CANCELLING` | 取消意图已登记，上游停止尚未确认 |

`target_content_revision` 记录最新的重建目标；排队阶段合并已经完成的内容变更，
构建期间也推进该目标及行版本以保护并发发布。已经提交的 Job 仍绑定自己的原始
内容快照，不能因新目标而重新定位或重复收费。当前内容版本以知识库的 `revision`
为准。`built_for_revision` 记录上次
成功发布所用的内容版本；`graph_generation` 是成功发布的图谱代数，构图不会递增
知识库内容版本。`configuration_version` 独立记录配置变化。还可通过 `job_id`、
`last_error` 和排队/开始/完成/更新时间诊断任务，公共响应不公开引擎任务标识。

只有 `READY` 且 `built_for_revision == knowledge_base.revision` 才可能用于在线检索。
上传、删除、版本切换/回滚或有关内容配置的变化会使旧图失效；自动构建开启时安排
新任务。回滚文档版本不会让旧图直接恢复可用，必须重新核对版本并重建。

PostgreSQL 保存 Job 和图谱状态事实；Celery/Redis 负责唤醒执行，Worker 复用任务
认领、租约、心跳及检查点。外部提交前记录检查点，恢复时优先回读原生任务，不能在
提交响应丢失后盲目重复收费构图。无法确认提交结果会报 `RAG-GRAPH-006`；操作者
应先核对实际任务，再决定是否手动重试。迟到任务不能跳过配置/内容版本检查发布结果。
发布阶段的行版本竞争最多局部重试三次，不重新构图；持续竞争进入既有有限任务重试，
耗尽后明确失败并允许人工重试。切换查询或自动构建配置不能把失败图谱直接标为就绪。

取消调用上游 `DELETE .../index?type=graph&wipe=false`，保留图谱数据，不删除文档。
尚未提交上游的本地任务可以直接结束；已经提交后，只有观察到明确终态才确认取消。
v0.26.4 的任务记录消失不能证明执行器已经退出，因此可能长期保留 `CANCELLING`，
并出现 `RAG-GRAPH-011`。不能把超时、空任务响应、关闭页面或重启应用当成“已停止
调用/计费”的证据；取消期间的结果也不会发布为当前 `READY`。

## 5. 检索、来源与缓存边界

### 5.1 固定版本的官方 KG 聚合限制

本次官方 v0.26.4 镜像的真实构图成功，不等于 `POST /api/v1/retrieval` 的
`use_kg=true` 能返回聚合证据。上述冷诊断没有返回 KG 聚合，结合只读源码核对发现：

- `KGSearch` 的两个同步候选检索方法调用已经异步化的 `Dealer.get_vector`，没有
  `await`；传给文档引擎的是协程，不能被识别为预期的稠密向量查询表达式。
- `ESConnection.get_fields` 未从 ES 命中顶层回读 `_score`，而 KG 候选转换依赖
  该值并以默认 `0.3` 阈值过滤；缺失分数按 `0` 处理，会过滤实体/关系候选。
- 官方公开路由只有在 KG 聚合正文非空时才插入聚合项，因此 HTTP 200 与普通
  Chunk 命中均不能证明 KG 命中。类型池由上游内部读取，不靠客户端补传实体类型修复。

对应实现见官方固定版本的 [KGSearch](https://github.com/infiniflow/ragflow/blob/v0.26.4/rag/graphrag/search.py)、
[Dealer](https://github.com/infiniflow/ragflow/blob/v0.26.4/rag/nlp/search.py) 与
[ESConnection](https://github.com/infiniflow/ragflow/blob/v0.26.4/rag/utils/es_conn.py)。
这些问题位于检索实现，与本次临时模型选择分开记录。本期不修改或 Fork 上游，
也不为临时模型改变代码或 Token 参数。

### 5.2 一期在线采用有界图结构导航

原生图谱仍是既有向量/BM25/RRF 检索的可选补充。在线流程逐库核验授权、图谱状态、
内容版本与活动来源，然后读取官方 `GET /api/v1/datasets/{dataset_id}/graph`。
图响应以流式读取限制为 **4 MiB**，解析前即拒绝超限正文。仅对最多
**256 个节点、1024 条边**的完整图启用结构导航：按问题中的完整实体词
匹配种子，再沿一跳邻居扩展有限实体名，经同库正常关键词检索回读原始 Chunk，
进入现有合并、去重、Rerank、上下文与引用校验流程。空图、超限图、无匹配种子或
无法核验的结构按原因降级为普通检索；不截断大图后冒充完整关系。

该模式明确标记为 `native_graph_read_v1`，并返回
`native_graph_structural_navigation`，表示使用原生构图结果的结构导航，不表示官方
KG 聚合检索已修复。原有 `use_kg=true` 调用仅保留在显式诊断方法
`probe_kg_navigation` 中，在线问答不调用它，也不通过后台预热掩盖冷调用耗时。
在线结构导航本身不调用 Graph Chat；构建原生图谱仍必须配置和验证原生 Chat 模型。

图谱聚合、实体描述和社区报告不会直接成为候选正文或引用摘录。来源指针也不能
绕过当前活动版本映射；答案的引用仍须回读原始文档块。没有原文证据时，继续遵守
既有无证据处理，而不是把关系描述当作原文回答。原有侧车路线 `entity:graph` 与
原生路线 `ragflow:graph` 应分开观察，看到前者不能证明调用了原生 GraphRAG。
TXT/Markdown 没有物理页面，其上游占位坐标会被清除，新引用的页码为 `null`、
位置为空；PDF 与图片沿用原定位语义。候选版本使用自己的文件名/MIME，旧文本答案
缓存中仍含页码或坐标时拒绝复用。原文内容不因此改写，Parser 的外层换行保持原样。

原生图谱不能表达本项目全部过滤条件，因此：

- 请求包含 `metadata_filter` 时跳过原生图谱，并返回 `native_graph_bypassed_metadata_filter`。
- 纯检索请求指定 `document_ids` 时跳过原生图谱，并返回 `native_graph_bypassed_document_filter`。
- 过滤条件继续由原有检索路径执行，不能先取全库图谱摘要再假装结果已受过滤限制。

**活动物理文档必须严格一致。** 构建前、完成发布前和可用图谱的在线核查，都会对比
本地知识库全部活动主记录与上游 Dataset 的全部物理文档 ID：本地记录须全部
`READY`，ID 不得缺失/重复，两端集合必须完全相等。上游候选版本、退役/残留版本、
只被 disabled 的旧物理文档、缺失文档或分页不一致都会阻断该图谱路径，典型错误为
`RAG-GRAPH-004`。这比普通 RAG 的活动版本过滤更严格。

因此，保留多个物理历史版本的知识库可能继续正常使用普通检索，但暂不能构图或
使用原生图谱。应先完成摄取与版本生命周期处理，按已授权流程核查清理；不能为
绕过校验直接清库、删除版本历史或只修改上游 disabled 标记。现有版本保留能力与
这一严格条件的操作兼容性仍是手动验收重点。

图谱不可用、未就绪、过期或为空时保留普通检索。不可核验的图谱/来源异常会同时
跳过该次答案缓存的读取或写入，防止缓存绕过校验。缓存边界包含内容版本、图谱
配置/状态/代数摘要及图谱使用模式，关闭或重建后不会沿用旧图谱边界的答案。
`native_graph_read_v1` 同时进入精确与语义缓存边界，与旧 `native` 聚合导航模式隔离。

可以查看以下公开诊断，而无需查看内部缓存键：

| 诊断 | 观察位置与含义 |
| --- | --- |
| `ragflow:graph` | `query_plan.retrieval_routes`：本次执行过原生图结构导航的原文补召回路线，不保证最终引用来自这条路线 |
| `native_graph_read_v1` | `graph_diagnostics.mode`：使用有界官方图结构，不是官方 KG 聚合命中 |
| `native_graph_structural_navigation` | `warnings`：本次采用有界实体种子/一跳邻居导航，结构描述没有进入回答证据 |
| `native_graph_retrieval_disabled` | `warnings`：知识库关闭了图谱检索 |
| `native_graph_not_ready` / `native_graph_stale` | `warnings`：状态或内容版本不符合使用条件 |
| `native_graph_empty` | `warnings`：快照无实体或导航没有可用词，普通检索继续 |
| `native_graph_unavailable` | `warnings`：状态、来源、协议或依赖无法核验，图谱路线降级并阻止相关缓存 |
| 两类 `native_graph_bypassed_*_filter` | `warnings`：因过滤边界主动跳过图谱 |

原生路线最多检查/调用两个知识库，状态检查与导航各有整条路线 8 秒预算。预算耗尽返回
`native_graph_budget_exhausted`，基础检索继续且该次不缓存。纯检索响应的 `graph_diagnostics`
提供模式、版本摘要、目标图代数、实际导航调用数、回读原文候选数与图谱阶段耗时；诊断
控制台可直接查看原始响应，不包含引擎 ID、密钥或聚合正文。
此处的导航调用数是结构导航次数，不能当作新增 Graph Chat 调用次数或费用计量。

## 6. 关闭与回退

只回退问答路线时，将知识库 `retrieval_enabled=false` 后保存；若 `auto_build` 仍开启，
内容变化仍可能安排构建。停止该知识库后续构建需将 `enabled=false`，并跟踪在途任务
是否确认取消。停用或构图策略变化会使在途任务进入取消待确认；单独切换查询/自动调度不会重收构图费用。

部署级回退将 `RAGFLOW_GRAPH_ENABLED=false` 并按部署流程重新加载应用配置，可停止
新的原生构建和在线图谱路线，继续既有普通检索。这个开关不会删除图谱数据、恢复
RAGFlow 租户旧默认 Chat、回滚数据库结构，也不证明旧的上游任务已结束；先单独
处理在途任务。Schema 不通过删除状态表降级，数据库恢复遵守备份与发布流程。

## 7. 手动验收清单

使用小型合成文档和独立测试知识库，顺序执行，每次只保留一个构图任务。内存有限
时测试并发为 `1`，不要同时跑多套测试/构建或为本清单额外启动完整服务栈。以下
是待执行的验收步骤，不是已经完成的事实；记录请求 ID、状态、版本与脱敏错误。

1. 核对应用 Schema、不可变 Graph Profile、全局关闭状态和普通检索基线。确认旧知识库
   默认关闭，重复 GET 不登记构建，不在日志或浏览器出现模型密钥。
2. 显式配置 Graph Chat，按部署入口执行模型 Bootstrap，验证登记引用和租户默认值
   回读一致。把此项与四组最小 API 请求的证据分开记录。
3. 在隔离测试知识库上传少量明确写出实体关系的文本，等待全部文档 READY，核查
   上游物理 ID 集合与本地活动集合一致；先设 `auto_build=false`、`retrieval_enabled=false`。
4. 单次请求构建，观察 QUEUED/BUILDING，确认任务接收并未直接变为 READY。成功后
   核对 `built_for_revision`、当前知识库 revision 与图谱代数；重复在途请求不产生第二项任务。
5. 开启图谱检索，用跨文档但有明确原文依据的问题检查 `ragflow:graph`、
   `native_graph_read_v1` 与结构导航警告，核对完整实体种子/一跳扩展、候选和最终
   引用；逐条回读原始 Chunk，确认结构描述、图谱聚合和生成关系均未成为 quote。
   空图、无种子、超过节点/边预算时应回退；官方 KG 聚合仅另行显式诊断，不能冒充在线命中。
6. 带元数据条件、限定文档分别检索，确认旁路警告和普通检索过滤结果；对租户级测试
   知识库，用另一租户身份访问同一 UUID，确认不能读配置、提交任务或获得该库候选
   与缓存。系统共享知识库按其明确授权范围另验，不能把合法共享误判为越权。
7. 顺序测试新增/删除、候选版本发布和回滚，确认旧图立即失效；在候选或旧物理版本
   残留时观察严格边界阻断。没有获授权的清理流程前，不删除历史版本来强行验收通过。
8. 分别在提交前和提交后请求取消。对后一种记录能否观察到上游明确终态；若记录被
   删除、状态不可确认或超时，保留 CANCELLING，不报告已停止计费。关闭与恢复页面
   后状态应来自服务端。
9. 顺序验证模型失败、原生任务/网络异常、提交结果未知与 Worker 恢复，确认不盲目
   重提收费任务、旧结果不迟到发布，普通检索降级且不可核验状态不命中旧缓存。
10. 最后关闭检索/知识库/全局能力，验证普通检索回退、缓存隔离与数据保留。单独记录
    原生完整流程、引用正确率和资源峰值，再决定是否在真实知识库启用。
