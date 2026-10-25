#!/usr/bin/env python3
"""为本地 RAGFlow 幂等创建管理账号和稳定 API Key。

这个脚本由 ``deploy/local/start-full.sh`` 在 RAGFlow Server 健康后调用。它只使用
RAGFlow v0.26.4 已提供的用户、登录和 Token HTTP API，绝不直接读写 RAGFlow
PostgreSQL，也不会修改管理员密码。脚本会先验证 ``deploy/local/.env.ragflow`` 中已有的
API Key；Key 可用时直接结束，Key 为空或失效时才登录现有账号，账号不存在时才注册，
最后复用或创建一个可验证的 API Key 并原子写回 Git 忽略的环境文件。

密码在发送给 RAGFlow 前，必须使用正在运行的官方 RAGFlow 容器内置 RSA 工具加密。
这样既保持与管理网页完全相同的协议，也避免在本仓库复制或维护 RAGFlow 私钥。
终端输出只包含动作摘要，不包含密码、Cookie、访问 Token 或 API Key。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


class AccountBootstrapFailure(RuntimeError):
    """表示账号/API Key 初始化无法在不破坏现有数据的前提下继续。"""


@dataclass(frozen=True)
class RagflowEnvelope:
    """保存 RAGFlow 统一响应中允许向后续逻辑传递的非敏感字段。"""

    code: int
    data: Any
    message: str


def parse_env_file(path: Path) -> tuple[list[str], dict[str, str]]:
    """读取简单 ``KEY=VALUE`` 文件，同时保留原始行序供原子最小更新。

    环境文件不会通过 Shell ``source`` 执行，因此用户密码或 Key 中即使含有特殊字符，
    也只会作为普通文本处理。调用方负责保证文件已经存在且只属于当前本机。
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    values: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in raw_line:
            continue
        name, value = raw_line.split("=", 1)
        values[name.strip()] = value.strip()
    return lines, values


def update_env_api_key(path: Path, api_key: str) -> None:
    """只替换 ``RAGFLOW_API_KEY``，不触碰用户填写的任何密码。

    新内容先写入同目录权限为 ``0600`` 的临时文件，刷新并同步到磁盘后再原子替换。
    即使进程在写入中途退出，原环境文件也仍然完整；成功后文件不会因为原先权限过宽
    而继续暴露本地凭据。
    """
    lines, _ = parse_env_file(path)
    rendered: list[str] = []
    replaced = False
    for line in lines:
        if line and not line.lstrip().startswith("#") and "=" in line:
            name = line.split("=", 1)[0].strip()
            if name == "RAGFLOW_API_KEY":
                rendered.append(f"RAGFLOW_API_KEY={api_key}")
                replaced = True
                continue
        rendered.append(line)
    if not replaced:
        if rendered and rendered[-1]:
            rendered.append("")
        rendered.append(f"RAGFLOW_API_KEY={api_key}")

    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=".env.ragflow.bootstrap.",
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


def _required(values: dict[str, str], name: str) -> str:
    """取得已配置值，并拒绝空值或示例占位符进入账号初始化请求。"""
    value = values.get(name, "").strip()
    if not value or value.startswith("change-me"):
        raise AccountBootstrapFailure(f"{name} must be configured before account bootstrap")
    return value


def decode_envelope(response: httpx.Response, operation: str) -> RagflowEnvelope:
    """解析 RAGFlow 统一响应，但错误中不回显可能包含 Token 的完整 Payload。"""
    try:
        payload = response.json()
    except ValueError as exc:
        raise AccountBootstrapFailure(
            f"{operation}: RAGFlow returned invalid JSON (HTTP {response.status_code})"
        ) from exc
    if not isinstance(payload, dict):
        raise AccountBootstrapFailure(f"{operation}: RAGFlow response is not an object")
    raw_code = payload.get("code", -1)
    if not isinstance(raw_code, int):
        raise AccountBootstrapFailure(f"{operation}: RAGFlow response code is invalid")
    message = str(payload.get("message", ""))
    return RagflowEnvelope(code=raw_code, data=payload.get("data"), message=message)


def require_success(response: httpx.Response, operation: str) -> Any:
    """要求 HTTP 和 RAGFlow 业务状态同时成功，并返回 ``data`` 字段。"""
    envelope = decode_envelope(response, operation)
    if not response.is_success or envelope.code != 0:
        safe_message = envelope.message[:240] or "no message"
        raise AccountBootstrapFailure(
            f"{operation}: HTTP {response.status_code}, code {envelope.code}: {safe_message}"
        )
    return envelope.data


