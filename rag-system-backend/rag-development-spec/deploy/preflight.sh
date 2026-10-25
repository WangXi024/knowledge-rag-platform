#!/usr/bin/env sh
# 规格目录中的参考部署前检，验证 Docker、环境变量、架构和 Elasticsearch 内核参数。
set -eu

cd "$(dirname "$0")"

command -v docker >/dev/null 2>&1 || {
  echo "docker is not installed or not on PATH" >&2
  exit 1
}

docker info >/dev/null 2>&1 || {
  echo "Docker daemon/Desktop is not running" >&2
  exit 1
}

test -f .env.full || {
  echo "missing deploy/.env.full; copy .env.full.example and fill every required value" >&2
  exit 1
}

if grep -E '(^|=)change-me' .env.full >/dev/null 2>&1; then
  echo "deploy/.env.full still contains change-me values" >&2
  exit 1
fi

# 逐一确认关键服务地址、凭据和模型契约都显式存在。
for required in \
  RAG_APP_IMAGE RAGFLOW_IMAGE RAGFLOW_PLATFORM APP_DATABASE_DSN \
  APP_REDIS_URL CACHE_REDIS_URL RAGFLOW_BASE_URL RAGFLOW_API_KEY \
  AUTH_AUDIENCE EMBEDDING_BASE_URL EMBEDDING_MODEL \
  EMBEDDING_DIMENSION LLM_BASE_URL LLM_MODEL; do
  grep -q "^${required}=" .env.full || {
    echo "deploy/.env.full is missing required setting: ${required}" >&2
    exit 1
  }
done

# 音视频开启时，ASR 与视频抽帧视觉都必须显式配置；文档 OCR 的三元组不能被代码
# 自动复制到媒体职责，否则更换任一模型时会产生难以诊断的隐式耦合。
media_enabled="$(awk -F= '$1 == "MEDIA_PROCESSING_ENABLED" { print $2 }' .env.full)"
case "${media_enabled:-false}" in
  false) ;;
  true)
    for required in \
      ASR_BASE_URL ASR_API_KEY ASR_MODEL \
      MEDIA_VISION_BASE_URL MEDIA_VISION_API_KEY MEDIA_VISION_MODEL; do
      value="$(awk -F= -v wanted="$required" '$1 == wanted { sub(/^[^=]*=/, ""); print }' .env.full)"
      test -n "$value" || {
        echo "enabled media processing requires: $required" >&2
        exit 1
      }
    done
    ;;
  *)
    echo "MEDIA_PROCESSING_ENABLED must be exactly true or false" >&2
    exit 1
    ;;
esac

if [ "${DOCKER_DEFAULT_PLATFORM:-}" = "linux/amd64" ]; then
  echo "unset global DOCKER_DEFAULT_PLATFORM; only RAGFlow should use linux/amd64" >&2
  exit 1
fi

# Apple Silicon 仅让 RAGFlow 使用 amd64 兼容模式，不能全局覆盖所有服务平台。
host_arch="$(uname -m)"
if [ "$host_arch" = "arm64" ] || [ "$host_arch" = "aarch64" ]; then
  grep -q '^RAGFLOW_PLATFORM=linux/amd64$' .env.full || {
    echo "Apple Silicon requires RAGFLOW_PLATFORM=linux/amd64 for the prebuilt RAGFlow image" >&2
    exit 1
  }
  echo "preflight: Apple Silicon detected; RAGFlow will run in linux/amd64 compatibility mode"
fi

docker compose --env-file .env.full -f docker-compose.full.yml config >/dev/null

map_count="$(sysctl -n vm.max_map_count 2>/dev/null || true)"
if [ -n "$map_count" ] && [ "$map_count" -lt 262144 ]; then
  echo "vm.max_map_count must be at least 262144 (current: $map_count)" >&2
  exit 1
fi

echo "preflight ok"
