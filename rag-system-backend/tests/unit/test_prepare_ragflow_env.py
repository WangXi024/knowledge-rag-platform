"""首次 RAGFlow 私有环境自动补齐测试。"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

# 运维脚本不安装进 Gateway 包，测试按真实文件位置加载，避免为了测试改变生产镜像边界。
_SCRIPT = Path(__file__).resolve().parents[2] / "tools" / "prepare_ragflow_env.py"
_SPEC = importlib.util.spec_from_file_location("prepare_ragflow_env", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
PASSWORD_FIELDS = _MODULE.PASSWORD_FIELDS
prepare_environment = _MODULE.prepare_environment


def _read_env(path: Path) -> dict[str, str]:
    """读取合成测试文件；测试内容不包含任何真实密码或平台 Key。"""
    return {
        name: value
        for line in path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#")
        for name, value in [line.split("=", 1)]
    }


def test_first_run_generates_seven_distinct_passwords_and_default_email(
    tmp_path: Path,
) -> None:
    """新用户只填云端 Key 时，七个密码应自动生成且不泄露到动作摘要。"""
    env_file = tmp_path / ".env.ragflow"
    env_file.write_text(
        "\n".join(
            [
                "RAGFLOW_ADMIN_EMAIL=admin@ragflow.io",
                "RAGFLOW_API_KEY=",
                "LLM_API_KEY=fixture-llm-key",
                "EMBEDDING_API_KEY=fixture-embedding-key",
                *(f"{name}=change-me-{index}" for index, name in enumerate(PASSWORD_FIELDS)),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    summary = prepare_environment(env_file)
    values = _read_env(env_file)
    generated = [values[name] for name in PASSWORD_FIELDS]

    assert summary["generated_password_count"] == len(PASSWORD_FIELDS)
    assert summary["passwords_rotated"] is False
    assert summary["admin_email_action"] == "migrated_legacy_default"
    assert values["RAGFLOW_ADMIN_EMAIL"] == "admin@lazytt.io"
    assert len(set(generated)) == len(PASSWORD_FIELDS)
    assert all(value and not value.startswith("change-me") for value in generated)
    assert values["LLM_API_KEY"] == "fixture-llm-key"
    assert values["EMBEDDING_API_KEY"] == "fixture-embedding-key"
    assert env_file.stat().st_mode & 0o777 == 0o600
    # 摘要只列字段名和数量，任何真实生成值都不能进入终端 JSON。
    assert not any(value in str(summary) for value in generated)


def test_repeat_run_preserves_all_existing_values(tmp_path: Path) -> None:
    """已有稳定 Key 的成熟环境重复启动时，不得改变邮箱或任何一个密码。"""
    env_file = tmp_path / ".env.ragflow"
    original_values = {
        "RAGFLOW_ADMIN_EMAIL": "admin@ragflow.io",
        "RAGFLOW_API_KEY": "fixture-stable-ragflow-key",
        **{name: f"fixture-existing-{index}" for index, name in enumerate(PASSWORD_FIELDS)},
    }
    env_file.write_text(
        "\n".join(f"{name}={value}" for name, value in original_values.items()) + "\n",
        encoding="utf-8",
    )
    os.chmod(env_file, 0o644)

    summary = prepare_environment(env_file)

    assert summary["generated_password_count"] == 0
    assert summary["existing_passwords_preserved"] == len(PASSWORD_FIELDS)
    assert summary["admin_email_action"] == "preserved"
    assert _read_env(env_file) == original_values
    assert env_file.stat().st_mode & 0o777 == 0o600


def test_failed_legacy_first_run_only_migrates_email(tmp_path: Path) -> None:
    """已手填密码但尚无 Key 的失败首启只换冲突邮箱，不重新生成用户密码。"""
    env_file = tmp_path / ".env.ragflow"
    passwords = {
        name: f"fixture-user-selected-{index}" for index, name in enumerate(PASSWORD_FIELDS)
    }
    env_file.write_text(
        "RAGFLOW_ADMIN_EMAIL=admin@ragflow.io\n"
        "RAGFLOW_API_KEY=\n"
        + "\n".join(f"{name}={value}" for name, value in passwords.items())
        + "\n",
        encoding="utf-8",
    )

    summary = prepare_environment(env_file)
    values = _read_env(env_file)

    assert summary["generated_password_count"] == 0
    assert summary["admin_email_action"] == "migrated_legacy_default"
    assert values["RAGFLOW_ADMIN_EMAIL"] == "admin@lazytt.io"
    assert {name: values[name] for name in PASSWORD_FIELDS} == passwords


def test_model_fields_are_preserved_without_vendor_alias_migration(tmp_path: Path) -> None:
    """密码准备工具不得解释或改写通用模型配置。"""
    env_file = tmp_path / ".env.ragflow"
    env_file.write_text(
        "RAGFLOW_ADMIN_EMAIL=admin@example.test\n"
        "RAGFLOW_API_KEY=fixture-stable-ragflow-key\n"
        "LLM_BASE_URL=https://models.example.test/v1\n"
        "LLM_API_KEY=fixture-generic-key\n"
        "LLM_MODEL=fixture-chat\n"
        + "\n".join(
            f"{name}=fixture-existing-{index}" for index, name in enumerate(PASSWORD_FIELDS)
        )
        + "\n",
        encoding="utf-8",
    )

    summary = prepare_environment(env_file)
    values = _read_env(env_file)

    assert summary["model_configuration"] == "preserved"
    assert values["LLM_API_KEY"] == "fixture-generic-key"
    assert not any(name.startswith(("SILICONFLOW_", "DASHSCOPE_")) for name in values)


def test_legacy_ragflow_redis_password_is_migrated_without_rotation(tmp_path: Path) -> None:
    """三 Redis 旧环境应复用原 RAGFlow 密码，并移除已废弃字段。"""
    env_file = tmp_path / ".env.ragflow"
    env_file.write_text(
        "RAGFLOW_ADMIN_EMAIL=admin@example.test\n"
        "RAGFLOW_API_KEY=fixture-stable-ragflow-key\n"
        "RAGFLOW_REDIS_PASSWORD=fixture-legacy-redis-password\n"
        + "\n".join(
            f"{name}=fixture-existing-{index}"
            for index, name in enumerate(PASSWORD_FIELDS)
            if name != "REDIS_STACK_PASSWORD"
        )
        + "\n",
        encoding="utf-8",
    )

    summary = prepare_environment(env_file)
    values = _read_env(env_file)

    assert summary["redis_password_action"] == "migrated_legacy_value"
    assert summary["generated_password_count"] == 0
    assert values["REDIS_STACK_PASSWORD"] == "fixture-legacy-redis-password"
    assert "RAGFLOW_REDIS_PASSWORD" not in values
    assert "APP_REDIS_PASSWORD" not in values
    assert "CACHE_REDIS_PASSWORD" not in values
