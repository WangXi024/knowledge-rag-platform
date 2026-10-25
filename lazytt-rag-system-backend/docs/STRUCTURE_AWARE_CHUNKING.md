# 结构感知分块与上下文索引

## 1. 这次优化解决什么

过去所有文件基本共用一个固定 Chunk Profile，Gateway 侧车 Parent 也主要按
固定四块组合。这种基线简单稳定，但它无法同时适合长文章、电子表格、
PPT/图片 OCR 和音视频转写。

当前实现把优化分成两层：

1. **RAGFlow 原始 Chunk**：按文件族自动选择长度、分隔符和重叠比例；
2. **Gateway 侧车索引**：用标题、长度和文件族动态组成 Parent，再建立
   Section 和 Document Summary。

全程不修改或 Fork RAGFlow 源码。RAGFlow 仍负责解析、原始 Chunk、Embedding 与
Elasticsearch 索引；Gateway 通过 RAGFlow 官方 API 应用文档级配置。

## 2. 文件类型自动选择 Profile

| 文件族 | 扩展名示例 | Chunk Profile | 目标 Token | 重叠 |
| --- | --- | --- | ---: | ---: |
| 正文 | PDF、DOCX、TXT、MD、HTML | `prose-384@1` | 384 | 12% |
| 表格 | XLS、XLSX、CSV | `table-192@1` | 192 | 5% |
| 视觉 | PPT、PPTX、PNG、JPG、WebP | `visual-192@1` | 192 | 8% |
| 媒体转写 | 常见音频和视频 | `media-320@1` | 320 | 12% |

`general-256@2` 继续作为知识库创建时的安全默认、未知格式回退和历史对照。
调用方显式传入 Chunk Profile 时，显式值优先，便于开发对照和故障回滚。

### 2.1 知识库默认值与文档实际值

开发控制台创建知识库时仍要求选择 Parser、Chunk、Embedding 和 Retrieval Profile。
这些字段的含义并不完全相同：

| 字段 | 知识库中的含义 | 文档/请求是否可以改变 |
| --- | --- | --- |
| Parser Profile | 新文档自动选择时优先尝试的默认解析器 | 上传时可按文档覆盖；默认值不兼容时按配置规则切换 |
| Chunk Profile | 未知格式的安全回退和历史默认值 | 上传未显式覆盖时，Gateway 按文件族自动选择 |
| Embedding Profile | 当前知识库向量空间的固定模型与维度 | 不允许在同一知识库中按文档混用 |
| Retrieval Profile | 当前知识库的默认检索编排 | 检索/问答请求可以显式选择其他已启用版本 |

Chunk Profile 的优先级是“上传请求显式值 → 文件族推荐值 → 知识库回退值”。Parser
Profile 的优先级是“上传请求显式值 → 兼容的知识库默认值 → 配置文件中的首条匹配规则
→ 422”。因此，同一个知识库可以混合上传 PDF、Office、图片和音视频；显式覆盖仍用于
对照实验，且不会绕过文件兼容性校验。

上传接口会把选中的 Profile 引用保存到 DocumentRecord。Worker 开始解析前，再从
App PostgreSQL 读取已启用的不可变 Profile，先调用：

```text
PATCH /api/v1/datasets/{dataset_id}/documents/{document_id}
```

写入 `chunk_method` 和 `parser_config`，成功后才调用解析接口。配置写入失败时任务
明确失败，不会静默沿用错误的 Dataset 默认值。

## 3. 动态 Parent 怎样分组

`parent_size=4` 仍是单个 Parent 可包含源 Chunk 数的硬上限，但不再要求必须凑满四块。
出现下面任一条件时就会结束当前 Parent：

- 下一个 Chunk 以 Markdown 标题、“第 X 章/节”或数字编号标题开始；
- 累计字符数将超出当前文件族预算；
- 源 Chunk 数到达文件族上限。

| 文件族 | Parent 最大源块数 | 累计字符预算 |
| --- | ---: | ---: |
| 正文 | 4 | 4,800 |
| 表格 | 3 | 3,600 |
| PPT/图片 | 2 | 2,600 |
| 媒体转写 | 4 | 5,200 |

标题识别不确定时，算法会自然退化为受长度和数量上限保护的连续窗口，不会因为
一个 OCR 标题识别失败就让文档无法检索。

## 4. 表格专用侧车切块

对 XLS/XLSX/CSV，Gateway 会在前部 RAGFlow Chunk 中寻找 TSV、CSV 或 Markdown 表格形式的
可复核表头。如果成功识别：

- 表格 Parent 每组最多三个原始 Chunk；
- 不含表头的中段 Parent 在侧车内容中重复表头；
- RAGFlow 原始 Chunk 正文、页码和坐标不做任何修改；
- 无法可靠识别表头时，仍使用小行组，不猜测列名。

这可以改善“只命中表格中间一行，但不知道数值属于哪一列”的问题。

## 5. 上下文前缀不会污染引用

Parent、Section 和 Document Summary 在生成侧车 Embedding 前，会加上可确定的短标签：

```text
文档：battery
内容类型：spreadsheet
索引层级：parent
章节：第二章 试验数据
语言：zh-CN
表头：型号    容量    温度
正文：
...
```

前缀只存在 App PostgreSQL 的侧车索引中，目的是让向量更容易识别文档、章节和
列含义。在线命中侧车节点后，Gateway 使用 `source_chunk_ids` 回读 RAGFlow 原始
Chunk，所以前缀不会被 LLM 当成原文，也不会出现在 C1/C2 引用卡片中。

