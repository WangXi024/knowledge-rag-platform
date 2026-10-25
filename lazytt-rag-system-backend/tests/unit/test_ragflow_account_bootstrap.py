"""RAGFlow 账号自动初始化回归测试。

所有 HTTP 请求都由 ``httpx.MockTransport`` 在进程内返回合成响应，不访问本机容器、
公网或真实账号。测试覆盖正常重复启动、已有账号首次取 Key、首次注册、错误密码保护和
环境文件原子更新，防止未来改动重新引入密码轮换、Token 泄露或重复创建。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import httpx
import pytest

# 运维脚本不会安装进线上 ``rag_service`` 包；测试按文件位置加载，既覆盖真实脚本，
# 又不改变 Gateway 镜像的模块边界。注册到 ``sys.modules`` 使 dataclass 能解析模块名。
_SCRIPT = Path(__file__).resolve().parents[2] / "tools" / "ragflow_account_bootstrap.py"
_SPEC = importlib.util.spec_from_file_location("ragflow_account_bootstrap", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
AccountBootstrapFailure = _MODULE.AccountBootstrapFailure
bootstrap_account = _MODULE.bootstrap_account
update_env_api_key = _MODULE.update_env_api_key


def _client(handler: httpx.MockTransport) -> httpx.Client:
    """创建带固定本地地址的同步测试客户端，保证请求路径与真实部署一致。"""
    return httpx.Client(base_url="http://127.0.0.1:9380", transport=handler)


def test_valid_configured_key_skips_password_and_account_calls() -> None:
    """重复启动遇到有效 Key 时，只验证身份，绝不能再次发送密码或创建 Token。"""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/api/v1/users/me"
        assert request.headers["Authorization"] == "Bearer fixture-configured-key"
        return httpx.Response(200, json={"code": 0, "data": {"email": "admin@example.test"}})

    def forbidden_encrypt(_password: str) -> str:
        raise AssertionError("有效 API Key 路径不应读取或加密管理员密码")

    with _client(httpx.MockTransport(handler)) as client:
        api_key, summary = bootstrap_account(
            client,
            email="admin@example.test",
            password="fixture-password",
            configured_api_key="fixture-configured-key",
            encrypt_password=forbidden_encrypt,
        )

    assert api_key == "fixture-configured-key"
    assert summary == {
        "account_action": "not_required",
        "api_key_action": "reused_configured",
        "password_changed": False,
        "environment_updated": False,
    }
    assert len(requests) == 1


def test_existing_account_creates_and_verifies_key_without_password_rotation() -> None:
    """Key 为空但账号存在时，应登录后创建 Key，密码只用于登录且从不被修改。"""
    requested_paths: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append((request.method, request.url.path))
        if request.url.path == "/api/v1/auth/login":
            assert json.loads(request.read()) == {
                "email": "admin@example.test",
                "password": "fixture-encrypted",
            }
            return httpx.Response(
                200,
                headers={"set-cookie": "session=fixture-session; Path=/"},
                json={"code": 0, "data": {"email": "admin@example.test"}},
            )
        if request.method == "GET" and request.url.path == "/api/v1/system/tokens":
            assert "session=fixture-session" in request.headers.get("cookie", "")
            return httpx.Response(200, json={"code": 0, "data": []})
        if request.method == "POST" and request.url.path == "/api/v1/system/tokens":
            return httpx.Response(
                200,
                json={"code": 0, "data": {"token": "fixture-created-key"}},
            )
        if request.url.path == "/api/v1/users/me":
            assert request.headers["Authorization"] == "Bearer fixture-created-key"
            # Key 验证必须剥离刚才登录产生的 Cookie，否则测试无法证明 Bearer Key 有效。
            assert "cookie" not in request.headers
            return httpx.Response(
                200,
                json={"code": 0, "data": {"email": "admin@example.test"}},
            )
        raise AssertionError(f"未预期请求：{request.method} {request.url.path}")

    with _client(httpx.MockTransport(handler)) as client:
        api_key, summary = bootstrap_account(
            client,
            email="admin@example.test",
            password="fixture-password",
            configured_api_key="",
            encrypt_password=lambda value: (
                "fixture-encrypted" if value == "fixture-password" else "wrong"
            ),
        )

    assert api_key == "fixture-created-key"
    assert summary["account_action"] == "logged_in"
    assert summary["api_key_action"] == "created"
    assert summary["password_changed"] is False
    assert ("PATCH", "/api/v1/users/me") not in requested_paths


def test_missing_account_registers_and_reuses_existing_token() -> None:
    """全新卷应通过官方注册 API 建号，并优先复用账号已有 Token 避免重复创建。"""
    create_token_called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal create_token_called
        if request.url.path == "/api/v1/auth/login":
            return httpx.Response(
                200,
                json={
                    "code": 100,
                    "message": "Email: admin@example.test is not registered!",
                    "data": False,
                },
            )
        if request.url.path == "/api/v1/users":
            body = json.loads(request.read())
            assert body["nickname"] == "LazyTT Admin"
            assert body["password"] == "fixture-encrypted"
            return httpx.Response(
                200,
                headers={"set-cookie": "session=registered-session; Path=/"},
                json={"code": 0, "data": {"email": "admin@example.test"}},
            )
        if request.method == "GET" and request.url.path == "/api/v1/system/tokens":
            return httpx.Response(
                200,
                json={"code": 0, "data": [{"token": "fixture-existing-key"}]},
            )
        if request.method == "POST" and request.url.path == "/api/v1/system/tokens":
            create_token_called = True
            raise AssertionError("存在有效 Token 时不应创建新 Token")
        if request.url.path == "/api/v1/users/me":
            # 注册响应同样会设置会话；复用 Token 时仍必须单独验证 Token 本身。
            assert "cookie" not in request.headers
            return httpx.Response(
                200,
                json={"code": 0, "data": {"email": "admin@example.test"}},
            )
        raise AssertionError(f"未预期请求：{request.method} {request.url.path}")

    with _client(httpx.MockTransport(handler)) as client:
        api_key, summary = bootstrap_account(
            client,
            email="admin@example.test",
            password="fixture-password",
            configured_api_key="",
            encrypt_password=lambda _value: "fixture-encrypted",
        )

    assert api_key == "fixture-existing-key"
    assert summary["account_action"] == "registered"
    assert summary["api_key_action"] == "reused_existing"
    assert create_token_called is False


def test_wrong_password_stops_without_registering_or_creating_token() -> None:
    """已有邮箱密码不匹配时必须失败，不能误判为新账号或重置用户密码。"""
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(
            200,
            json={"code": 100, "message": "Email and password do not match!", "data": False},
        )

    with _client(httpx.MockTransport(handler)) as client:
        with pytest.raises(AccountBootstrapFailure, match="do not match"):
            bootstrap_account(
                client,
                email="admin@example.test",
                password="wrong-password",
                configured_api_key="",
                encrypt_password=lambda _value: "fixture-encrypted",
            )

    assert paths == ["/api/v1/auth/login"]


def test_api_key_update_preserves_passwords_and_forces_private_permissions(tmp_path: Path) -> None:
    """写回 Key 只能改变一个字段，并把私有环境文件权限收紧为当前用户可读写。"""
    env_file = tmp_path / ".env.ragflow"
    env_file.write_text(
        "# fixture\n"
        "RAGFLOW_ADMIN_PASSWORD=fixture-admin-password\n"
        "RAGFLOW_POSTGRES_PASSWORD=fixture-postgres-password\n"
        "RAGFLOW_API_KEY=\n",
        encoding="utf-8",
    )
    os.chmod(env_file, 0o644)

    update_env_api_key(env_file, "fixture-new-key")

    content = env_file.read_text(encoding="utf-8")
    assert "RAGFLOW_API_KEY=fixture-new-key" in content
    assert "RAGFLOW_ADMIN_PASSWORD=fixture-admin-password" in content
    assert "RAGFLOW_POSTGRES_PASSWORD=fixture-postgres-password" in content
    assert env_file.stat().st_mode & 0o777 == 0o600
