# RAGFlow v0.26.4 接口映射

## 1. 目的

本文是 `RagFlowAdapter` 与独立 `RagFlowGraphAdapter` 的开发契约。只允许通过
RAGFlow v0.26.4 公开 HTTP API 接入业务能力；Adapter 不直接访问 RAGFlow
PostgreSQL、Redis、对象存储或 Elasticsearch。

官方基线：

- `https://github.com/infiniflow/ragflow/blob/v0.26.4/docs/references/http_api_reference.md`
- `https://github.com/infiniflow/ragflow/blob/v0.26.4/docs/guides/dataset/select_pdf_parser.md`

所有接口在首次开发时必须用固定镜像执行真实 Smoke，保存脱敏响应 fixture。官方文档与实际镜像不一致时，先记录偏差并修正规格，不能在 Adapter 中隐式猜测。

## 2. Adapter 规则

- Base URL 和 API Key 从配置读取。
- 所有请求带 `Authorization: Bearer <RAGFLOW_API_KEY>`。
- 成功必须同时满足 HTTP 2xx 与响应 `code == 0`；二进制接口除外。
- RAGFlow 原始 ID 只保存在映射表和内部 DTO。
- 普通引擎方法接收 `SecurityContext`；图谱方法显式接收资源 `security_scope_id`、
  本地知识库 UUID 与图谱代数。两者均须按已授权 Scope 查询本地映射，才能得到
  RAGFlow Dataset/Document ID。
  禁止提供只按 RAGFlow ID 或只按项目 UUID 访问下游的公开方法。
- 对外错误使用稳定项目错误码，原始 message 只进入脱敏调试日志。
- JSON 使用宽松读取未知字段、严格检查必要字段。
- 429、502、503、504 和连接失败可有限重试；上传等非幂等请求需要孤儿核对。

## 3. 接口矩阵

