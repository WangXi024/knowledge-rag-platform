# ADR-0001：早期本地模型与 RAGFlow Profile

状态：已被 ADR-0004 取代

原始日期：2026-07-27

取代日期：2026-08-10

## 历史说明

本 ADR 曾固定本机模型、特定远端供应商和早期 Profile，用于证明 RAGFlow v0.26.4、
PostgreSQL、Elasticsearch、MinIO、Valkey、Gateway 与本地模型能够完成最小闭环。

这些厂商专用字段和固定模型值已经从当前代码、示例配置和部署流程中移除。新部署不得
依据旧值配置环境，也不应把旧 Profile 选作当前默认。

## 仍然有效的架构约束

- 固定使用官方 `infiniflow/ragflow:v0.26.4`，不修改或 Fork RAGFlow 源码；
- RAGFlow 元数据与 Gateway 业务数据使用相互独立的 PostgreSQL；
- Apple Silicon 只对 RAGFlow Server 与 Task Executor 设置 `linux/amd64`；
- RAGFlow 管理端口在本地开发时只绑定 loopback；
- Parser、Chunk、Embedding 和 Retrieval Profile 必须版本化；
- Embedding 模型或维度变化时必须建立新索引世代；
- Rerank 必须是真实 Cross-Encoder，不能用向量余弦相似度冒充。

## 当前决策

当前模型接入、通用变量、协议边界和迁移规则统一以
[ADR-0004](ADR-0004-OPENAI-COMPATIBLE-MODELS.md) 为准。
