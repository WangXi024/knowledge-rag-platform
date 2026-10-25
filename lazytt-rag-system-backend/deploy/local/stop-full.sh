#!/usr/bin/env sh
# 停止 RAGFlow、应用及已启用的 MinerU 适配服务；存储栈和数据卷保持不变。
set -eu

repo_root="$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)"

docker compose --profile mineru \
  --env-file "$repo_root/deploy/local/.env.full" \
  -f "$repo_root/compose.local.yml" \
  stop

echo "runtime stack stopped; storage stack is unchanged and persistent volumes were retained"
