"""验证 MinerU 云端协议、ZIP 保真、安全边界及可选 Profile，不连接公网或业务数据库。"""

import asyncio
import json
import stat
from io import BytesIO
from typing import Any
from zipfile import ZipFile, ZipInfo

import httpx
import pytest
from fastapi.testclient import TestClient

from rag_service.bootstrap.profiles import load_profile_seeds
from rag_service.infrastructure.mineru_cloud import server
from rag_service.infrastructure.mineru_cloud.archive import compatible_zip
from rag_service.infrastructure.mineru_cloud.client import (
    MinerUCloudClient,
    MinerUError,
    checked_url,
)
from rag_service.infrastructure.mineru_cloud.register import register
from rag_service.settings import ServiceSettings


def archive(files: dict[str, bytes]) -> bytes:
    """创建最小模拟官网包；保留字节以断言表格、图片和坐标未被改写。"""
    output = BytesIO()
    with ZipFile(output, "w") as result:
        for name, content in files.items():
            result.writestr(name, content)
    return output.getvalue()


def test_archive_preserves_content_and_images() -> None:
    """官网根目录和随机文件名前缀变化时，内容及图片仍应逐字节保留。"""
    content = json.dumps([
        {"type": "text", "text": "合成测试", "page_idx": 0, "bbox": [1, 2, 3, 4]},
        {"type": "table", "table_body": "<table><tr><td>42</td></tr></table>",
         "page_idx": 1, "bbox": [5, 6, 7, 8], "img_path": "images/table.jpg"},
    ], ensure_ascii=False).encode()
    result = compatible_zip(archive({
        "batch/random_content_list.json": content,
        "batch/images/table.jpg": b"synthetic-image",
        "batch/layout.json": b"{}", "batch/full.md": b"unchanged",
    }))
    with ZipFile(BytesIO(result)) as parsed:
        assert parsed.read("content_list.json") == content
        assert parsed.read("images/table.jpg") == b"synthetic-image"
        assert parsed.read("layout.json") == b"{}"
        assert parsed.read("full.md") == b"unchanged"


@pytest.mark.parametrize("files", [
    {"../content_list.json": b"[{}]"},
    {"/content_list.json": b"[{}]"},
    {"content_list.json": b"[]"},
    {"content_list.json": b"{}"},
    {"content_list.json": b"[{}]", "other_content_list.json": b"[{}]"},
    {"content_list.json": b'[{"img_path":"../secret"}]'},
    {"content_list.json": b'[{"img_path":"images/missing.png"}]'},
])
def test_archive_rejects_incomplete_or_unsafe_output(files: dict[str, bytes]) -> None:
    """不安全目录、空结果和丢失图片必须报错，不能冒充完整解析成功。"""
    with pytest.raises(MinerUError):
        compatible_zip(archive(files))


def test_archive_rejects_symlink() -> None:
    """云端 ZIP 中符号链接不能传入 RAGFlow 的文件系统。"""
    output = BytesIO()
    with ZipFile(output, "w") as result:
        link = ZipInfo("content_list.json")
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        result.writestr(link, "/etc/passwd")
    with pytest.raises(MinerUError):
        compatible_zip(output.getvalue())


@pytest.mark.parametrize("url", [
    "http://mineru.net/result", "https://127.0.0.1/result", "https://example.com/file",
    "https://mineru.net.evil.example/file", "https://token@mineru.net/file",
])
def test_storage_url_allowlist(url: str) -> None:
    """下游返回的 URL 不能将服务变成内网代理或把文件上传到陌生主机。"""
    with pytest.raises(MinerUError):
        checked_url(url)


@pytest.mark.asyncio
async def test_cloud_vlm_upload_poll_download() -> None:
    """完整模拟官网协议：VLM 参数、状态推进、文件上传和下载都必须正确。"""
    requests: list[httpx.Request] = []
    data_id = ""
    polls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        """按实际调用顺序响应；只允许官网 API 收到 Token。"""
        nonlocal data_id, polls
        requests.append(request)
        if request.url.path == "/api/v4/file-urls/batch":
            body = json.loads(request.content)
            assert body["model_version"] == "vlm"
            assert body["enable_formula"] is True and body["enable_table"] is True
            data_id = body["files"][0]["data_id"]
            assert body["files"][0]["name"] == "document.pdf"
            return httpx.Response(200, json={"code": 0, "data": {
                "batch_id": "batch", "file_urls": [
                    "https://mineru.oss-cn-shanghai.aliyuncs.com/upload?signature=test",
                ],
            }})
        if request.method == "PUT":
            assert request.content == b"%PDF-synthetic"
            assert "authorization" not in request.headers
            return httpx.Response(200)
        if request.url.path == "/api/v4/extract-results/batch/batch":
            polls += 1
            return httpx.Response(200, json={"code": 0, "data": {"extract_result": [{
                "data_id": data_id, "state": "running" if polls == 1 else "done",
                "full_zip_url": "https://cdn-mineru.openxlab.org.cn/result.zip",
            }]}})
        assert "authorization" not in request.headers
        return httpx.Response(200, content=b"zip-result")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        cloud = MinerUCloudClient(http, "test-token", poll_interval=0)
        result = await cloud.parse(
            b"%PDF-synthetic", language="ch", ocr=False, formula=True, table=True,
        )
    assert result == b"zip-result" and polls == 2
    assert len(requests) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["failed", "mismatch", "timeout", "unauthorized"])
