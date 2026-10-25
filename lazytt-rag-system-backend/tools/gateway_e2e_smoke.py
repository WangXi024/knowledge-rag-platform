#!/usr/bin/env python3
"""对运行中的 Gateway 执行全流程验收：隔离、幂等、知识库、文档、检索、问答与反馈。"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

TERMINAL_JOB_STATES = {"SUCCEEDED", "FAILED", "CANCELLED"}


def build_parser() -> argparse.ArgumentParser:
    """定义 Gateway 地址、Tenant、两个隔离 User ID、测试文件和轮询超时。"""
    parser = argparse.ArgumentParser(
        description="Run a non-business Gateway/RAGFlow/model lifecycle smoke."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument(
        "--tenant-id",
        default="gateway-smoke-tenant",
    )
    parser.add_argument(
        "--user-a",
        default="gateway-smoke-user-a",
    )
    parser.add_argument(
        "--user-b",
        default="gateway-smoke-user-b",
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        default=Path("tests/fixtures/ragflow-smoke.txt"),
    )
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--output", type=Path)
    return parser


class SmokeClient:
    """封装两个租户身份的 HTTP 调用、轮询和 SSE 解析，并统一失败诊断。"""
    def __init__(
        self,
        base_url: str,
        tenant_id: str,
        user_a: str,
        user_b: str,
        timeout_seconds: int,
    ) -> None:
        """创建同一 Tenant 内带独立 User ID 的 A/B 两个同步客户端。"""
        self._client = httpx.Client(
            base_url=base_url.rstrip("/") + "/",
            timeout=httpx.Timeout(180, connect=10),
            trust_env=False,
        )
        self._headers_a = {"X-Tenant-ID": tenant_id, "X-User-ID": user_a}
        self._headers_b = {"X-Tenant-ID": tenant_id, "X-User-ID": user_b}
        self._deadline_seconds = timeout_seconds

    def close(self) -> None:
        """关闭两个客户端连接池。"""
        self._client.close()

    def request_a(
        self,
        method: str,
        path: str,
        *,
        expected: int,
        idempotency_key: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """以 Scope A 发请求，要求预期状态并返回 JSON Object。"""
        headers = dict(self._headers_a)
        if idempotency_key is not None:
            headers["Idempotency-Key"] = idempotency_key
        response = self._client.request(method, path, headers=headers, **kwargs)
        if response.status_code != expected:
            raise RuntimeError(
                f"{method} {path} expected {expected}, got {response.status_code}: "
                f"{_safe_error(response)}"
            )
        value = response.json()
        if not isinstance(value, dict):
            raise RuntimeError(f"{method} {path} returned a non-object response")
        return value

    def request_b_status(self, method: str, path: str, **kwargs: Any) -> int:
        """以 Scope B 调用同一资源，只返回状态码以验证跨租户不可见。"""
        return self._client.request(
            method,
            path,
            headers=self._headers_b,
            **kwargs,
        ).status_code

    def request_a_status(self, method: str, path: str) -> int:
        """以 Scope A 调用并只返回状态码。"""
        return self._client.request(
            method,
            path,
            headers=self._headers_a,
        ).status_code

    def wait_job(self, job_id: str) -> dict[str, Any]:
        """轮询 Job 直至成功/失败/取消或超时，失败时包含安全错误摘要。"""
        deadline = time.monotonic() + self._deadline_seconds
        while time.monotonic() < deadline:
            value = self.request_a("GET", f"v1/jobs/{job_id}", expected=200)["data"]
            if value["status"] in TERMINAL_JOB_STATES:
                if value["status"] != "SUCCEEDED":
                    error = value.get("error") or {}
                    raise RuntimeError(
                        f"job {job_id} ended as {value['status']}: {error.get('code', 'unknown')}"
                    )
                return value
            time.sleep(1)
        raise TimeoutError(f"job {job_id} did not finish before the smoke deadline")

    def wait_document_ready(self, document_id: str) -> dict[str, Any]:
        """轮询文档直至 READY 或明确失败/超时。"""
        deadline = time.monotonic() + self._deadline_seconds
        while time.monotonic() < deadline:
            value = self.request_a(
                "GET",
                f"v1/documents/{document_id}",
                expected=200,
            )["data"]
            if value["status"] == "READY":
                return value
            if value["status"] in {"FAILED", "DELETE_FAILED", "DELETED"}:
                error = value.get("last_error") or {}
                raise RuntimeError(
                    f"document ended as {value['status']}: {error.get('code', 'unknown')}"
                )
            time.sleep(2)
        raise TimeoutError("document did not become READY before the smoke deadline")

    def stream_answer(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        """消费 SSE 问答流，校验帧结构并返回事件数据列表。"""
        events: list[dict[str, Any]] = []
        with self._client.stream(
            "POST",
            "v1/answers/stream",
            headers=self._headers_a,
            json=body,
        ) as response:
            if response.status_code != 200:
                response.read()
                raise RuntimeError(
                    f"SSE expected 200, got {response.status_code}: {_safe_error(response)}"
                )
            current_event = ""
            for line in response.iter_lines():
                if line.startswith("event: "):
                    current_event = line.removeprefix("event: ")
                elif line.startswith("data: "):
                    data = json.loads(line.removeprefix("data: "))
                    events.append({"event": current_event, "data": data})
        return events


def _safe_error(response: httpx.Response) -> str:
    """截断并脱敏失败响应，避免 Smoke 日志意外输出大内容或凭据。"""
    try:
        value = response.json()
    except ValueError:
        return "non_json_response"
    if not isinstance(value, dict):
        return "non_object_response"
    error = value.get("error")
    if not isinstance(error, dict):
        return "unknown_error"
    return str(error.get("code", "unknown_error"))


def run(args: argparse.Namespace) -> dict[str, Any]:
    """按依赖顺序执行端到端场景，并在 finally 中尽力清理创建资源。"""
    run_id = uuid4().hex[:12]
    fixture = args.fixture.resolve()
    client = SmokeClient(
        args.base_url,
        args.tenant_id,
        args.user_a,
        args.user_b,
        args.timeout_seconds,
    )
    knowledge_base_id: str | None = None
    document_id: str | None = None
    document_deleted = False
    knowledge_base_deleted = False
    checks: dict[str, Any] = {}
    document_external_id = f"synthetic-document-{run_id}"
    try:
        health = client._client.get("health/ready")
        health.raise_for_status()
        checks["readiness"] = health.json()["data"]["status"]
        capabilities = client.request_a("GET", "v1/capabilities", expected=200)["data"]
        checks["rerank_enabled"] = capabilities["features"]["rerank"]
        checks["rrf_enabled"] = capabilities["features"]["retrieval_vector_bm25_rrf"]
        # RRF 是无论外部 Rerank 是否启用都必须存在的基础排序；Rerank 能力则由当前
        # 部署配置决定，后续会按该值分别验证真实分数或明确的关闭降级 Warning。
        if not checks["rrf_enabled"]:
            raise RuntimeError("local retrieval capability does not advertise RRF")

        created = client.request_a(
            "POST",
            "v1/knowledge-bases",
            expected=201,
            idempotency_key=f"smoke-{run_id}-create-kb",
            json={
                "external_id": f"synthetic-smoke-{run_id}",
                "name": f"Synthetic Smoke {run_id}",
                "description": "Non-business lifecycle fixture",
                "parser_profile": "office-default@1",
                "chunk_profile": "general-256@2",
                # 新建 Smoke 数据集使用当前供应商无关 Embedding Profile。
                "embedding_profile": "embedding-openai-compatible@3",
                "retrieval_profile": "hybrid-default@1",
            },
        )["data"]
        knowledge_base_id = str(created["id"])
        if created["status"] != "READY":
            raise RuntimeError("knowledge base did not become READY")
        checks["knowledge_base_created"] = True
        checks["cross_scope_kb_status"] = client.request_b_status(
            "GET",
            f"v1/knowledge-bases/{knowledge_base_id}",
        )
        if checks["cross_scope_kb_status"] != 404:
            raise RuntimeError("cross-scope knowledge base lookup did not return 404")

        with fixture.open("rb") as fixture_stream:
            uploaded = client.request_a(
                "POST",
                f"v1/knowledge-bases/{knowledge_base_id}/documents",
                expected=202,
                idempotency_key=f"smoke-{run_id}-upload",
                files={"file": (fixture.name, fixture_stream, "text/plain")},
                data={
                    "external_id": document_external_id,
                    "metadata": json.dumps({"fixture": "gateway-e2e"}),
                    "auto_parse": "true",
                },
            )["data"]
        document_id = str(uploaded["document_id"])
        with fixture.open("rb") as fixture_stream:
            duplicate = client.request_a(
                "POST",
                f"v1/knowledge-bases/{knowledge_base_id}/documents",
                expected=409,
                idempotency_key=f"smoke-{run_id}-duplicate-active-document",
                files={"file": (fixture.name, fixture_stream, "text/plain")},
                data={
                    "external_id": document_external_id,
                    "metadata": json.dumps({"fixture": "gateway-e2e-duplicate"}),
                    "auto_parse": "false",
                },
            )
        if (duplicate.get("error") or {}).get("code") != "RAG-DOC-009":
            raise RuntimeError("active duplicate external ID did not return RAG-DOC-009")
        checks["active_external_id_conflict"] = "RAG-DOC-009"
        client.wait_job(str(uploaded["job_id"]))
        document = client.wait_document_ready(document_id)
        checks["document_ready"] = True
        checks["document_chunk_count"] = int(document["chunk_count"])
        if checks["document_chunk_count"] < 1:
            raise RuntimeError("parsed fixture did not produce chunks")

        chunks = client.request_a(
            "GET",
            f"v1/documents/{document_id}/chunks",
            expected=200,
        )["data"]
        if not chunks:
            raise RuntimeError("chunk listing is empty")
        checks["chunks_listed"] = len(chunks)

        question = "What does the lifecycle test verify?"
        answer_body = {
            "knowledge_base_ids": [knowledge_base_id],
            "query": question,
            "messages": [],
            "retrieval_profile": "hybrid-default@1",
            "generation_profile": "answer-openai-compatible@4",
            "response_language": "en-US",
        }
        retrieval = client.request_a(
            "POST",
            "v1/retrieval",
            expected=200,
            json={
                "knowledge_base_ids": [knowledge_base_id],
                "document_ids": [],
                "query": question,
                "retrieval_profile": "hybrid-default@1",
                "options": {
                    "page_size": 10,
                    "include_content": True,
                    "include_positions": True,
                },
            },
        )["data"]
        if not retrieval["candidates"]:
            raise RuntimeError("hybrid retrieval returned no candidates")
        if checks["rerank_enabled"]:
            rerank_scores = [
                candidate.get("scores", {}).get("rerank_score")
                for candidate in retrieval["candidates"]
            ]
            if any(score is None for score in rerank_scores):
                raise RuntimeError("enabled rerank did not return a score for every candidate")
            if any(
                "rerank" in warning and "fallback" in warning
                for warning in retrieval["warnings"]
            ):
                raise RuntimeError("enabled rerank unexpectedly fell back to RRF during smoke")
            checks["rerank_scores"] = rerank_scores
        elif "rerank_disabled_vector_bm25_rrf_fallback" not in retrieval["warnings"]:
            raise RuntimeError("disabled rerank fallback was not reported")
        checks["retrieval_candidates"] = len(retrieval["candidates"])

        positive_metadata = client.request_a(
            "POST",
            "v1/retrieval",
            expected=200,
            json={
                "knowledge_base_ids": [knowledge_base_id],
                "document_ids": [],
                "query": question,
                "metadata_filter": {
                    "logic": "and",
                    "conditions": [
                        {"field": "fixture", "operator": "eq", "value": "gateway-e2e"}
                    ],
                },
                "retrieval_profile": "hybrid-default@1",
                "options": {
                    "page_size": 10,
                    "include_content": True,
                    "include_positions": True,
                },
            },
        )["data"]
        if not positive_metadata["candidates"]:
            raise RuntimeError("positive metadata filter returned no candidates")
        negative_metadata = client.request_a(
            "POST",
            "v1/retrieval",
            expected=200,
            json={
                "knowledge_base_ids": [knowledge_base_id],
                "document_ids": [],
                "query": question,
                "metadata_filter": {
                    "logic": "and",
                    "conditions": [
                        {"field": "fixture", "operator": "eq", "value": "not-present"}
                    ],
                },
                "retrieval_profile": "hybrid-default@1",
                "options": {
                    "page_size": 10,
                    "include_content": True,
                    "include_positions": True,
                },
            },
        )["data"]
        if negative_metadata["candidates"] or negative_metadata["total"] != 0:
            raise RuntimeError("negative metadata filter did not return an empty result")
        checks["metadata_filter"] = "positive_and_negative"

        first_response = client.request_a(
            "POST",
            "v1/answers",
            expected=200,
            json=answer_body,
        )
        first = first_response["data"]
        if first["outcome"] != "success" or not first["citations"]:
            raise RuntimeError("non-streaming answer did not return verified evidence")
        if first["meta"]["cache_type"] != "none":
            raise RuntimeError("first answer unexpectedly hit cache")
        first_request_id = str(first_response["request_id"])
        checks["non_stream_answer"] = first["outcome"]

        exact = client.request_a(
            "POST",
            "v1/answers",
            expected=200,
            json=answer_body,
        )["data"]
        if exact["meta"]["cache_type"] != "exact":
            raise RuntimeError("identical answer request did not hit exact cache")
        checks["exact_cache"] = "exact"

        semantic_type = "none"
        for similar_query in (
            "What exactly does the lifecycle test verify?",
            "What does this lifecycle test verify?",
            "Please explain what the lifecycle test verifies.",
        ):
            semantic_body = {**answer_body, "query": similar_query}
            semantic = client.request_a(
                "POST",
                "v1/answers",
                expected=200,
                json=semantic_body,
            )["data"]
            semantic_type = str(semantic["meta"]["cache_type"])
            if semantic_type == "semantic":
                break
        if semantic_type != "semantic":
            raise RuntimeError("similar question did not hit semantic cache")
        checks["semantic_cache"] = semantic_type

        cross_scope_answer = client.request_b_status(
            "POST",
            "v1/answers",
            json=answer_body,
        )
        checks["cross_scope_answer_status"] = cross_scope_answer
        if cross_scope_answer != 404:
            raise RuntimeError("cross-scope answer request did not return 404")

        request_record = client.request_a(
            "GET",
            f"v1/requests/{first_request_id}",
            expected=200,
        )["data"]
        citations = client.request_a(
            "GET",
            f"v1/requests/{first_request_id}/citations",
            expected=200,
        )["data"]
        if request_record["status"] != "COMPLETED" or not citations:
            raise RuntimeError("query or citation records were not persisted")
        client.request_a(
            "POST",
            "v1/feedback",
            expected=201,
            idempotency_key=f"smoke-{run_id}-feedback",
            json={
                "request_id": first_request_id,
                "rating": 1,
                "feedback_type": "helpful",
                "comment": "Synthetic smoke feedback",
            },
        )
        checks["query_citation_feedback"] = True

        stream_events = client.stream_answer(
            {
                **answer_body,
                "query": "List the lifecycle operations covered by the fixture.",
            }
        )
        event_names = [item["event"] for item in stream_events]
        if "answer.delta" not in event_names or not event_names or event_names[-1] != "completed":
            raise RuntimeError("SSE response did not stream deltas through completed")
        completed = stream_events[-1]["data"]
        if completed.get("outcome") != "success":
            raise RuntimeError("SSE response did not complete successfully")
        sequences = [int(item["data"]["sequence"]) for item in stream_events]
        if sequences != list(range(1, len(sequences) + 1)):
            raise RuntimeError("SSE sequence is not contiguous")
        checks["sse_stream"] = {
            "events": len(stream_events),
            "outcome": completed["outcome"],
        }

        deleted_document = client.request_a(
            "DELETE",
            f"v1/documents/{document_id}",
            expected=202,
            idempotency_key=f"smoke-{run_id}-delete-document",
        )["data"]
        client.wait_job(str(deleted_document["job_id"]))
        document_deleted = True
        if client.request_a_status("GET", f"v1/documents/{document_id}") != 404:
            raise RuntimeError("deleted document remains visible")
        after_delete = client.request_a(
            "POST",
            "v1/answers",
            expected=200,
            json=answer_body,
        )["data"]
        if after_delete["outcome"] != "no_evidence" or after_delete["meta"]["cache_type"] != "none":
            raise RuntimeError("revision change did not invalidate the previous answer cache")
        checks["revision_cache_invalidation"] = True

        with fixture.open("rb") as fixture_stream:
            replacement = client.request_a(
                "POST",
                f"v1/knowledge-bases/{knowledge_base_id}/documents",
                expected=202,
                idempotency_key=f"smoke-{run_id}-reuse-deleted-external-id",
                files={"file": (fixture.name, fixture_stream, "text/plain")},
                data={
                    "external_id": document_external_id,
                    "metadata": json.dumps({"fixture": "gateway-e2e-replacement"}),
                    "auto_parse": "false",
                },
            )["data"]
        document_id = str(replacement["document_id"])
        document_deleted = False
        client.wait_job(str(replacement["job_id"]))
        replacement_document = client.request_a(
            "GET",
            f"v1/documents/{document_id}",
            expected=200,
        )["data"]
        if replacement_document["status"] != "UPLOADED":
            raise RuntimeError("replacement document did not finish uploading")
        replacement_delete = client.request_a(
            "DELETE",
            f"v1/documents/{document_id}",
            expected=202,
            idempotency_key=f"smoke-{run_id}-delete-replacement-document",
        )["data"]
        client.wait_job(str(replacement_delete["job_id"]))
        document_deleted = True
        checks["deleted_external_id_reused"] = True

        deleted_kb = client.request_a(
            "DELETE",
            f"v1/knowledge-bases/{knowledge_base_id}?force=true",
            expected=202,
            idempotency_key=f"smoke-{run_id}-delete-kb",
        )["data"]
        client.wait_job(str(deleted_kb["job_id"]))
        knowledge_base_deleted = True
        if (
            client.request_a_status(
                "GET",
                f"v1/knowledge-bases/{knowledge_base_id}",
            )
            != 404
        ):
            raise RuntimeError("deleted knowledge base remains visible")
        checks["fixture_cleanup"] = "api_resources_deleted"
        return {
            "status": "passed",
            "run_id": run_id,
            "checks": checks,
        }
    finally:
        try:
            if document_id is not None and not document_deleted:
                response = client._client.delete(
                    f"v1/documents/{document_id}",
                    headers={
                        **client._headers_a,
                        "Idempotency-Key": f"smoke-{run_id}-finally-delete-document",
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
                        "Idempotency-Key": f"smoke-{run_id}-finally-delete-kb",
                    },
                )
                if response.status_code == 202:
                    client.wait_job(str(response.json()["data"]["job_id"]))
            client.close()


def main() -> None:
    """解析参数、运行 Smoke、把证据写文件并输出 JSON 摘要。"""
    args = build_parser().parse_args()
    result = run(args)
    encoded = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output is not None:
        output = args.output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded + "\n", encoding="utf-8")
        print(f"gateway E2E smoke passed; report written to {output}")
    else:
        print(encoded)


if __name__ == "__main__":
    main()
