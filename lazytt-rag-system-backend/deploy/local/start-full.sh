#!/usr/bin/env sh
# 本地完整栈启动入口：先按配置启动独立存储栈，再启动 RAGFlow 与应用运行时。
# external 模式跳过本地存储容器；重复执行会复用账号、Key 和全部数据卷。
set -eu

repo_root="$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)"
ragflow_env_file="$repo_root/deploy/local/.env.ragflow"
env_file="$repo_root/deploy/local/.env.full"
compose_file="$repo_root/compose.local.yml"

# 用户只需要从示例复制并填写这一份私有配置。脚本不会替用户猜密码或云端 Key，
# 缺失时立即给出可操作提示，也不会覆盖现有密码、API Key 或任何 Docker Volume。
test -f "$ragflow_env_file" || {
  echo "missing deploy/local/.env.ragflow; copy deploy/local/.env.ragflow.example and fill it first" >&2
  exit 1
}

# ``uv sync`` 后先补齐七个本地密码：只处理空值/change-me，并把旧
# RAGFLOW_REDIS_PASSWORD 无损迁移为统一 REDIS_STACK_PASSWORD；占位值第一次生成后永不
# 轮换。环境合并具有幂等性，已有依赖、密码和数据卷会直接复用。
cd "$repo_root"
uv sync --frozen --extra dev
uv run python tools/prepare_ragflow_env.py --env-file "$ragflow_env_file"
uv run python tools/prepare_local_env.py --force --allow-pending-ragflow-key

# 第一轮静态前检允许专用 pending Key，因为此时只会启动 RAGFlow 子系统，不会让
# Gateway/Worker 使用占位值。镜像版本、模型、密码、网络暴露和 Compose 拓扑仍会校验。
ALLOW_PENDING_MODEL_CONFIG=1 "$repo_root/deploy/local/preflight-full.sh"

# 两个 Compose 使用不同 Project 名，但服务之间仍需通过同一 Docker 网络通信。
# 该网络不归任一 Project 所有：不存在时只创建一次，已存在时直接复用。这样停止或
# 重建 runtime 不会影响 storage，外部存储模式也能让 RAGFlow 与 Gateway 相互访问。
network_name="$(awk -F= '$1 == "RAG_DOCKER_NETWORK" { print $2 }' "$env_file")"
test -n "$network_name" || {
  echo "RAG_DOCKER_NETWORK must be configured" >&2
  exit 1
}
if ! docker network inspect "$network_name" >/dev/null 2>&1; then
  docker network create "$network_name" >/dev/null
  echo "created shared Docker network $network_name"
fi

# local 模式先启动独立 storage Project；external 模式跳过本地存储容器。
storage_mode="$(awk -F= '$1 == "RAG_STORAGE_MODE" { print $2 }' "$env_file")"
if [ "${storage_mode:-local}" = "local" ]; then
  # --remove-orphans 只停止并删除已从当前拓扑移除的三个旧 Redis
  # 容器；不删除它们的命名卷。因此 Docker Desktop 最终只保留一个
  # redis-stack 服务，仍可用合并前 Git 检查点恢复旧拓扑。
  docker compose \
    --env-file "$env_file" \
    -f "$repo_root/deploy/local/docker-compose.storage.yml" \
    up -d --wait --remove-orphans --wait-timeout "${COMPOSE_WAIT_TIMEOUT_SECONDS:-600}"
else
  echo "external storage mode selected; local storage containers were not started"
fi

# RAGFlow Server 依赖只读的 ``rag-app-schema-check``。这个检查容器和
# Gateway/Worker 共用同一个应用镜像，因此必须在第一次启动 RAGFlow 依赖链之前构建。
# 否则，当本地还残留旧版 ``RAG_APP_IMAGE`` 时，Compose 会直接运行旧镜像；旧版 CLI
# 不认识当前的 ``schema check`` 子命令，会在真正连接 PostgreSQL 之前以退出码 2 结束。
# 这里只构建无状态应用镜像，不启动服务，也不会修改数据库、对象存储或任何数据卷。
docker compose \
  --env-file "$env_file" \
  -f "$compose_file" \
  build rag-app-schema-check

# 本地开发库由项目自己维护：空库执行冻结 V1.0.0 SQL，再升级到 V1.1.0，
# 已登记版本则幂等跳过。命令内部还会再次校验 RAG_STORAGE_MODE；发现半成品 Schema
# 会保留数据并停止。external 仍完全由 DBA 管理，因此绝不调用这个写入入口。
if [ "${storage_mode:-local}" = "local" ]; then
  docker compose \
    --env-file "$env_file" \
    -f "$compose_file" \
    run --rm --no-deps rag-app-schema-check \
    python -m rag_service.cli schema prepare-local
  # 同一套冻结 SQL 初始化空的 RAGFlow 库；已有完整结构只读复用，不造账号或模型。
  docker compose --env-file "$env_file" -f "$compose_file" \
    run --rm --no-deps rag-app-schema-check \
    python -m rag_service.cli schema prepare-local-ragflow
fi

# 存储健康后只启动 RAGFlow 运行时。注册入口在本地开发 Profile 中保持开启，但
# 管理 UI/API 只发布到 127.0.0.1，不会向局域网或公网暴露。
docker compose \
  --env-file "$env_file" \
  -f "$compose_file" \
  up -d --wait --wait-timeout "${COMPOSE_WAIT_TIMEOUT_SECONDS:-600}" \
  ragflow-server ragflow-task-executor

