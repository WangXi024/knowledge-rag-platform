# LazyTT RAG 开发测试控制台

这是 3000 端口的全接口验收前端，不是普通用户产品页面。普通用户端位于独立
`lazytt-frontend-console` 仓库，默认端口 3001。

## 启动

先在后端仓库根目录启动完整栈：

```bash
./deploy/start-full.sh
```

再运行控制台：

```bash
cd frontend
npm install
npm run prepare:local
npm run dev
```

打开 <http://127.0.0.1:3000>。

## Direct Tenant/User 模式

当前版本没有注册、登录、密码、Session 或 JWT。控制台左侧可直接编辑 Tenant ID 与
User ID；同源 BFF 把它们作为 `X-Tenant-ID` 和 `X-User-ID` 转发给 Gateway。

切换任一 ID 时会清空浏览器保存的最近资源 UUID，但不会删除后台数据。Tenant ID
决定可访问的租户共享知识库，所有 Tenant 都可访问系统共享知识库；Tenant/User
组合只派生调用主体范围，用于审计、缓存和幂等隔离。两个 Header 不是认证凭据，
禁止用于生产。

## 覆盖范围

- 系统版本、能力和健康状态；
- 知识库 CRUD；
- 文件上传、自动/手动解析、Job 查询、重试、取消和删除；
- 文档 Chunk 与原文位置；
- 文档安全版本更新、版本查询和回滚；
- 混合检索、Metadata/Document ID 过滤、QueryPlan 和分项分数；
- JSON/SSE 问答、缓存、引用、取消和反馈；
- 原始请求控制台，可覆盖全部公开 Gateway 接口。

## 环境变量

`npm run prepare:local` 会生成 Git 忽略的 `.env.local`。也可以从 `.env.example` 复制：

```dotenv
GATEWAY_BASE_URL=http://127.0.0.1:8080
NEXT_PUBLIC_DEFAULT_USER_ID=local-console-user
```

控制台不读取后端 JWT 私钥、RAGFlow Key、模型 Key 或数据库密码。

## 验证

```bash
npm test
npm run build
```

完整人工验收见 [MANUAL_ACCEPTANCE_TEST.md](MANUAL_ACCEPTANCE_TEST.md)。
