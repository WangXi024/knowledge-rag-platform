"""图谱环境边界回归：显式凭据、默认关闭和 ECS 一次性秘密传递。

只运行本机配置生成器和 Mock，不启动 Docker、不连接数据库或模型服务。生成文件
全部位于 pytest 临时目录，业务 Compose 不得包含图谱 Chat 三元组。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


def base_environment() -> dict[str, str]:
    """返回本地生成器和 ECS 预检所需的最小合成环境，禁止借用真实开发配置。"""
    return {
        "RAGFLOW_API_KEY": "synthetic-ragflow-key",
        "RAGFLOW_ADMIN_EMAIL": "fixture@example.test",
        "RAGFLOW_ADMIN_PASSWORD": "synthetic-admin-password",
        "RAGFLOW_IMAGE": "infiniflow/ragflow:v0.26.4",
        "RAGFLOW_POSTGRES_PASSWORD": "synthetic-postgres-key",
        "ELASTIC_PASSWORD": "synthetic-elastic-key",
        "MINIO_PASSWORD": "synthetic-minio-key",
        "REDIS_STACK_PASSWORD": "synthetic-redis-key",
        "LLM_BASE_URL": "https://gateway.test/v1",
        "LLM_API_KEY": "synthetic-gateway-key",
        "LLM_MODEL": "gateway-fixture",
        "EMBEDDING_BASE_URL": "https://embedding.test/v1",
        "EMBEDDING_API_KEY": "synthetic-embedding-key",
        "EMBEDDING_MODEL": "embedding-fixture",
        "VISION_BASE_URL": "https://vision.test/v1",
        "VISION_API_KEY": "synthetic-vision-key",
        "VISION_MODEL": "vision-fixture",
        "EMBEDDING_DIMENSION": "1024",
        "MEDIA_PROCESSING_ENABLED": "false",
        "RERANK_ENABLED": "false",
        "MINERU_CLOUD_API_TOKEN": "",
    }


@pytest.mark.parametrize("enabled", [False, True])
def test_local_environment_preserves_graph_configuration(tmp_path: Path, enabled: bool) -> None:
    """开启时保留显式图谱三元组，关闭时不强制提供；不借用 Gateway 的模型配置。"""
    source = tmp_path / ".env.ragflow"
    target = tmp_path / ".env.full"
    values = base_environment()
    if enabled:
        values.update(
            {
                "RAGFLOW_GRAPH_ENABLED": "true",
                "RAGFLOW_GRAPH_CHAT_BASE_URL": "https://graph.test/v1",
                "RAGFLOW_GRAPH_CHAT_API_KEY": "synthetic-graph-key",
                "RAGFLOW_GRAPH_CHAT_MODEL": "graph-fixture",
                "RAGFLOW_GRAPH_CHAT_MODEL_REF": "graph-fixture@registered@OpenAI-API-Compatible",
            }
        )
    source.write_text(
        "".join(f"{name}={value}\n" for name, value in values.items()), encoding="utf-8"
    )
    repository = Path(__file__).resolve().parents[2]
    subprocess.run(  # noqa: S603 - 固定仓库脚本与合成临时环境，不执行任意命令。
        [
            sys.executable,
            "tools/prepare_local_env.py",
            "--ragflow-env",
            str(source),
            "--output",
            str(target),
        ],
        cwd=repository,
        capture_output=True,
        check=True,
    )
    generated = {
        name: value
        for line in target.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
        for name, value in [line.split("=", 1)]
    }
    assert generated["RAGFLOW_GRAPH_ENABLED"] == str(enabled).lower()
    assert generated["RAGFLOW_GRAPH_CHAT_MODEL"] == ("graph-fixture" if enabled else "")
    assert generated["RAGFLOW_GRAPH_CHAT_API_KEY"] == ("synthetic-graph-key" if enabled else "")
    if enabled:
        assert generated["RAGFLOW_GRAPH_CHAT_MODEL_REF"] == values["RAGFLOW_GRAPH_CHAT_MODEL_REF"]


def test_enabled_local_graph_rejects_missing_separate_key(tmp_path: Path) -> None:
    """Gateway Key 已配置仍不能替代图谱 Key；失败必须发生在环境写回之前。"""
    source = tmp_path / ".env.ragflow"
    target = tmp_path / ".env.full"
    values = base_environment()
    values.update(
        {
            "RAGFLOW_GRAPH_ENABLED": "true",
            "RAGFLOW_GRAPH_CHAT_BASE_URL": "https://graph.test/v1",
            "RAGFLOW_GRAPH_CHAT_MODEL": "graph-fixture",
        }
    )
    source.write_text(
        "".join(f"{name}={value}\n" for name, value in values.items()), encoding="utf-8"
    )
    result = subprocess.run(  # noqa: S603 - 固定配置生成器，无外部副作用。
        [
            sys.executable,
            "tools/prepare_local_env.py",
            "--ragflow-env",
            str(source),
            "--output",
            str(target),
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "RAGFLOW_GRAPH_CHAT_API_KEY" in result.stderr
    assert not target.exists()


def test_business_compose_has_graph_reference_without_graph_credentials() -> None:
    """Graph Chat 的 Key 和 URL 只交模型登记命令，不得从共享环境泄入普通业务进程。"""
    repository = Path(__file__).resolve().parents[2]
    for filename in ("compose.local.yml", "compose.dev.yml"):
        compose = yaml.safe_load((repository / filename).read_text(encoding="utf-8"))
        for name in ("rag-gateway", "rag-worker", "rag-scheduler", "rag-app-bootstrap"):
            environment = compose["services"][name]["environment"]
            assert "RAGFLOW_GRAPH_ENABLED" in environment
            assert "RAGFLOW_GRAPH_CHAT_MODEL_REF" in environment
            for secret in ("BASE_URL", "API_KEY", "MODEL"):
                assert f"RAGFLOW_GRAPH_CHAT_{secret}" not in environment


def test_ecs_graph_model_credentials_only_reach_explicit_bootstrap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ECS 仅通过 --env 变量名把三元组交给一次性进程，命令参数不含合成密钥。"""
    pytest.importorskip("fcntl", reason="ECS 发布器只在 POSIX 主机运行")
    from deploy.ecs import release

    monkeypatch.setenv("RAGFLOW_GRAPH_ENABLED", "true")
    monkeypatch.setenv("RAGFLOW_GRAPH_CHAT_API_KEY", "synthetic-secret-value")
    monkeypatch.setattr(release, "validate_configuration_file", lambda _path: None)
    monkeypatch.setattr(release, "save_configuration", lambda _path, _values: None)
    calls: list[list[str]] = []

    def run(command: list[str], *, input_text: str | None = None) -> str:
        """仅记录命令形状，不执行 Docker，也不把管理员环境内容复制进参数。"""
        del input_text
        calls.append(command)
        return json.dumps({"RAGFLOW_GRAPH_CHAT_MODEL_REF": "registered-ref"})

    monkeypatch.setattr(release, "run", run)
    release.bootstrap(tmp_path, "bootstrap-models", tmp_path / "rag.env")
    command = calls[0]
    for name in ("BASE_URL", "API_KEY", "MODEL"):
        position = command.index(f"RAGFLOW_GRAPH_CHAT_{name}")
        assert command[position - 1] == "--env"
    assert "synthetic-secret-value" not in " ".join(command)