## 6. 新检索 Profile

新请求默认使用 `hybrid-structure-aware@6`。它保留 `@5` 的全部结构感知路线，并在
Summary、Section 和 Context Parent 取 Top-N 前增加 0.40 余弦相似度门槛。`@5` 保留
用于回滚；它与 `@4` 一样具备双语 QueryPlan、动态
Top-K、向量 + BM25 + RRF、Section/Summary、Parent-Child、轻量实体关系导航、
语义去重和 Rerank，再增加：

- `context:parent`：普通上下文 Parent 路线；
- `context:table`：带表头的表格 Parent 路线。

这两条路线在候选 Metadata 的 `gateway_retrieval_routes` 中可见。旧
`hybrid-multigranular@4` 继续保留，遇到新版策略回归可显式切回对照。

## 7. 升级后怎样验收

### 7.1 新文件的 Profile

使用 <http://127.0.0.1:3000> 的开发控制台验收；普通用户端会隐藏 Profile，不适合
核对内部策略。

1. 创建测试知识库时保留 `general-256@2` 作为 Chunk 安全默认；Embedding 使用当前
   `embedding-openai-compatible@3`，Retrieval 使用 `hybrid-structure-aware@6`；
2. 进入“文档与解析”，分别上传一个小 PDF、DOCX、XLSX、PPTX 和图片；
3. 上传时将“Chunk 覆盖”全部留空，使 Gateway 执行文件族自动选择；
4. 如果复用同一个测试知识库，在“Parser 覆盖”中分别填写：PDF 使用
   `deepdoc-default@1`，DOCX/XLSX/PPTX 使用 `office-default@1`，PNG/JPG/WebP
   使用 `picture-default@1`；也可以为三个 Parser 分别创建测试知识库；
5. 等待上传和解析 Job 完成、文档状态变成 `READY`，再查看文档列表或详情响应；
6. 确认文档实际 Profile：PDF/DOCX 为 `prose-384@1`，XLSX 为
   `table-192@1`，PPTX/图片为 `visual-192@1`；
7. 同时确认 Parser 分别是 DeepDoc、Office 和 Picture 对应版本。知识库详情仍显示
   `general-256@2` 并不代表失败，因为它是知识库回退值，文档详情才是本项验收依据。

### 7.1.1 2026-08-04 已完成的真实检查

当前本地环境已通过同一知识库中的两类文档检查：

| 文件 | Parser | Chunk | 状态 | 原始 Chunk |
| --- | --- | --- | --- | ---: |
| `01_结构化实验报告_主文档.docx` | `office-default@1` | `prose-384@1` | `READY` | 7 |
| `04_锂离子电池SOH估计.pdf` | `deepdoc-default@1` | `prose-384@1` | `READY` | 61 |

两份文档均显示 `lazytt_hierarchy_version=3`、
`lazytt_parent_strategy=heading-length-v3`、`lazytt_hierarchical_index=ready` 和
`lazytt_structured_metadata_status=completed`。这份证据确认正文文件族和文档级
Parser 覆盖真实生效；XLSX、PPTX 和图片仍需按上面的步骤分别记录结果，不能因为
DOCX/PDF 通过就宣称所有文件族均已验收。

### 7.2 层级索引版本

文档变成 READY 后，确认 Metadata 包含：

```text
lazytt_hierarchy_version=3
lazytt_context_prefix_version=1
lazytt_parent_strategy=heading-length-v3
```

XLSX/CSV 如果识别到表头，`lazytt_table_parent_count` 应大于 0。

### 7.3 检索回读原文

1. 在“混合检索”显式选择 `hybrid-structure-aware@6`；
2. 对表格中间的数据行提问；
3. 查看 `query_plan.retrieval_routes` 或候选 Metadata，理想情况能见到
   `context:table`；
4. 确认候选 `content` 是文件真实原文，不包含“文档：”、“索引层级：”等
   侧车标签。

`context:*` 是增强路线，不保证每个问题都必须命中。基础向量、BM25 和 RRF 仍正常
执行；没有上下文路线不等于检索失败。

## 8. 旧文档和回滚

- 升级前已经 READY 的文档不会自动重切，防止部署时批量删除旧 Chunk；
- 需要新策略时，对旧文档显式执行一次“重解析”；
- 需要回滚检索时，请求显式传 `hybrid-multigranular@4`；
- 需要做切块对照时，上传/重解析接口显式传历史 Chunk Profile；
- 不要直接修改已入库 Profile，也不要直接改 RAGFlow 数据库。

## 9. 当前限制

- 动态 Parent 使用可解释规则，不是 LLM 语义分段；
- 通用 Gateway 上传接口会自动选择 Parser/Chunk Profile，并把最终版本保存到文档；
  重试与重解析沿用已保存值，不会随配置变化发生漂移；
- 表头识别重点支持 TSV、CSV 和 Markdown 表格，复杂多层合并表头可能无法识别；
- 超大文档侧车最多处理前 1,000 个 Chunk，RAGFlow 基础全文索引仍覆盖全部；
- 当请求显式携带 `metadata_filter` 时，侧车路线为避免越过过滤边界会整体绕过；
- 这项优化不是 RAPTOR、完整 GraphRAG 或对 RAGFlow 内部 Chunker 的 Fork。
