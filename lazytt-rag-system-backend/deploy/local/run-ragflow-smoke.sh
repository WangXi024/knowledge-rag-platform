#!/usr/bin/env sh
# 安全加载 RAGFlow 本地环境，并直接运行数据集/文档/检索生命周期 Smoke。
set -eu

repo_root="$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)"
env_file="$repo_root/deploy/local/.env.ragflow"

test -f "$env_file" || {
  echo "missing deploy/local/.env.ragflow; copy deploy/local/.env.ragflow.example first" >&2
  exit 1
}

# 仅解析合法 KEY=VALUE，不执行环境文件内容。
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in
    ""|\#*) continue ;;
    *=*)
      key=${line%%=*}
      value=${line#*=}
      case "$key" in
        [A-Za-z_]*)
          case "$key" in
            *[!A-Za-z0-9_]*)
              echo "invalid environment variable name in deploy/local/.env.ragflow: $key" >&2
              exit 1
              ;;
          esac
          export "$key=$value"
          ;;
        *)
          echo "invalid environment line in deploy/local/.env.ragflow" >&2
          exit 1
          ;;
      esac
      ;;
    *)
      echo "invalid environment line in deploy/local/.env.ragflow" >&2
      exit 1
      ;;
  esac
done < "$env_file"

test -n "${RAGFLOW_API_KEY:-}" || {
  echo "RAGFLOW_API_KEY is empty; create a local API Key first" >&2
  exit 1
}

cd "$repo_root"
exec python3 tools/ragflow_lifecycle_smoke.py "$@"
