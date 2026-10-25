#!/usr/bin/env sh
# RAGFlow 专项前检：确认固定镜像、凭据文件和 Apple Silicon 兼容平台，再复用全栈前检。
set -eu

cd "$(dirname "$0")"

command -v docker >/dev/null 2>&1 || {
  echo "docker is not installed or not on PATH" >&2
  exit 1
}

docker info >/dev/null 2>&1 || {
  echo "Docker Desktop/daemon is unavailable" >&2
  exit 1
}

test -f .env.ragflow || {
  echo "missing deploy/local/.env.ragflow; copy .env.ragflow.example first" >&2
  exit 1
}

if grep -E '(^|=)change-me' .env.ragflow >/dev/null 2>&1; then
  echo "deploy/local/.env.ragflow still contains change-me placeholders" >&2
  exit 1
fi

if [ -n "${DOCKER_DEFAULT_PLATFORM:-}" ]; then
  echo "unset global DOCKER_DEFAULT_PLATFORM; platforms are scoped per service" >&2
  exit 1
fi

grep -q '^RAGFLOW_IMAGE=infiniflow/ragflow:v0.26.4$' .env.ragflow || {
  echo "RAGFLOW_IMAGE must be the specification-pinned official v0.26.4 image" >&2
  exit 1
}

# 官方预构建镜像在 Apple Silicon 本地通过 amd64 兼容模式运行。
host_arch="$(uname -m)"
if [ "$host_arch" = "arm64" ] || [ "$host_arch" = "aarch64" ]; then
  grep -q '^RAGFLOW_PLATFORM=linux/amd64$' .env.ragflow || {
    echo "Apple Silicon requires RAGFLOW_PLATFORM=linux/amd64" >&2
    exit 1
  }
fi

test -f .env.full || {
  echo "missing deploy/local/.env.full; the unified M4 Compose requires the full environment" >&2
  exit 1
}

echo "ragflow environment preflight passed: host_arch=$host_arch"
exec ./preflight-full.sh
