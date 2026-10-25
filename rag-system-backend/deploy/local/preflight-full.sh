#!/usr/bin/env sh
# 完整本地栈的静态启动前检查；只验证工具、环境、固定版本和 Compose 渲染，不启动服务。
set -eu

repo_root="$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)"
env_file="$repo_root/deploy/local/.env.full"
compose_file="$repo_root/compose.local.yml"
storage_compose_file="$repo_root/deploy/local/docker-compose.storage.yml"

# 先检查本机工具与 Docker Daemon，避免后面给出误导性的配置错误。
command -v docker >/dev/null 2>&1 || {
  echo "docker is not installed or not on PATH" >&2
  exit 1
}
command -v uv >/dev/null 2>&1 || {
  echo "uv is not installed or not on PATH" >&2
  exit 1
}
docker info >/dev/null 2>&1 || {
  echo "Docker Desktop/daemon is unavailable" >&2
  exit 1
}
test -f "$env_file" || {
  echo "missing deploy/local/.env.full; run tools/prepare_local_env.py first" >&2
  exit 1
}
# 默认占位值、全局跨架构覆盖和错误模型契约都必须在启动前失败。
if grep -E '(^|=)change-me' "$env_file" >/dev/null 2>&1; then
  echo "deploy/local/.env.full still contains change-me placeholders" >&2
  exit 1
fi
if [ -n "${DOCKER_DEFAULT_PLATFORM:-}" ]; then
  echo "unset global DOCKER_DEFAULT_PLATFORM; platforms are scoped per service" >&2
  exit 1
fi
grep -q '^RAGFLOW_IMAGE=infiniflow/ragflow:v0.26.4$' "$env_file" || {
  echo "RAGFLOW_IMAGE must be the specification-pinned official v0.26.4 image" >&2
  exit 1
}
# 本地自动账号初始化依赖注册入口开启。该 Compose 同时强制所有 RAGFlow 宿主机端口
# 绑定 127.0.0.1，因此这里只批准开发 Profile 的值 1，不代表生产注册策略。
grep -q '^RAGFLOW_REGISTER_ENABLED=1$' "$env_file" || {
  echo "RAGFLOW_REGISTER_ENABLED must remain 1 for the loopback-only local profile" >&2
  exit 1
}
# 通用模型契约只关心每种能力的 Base URL、API Key 和模型名，不限制供应商、域名
# 或具体模型。这里仅执行静态完整性检查；鉴权、Embedding 维度、Chat JSON/SSE、
# Vision 和可选 Rerank/ASR 的真实协议由随后 Bootstrap 与运行时健康检查验证。
required_model_fields="
LLM_BASE_URL LLM_API_KEY LLM_MODEL REWRITE_MODEL
VISION_BASE_URL VISION_API_KEY VISION_MODEL
EMBEDDING_BASE_URL EMBEDDING_API_KEY EMBEDDING_MODEL
"
for field_name in $required_model_fields; do
  field_value="$(awk -F= -v wanted="$field_name" '$1 == wanted { sub(/^[^=]*=/, ""); print }' "$env_file")"
  if [ -z "$field_value" ]; then
    echo "$field_name must be configured" >&2
    exit 1
  fi
done
for base_url_field in LLM_BASE_URL VISION_BASE_URL EMBEDDING_BASE_URL; do
  base_url_value="$(awk -F= -v wanted="$base_url_field" '$1 == wanted { sub(/^[^=]*=/, ""); print }' "$env_file")"
  case "$base_url_value" in
    http://*|https://*) ;;
    *)
      echo "$base_url_field must be an HTTP(S) URL" >&2
      exit 1
      ;;
  esac
done

# Rerank 关闭时保持向量、BM25、RRF 降级；开启时要求通用三元组完整。
rerank_enabled="$(awk -F= '$1 == "RERANK_ENABLED" { print $2 }' "$env_file")"
case "$rerank_enabled" in
  false)
    ;;
  true)
    rerank_base_url="$(awk -F= '$1 == "RERANK_BASE_URL" { sub(/^[^=]*=/, ""); print }' "$env_file")"
    rerank_api_key="$(awk -F= '$1 == "RERANK_API_KEY" { sub(/^[^=]*=/, ""); print }' "$env_file")"
    rerank_model="$(awk -F= '$1 == "RERANK_MODEL" { sub(/^[^=]*=/, ""); print }' "$env_file")"
    case "$rerank_base_url" in
      http://*|https://*) ;;
      *)
        echo "enabled rerank requires an HTTP(S) RERANK_BASE_URL" >&2
        exit 1
        ;;
    esac
    if [ -z "$rerank_api_key" ] || [ -z "$rerank_model" ]; then
      echo "enabled rerank requires RERANK_API_KEY and RERANK_MODEL" >&2
      exit 1
    fi
    ;;
  *)
    echo "RERANK_ENABLED must be exactly true or false" >&2
    exit 1
    ;;
