# LazyTT RAG 文档导航与版本同步说明

当前后端部署：[本地一键部署](../deploy/local/README.md) · [ECS 操作入口](../deploy/ecs/README.md) · [当前数据库字典](../deploy/ecs/数据库字典.md)。旧数据库部署包保留为历史参考。

更新时间：2026-08-24

本文描述当前工作分支的实现；具体可回滚代码基线以两个仓库各自的 Git 提交记录为准。
正式普通用户端位于同级独立仓库 `lazytt-frontend-console`，不再以内嵌前端目录作为
唯一事实来源。

## 1. 从哪里开始阅读

不同读者建议从不同入口开始：

| 读者 | 首先阅读 | 用途 |
| --- | --- | --- |
| 第一次部署的用户 | [USER_MANUAL.md](USER_MANUAL.md) | 从安装软件到上传、问答和停止服务 |
| 准备从其他系统调用 Gateway 的开发者 | [EXTERNAL_API_GUIDE.md](EXTERNAL_API_GUIDE.md) | 查看 34 个接口、Direct Tenant/User、文件解析、检索、SSE、引用和完整接入示例 |
| 第一次接触项目的开发者 | [NEWCOMER_LEARNING_GUIDE.md](NEWCOMER_LEARNING_GUIDE.md) | 用通俗语言学习 RAG、数据流和代码 |
| 准备修改代码的开发者 | [CODEBASE_STRUCTURE.md](CODEBASE_STRUCTURE.md) | 查目录、文件、类、接口和修改位置 |
| 负责人或评审人员 | [PROJECT_STATUS_AND_ROADMAP.md](PROJECT_STATUS_AND_ROADMAP.md) | 查看完成内容、缺口和下一步 |
| 验收人员 | [ACCEPTANCE_REPORT.md](ACCEPTANCE_REPORT.md) | 区分历史验收证据和验收后增量 |
| 检索算法开发者 | [MULTILINGUAL_RETRIEVAL.md](MULTILINGUAL_RETRIEVAL.md) | 理解双语 QueryPlan、加权 RRF 和 Rerank |
| 架构评审人员 | [ADR-0004-OPENAI-COMPATIBLE-MODELS.md](ADR-0004-OPENAI-COMPATIBLE-MODELS.md) | 查看供应商无关模型契约与启动探测决策 |
| 检索/摄取开发者 | [STRUCTURE_AWARE_CHUNKING.md](STRUCTURE_AWARE_CHUNKING.md) | 了解自动 Chunk Profile、动态 Parent、表格行组和上下文前缀 |
| 文档更新功能开发者 | [DOCUMENT_VERSION_UPDATES.md](DOCUMENT_VERSION_UPDATES.md) | 了解候选版本、安全发布、回滚和缓存失效边界 |

部署脚本、环境变量、Smoke 和恢复操作仍以
[deploy/local/README.md](../deploy/local/README.md) 为详细运维入口。

## 2. 本次相对旧版文档同步了什么

早期项目说明以 2026-07-27～2026-07-28 的 M0～M4 验收基线为主。其后代码已经
增加或修正了下面这些能力，本次文档在原内容上统一同步：

1. 普通用户端已经拆分到同级独立仓库 `lazytt-frontend-console`，默认端口为 3001；
   后端仓库内 `user-frontend/` 仅保留为开发参照快照；
2. 每个本地用户自动创建 PDF、Office、图片、音视频四个隔离的隐藏知识库；
3. 图片使用 RAGFlow Picture Parser，并由配置的 OpenAI-compatible Vision 模型提供
   OCR 能力；
4. 检索升级为双语 QueryPlan、知识库语言感知路由和加权 RRF；
5. 新请求默认使用带 0.40 侧车相似度门槛的 `hybrid-structure-aware@6`，旧版本继续保留兼容和回滚；
6. 新文件按正文、表格、视觉、媒体文件族自动选择 Chunk Profile；
7. 接入通用 `POST /rerank` 真实 Cross-Encoder Rerank，同时保留关闭和故障回退到
   RRF 的路径；
8. 用户端问题不携带历史上下文，但对话记录仍可回看；
9. 用户端支持最多两个不同会话并行流式回答、Markdown 安全渲染、默认收起的引用栏、
   缓存来源和 Rerank 结果提示；
10. Office 上传兼容 RAGFlow 名称/大小响应漂移，并在上传后回读文件核对大小和
    SHA-256；损坏副本会被安全删除后有限重传；
