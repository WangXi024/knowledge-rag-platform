"""结构化 JSON 日志配置。

日志只接受显式白名单中的附加字段，避免把 User ID、原始查询、Prompt 或其他敏感对象
随着 ``extra`` 意外写入日志。
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

# 允许被写入 JSON 的业务诊断字段；新增字段前应先评估是否可能包含敏感信息。
_ALLOWED_EXTRA_FIELDS = (
    "request_id",
    "job_id",
    "path",
    "method",
    "status_code",
    "error_code",
    "duration_ms",
    "candidate_count",
    "context_count",
    "cache_type",
)


class JsonFormatter(logging.Formatter):
    """把标准日志记录转换为单行、UTF-8 友好的 JSON。"""
    def format(self, record: logging.LogRecord) -> str:
        """生成固定基础字段，并从记录中拷贝非空的白名单扩展字段。"""
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for field_name in _ALLOWED_EXTRA_FIELDS:
            value = getattr(record, field_name, None)
            if value is not None:
                payload[field_name] = value
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def configure_structured_logging(level: str) -> None:
    """为项目日志器安装一次 JSON 输出处理器。

    重复调用是安全的：自定义标记用于发现已安装的处理器，避免热加载或多次创建
    FastAPI 应用后同一条日志被打印多遍。
    """
    logger = logging.getLogger("rag_service")
    logger.setLevel(level.upper())
    logger.propagate = False
    if any(getattr(handler, "_rag_json_handler", False) for handler in logger.handlers):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    handler._rag_json_handler = True  # type: ignore[attr-defined]
    logger.addHandler(handler)
