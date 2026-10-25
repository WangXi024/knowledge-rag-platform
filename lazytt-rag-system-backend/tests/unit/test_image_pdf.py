"""图片 PDF 封装测试：像素、原图、确定性与非法输入边界。"""
from io import BytesIO

import pikepdf
import pytest
from PIL import Image

from rag_service.domain.errors import RagError
from rag_service.infrastructure.ragflow.image_pdf import image_to_pdf


@pytest.mark.parametrize('mode,format', [('RGB', 'JPEG'), ('RGB', 'PNG'), ('RGBA', 'PNG')])
def test_image_pdf_retains_pixels_and_original(mode: str, format: str) -> None:
    """透明图和 JPEG 均不缩放；附件逐字节保留原图，重试不会产生新摘要。"""
    source = BytesIO()
    Image.new(mode, (320, 160)).save(source, format=format)
    original = source.getvalue()
    result = image_to_pdf(original)
    assert result == image_to_pdf(original)
    with pikepdf.Pdf.open(BytesIO(result)) as pdf:
        assert len(pdf.pages) == 1
        assert pdf.attachments['original-image'].get_file().read_bytes() == original
        images = list(pdf.pages[0].get_images().values())
        assert len(images) == 1
        assert (int(images[0].Width), int(images[0].Height)) == (320, 160)


def test_image_pdf_rejects_invalid_and_large_images() -> None:
    """非法内容与过大图在提交云端前失败，不会静默退回其他解析器。"""
    with pytest.raises(RagError):
        image_to_pdf(b'not an image')
    source = BytesIO()
    Image.new('1', (4001, 4000)).save(source, format='PNG')
    with pytest.raises(RagError):
        image_to_pdf(source.getvalue())


def test_image_pdf_rejects_animation() -> None:
    """多帧 PNG 不能悄悄丢弃后续帧。"""
    source = BytesIO()
    Image.new('RGB', (16, 16), 'red').save(
        source, format='PNG', save_all=True,
        append_images=[Image.new('RGB', (16, 16), 'blue')],
    )
    with pytest.raises(RagError):
        image_to_pdf(source.getvalue())
