# LazyTT 共享知识管理端

`admin-frontend` 是 RAG Gateway 的独立管理界面，用于维护租户共享知识库和系统共享
知识库。它不修改 PostgreSQL 表结构，也不直接连接 PostgreSQL、Redis、MinIO 或
RAGFlow；全部业务操作都通过现有 `/v1/management/*` API 完成。

## 当前角色边界

- 租户管理员：填写 `Tenant ID`、`User ID`，只列出和创建当前租户的 `TENANT` 知识库。
- 系统管理员：填写 `User ID`，只列出和创建所有租户可访问的 `SYSTEM` 知识库。
- 页面不会提供 `TENANT / SYSTEM` 自由切换字段，知识库归属由进入时的管理员类型固定。
- 支持知识库创建、编辑、异步删除、文档列表、多文件上传、失败重试、重解析和删除。
- 多文件上传默认不提交 Parser/Chunk，由后端先保留兼容的知识库默认值，再按配置中的
  文件类型规则自动路由；也可切换到手动 Profile 模式。

> [!WARNING]
> 当前角色选择只是开发联调身份模拟，不是管理员认证。前端仍只发送 `X-Tenant-ID` 和
> `X-User-ID`，知道 ID 的人可以模拟该身份。必须只在本机或可信内网使用，不能直接
> 暴露到公网。后续接入统一 Identity/JWT 后，应由可信会话提供角色、Tenant ID 和
> User ID，并由后端强制执行管理员权限与未来的成员 ACL。

## 本地运行

先在仓库根目录启动 RAG 后端，确保 Gateway 的 `health/ready` 可用：

```bash
./deploy/start-full.sh
```

再启动管理端：

```bash
cd admin-frontend
npm install
npm run prepare:local
npm run dev -- --port 3002
```

浏览器打开 <http://127.0.0.1:3002>。默认 `.env.local` 连接
`http://127.0.0.1:8080`；如果 Gateway 在其他主机，请只在本机修改：

```dotenv
GATEWAY_BASE_URL=http://<GATEWAY_HOST>:8080
```

`.env.local` 已被 Git 忽略。不要在其中放置或提交密码、Token、API Key 或私钥。

## 局域网访问与上传（仅可信测试环境）

保持本机 RAG 后端运行，在管理端目录执行：

```bash
cd /Users/wx/Lazyai_Project/Lazyai_RAG_Project/lazytt-rag-system/admin-frontend
npm run dev:lan
```

这条命令显式使用 `0.0.0.0:3002` 监听，并通过 Vite 的主机名白名单额外允许
`wx.local`，不会关闭主机名检查。原来的 `npm run dev` 仍保持本机监听。
以启动输出的实际端口为准；如果 3002 已被占用，请先确认占用进程，不要误停其他服务。

本机的 macOS `LocalHostName` 为 `wx` 时，网络互通且支持 Bonjour/mDNS 的其他电脑
可以打开 <http://wx.local:3002>。可以用 `scutil --get LocalHostName` 核对名称。
若名称无法解析，可改用 `http://<MAC_LAN_IP>:3002`；访客 Wi-Fi、客户端隔离、跨网段
或防火墙仍可能阻止连接。Mac 需要保持唤醒，前端和后端都不能停止。

浏览器选择的是访问者自己电脑上的文件，文件通过同源 `/api/gateway/*` 代理发送到
这台 Mac 的 RAG 后端，再由后端按原有配置保存、解析和索引。`GATEWAY_BASE_URL`
仍是服务端地址，默认保持 `http://127.0.0.1:8080`；不要改成访问者电脑的地址。
无需开放 Gateway、数据库、MinIO 或 RAGFlow 管理端口。

局域网 HTTP 通常没有 `crypto.randomUUID()`，上传和其他写操作会改用
`crypto.getRandomValues()` 生成同格式的 UUID v4。每次操作仍使用新的幂等键，
不改变文件上限、身份 Header、Profile 路由或后端幂等协议。

