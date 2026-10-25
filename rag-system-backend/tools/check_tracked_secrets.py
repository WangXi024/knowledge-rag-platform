#!/usr/bin/env python3
"""CI/本地安全门禁：扫描 Git 跟踪内容中的真实本地密钥并检查敏感文件权限。"""

from __future__ import annotations

import re
import shutil
import stat
import subprocess
from pathlib import Path

LOCAL_FILES = (
    Path("deploy/local/.env.ragflow"),
    Path("deploy/local/.env.full"),
)
# 只把完整语义段当作敏感字段。旧表达式会把 ``MAX_OUTPUT_TOKENS_FIELD`` 中的
# 复数 ``TOKENS`` 误判成访问令牌，导致普通协议配置值 ``max_tokens`` 命中大量源码。
# 新规则仍覆盖 PASSWORD、API_KEY、SECRET、TOKEN 和 PRIVATE_KEY，但不会因单词
# 子串相似而跳过真正的 Git 泄密检查。
SENSITIVE_NAME = re.compile(
    r"(?:^|_)(?:PASSWORD|API_KEY|SECRET|TOKEN|PRIVATE_KEY)(?:$|_)",
    re.IGNORECASE,
)
JWT_PATTERN = re.compile(rb"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
PRIVATE_KEY_MARKER = b"-----BEGIN " + b"PRIVATE KEY-----"


def _git() -> str:
    """定位 Git 可执行文件，缺失时立即失败。"""
    executable = shutil.which("git")
    if executable is None:
        raise RuntimeError("git executable was not found")
    return executable


def _run_git(repo_root: Path, *args: str) -> bytes:
    """在仓库根目录运行只读 Git 子命令并返回原始字节输出。"""
    result = subprocess.run(  # noqa: S603 - git resolved with shutil.which
        [_git(), *args],
        cwd=repo_root,
        check=False,
        capture_output=True,
    )
    if result.returncode:
        raise RuntimeError(f"git {' '.join(args[:2])} failed")
    return result.stdout


def _sensitive_values(repo_root: Path) -> set[bytes]:
    """从本地忽略环境/密钥文件提取足够长的真实敏感值作为扫描样本。"""
    values: set[bytes] = set()
    for relative in LOCAL_FILES:
        path = repo_root / relative
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            encoded = value.strip().encode()
            if SENSITIVE_NAME.search(name) and len(encoded) >= 8:
                values.add(encoded)
    return values


def _assert_ignored(repo_root: Path, relative: Path) -> None:
    """确认敏感路径受 .gitignore 保护且没有被 Git 跟踪。"""
    result = subprocess.run(  # noqa: S603 - git resolved with shutil.which
        [_git(), "check-ignore", "--quiet", str(relative)],
        cwd=repo_root,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(f"local sensitive path is not Git-ignored: {relative}")


def _assert_private_permissions(repo_root: Path) -> None:
    """在 POSIX 系统确认私钥只允许当前用户读取。"""
    secrets_dir = repo_root / ".secrets"
    if secrets_dir.exists() and stat.S_IMODE(secrets_dir.stat().st_mode) & 0o077:
        raise RuntimeError(".secrets directory is accessible by group or others")
    for relative in (
        Path("deploy/local/.env.ragflow"),
        Path("deploy/local/.env.full"),
    ):
        path = repo_root / relative
        if path.exists() and stat.S_IMODE(path.stat().st_mode) & 0o077:
            raise RuntimeError(f"local sensitive file has broad permissions: {relative}")


def main() -> None:
    """组合忽略规则、文件权限、真实值匹配和通用密钥模式检查。"""
    repo_root = Path(__file__).resolve().parents[1]
    for relative in LOCAL_FILES:
        if (repo_root / relative).exists():
            _assert_ignored(repo_root, relative)
    _assert_private_permissions(repo_root)

    tracked = [
        item.decode()
        for item in _run_git(repo_root, "ls-files", "-z").split(b"\0")
        if item
    ]
    sensitive_values = _sensitive_values(repo_root)
    violations: list[str] = []
    for relative in tracked:
        path = repo_root / relative
        if not path.is_file():
            continue
        content = path.read_bytes()
        if PRIVATE_KEY_MARKER in content or JWT_PATTERN.search(content):
            violations.append(relative)
            continue
        if any(value in content for value in sensitive_values):
            violations.append(relative)
    if violations:
        raise RuntimeError(
            "tracked secret material detected in: " + ", ".join(sorted(set(violations)))
        )
    print(
        f"tracked-secret check passed: {len(tracked)} tracked files, "
        f"{len(sensitive_values)} local secret values compared"
    )


if __name__ == "__main__":
    main()
