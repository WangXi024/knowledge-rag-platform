"""独立内部 HTTP 服务：兼容 RAGFlow /file_parse，调用 MinerU 官网而非本地 OCR。

仅部署于后端容器网络，不发布宿主机端口。每个请求占一个有限并发槽，返回兼容 ZIP；
不接触 Gateway 数据库，不改变官方识别内容，不记录 Token、文件或签名链接。
"""

import asyncio
import logging
import os
from typing import Annotated

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, Response

from rag_service.infrastructure.mineru_cloud.archive import compatible_zip
from rag_service.infrastructure.mineru_cloud.client import MinerUCloudClient, MinerUError

app = FastAPI(title="MinerU cloud adapter", openapi_url=None)
# httpx 的 INFO 会包含签名 URL；此独立进程只保留不含请求地址的应用错误码。
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
# 每个进程使用部署指定的并发槽；默认保持 2，非法值在启动时失败，避免无限放行。
concurrency = int(os.environ.get("MINERU_CLOUD_MAX_CONCURRENCY", "2"))
if concurrency < 1:
    raise ValueError("MINERU_CLOUD_MAX_CONCURRENCY must be positive")
slots = asyncio.Semaphore(concurrency)
# 十个请求可等待官网，但默认只允许两份结果同时下载和解压，降低内存峰值。
result_concurrency = int(os.environ.get("MINERU_CLOUD_RESULT_CONCURRENCY", "2"))
if result_concurrency < 1:
    raise ValueError("MINERU_CLOUD_RESULT_CONCURRENCY must be positive")
result_slots = asyncio.Semaphore(result_concurrency)


def configuration() -> tuple[str, str]:
    """只从适配服务环境读取 Token 与模型；配置缺失时拒绝调用，不回退本地解析。"""
    token = os.environ.get("MINERU_CLOUD_API_TOKEN", "").strip()
    model = os.environ.get("MINERU_CLOUD_MODEL", "vlm")
    if not token or "<" in token or model not in {"pipeline", "vlm"}:
        raise HTTPException(503, "MINERU_CLOUD_NOT_CONFIGURED")
    return token, model


@app.get("/health/ready")
async def ready() -> dict[str, str]:
    """检查配置存在，不发送文档、不消耗官网额度；Token 有效性由真实解析验证。"""
    configuration()
    return {"status": "ok"}


@app.api_route("/openapi.json", methods=["GET", "HEAD"], include_in_schema=False)
async def probe() -> Response:
    """兼容 RAGFlow 使用 HEAD 探测 API 的行为；未配置时明确失败。"""
    configuration()
    return JSONResponse(app.openapi())


@app.post("/file_parse")
async def file_parse(
    files: Annotated[UploadFile, File()],
    backend: Annotated[str, Form()] = "pipeline",
    parse_method: Annotated[str, Form()] = "auto",
    lang_list: Annotated[str, Form()] = "ch",
    formula_enable: Annotated[bool, Form()] = True,
    table_enable: Annotated[bool, Form()] = True,
    response_format_zip: Annotated[bool, Form()] = True,
    start_page_id: Annotated[int, Form()] = 0,
    end_page_id: Annotated[int, Form()] = 99999,
) -> Response:
    """接收 RAGFlow 单份完整 PDF；异步等待官网结果，超时/失败返回固定错误码。

云端模型由部署配置选择；pipeline 是 RAGFlow 兼容层标识，不代表本机运算。
拒绝不支持的页范围与模式，避免悄悄改变用户要求；不会吞掉请求取消。
"""
    token, model = configuration()
    if (
        backend != "pipeline" or parse_method not in {"auto", "ocr"}
        or not response_format_zip or start_page_id != 0 or end_page_id != 99999
    ):
        raise HTTPException(422, "MINERU_UNSUPPORTED_OPTIONS")
    if slots.locked():
        raise HTTPException(503, "MINERU_CLOUD_BUSY")
    async with slots:
        pdf = await files.read(100 * 1024 * 1024 + 1)
        await files.close()
        if len(pdf) > 100 * 1024 * 1024:
            raise HTTPException(413, "MINERU_FILE_TOO_LARGE")
        if not pdf.startswith(b"%PDF-"):
            raise HTTPException(422, "MINERU_PDF_REQUIRED")
        try:
            async with httpx.AsyncClient(
                timeout=60, follow_redirects=False, trust_env=False,
            ) as http:
                cloud = MinerUCloudClient(http, token, model)
                async def download_result(url: str) -> bytes:
                    """结果转换槽覆盖下载与解压全程，不让下载字节在队列中积压。"""
                    async with result_slots:
                        raw = await cloud.download(url)
                        return await asyncio.to_thread(compatible_zip, raw)

                cloud.result_handler = download_result
                result = await cloud.parse(
                    pdf, language=lang_list, ocr=parse_method == "ocr",
                    formula=formula_enable, table=table_enable,
                )
            return Response(result, media_type="application/zip")
        except TimeoutError:
            raise HTTPException(504, "MINERU_CLOUD_TIMEOUT") from None
        except MinerUError as exc:
            raise HTTPException(502, str(exc)) from None
        except (httpx.HTTPError, ValueError):
            raise HTTPException(502, "MINERU_CLOUD_REQUEST_FAILED") from None
