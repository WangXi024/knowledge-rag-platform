# ADR-0004：按能力接入通用模型端点

- 状态：已实现，离线自动测试已覆盖；真实云端全链路需在 Docker 可用时复验
- 日期：2026-08-10
- 替代：ADR-0002、ADR-0003 的厂商绑定部分

## 背景

过去每更换供应商都需要增加环境变量、Profile、Bootstrap 分支和 Adapter 名称。多数
Chat、Embedding 和多模态服务已经提供 OpenAI-compatible 协议，继续按厂商扩展会让
部署复杂、测试重复，也容易让文档与真实活动模型漂移。

## 决策

1. Chat/Rewrite、Embedding、文档 Vision 和媒体 Vision 各自只接收 Base URL、API Key、
   模型名；文档图片与视频抽帧不隐式复用模型；
2. Chat/Rewrite 使用 `POST /chat/completions`，Embedding 使用 `POST /embeddings`，
   Vision 使用 Chat Completions `image_url`；
3. Bootstrap 用合成数据探测普通回答、JSON Object、文档 OCR、媒体图片理解、单条/
   批量 Embedding，自动写回实际维度和有限 Chat 协议覆盖；业务代码不按供应商或
   模型名分支；
4. RAGFlow Embedding/Vision 统一通过官方 `OpenAI-API-Compatible` Provider API 注册，
   不修改 RAGFlow 源码或数据库；
5. 活动 Profile 使用 `embedding-openai-compatible@3`、
   `answer-openai-compatible@4`、`rewrite-openai-compatible@4` 和
   `rerank-compatible@2`；
6. Rerank 与 ASR 不是 OpenAI 官方统一标准。前者使用项目约定 `POST /rerank`，后者
   使用 Chat Completions `input_audio`；开启时必须真实探测，不能仅凭兼容标签通过；
7. 旧厂商环境变量和种子 Profile 不再兼容。升级本机需要迁移到通用字段；已有数据库
   行和 RAGFlow 实例只为数据完整性保留，不作为新默认值；
8. API Key 绝不进入 Profile、日志或提交。同一个 Key 可以由用户显式填到多个职责，
   代码不隐式复制。

## 失败与索引边界

- 实际 Embedding 维度与已有数值配置不一致时启动失败；
- 模型或维度变化必须新建索引/缓存世代并受控重建；
- 启动 Rerank 失败会阻止“已启用”状态，在线临时故障则保留 RRF 排名并返回 Warning；
- `GET /models` 不是必需能力：404/405 不否定启动时真实调用，401/403/5xx 仍判为
  不可用；若端点提供标准清单，则持续校验配置模型存在；
- 仅轮换 API Key 且 Base URL/模型不变时，Bootstrap 通过 RAGFlow v0.26.4 官方
  Provider Instance `PUT` 接口原地刷新凭据并复验；Base URL 或模型变化时生成新
  实例并切换默认引用。两种情况都不直接修改数据库，也不删除旧 Dataset。

## 影响

- 好处：新用户更换兼容服务只改环境三元组，无需写 Adapter；启动错误更早、更明确；
- 代价：并非所有标称兼容服务都实现图片、JSON、SSE、Rerank 或 ASR，真实探测仍可能
  失败；云端调用仍依赖网络、额度和数据合规；
- 回滚：回滚代码和通用环境文件即可；禁止删除 Docker Volume 或强制清理历史 Profile。