| 内部能力        | RAGFlow API                                                        | 主要功能                                         | 使用场景                                    |
| ----------- | ------------------------------------------------------------------ | -------------------------------------------- | --------------------------------------- |
| 健康检查        | `GET /api/v1/system/healthz`                                       | 检查 RAGFlow 及其数据库、Redis、文档引擎、对象存储等依赖是否正常      | Gateway 启动、就绪检查和运维监控                    |
| 创建 Dataset  | `POST /api/v1/datasets`                                            | 在 RAGFlow 中创建知识库，并配置 Embedding 模型、解析器、切分方式等  | 上游调用“创建知识库”后，由 Gateway 转换调用             |
| 查询 Dataset  | `GET /api/v1/datasets?id=...`                                      | 查询知识库是否存在、配置和当前状态                            | 查询详情、状态核对、失败补偿                          |
| 更新 Dataset  | `PUT /api/v1/datasets/{dataset_id}`                                | 更新知识库描述、解析器、切分和相关安全配置                        | 修改知识库配置                                 |
| 删除 Dataset  | `DELETE /api/v1/datasets`                                          | 删除指定知识库以及 RAGFlow 中的相关数据                     | 上游删除知识库时调用；必须指定 ID，禁止全量删除               |
| 上传 Document | `POST /api/v1/datasets/{dataset_id}/documents`                     | 把 PDF、Office、图片或媒体派生 Markdown 上传到指定知识库       | 上游上传后由 Worker 校验并转交；音视频先在 Gateway 预处理 |
| 更新 Document | `PUT /api/v1/datasets/{dataset_id}/documents/{document_id}`        | 更新文档元数据、解析器和切分配置                             | 写入业务元数据，或者重新解析前调整配置                     |
| 列出 Document | `GET /api/v1/datasets/{dataset_id}/documents`                      | 查询知识库中的文档列表、解析状态和进度                          | 文档列表查询、任务状态轮询和失败补偿                      |
| 下载 Document | `GET /api/v1/datasets/{dataset_id}/documents/{document_id}`        | 从 RAGFlow 获取上传的原始文档文件                        | 内部核对或运维使用；当前不直接提供给上游                    |
| 删除 Document | `DELETE /api/v1/datasets/{dataset_id}/documents`                   | 删除指定文档及其对应的 Chunk、Embedding 和索引              | 上游删除文档时调用；必须指定文档 ID                     |
| 内置解析        | `POST /api/v1/datasets/{dataset_id}/chunks`                        | 启动 RAGFlow 内置解析流程：解析文档、切分 Chunk、生成向量并建立索引    | 使用 DeepDoc、MinerU、Plain Text 等内置解析器处理文档 |
| 停止解析        | `DELETE /api/v1/datasets/{dataset_id}/chunks`                      | 请求停止正在执行的内置文档解析任务                            | 用户取消任务、任务超时或系统停止处理                      |
| 自定义 Ingest  | `POST /api/v1/documents/ingest`                                    | 使用 RAGFlow Ingestion Pipeline 启动、重新执行或取消文档处理 | 知识库使用自定义 Pipeline，而不是内置解析模式时使用          |
| 列出 Chunk    | `GET /api/v1/datasets/{dataset_id}/documents/{document_id}/chunks` | 查询文档解析后产生的文本片段、页码、坐标等信息                      | 检查解析结果、验收切分质量、问题排查                      |
| 切换 Chunk 可用性 | `PATCH /api/v1/datasets/{dataset_id}/documents/{document_id}/chunks` | 按 `chunk_ids` 批量设置 `available` | 文档版本发布、回滚和数据库失败补偿 |
| 纯检索         | `POST /api/v1/retrieval`                                           | 根据问题，从一个或多个知识库中检索最相关的 Chunk，并返回内容、分数和位置信息    | RAG 问答的检索阶段；该接口本身不调用大模型生成答案             |
| 图谱配置 | `GET /api/v1/datasets?id=...` 与 `PUT /api/v1/datasets/{dataset_id}` | 回读并合并受控 `parser_config.graphrag`，保留原有 Parser 配置 | 原生构图前应用不可变图谱策略；不直接改数据库 |
| 原生构图/状态 | `POST/GET /api/v1/datasets/{dataset_id}/index?type=graph` | 提交及回读原生图谱任务 | 知识库级持久 Job 调度与恢复；提交受理不等于构图完成 |
| 取消构图 | `DELETE /api/v1/datasets/{dataset_id}/index?type=graph&wipe=false` | 请求取消并保留已有图谱 | 只能报告受理；上游记录消失不能证明执行器已停止 |
| 读取图谱结构 | `GET /api/v1/datasets/{dataset_id}/graph` | 获取原生构图产生的节点和边 | 一期在线有界实体种子/一跳导航，随后同库回读原文 |
| KG 聚合诊断 | `POST /api/v1/retrieval`，`use_kg=true` | 显式诊断官方 KG 检索 | 仅 `probe_kg_navigation` 使用，不进入一期在线问答 |

## 4. Dataset 映射

### 4.1 创建

项目请求：

```json
{
  "name": "产品文档",
  "description": "产品手册",
  "embedding_profile": "embedding-openai-compatible@3",
  "parser_profile": "deepdoc-default@1",
  "chunk_profile": "general-256@2"
}
```

转换为：

其中 `general-256@2` 的 `chunk_token_num=256`，`overlap_ratio=0.10` 会由 Gateway
转换成引擎配置 `parser_config.overlapped_percent=10`。由于 RAGFlow v0.26.4 的
解析内核已支持该字段、公开 `ParserConfig` Schema 却尚未声明它，Adapter 会先发送
不含该字段的标准 `parser_config` 进行校验，同时通过公开的顶层
`ext.parser_config` 发送完整配置。RAGFlow 服务层会将完整配置展开成下方最终持久化
结果，供解析内核读取；本项目不直接修改 RAGFlow 数据库或源码。

```json
{
  "name": "<唯一安全名称>",
  "description": "产品手册",
  "embedding_model": "<model@factory>",
  "permission": "me",
  "chunk_method": "naive",
  "parser_config": {
    "chunk_token_num": 256,
    "delimiter": "\n!?;。；！？",
    "overlapped_percent": 10,
    "layout_recognize": "DeepDOC",
    "task_page_size": 12,
    "raptor": {"use_raptor": false},
    "graphrag": {"use_graphrag": false},
    "parent_child": {"use_parent_child": false, "children_delimiter": "\n"}
  }
}
```

