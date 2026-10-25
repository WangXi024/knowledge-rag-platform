#!/usr/bin/env sh
# 单独启动本地存储栈；只创建缺失网络和启动服务，不删除或重建任何命名卷。
set -eu

repo_root="$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)"
env_file="$repo_root/deploy/local/.env.full"
compose_file="$repo_root/deploy/local/docker-compose.storage.yml"

test -f "$env_file" || {
  echo "missing deploy/local/.env.full; run ./deploy/local/start-full.sh once or prepare the environment first" >&2
  exit 1
}

storage_mode="$(awk -F= '$1 == "RAG_STORAGE_MODE" { print $2 }' "$env_file")"
if [ "${storage_mode:-local}" != "local" ]; then
  echo "RAG_STORAGE_MODE is external; local storage containers were not started"
  exit 0
fi

# storage 与 runtime 是两个独立 Project，共享网络必须在 Project 之外存在。这里
# 幂等创建网络，使单独执行本脚本也能从全新 Docker 环境启动；不会删除已有网络。
network_name="$(awk -F= '$1 == "RAG_DOCKER_NETWORK" { print $2 }' "$env_file")"
test -n "$network_name" || {
  echo "RAG_DOCKER_NETWORK must be configured" >&2
  exit 1
}
if ! docker network inspect "$network_name" >/dev/null 2>&1; then
  docker network create "$network_name" >/dev/null
  echo "created shared Docker network $network_name"
fi

# 第一次从三个 Redis 容器切换时，移除已不在 Compose 中的旧容器。
# 该选项不会删除旧命名卷，也不会执行任何 down -v 操作。
docker compose --env-file "$env_file" -f "$compose_file" \
  up -d --wait --remove-orphans --wait-timeout "${COMPOSE_WAIT_TIMEOUT_SECONDS:-600}"

echo "local storage stack is healthy; persistent volumes were retained"
