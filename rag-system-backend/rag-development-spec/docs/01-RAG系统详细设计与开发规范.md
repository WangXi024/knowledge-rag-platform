# RAG 系统详细设计与开发规范

本文是当前实现的精简架构约束。接口字段以[OpenAPI](../openapi/rag-api-v1.yaml)为准，
目录定位见[代码结构](../../docs/CODEBASE_STRUCTURE.md)，原生图谱细节见
[GraphRAG 接入与验收](../../docs/NATIVE_GRAPHRAG.md)。

## 1. 层次与职责

| 层次 | 代码入口 | 职责与依赖边界 |
| --- | --- | --- |
| API | `src/rag_service/api` | HTTP/SSE、参数和身份上下文、错误映射、事务与依赖组装；不把上游响应原样暴露 |
| Application | `src/rag_service/application` | 摄取、任务、检索、回答、引用与图谱用例；通过领域端口使用外部能力 |
| Domain | `src/rag_service/domain` | 资源、版本、状态、错误及端口契约；业务隔离规则不依赖供应商模型名称 |
| Infrastructure | `src/rag_service/infrastructure` | PostgreSQL、Redis、RAGFlow、模型和媒体适配；校验外部协议与范围 |
| Bootstrap / Workers | `bootstrap`、`workers` | 不可变 Profile 导入、结构/依赖预检、Celery 调度及租约保护执行 |

固定 RAGFlow `v0.26.4`，仅通过公开 HTTP API 调用；应用不得直接读写其数据库、
Elasticsearch 或对象存储。应用 PostgreSQL 保存业务事实，Redis 负责缓存与任务通知，
不能把队列消息当作 Job 状态事实。一个数据库 Session 不得跨并发协程共享。

## 2. 普通摄取与检索流程

1. Gateway 校验请求与当前可访问资源，将文件原子发布到共享暂存区，提交文档和 Job，
   返回接收状态；接收不等于可检索。
2. Celery 唤醒 Worker；Worker 从 PostgreSQL 认领任务，按租约、心跳和检查点上传、
   解析、建立索引，核对结果后发布活动文档/版本，推进知识库内容 revision。
3. 在线请求先解析授权知识库与活动文档范围，再执行 Query Rewrite、向量/BM25/RRF、
   已启用的侧车导航、可选 Rerank 与上下文组装。过滤条件不得在可选路线中丢失。
4. 回答由配置的 Chat 模型生成，引用必须映射到当前可见活动文档的原始 Chunk；
   缺乏证据时按无证据/拒答协议处理。SSE 的接收、增量内容与最终校验结果分开表达。
5. 缓存读写包含调用主体、资源范围、版本、过滤与策略边界，命中后仍按现有协议校验
   引用和版本。查询失败不能把其他租户或旧版本结果作为兜底。

文档更新先建立独立候选版本，成功才切换活动指针；失败保留旧版。具体流程见
[文档版本更新](../../docs/DOCUMENT_VERSION_UPDATES.md)。

## 3. 原生图谱流程

全局 `RAGFLOW_GRAPH_ENABLED` 与知识库图谱默认关闭。启用需显式 Graph Chat 三元组，
模型 Bootstrap 登记独立 Chat 引用，运行时仅接收开关与非敏感引用；不隐式借用 Gateway
凭据。临时 Kimi K3 不触发代码或专用 Token 参数变更。

知识库配置引用不可变 `native-graph-light@1`。构建写入 `GRAPH_BUILD` Job，由 Worker
配置原生图谱子参数、提交并轮询任务，完成后重验来源、内容和配置版本才发布图谱代数。
提交结果未知不得自动重复构图；取消必须观察明确终态，任务记录消失不能证明已停止。

在线只有当前版本 READY 图谱才参与导航；一期模式为 `native_graph_read_v1`，通过
官方 Graph Read 读取最多 256 个节点、1024 条边的完整图，按完整实体词种子和一跳
邻居生成有限导航词，再经普通同库检索回到原始 Chunk。图谱结构描述不直接成为引用；
无种子、空图或超限时降级。实际 v0.26.4 官方 KG 聚合有异步向量调用/分数回读缺陷，
`use_kg=true` 仅保留显式诊断，在线不调用；详见[接口映射](03-RAGFlow接口映射.md#83-原生图谱的结构导航与-kg-聚合限制)。
模式和 `native_graph_structural_navigation` 警告共同说明真实路线，缓存与旧 `native`
模式隔离，不把普通 Chunk 命中当作 KG 聚合成功。文档/元数据过滤会旁路原生图谱。
上游全部物理文档 ID 必须
与本地全部 READY 活动主记录严格一致，候选或旧物理版本残留会阻断图谱路径。

## 4. 变更与交付约束

- Repository 查询显式传资源 Scope；请求审计、幂等和缓存仍保留调用主体边界。
- Profile 的名称、版本和 checksum 不可变；改变含义新增版本，不能覆盖旧种子。
- API、状态、错误、Schema 或环境变化同时同步规格、针对性用例和部署说明。
- Schema 用版本 SQL 演进，不在普通请求或生产启动阶段自动创建缺失结构。
- 测试和构建串行执行，受限本机并发为 `1`；模型调用、真实集成与 Mock 证据分开记录。

四组最小模型 API 请求通过只证明所测调用形态可用。原生图谱的真实构建、检索、
原文引用及变更/取消完整流程仍待验收，不得据代码或模拟用例宣称真实集成完成。
