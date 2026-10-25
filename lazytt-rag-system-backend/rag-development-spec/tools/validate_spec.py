"""技术规格自洽性门禁：交叉校验 OpenAPI、数据库、配置、部署和安全文档。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]

REQUIRED_FILES = {
    "README.md",
    "openapi/rag-api-v1.yaml",
    "database/schema.sql",
    "configs/application.example.yaml",
    "deploy/docker-compose.app.yml",
    "deploy/docker-compose.full.yml",
    "deploy/docker-compose.storage.yml",
    "deploy/.env.example",
    "deploy/.env.full.example",
    "deploy/ragflow/service_conf.postgres.yaml.template",
    "deploy/preflight.sh",
    "deploy/smoke.sh",
    "docs/00-需求与范围说明.md",
    "docs/01-RAG系统详细设计与开发规范.md",
    "docs/02-API接口说明.md",
    "docs/03-RAGFlow接口映射.md",
    "docs/04-错误码说明.md",
    "docs/05-数据库设计.md",
    "docs/06-部署运维手册.md",
    "docs/07-测试与验收规范.md",
    "docs/08-Codex实施指令.md",
    "docs/09-技术分析与选型报告.md",
    "docs/10-安全与威胁模型.md",
    "docs/11-第一期测试用例与验收清单.md",
    "docs/12-Redis语义缓存物理设计.md",
    "contracts/ragflow-fixture-manifest.yaml",
}

REQUIRED_OPERATIONS = {
    "createKnowledgeBase",
    "uploadDocument",
    "parseDocument",
    "getJob",
    "retrieve",
    "createAnswer",
    "streamAnswer",
    "listRequestCitations",
    "createFeedback",
}

REQUIRED_TABLES = {
    "config_profile",
    "knowledge_base",
    "document_record",
    "ingestion_job",
    "idempotency_record",
    "query_record",
    "citation_record",
    "feedback_record",
}

SCOPED_TABLES = REQUIRED_TABLES - {"config_profile"}

REQUIRED_RUNTIME_COMPOSE_SERVICES = {
    "ragflow-smoke-embedding",
    "ragflow-server",
    "ragflow-task-executor",
    "rag-gateway",
    "rag-worker",
    "rag-scheduler",
    "rag-app-schema-check",
    "rag-app-bootstrap",
}

REQUIRED_STORAGE_COMPOSE_SERVICES = {
    "app-postgres",
    "ragflow-postgres",
    "redis-stack",
    "minio",
    "elasticsearch",
}


def walk(value: Any):
    """深度遍历嵌套字典/列表中的所有节点。"""
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def resolve_local_ref(spec: dict[str, Any], ref: str) -> Any:
    """解析 OpenAPI 文档内部 ``#/...`` JSON Pointer 引用。"""
    if not ref.startswith("#/"):
        return None
    current: Any = spec
    for part in ref[2:].split("/"):
        if not isinstance(current, dict) or part not in current:
            raise AssertionError(f"Missing OpenAPI ref: {ref}")
        current = current[part]
    return current


def validate_openapi(path: Path) -> tuple[int, int]:
    """校验路径/方法、operationId、响应、错误结构和全部本地引用。"""
    text = path.read_text(encoding="utf-8")
    spec = yaml.safe_load(text)
    assert spec["openapi"].startswith("3.1")
    assert isinstance(spec.get("paths"), dict) and spec["paths"]
    assert spec.get("security") == [{"directTenantId": [], "directUserId": []}]

    tenant_identity = spec["components"]["securitySchemes"]["directTenantId"]
    assert tenant_identity == {
        "type": "apiKey",
        "in": "header",
        "name": "X-Tenant-ID",
        "description": tenant_identity["description"],
    }
    user_identity = spec["components"]["securitySchemes"]["directUserId"]
    assert user_identity == {
        "type": "apiKey",
        "in": "header",
        "name": "X-User-ID",
        "description": user_identity["description"],
    }
    assert "不是认证凭据" in tenant_identity["description"]

    refs = [node["$ref"] for node in walk(spec) if isinstance(node, dict) and "$ref" in node]
    for ref in refs:
        resolve_local_ref(spec, ref)

    methods = {"get", "post", "put", "patch", "delete", "options", "head"}
    operation_ids: list[str] = []
    for path_item in spec["paths"].values():
        for method, operation in path_item.items():
            if method in methods:
                operation_ids.append(operation["operationId"])
    assert len(operation_ids) == len(set(operation_ids)), "Duplicate operationId"
    missing_operations = REQUIRED_OPERATIONS - set(operation_ids)
    assert not missing_operations, f"Missing required operations: {sorted(missing_operations)}"
    assert "EVALUATE" not in text, "Deferred evaluation job remains in OpenAPI"
    assert "cache_type" in text, "Answer contract must expose cache_type"
    return len(refs), len(operation_ids)


