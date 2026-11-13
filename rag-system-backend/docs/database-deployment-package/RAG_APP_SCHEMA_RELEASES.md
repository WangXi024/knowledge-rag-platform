# 自研 RAG Gateway PostgreSQL Schema 发布记录

> 历史参考：本文保留改造前的流程与审计说明。2026-09-07 起，部署与 SQL 执行以[当前 ECS 操作入口](../../deploy/ecs/README.md)为准；本文旧 `migrations/releases`、Alembic 或一体化安装命令不再执行。旧文件可在 Git 检查点 `335d418` 查阅。

本文只描述 `rag_app`。它把开发期的六次 Alembic 演进冻结为正式发布基线
`V1.0.0`，后续数据库变更改为 DBA 审核并人工执行增量 SQL。

## V1.2.0 原生图谱状态

当前应用要求 `V1.2.0 / 0007_native_graphrag`，共 14 张表（含版本表）。新增
`graph_state` 以 `(security_scope_id, knowledge_base_id)` 为复合主键并指向同 Scope
知识库；配置版本、内容版本、图谱代数与乐观行版本分别保存。`GRAPH` 配置和
`GRAPH_BUILD` 任务通过数据库约束验收，同一资源知识库只能有一项在途构图。

已有 V1.1.0 数据库执行
`migrations/rag_app/V1.1.0__to__V1.2.0__native_graphrag.sql`。该事务允许保留已有
知识库、文档与审计，不创建默认状态行、不自动开启或触发构图。失败时完整回滚，
重复执行则拒绝；不提供删除状态表的降级，灾备恢复使用事先验证的完整备份。

全新库按 `V1.0.0__init_schema.sql` → `V1.0.0__to__V1.1.0__shared_knowledge_bases.sql`
→ `V1.1.0__to__V1.2.0__native_graphrag.sql` 顺序安装。V1.0→V1.1 对带业务数据的
限制继续生效，不通过清空数据绕过。`local` 入口只识别上述已知链，`external` 必须
由 DBA 执行 SQL，再由应用只读验收；历史迁移保持冻结。具体命令以当前 ECS 入口为准。

## V1.0.0 基线

V1.0.0 包含 13 张表：`alembic_version`、`knowledge_base`、`document_record`、
`ingestion_job`、`config_profile`、`idempotency_record`、`query_record`、
`citation_record`、`feedback_record`、`document_index_node`、`document_entity`、
`entity_relation`、`document_version`。

`alembic_version` 同时保存四类基线信息：

| 字段 | 含义 |
|---|---|
| `version_num` | 开发期 Alembic 历史头，V1.0.0 固定为 `0006_document_version_updates` |
| `release_version` | 面向部署和 DBA 的正式 Schema 版本，当前为 `V1.0.0` |
| `schema_checksum` | V1.0.0 规范化表清单与版本信息的 SHA-256 摘要 |
| `applied_at` | DBA 完成基线登记的时间 |

## 六个开发期演进阶段

| 历史版本 | 主要变化 | V1.0.0 中的结果 |
|---|---|---|
| `0001_initial_rag_app` | 建立知识库、文档、任务、Profile、幂等、查询、引用、反馈等核心表 | 形成基础上传、解析、检索和反馈模型 |
| `0002_reuse_deleted_document_external_id` | 调整已软删除文档的外部 ID 复用约束 | 删除后允许再次使用相同外部 ID |
| `0003_document_hierarchical_index` | 新增 `document_index_node` | 支持 Parent、Summary 等层级侧车索引 |
| `0004_entity_graph_index` | 新增 `document_entity` 和 `entity_relation` | 支持实体与轻量关系侧车索引 |
| `0005_allow_section_index_nodes` | 放开 `SECTION` 节点类型 | 支持章节级检索路线 |
| `0006_document_version_updates` | 新增 `document_version` 并扩展文档更新任务 | 支持安全版本更新、切换和回滚 |

六个文件是历史事实，不再承担生产环境自动升级职责，也不得原地修改。

## V1.0.0 之后怎样升级

每次变更先确定新的语义版本，再生成一个从当前版本到目标版本的 SQL。例如
V1.1.0 只能由 `V1.0.0__to__V1.1.0__*.sql` 产生。SQL 在同一事务内完成结构变更、
必要回填和 `alembic_version` 发布信息更新。应用容器没有执行权限，也不会代替 DBA
运行该文件。

首次安装执行 `migrations/releases/V1.0.0__fresh_install.sql`，它会直接创建 V1.0.0；
已有 0006 数据库执行 `migrations/releases/V1.0.0__stamp_existing_schema.sql`
完成一次性基线登记。
