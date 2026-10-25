#!/usr/bin/env python3
"""为首次本地部署补齐 RAGFlow 私有环境中的随机密码和默认邮箱。

用户从示例复制 ``deploy/local/.env.ragflow`` 后，只需填写各模型能力的通用
``BASE_URL``、``API_KEY`` 和 ``MODEL``。七个本地基础设施密码如果为空或仍是
``change-me`` 占位符，本工具会各自生成一个互不相同的高强度 URL-safe 随机值。
旧环境中的 ``RAGFLOW_REDIS_PASSWORD`` 会被原值迁移为统一的
``REDIS_STACK_PASSWORD``，不会因为合并 Redis 而轮换密码。

工具只在首次初始化时生成缺失密码，已经配置的密码永远原样保留。没有稳定
``RAGFLOW_API_KEY`` 的旧首次部署如果仍使用会与 RAGFlow v0.26.4 内置管理员冲突的
``admin@ragflow.io``，会迁移到项目固定邮箱 ``admin@lazytt.io``；已经完成初始化的
环境不会更换邮箱。所有写入均为原子替换，文件权限固定为 ``0600``，终端不会输出
密码、云端 Key 或 RAGFlow API Key。
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import tempfile
from pathlib import Path

DEFAULT_ADMIN_EMAIL = "admin@lazytt.io"
LEGACY_CONFLICTING_ADMIN_EMAIL = "admin@ragflow.io"
PASSWORD_FIELDS = (
    "POSTGRES_ADMIN_PASSWORD",
    "APP_POSTGRES_PASSWORD",
    "RAGFLOW_POSTGRES_PASSWORD",
    "ELASTIC_PASSWORD",
    "MINIO_PASSWORD",
    "REDIS_STACK_PASSWORD",
    "RAGFLOW_ADMIN_PASSWORD",
)
LEGACY_REDIS_PASSWORD_FIELDS = (
    "RAGFLOW_REDIS_PASSWORD",
    "CACHE_REDIS_PASSWORD",
    "APP_REDIS_PASSWORD",
)


def _parse_env(path: Path) -> tuple[list[str], dict[str, str]]:
    """读取简单 ``KEY=VALUE`` 文件并保留原始行，避免重排用户的其他配置。"""
    lines = path.read_text(encoding="utf-8").splitlines()
    values: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in raw_line:
            continue
        name, value = raw_line.split("=", 1)
        values[name.strip()] = value.strip()
    return lines, values


def _is_placeholder(value: str) -> bool:
    """判断字段是否尚未配置；真实值即使较短也不会被工具擅自替换。"""
    normalized = value.strip()
    return not normalized or normalized.startswith("change-me")


def _has_stable_api_key(value: str) -> bool:
    """区分已验证的稳定 Key 与首次启动阶段的空值/占位值。"""
    normalized = value.strip()
    return bool(normalized) and not normalized.startswith(
        ("change-me", "pending-local-initialization")
    )


def _random_password(existing: set[str]) -> str:
    """生成不含 Shell 特殊语义的高熵密码，并保证七个字段不会复用同一值。"""
    while True:
        candidate = secrets.token_urlsafe(32)
        if candidate not in existing:
            existing.add(candidate)
            return candidate


def _atomic_update(
    path: Path,
    lines: list[str],
    replacements: dict[str, str],
    *,
    removals: frozenset[str] = frozenset(),
) -> None:
    """替换指定字段并删除已废弃字段，再原子覆盖且收紧权限。

    ``removals`` 只用于已明确废弃的 Redis 三实例密码名。删除的是
    Git 忽略环境文件中的重复字段，真实值已先复用到统一字段，不会
    输出到终端或因迁移而丢失。
    """
    rendered: list[str] = []
    remaining = dict(replacements)
    for line in lines:
        if line and not line.lstrip().startswith("#") and "=" in line:
            name = line.split("=", 1)[0].strip()
            if name in removals:
                continue
            if name in remaining:
                rendered.append(f"{name}={remaining.pop(name)}")
                continue
        rendered.append(line)
    if remaining and rendered and rendered[-1]:
        rendered.append("")
    rendered.extend(f"{name}={value}" for name, value in remaining.items())

    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=".env.ragflow.prepare.",
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write("\n".join(rendered) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def prepare_environment(path: Path) -> dict[str, object]:
    """补齐首次部署的本地密码，并返回完全不含 Secret 的动作摘要。

    参数 ``path`` 是 Git 忽略的本机 RAGFlow 环境文件。函数不会识别、复制或迁移
    任何厂商专用模型字段；模型配置只由通用字段负责。返回值只包含字段名、数量和
    动作名称，供启动脚本安全展示，不会把密码、API Key 或 Token 写入终端。
    """
    lines, values = _parse_env(path)
    replacements: dict[str, str] = {}
    generated_password_fields: list[str] = []
    redis_password_migrated = False

    # 优先复用旧 RAGFlow Redis 的密码，让已部署用户在首次启动新拓扑时
    # 不需要再填一个密码。后两个候选只用于兼容曾手工把它们写进
    # .env.ragflow 的早期环境；所有候选都是本地私有值，不会打印。
    if _is_placeholder(values.get("REDIS_STACK_PASSWORD", "")):
        legacy_password = next(
            (
                values.get(name, "").strip()
                for name in LEGACY_REDIS_PASSWORD_FIELDS
                if not _is_placeholder(values.get(name, ""))
            ),
            "",
        )
        if legacy_password:
            replacements["REDIS_STACK_PASSWORD"] = legacy_password
            redis_password_migrated = True

    existing_passwords = {
        values.get(name, "").strip()
        for name in PASSWORD_FIELDS
        if not _is_placeholder(values.get(name, ""))
    }
    for name in PASSWORD_FIELDS:
        if name in replacements:
            continue
        if _is_placeholder(values.get(name, "")):
            replacements[name] = _random_password(existing_passwords)
            generated_password_fields.append(name)

    # 早期示例把服务账号设成 RAGFlow Admin Server 会预占的邮箱。只有尚未取得稳定
    # API Key 的首次部署才自动迁移，避免改变已运行环境中的账号归属。
    current_email = values.get("RAGFLOW_ADMIN_EMAIL", "").strip()
    stable_api_key = _has_stable_api_key(values.get("RAGFLOW_API_KEY", ""))
    if not stable_api_key and (
        _is_placeholder(current_email) or current_email == LEGACY_CONFLICTING_ADMIN_EMAIL
    ):
        replacements["RAGFLOW_ADMIN_EMAIL"] = DEFAULT_ADMIN_EMAIL
        email_action = (
            "migrated_legacy_default"
            if current_email == LEGACY_CONFLICTING_ADMIN_EMAIL
            else "set_default"
        )
    else:
        email_action = "preserved"

    legacy_fields_present = frozenset(LEGACY_REDIS_PASSWORD_FIELDS) & values.keys()
    if replacements or legacy_fields_present:
        _atomic_update(
            path,
            lines,
            replacements,
            removals=frozenset(legacy_fields_present),
        )
    else:
        # 即使没有字段变化，也修复用户复制文件后可能继承的过宽权限。
        os.chmod(path, 0o600)

    return {
        "result": "ok",
        "generated_password_fields": generated_password_fields,
        "generated_password_count": len(generated_password_fields),
        "existing_passwords_preserved": len(PASSWORD_FIELDS) - len(generated_password_fields),
        "passwords_rotated": False,
        "redis_password_action": (
            "migrated_legacy_value" if redis_password_migrated else "preserved_or_generated"
        ),
        "admin_email_action": email_action,
        "admin_email": replacements.get("RAGFLOW_ADMIN_EMAIL", current_email),
        "model_configuration": "preserved",
        "file_mode": "0600",
        "secrets_printed": False,
    }


def build_parser() -> argparse.ArgumentParser:
    """定义唯一私有环境文件参数，便于测试和非默认目录复用。"""
    parser = argparse.ArgumentParser(
        description="Generate missing local RAGFlow passwords without rotating existing values."
    )
    parser.add_argument("--env-file", type=Path, default=Path("deploy/local/.env.ragflow"))
    return parser


def main() -> None:
    """解析仓库相对路径，执行准备并输出不含 Secret 的 JSON 摘要。"""
    args = build_parser().parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    path = args.env_file if args.env_file.is_absolute() else repo_root / args.env_file
    if not path.is_file():
        raise SystemExit(
            "missing deploy/local/.env.ragflow; "
            "copy deploy/local/.env.ragflow.example and fill API keys first"
        )
    print(
        json.dumps(
            prepare_environment(path.resolve()),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
