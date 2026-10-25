#!/usr/bin/env sh
# 安全加载本地模型环境，并从宿主机回环地址运行 Gateway 模型适配器 Smoke。
set -eu

repo_root="$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)"
source_env_file="$repo_root/deploy/local/.env.ragflow"
runtime_env_file="$repo_root/deploy/local/.env.full"

# 完整运行配置是 Gateway 容器真正使用的环境，也是 DBA 安装并通过只读门禁后的运行事实来源。
# 尚未执行一键启动时才回退读取用户源文件，让该 Smoke 仍能单独用于初始排错。
if [ -f "$runtime_env_file" ]; then
  env_file="$runtime_env_file"
else
  env_file="$source_env_file"
fi

test -f "$env_file" || {
  echo "missing deploy/local/.env.ragflow; copy deploy/local/.env.ragflow.example first" >&2
  exit 1
}

# 不 source 环境文件，避免其中任意 Shell 语法被执行。
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

# Smoke 与运行服务读取完全相同的通用字段，不迁移、不推断任何旧厂商别名。
# 缺少 LLM_API_KEY 或 EMBEDDING_API_KEY 时，Python Smoke 会按字段名安全失败。

cd "$repo_root"
exec uv run python tools/gateway_model_smoke.py "$@"
