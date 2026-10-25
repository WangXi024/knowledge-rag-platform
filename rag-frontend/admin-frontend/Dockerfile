# 管理端镜像：构建 vinext Worker，并在容器中运行同源 Gateway 代理。
# 运行时通过 GATEWAY_BASE_URL 指定 Gateway；镜像中不保存凭据或环境文件。
FROM node:22-bookworm-slim

# workerd 通过系统 CA 验证 Gateway 的 HTTPS 证书；slim 基础镜像未包含证书包。
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN chown node:node /app
USER node

COPY --chown=node:node package.json package-lock.json ./
RUN npm ci
COPY --chown=node:node . .
RUN npm run build && npm run lint && node --test tests/*.test.mjs

EXPOSE 19382
# 健康检查经过同源代理访问 Gateway，避免只检测到前端首页可打开。
HEALTHCHECK --interval=10s --timeout=5s --start-period=30s --retries=12 CMD node -e "fetch('http://127.0.0.1:19382/api/gateway/health/ready', {headers: {'x-tenant-id': 'health', 'x-user-id': 'health'}, signal: AbortSignal.timeout(4000)}).then(r => { if (!r.ok) console.error('Gateway readiness HTTP', r.status); process.exit(r.ok ? 0 : 1); }).catch(e => { console.error('Gateway readiness failed:', e.message); process.exit(1); })"

# 当前 API 路由依赖 Cloudflare Worker 绑定；vinext start 的 Node 路由无法加载该绑定。
CMD ["sh", "-ec", "exec ./node_modules/.bin/wrangler dev --config dist/server/wrangler.json --ip 127.0.0.1 --port 19382 --local --var \"GATEWAY_BASE_URL:${GATEWAY_BASE_URL:?}\""]