11. 新增面向小白用户的完整本地部署手册；
12. 同 Scope、同知识库的 SHA-256 完全重复文件会返回已有文档 ID，不再重复保存；
13. 检索增加问题分类、动态 Top-K、语义候选去重与相邻 Chunk 上下文合并；
14. 新增 Document Summary、Section、Parent-Child 多粒度索引和确定性结构化元数据；
    既有轻量实体与显式关系导航保持不变，最终引用始终回读 RAGFlow 原始 Chunk；
15. 新增音视频入库：FFmpeg 分段/抽帧、可配置 ASR、时间戳 Markdown、失败恢复点与
    独立媒体知识库，全程不修改 RAGFlow 源码；
16. Parent 升级为标题/长度动态分组，表格侧车重复表头，并增加不污染引用的
    上下文检索前缀；
17. 新增能力有单元/契约自动断言，用户端构建、界面测试、TypeScript 和
    ESLint 继续作为交付门禁；
18. 明确知识库 Profile 是兼容优先的默认值：文档 Parser/Chunk 会在未显式覆盖时按
    文件类型自动选择，最终版本固化到文档；管理端应查看文档详情验收实际结果。
19. 模型配置完成供应商无关迁移：Chat、Embedding、Vision 只填写 Base URL、API Key
    和模型名；Rerank/ASR 明确使用项目扩展协议。活动 Profile 改为通用引用，启动会
    自动探测 Chat 协议差异、Embedding 维度和 RAGFlow 模型实例；旧数据库行不会被
    破坏，但新配置不再保留厂商专属变量或 Profile。
20. 文档更新升级为不可变版本：候选文件独立解析和索引，成功后才切换活动版本；
    失败时旧版继续检索，支持版本历史、快速回滚、Revision 缓存失效和删除时清理
    全部 RAGFlow 物理版本。当前 `AUTO` 明确执行 `SAFE_REPLACE`，尚不伪装成 Chunk
    级增量更新。
21. Gateway 从 27 个操作增加到 30 个：新增上传候选版本、查询版本历史和回滚退役
    版本三个接口；应用数据库随 `0006` 迁移从 11 张增至 12 张表。

## 3. 仍需特别区分的状态

文档中的几个词不能互相替代：

- **已实现**：代码和配置已经存在；
- **已自动测试**：在说明的测试环境中有自动断言；
- **已真实联调**：调用过真实 RAGFlow、云端模型、数据库或第三方服务；
- **尚未验证**：没有当前环境或业务数据证据；
- **生产可用**：还需要安全、容量、监控、备份和合规等完整条件。

[ACCEPTANCE_REPORT.md](ACCEPTANCE_REPORT.md) 的主体是一个带日期的历史验收快照。
后续代码不能倒填成 2026-07-27 已经验证；新增能力会单独列在“验收基线后的增量”
部分。

## 4. 当前仍然存在的主要限制

- 用户端当前只填写 Tenant ID 与 User ID，Direct Identity 不验证身份所有权，不是生产身份系统；
- 示例环境默认开启外部 Rerank；模型调用会把对应的合成探测或业务上下文发送给
  用户配置的服务商，正式使用前需要配额和合规评估；
- 跨语言检索目前重点支持中文和英文；
- 完全重复文件已按 `content_sha256` 拒绝；内容相似但字节不同的文件不会自动删除，
  以免误删合同/说明书的新版本；在线语义去重只作用于一次召回的候选 Chunk；
- 当前实体/关系索引是确定性轻量导航，不是完整 GraphRAG，没有跨文档实体消歧、
  隐含关系推理或外部图数据库；既有 READY 文档需要重解析才会获得侧车索引；
- 大型 Excel、扫描 PDF、图片和长音视频的处理速度受 RAGFlow、云端模型配额/延迟、
  FFmpeg、内存和 Chunk 数量共同影响；音视频目前不提供说话人分离和词级时间戳；
- 当前是单机本地开发 Profile，尚未完成生产 TLS、Secret Manager、高可用、容量
  基线和完整灾备。

## 5. 文档维护规则

以后发生以下变化时，必须同步更新对应文档：

- API、错误码或字段变化：规格、接口测试、前端说明和代码结构；
- Parser、Chunk、Retrieval 或模型 Profile 变化：版本号、默认值、Bootstrap、
  Smoke 和用户说明；
- 用户端行为变化：用户端 README、新人指南和本文件；
- 部署、端口或环境变量变化：用户手册、部署 README 和预检说明；
- 新的真实联调结果：验收报告中追加带日期证据，不覆盖历史结果；
- 新的生产限制或安全边界：项目状态、ADR 和部署文档。
