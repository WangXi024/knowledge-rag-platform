"""图片 Smoke 合成 OCR Fixture 的确定性回归测试。"""

from __future__ import annotations

import importlib.util
import struct
import sys
from pathlib import Path

# Smoke 工具不安装进 Gateway 包；测试按仓库真实路径加载，避免改变生产镜像边界。
_SCRIPT = Path(__file__).resolve().parents[2] / "tools" / "gateway_image_smoke.py"
_SPEC = importlib.util.spec_from_file_location("gateway_image_smoke", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
synthetic_ocr_text_png = _MODULE.synthetic_ocr_text_png


def test_synthetic_ocr_fixture_is_a_large_rgb_png() -> None:
    """Fixture 应保持足够大的高对比文字画布，防止回退成无文字纯色图。

    这里只验证标准库生成结果的 PNG 签名、宽高和 RGB 色彩类型；真实文字识别由
    ``gateway_image_smoke.py`` 联调 RAGFlow Picture Parser 与 Qwen OCR 验证。
    """
    payload = synthetic_ocr_text_png()

    assert payload.startswith(b"\x89PNG\r\n\x1a\n")
    width, height, bit_depth, color_type = struct.unpack(">IIBB", payload[16:26])
    assert (width, height) == (512, 160)
    assert bit_depth == 8
    assert color_type == 2
    # 纯白空图的压缩体积会非常小；最低体积断言保护点阵文字确实写入像素数据。
    assert len(payload) > 500
