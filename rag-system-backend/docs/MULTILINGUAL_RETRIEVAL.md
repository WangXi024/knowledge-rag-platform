# 双语 QueryPlan 与语言感知检索

## 目标

Gateway 在不修改 RAGFlow 源码的情况下，对中文、英文和中英混合知识库执行对称的
跨语言检索。最终回答语言仍由 `response_language` 独立控制，与检索使用中文还是
英文 Query 无关。

## QueryPlan

当问题语言与知识库检索语言不一致，或知识库语言未知/混合时，Gateway 使用
`rewrite-openai-compatible@4` 和 `retrieval-query-bilingual@1` 生成结构化双语问题：

```json
{
  "query_zh": "篮球运动员累计五次犯规后的处理规则",
  "query_en": "What happens after a basketball player commits five fouls?"
}
```

Rewrite 必须保留数字、错误码、型号和专有名词。模型输出无效时保留原问题，并返回 `query_rewrite_fallback`，不会伪造翻译。

## 知识库语言判断

Gateway 读取文档 Metadata 的 `language` 或 `lang`：

- 中文：`zh`、`zh-CN`、`zh-Hans`、`chinese` 等；
- 英文：`en`、`en-US`、`en-GB`、`english` 等；
- 缺失或无法识别：按未知语言处理。

Gateway 使用 PostgreSQL 在当前 `security_scope_id` 和目标知识库范围内直接聚合 Metadata，不会把全部文档加载到应用内存。只要存在未知语言文档，就安全地启用中英文双路，并返回 `knowledge_language_unknown_dual_route`。

## 检索路线

| 场景 | 路线 |
| --- | --- |
| 英文问题 + 英文知识库 | 英文原问题向量、英文 BM25 |
| 中文问题 + 英文知识库 | 中文原问题向量、英文翻译向量、英文 BM25 |
| 中文问题 + 中文知识库 | 中文原问题向量、中文 BM25 |
| 英文问题 + 中文知识库 | 英文原问题向量、中文翻译向量、中文 BM25 |
| 混合或未知知识库 | 原问题向量、中英文 Query 向量、中英文 BM25；相同路线自动去重 |

各路线并发请求 RAGFlow。新请求默认使用不可变版本 `hybrid-structure-aware@6`；原有
`hybrid-default@1/@2`、`hybrid-adaptive@3`、`hybrid-multigranular@4` 与
`hybrid-structure-aware@5` 保持原配置，用于兼容和显式回滚。`@6` 会先把问题分类为
事实、列表、比较、概览或多跳，再选择
30～80 的 Top-K、8～16 条 Rerank 候选和 5～8 条最终上下文；除 Document Summary、
Section 外，它还允许带文档/章节前缀的正文 Parent 和带表头的 Table Parent 侧车节点
导航到真实 Chunk，并在侧车向量节点取 Top-N 前过滤低于 0.40 的余弦相似度。默认加权 RRF 配置位于
`retrieval_profiles.example.yaml`：

```yaml
rrf:
  k: 60
  original_vector_weight: 1.0
  translated_vector_weight: 0.9
  primary_term_weight: 1.0
  secondary_term_weight: 0.8
  unknown_term_weight: 0.9
```

融合公式为 `route_weight / (k + rank)`。加权 RRF 后还可以加入 Document Summary、
Section、上下文 Parent、Table Parent、实体和显式关系的原始 Chunk 导航路线，再执行
正文 Hash/Embedding 语义候选去重，并把 8～16 条正文交给配置的 Cross-Encoder
重排；外部服务故障时仍稳定保留向量、BM25 和 RRF 排名。侧车只负责导航，公开正文
始终来自 RAGFlow 原始 Chunk；侧车路线没有命中时不改变基础跨语种检索。

## 可观测字段

`POST /v1/retrieval`、`POST /v1/answers` 和 SSE 的 `retrieval.completed` 事件保留兼容字段 `original_query`、`effective_query`，并增加：

```json
{
  "query_plan": {
    "detected_language": "zh-CN",
    "variants": {
      "zh-CN": "原始或标准化中文问题",
      "en-US": "English retrieval query"
    },
    "knowledge_languages": ["zh-CN", "en-US"],
    "retrieval_routes": [
      "vector:original:zh-CN",
      "term:zh-CN",
      "vector:translated:en-US",
      "term:en-US"
    ],
    "query_type": "multi_hop",
    "retrieval_top_k": 80,
    "rerank_top_n": 16,
    "context_top_n": 8
  }
}
```

`query_type` 说明问题被识别为事实、列表、比较、概览还是多跳；后面三个字段依次是
每条 RAGFlow 路线的召回上限、送入 Cross-Encoder 的候选上限和交给 LLM 的证据
上限。最终 `total` 已经经过配额与去重，不能代替这些预算字段。

每个公开候选的 `metadata.gateway_retrieval_routes` 还会按首次命中顺序保存该 Chunk
实际经过的全部路线，例如：

```json
{
  "metadata": {
    "gateway_retrieval_routes": [
      "vector:original:zh-CN",
      "term:zh-CN",
      "summary:document",
      "parent:children"
    ],
    "sidecar_route": "summary:document"
  }
}
```

`sidecar_route` 是旧客户端使用的单值兼容字段，表示最早把候选带入结果的侧车路线；
它不会再被后执行的 Parent 扩展覆盖。完整诊断应以数组为准。该数组只解释 RRF 和
侧车导航来源，不参与权限判断、Rerank 计算或引用校验。

开发测试前端的 Query Rewrite 卡片和原始响应可以查看这些字段。QueryPlan 也会写入
精确缓存和语义缓存，升级后写入的新缓存命中时仍可追踪原检索路线与动态预算；升级
前的旧缓存没有预算字段时返回 `null`，不会伪造历史值。

普通用户端不展示完整调试 QueryPlan，但会显示最终候选、引用和可用分数。普通用户端
每个问题固定传入空历史：保存的会话仅供回看，不影响下一次 QueryPlan，也让完全重复
或语义相近问题可以共享稳定的缓存历史边界。其他 API 客户端仍可按 OpenAPI 传入历史。

## 验证

```bash
UV_CACHE_DIR=/private/tmp/lazytt-uv-cache uv run pytest -q tests/unit
UV_CACHE_DIR=/private/tmp/lazytt-uv-cache uv run mypy src/rag_service
UV_CACHE_DIR=/private/tmp/lazytt-uv-cache uv run ruff check src tests
```

真实联调时至少准备中文、英文和混合三类非业务 Fixture，分别使用中文和英文问题验证六种组合。对比 Recall@5、无依据拒答率、引用有效率和 P95 延迟。

## 当前限制

- 用户端上传不会猜测 PDF/Office 正文语言；未提供 `metadata.language` 时启用双语路线，正确性优先但调用数较多。
- 当前 Cross-Encoder 由 `RERANK_BASE_URL/API_KEY/MODEL` 配置；双语路由负责提高
  召回覆盖，Rerank 负责提高前几名精度。仓库示例环境默认启用 Rerank；启动时必须
  先通过真实探测，服务故障时再明确降级到 RRF，生产效果仍需带标注问题集评估。
- 目前只支持中文和英文；其他语言按英文问题处理并对未知知识库执行双语回退。