RAGFlow Dataset 名称要求租户内大小写不敏感唯一。Adapter 应使用 `<knowledge_base_id短码>-<安全名称>` 作为内部名称，对外仍展示项目名称。

RAGFlow v0.26.4 本身不是本项目的最终用户授权边界。Gateway 在创建 Dataset
前完成 JWT 验证，并把本地 `knowledge_base` 与 RAGFlow Dataset 建立一对一映射；
列表、修改、删除和检索均先按 `security_scope_id` 查询映射。不得依赖 Dataset
名称中的短码实现安全隔离。

如果使用自定义 Ingestion Pipeline，则 `pipeline_id`/`parse_type` 与 `chunk_method`/`parser_config` 互斥，Adapter 必须在发送前验证。

### 4.2 删除

只允许：

```json
{"ids": ["<dataset_id>"], "delete_all": false}
```

任何代码路径不得发送 `delete_all=true`。

## 5. Document 映射

### 5.1 上传

请求：`POST /api/v1/datasets/{dataset_id}/documents?type=local`，字段名为一个或多个 `file`。第一阶段一个项目请求只上传一个文件，便于幂等、状态和错误归属。

响应必要字段：

```json
{
  "code": 0,
  "data": [
    {
      "id": "<ragflow_document_id>",
      "dataset_id": "<dataset_id>",
      "name": "file.pdf",
      "run": "UNSTART",
      "size": 1234
    }
  ]
}
```

必要字段缺失返回 `RAG-RF-004`。一个项目请求只允许 RAGFlow 返回一个新上传项；
Dataset ID 必须与请求目标一致。Gateway 发送的内部文件名以本地文档 UUID 开头，
因此原名和 RAGFlow 为避免重名生成的 `file(1).ext`、`file(2).ext` 属于同一个
受控名称族，其他名称仍按协议无效拒绝。

音频和视频使用同一公开上传接口，但 Worker 不把原始媒体二进制直接交给 RAGFlow。
它先调用 `MediaProcessingPort`：ffprobe 校验容器与时长，FFmpeg 把音轨切成受控 WAV，
ASR 生成带时间戳转写；视频还会有限抽帧并补充视觉描述。最终上传的是 Markdown 派生
文件，解析 Profile 为 `media-default@1`。派生文件的路径、大小、SHA-256 与 MIME 只
保存在 Job Payload 中作为重试恢复点；本地 Document 行继续保存原媒体身份。这样
RAGFlow 临时错误不会触发重复 ASR 调用，也不需要修改 RAGFlow 源码或数据库。

RAGFlow v0.26.4 的 multipart 接收曾间歇性在 Office 二进制前增加两个字节
`CRLF`。此时上传接口仍返回成功，但远端 `size` 比本地大 2，ZIP 格式的 XLSX、
DOCX、PPTX 会在后续解析阶段失败。因此 Gateway 不把大小漂移视为兼容差异：上传
后必须通过官方 Document Download 接口回读文件，大小与 SHA-256 都和暂存阶段的
权威值一致，才能建立本地映射并进入解析。

Worker 重试时先查询同一 UUID 名称族，并对已有候选执行相同回读校验。确认损坏的
候选只通过 RAGFlow 官方删除接口清理，再用确定长度的二进制最多重传一次；这样既
不会复用损坏文件，也不会不断产生 `(1)`、`(2)` 副本。跨 Dataset、缺少 ID、负数
大小、无关名称或两次完整性校验均失败时返回 `RAG-RF-004`。

文档新版本使用 `version_id` 生成另一组唯一内部文件名，在同一 Dataset 中形成独立
RAGFlow Document。候选解析完成后，Gateway 通过官方 Chunk PATCH 接口暂时停用全部
候选块；发布时启用候选、停用旧版。若应用 PostgreSQL 事务失败，则反向启用旧版并
停用候选。Adapter 不读取或修改 RAGFlow PostgreSQL/Elasticsearch，也不伪造模型
配置。删除逻辑文档时按本地版本历史逐一调用官方 Document Delete。

