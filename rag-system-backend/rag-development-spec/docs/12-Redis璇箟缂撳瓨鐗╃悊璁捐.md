# Redis 语义缓存物理设计

> 状态：第一期开发基线  
> 适用组件：RAG Gateway、统一 Redis Stack DB 0 和 `rag-app-bootstrap`

## 1. 目标与边界

当前使用一个 Redis Stack 物理实例：DB 0 保存精确/语义缓存，DB 1 只供 RAGFlow
使用，DB 2 承担 Celery、取消标记和控制状态。三个逻辑 DB 不得互相扫描，但它们共享
内存、AOF、密码和故障域；逻辑 DB 不是安全或资源隔离。

语义缓存只复用已经通过 Citation Verification、最终
`outcome=success` 的回答。它是可丢失、可重建的加速数据，不是答案、权限或
Job 的事实来源；任何缓存错误都必须绕过并继续正常 RAG 链路。

## 2. Key 与索引

第一期固定一个语义缓存 Embedding 模型和维度。更换模型或维度必须创建新索引，
不能把不同维度的向量写入同一索引。

```text
Hash Key: rag-cache:semantic:v1:<cache_id>
Index:    rag_semantic_cache_v1
Prefix:   rag-cache:semantic:v1:
Metadata: rag-cache:index-metadata:rag_semantic_cache_v1
```

Bootstrap 在 Metadata Hash 中固定记录 `index_schema_version`、
`embedding_model`、`embedding_dimension` 和 `vector_data_type`。索引存在但
元数据缺失或任一值变化时必须失败，要求提升版本；不能仅因维度恰好相同就混用
不同模型。

`cache_id` 为以下规范化字段的 SHA-256，不包含原始身份 Claim：

```text
security_scope_id
+ knowledge_base_revisions_hash
+ retrieval_profile
+ prompt_profile
+ model_profile
+ language
+ history_hash
+ normalized_question_hash
```

精确缓存使用独立前缀 `rag-cache:exact:`，不得与向量索引 Prefix 重叠。

## 3. Redis Hash 字段

| 字段 | Redis 类型/索引类型 | 含义 |
|---|---|---|
| `cache_id` | string / TAG | 缓存记录稳定 ID |
| `security_scope_id` | string / TAG | 已验证 Scope 的 SHA-256，强制过滤 |
| `knowledge_base_revisions_hash` | string / TAG | 排序后的 KB 与 revision 快照 Hash |
| `retrieval_profile` | string / TAG | 检索配置 `<name>@<version>` |
| `prompt_profile` | string / TAG | Prompt 配置版本 |
| `model_profile` | string / TAG | 生成模型配置版本 |
| `language` | string / TAG | 输出语言 |
| `history_hash` | string / TAG | 会话历史摘要 Hash |
| `normalized_question` | UTF-8 string / NOINDEX | 规范化问题；按隐私配置决定是否保存 |
| `answer_json` | UTF-8 JSON / NOINDEX | 已验证答案和 usage 摘要 |
| `citations_json` | UTF-8 JSON / NOINDEX | 稳定项目 ID 引用，不保存 RAGFlow Dataset ID |
| `created_at_ms` | integer / NUMERIC | 创建时间 |
| `expires_at_ms` | integer / NUMERIC | 逻辑过期时间 |
| `last_hit_at_ms` | integer / NOINDEX | 最近命中时间 |
| `embedding` | little-endian FLOAT32 bytes / VECTOR | 问题向量 |

`answer_json` 和 `citations_json` 读取后必须经过 Pydantic DTO 校验。单条回答不得
超过 `cache.max_answer_bytes`，不得使用 pickle 或其他可执行反序列化格式。

## 4. 索引定义

`rag-app-bootstrap` 根据 `EMBEDDING_DIMENSION` 生成并执行等价的 Redis 命令：

