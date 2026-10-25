#!/usr/bin/env python3
"""对 ``docker compose config --format json`` 的渲染结果执行静态安全与拓扑断言。"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

RUNTIME_SERVICES = frozenset(
    {
        "ragflow-smoke-embedding",
        "ragflow-server",
        "ragflow-task-executor",
        "rag-app-schema-check",
        "rag-app-bootstrap",
        "rag-gateway",
        "rag-worker",
        "rag-scheduler",
    }
)
RUNTIME_VOLUMES = frozenset(
    {
        "upload_staging_data",
        "ragflow_logs",
    }
)
STORAGE_SERVICES = frozenset(
    {
        "postgres",
        "redis-stack",
        "elasticsearch",
        "minio",
    }
)
STORAGE_VOLUMES = frozenset(
    {
        "postgres_data",
        "redis_stack_data",
        "elasticsearch_data",
        "minio_data",
    }
)
RAGFLOW_AMD64_SERVICES = frozenset({"ragflow-server", "ragflow-task-executor"})
LOCAL_PUBLISHED_SERVICES = frozenset({"rag-gateway", "ragflow-server"})
EXPECTED_PROJECT_NAMES = {
    "runtime": "lazytt-rag-runtime",
    "storage": "lazytt-rag-storage",
}
# 这个全网卡地址只对 ``rag-gateway`` 有效；``_validate_published_host`` 会对
# RAGFlow 和其他服务继续强制回环绑定，所以这里的 S104 是经审查的例外。
GATEWAY_HOST_IPS = frozenset({"127.0.0.1", "0.0.0.0"})  # noqa: S104


def _mapping(value: Any, name: str) -> dict[str, Any]:
    """要求 Compose 节点是对象，并在错误中指出节点名。"""
    if not isinstance(value, dict):
        raise ValueError(f"Compose {name} must be an object")
    return value


def _validate_published_host(service_name: str, host_ip: object) -> str:
    """检查一个宿主机端口的绑定范围，避免把内部管理面意外暴露。

    Gateway 是唯一允许用户显式设为 ``0.0.0.0`` 的业务入口，以便同一
    受信局域网中的其他主机调用 API。RAGFlow UI/API 以及其他服务仍必须
    绑定 ``127.0.0.1``，因此开启局域网 Gateway 不会同时暴露 RAGFlow 管理界面。
    """
    normalized = str(host_ip)
    if service_name == "rag-gateway":
        if normalized not in GATEWAY_HOST_IPS:
            raise ValueError("Gateway host IP must be loopback or explicit LAN mode")
        return normalized
    if normalized != "127.0.0.1":
        raise ValueError(f"service {service_name} has a non-loopback published port")
    return normalized


def validate(document: dict[str, Any], *, topology: str = "runtime") -> dict[str, object]:
    """按独立运行时或存储拓扑验证 Project、共享网络、服务和固定版本。

    ``topology`` 决定当前文档代表无状态运行层还是有状态存储层。除了原有的服务、
    端口和命名卷检查，这里还要求两层使用不同 Compose Project 名，并且都把通信
    网络声明为外部网络。这样 Docker Desktop 才会显示两个项目，同时任一 Project
    停止或重建都不会删除另一层仍在使用的共享网络。
    """
    expected_project_name = EXPECTED_PROJECT_NAMES[topology]
    if document.get("name") != expected_project_name:
        raise ValueError(
            f"Compose project name must be {expected_project_name} for {topology} topology"
        )

    networks = _mapping(document.get("networks"), "networks")
    if frozenset(networks) != {"ragflow-backplane"}:
        raise ValueError("Compose must declare exactly one ragflow-backplane network")
    shared_network = _mapping(networks["ragflow-backplane"], "network ragflow-backplane")
    if shared_network.get("external") is not True:
        raise ValueError("ragflow-backplane must be an external shared network")
    shared_network_name = shared_network.get("name")
    if not isinstance(shared_network_name, str) or not shared_network_name.strip():
        raise ValueError("ragflow-backplane must have a non-empty explicit name")

    services = _mapping(document.get("services"), "services")
    volumes = _mapping(document.get("volumes"), "volumes")
    expected_services = RUNTIME_SERVICES if topology == "runtime" else STORAGE_SERVICES
    # MinerU 是根 Compose 内的可选服务，默认不启用；启用时仍检查隔离和配置边界。
    if topology == "runtime" and "mineru-cloud-adapter" in services:
        expected_services = expected_services | {"mineru-cloud-adapter"}
        mineru = _mapping(services["mineru-cloud-adapter"], "MinerU adapter")
        if mineru.get("profiles") != ["mineru"]:
            raise ValueError("MinerU adapter must remain optional")
        environment = _mapping(mineru.get("environment", {}), "MinerU environment")
        if set(environment) - {
            "MINERU_CLOUD_API_TOKEN", "MINERU_CLOUD_MODEL", "MINERU_CLOUD_MAX_CONCURRENCY",
            "MINERU_CLOUD_RESULT_CONCURRENCY"
        }:
            raise ValueError("MinerU adapter must not inherit unrelated credentials")
    expected_volumes = RUNTIME_VOLUMES if topology == "runtime" else STORAGE_VOLUMES
    if frozenset(services) != expected_services:
        raise ValueError(f"Compose service set differs from the reviewed {topology} topology")
    if frozenset(volumes) != expected_volumes:
        raise ValueError("Compose persistent volume set differs from the reviewed topology")

    if topology == "storage":
        for name, raw in services.items():
            service = _mapping(raw, f"service {name}")
            if service.get("ports"):
                raise ValueError(f"storage service {name} must not publish a host port")
            if not service.get("healthcheck"):
                raise ValueError(f"storage service {name} must define a healthcheck")

        # PostgreSQL 物理合并后仍必须保留两个数据库和两套业务凭据。此处只验证
        # Compose 是否把所需变量传入初始化容器，不读取或比较任何真实密码。
        postgres = _mapping(services["postgres"], "service postgres")
        postgres_environment = _mapping(
            postgres.get("environment"), "service postgres environment"
        )
        required_postgres_fields = {
            # 宿主机使用 ``POSTGRES_ADMIN_*`` 命名，Compose 注入官方镜像后必须
            # 映射为它实际识别的 ``POSTGRES_USER`` / ``POSTGRES_PASSWORD``。
            "POSTGRES_USER",
            "POSTGRES_PASSWORD",
            "APP_POSTGRES_DATABASE",
            "APP_POSTGRES_USER",
            "APP_POSTGRES_PASSWORD",
            "RAGFLOW_POSTGRES_DATABASE",
            "RAGFLOW_POSTGRES_USER",
            "RAGFLOW_POSTGRES_PASSWORD",
        }
        if not required_postgres_fields.issubset(postgres_environment):
            raise ValueError("shared PostgreSQL is missing dual-database initialization fields")

        # 统一 Redis 必须使用含 RediSearch 的固定 Redis Stack 版本，并且在
        # 一个物理卷中开启 AOF。noeviction 防止缓存压力静默驱逐 Celery
        # 任务或 RAGFlow 状态；容量不足应通过监控与扩容处理。
        redis_stack = _mapping(services["redis-stack"], "service redis-stack")
        if redis_stack.get("image") != "redis/redis-stack-server:7.4.0-v8":
            raise ValueError("unified Redis must use the pinned Redis Stack image")
        redis_environment = _mapping(
            redis_stack.get("environment"), "service redis-stack environment"
        )
        redis_args = str(redis_environment.get("REDIS_ARGS", ""))
        for required_argument in ("--appendonly yes", "--maxmemory-policy noeviction"):
            if required_argument not in redis_args:
                raise ValueError(f"unified Redis is missing {required_argument}")
        return {
            "status": "passed",
            "topology": topology,
            "project_name": expected_project_name,
            "shared_network": shared_network_name,
            "service_count": len(services),
            "volume_count": len(volumes),
            "published_services": [],
        }

    amd64 = {
        name
        for name, raw in services.items()
        if isinstance(raw, dict) and raw.get("platform") == "linux/amd64"
    }
    if amd64 != RAGFLOW_AMD64_SERVICES:
        raise ValueError("linux/amd64 must be scoped to the two RAGFlow services")

    published: dict[str, list[int]] = {}
    published_hosts: dict[str, set[str]] = {}
    for name, raw in services.items():
        service = _mapping(raw, f"service {name}")
        ports = service.get("ports", [])
        if not isinstance(ports, list):
            raise ValueError(f"service {name} ports must be an array")
        if ports and name not in LOCAL_PUBLISHED_SERVICES:
            raise ValueError(f"unexpected host port on service {name}")
        for port in ports:
            definition = _mapping(port, f"service {name} port")
            host_ip = _validate_published_host(name, definition.get("host_ip"))
            published_hosts.setdefault(name, set()).add(host_ip)
            published.setdefault(name, []).append(int(definition["published"]))

    if not published.get("rag-gateway"):
        raise ValueError("Gateway host port is missing")
    if not published.get("ragflow-server"):
        raise ValueError("local-development RAGFlow loopback ports are missing")

    staging_mounts: dict[str, str] = {}
    for name in ("rag-gateway", "rag-worker", "rag-scheduler"):
        service = _mapping(services[name], f"service {name}")
        mounts = service.get("volumes", [])
        for raw_mount in mounts if isinstance(mounts, list) else []:
            mount = _mapping(raw_mount, f"service {name} mount")
            if mount.get("target") == "/var/lib/rag/uploads":
                staging_mounts[name] = str(mount.get("source"))
    if set(staging_mounts.values()) != {"upload_staging_data"} or len(staging_mounts) != 3:
        raise ValueError("Gateway, Worker and Scheduler must share upload_staging_data")

    smoke = _mapping(services["ragflow-smoke-embedding"], "smoke embedding service")
    if smoke.get("profiles") != ["smoke"]:
        raise ValueError("contract-only embedding fixture must remain in the smoke profile")

    schema_check = _mapping(services["rag-app-schema-check"], "schema check service")
    if schema_check.get("command") != [
        "python",
        "-m",
        "rag_service.cli",
        "schema",
        "check",
    ]:
        raise ValueError("database startup gate must be the read-only schema checker")
    if "alembic" in " ".join(str(part) for part in schema_check.get("command", [])):
        raise ValueError("runtime Compose must not execute Alembic migrations")

    ragflow_images = {
        str(_mapping(services[name], f"service {name}").get("image"))
        for name in RAGFLOW_AMD64_SERVICES
    }
    if ragflow_images != {"infiniflow/ragflow:v0.26.4"}:
        raise ValueError("RAGFlow services must use the pinned official v0.26.4 image")

    return {
        "status": "passed",
        "topology": topology,
        "project_name": expected_project_name,
        "shared_network": shared_network_name,
        "service_count": len(services),
        "volume_count": len(volumes),
        "amd64_services": sorted(amd64),
        "published_services": sorted(published),
        "published_hosts": {
            name: sorted(hosts) for name, hosts in sorted(published_hosts.items())
        },
    }


def main() -> None:
    """从标准输入读取 Compose JSON，验证后输出机器可读摘要。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--topology", choices=("runtime", "storage"), default="runtime")
    args = parser.parse_args()
    raw = json.load(sys.stdin)
    document = _mapping(raw, "root")
    print(
        json.dumps(
            validate(document, topology=args.topology),
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