def verify_api_key(client: httpx.Client, api_key: str, expected_email: str) -> bool:
    """验证 API Key 是否属于配置中的管理员，而不是只检查字符串是否非空。

    401/403 或明确的鉴权业务错误表示 Key 已失效，可以安全进入登录恢复流程；网络
    错误、5xx 和协议漂移则立即失败，避免在短暂故障时重复创建 Token。
    """
    if not api_key or api_key.startswith(("change-me", "pending-local-initialization")):
        return False
    # 登录/注册恢复流程会让 ``client`` 持有会话 Cookie。如果直接调用 ``client.get``，
    # RAGFlow 可能通过 Cookie 而不是 Bearer Key 完成鉴权，进而把无效 Key 误判为有效。
    # 因此先构造请求，再显式删除 Cookie；``send`` 不会重新执行 Cookie 合并，验证结果
    # 只能来自待检查的 API Key。客户端本身的 Cookie 仍保留给后续 Token 管理 API。
    request = client.build_request(
        "GET",
        "/api/v1/users/me",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    request.headers.pop("cookie", None)
    response = client.send(request)
    if response.status_code in {401, 403}:
        return False
    envelope = decode_envelope(response, "verify_api_key")
    if response.status_code >= 500:
        raise AccountBootstrapFailure(
            f"verify_api_key: RAGFlow is unavailable (HTTP {response.status_code})"
        )
    if not response.is_success or envelope.code != 0:
        lowered = envelope.message.lower()
        if "auth" in lowered or "unauthorized" in lowered or "token" in lowered:
            return False
        raise AccountBootstrapFailure(
            f"verify_api_key: unexpected RAGFlow failure code {envelope.code}"
        )
    if not isinstance(envelope.data, dict):
        raise AccountBootstrapFailure("verify_api_key: user profile is not an object")
    actual_email = str(envelope.data.get("email", ""))
    if actual_email != expected_email:
        raise AccountBootstrapFailure(
            "configured RAGFLOW_API_KEY belongs to a different RAGFlow account"
        )
    return True


def ensure_authenticated_session(
    client: httpx.Client,
    *,
    email: str,
    encrypted_password: str,
) -> str:
    """登录已有账号；仅在服务明确报告邮箱未注册时创建新账号。

    密码不匹配、账号被停用或注册功能不可用都属于需要用户修复的真实错误，脚本不会
    重置密码或直接修改数据库。返回值只描述 ``logged_in``/``registered`` 动作，不含
    会话 Cookie 或 RAGFlow 内部用户 ID。
    """
    login_response = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": encrypted_password},
    )
    login_envelope = decode_envelope(login_response, "login_admin")
    if login_response.is_success and login_envelope.code == 0:
        if not isinstance(login_envelope.data, dict) or login_envelope.data.get("email") != email:
            raise AccountBootstrapFailure("login_admin: RAGFlow returned an unexpected account")
        return "logged_in"

    if "not registered" not in login_envelope.message.lower():
        safe_message = login_envelope.message[:240] or "authentication failed"
        raise AccountBootstrapFailure(f"login_admin: {safe_message}")

    register_data = require_success(
        client.post(
            "/api/v1/users",
            json={
                "nickname": "LazyTT Admin",
                "email": email,
                "password": encrypted_password,
            },
        ),
        "register_admin",
    )
    if not isinstance(register_data, dict) or register_data.get("email") != email:
        raise AccountBootstrapFailure("register_admin: RAGFlow returned an unexpected account")
    return "registered"


def reuse_or_create_api_key(client: httpx.Client, *, email: str) -> tuple[str, str]:
    """在当前登录会话中优先复用已有有效 Key，没有时才创建一个。

    RAGFlow 的 Token 列表属于当前登录租户。逐个验证后复用可以避免每次启动都新增
    Token；只有列表中没有可用项时才调用创建接口。返回值中的 Key 只供写入受限环境
    文件，调用方不得打印。
    """
    token_data = require_success(client.get("/api/v1/system/tokens"), "list_api_keys")
    if not isinstance(token_data, list):
        raise AccountBootstrapFailure("list_api_keys: RAGFlow data is not a list")
    for item in reversed(token_data):
        if not isinstance(item, dict):
            continue
        candidate = str(item.get("token", "")).strip()
        if candidate and verify_api_key(client, candidate, email):
            return candidate, "reused_existing"

    created = require_success(
        client.post("/api/v1/system/tokens"),
        "create_api_key",
    )
    if not isinstance(created, dict):
        raise AccountBootstrapFailure("create_api_key: RAGFlow data is not an object")
    api_key = str(created.get("token", "")).strip()
    if not api_key:
        raise AccountBootstrapFailure("create_api_key: RAGFlow returned an empty token")
    if not verify_api_key(client, api_key, email):
        raise AccountBootstrapFailure("create_api_key: new token verification failed")
    return api_key, "created"


