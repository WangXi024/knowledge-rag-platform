"""通用模型配置生成测试。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from tools.prepare_local_env import _render_grouped_environment, resolve_gateway_bind_address


def _read_env(path: Path) -> dict[str, str]:
    """读取测试生成的简单 KEY=VALUE 文件；测试值均为合成字符串。"""
    return {
        name: value
        for line in path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
        for name, value in [line.split("=", 1)]
    }


def test_generic_model_configuration_is_copied_without_vendor_fields(
    tmp_path: Path,
) -> None:
    """生成器复制通用模型三元组，并为统一 Redis 派生三个逻辑 DB。"""
    repo_root = Path(__file__).resolve().parents[2]
    ragflow_env = tmp_path / ".env.ragflow"
    output = tmp_path / ".env.full"
    ragflow_env.write_text(
        "\n".join(
            [
                "RAGFLOW_API_KEY=fixture-ragflow-key",
                "RAGFLOW_ADMIN_EMAIL=admin@example.test",
                "RAGFLOW_ADMIN_PASSWORD=fixture-admin-password",
                "POSTGRES_ADMIN_USER=fixture-postgres-admin",
                "POSTGRES_ADMIN_PASSWORD=fixture-postgres-admin-password",
                "APP_POSTGRES_PASSWORD=fixture-app-postgres",
                "RAGFLOW_POSTGRES_PASSWORD=fixture-ragflow-postgres",
                "ELASTIC_PASSWORD=fixture-elastic",
                "MINIO_PASSWORD=fixture-minio",
                "REDIS_STACK_PASSWORD=fixture-redis-stack",
                "REDIS_STACK_IMAGE=redis/redis-stack-server:7.4.0-v8",
                "REDIS_STACK_MAXMEMORY=1gb",
                "RAGFLOW_IMAGE=infiniflow/ragflow:v0.26.4",
                "RAGFLOW_PLATFORM=linux/amd64",
                # 模拟用户在持久私有文件中显式开启局域网 Gateway。
                "RAG_APP_BIND_ADDRESS=0.0.0.0",
                "LLM_BASE_URL=https://chat.example.test/v1",
                "LLM_API_KEY=fixture-chat-key",
                # 统一配置生成过程必须保留可选 MinerU 参数及引号。
                "MINERU_CLOUD_API_TOKEN='fixture-mineru-key'",
                "MINERU_CLOUD_MODEL=vlm",
                "LLM_MODEL=chat-model",
                "REWRITE_MODEL=rewrite-model",
                "VISION_BASE_URL=https://vision.example.test/v1",
                "VISION_API_KEY=fixture-vision-key",
                "VISION_MODEL=vision-model",
                "MEDIA_VISION_BASE_URL=https://media-vision.example.test/v1",
                "MEDIA_VISION_API_KEY=fixture-media-vision-key",
                "MEDIA_VISION_MODEL=media-vision-model",
                "EMBEDDING_BASE_URL=https://embedding.example.test/v1",
                "EMBEDDING_API_KEY=fixture-embedding-key",
                "EMBEDDING_MODEL=embedding-model",
                "EMBEDDING_DIMENSION=768",
                "RERANK_ENABLED=true",
                "RERANK_BASE_URL=https://rerank.example.test/v1",
                "RERANK_API_KEY=fixture-rerank-key",
                "RERANK_MODEL=rerank-model",
                "MEDIA_PROCESSING_ENABLED=true",
                "ASR_BASE_URL=https://asr.example.test/v1",
                "ASR_API_KEY=fixture-asr-key",
                "ASR_MODEL=asr-model",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    output.write_text("BUILD_COMMIT=fixture-old-build\n", encoding="utf-8")

    subprocess.run(  # noqa: S603 - 测试只调用仓库内固定脚本和 pytest 临时路径
        [
            sys.executable,
            str(repo_root / "tools" / "prepare_local_env.py"),
            "--ragflow-env",
            str(ragflow_env),
            "--output",
            str(output),
            "--force",
        ],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )

    values = _read_env(output)
    assert values["MINERU_CLOUD_API_TOKEN"] == "'fixture-mineru-key'"
    assert values["MINERU_CLOUD_MODEL"] == "vlm"
    assert values["LLM_BASE_URL"] == "https://chat.example.test/v1"
    assert values["LLM_API_KEY"] == "fixture-chat-key"
    assert values["EMBEDDING_API_KEY"] == "fixture-embedding-key"
    assert values["VISION_API_KEY"] == "fixture-vision-key"
    assert values["MEDIA_VISION_API_KEY"] == "fixture-media-vision-key"
    assert values["MEDIA_VISION_MODEL"] == "media-vision-model"
    assert values["RERANK_API_KEY"] == "fixture-rerank-key"
    assert values["APP_POSTGRES_PASSWORD"] == "fixture-app-postgres"
    assert values["POSTGRES_ADMIN_USER"] == "fixture-postgres-admin"
    assert values["POSTGRES_ADMIN_PASSWORD"] == "fixture-postgres-admin-password"
    assert values["POSTGRES_IMAGE"] == "postgres:18.4-bookworm"
    assert "@postgres:5432/rag_app" in values["APP_DATABASE_DSN"]
    assert values["RAGFLOW_POSTGRES_HOST"] == "postgres"
    assert values["REDIS_STACK_HOST"] == "redis-stack"
    assert values["REDIS_STACK_PASSWORD"] == "fixture-redis-stack"
    assert values["CACHE_REDIS_URL"].endswith("@redis-stack:6379/0")
    assert values["APP_REDIS_URL"].endswith("@redis-stack:6379/2")
    assert "APP_REDIS_PASSWORD" not in values
    assert "CACHE_REDIS_PASSWORD" not in values
    assert "RAGFLOW_REDIS_PASSWORD" not in values
    assert values["RAG_APP_BIND_ADDRESS"] == "0.0.0.0"  # noqa: S104
    assert values["RAGFLOW_EMBEDDING_MODEL"] == "pending-model-bootstrap"
    assert values["RAGFLOW_VISION_RUNTIME_MODEL"] == "vision-model"
    assert values["RAGFLOW_VISION_MODEL"] == "pending-model-bootstrap"
    assert values["RAGFLOW_REGISTER_ENABLED"] == "1"
    assert values["RAGFLOW_ADMIN_PASSWORD"] == "fixture-admin-password"
    assert values["LLM_MODEL"] == "chat-model"
    assert values["VISION_MODEL"] == "vision-model"
    assert not any(name.startswith(("SILICONFLOW_", "DASHSCOPE_", "KIMI_")) for name in values)


def test_gateway_bind_address_is_safe_by_default_and_rejects_unknown_values() -> None:
    """默认仅绑定本机，且错误地址必须在生成 Compose 环境前失败。

    这个测试防止后续重构把“开放到局域网”变成无条件默认，也防止拼错
    IP 后 Docker 才给出难以理解的端口绑定错误。
    """
    assert resolve_gateway_bind_address({}, {}) == "127.0.0.1"
    assert (
        resolve_gateway_bind_address(
            {"RAG_APP_BIND_ADDRESS": "0.0.0.0"},  # noqa: S104
            {"RAG_APP_BIND_ADDRESS": "127.0.0.1"},
        )
        == "0.0.0.0"  # noqa: S104
    )
    with pytest.raises(SystemExit, match="RAG_APP_BIND_ADDRESS must be one of"):
        resolve_gateway_bind_address({"RAG_APP_BIND_ADDRESS": "192.0.2.10"}, {})


def test_external_storage_environment_does_not_require_local_images(tmp_path: Path) -> None:
    """外部模式只生成连接配置，不携带四个不会启动的本地存储镜像。

    该测试使用合成地址和凭据，不访问真实数据库。它保护的契约是：公司数据库或
    独立演练 Compose 已经部署好后，用户不需要在 RAG 私有配置中继续维护数据库
    镜像版本；RAGFlow 自身的固定运行镜像仍必须保留。
    """
    repo_root = Path(__file__).resolve().parents[2]
    ragflow_env = tmp_path / ".env.ragflow"
    output = tmp_path / ".env.full"
    ragflow_env.write_text(
        "\n".join(
            [
                "RAG_STORAGE_MODE=external",
                "RAGFLOW_API_KEY=fixture-ragflow-key",
                "RAGFLOW_ADMIN_EMAIL=admin@example.test",
                "RAGFLOW_ADMIN_PASSWORD=fixture-admin-password",
                "POSTGRES_ADMIN_PASSWORD=unused-local-admin-password",
                "APP_POSTGRES_PASSWORD=fixture-app-postgres",
                "RAGFLOW_POSTGRES_PASSWORD=fixture-ragflow-postgres",
                "ELASTIC_PASSWORD=fixture-elastic",
                "MINIO_USER=fixture-minio-user",
                "MINIO_PASSWORD=fixture-minio-password",
                "REDIS_STACK_PASSWORD=fixture-redis-stack",
                "RAGFLOW_IMAGE=infiniflow/ragflow:v0.26.4",
                "RAGFLOW_PLATFORM=linux/amd64",
                "APP_DATABASE_DSN=postgresql+psycopg://fixture:password@db.example.test:5432/rag_app",
                "REDIS_STACK_HOST=redis.example.test",
                "REDIS_STACK_PORT=6379",
                "RAGFLOW_POSTGRES_HOST=db.example.test",
                "RAGFLOW_POSTGRES_PORT=5432",
                "RAGFLOW_ES_HOST=search.example.test",
                "RAGFLOW_ES_PORT=9200",
                "RAGFLOW_MINIO_HOST=objects.example.test",
                "RAGFLOW_MINIO_PORT=9000",
                "LLM_BASE_URL=https://chat.example.test/v1",
                "LLM_API_KEY=fixture-chat-key",
                "LLM_MODEL=chat-model",
                "VISION_BASE_URL=https://vision.example.test/v1",
                "VISION_API_KEY=fixture-vision-key",
                "VISION_MODEL=vision-model",
                "EMBEDDING_BASE_URL=https://embedding.example.test/v1",
                "EMBEDDING_API_KEY=fixture-embedding-key",
                "EMBEDDING_MODEL=embedding-model",
                "EMBEDDING_DIMENSION=768",
                "RERANK_ENABLED=false",
                "MEDIA_PROCESSING_ENABLED=false",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    subprocess.run(  # noqa: S603 - 仅调用仓库脚本和 pytest 临时文件
        [
            sys.executable,
            str(repo_root / "tools" / "prepare_local_env.py"),
            "--ragflow-env",
            str(ragflow_env),
            "--output",
            str(output),
            "--force",
        ],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )

    values = _read_env(output)
    assert values["RAG_STORAGE_MODE"] == "external"
    assert values["RAGFLOW_IMAGE"] == "infiniflow/ragflow:v0.26.4"
    assert values["RAGFLOW_POSTGRES_HOST"] == "db.example.test"
    assert values["RAGFLOW_ES_HOST"] == "search.example.test"
    assert values["RAGFLOW_MINIO_HOST"] == "objects.example.test"
    assert values["REDIS_STACK_HOST"] == "redis.example.test"
    for local_image_name in (
        "POSTGRES_IMAGE",
        "REDIS_STACK_IMAGE",
        "ELASTICSEARCH_IMAGE",
        "MINIO_IMAGE",
    ):
        assert local_image_name not in values


def test_generated_environment_groups_model_capabilities_in_stable_order() -> None:
    """模型配置按职责分隔，且每组三元组固定为 URL、Key、模型名。"""
    rendered = _render_grouped_environment(
        {
            "VISION_MODEL": "vision-model",
            "LLM_MODEL": "chat-model",
            "EMBEDDING_API_KEY": "embedding-key",
            "VISION_API_KEY": "vision-key",
            "MEDIA_VISION_API_KEY": "media-vision-key",
            "LLM_API_KEY": "chat-key",
            "RERANK_MODEL": "rerank-model",
            "EMBEDDING_MODEL": "embedding-model",
            "RERANK_API_KEY": "rerank-key",
            "VISION_BASE_URL": "https://vision.example/v1",
            "MEDIA_VISION_BASE_URL": "https://media-vision.example/v1",
            "MEDIA_VISION_MODEL": "media-vision-model",
            "LLM_BASE_URL": "https://chat.example/v1",
            "EMBEDDING_BASE_URL": "https://embedding.example/v1",
            "RERANK_BASE_URL": "https://rerank.example/v1",
        }
    )

    headings = [
        "# Chat 与 Query Rewrite",
        "# Embedding 与语义缓存",
        "# 图片理解与 OCR",
        "# 视频抽帧理解",
        "# Cross-Encoder Rerank",
    ]
    assert [rendered.index(heading) for heading in headings] == sorted(
        rendered.index(heading) for heading in headings
    )
    for prefix in ("LLM", "EMBEDDING", "VISION", "MEDIA_VISION", "RERANK"):
        assert (
            rendered.index(f"{prefix}_BASE_URL=")
            < rendered.index(f"{prefix}_API_KEY=")
            < rendered.index(f"{prefix}_MODEL=")
        )