def validate_required_files() -> None:
    """确认规格定义的关键契约、配置和部署文件均存在。"""
    missing = [relative for relative in sorted(REQUIRED_FILES) if not (ROOT / relative).is_file()]
    assert not missing, f"Missing required files: {missing}"


def table_block(sql: str, table: str) -> str:
    """从 Schema SQL 中截取指定 CREATE TABLE 块。"""
    match = re.search(
        rf"CREATE\s+TABLE\s+{re.escape(table)}\s*\((.*?)\n\);",
        sql,
        flags=re.IGNORECASE | re.DOTALL,
    )
    assert match, f"Cannot locate table block: {table}"
    return match.group(1)


def validate_schema(path: Path) -> int:
    """校验必需表、范围字段、复合外键、约束和索引。"""
    sql = path.read_text(encoding="utf-8")
    assert "PostgreSQL 18" in sql
    assert "CREATE SCHEMA IF NOT EXISTS rag_app" in sql
    assert "JSONB" in sql
    assert "ENGINE=InnoDB" not in sql
    tables = set(re.findall(r"CREATE\s+TABLE\s+([a-zA-Z0-9_]+)", sql, flags=re.IGNORECASE))
    missing_tables = REQUIRED_TABLES - tables
    assert not missing_tables, f"Missing required tables: {sorted(missing_tables)}"
    assert not any(name.startswith("evaluation_") for name in tables)
    assert "EVALUATE" not in sql

    for table in SCOPED_TABLES:
        block = table_block(sql, table)
        assert "security_scope_id" in block, f"{table} is not scope-isolated"
        assert "NOT NULL" in block.split("security_scope_id", 1)[1].splitlines()[0]

    assert "PRIMARY KEY (security_scope_id, idempotency_key" in sql
    assert "FOREIGN KEY (security_scope_id, request_id)" in sql
    assert "FOREIGN KEY (security_scope_id, knowledge_base_id)" in sql
    return len(tables)


def validate_application_config() -> None:
    """校验示例应用配置的必需段、模型维度、缓存版本和安全开关。"""
    config = yaml.safe_load((ROOT / "configs/application.example.yaml").read_text(encoding="utf-8"))
    identity = config["identity"]
    assert identity["mode"] == "direct_user"
    assert identity["tenant_header_name"] == "X-Tenant-ID"
    assert identity["user_header_name"] == "X-User-ID"
    assert identity["max_tenant_id_length"] == 128
    semantic = config["cache"]["semantic"]
    assert semantic["require_same_security_scope"] is True
    assert semantic["revalidate_citations"] is True
    assert semantic["vector_data_type"] == "FLOAT32"
    # 两个模型或修订即使返回相同维度也不能共享语义缓存；门禁固定检查 v2 名称和
    # 前缀，防止只改版本数字却仍把新向量写进旧 Key 空间。
    assert semantic["index_schema_version"] == 2
    assert semantic["index_name"] == "rag_semantic_cache_v2"
    assert semantic["key_prefix"] == "rag-cache:semantic:v2:"
    uploads = config["uploads"]
    assert uploads["staging_backend"] == "local_volume"
    assert uploads["staging_dir"] == "/var/lib/rag/uploads"
    assert uploads["uploading_suffix"] == ".uploading"
    assert uploads["ready_suffix"] == ".ready"
    profiles = config["profiles"]
    assert profiles["runtime_source"] == "database"
    assert profiles["conflict_policy"] == "fail"
    assert profiles["required_refs"]
    assert "application.example.yaml" not in profiles["seed_files"]


