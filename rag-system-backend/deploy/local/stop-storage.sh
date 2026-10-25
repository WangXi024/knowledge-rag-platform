#!/usr/bin/env sh
# 单独停止本地存储容器；不执行 down -v，因此数据库、索引和对象文件仍保留。
set -eu

repo_root="$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)"

docker compose \
  --env-file "$repo_root/deploy/local/.env.full" \
  -f "$repo_root/deploy/local/docker-compose.storage.yml" \
  stop

echo "storage stack stopped; persistent volumes were retained"
