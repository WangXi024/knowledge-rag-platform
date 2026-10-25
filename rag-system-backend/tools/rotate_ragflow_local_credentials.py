"""轮换本地 RAGFlow 管理员密码与 API Key，并同步更新环境文件和数据库密文。"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import secrets
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import httpx


class CredentialFailure(RuntimeError):
    """凭据轮换任一步骤未满足安全或协议要求。"""
    pass


def parse_env_file(path: Path) -> tuple[list[str], dict[str, str]]:
    """保留原文件行序读取 KEY=VALUE，供最小化原子更新。"""
    lines = path.read_text(encoding="utf-8").splitlines()
    values: dict[str, str] = {}
    for line in lines:
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value
    return lines, values


def update_env_file(path: Path, updates: dict[str, str]) -> None:
    """在同目录临时文件中替换指定键并原子覆盖，保留其他行和私有权限。"""
    lines, _ = parse_env_file(path)
    remaining = dict(updates)
    rendered: list[str] = []
    for line in lines:
        if line and not line.startswith("#") and "=" in line:
            key = line.split("=", 1)[0]
            if key in remaining:
                rendered.append(f"{key}={remaining.pop(key)}")
                continue
        rendered.append(line)
    if remaining:
        if rendered and rendered[-1]:
            rendered.append("")
        for key, value in remaining.items():
            rendered.append(f"{key}={value}")

    mode = path.stat().st_mode & 0o777
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=".env.ragflow.",
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write("\n".join(rendered) + "\n")
        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def require_success(response: httpx.Response, operation: str) -> Any:
    """校验 RAGFlow HTTP/Envelope 成功并返回 data。"""
    try:
        payload = response.json()
    except ValueError as exc:
        raise CredentialFailure(
            f"{operation}: HTTP {response.status_code} returned invalid JSON"
        ) from exc
    if not response.is_success:
        raise CredentialFailure(f"{operation}: HTTP {response.status_code}: {payload!r}")
    if not isinstance(payload, dict) or payload.get("code") != 0:
        raise CredentialFailure(f"{operation}: RAGFlow business failure: {payload!r}")
    return payload.get("data")


def encrypt_with_ragflow(container: str, plaintext: str) -> str:
    """调用 RAGFlow 容器内官方加密实现生成数据库可用密码密文。"""
    command = [
        "docker",
        "exec",
        "-i",
        container,
        "python",
        "-c",
        ("import sys; from api.utils.crypt import crypt; print(crypt(sys.stdin.read()))"),
    ]
    completed = subprocess.run(  # noqa: S603 - fixed executable/argv, no shell
        command,
        input=plaintext,
        capture_output=True,
        check=False,
        text=True,
    )
    if completed.returncode != 0:
        raise CredentialFailure("RAGFlow official password encryption helper failed")
    encrypted = completed.stdout.strip()
    if not encrypted:
        raise CredentialFailure("RAGFlow password encryption returned empty output")
    return encrypted


def parse_args() -> argparse.Namespace:
    """定义环境文件、容器名和非交互轮换参数。"""
    parser = argparse.ArgumentParser(
        description="Rotate local RAGFlow password and replace a disposable API key"
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path("deploy/local/.env.ragflow"),
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:9380",
    )
    parser.add_argument(
        "--container",
        default="lazytt-ragflow-poc-ragflow-server-1",
    )
    return parser.parse_args()


def run() -> dict[str, Any]:
    """验证旧凭据、生成/接收新值、更新服务与数据库，再原子写回环境文件。"""
    args = parse_args()
    env_path = args.env_file.resolve()
    if not env_path.is_file():
        raise CredentialFailure(f"environment file does not exist: {env_path}")
    _, values = parse_env_file(env_path)
    old_api_key = values.get("RAGFLOW_API_KEY", "").strip()
    if not old_api_key:
        raise CredentialFailure("RAGFLOW_API_KEY is missing from local environment")

    current_password = values.get("RAGFLOW_ADMIN_PASSWORD", "")
    if not current_password:
        current_password = getpass.getpass("Current local RAGFlow administrator password: ")
    if not current_password:
        raise CredentialFailure("current administrator password is empty")

    email = values.get("RAGFLOW_ADMIN_EMAIL", "admin@lazytt.io")
    new_password = secrets.token_urlsafe(32)
    old_password_ciphertext = encrypt_with_ragflow(args.container, current_password)
    new_password_ciphertext = encrypt_with_ragflow(args.container, new_password)

    with httpx.Client(
        base_url=args.base_url.rstrip("/"),
        headers={"Authorization": f"Bearer {old_api_key}"},
        timeout=httpx.Timeout(connect=5, read=30, write=30, pool=5),
    ) as old_client:
        profile = require_success(old_client.get("/api/v1/users/me"), "verify_old_key")
        if not isinstance(profile, dict) or profile.get("email") != email:
            raise CredentialFailure("current API key does not belong to expected admin")

        token_data = require_success(
            old_client.post("/api/v1/system/tokens"),
            "create_stable_api_key",
        )
        if not isinstance(token_data, dict) or not token_data.get("token"):
            raise CredentialFailure("RAGFlow did not return a new API key")
        new_api_key = str(token_data["token"])

        require_success(
            old_client.patch(
                "/api/v1/users/me",
                json={
                    "password": old_password_ciphertext,
                    "new_password": new_password_ciphertext,
                },
            ),
            "rotate_admin_password",
        )

    with httpx.Client(
        base_url=args.base_url.rstrip("/"),
        headers={"Authorization": f"Bearer {new_api_key}"},
        timeout=httpx.Timeout(connect=5, read=30, write=30, pool=5),
    ) as new_client:
        require_success(new_client.get("/api/v1/users/me"), "verify_new_key")
        login_data = require_success(
            new_client.post(
                "/api/v1/auth/login",
                json={"email": email, "password": new_password_ciphertext},
            ),
            "verify_new_password",
        )
        if not isinstance(login_data, dict) or login_data.get("email") != email:
            raise CredentialFailure("new administrator password verification failed")

        # Persist the replacement credentials before revoking the old key so a
        # local filesystem error cannot leave the environment without a usable
        # recovery credential.
        update_env_file(
            env_path,
            {
                "RAGFLOW_ADMIN_EMAIL": email,
                "RAGFLOW_ADMIN_PASSWORD": new_password,
                "RAGFLOW_API_KEY": new_api_key,
            },
        )
        require_success(
            new_client.delete(f"/api/v1/system/tokens/{old_api_key}"),
            "delete_disposable_api_key",
        )
        require_success(new_client.get("/api/v1/users/me"), "verify_key_after_cleanup")
    return {
        "result": "ok",
        "admin_password_rotated": True,
        "stable_api_key_created": True,
        "disposable_api_key_deleted": True,
        "local_environment_updated": True,
        "secrets_printed": False,
    }


def main() -> None:
    """执行敏感轮换流程，只输出不含新密钥的摘要。"""
    try:
        print(json.dumps(run(), ensure_ascii=False, indent=2, sort_keys=True))
    except (CredentialFailure, httpx.HTTPError) as exc:
        raise SystemExit(f"RAGFlow credential rotation failed: {exc}") from exc


if __name__ == "__main__":
    main()
