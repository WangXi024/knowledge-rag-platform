# LazyTT 用户端（后端仓库内参照快照）

> [!WARNING]
> 正式普通用户端已经拆分到项目同级独立仓库 `lazytt-frontend-console`。本目录是后端
> 联调与历史参照快照；如果正式产品仍从独立仓库发布，需要把本次策略同步过去。

本目录提供 3001 端口的共享知识用户界面；3000 端口仍是内部开发控制台。

## 当前策略

- 不提供本地账号注册、邮箱或密码登录。
- 进入页面时只填写 `Tenant ID` 和 `User ID`。它们会保存到 `HttpOnly + SameSite=Lax`
  Cookie，并由服务端 BFF 写入 `X-Tenant-ID`、`X-User-ID`。
- 当前是 Direct Identity 开发联调模式：只校验两个 ID 的格式，不验证填写者是否拥有
  该身份，不能直接作为生产认证方案。
- 同一 Tenant 下的所有 User 当前都能读取该租户全部 `TENANT` 知识库；所有 Tenant
  都能读取全部 `SYSTEM` 知识库；不能读取其他租户的 `TENANT` 知识库。
- “共享知识库”按 12 个一页展示，支持“全部 / 租户共享 / 系统共享”筛选和准确计数。
  每个知识库都显示“租户共享 · 当前 Tenant”或“系统共享”归属标签。
- 可以只读浏览知识库文档和已解析切块。用户端不提供创建知识库、上传、重试、删除、
  重解析或修改配置的接口，这些操作由内部管理端统一完成。
- 问答暂时自动检索当前身份可访问的全部租户共享和系统共享知识库；以后可以再增加
  默认库或业务侧选择，以缩小检索范围。
- 后期增加知识库—成员多对多 ACL 时，权限判断由后端共享访问策略收窄；BFF 不维护
  固定白名单，因此无需重写页面路由。

问答继续支持流式输出、安全 GFM Markdown、证据引用、检索分数、缓存来源和每页最多
两个并行会话。问答记录不作为下一次提问上下文。

## 本地启动

先在仓库根目录启动完整后端：

```bash
./deploy/start-full.sh
```

再启动用户端：

```bash
cd user-frontend
npm install
npm run prepare:local
npm run dev -- --port 3001
```

打开 <http://127.0.0.1:3001/login>，填写与本地测试数据一致的 Tenant ID 和任意 User ID。
同一 Tenant 下更换 User ID 后，知识库列表应相同；更换 Tenant ID 后，只保留系统库和
新 Tenant 自己的租户库。

`npm run prepare:local` 只生成 Git 忽略、权限为 `0600` 的 `.env.local`，其中包含
Gateway 地址和身份 Cookie 有效期，不包含密码、API Key、JWT 或私钥。

## 数据库影响

本次没有修改 RAG PostgreSQL 的表结构。用户端本地 D1 会按需新增：

- `direct_conversations`
- `direct_conversation_messages`

新表用 Tenant ID + User ID 的主体摘要隔离对话。旧 `users`、`sessions`、
`conversations`、`conversation_messages` 和 `user_workspaces` 表不会删除或继续读取；
如需清理应另做一次明确的数据迁移，不在应用启动时破坏性执行。

## 安全边界

- Direct Identity Cookie 不是可信凭证；知道任意 Tenant/User 值的人目前都能选择该身份。
- 浏览器业务请求不能逐次覆盖 Tenant/User Header；BFF 总是从 HttpOnly Cookie 读取。
- Gateway 负责同租户、系统共享、跨租户 404 和未来 ACL 的最终判断。
- 用户端只调用 `GET /v1/knowledge-bases`、文档和 Chunk 的普通用户只读接口；不调用
  `/v1/management/*`。
- 正式上线前必须由可信上层 Identity 注入已验证的身份，并增加防伪、有效期、撤销、
  审计和成员权限；届时可保留现有 BFF 和访问策略扩展点。

## 验证

```bash
npx tsc --noEmit
npm run lint
npm test
```