### 5.2 metadata

上传完成后可调用 Document Update 写入白名单 `meta_fields`。元数据 Key 规则：小写字母、数字和下划线，1-64 字符；Value 仅允许字符串、数值、布尔或字符串数组，单文档序列化后最多 16 KiB。

### 5.3 状态映射

| RAGFlow `run` | 项目状态 |
|---|---|
| `UNSTART` | `UPLOADED` 或 `QUEUED` |
| `RUNNING` | `PARSING`；首期不根据自然语言进度猜测 INDEXING 阶段 |
| `DONE` | `READY` |
| `FAIL` | `FAILED` |
| `CANCEL` | `FAILED`，错误原因 `engine_cancelled` |

未知状态不得映射为成功，记录 `RAG-RF-004` 并保持任务待人工核对。

`progress_msg` 只用于运维展示，不能解析自然语言驱动状态机。`INDEXING` 状态仅为未来结构化 Pipeline 事件预留，v0.26.4 轮询路径可以从 `PARSING` 直接进入 `READY`。

## 6. 解析器映射

RAGFlow v0.26.4 将 DeepDoc、MinerU 等作为并列 PDF Parser。首期 Profile：

| 项目 Parser | RAGFlow 配置 |
|---|---|
| `deepdoc` | `layout_recognize: DeepDOC` |
| `mineru` | `layout_recognize: MinerU`；RAGFlow 通过已配置的 MinerU OCR Provider 远程调用 MinerU API |
| `plaintext` | `layout_recognize: Plain Text` 或对应 Naive 选项 |
| `picture` | RAGFlow Picture Parser；视觉模型由官方 Provider 配置 |
| `media` | Gateway 先生成 Markdown，RAGFlow 使用 Naive/Plain Text 解析派生文本 |

MinerU 环境：`MINERU_APISERVER`、`MINERU_BACKEND`，可选 `MINERU_SERVER_URL`、`MINERU_OUTPUT_DIR`、`MINERU_DELETE_OUTPUT`。MinerU 不替换 DeepDoc；知识库或 Pipeline 可选择不同解析器。

切换后必须 Reparse 已有文档。解析器返回结果最终仍由 RAGFlow Chunk/Embedding/Index 链路处理。

## 7. 解析调用

### 7.1 Built-in Pipeline

```http
POST /api/v1/datasets/{dataset_id}/chunks
Content-Type: application/json

{"document_ids": ["<document_id>"]}
```

### 7.2 Ingestion Pipeline

```http
POST /api/v1/documents/ingest
Content-Type: application/json

{"doc_ids": ["<document_id>"], "run": "1", "delete": false}
```

Reparse 使用 `delete=true`。取消 `run="2"`。调用方必须依据 Dataset 创建模式选择接口，不允许先调用一个失败后盲目尝试另一个。

## 8. Retrieval 映射

项目请求转换为：

```json
{
  "question": "规范化后的问题",
  "dataset_ids": ["<mapped_dataset_id>"],
  "document_ids": [],
  "page": 1,
  "page_size": 20,
  "similarity_threshold": 0.2,
  "vector_similarity_weight": 0.5,
  "top_k": 50,
  "rerank_id": "<configured-reranker-id>",
  "keyword": false,
  "highlight": false,
  "metadata_condition": {
    "logic": "and",
    "conditions": []
  },
  "use_kg": false,
  "toc_enhance": false
}
```

普通 Chunk 检索固定 `use_kg=false`、`toc_enhance=false`。一期图谱增强通过独立的
官方图结构读取完成，不在这条请求上开启 KG 聚合。不使用 `cross_languages`，除非评测确认。

### 8.1 Metadata 操作符

项目操作符映射：

