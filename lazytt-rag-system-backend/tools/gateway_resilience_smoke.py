#!/usr/bin/env python3
"""通过暂停/恢复容器验证统一 Redis 与 RAGFlow 故障后的安全失败和恢复。"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from gateway_e2e_smoke import SmokeClient

COMPOSE_SERVICES = (
    "ragflow-server",
    "ragflow-task-executor",
    "rag-gateway",
    "rag-worker",
    "rag-scheduler",
)


def build_parser() -> argparse.ArgumentParser:
    """定义 Compose 拓扑、Direct Tenant/User、测试文件、超时和证据参数。"""
    parser = argparse.ArgumentParser(
        description="Exercise unified Redis failure and non-destructive service restart recovery."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument(
        "--tenant-id",
        default="gateway-resilience-tenant",
    )
    parser.add_argument(
        "--user-id",
        default="gateway-resilience-user",
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        default=Path("tests/fixtures/ragflow-smoke.txt"),
    )
    parser.add_argument(
        "--compose-env",
        type=Path,
        default=Path("deploy/local/.env.full"),
    )
    parser.add_argument(
        "--compose-file",
        type=Path,
        default=Path("compose.local.yml"),
    )
    parser.add_argument(
        "--storage-compose-file",
        type=Path,
        default=Path("deploy/local/docker-compose.storage.yml"),
    )
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--output", type=Path)
    return parser


def _compose(
    repo_root: Path,
    args: argparse.Namespace,
    *command: str,
) -> None:
    """运行固定 Docker Compose 命令，失败时只报告子命令和退出码。"""
    docker = shutil.which("docker")
    if docker is None:
        raise RuntimeError("docker executable was not found")
    compose_file = (
        args.storage_compose_file if "redis-stack" in command else args.compose_file
    )
    result = subprocess.run(  # noqa: S603 - fixed docker executable and validated arguments
        [
            docker,
            "compose",
            "--env-file",
            str((repo_root / args.compose_env).resolve()),
            "-f",
            str((repo_root / compose_file).resolve()),
            *command,
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=args.timeout_seconds,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"docker compose {' '.join(command)} failed with exit code {result.returncode}"
        )


def _wait_readiness(
    client: SmokeClient,
    expected: str,
    timeout_seconds: int,
) -> dict[str, Any]:
    """轮询就绪接口直到整体状态等于预期值。"""
    deadline = time.monotonic() + timeout_seconds
    last_status = "unreachable"
    while time.monotonic() < deadline:
        try:
            response = client._client.get("health/ready")
            last_status = f"http_{response.status_code}"
            if response.status_code == 200:
                value = response.json()["data"]
                last_status = str(value["status"])
                if last_status == expected:
                    return value
        except Exception:
            last_status = "unreachable"
        time.sleep(2)
    raise TimeoutError(f"readiness did not become {expected}; last status was {last_status}")


def _wait_unavailable(client: SmokeClient, timeout_seconds: int) -> None:
    """轮询直到就绪接口以稳定错误码报告核心依赖不可用。"""
    deadline = time.monotonic() + timeout_seconds
    last_status = "unreachable"
    while time.monotonic() < deadline:
        try:
            response = client._client.get("health/ready")
            last_status = f"http_{response.status_code}"
            if (
                response.status_code == 503
                and response.json().get("error", {}).get("code") == "RAG-COMMON-012"
            ):
                return
        except Exception:
            last_status = "unreachable"
        time.sleep(2)
    raise TimeoutError(f"readiness did not become unavailable; last status was {last_status}")


def run(args: argparse.Namespace) -> dict[str, Any]:
    """建立测试资源，逐项暂停/恢复服务并验证非破坏性恢复，最终尽力清理。"""
    repo_root = Path(__file__).resolve().parents[1]
    fixture = (repo_root / args.fixture).resolve()
    # 韧性验证只需要一个隔离身份，因此 A/B 客户端使用同一个 User ID；Tenant ID 仍
    # 必须单独传入，避免测试绕过生产代码中的双 Header 身份前置条件。
    client = SmokeClient(
        args.base_url,
        args.tenant_id,
        args.user_id,
        args.user_id,
        args.timeout_seconds,
    )
    run_id = uuid4().hex[:12]
    knowledge_base_id: str | None = None
    document_id: str | None = None
    document_deleted = False
    knowledge_base_deleted = False
    cache_started = True
    ragflow_started = True
    checks: dict[str, Any] = {}
    try:
        created = client.request_a(
            "POST",
            "v1/knowledge-bases",
            expected=201,
            idempotency_key=f"resilience-{run_id}-create-kb",
            json={
                "external_id": f"synthetic-resilience-{run_id}",
                "name": f"Synthetic Resilience {run_id}",
                "description": "Non-business restart and degradation fixture",
                "parser_profile": "office-default@1",
                "chunk_profile": "general-256@2",
                # 故障演练覆盖当前供应商无关候选链路。
                "embedding_profile": "embedding-openai-compatible@3",
                "retrieval_profile": "hybrid-default@1",
            },
        )["data"]
        knowledge_base_id = str(created["id"])
        with fixture.open("rb") as fixture_stream:
            uploaded = client.request_a(
                "POST",
                f"v1/knowledge-bases/{knowledge_base_id}/documents",
                expected=202,
                idempotency_key=f"resilience-{run_id}-upload",
                files={"file": (fixture.name, fixture_stream, "text/plain")},
                data={
                    "external_id": f"synthetic-resilience-document-{run_id}",
                    "metadata": json.dumps({"fixture": "gateway-resilience"}),
                    "auto_parse": "true",
                },
            )["data"]
        document_id = str(uploaded["document_id"])
        client.wait_job(str(uploaded["job_id"]))
        client.wait_document_ready(document_id)
        checks["fixture_ready"] = True

        answer_body = {
            "knowledge_base_ids": [knowledge_base_id],
            "query": "What does the lifecycle test verify?",
            "messages": [],
            "retrieval_profile": "hybrid-default@1",
            "generation_profile": "answer-openai-compatible@4",
            # API 契约只接受带地区的 BCP 47 语言值。韧性 Smoke 必须使用与真实
            # 用户端相同的 ``en-US``，否则会在进入缓存/故障演练前被 Schema 拒绝，
            # 造成“服务故障恢复失败”的假阳性诊断。
            "response_language": "en-US",
        }
        seeded = client.request_a(
            "POST",
            "v1/answers",
            expected=200,
            json=answer_body,
        )["data"]
        if seeded["outcome"] != "success":
            raise RuntimeError("cache seed answer did not complete successfully")
        checks["cache_seeded"] = True

        # 合并后 Redis Stack 同时承载缓存、Celery 与 RAGFlow 状态，已不再存在
        # “只停缓存实例、同步问答仍保持 Ready”的故障边界。停止统一实例后，
        # Ready 必须安全失败；恢复同一实例后再继续验证业务链路。
        _compose(repo_root, args, "stop", "redis-stack")
        cache_started = False
        _wait_unavailable(client, 90)
        checks["redis_stack_outage_fail_closed"] = "RAG-COMMON-012"

        _compose(repo_root, args, "start", "redis-stack")
        cache_started = True
        _wait_readiness(client, "ok", 120)

        _compose(repo_root, args, "stop", "ragflow-server")
        ragflow_started = False
        _wait_unavailable(client, 90)
        unavailable = client.request_a(
            "POST",
            "v1/answers",
            expected=503,
            json=answer_body,
        )
        if unavailable.get("error", {}).get("code") not in {"RAG-RF-001", "RAG-RF-003"}:
            raise RuntimeError("RAGFlow outage did not return a stable dependency error")
        checks["ragflow_outage_fail_closed"] = unavailable["error"]["code"]
        _compose(repo_root, args, "start", "ragflow-server")
        ragflow_started = True
        _compose(
            repo_root,
            args,
            "up",
            "-d",
            "--wait",
            "--wait-timeout",
            "300",
            "ragflow-server",
        )
        _wait_readiness(client, "ok", 120)

        _compose(repo_root, args, "restart", *COMPOSE_SERVICES)
        _compose(
            repo_root,
            args,
            "up",
            "-d",
            "--wait",
            "--wait-timeout",
            "300",
            *COMPOSE_SERVICES,
        )
        _wait_readiness(client, "ok", 120)
        checks["service_restart_recovered"] = list(COMPOSE_SERVICES)

        replay = client.request_a(
            "POST",
            "v1/answers",
            expected=200,
            json=answer_body,
        )["data"]
        if replay["outcome"] != "success" or replay["meta"]["cache_type"] != "exact":
            raise RuntimeError("exact cache did not persist through the service restart")
        checks["cache_persisted_after_restart"] = "exact"
        retrieval = client.request_a(
            "POST",
            "v1/retrieval",
            expected=200,
            json={
                "knowledge_base_ids": [knowledge_base_id],
                "document_ids": [],
                "query": answer_body["query"],
                "retrieval_profile": "hybrid-default@1",
                "options": {"page_size": 10},
            },
        )["data"]
        if not retrieval["candidates"]:
            raise RuntimeError("RAGFlow mapping or index was lost after restart")
        checks["ragflow_mapping_persisted"] = True

        deleted_document = client.request_a(
            "DELETE",
            f"v1/documents/{document_id}",
            expected=202,
            idempotency_key=f"resilience-{run_id}-delete-document",
        )["data"]
        client.wait_job(str(deleted_document["job_id"]))
        document_deleted = True
        deleted_kb = client.request_a(
            "DELETE",
            f"v1/knowledge-bases/{knowledge_base_id}?force=true",
            expected=202,
            idempotency_key=f"resilience-{run_id}-delete-kb",
        )["data"]
        client.wait_job(str(deleted_kb["job_id"]))
        knowledge_base_deleted = True
        checks["fixture_cleanup"] = "api_resources_deleted"
        return {"status": "passed", "run_id": run_id, "checks": checks}
    finally:
        if not cache_started:
            _compose(repo_root, args, "start", "redis-stack")
        if not ragflow_started:
            _compose(repo_root, args, "start", "ragflow-server")
            _compose(
                repo_root,
                args,
                "up",
                "-d",
                "--wait",
                "--wait-timeout",
                "300",
                "ragflow-server",
            )
        if not cache_started or not ragflow_started:
            _wait_readiness(client, "ok", 120)
        try:
            if document_id is not None and not document_deleted:
                response = client._client.delete(
                    f"v1/documents/{document_id}",
                    headers={
                        **client._headers_a,
                        "Idempotency-Key": f"resilience-{run_id}-finally-delete-document",
                    },
                )
                if response.status_code == 202:
                    client.wait_job(str(response.json()["data"]["job_id"]))
        finally:
            if knowledge_base_id is not None and not knowledge_base_deleted:
                response = client._client.delete(
                    f"v1/knowledge-bases/{knowledge_base_id}?force=true",
                    headers={
                        **client._headers_a,
                        "Idempotency-Key": f"resilience-{run_id}-finally-delete-kb",
                    },
                )
                if response.status_code == 202:
                    client.wait_job(str(response.json()["data"]["job_id"]))
            client.close()


def main() -> None:
    """执行韧性 Smoke 并输出/保存 JSON 证据。"""
    args = build_parser().parse_args()
    result = run(args)
    encoded = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output is not None:
        output = args.output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded + "\n", encoding="utf-8")
        print(f"gateway resilience smoke passed; report written to {output}")
    else:
        print(encoded)


if __name__ == "__main__":
    main()