> [!WARNING]
> `0.0.0.0` 会监听所有 IPv4 网卡，不是访问控制。仅对可信内网开放前端端口，
> 不关闭系统防火墙、不配置公网端口转发。当前网页没有真正的管理员认证，访问者
> 不仅可以上传，还可能创建、修改或删除资源；HTTP 也不加密文件内容。只使用非敏感
> 测试文件；面向普通成员或处理敏感文件前，必须先接入认证、授权和 HTTPS。

另一台电脑的验收步骤：

1. 打开上述地址，用约定的测试 Tenant/User 进入，确认知识库列表能加载。
2. 选择测试知识库，从该电脑上传一个小型、非敏感文件，确认页面收到成功结果。
3. 等待文档解析/索引状态完成；仅显示“已接收”不代表已经可检索。
4. 回到这台 Mac，用相同身份和知识库查看同一文档，确认文件进入同一套 RAG 系统。

测试结束后在运行终端按 `Ctrl+C` 停止局域网前端；需要仅本机使用时，重新运行
`npm run dev -- --port 3002`。

## 操作顺序

1. 选择租户管理员或系统管理员身份进入。
2. 在“共享知识库”中新建知识库；`External ID` 可留空，填写时应使用外部业务系统的
   稳定唯一 ID。
3. 打开“文档与任务”，选择目标知识库。
4. 一次选择一个或多个文件，在选择区域下方核对“已选择 N 个文件”、完整文件名和大小，
   保留“按文件类型自动路由”并上传。后端能力列表包含 `mineru-cloud@1` 时，PDF
   默认使用 MinerU；同批其他文件仍由后端自动路由。重新选择会更新列表；关闭弹窗会清空选择。
5. 页面自动刷新处理中记录；失败文档可重试，或重新选择 Parser/Chunk 后重解析。
6. 文档进入 `READY` 或 `INDEXED` 后，点击“查看切块”，在与用户端一致的右侧抽屉中
   查看文件名、片段总数、正文和来源页码。顶部和分页固定，只有正文卡片区域滚动，
   每页 20 条；只有多页时才显示分页。窄屏使用全宽抽屉。
   加载失败可重试；点击关闭、遮罩或按 Escape 退出，恢复原按钮焦点和背景滚动。
   关闭后再打开从第一页开始，翻页回到正文顶部。
   查看已有切块不会重新解析文档；仍经管理端同源代理和后端资源范围校验。
7. PDF 使用双栏阅读：左侧原文件、右侧切块。点击切块的“查看原文”跳到对应页并
   高亮有效坐标；跨页切块可点击左侧“对应页”切换。仅有页码则只跳页；缺少页码、
   越界坐标、旋转/裁切等非标准页面会明确提示，不能保证所有历史 PDF 都能高亮。
   非 PDF 继续使用只读切块抽屉，不做格式转换。详见
   [PDF 预览接口与验收](../docs/PDF_SOURCE_PREVIEW.md)。

PDF.js 及其 Worker、字体、字符映射和解码器从本站提供，不使用第三方在线预览服务。
`npm run dev` / `npm run build` 会自动准备这些依赖资源。更新已运行的旧前端时可先执行
`node scripts/prepare-pdf-assets.mjs`；新增 `/source` 接口需要后端加载本次代码才能使用。

每个文件上传都会生成新的 `Idempotency-Key`。不要把一个上传请求的幂等键复制给不同
文件，否则 Gateway 会返回“幂等键与不同请求冲突”。

## 验证

```bash
npx tsc --noEmit
npm run lint
npm test
```

`npm test` 会先执行生产构建，再运行管理范围、API 路径、多文件路由的源码回归，
以及模拟 HTTP/HTTPS 随机数能力的请求行为测试。后者使用假 Gateway，不会上传真实文件。
只运行局域网兼容回归可使用 `node --test tests/lan-upload.test.mjs`；真实跨电脑上传
仍需按上面的验收步骤另行验证。

## 数据库影响

无。该前端没有数据库迁移、建表、清空数据或直连数据库逻辑。