| 项目 | RAGFlow |
|---|---|
| `eq` | `=` |
| `ne` | `≠` |
| `contains` | `contains` |
| `not_contains` | `not contains` |
| `starts_with` | `start with` |
| `empty` | `empty` |
| `not_empty` | `not empty` |
| `gt`/`gte`/`lt`/`lte` | `>`/`≥`/`<`/`≤` |

只有知识库元数据白名单内的字段才能下发。

### 8.2 响应转换

RAGFlow Chunk 字段映射：

| RAGFlow | 内部 Candidate |
|---|---|
| `id` | `engine_chunk_id`，同时派生稳定 chunk_id |
| `kb_id` | 查表得到 `knowledge_base_id` |
| `document_id` | 查表得到 `document_id` |
| `document_keyword` | `document_name` 备选 |
| `content` | `content` |
| `positions` | 规范化为 page/bbox 列表 |
| `similarity` | `score` |
| `vector_similarity` | `vector_score` |
| `term_similarity` | `term_score` |
| `highlight` | 默认不进入 Context |

如果文档映射不存在，候选丢弃并增加 `rag_candidate_unmapped_total`；不得把 RAGFlow ID 返回上游。

候选转换还必须执行 Scope 重验：本地 Dataset、Document 映射必须同时匹配
当前 `security_scope_id`。不匹配的候选不得读取正文、不得进入 Context，并记录
脱敏安全事件。一次请求中的知识库 ID 在调用 RAGFlow 前必须全部通过当前 Scope
校验；禁止“合法部分继续检索”的部分成功模式。

### 8.3 原生图谱的结构导航与 KG 聚合限制

本次 v0.26.4 隔离真实联调中，原生构图完成，官方 Graph Read 返回 **9 个节点、
15 条边**。随后一次 `use_kg=true` 冷诊断耗时约 **71 秒**，HTTP 200 响应只含
**2 个普通 Chunk**，没有 KG 聚合项。这是该次小型合成场景的观察结果，不代表
一期整体或业务质量已验收，也不能用普通 Chunk 数量冒充图谱命中。

对实际固定镜像只读核对发现两个相关缺陷：

1. `rag/graphrag/search.py` 的同步候选方法调用
   `rag/nlp/search.py` 中已为异步函数的 `Dealer.get_vector`，缺少 `await`；
   `ESConnection.search` 不能将该协程识别为 `MatchDenseExpr`，因而缺失预期向量查询。
2. `rag/utils/es_conn.py` 的 `get_fields` 未回读命中顶层 `_score`，而 KG 实体/关系
   转换依赖该分数并按默认 `0.3` 阈值筛选；缺失分数按 `0` 处理，候选会被过滤。

公开路由只有在聚合正文非空时插入该项。KG Query Rewrite 的 JSON/调用异常还可能
被上游捕获并退回原问题，因此 HTTP 成功不能证明聚合成功。实体类型池由上游内部
读取，客户端不需要为图谱请求另传 `entity_types`。Query Rewrite 有精确提示词缓存，
不等于整条 KG 检索缓存；模型名、问题或类型池样本及顺序改变都可能未命中。

在不修改/Fork 上游、不改变模型或 Token 参数的约束下，一期在线采用明确版本化的
`native_graph_read_v1`：先检查知识库 Scope、READY/内容代数与全部活动物理来源，
再调用官方 Graph Read。仅处理完整的 **最多 256 个节点、1024 条边** 的图，按完整
实体词匹配问题种子，扩展一跳邻居，所得实体名用于同库普通原文补召回。空图、
无种子、结构无效或超限时降级；不将大图静默截断后当作完整导航结果。

在线路线 `ragflow:graph` 同时返回 `native_graph_structural_navigation`，并在
`graph_diagnostics.mode` 声明 `native_graph_read_v1`。该模式进入精确/语义缓存边界，
与旧 `native` 聚合导航答案隔离。它表示使用原生构图产物，不表示官方 `use_kg`
聚合实现已经修复。图谱描述、关系描述和社区摘要均不成为可引用正文，最终证据仍须
映射当前活动文档并回读真实 Chunk；范围或来源无法核验时不返回图谱证据。

