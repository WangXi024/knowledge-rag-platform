#!/usr/bin/env bash
# 输出统一 Redis Stack 的脱敏运行快照。
#
# 本脚本通过 storage Compose 在 redis-stack 容器内部执行 redis-cli，因此不会把密码
# 拼进命令行输出，也不要求宿主机安装 Redis 客户端。它只读取 INFO、DBSIZE 和 Celery
# 队列长度，不修改 Key、不触发清理，更不会执行 FLUSHDB/FLUSHALL。输出适合用于判断
# DB0/1/2 的 Key 规模、DB2 两条任务队列是否积压以及共享内存是否接近 noeviction 上限。

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${RAG_ENV_FILE:-${REPO_ROOT}/deploy/local/.env.full}"
COMPOSE_FILE="${REPO_ROOT}/deploy/local/docker-compose.storage.yml"

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "missing ignored runtime environment: ${ENV_FILE}" >&2
  echo "run ./deploy/local/start-full.sh once before requesting the Redis report" >&2
  exit 1
fi

# REDISCLI_AUTH 只存在于容器内这次 shell 进程，避免 redis-cli 的 -a 参数出现在进程
# 参数或诊断输出中。所有 Key 名都是项目固定命名空间，不接受用户输入。
docker compose --env-file "${ENV_FILE}" -f "${COMPOSE_FILE}" \
  exec -T redis-stack sh -c '
    set -eu
    export REDISCLI_AUTH="$REDIS_STACK_PASSWORD"

    # Redis 的 INFO 每次最多接受一个 section。读取完整 INFO 后只提取固定字段，
    # 既兼容 Redis Stack 当前版本，也避免三次网络往返造成同一报告时间点不一致。
    info="$(redis-cli --no-auth-warning --raw INFO)"
    info_value() {
      printf "%s\n" "$info" | awk -F: -v key="$1" \
        "$1 == key { gsub(/\\r/, \"\", $2); print $2; exit }"
    }

    used_memory="$(info_value used_memory)"
    maxmemory="$(info_value maxmemory)"
    memory_ratio="unbounded"
    if [ "${maxmemory:-0}" -gt 0 ]; then
      memory_ratio="$(awk -v used="$used_memory" -v max="$maxmemory" \
        "BEGIN { printf \"%.2f%%\", used * 100 / max }")"
    fi

    echo "Redis Stack runtime report"
    echo "role_key_counts: db0_cache=$(redis-cli --no-auth-warning -n 0 DBSIZE) db1_ragflow=$(redis-cli --no-auth-warning -n 1 DBSIZE) db2_celery=$(redis-cli --no-auth-warning -n 2 DBSIZE)"
    echo "celery_queue_lengths: ingestion=$(redis-cli --no-auth-warning -n 2 LLEN rag:celery:rag-ingestion) maintenance=$(redis-cli --no-auth-warning -n 2 LLEN rag:celery:rag-maintenance)"
    echo "celery_unacked: messages=$(redis-cli --no-auth-warning -n 2 HLEN rag:celery:unacked) deadlines=$(redis-cli --no-auth-warning -n 2 ZCARD rag:celery:unacked_index)"
    echo "memory: used=$(info_value used_memory_human) max=$(info_value maxmemory_human) ratio=$memory_ratio policy=$(info_value maxmemory_policy)"
    echo "safety: evicted_keys=$(info_value evicted_keys) rejected_connections=$(info_value rejected_connections) aof_enabled=$(info_value aof_enabled) aof_status=$(info_value aof_last_write_status)"
  '
