"""契约测试：运行时 FastAPI 生成的路径/方法必须与版本化 OpenAPI 规格精确一致。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from rag_service.api.app import create_app
from rag_service.settings import ServiceSettings

HTTP_METHODS = frozenset({"get", "post", "put", "patch", "delete"})


def _operations(document: dict[str, Any]) -> set[tuple[str, str]]:
    paths = document.get("paths")
    assert isinstance(paths, dict)
    return {
        (str(path), str(method).lower())
        for path, value in paths.items()
        if isinstance(value, dict)
        for method in value
        if str(method).lower() in HTTP_METHODS
    }


def test_runtime_openapi_has_exactly_the_specified_paths_and_methods(
    service_settings: ServiceSettings,
) -> None:
    root = Path(__file__).resolve().parents[2]
    specification = yaml.safe_load(
        (root / "rag-development-spec/openapi/rag-api-v1.yaml").read_text(
            encoding="utf-8"
        )
    )
    app = create_app(
        service_settings,
        offline_service=object(),  # type: ignore[arg-type]
        online_service=object(),  # type: ignore[arg-type]
    )

    assert _operations(app.openapi()) == _operations(specification)
