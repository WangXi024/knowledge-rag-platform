#!/usr/bin/env bash
# PostgreSQL 官方镜像只会自动创建一个业务数据库。本脚本仅在新的共享数据卷首次
# 初始化时运行：使用容器内部管理员连接，分别创建应用数据库和 RAGFlow 数据库，
# 再撤销 PUBLIC 的默认连接权限，使两个业务账号不能读取或修改对方数据库。
set -Eeuo pipefail

required_names=(
  APP_POSTGRES_DATABASE
  APP_POSTGRES_USER
  APP_POSTGRES_PASSWORD
  RAGFLOW_POSTGRES_DATABASE
  RAGFLOW_POSTGRES_USER
  RAGFLOW_POSTGRES_PASSWORD
)
for name in "${required_names[@]}"; do
  if [[ -z "${!name:-}" ]]; then
    echo "missing required PostgreSQL initialization variable: ${name}" >&2
    exit 1
  fi
done

# 数据库名和角色名最终会作为 SQL 标识符使用。密码通过 psql 变量按字符串字面量
# 引用，不经过 Shell 或 SQL 拼接；标识符仍在进入 psql 前限制为安全字符集合。
for identifier in \
  "$APP_POSTGRES_DATABASE" \
  "$APP_POSTGRES_USER" \
  "$RAGFLOW_POSTGRES_DATABASE" \
  "$RAGFLOW_POSTGRES_USER"; do
  if [[ ! "$identifier" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
    echo "invalid PostgreSQL identifier: ${identifier}" >&2
    exit 1
  fi
done

psql \
  --set=ON_ERROR_STOP=1 \
  --username "$POSTGRES_USER" \
  --dbname "$POSTGRES_DB" \
  --set=app_database="$APP_POSTGRES_DATABASE" \
  --set=app_user="$APP_POSTGRES_USER" \
  --set=app_password="$APP_POSTGRES_PASSWORD" \
  --set=ragflow_database="$RAGFLOW_POSTGRES_DATABASE" \
  --set=ragflow_user="$RAGFLOW_POSTGRES_USER" \
  --set=ragflow_password="$RAGFLOW_POSTGRES_PASSWORD" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'app_user', :'app_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'app_user') \gexec

SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'ragflow_user', :'ragflow_password')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'ragflow_user') \gexec

SELECT format('CREATE DATABASE %I OWNER %I ENCODING ''UTF8''', :'app_database', :'app_user')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'app_database') \gexec

SELECT format(
  'CREATE DATABASE %I OWNER %I ENCODING ''UTF8''',
  :'ragflow_database',
  :'ragflow_user'
)
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'ragflow_database') \gexec

SELECT format('REVOKE CONNECT ON DATABASE %I FROM PUBLIC', :'app_database') \gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO %I', :'app_database', :'app_user') \gexec
SELECT format('REVOKE CONNECT ON DATABASE %I FROM PUBLIC', :'ragflow_database') \gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO %I', :'ragflow_database', :'ragflow_user') \gexec
SQL
