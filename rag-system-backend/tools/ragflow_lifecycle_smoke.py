"""直接对 RAGFlow 执行数据集—上传—解析—检索—删除生命周期 Smoke。"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx


class SmokeFailure(RuntimeError):
    """RAGFlow 响应或生命周期状态与验收预期不一致。"""
    pass


SMOKE_PROVIDER = "OpenAI-API-Compatible"
SMOKE_INSTANCE = "smoke-local"
SMOKE_MODEL = "lazytt-smoke-embedding"
SMOKE_MODEL_REF = f"{SMOKE_MODEL}@{SMOKE_INSTANCE}@{SMOKE_PROVIDER}"


def require_success(response: httpx.Response, operation: str) -> Any:
    """校验 HTTP 和 RAGFlow Envelope 成功，失败时带操作名而不泄漏凭据。"""
    try:
        payload = response.json()
    except ValueError as exc:
        raise SmokeFailure(
            f"{operation}: HTTP {response.status_code} returned invalid JSON"
        ) from exc
    if not response.is_success:
        raise SmokeFailure(f"{operation}: HTTP {response.status_code}: {payload!r}")
    if not isinstance(payload, dict) or payload.get("code") != 0:
        raise SmokeFailure(f"{operation}: RAGFlow business failure: {payload!r}")
    return payload.get("data")


def parse_args() -> argparse.Namespace:
    """定义 RAGFlow 地址、API Key、测试文件、轮询参数和证据输出。"""
    parser = argparse.ArgumentParser(description="RAGFlow v0.26.4 lifecycle smoke")
    parser.add_argument(
        "--base-url",
        default=os.getenv("RAGFLOW_BASE_URL", "http://127.0.0.1:9380"),
    )
    parser.add_argument("--api-key", default=os.getenv("RAGFLOW_API_KEY"))
    parser.add_argument(
        "--embedding-model",
        default=os.getenv("RAGFLOW_EMBEDDING_MODEL"),
        help="RAGFlow model identifier such as model@provider",
    )
    parser.add_argument(
        "--smoke-embedding-base-url",
        default=os.getenv("RAGFLOW_SMOKE_EMBEDDING_BASE_URL"),
        help="Internal OpenAI-compatible URL used only to prepare the smoke model",
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        default=Path("tests/fixtures/ragflow-smoke.txt"),
    )
    parser.add_argument("--parse-timeout", type=int, default=300)
    return parser.parse_args()


def ensure_smoke_embedding(client: httpx.Client, base_url: str, evidence: dict[str, Any]) -> None:
    """确保测试专用 Embedding 提供方/模型已配置，并记录是否新建。"""
    providers = require_success(client.get("/api/v1/providers"), "list_smoke_providers")
    provider_names = {
        item.get("name")
        for item in providers
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    if SMOKE_PROVIDER not in provider_names:
        require_success(
            client.put(
                "/api/v1/providers",
                json={"provider_name": SMOKE_PROVIDER},
            ),
            "add_smoke_provider",
        )

    instances = require_success(
        client.get(f"/api/v1/providers/{SMOKE_PROVIDER}/instances"),
        "list_smoke_instances",
    )
    instance_names = {
        item.get("instance_name")
        for item in instances
        if isinstance(item, dict) and isinstance(item.get("instance_name"), str)
    }
    if SMOKE_INSTANCE not in instance_names:
        require_success(
            client.post(
                f"/api/v1/providers/{SMOKE_PROVIDER}/instances",
                json={
                    "instance_name": SMOKE_INSTANCE,
                    "api_key": "smoke-only",
                    "base_url": base_url,
                    "region": "local",
                    "model_info": [
                        {
                            "model_name": SMOKE_MODEL,
                            "model_type": ["embedding"],
                            "max_tokens": 8192,
                            "extra": {"smoke_only": True},
                        }
                    ],
                },
                timeout=30,
            ),
            "create_smoke_embedding_instance",
        )

    models = require_success(
        client.get(f"/api/v1/providers/{SMOKE_PROVIDER}/instances/{SMOKE_INSTANCE}/models"),
        "list_smoke_models",
    )
    model_names = {
        item.get("name") or item.get("model_name") for item in models if isinstance(item, dict)
    }
    if SMOKE_MODEL not in model_names:
        require_success(
            client.post(
                f"/api/v1/providers/{SMOKE_PROVIDER}/instances/{SMOKE_INSTANCE}/models",
                json={
                    "model_name": SMOKE_MODEL,
                    "model_type": ["embedding"],
                    "max_tokens": 8192,
                    "extra": {"smoke_only": True},
                },
            ),
            "add_smoke_embedding_model",
        )

    evidence["steps"].append(
        {
            "operation": "prepare_smoke_embedding",
            "result": "ok",
            "model": SMOKE_MODEL_REF,
        }
    )


def document_state(data: Any, document_id: str) -> dict[str, Any]:
    """从文档列表响应中定位唯一目标文档。"""
    if not isinstance(data, dict):
        raise SmokeFailure(f"list_documents: invalid data shape: {data!r}")
    docs = data.get("docs")
    if not isinstance(docs, list):
        raise SmokeFailure(f"list_documents: missing docs: {data!r}")
    for item in docs:
        if isinstance(item, dict) and item.get("id") == document_id:
            return item
    raise SmokeFailure("list_documents: uploaded document is absent")


def run() -> dict[str, Any]:
    """执行真实 RAGFlow 全生命周期并在 finally 中尽力删除合成资源。"""
    args = parse_args()
    if not args.api_key:
        raise SmokeFailure("RAGFLOW_API_KEY is required")
    if args.smoke_embedding_base_url and not args.embedding_model:
        args.embedding_model = SMOKE_MODEL_REF
    if not args.embedding_model:
        raise SmokeFailure("RAGFLOW_EMBEDDING_MODEL is required")
    if not args.fixture.is_file():
        raise SmokeFailure(f"fixture does not exist: {args.fixture}")

    headers = {"Authorization": f"Bearer {args.api_key}"}
    dataset_id: str | None = None
    document_id: str | None = None
    evidence: dict[str, Any] = {"steps": []}

    with httpx.Client(
        base_url=args.base_url.rstrip("/"),
        headers=headers,
        timeout=httpx.Timeout(connect=5, read=30, write=30, pool=5),
    ) as client:
        health_response = client.get("/api/v1/system/healthz")
        if not health_response.is_success:
            raise SmokeFailure(
                f"health: HTTP {health_response.status_code}: {health_response.text}"
            )
        health = health_response.json()
        if not isinstance(health, dict) or health.get("status") != "ok":
            raise SmokeFailure(f"health: unhealthy response: {health!r}")
        evidence["steps"].append({"operation": "health", "result": "ok", "data": health})

        if args.smoke_embedding_base_url and args.embedding_model == SMOKE_MODEL_REF:
            ensure_smoke_embedding(client, args.smoke_embedding_base_url, evidence)

        try:
            dataset_name = f"lazytt-smoke-{uuid4().hex[:10]}"
            dataset = require_success(
                client.post(
                    "/api/v1/datasets",
                    json={
                        "name": dataset_name,
                        "description": "Synthetic lifecycle smoke fixture",
                        "embedding_model": args.embedding_model,
                        "permission": "me",
                        "chunk_method": "naive",
                        "parser_config": {
                            "chunk_token_num": 128,
                            "delimiter": "\n",
                            "layout_recognize": "Plain Text",
                            "raptor": {"use_raptor": False},
                            "graphrag": {"use_graphrag": False},
                            "parent_child": {"use_parent_child": False},
                        },
                    },
                ),
                "create_dataset",
            )
            if not isinstance(dataset, dict) or not isinstance(dataset.get("id"), str):
                raise SmokeFailure(f"create_dataset: missing id: {dataset!r}")
            dataset_id = dataset["id"]
            evidence["steps"].append(
                {"operation": "create_dataset", "result": "ok", "dataset_id": dataset_id}
            )

            with args.fixture.open("rb") as fixture_file:
                uploaded = require_success(
                    client.post(
                        f"/api/v1/datasets/{dataset_id}/documents",
                        params={"type": "local"},
                        files={"file": (args.fixture.name, fixture_file, "text/plain")},
                        timeout=60,
                    ),
                    "upload_document",
                )
            if not isinstance(uploaded, list) or len(uploaded) != 1:
                raise SmokeFailure(f"upload_document: ambiguous response: {uploaded!r}")
            document = uploaded[0]
            if not isinstance(document, dict) or not isinstance(document.get("id"), str):
                raise SmokeFailure(f"upload_document: missing id: {document!r}")
            document_id = document["id"]
            evidence["steps"].append(
                {"operation": "upload_document", "result": "ok", "document_id": document_id}
            )

            require_success(
                client.post(
                    f"/api/v1/datasets/{dataset_id}/chunks",
                    json={"document_ids": [document_id]},
                ),
                "start_parse",
            )
            evidence["steps"].append({"operation": "start_parse", "result": "ok"})

            deadline = time.monotonic() + args.parse_timeout
            last_state: dict[str, Any] = {}
            while time.monotonic() < deadline:
                listed = require_success(
                    client.get(
                        f"/api/v1/datasets/{dataset_id}/documents",
                        params={"id": document_id, "page": 1, "page_size": 20},
                    ),
                    "get_document_status",
                )
                last_state = document_state(listed, document_id)
                run_state = str(last_state.get("run", "")).upper()
                if run_state in {"DONE", "3"}:
                    break
                if run_state in {"FAIL", "CANCEL", "2", "4"}:
                    raise SmokeFailure(f"parse failed: {last_state!r}")
                time.sleep(2)
            else:
                raise SmokeFailure(f"parse timed out: {last_state!r}")
            evidence["steps"].append(
                {
                    "operation": "parse_status",
                    "result": "ok",
                    "run": last_state.get("run"),
                    "chunk_count": last_state.get("chunk_count"),
                }
            )

            chunks = require_success(
                client.get(
                    f"/api/v1/datasets/{dataset_id}/documents/{document_id}/chunks",
                    params={"page": 1, "page_size": 20},
                ),
                "list_chunks",
            )
            chunk_count = len(chunks.get("chunks", [])) if isinstance(chunks, dict) else 0
            if chunk_count < 1:
                raise SmokeFailure(f"list_chunks: no chunks returned: {chunks!r}")
            evidence["steps"].append(
                {"operation": "list_chunks", "result": "ok", "chunk_count": chunk_count}
            )

            retrieval = require_success(
                client.post(
                    "/api/v1/retrieval",
                    json={
                        "question": "What does the lifecycle test verify?",
                        "dataset_ids": [dataset_id],
                        "document_ids": [document_id],
                        "page": 1,
                        "page_size": 10,
                        "similarity_threshold": 0.0,
                        "vector_similarity_weight": 0.5,
                        "top_k": 20,
                        "keyword": False,
                        "highlight": False,
                        "use_kg": False,
                        "toc_enhance": False,
                    },
                ),
                "retrieve",
            )
            retrieved_count = len(retrieval.get("chunks", [])) if isinstance(retrieval, dict) else 0
            if retrieved_count < 1:
                raise SmokeFailure(f"retrieve: no chunks returned: {retrieval!r}")
            first_retrieval_chunk = retrieval["chunks"][0]
            evidence["steps"].append(
                {
                    "operation": "retrieve",
                    "result": "ok",
                    "candidate_count": retrieved_count,
                    "chunk_fields": (
                        sorted(first_retrieval_chunk)
                        if isinstance(first_retrieval_chunk, dict)
                        else []
                    ),
                    "chunk_field_types": (
                        {
                            name: type(value).__name__
                            for name, value in first_retrieval_chunk.items()
                        }
                        if isinstance(first_retrieval_chunk, dict)
                        else {}
                    ),
                }
            )
        finally:
            if dataset_id and document_id:
                deleted_document = require_success(
                    client.request(
                        "DELETE",
                        f"/api/v1/datasets/{dataset_id}/documents",
                        json={"ids": [document_id]},
                    ),
                    "delete_document",
                )
                evidence["steps"].append(
                    {
                        "operation": "delete_document",
                        "result": "ok",
                        "data": deleted_document,
                    }
                )
            if dataset_id:
                deleted_dataset = require_success(
                    client.request(
                        "DELETE",
                        "/api/v1/datasets",
                        json={"ids": [dataset_id], "delete_all": False},
                    ),
                    "delete_dataset",
                )
                evidence["steps"].append(
                    {
                        "operation": "delete_dataset",
                        "result": "ok",
                        "data": deleted_dataset,
                    }
                )

    evidence["result"] = "ok"
    return evidence


def main() -> None:
    """运行 Smoke 并输出/保存不含 API Key 的证据。"""
    try:
        print(json.dumps(run(), ensure_ascii=False, indent=2, sort_keys=True))
    except SmokeFailure as exc:
        raise SystemExit(f"ragflow lifecycle smoke failed: {exc}") from exc


if __name__ == "__main__":
    main()
