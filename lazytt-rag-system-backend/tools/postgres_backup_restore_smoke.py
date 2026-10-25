#!/usr/bin/env python3
"""验证应用/RAGFlow PostgreSQL 逻辑备份可在临时数据库恢复并通过关键数据断言。"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

DATABASES = (
    ("postgres", "APP_POSTGRES_USER", "APP_POSTGRES_DATABASE", "rag_app"),
    (
        "postgres",
        "RAGFLOW_POSTGRES_USER",
        "RAGFLOW_POSTGRES_DATABASE",
        "rag_flow",
    ),
)


def build_parser() -> argparse.ArgumentParser:
    """定义 Compose、环境、备份目录、容器服务和保留临时文件选项。"""
    parser = argparse.ArgumentParser(
        description=(
            "Dump both PostgreSQL databases, restore into temporary databases, "
            "verify critical schemas, then remove only the temporary databases."
        )
    )
    parser.add_argument(
        "--compose-env",
        type=Path,
        default=Path("deploy/local/.env.full"),
    )
    parser.add_argument(
        "--compose-file",
        type=Path,
        default=Path("deploy/local/docker-compose.storage.yml"),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Optional new directory in which encrypted-at-rest handling is your responsibility.",
    )
    parser.add_argument(
        "--keep-backups",
        action="store_true",
        help="Retain dump files; requires --output-dir.",
    )
    return parser


def _parse_env(path: Path) -> dict[str, str]:
    """读取 Compose 使用的简单环境文件。"""
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#") and "=" in line:
            name, value = line.split("=", 1)
            values[name.strip()] = value.strip()
    return values


def _required(values: dict[str, str], name: str) -> str:
    """读取非空环境值。"""
    value = values.get(name, "").strip()
    if not value:
        raise RuntimeError(f"required environment value is missing: {name}")
    return value


def _docker() -> str:
    """定位 Docker CLI。"""
    executable = shutil.which("docker")
    if executable is None:
        raise RuntimeError("docker executable was not found")
    return executable


def _compose_prefix(repo_root: Path, args: argparse.Namespace) -> list[str]:
    """构造限定环境文件和 Compose 文件的公共命令前缀。"""
    return [
        _docker(),
        "compose",
        "--env-file",
        str((repo_root / args.compose_env).resolve()),
        "-f",
        str((repo_root / args.compose_file).resolve()),
    ]


def _run(
    prefix: list[str],
    *command: str,
    stdout: BinaryIO | int = subprocess.PIPE,
) -> subprocess.CompletedProcess[bytes]:
    """运行数据库恢复相关命令，并在失败时提供受控摘要。"""
    result = subprocess.run(  # noqa: S603 - executable resolved with shutil.which
        [*prefix, *command],
        check=False,
        stdout=stdout,
        stderr=subprocess.PIPE,
        timeout=300,
    )
    if result.returncode:
        raise RuntimeError(
            f"database recovery command failed: {' '.join(command[:3])} "
            f"(exit {result.returncode})"
        )
    return result


@contextmanager
def _backup_directory(
    repo_root: Path,
    args: argparse.Namespace,
) -> Iterator[Path]:
    """提供用户目录或安全临时目录，并按保留选项决定是否自动清理。"""
    if args.keep_backups and args.output_dir is None:
        raise RuntimeError("--keep-backups requires --output-dir")
    if args.output_dir is None:
        with tempfile.TemporaryDirectory(prefix="lazytt-rag-restore-") as directory:
            yield Path(directory)
        return
    output = (repo_root / args.output_dir).resolve()
    if output.exists():
        raise RuntimeError("backup output directory must not already exist")
    output.mkdir(parents=True, mode=0o700)
    try:
        yield output
    finally:
        if not args.keep_backups:
            shutil.rmtree(output)


def _dump(
    prefix: list[str],
    service: str,
    user: str,
    database: str,
    target: Path,
) -> None:
    """在容器内用 pg_dump 生成自定义格式备份并安全写入宿主机文件。"""
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        _run(
            prefix,
            "exec",
            "-T",
            service,
            "pg_dump",
            "-U",
            user,
            "-d",
            database,
            "--format=custom",
            "--no-owner",
            "--no-privileges",
            stdout=stream,
        )
    if target.stat().st_size == 0:
        raise RuntimeError(f"{service} produced an empty PostgreSQL dump")


def _restore_and_verify(
    prefix: list[str],
    service: str,
    source_database: str,
    schema: str,
    dump: Path,
    admin_user: str,
) -> dict[str, object]:
    """由共享实例管理员创建临时库，恢复并验证后立即删除。

    两个业务角色在合并后都刻意没有 ``CREATEDB`` 权限，因此备份仍用数据库所有者
    读取，只有临时恢复生命周期使用初始化管理员。这样 Smoke 不会为了方便测试而
    破坏正式运行时的最小权限边界。
    """
    temporary_database = f"restore_smoke_{uuid4().hex[:12]}"
    _run(prefix, "exec", "-T", service, "createdb", "-U", admin_user, temporary_database)
    try:
        with dump.open("rb") as stream:
            restore = subprocess.run(  # noqa: S603 - executable resolved with shutil.which
                [
                    *prefix,
                    "exec",
                    "-T",
                    service,
                    "pg_restore",
                    "-U",
                    admin_user,
                    "-d",
                    temporary_database,
                    "--no-owner",
                    "--no-privileges",
                ],
                stdin=stream,
                capture_output=True,
                timeout=300,
                check=False,
            )
        if restore.returncode:
            raise RuntimeError(
                f"{service} pg_restore failed with exit code {restore.returncode}"
            )
        if schema == "rag_app":
            query = (
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema='rag_app' AND table_name <> 'alembic_version'"
            )
            expected_minimum = 8
        else:
            query = (
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema='public'"
            )
            expected_minimum = 40
        result = _run(
            prefix,
            "exec",
            "-T",
            service,
            "psql",
            "-U",
            admin_user,
            "-d",
            temporary_database,
            "-Atc",
            query,
        )
        count = int(result.stdout.decode().strip())
        if count < expected_minimum:
            raise RuntimeError(
                f"{service} restored {count} tables; expected at least {expected_minimum}"
            )
        return {
            "service": service,
            "source_database": source_database,
            "temporary_restore_verified": True,
            "table_count": count,
        }
    finally:
        _run(
            prefix,
            "exec",
            "-T",
            service,
            "dropdb",
            "-U",
            admin_user,
            "--force",
            temporary_database,
        )


def run(args: argparse.Namespace) -> dict[str, object]:
    """分别备份并恢复验证应用库和 RAGFlow 库，汇总证据。"""
    repo_root = Path(__file__).resolve().parents[1]
    values = _parse_env((repo_root / args.compose_env).resolve())
    admin_user = _required(values, "POSTGRES_ADMIN_USER")
    prefix = _compose_prefix(repo_root, args)
    results: list[dict[str, object]] = []
    with _backup_directory(repo_root, args) as directory:
        for service, user_name, database_name, schema in DATABASES:
            user = _required(values, user_name)
            database = _required(values, database_name)
            # 两个逻辑数据库现在共用 ``postgres`` 服务名，因此备份文件必须按
            # 逻辑数据库角色区分；继续使用服务名会让第二份备份碰撞第一份文件。
            dump = directory / f"{schema}.dump"
            _dump(prefix, service, user, database, dump)
            results.append(
                _restore_and_verify(
                    prefix,
                    service,
                    database,
                    schema,
                    dump,
                    admin_user,
                )
            )
        retained = args.keep_backups
        output_dir = str(directory) if retained else None
    return {
        "status": "passed",
        "databases": results,
        "backup_files_retained": retained,
        "backup_directory": output_dir,
        "temporary_databases_removed": True,
    }


def main() -> None:
    """执行备份恢复 Smoke 并输出 JSON 结果。"""
    result = run(build_parser().parse_args())
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