esac

# 音视频是可选能力；开启时 ASR 与视频抽帧视觉各自使用独立三元组，关闭时允许留空。
media_enabled="$(awk -F= '$1 == "MEDIA_PROCESSING_ENABLED" { print $2 }' "$env_file")"
case "$media_enabled" in
  false) ;;
  true)
    for field_name in \
      ASR_BASE_URL ASR_API_KEY ASR_MODEL \
      MEDIA_VISION_BASE_URL MEDIA_VISION_API_KEY MEDIA_VISION_MODEL; do
      field_value="$(awk -F= -v wanted="$field_name" '$1 == wanted { sub(/^[^=]*=/, ""); print }' "$env_file")"
      if [ -z "$field_value" ]; then
        echo "enabled media processing requires $field_name" >&2
        exit 1
      fi
    done
    ;;
  *)
    echo "MEDIA_PROCESSING_ENABLED must be exactly true or false" >&2
    exit 1
    ;;
esac

# 首轮只启动 RAGFlow 时允许模型引用与维度等待真实探测；Gateway 启动前的最终
# Preflight 必须拿到正整数维度和完整 RAGFlow 模型引用。
embedding_dimension="$(awk -F= '$1 == "EMBEDDING_DIMENSION" { print $2 }' "$env_file")"
ragflow_embedding_model="$(awk -F= '$1 == "RAGFLOW_EMBEDDING_MODEL" { print $2 }' "$env_file")"
ragflow_vision_model="$(awk -F= '$1 == "RAGFLOW_VISION_MODEL" { print $2 }' "$env_file")"
if [ "${ALLOW_PENDING_MODEL_CONFIG:-0}" = "1" ]; then
  case "$embedding_dimension" in
    pending-auto-detection|[1-9][0-9]*) ;;
    *) echo "EMBEDDING_DIMENSION must be pending-auto-detection or a positive integer" >&2; exit 1 ;;
  esac
else
  case "$embedding_dimension" in
    [1-9][0-9]*) ;;
    *) echo "EMBEDDING_DIMENSION must be a positive integer after model bootstrap" >&2; exit 1 ;;
  esac
  case "$ragflow_embedding_model" in
    ""|pending-model-bootstrap) echo "RAGFLOW_EMBEDDING_MODEL is unresolved" >&2; exit 1 ;;
  esac
  case "$ragflow_vision_model" in
    ""|pending-model-bootstrap) echo "RAGFLOW_VISION_MODEL is unresolved" >&2; exit 1 ;;
  esac
fi

cd "$repo_root"
# 让 Docker 完成变量插值/合并，再把最终 JSON 交给项目专用安全拓扑校验器。
docker compose --profile smoke --env-file "$env_file" -f "$compose_file" \
  config --format json \
  | uv run python tools/validate_compose.py --topology runtime

# 本地模式继续渲染并校验独立存储 Compose；external 模式只使用远端连接字段，
# 不应为了一个不会启动的容器要求 PostgreSQL、Redis、Elasticsearch 或 MinIO 镜像。
# 外部服务的协议、账号、权限和健康状态会由后续 RAGFlow/Application Bootstrap 真实
# 连接验证；这里仍先拒绝缺少模式或拼写错误，避免静默落到错误拓扑。
storage_mode="$(awk -F= '$1 == "RAG_STORAGE_MODE" { print $2 }' "$env_file")"
case "$storage_mode" in
  local)
    docker compose --env-file "$env_file" -f "$storage_compose_file" \
      config --format json \
      | uv run python tools/validate_compose.py --topology storage
    echo "runtime and local storage static preflight passed"
    ;;
  external)
    echo "runtime and external storage static preflight passed"
    ;;
  *)
    echo "RAG_STORAGE_MODE must be exactly local or external" >&2
    exit 1
    ;;
esac