def validate_deployment() -> None:
    """校验运行时/存储 Compose 的服务边界、网络暴露和持久卷。"""
    runtime = yaml.safe_load(
        (ROOT / "deploy/docker-compose.full.yml").read_text(encoding="utf-8")
    )
    storage = yaml.safe_load(
        (ROOT / "deploy/docker-compose.storage.yml").read_text(encoding="utf-8")
    )
    services = runtime["services"]
    storage_services = storage["services"]
    assert set(services) == REQUIRED_RUNTIME_COMPOSE_SERVICES
    assert set(storage_services) == REQUIRED_STORAGE_COMPOSE_SERVICES
    assert not (set(services) & set(storage_services))

    exposed = {name for name, service in services.items() if service.get("ports")}
    assert exposed == {"rag-gateway", "ragflow-server"}
    assert not any(service.get("ports") for service in storage_services.values())
    assert services["ragflow-server"]["environment"]["DB_TYPE"] == "postgres"
    assert services["ragflow-server"]["platform"] == "${RAGFLOW_PLATFORM:-linux/amd64}"
    assert services["rag-gateway"]["depends_on"]["ragflow-server"]["condition"] == "service_healthy"
    assert services["rag-gateway"]["depends_on"]["rag-app-bootstrap"]["condition"] == (
        "service_completed_successfully"
    )
    assert services["rag-app-bootstrap"]["depends_on"]["rag-app-schema-check"]["condition"] == (
        "service_completed_successfully"
    )
    assert services["rag-app-schema-check"]["command"] == [
        "python",
        "-m",
        "rag_service.cli",
        "schema",
        "check",
    ]
    assert services["ragflow-server"]["depends_on"]["rag-app-schema-check"]["condition"] == (
        "service_completed_successfully"
    )
    assert services["rag-gateway"].get("build"), "Full Compose must build the app image"
    assert "upload_staging_data" in runtime["volumes"]
    staging_mount = "upload_staging_data:/var/lib/rag/uploads"
    for name in ("rag-gateway", "rag-worker", "rag-scheduler"):
        assert staging_mount in services[name].get("volumes", []), (
            f"{name} must mount the shared upload staging volume"
        )
    ragflow_env = services["ragflow-server"]["environment"]
    app_env = services["rag-gateway"]["environment"]
    assert "APP_DATABASE_DSN" not in ragflow_env, "RAGFlow must not receive the app database DSN"
    assert "POSTGRES_PASSWORD" not in app_env, (
        "Gateway must not receive the RAGFlow database password"
    )

    env = (ROOT / "deploy/.env.full.example").read_text(encoding="utf-8")
    for name in (
        "RAG_STORAGE_MODE=local",
        "RAGFLOW_BASE_URL=http://ragflow-server:9380",
        "APP_DATABASE_DSN=postgresql+psycopg://",
        "RAGFLOW_PLATFORM=linux/amd64",
    ):
        assert name in env, f"Missing full deployment setting: {name}"
    assert "RAG_SERVICE_TOKEN" not in env

    service_conf = (ROOT / "deploy/ragflow/service_conf.postgres.yaml.template").read_text(
        encoding="utf-8"
    )
    assert "\npostgres:" in service_conf and "\nmysql:" not in service_conf
    assert "http_port: 9380" in service_conf


def validate_scope_documentation() -> None:
    """确认安全文档明确解释 security_scope_id 的来源与不可替代规则。"""
    api_doc = (ROOT / "docs/02-API接口说明.md").read_text(encoding="utf-8")
    security_doc = (ROOT / "docs/10-安全与威胁模型.md").read_text(encoding="utf-8")
    codex_doc = (ROOT / "docs/08-Codex实施指令.md").read_text(encoding="utf-8")
    assert "X-Tenant-ID" in api_doc and "X-User-ID" in api_doc
    assert "不是认证凭据" in api_doc
    assert "其他 Scope" in api_doc and "404" in api_doc
    assert "security_scope_id" in security_doc
    assert "RAG_SERVICE_TOKEN" not in api_doc
    assert "单个 Codex" in codex_doc
    assert "开发者 A" not in codex_doc and "开发者 B" not in codex_doc


def main() -> None:
    """运行全部规格检查并输出路径/操作/表数量摘要。"""
    validate_required_files()
    validate_application_config()
    validate_deployment()
    validate_scope_documentation()

    yaml_files = sorted(ROOT.rglob("*.yaml")) + sorted(ROOT.rglob("*.yml"))
    json_files = sorted(ROOT.rglob("*.json"))
    for path in yaml_files:
        yaml.safe_load(path.read_text(encoding="utf-8"))
    for path in json_files:
        json.loads(path.read_text(encoding="utf-8"))

    ref_count, operation_count = validate_openapi(ROOT / "openapi/rag-api-v1.yaml")
    table_count = validate_schema(ROOT / "database/schema.sql")
    print(
        f"ok yaml={len(yaml_files)} json={len(json_files)} "
        f"openapi_refs={ref_count} operations={operation_count} tables={table_count}"
    )


if __name__ == "__main__":
    main()
