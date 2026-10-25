"""仅供 Smoke 测试使用的确定性 OpenAI-compatible Embedding 假服务。

它不具备真实语义能力：相同文本始终得到相同的 128 维归一化哈希向量，便于端到端
验证 API、缓存和容器网络。绝不能用于生产知识检索。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MODEL_ID = "lazytt-smoke-embedding"
VECTOR_DIMENSION = 128


def embed(text: str) -> list[float]:
    """把英文数字 Token 哈希投影到固定向量并做 L2 归一化。"""
    vector = [0.0] * VECTOR_DIMENSION
    tokens = re.findall(r"[a-z0-9]+", text.lower())
    if not tokens:
        vector[0] = 1.0
        return vector

    for token in tokens:
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        for offset in range(0, 8, 2):
            index = int.from_bytes(digest[offset : offset + 2], "big") % VECTOR_DIMENSION
            sign = 1.0 if digest[8 + offset] & 1 else -1.0
            vector[index] += sign

    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


class Handler(BaseHTTPRequestHandler):
    """实现健康、模型列表和 Embeddings 三个最小 HTTP 端点。"""
    server_version = "LazyTTSmokeEmbedding/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        """关闭默认访问日志，保持 Smoke 输出只包含结构化结果。"""
        return

    def send_json(self, status: int, payload: dict[str, Any]) -> None:
        """发送带正确类型与长度头的紧凑 JSON 响应。"""
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        """处理 ``/healthz`` 与 ``/v1/models``，其他路径返回 404。"""
        if self.path == "/healthz":
            self.send_json(200, {"status": "ok"})
            return
        if self.path.rstrip("/") == "/v1/models":
            self.send_json(
                200,
                {
                    "object": "list",
                    "data": [
                        {
                            "id": MODEL_ID,
                            "object": "model",
                            "owned_by": "lazytt-smoke",
                        }
                    ],
                },
            )
            return
        self.send_json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:
        """校验 Embeddings 请求并按输入顺序返回确定性向量和粗略 Token 用量。"""
        if self.path.rstrip("/") != "/v1/embeddings":
            self.send_json(404, {"error": {"message": "not found"}})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length))
            raw_input = payload.get("input", [])
            inputs = [raw_input] if isinstance(raw_input, str) else list(raw_input)
            if payload.get("model") not in {None, MODEL_ID}:
                raise ValueError("unsupported smoke model")
            if not all(isinstance(item, str) for item in inputs):
                raise ValueError("input must be a string or list of strings")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            self.send_json(400, {"error": {"message": str(exc)}})
            return

        prompt_tokens = sum(
            len(re.findall(r"[a-z0-9]+", item.lower())) for item in inputs
        )
        self.send_json(
            200,
            {
                "object": "list",
                "model": MODEL_ID,
                "data": [
                    {
                        "object": "embedding",
                        "index": index,
                        "embedding": embed(item),
                    }
                    for index, item in enumerate(inputs)
                ],
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "total_tokens": prompt_tokens,
                },
            },
        )


if __name__ == "__main__":
    # 该服务运行在容器中，必须监听全部容器网卡，宿主机和其他容器才能访问测试端口。
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()  # noqa: S104
