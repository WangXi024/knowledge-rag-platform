"""来源格式的位置语义：TXT/Markdown 无页布局，不改变 PDF 或图片。"""

import pytest

from rag_service.domain.models import is_pageless_text


@pytest.mark.parametrize(
    ("filename", "mime_type", "expected"),
    [
        ("fixture.TXT", "application/octet-stream", True),
        ("fixture.md", "application/octet-stream", True),
        ("fixture.markdown", "application/octet-stream", True),
        ("fixture", "Text/Plain; charset=utf-8", True),
        ("fixture", "text/markdown; charset=UTF-8", True),
        ("fixture", "text/x-markdown", True),
        ("fixture.pdf", "application/pdf", False),
        ("fixture.png", "image/png", False),
        ("fixture.csv", "text/csv", False),
        ("fixture.html", "text/html", False),
    ],
)
def test_pageless_text_is_explicit_and_preserves_other_source_formats(
    filename: str, mime_type: str, expected: bool,
) -> None:
    """后缀和带 charset 的 MIME 均可识别，但不把所有文本类型当成 TXT。"""
    assert is_pageless_text(filename, mime_type) is expected
