"""Compose 网络暴露测试：只允许 Gateway 在显式配置后对局域网开放。"""

from __future__ import annotations

import pytest
from tools.validate_compose import _validate_published_host, validate

LAN_BIND_ADDRESS = "0.0.0.0"  # noqa: S104 - 测试用户显式开启的局域网模式。


def test_gateway_accepts_loopback_and_explicit_lan_mode() -> None:
    """业务 Gateway 可以安全默认回环，也可由用户显式开启局域网模式。"""
    assert _validate_published_host("rag-gateway", "127.0.0.1") == "127.0.0.1"
    assert _validate_published_host("rag-gateway", LAN_BIND_ADDRESS) == LAN_BIND_ADDRESS


def test_ragflow_management_ports_remain_loopback_only() -> None:
    """即使 Gateway 开放到 Wi-Fi，RAGFlow UI/API 也不得随之暴露。"""
    assert _validate_published_host("ragflow-server", "127.0.0.1") == "127.0.0.1"
    with pytest.raises(ValueError, match="non-loopback"):
        _validate_published_host("ragflow-server", LAN_BIND_ADDRESS)


def test_gateway_rejects_unreviewed_specific_host_address() -> None:
    """限制为两种明确模式，让拼写错误或未评审公网绑定在预检阶段失败。"""
    with pytest.raises(ValueError, match="loopback or explicit LAN mode"):
        _validate_published_host("rag-gateway", "192.0.2.10")


def _minimal_compose(*, project_name: str, external_network: bool) -> dict[str, object]:
    """构造只用于前置 Project/网络断言的最小 Compose 文档。

    服务和卷故意留空：本组测试只验证错误会在拓扑集合检查之前被发现，防止未来
    两个 Compose 又误用同一 Project，或让某一层重新取得共享网络所有权。
    """
    return {
        "name": project_name,
        "networks": {
            "ragflow-backplane": {
                "name": "lazytt-ragflow-poc_ragflow-backplane",
                "external": external_network,
            }
        },
        "services": {},
        "volumes": {},
    }


def test_runtime_rejects_storage_project_name() -> None:
    """运行时不得复用存储 Project，否则 Docker Desktop 会再次合并两层。"""
    document = _minimal_compose(
        project_name="lazytt-rag-storage",
        external_network=True,
    )

    with pytest.raises(ValueError, match="lazytt-rag-runtime"):
        validate(document, topology="runtime")


def test_storage_rejects_project_owned_shared_network() -> None:
    """存储层不得拥有共享网络，避免停止存储时影响仍运行的业务容器。"""
    document = _minimal_compose(
        project_name="lazytt-rag-storage",
        external_network=False,
    )

    with pytest.raises(ValueError, match="external shared network"):
        validate(document, topology="storage")


def test_storage_topology_requires_one_pinned_redis_stack() -> None:
    """存储拓扑只允许一个 Redis Stack，且必须开启 AOF 和 noeviction。"""
    services: dict[str, object] = {
        name: {"healthcheck": {"test": ["CMD", "true"]}}
        for name in ("elasticsearch", "minio")
    }
    services["postgres"] = {
        "healthcheck": {"test": ["CMD", "true"]},
        "environment": {
            name: "fixture"
            for name in (
                "POSTGRES_USER",
                "POSTGRES_PASSWORD",
                "APP_POSTGRES_DATABASE",
                "APP_POSTGRES_USER",
                "APP_POSTGRES_PASSWORD",
                "RAGFLOW_POSTGRES_DATABASE",
                "RAGFLOW_POSTGRES_USER",
                "RAGFLOW_POSTGRES_PASSWORD",
            )
        },
    }
    services["redis-stack"] = {
        "image": "redis/redis-stack-server:7.4.0-v8",
        "healthcheck": {"test": ["CMD", "true"]},
        "environment": {
            "REDIS_ARGS": "--appendonly yes --maxmemory-policy noeviction"
        },
    }
    document = {
        "name": "lazytt-rag-storage",
        "networks": {
            "ragflow-backplane": {
                "name": "lazytt-ragflow-poc_ragflow-backplane",
                "external": True,
            }
        },
        "services": services,
        "volumes": {
            name: {}
            for name in (
                "postgres_data",
                "redis_stack_data",
                "elasticsearch_data",
                "minio_data",
            )
        },
    }

    result = validate(document, topology="storage")

    assert result["status"] == "passed"
    assert result["service_count"] == 4
    assert result["volume_count"] == 4
