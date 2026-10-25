#!/usr/bin/env sh
# 从 Git 忽略环境文件安全读取云端模型配置，再执行官方 RAGFlow Provider Bootstrap。
# 脚本不 source 文件、不打印变量值，也不修改或删除任何现有 Dataset、文档和 Volume。
set -eu

repo_root="$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)"
# 独立执行用于首次初始化时读取已轮换的 .env.ragflow；完整启动脚本会显式传入
# .env.full，使容器与 Bootstrap 始终使用同一份最终配置。
env_file="${RAGFLOW_ENV_FILE:-$repo_root/deploy/local/.env.ragflow}"

test -f "$env_file" || {
  echo "missing ignored model environment: $env_file" >&2
  exit 1
}

# 只接受简单 KEY=VALUE；任何 Shell 语法都作为普通文本处理，避免执行环境文件内容。
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in
    ""|\#*) continue ;;
    *=*)
      key=${line%%=*}
      value=${line#*=}
      case "$key" in
        [A-Za-z_]* )
          case "$key" in
            *[!A-Za-z0-9_]* )
              echo "invalid environment variable name in $env_file: $key" >&2
              exit 1
              ;;
          esac
          export "$key=$value"
          ;;
        *)
          echo "invalid environment line in $env_file" >&2
          exit 1
          ;;
      esac
      ;;
    *)
      echo "invalid environment line in $env_file" >&2
      exit 1
      ;;
  esac
done < "$env_file"

# 宿主机通过本地回环端口访问已启动的 RAGFlow；容器内 Gateway 仍使用内部服务名。
RAGFLOW_BASE_URL="http://127.0.0.1:${RAGFLOW_API_PORT:-9380}"
export RAGFLOW_BASE_URL

cd "$repo_root"
exec uv run python tools/ragflow_cloud_model_bootstrap.py \
  --source-env "$repo_root/deploy/local/.env.ragflow" \
  --runtime-env "$env_file" \
  "$@"