def bootstrap_account(
    client: httpx.Client,
    *,
    email: str,
    password: str,
    configured_api_key: str,
    encrypt_password: Callable[[str], str],
) -> tuple[str, dict[str, object]]:
    """执行无副作用优先的账号和 API Key 幂等编排。

    已配置 Key 验证成功时不会发送密码、不会登录，也不会创建 Token。只有恢复流程
    才调用官方加密器并建立登录会话。返回的新 Key 与脱敏摘要分离，避免上层误打印。
    """
    if verify_api_key(client, configured_api_key, email):
        return configured_api_key, {
            "account_action": "not_required",
            "api_key_action": "reused_configured",
            "password_changed": False,
            "environment_updated": False,
        }

    encrypted_password = encrypt_password(password)
    account_action = ensure_authenticated_session(
        client,
        email=email,
        encrypted_password=encrypted_password,
    )
    api_key, api_key_action = reuse_or_create_api_key(client, email=email)
    return api_key, {
        "account_action": account_action,
        "api_key_action": api_key_action,
        "password_changed": False,
        "environment_updated": True,
    }


def encrypt_with_ragflow(
    *,
    repo_root: Path,
    compose_file: Path,
    full_env_file: Path,
    plaintext: str,
) -> str:
    """通过 Compose 服务执行官方 RSA 加密工具，标准输入承载明文且不进入命令行。"""
    command = [
        "docker",
        "compose",
        "--env-file",
        str(full_env_file),
        "-f",
        str(compose_file),
        "exec",
        "-T",
        "ragflow-server",
        "python",
        "-c",
        "import sys; from api.utils.crypt import crypt; print(crypt(sys.stdin.read()))",
    ]
    completed = subprocess.run(  # noqa: S603 - 固定 Docker/Compose 参数，不经过 Shell
        command,
        cwd=repo_root,
        input=plaintext,
        capture_output=True,
        check=False,
        text=True,
    )
    if completed.returncode != 0:
        raise AccountBootstrapFailure("RAGFlow official password encryption helper failed")
    encrypted = completed.stdout.strip()
    if not encrypted:
        raise AccountBootstrapFailure("RAGFlow official password encryption returned empty output")
    return encrypted


def build_parser() -> argparse.ArgumentParser:
    """定义本地私有环境、Compose 和 RAGFlow API 地址参数。"""
    parser = argparse.ArgumentParser(
        description="Create or reuse a local RAGFlow account API key without rotating passwords."
    )
    parser.add_argument("--env-file", type=Path, default=Path("deploy/local/.env.ragflow"))
    parser.add_argument("--full-env-file", type=Path, default=Path("deploy/local/.env.full"))
    parser.add_argument(
        "--compose-file",
        type=Path,
        default=Path("compose.local.yml"),
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:9380")
    return parser


def run() -> dict[str, object]:
    """加载本机配置、执行幂等初始化，并仅在需要时原子写入 API Key。"""
    args = build_parser().parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    env_path = (repo_root / args.env_file).resolve()
    full_env_path = (repo_root / args.full_env_file).resolve()
    compose_path = (repo_root / args.compose_file).resolve()
    for path, label in (
        (env_path, "RAGFlow environment"),
        (full_env_path, "full-stack environment"),
        (compose_path, "Compose file"),
    ):
        if not path.is_file():
            raise AccountBootstrapFailure(f"missing {label}: {path}")

    _, values = parse_env_file(env_path)
    email = _required(values, "RAGFLOW_ADMIN_EMAIL")
    password = _required(values, "RAGFLOW_ADMIN_PASSWORD")
    configured_api_key = values.get("RAGFLOW_API_KEY", "").strip()

    def encrypt_password(plaintext: str) -> str:
        """把当前运行上下文固定到本仓库 Compose，避免误操作其他容器。"""
        return encrypt_with_ragflow(
            repo_root=repo_root,
            compose_file=compose_path,
            full_env_file=full_env_path,
            plaintext=plaintext,
        )

    with httpx.Client(
        base_url=args.base_url.rstrip("/"),
        timeout=httpx.Timeout(connect=5, read=30, write=30, pool=5),
        follow_redirects=False,
    ) as client:
        api_key, summary = bootstrap_account(
            client,
            email=email,
            password=password,
            configured_api_key=configured_api_key,
            encrypt_password=encrypt_password,
        )
    if summary["environment_updated"]:
        update_env_api_key(env_path, api_key)
    return {
        "result": "ok",
        **summary,
        "registration_remains_enabled": True,
        "api_key_verified": True,
        "secrets_printed": False,
    }


def main() -> None:
    """输出不含 Secret 的机器可读摘要，并用非零退出码阻止半初始化服务继续启动。"""
    try:
        print(json.dumps(run(), ensure_ascii=False, indent=2, sort_keys=True))
    except (AccountBootstrapFailure, httpx.HTTPError) as exc:
        raise SystemExit(f"RAGFlow account bootstrap failed: {exc}") from exc


if __name__ == "__main__":
    main()
