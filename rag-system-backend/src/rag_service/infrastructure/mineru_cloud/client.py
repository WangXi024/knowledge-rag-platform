"""把单份 PDF 上传到 MinerU 官网并等待 ZIP，供内部解析适配服务调用。

Token 只发给固定官网 API；签名上传和结果下载使用独立请求，不携带 Token。
不重试创建任务，避免重复消耗额度；轮询有总时限，异常不包含下游正文和签名 URL。
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote, urlsplit
from uuid import uuid4

import httpx

API_URL = "https://mineru.net/api/v4"
MAX_ZIP_BYTES = 128 * 1024 * 1024


class MinerUError(Exception):
    """可安全返回的固定错误；不保存原始响应、Token 或文件内容。"""


def checked_url(value: Any) -> str:
    """仅接受官网返回的已知 HTTPS 存储地址，拒绝内网、其他主机和跳转。"""
    if not isinstance(value, str):
        raise MinerUError("MINERU_INVALID_DOWNLOAD_URL")
    url = urlsplit(value)
    allowed = {
        "mineru.oss-cn-shanghai.aliyuncs.com",
        "cdn-mineru.openxlab.org.cn",
        "mineru.net",
    }
    if (
        url.scheme != "https" or url.hostname not in allowed
        or url.username or url.password or url.port not in (None, 443)
    ):
        raise MinerUError("MINERU_UNAPPROVED_STORAGE_HOST")
    return value


@dataclass
class MinerUCloudClient:
    """每次请求创建的无共享状态客户端；HTTP 连接由调用方关闭，取消会向上传播。"""

    http: httpx.AsyncClient
    token: str = field(repr=False)
    model: str = "vlm"
    timeout: float = 1500
    poll_interval: float = 3
    # 服务层可包裹下载与转换，共用独立资源槽；普通客户端仍返回原始 ZIP。
    result_handler: Callable[[str], Awaitable[bytes]] | None = None

    async def api(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """调用固定官方 API 并校验业务状态；失败只抛固定错误，不泄露响应。"""
        response = await self.http.request(
            method, API_URL + path,
            headers={"Authorization": f"Bearer {self.token}"}, **kwargs,
        )
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or body.get("code") != 0:
            raise MinerUError("MINERU_API_REJECTED")
        data = body.get("data")
        if not isinstance(data, dict):
            raise MinerUError("MINERU_INVALID_RESPONSE")
        return data

    async def parse(
        self, pdf: bytes, *, language: str, ocr: bool, formula: bool, table: bool,
    ) -> bytes:
        """提交单文件并下载结果；上传不使用用户文件名，超时不自动重新提交。"""
        async with asyncio.timeout(self.timeout):
            data_id = uuid4().hex
            data = await self.api("POST", "/file-urls/batch", json={
                "files": [{"name": "document.pdf", "data_id": data_id, "is_ocr": ocr}],
                "model_version": self.model, "language": language,
                "enable_formula": formula, "enable_table": table,
            })
            urls, batch_id = data.get("file_urls"), data.get("batch_id")
            if not isinstance(urls, list) or len(urls) != 1 or not isinstance(batch_id, str):
                raise MinerUError("MINERU_INVALID_RESPONSE")
            # 签名 URL 本身带授权；这里绝不附加官网 Bearer Token。
            uploaded = await self.http.put(checked_url(urls[0]), content=pdf, timeout=180)
            uploaded.raise_for_status()
            failures = 0
            while True:
                try:
                    result = await self.api(
                        "GET", "/extract-results/batch/" + quote(batch_id, safe=""),
                    )
                    failures = 0
                except (httpx.TimeoutException, httpx.HTTPStatusError) as exc:
                    retryable = (
                        isinstance(exc, httpx.TimeoutException)
                        or exc.response.status_code == 429 or exc.response.status_code >= 500
                    )
                    failures += 1
                    if not retryable or failures >= 3:
                        raise MinerUError("MINERU_POLL_FAILED") from None
                    await asyncio.sleep(self.poll_interval)
                    continue
                entries = result.get("extract_result")
                if not isinstance(entries, list):
                    raise MinerUError("MINERU_INVALID_RESPONSE")
                matches = [
                    r for r in entries if isinstance(r, dict) and r.get("data_id") == data_id
                ]
                if len(matches) != 1:
                    raise MinerUError("MINERU_RESULT_MISMATCH")
                entry = matches[0]
                state = entry.get("state")
                if state == "done":
                    handler = self.result_handler or self.download
                    return await handler(checked_url(entry.get("full_zip_url")))
                if state == "failed":
                    raise MinerUError("MINERU_PARSE_FAILED")
                if state not in {"pending", "running", "converting", "waiting-file"}:
                    raise MinerUError("MINERU_UNKNOWN_STATE")
                await asyncio.sleep(self.poll_interval)

    async def download(self, url: str) -> bytes:
        """限量读取 ZIP；不跟随重定向，避免签名地址被转发到其他目标。"""
        output = bytearray()
        async with self.http.stream("GET", url) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                output.extend(chunk)
                if len(output) > MAX_ZIP_BYTES:
                    raise MinerUError("MINERU_RESULT_TOO_LARGE")
        return bytes(output)