# 账号脚本先验证已有 Key；无有效 Key 时使用固定邮箱和已生成的密码登录，账号不存在
# 才注册，再复用或创建 Token。它只写回 RAGFLOW_API_KEY，绝不轮换管理员密码。
uv run python tools/ragflow_account_bootstrap.py \
  --env-file "$ragflow_env_file" \
  --full-env-file "$env_file" \
  --compose-file "$compose_file"

# 把刚验证的 Key 同步进最终 Compose 环境。随后执行真实模型探测和 RAGFlow Provider
# 注册；Bootstrap 会把实际维度、兼容参数和 RAGFlow 模型引用原子写回私有环境。
uv run python tools/prepare_local_env.py --force
RAGFLOW_ENV_FILE="$env_file" "$repo_root/deploy/local/bootstrap-ragflow-cloud-models.sh"

# 重新生成运行环境以载入 Bootstrap 写回的模型结果，并执行禁止 pending 值的最终
# 静态门禁。只有这一步通过，Gateway、Worker 和 Scheduler 才允许启动。
uv run python tools/prepare_local_env.py --force
"$repo_root/deploy/local/preflight-full.sh"

# 只输出是否启用，不把官网 Token 放进命令参数或启动日志。
mineru_enabled="$(uv run python - "$env_file" <<'PYCONFIG'
import shlex
import sys
from pathlib import Path
value = ""
for line in Path(sys.argv[1]).read_text().splitlines():
    if line.startswith("MINERU_CLOUD_API_TOKEN="):
        parts = shlex.split(line.split("=", 1)[1], comments=True)
        value = parts[0] if parts else ""
print("yes" if value and "<" not in value and not value.startswith("change-me") else "no")
PYCONFIG
)"
# 填写 Token 后自动启用并通过官方 API 注册；失败则阻止后续启动，不冒充就绪。
if [ "$mineru_enabled" = "yes" ]; then
  docker compose --env-file "$env_file" -f "$compose_file" \
    up -d --no-deps --wait --wait-timeout "${COMPOSE_WAIT_TIMEOUT_SECONDS:-600}" \
    mineru-cloud-adapter
  docker compose --env-file "$env_file" -f "$compose_file" \
    run --rm --no-deps rag-app-bootstrap \
    python -m rag_service.infrastructure.mineru_cloud.register
else
  echo "MinerU skipped: MINERU_CLOUD_API_TOKEN is not configured"
fi

# Provider 默认值持久化并验证后才构建 Gateway/Worker，避免半配置服务进入 ready。
# full Compose 只包含运行时服务；存储由上面的独立 Compose 或外部地址继续提供。
# --force-recreate 只重建三个无状态应用容器，确保它们一定读取本次生成的 Redis URL。
# 这一步不会删除或重建 PostgreSQL、Redis Stack、Elasticsearch、MinIO 数据卷，也不会
# 重建刚完成模型注册的 RAGFlow 容器。
docker compose \
  --env-file "$env_file" \
  -f "$compose_file" \
  up -d --build --wait --force-recreate \
  --wait-timeout "${COMPOSE_WAIT_TIMEOUT_SECONDS:-600}" \
  rag-gateway rag-worker rag-scheduler

# 根据最终环境显示真实可用入口。绑定 0.0.0.0 时，本机仍可用回环
# 地址；同时尽力识别 macOS 的 Bonjour/mDNS 主机名和 Mac/Linux 的
# 局域网 IP。mDNS 名称不依赖 DHCP 分配的具体 IP，适合同一局域网内
# 支持 Bonjour/Avahi 的调用方使用。这里只读取系统信息，不会修改主机名、
# 防火墙或路由器。
gateway_bind_address="$(awk -F= '$1 == "RAG_APP_BIND_ADDRESS" { print $2 }' "$env_file")"
gateway_port="$(awk -F= '$1 == "RAG_APP_PORT" { print $2 }' "$env_file")"
if [ "$gateway_bind_address" = "0.0.0.0" ]; then
  mdns_hostname=""
  if command -v scutil >/dev/null 2>&1; then
    local_hostname="$(scutil --get LocalHostName 2>/dev/null || true)"
    test -z "$local_hostname" || mdns_hostname="${local_hostname}.local"
  fi
  lan_ip=""
  if command -v ipconfig >/dev/null 2>&1; then
    lan_ip="$(ipconfig getifaddr en0 2>/dev/null || true)"
    test -n "$lan_ip" || lan_ip="$(ipconfig getifaddr en1 2>/dev/null || true)"
  fi
  if [ -z "$lan_ip" ] && command -v hostname >/dev/null 2>&1; then
    lan_ip="$(hostname -I 2>/dev/null | awk '{ print $1 }' || true)"
  fi
  echo "RAG Gateway local: http://127.0.0.1:${gateway_port:-8080}"
  if [ -n "$mdns_hostname" ]; then
    echo "RAG Gateway mDNS: http://${mdns_hostname}:${gateway_port:-8080}"
  fi
  if [ -n "$lan_ip" ]; then
    echo "RAG Gateway LAN: http://${lan_ip}:${gateway_port:-8080}"
  else
    echo "RAG Gateway LAN: http://<this-computer-LAN-IP>:${gateway_port:-8080}"
  fi
else
  echo "RAG Gateway: http://127.0.0.1:${gateway_port:-8080}"
fi
echo "RAGFlow local management UI: http://127.0.0.1:8081"