```text
FT.CREATE rag_semantic_cache_v1
  ON HASH
  PREFIX 1 rag-cache:semantic:v1:
  SCHEMA
    cache_id TAG
    security_scope_id TAG
    knowledge_base_revisions_hash TAG
    retrieval_profile TAG
    prompt_profile TAG
    model_profile TAG
    language TAG
    history_hash TAG
    created_at_ms NUMERIC
    expires_at_ms NUMERIC
    embedding VECTOR HNSW 10
      TYPE FLOAT32
      DIM <EMBEDDING_DIMENSION>
      DISTANCE_METRIC COSINE
      M 16
      EF_CONSTRUCTION 200
```

实现时使用 Redis 客户端的参数数组发送命令，不拼接来自请求的原始字符串。
TAG 查询值必须使用 RediSearch 规定的转义函数。系统生成的 Scope Filter 不能被
客户端 Metadata Filter 覆盖。

## 5. 查询

固定执行顺序：

1. 在应用 PostgreSQL 内按当前 `security_scope_id` 校验全部知识库并取得 revision。
2. 计算规范化问题向量。
3. 使用以下字段构建强制 Filter：
   `security_scope_id`、`knowledge_base_revisions_hash`、Retrieval/Prompt/Model
   Profile、语言和 `history_hash`。
4. 在强制 Filter 内执行 KNN，最多返回配置的 `top_k`。
5. 将 Redis 距离转换为统一相似度并比较阈值。
6. 按候选顺序重验知识库状态、文档 `READY` 状态和每条 Citation。
7. 首个全部通过的候选才算命中；否则继续正常 RAG。

查询形态等价于：

```text
(@security_scope_id:{...}
 @knowledge_base_revisions_hash:{...}
 @retrieval_profile:{...}
 @prompt_profile:{...}
 @model_profile:{...}
 @language:{...}
 @history_hash:{...})
=>[KNN $top_k @embedding $query_vector AS vector_distance]
```

查询中禁止先取全局最近邻后在应用内过滤 Scope，因为这既可能泄露候选，也会
降低合法 Scope 的召回。

## 6. 写入、并发和 TTL

- 只有正常问答完成引用校验后才写入缓存，写入失败不改变本次答案。
- 使用确定性 `cache_id`，以 `MULTI/EXEC` 或 Lua 完成 Hash 字段写入和
  `EXPIRE`，避免出现部分字段或无 TTL 记录。
- 相同 `cache_id` 并发写入执行幂等 Upsert；后写者只有在 Schema/Profile
  完全一致时才能覆盖。
- Key TTL 是物理过期依据，`expires_at_ms` 用于查询双重校验和排障。
- 命中时更新 `last_hit_at_ms` 不能延长 TTL，除非未来版本明确采用滑动过期。
- 文档变化通过知识库 revision 使旧记录立即不可命中；后台只负责批量回收，
  不以扫描删除作为正确性前提。

## 7. Bootstrap、升级与降级

`rag-app-bootstrap` 在 Gateway/Worker 启动前：

1. 检查 Redis Stack 是否支持 RediSearch 和 VECTOR。
2. 不存在索引时创建 `rag_semantic_cache_v1`。
3. 索引存在时校验 Prefix、字段、算法、模型和维度。
4. 完全一致则幂等成功；不一致则失败并阻止 Ready，禁止现场修改已有索引定义。

Schema、Embedding 模型或维度变化时创建新版本索引，例如
`rag_semantic_cache_v2`。应用先双读/新写，验证后切换，再按 TTL 清理旧索引。
DB 0 缓存操作故障时记录稳定内部错误并绕过缓存；不得因为缓存不可用而
关闭 JWT、Scope 校验或 Citation Verification。

## 8. 验收

- 同 Scope、相同版本边界的相似问题可以命中。
- 其他 Scope 即使有更高相似度候选也不能返回或读取其正文。
- revision、Profile、模型、语言或历史 Hash 任一变化均不命中。
- 引用文档删除或非 `READY` 时视为未命中。
- 向量维度不匹配时 Bootstrap/写入明确失败，不静默截断或补零。
- 重复 Bootstrap、并发 Upsert、TTL 到期、Redis 重启和 Redis 故障绕过通过。
