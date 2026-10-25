"""将显式选择 MinerU 的静态图片包装为单页 PDF，供上传适配器调用。

不缩放、不做 OCR；原图字节作为附件保存，避免转换丢失原文件。输出不含时间戳，
同一输入在重试时保持一致，供现有上传回读与摘要校验使用。所有处理只发生在内存中。
"""

from io import BytesIO

import img2pdf  # type: ignore[import-untyped]
import pikepdf
from PIL import Image, UnidentifiedImageError

from rag_service.domain.errors import RagError

IMAGE_PDF_PROFILE = "mineru-image@1"


def image_to_pdf(content: bytes) -> bytes:
    """校验 PNG/JPEG 后无损封装；拒绝动画、坏图及过大像素，失败不调用云端。

    1600 万像素上限限制并发任务的解码内存；100 MiB 输出上限与 MinerU 适配器一致。
    PDF 内只保留一页，原始字节在 original-image 附件内，不增加解析页数。
    """
    try:
        with Image.open(BytesIO(content)) as image:
            if (
                image.format not in {"PNG", "JPEG"}
                or getattr(image, "n_frames", 1) != 1
                or image.width * image.height > 16_000_000
            ):
                raise ValueError("unsupported image")
            image.verify()
        converted = img2pdf.convert(content, nodate=True)
        with pikepdf.Pdf.open(BytesIO(converted)) as pdf:
            pdf.attachments["original-image"] = content
            output = BytesIO()
            pdf.save(output, deterministic_id=True)
        result = output.getvalue()
        if len(result) > 100 * 1024 * 1024:
            raise ValueError("PDF too large")
        return result
    except (
        ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombError,
        pikepdf.PdfError, img2pdf.ImageOpenError, img2pdf.AlphaChannelError,
    ) as exc:
        raise RagError(
            "RAG-DOC-004",
            "MinerU 图片策略仅支持有效的静态 PNG/JPEG，最多 1600 万像素，转换后不超过 100 MiB",
            http_status=422,
            retryable=False,
        ) from exc