async def test_cloud_failure_does_not_resubmit(failure: str) -> None:
    """失败、超时与任务错配不能静默回退或再次创建官网任务。"""
    data_id = ""
    submissions = 0

    def handle(request: httpx.Request) -> httpx.Response:
        """模拟各失败阶段，并记录是否发生重复提交。"""
        nonlocal data_id, submissions
        if request.method == "POST":
            submissions += 1
            data_id = json.loads(request.content)["files"][0]["data_id"]
            return httpx.Response(200, json={"code": 0, "data": {
                "batch_id": "batch", "file_urls": [
                    "https://mineru.oss-cn-shanghai.aliyuncs.com/upload",
                ],
            }})
        if request.method == "PUT":
            return httpx.Response(200)
        if failure == "unauthorized":
            return httpx.Response(401)
        return httpx.Response(200, json={"code": 0, "data": {"extract_result": [{
            "data_id": "other" if failure == "mismatch" else data_id,
            "state": "running" if failure == "timeout" else "failed",
            "err_msg": "secret-must-not-escape",
        }]}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        cloud = MinerUCloudClient(http, "test-token", timeout=0.02, poll_interval=0.01)
        with pytest.raises((MinerUError, TimeoutError)) as error:
            await cloud.parse(b"%PDF-test", language="ch", ocr=False, formula=True, table=True)
        assert "secret-must-not-escape" not in str(error.value)
    assert submissions == 1


def test_http_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    """RAGFlow 的 HEAD 探测和 multipart 请求应得到 ZIP；未配置时拒绝调用。"""
    monkeypatch.delenv("MINERU_CLOUD_API_TOKEN", raising=False)
    monkeypatch.setattr(server, "slots", asyncio.Semaphore(2))
    with TestClient(server.app) as client:
        assert client.head("/openapi.json").status_code == 503
        monkeypatch.setenv("MINERU_CLOUD_API_TOKEN", "test-token")
        assert client.head("/openapi.json").status_code == 200

        async def parse(self: MinerUCloudClient, pdf: bytes, **kwargs: Any) -> bytes:
            """避免公网调用，仅返回最小官方内容列表。"""
            assert self.model == "vlm"
            return archive({"content_list.json": b'[{"type":"text","text":"ok"}]'})

        monkeypatch.setattr(MinerUCloudClient, "parse", parse)
        response = client.post("/file_parse", files={"files": ("test.pdf", b"%PDF-test")})
        assert response.status_code == 200
        assert response.headers["content-type"] == "application/zip"
        assert client.post("/file_parse", files={"files": ("bad.pdf", b"bad")}).status_code == 422
        assert client.post(
            "/file_parse", files={"files": ("test.pdf", b"%PDF-test")},
            data={"start_page_id": "12"},
        ).status_code == 422


@pytest.mark.parametrize("redacted", [False, True])
def test_registration_is_idempotent(redacted: bool) -> None:
    """只新增云端实例，不覆盖用户已有 MinerU 实例或租户默认模型。"""
    created: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        """模拟 Provider API，两次注册应仅有一次实例创建。"""
        if request.method == "POST":
            payload = json.loads(request.content)
            assert payload["instance_name"] == "mineru-cloud"
            assert "TOKEN" not in payload["api_key"]
            created.append(payload)
            result: Any = {}
        elif request.url.path.endswith("/models"):
            result = [{"name": "mineru-cloud", "model_type": ["ocr"]}]
        elif request.url.path.endswith("/instances/mineru-cloud"):
            result = (
                {"instance_name": "mineru-cloud"}
                if redacted else {"api_key": created[0]["api_key"]}
            )
        elif request.url.path.endswith("/instances"):
            result = [{"instance_name": "other-mineru"}] + [
                {"instance_name": p["instance_name"]} for p in created
            ]
        else:
            result = [{"name": "MinerU"}]
        return httpx.Response(200, json={"code": 0, "data": result})

    with httpx.Client(base_url="http://ragflow", transport=httpx.MockTransport(handle)) as client:
        register(client)
        register(client)
    assert len(created) == 1


def test_cloud_profile_is_explicit_and_submits_whole_document(
    service_settings: ServiceSettings,
) -> None:
    """显式注册引用且单文档单任务，防止十二页切分造成整份 PDF 重复计费。"""
    seeds = load_profile_seeds(service_settings.profiles)
    cloud = next(s for s in seeds if s.name == "mineru-cloud")
    config = cloud.config_json["ragflow"]["parser_config"]
    assert config["layout_recognize"] == "mineru-cloud@mineru-cloud@MinerU"
    assert config["task_page_size"] >= 200


@pytest.mark.asyncio
async def test_download_rejects_redirect_and_size(monkeypatch: pytest.MonkeyPatch) -> None:
    """下载不跟随跳转，且响应体超限时及时终止，防止意外转发和内存耗尽。"""
    from rag_service.infrastructure.mineru_cloud import client as cloud_module

    monkeypatch.setattr(cloud_module, "MAX_ZIP_BYTES", 4)
    for status in (302, 200):
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request, status=status: httpx.Response(
                status, content=b"too-large", headers={"location": "https://example.com"},
            ),
        )) as http:
            cloud = MinerUCloudClient(http, "test-token")
            with pytest.raises((httpx.HTTPStatusError, MinerUError)):
                await cloud.download("https://cdn-mineru.openxlab.org.cn/result.zip")


