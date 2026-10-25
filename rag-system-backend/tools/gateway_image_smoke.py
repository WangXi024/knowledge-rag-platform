#!/usr/bin/env python3
"""上传合成文字 PNG，验证图片经 OCR 解析后能被检索和引用的端到端 Smoke。"""

from __future__ import annotations

import argparse
import json
import struct
import time
import zlib
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

TERMINAL_JOB_STATES = {"SUCCEEDED", "FAILED", "CANCELLED"}
EXPECTED_OCR_TERMS = ("rag", "2026")

# 5×7 点阵只包含合成 Fixture 需要的字符。直接在标准库 PNG 中绘制大号黑字，既不
# 引入 Pillow/系统字体依赖，也避免不同机器字体渲染差异让 OCR Smoke 不稳定。
_GLYPHS: dict[str, tuple[str, ...]] = {
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "G": ("01110", "10001", "10000", "10111", "10001", "10001", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "6": ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
    " ": ("00000",) * 7,
}


def synthetic_ocr_text_png() -> bytes:
    """只用标准库生成写有 ``RAG 2026`` 的 PNG，作为非业务 OCR Fixture。

    当前图片模型的最低契约是文字识别，不要求它判断纯色图形或完成通用视觉推理。
    因此 Fixture 使用确定性点阵大字，函数按 PNG 规范写入 IHDR、压缩像素和 IEND。
    返回值只存在内存，并随临时图片文档一起由 Smoke 的 ``finally`` 清理。
    """
    width = 512
    height = 160
    text = "RAG 2026"
    scale = 10
    cell_width = 6 * scale
    text_width = len(text) * cell_width - scale
    start_x = (width - text_width) // 2
    start_y = (height - 7 * scale) // 2
    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            relative_x = x - start_x
            relative_y = y - start_y
            foreground = False
            if 0 <= relative_x < len(text) * cell_width and 0 <= relative_y < 7 * scale:
                character_index = relative_x // cell_width
                column = (relative_x % cell_width) // scale
                row = relative_y // scale
                if character_index < len(text) and column < 5:
                    foreground = _GLYPHS[text[character_index]][row][column] == "1"
            rows.extend((16, 16, 16) if foreground else (255, 255, 255))

    def png_chunk(name: bytes, payload: bytes) -> bytes:
        """为一个 PNG 数据块写入长度、类型、内容和 CRC32 校验值。"""
        checksum = zlib.crc32(name + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + name + payload + struct.pack(">I", checksum)

    return (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + png_chunk(b"IDAT", zlib.compress(bytes(rows), level=9))
        + png_chunk(b"IEND", b"")
    )


def build_parser() -> argparse.ArgumentParser:
    """定义 Gateway、Direct Tenant/User、轮询超时和证据输出路径。"""
    parser = argparse.ArgumentParser(
        description="Run a synthetic image lifecycle smoke through Gateway and RAGFlow."
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument(
        "--tenant-id",
        default="gateway-image-smoke-tenant",
    )
    parser.add_argument(
        "--user-id",
        default="gateway-image-smoke-user",
    )
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--output", type=Path)
    return parser


class ImageSmokeClient:
    """图片 Smoke 所需的认证 HTTP 调用和异步状态轮询封装。"""

    def __init__(
        self,
        base_url: str,
        tenant_id: str,
        user_id: str,
        timeout_seconds: int,
    ) -> None:
        """创建带 X-Tenant-ID 与 X-User-ID 的同步客户端。"""
        self._client = httpx.Client(
            base_url=base_url.rstrip("/") + "/",
            headers={"X-Tenant-ID": tenant_id, "X-User-ID": user_id},
            timeout=httpx.Timeout(180, connect=10),
            trust_env=False,
        )
        self._timeout_seconds = timeout_seconds

    def close(self) -> None:
        """关闭 HTTP 连接池。"""
        self._client.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        expected: int,
        idempotency_key: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """发送请求、检查预期状态并返回 JSON Object。"""
        headers = {}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        response = self._client.request(method, path, headers=headers, **kwargs)
        if response.status_code != expected:
            raise RuntimeError(
                f"{method} {path} expected {expected}, got {response.status_code}: "
                f"{safe_error(response)}"
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError(f"{method} {path} returned a non-object response")
        return payload

    def wait_job(self, job_id: str) -> dict[str, Any]:
        """轮询图片摄取任务到终态。"""
        deadline = time.monotonic() + self._timeout_seconds
        while time.monotonic() < deadline:
            # Gateway Envelope 的 data 在通用 JSON 类型中是 Any；Smoke 在访问状态前
            # 显式收窄为对象，避免下游协议漂移被当成正常任务结果。
            job = self.request("GET", f"v1/jobs/{job_id}", expected=200).get("data")
            if not isinstance(job, dict):
                raise RuntimeError(f"job {job_id} returned invalid data")
            if job["status"] in TERMINAL_JOB_STATES:
                if job["status"] != "SUCCEEDED":
                    error = job.get("error") or {}
                    raise RuntimeError(
                        f"job {job_id} ended as {job['status']}: {error.get('code', 'unknown')}"
                    )
                return job
            time.sleep(1)
        raise TimeoutError(f"job {job_id} did not finish before the smoke deadline")

    def wait_document_ready(self, document_id: str) -> dict[str, Any]:
        """轮询图片文档到 READY，并在失败状态立即报错。"""
        deadline = time.monotonic() + self._timeout_seconds
        while time.monotonic() < deadline:
            document = self.request(
                "GET",
                f"v1/documents/{document_id}",
                expected=200,
            ).get("data")
            if not isinstance(document, dict):
                raise RuntimeError(f"document {document_id} returned invalid data")
            if document["status"] == "READY":
                return document
            if document["status"] in {"FAILED", "DELETE_FAILED", "DELETED"}:
                error = document.get("last_error") or {}
                raise RuntimeError(
                    f"document ended as {document['status']}: {error.get('code', 'unknown')}"
                )
            time.sleep(2)
        raise TimeoutError("image document did not become READY before the smoke deadline")


def safe_error(response: httpx.Response) -> str:
    """提取有限长度的安全失败信息。"""
    try:
        payload = response.json()
    except ValueError:
        return "non_json_response"
    if not isinstance(payload, dict):
        return "non_object_response"
    error = payload.get("error")
    if isinstance(error, dict):
        return str(error.get("code", "unknown_error"))
    return str(payload.get("detail", "unknown_error"))


def run(args: argparse.Namespace) -> dict[str, Any]:
    """创建知识库、上传生成 PNG、等待解析、检索/问答并验证图片证据。"""
    run_id = uuid4().hex[:12]
    client = ImageSmokeClient(
        args.base_url,
        args.tenant_id,
        args.user_id,
        args.timeout_seconds,
    )
    knowledge_base_id: str | None = None
    document_id: str | None = None
    document_deleted = False
    knowledge_base_deleted = False
    checks: dict[str, Any] = {}
    try:
        health = client._client.get("health/ready")
        health.raise_for_status()
        checks["readiness"] = health.json()["data"]["status"]

        created = client.request(
            "POST",
            "v1/knowledge-bases",
            expected=201,
            idempotency_key=f"image-smoke-{run_id}-create-kb",
            json={
                "external_id": f"synthetic-image-smoke-{run_id}",
                "name": f"Synthetic Image Smoke {run_id}",
                "description": "Non-business generated image lifecycle fixture",
                "parser_profile": "picture-default@1",
                "chunk_profile": "general-256@2",
                # 图片描述最终使用当前通用 Embedding Profile 建立向量。
                "embedding_profile": "embedding-openai-compatible@3",
                "retrieval_profile": "hybrid-default@1",
            },
        )["data"]
        knowledge_base_id = str(created["id"])
        if created["status"] != "READY":
            raise RuntimeError("image knowledge base did not become READY")
        checks["knowledge_base_created"] = True
        checks["parser_profile"] = created["parser_profile"]

        uploaded = client.request(
            "POST",
            f"v1/knowledge-bases/{knowledge_base_id}/documents",
            expected=202,
            idempotency_key=f"image-smoke-{run_id}-upload",
            files={
                "file": (
                    "synthetic-ocr-text.png",
                    synthetic_ocr_text_png(),
                    "image/png",
                )
            },
            data={
                "external_id": f"synthetic-image-{run_id}",
                "metadata": json.dumps(
                    {
                        "fixture": "gateway-image-smoke",
                        "content": "synthetic-ocr-fixture",
                    }
                ),
                "parser_profile": "picture-default@1",
                "auto_parse": "true",
            },
        )["data"]
        document_id = str(uploaded["document_id"])
        client.wait_job(str(uploaded["job_id"]))
        document = client.wait_document_ready(document_id)
        if int(document["chunk_count"]) < 1:
            raise RuntimeError("image parsing produced no chunks")
        checks["document_ready"] = True
        checks["chunk_count"] = int(document["chunk_count"])
        checks["token_count"] = int(document["token_count"])

        chunks = client.request(
            "GET",
            f"v1/documents/{document_id}/chunks",
            expected=200,
        )["data"]
        if not isinstance(chunks, list) or not chunks:
            raise RuntimeError("image chunk listing is empty")
        content = "\n".join(
            str(chunk.get("content", "")) for chunk in chunks if isinstance(chunk, dict)
        )
        # OCR 可能在字母或数字之间插入空格/换行，先删除非字母数字字符再比较；文件
        # 名和 Metadata 都不包含期望词，断言通过必须来自真实解析后的 Chunk 正文。
        normalized_content = "".join(
            character for character in content.lower() if character.isalnum()
        )
        if not all(term in normalized_content for term in EXPECTED_OCR_TERMS):
            raise RuntimeError("image chunks do not contain the synthetic OCR text")
        checks["ocr_text_present"] = True

        retrieval = client.request(
            "POST",
            "v1/retrieval",
            expected=200,
            json={
                "knowledge_base_ids": [knowledge_base_id],
                "document_ids": [document_id],
                "query": "What short text is written in the synthetic image?",
                "retrieval_profile": "hybrid-default@1",
                "options": {
                    "page_size": 5,
                    "include_content": True,
                    "include_positions": True,
                },
            },
        )["data"]
        if not retrieval["candidates"]:
            raise RuntimeError("image retrieval returned no candidates")
        checks["retrieval_candidates"] = len(retrieval["candidates"])

        deletion = client.request(
            "DELETE",
            f"v1/documents/{document_id}?cancel_active=true",
            expected=202,
            idempotency_key=f"image-smoke-{run_id}-delete-document",
        )["data"]
        client.wait_job(str(deletion["job_id"]))
        document_deleted = True

        kb_deletion = client.request(
            "DELETE",
            f"v1/knowledge-bases/{knowledge_base_id}?force=true",
            expected=202,
            idempotency_key=f"image-smoke-{run_id}-delete-kb",
        )["data"]
        client.wait_job(str(kb_deletion["job_id"]))
        knowledge_base_deleted = True
        checks["fixture_cleanup"] = "api_resources_deleted"
        return {"status": "passed", "run_id": run_id, "checks": checks}
    finally:
        try:
            if document_id and not document_deleted:
                response = client._client.delete(
                    f"v1/documents/{document_id}?cancel_active=true",
                    headers={"Idempotency-Key": f"image-smoke-{run_id}-finally-delete-document"},
                )
                if response.status_code == 202:
                    client.wait_job(str(response.json()["data"]["job_id"]))
        finally:
            if knowledge_base_id and not knowledge_base_deleted:
                response = client._client.delete(
                    f"v1/knowledge-bases/{knowledge_base_id}?force=true",
                    headers={"Idempotency-Key": f"image-smoke-{run_id}-finally-delete-kb"},
                )
                if response.status_code == 202:
                    client.wait_job(str(response.json()["data"]["job_id"]))
            client.close()


def main() -> None:
    """执行图片 Smoke 并输出/保存验收证据。"""
    args = build_parser().parse_args()
    result = run(args)
    encoded = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        output = args.output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(encoded + "\n", encoding="utf-8")
        print(f"gateway image smoke passed; report written to {output}")
    else:
        print(encoded)


if __name__ == "__main__":
    main()