`probe_kg_navigation` 只保留为显式的官方 KG 诊断能力，在线问答不调用它。原生构图
仍需要实际可用的 Graph Chat，部署与知识库的图谱能力默认关闭。完整操作边界见
[原生 GraphRAG 接入说明](../../docs/NATIVE_GRAPHRAG.md#5-检索来源与缓存边界)。

## 9. 健康与能力探测

启动 Ready 流程：

1. 调 `/api/v1/system/healthz`。
2. 调 Dataset List，验证 API Key。
3. 检查响应 Schema 和契约版本记录。
4. 缓存能力结果 30 秒，避免每个请求探测。

Health 通过不代表 Retrieval 可用；上线 Smoke 还必须对专用小知识库执行一次检索。

## 10. 超时与重试

| 操作 | 连接 | 读取/总时限 | 自动重试 |
|---|---:|---:|---|
| Health/List | 3s | 10s | 1 次 |
| Create/Update/Delete | 3s | 30s | 只在可确认幂等时 1 次 |
| Upload | 5s | 300s | 默认不自动重放文件 |
| Parse trigger | 3s | 30s | 只在状态核对后 |
| Retrieval | 3s | 8s | 连接错误或 429/503 最多 1 次 |

重试使用指数退避和抖动，并受调用方总 Deadline 约束。

## 11. 契约测试

`RagEnginePort` 的方法必须覆盖第 3 节普通引擎能力，至少包括：

```text
create/get/update/delete_dataset
upload/get/list/update/delete_document
start/stop_parse
list_chunks
retrieve
health
```

图谱能力由独立 `GraphPort` 承担，覆盖配置、原生构建、状态、取消、结构快照和
有界导航；KG 聚合诊断与在线结构导航必须分开验收。应验证结构上限、完整实体种子、
一跳扩展、过滤旁路、缓存模式隔离、来源重验与无匹配降级，不能只断言 HTTP 200。

不得因为某个方法当前只被补偿流程调用就留给 Adapter 自由发挥。必须测试：
成功、资源不存在、请求无效、RAGFlow `code != 0`、HTTP 非 2xx、未知字段、
必要字段缺失、慢响应、断连、429、5xx、空候选、positions 多种形态和未知
`run` 状态。

Fixture 放在 `contracts/`，覆盖要求以
`contracts/ragflow-fixture-manifest.yaml` 为准。每个 Adapter 方法至少有一份
成功响应和一份最相关失败响应；通用网络/协议失败可以复用 Transport Fixture，
但测试必须对每个方法参数化执行。真实环境 Smoke 每次 RAGFlow 升级执行，结果
不得包含 API Key 和原文敏感内容。

## 12. PostgreSQL 部署说明

本方案要求 RAGFlow 与自研应用分别使用 PostgreSQL 数据库。RAGFlow v0.26.4
官方 Compose 默认启用 MySQL，但其配置模板提供 PostgreSQL 配置项；因此
`compose.local.yml` 会显式设置 `DB_TYPE=postgres` 并挂载本项目
的 PostgreSQL 配置模板。

这属于必须在 PoC 中验证的部署适配点，而不是假设已经得到官方默认 Compose
完整覆盖。验收至少包括：空库初始化迁移、创建 Dataset、上传、解析、列文档、
列 Chunk、检索、停止任务、删除、全部容器重启后再次创建/检索，以及备份恢复。

PoC 报告必须把失败归为以下之一，不能用一个“启动失败”结论混在一起：

- RAGFlow PostgreSQL 初始化/SQL/迁移兼容问题；
- RAGFlow 公开 API 或业务流程问题；
- Apple Silicon 上 `linux/amd64` 模拟运行或镜像架构问题；
- 模型 Provider、Elasticsearch、MinIO 或 Redis 等外部依赖问题。

若固定版本在上述流程存在阻断，第一期应优先保持 RAG Gateway API 不变，临时
将 RAGFlow 内部库回退到官方默认 MySQL；不得为了强行 PostgreSQL 化而直接
修改 RAGFlow 业务表或让应用库访问其内部 Schema。
