#!/usr/bin/env sh
# 参考部署的最小存活检查：Gateway live/ready 与容器内 RAGFlow healthz 均须成功。
set -eu

cd "$(dirname "$0")"

curl -fsS http://127.0.0.1:${RAG_APP_PORT:-8080}/health/live >/dev/null
curl -fsS http://127.0.0.1:${RAG_APP_PORT:-8080}/health/ready >/dev/null

docker compose --env-file .env.full -f docker-compose.full.yml \
  exec -T ragflow-server \
  curl -fsS http://127.0.0.1:9380/api/v1/system/healthz >/dev/null

echo "smoke ok"