def test_http_busy_and_safe_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """满载立即拒绝；下游包含签名链接的异常只对外显示固定错误。"""
    monkeypatch.setenv("MINERU_CLOUD_API_TOKEN", "test-token")
    monkeypatch.setattr(server, "slots", asyncio.Semaphore(0))
    with TestClient(server.app) as client:
        request_files = {"files": ("test.pdf", b"%PDF-test")}
        assert client.post("/file_parse", files=request_files).status_code == 503
        monkeypatch.setattr(server, "slots", asyncio.Semaphore(2))

        async def fail(self: MinerUCloudClient, pdf: bytes, **kwargs: Any) -> bytes:
            """模拟 HTTP 库异常含秘密 URL，防止错误链被直接回显。"""
            raise httpx.ConnectError("https://example.com?signature=secret")

        monkeypatch.setattr(MinerUCloudClient, "parse", fail)
        response = client.post("/file_parse", files=request_files)
        assert response.status_code == 502
        assert response.json() == {"detail": "MINERU_CLOUD_REQUEST_FAILED"}


@pytest.mark.asyncio
async def test_ten_active_requests_reject_eleventh(monkeypatch: pytest.MonkeyPatch) -> None:
    """十个云端请求可同时等待，第十一个明确忙碌；完成后槽位恢复且不调用公网。"""
    monkeypatch.setenv("MINERU_CLOUD_API_TOKEN", "test-token")
    monkeypatch.setattr(server, "slots", asyncio.Semaphore(10))
    entered = 0
    full = asyncio.Event()
    finish = asyncio.Event()

    async def parse(self: MinerUCloudClient, pdf: bytes, **kwargs: Any) -> bytes:
        """阻塞模拟官网响应，确保测到真实同时在途的请求数。"""
        nonlocal entered
        entered += 1
        if entered == 10:
            full.set()
        await finish.wait()
        return archive({"content_list.json": b'[{"type":"text","text":"ok"}]'})

    monkeypatch.setattr(MinerUCloudClient, "parse", parse)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.app), base_url="http://test"
    ) as client:
        tasks = [asyncio.create_task(client.post(
            "/file_parse", files={"files": ("test.pdf", b"%PDF-test")}
        )) for _ in range(10)]
        try:
            await asyncio.wait_for(full.wait(), timeout=5)
            extra = await client.post(
                "/file_parse", files={"files": ("test.pdf", b"%PDF-test")}
            )
            assert extra.status_code == 503
            assert extra.json()["detail"] == "MINERU_CLOUD_BUSY"
        finally:
            finish.set()
            results = await asyncio.gather(*tasks)
        assert all(result.status_code == 200 for result in results)
        assert not server.slots.locked()


@pytest.mark.asyncio
async def test_result_stage_limits_download_and_conversion(monkeypatch: pytest.MonkeyPatch) -> None:
    """十个在途请求最多两份结果同时下载和转换，其余不提前缓存结果字节。"""
    monkeypatch.setenv("MINERU_CLOUD_API_TOKEN", "test-token")
    monkeypatch.setattr(server, "slots", asyncio.Semaphore(10))
    monkeypatch.setattr(server, "result_slots", asyncio.Semaphore(2))
    active = peak = 0
    full = asyncio.Event()
    finish = asyncio.Event()

    async def download(self: MinerUCloudClient, url: str) -> bytes:
        """阻塞结果下载，观察资源槽是否在读取 ZIP 之前生效。"""
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == 2:
            full.set()
        await finish.wait()
        active -= 1
        return archive({"content_list.json": b'[{"type":"text","text":"ok"}]'})

    async def parse(self: MinerUCloudClient, pdf: bytes, **kwargs: Any) -> bytes:
        """官网已完成，调用服务注入的有界结果处理入口。"""
        assert self.result_handler is not None
        return await self.result_handler("https://mineru.net/test-only")

    monkeypatch.setattr(MinerUCloudClient, "parse", parse)
    monkeypatch.setattr(MinerUCloudClient, "download", download)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=server.app), base_url="http://test"
    ) as client:
        tasks = [asyncio.create_task(client.post(
            "/file_parse", files={"files": ("test.pdf", b"%PDF-test")}
        )) for _ in range(10)]
        try:
            await asyncio.wait_for(full.wait(), timeout=5)
            await asyncio.sleep(0.02)
            assert active == 2
        finally:
            finish.set()
            results = await asyncio.gather(*tasks)
        assert peak == 2
        assert all(result.status_code == 200 for result in results)
