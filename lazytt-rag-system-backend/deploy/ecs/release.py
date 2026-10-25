#!/usr/bin/env python3
"""ECS 后端制品准备、显式 SQL 迁移和应用发布入口，只依赖 Python 标准库与 Docker。

配置由受限主机环境文件提前加载。Compose 原文保留变量引用，不把渲染后的秘密写盘。
prepare 可用于已有服务，候选保存在镜像与非敏感回执中；migrate 使用内存中的候选
Compose，不覆盖当前运行文件。release 失败先验证旧结构兼容，再恢复原有运行服务。
脚本不创建 Store、不修改共享 ACL、不自动执行迁移、不运行本地初始化脚本。
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

PROJECT = "lazytt-rag-ecs-dev"
SERVICES = ("ragflow-server", "ragflow-task-executor", "rag-gateway", "rag-worker", "rag-scheduler")
MINERU_SERVICE = "mineru-cloud-adapter"
IMAGE = re.compile(r"^ghcr\.io/[a-z0-9_.\-/]+@sha256:[a-f0-9]{64}$")
UPSTREAM_IMAGE = re.compile(r"^[a-z0-9_.:/\-]+@sha256:[a-f0-9]{64}$")


class ReleaseError(RuntimeError):
    """只携带可公开的阶段名称，不包含子进程原始输出或配置值。"""


def validate_environment(mode: str) -> None:
    """按实际阶段校验输入；制品准备和显式迁移不要求未消费的模型配置。"""
    if mode not in {"release", "init-ragflow", "bootstrap-models"}:
        return
    required = set()
    if mode in {"release", "init-ragflow"}:
        required.update(
            "APP_DATABASE_DSN RAGFLOW_POSTGRES_HOST RAGFLOW_POSTGRES_PORT "
            "RAGFLOW_POSTGRES_DATABASE RAGFLOW_POSTGRES_USER RAGFLOW_POSTGRES_PASSWORD "
            "RAGFLOW_ES_HOST ELASTIC_PASSWORD RAGFLOW_MINIO_HOST MINIO_USER MINIO_PASSWORD "
            "REDIS_STACK_HOST REDIS_STACK_PASSWORD".split()
        )
    if mode in {"release", "bootstrap-models"}:
        required.update(
            "RAGFLOW_API_KEY EMBEDDING_BASE_URL EMBEDDING_API_KEY EMBEDDING_MODEL "
            "LLM_BASE_URL LLM_API_KEY LLM_MODEL VISION_BASE_URL VISION_API_KEY VISION_MODEL".split()
        )
    if mode == "release":
        required.update(
            "APP_REDIS_URL CACHE_REDIS_URL EMBEDDING_DIMENSION RAGFLOW_EMBEDDING_MODEL "
            "RAGFLOW_VISION_MODEL RAGFLOW_VISION_RUNTIME_MODEL".split()
        )
        for flag, fields in {
            "RERANK_ENABLED": "RERANK_BASE_URL RERANK_API_KEY RERANK_MODEL",
            "MEDIA_PROCESSING_ENABLED": (
                "ASR_BASE_URL ASR_API_KEY ASR_MODEL MEDIA_VISION_BASE_URL "
                "MEDIA_VISION_API_KEY MEDIA_VISION_MODEL"
            ),
        }.items():
            if os.environ.get(flag, "false") not in {"true", "false"}:
                raise ReleaseError(f"invalid boolean configuration: {flag}")
            if os.environ.get(flag) == "true":
                required.update(fields.split())
        if not os.environ.get("EMBEDDING_DIMENSION", "").isdigit():
            raise ReleaseError("EMBEDDING_DIMENSION must be a verified positive integer")
        if int(os.environ["EMBEDDING_DIMENSION"]) <= 0:
            raise ReleaseError("EMBEDDING_DIMENSION must be a verified positive integer")
    if mode == "release" and os.environ.get("MINERU_CLOUD_API_TOKEN", "").strip():
        required.add("MINERU_CLOUD_API_TOKEN")
        if os.environ.get("MINERU_CLOUD_MODEL", "vlm") != "vlm":
            raise ReleaseError("MINERU_CLOUD_MODEL must be vlm")
    for name in sorted(required):
        value = os.environ.get(name, "").strip()
        if not value or any(
            marker in value.lower() for marker in ("<", ">", "change-me", "pending-")
        ):
            raise ReleaseError(f"missing or unresolved configuration: {name}")


def run(args: list[str], *, input_text: str | None = None) -> str:
    """执行无 Shell 的固定参数命令；捕获输出用于检查，失败时不泄露容器环境。"""
    result = subprocess.run(  # noqa: S603 -- 固定参数列表，未启用 Shell。
        args, input=input_text, capture_output=True, text=True, check=False, timeout=900
    )
    if result.returncode:
        raise ReleaseError("command failed: " + " ".join(args[:2]))
    return result.stdout


def atomic_write(path: Path, value: str) -> None:
    """在同一目录原子写入受限文件；异常时清理本次临时文件，不改其他资源。"""
    descriptor, temporary = tempfile.mkstemp(prefix=".rag-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(value)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def candidate_text(source: str, app_image: str, ragflow_image: str) -> str:
    """只固化两个镜像，不展开任何应用秘密；所有实际镜像须为不可变 digest。"""
    if not IMAGE.fullmatch(app_image) or not UPSTREAM_IMAGE.fullmatch(ragflow_image):
        raise ReleaseError("immutable application and RAGFlow image digests are required")
    return source.replace("${RAG_APP_IMAGE}", app_image).replace("${RAGFLOW_IMAGE}", ragflow_image)


def validate_configuration_file(path: Path) -> None:
    """在调用管理 API 前检查文件归属，避免注册后才发现配置不可保存。"""
    metadata = path.stat()
    if (
        path.is_symlink()
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
    ):
        raise ReleaseError("configuration must be owned by the operator with mode 0600")


def save_configuration(path: Path, replacements: dict[str, str]) -> None:
    """仅更新获准字段，保留其他配置；文件须归当前用户且权限为 0600。"""
    validate_configuration_file(path)
    allowed = {
        "RAGFLOW_API_KEY",
        "EMBEDDING_DIMENSION",
        "RAGFLOW_EMBEDDING_MODEL",
        "RAGFLOW_VISION_MODEL",
        "RAGFLOW_VISION_RUNTIME_MODEL",
        "LLM_OMIT_SAMPLING_PARAMETERS",
        "LLM_MAX_OUTPUT_TOKENS_FIELD",
        "LLM_EXTRA_BODY_JSON",
    }
    if not replacements or not set(replacements).issubset(allowed):
        raise ReleaseError("unexpected bootstrap configuration fields")
    lines = path.read_text().splitlines()
    for key, value in replacements.items():
        # 写回仍采用规范规定的 export 格式，shell 引号保证值不会变成可执行命令。
        lines = [line for line in lines if not re.match(rf"^\s*(?:export\s+)?{key}=", line)]
        lines.append(f"export {key}={shlex.quote(value)}")
    atomic_write(path, "\n".join(lines) + "\n")


def bootstrap(directory: Path, mode: str, config_file: Path) -> None:
    """对已启动的首次 RAGFlow 执行官方账号或模型配置，秘密仅在父子进程间传递。"""
    validate_configuration_file(config_file)
    base = ["docker", "compose", "--project-name", PROJECT, "-f", str(directory / "compose.yml")]
    request_text = None
    if mode == "bootstrap-account":
        request = json.load(sys.stdin)
        encrypted = run(
            [
                *base,
                "exec",
                "-T",
                "ragflow-server",
                "python",
                "-c",
                "import sys; from api.utils.crypt import crypt; print(crypt(sys.stdin.read()))",
            ],
            input_text=request["password"],
        ).strip()
        request_text = json.dumps({"email": request["email"], "encrypted_password": encrypted})
    output = run(
        [
            *base,
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "rag-app-bootstrap",
            "python",
            "-m",
            "tools.ecs_ragflow_bootstrap",
            "account" if mode == "bootstrap-account" else "models",
        ],
        input_text=request_text,
    )
    save_configuration(config_file, json.loads(output))


class Deployment:
    """管理一个固定 Compose 项目；候选输入只在内存中渲染，常驻文件只有 compose.yml。"""

    def __init__(self, directory: Path, compose: str) -> None:
        """绑定已审阅的部署目录和 Compose 原文，不隐式启动任何容器。"""
        self.directory = directory
        self.compose = compose

    def command(self, *args: str) -> str:
        """通过标准输入传入唯一候选定义；run 禁用交互以避免争用 Compose 标准输入。"""
        return run(
            [
                "docker",
                "compose",
                "--project-directory",
                str(self.directory),
                "--project-name",
                PROJECT,
                "-f",
                "-",
                *args,
            ],
            input_text=self.compose,
        )

    def one_shot(self, service: str, *command: str) -> str:
        """复用固定服务定义执行候选检查或管理员任务，不自动启动 depends_on 服务。"""
        return self.command(
            "run", "--rm", "--no-deps", "--interactive=false", "-T", service, *command
        )

    def validate(self) -> dict[str, Any]:
        """验证实际 Compose 拓扑、镜像、端口和资源限制；不返回或输出环境值。"""
        document: dict[str, Any] = json.loads(
            self.command("--profile", "admin", "--profile", "mineru", "config", "--format", "json")
        )
        if document.get("name") != PROJECT:
            raise ReleaseError("unexpected Compose project")
        services = document.get("services", {})
        if (
            set(services)
            - set(SERVICES)
            - {"rag-app-bootstrap", "rag-app-schema-check", "rag-app-migrate", MINERU_SERVICE}
        ):
            raise ReleaseError("unexpected service, shared Store is not managed here")
        if not set(SERVICES).issubset(services):
            raise ReleaseError("missing backend service")
        for name, service in services.items():
            if service.get("build") or service.get("privileged") or service.get("network_mode"):
                raise ReleaseError("unapproved build or host privileges")
            if service.get("platform") != "linux/amd64":
                raise ReleaseError("ECS candidate must use linux/amd64")
            if not UPSTREAM_IMAGE.fullmatch(service.get("image", "")):
                raise ReleaseError("all services require immutable image digests")
            if not all(service.get(key) for key in ("cpus", "mem_limit", "pids_limit", "logging")):
                raise ReleaseError("service lacks resource or log limits")
            for port in service.get("ports", []):
                # 只允许 Gateway 显式绑定全部 IPv4 接口；RAGFlow 仍限回环。
                # 同时接受新旧合法绑定，保证关闭公网或失败恢复时旧定义仍可校验。
                allowed_hosts = {"127.0.0.1"}
                if name == "rag-gateway":
                    allowed_hosts.add("0.0.0.0")  # noqa: S104 -- 用户显式选择的开发公网 Gateway。
                if port.get("host_ip") not in allowed_hosts or name not in {
                    "rag-gateway",
                    "ragflow-server",
                }:
                    raise ReleaseError("unapproved Gateway or RAGFlow endpoint binding")
            if name == MINERU_SERVICE and (
                service.get("profiles") != ["mineru"]
                # 新增并发配置可选，旧 Compose 仍须能通过校验以支持发布失败恢复。
                or not {"MINERU_CLOUD_API_TOKEN", "MINERU_CLOUD_MODEL"}.issubset(
                    service.get("environment", {})
                )
                or set(service.get("environment", {})) - {
                    "MINERU_CLOUD_API_TOKEN", "MINERU_CLOUD_MODEL",
                    "MINERU_CLOUD_MAX_CONCURRENCY", "MINERU_CLOUD_RESULT_CONCURRENCY",
                }
            ):
                raise ReleaseError("unexpected MinerU profile or credentials")
            if name in (*SERVICES, MINERU_SERVICE) and (
                service.get("restart") != "always" or not service.get("healthcheck")
            ):
                raise ReleaseError("resident service lacks restart or health policy")
        return {"project": PROJECT, "service_count": len(services)}

    def check_ownership(self) -> None:
        """拒绝接管同名网络或卷；不存在时由本项目 Compose 使用自动 IPAM 创建。"""
        resources = [
            ("network", "ragflow-backplane"),
            ("volume", "upload_staging_data"),
            ("volume", "ragflow_logs"),
            ("volume", "scheduler_data"),
        ]
        for kind, suffix in resources:
            name = f"{PROJECT}_{suffix}"
            existing = run(["docker", kind, "ls", "--format", "{{.Name}}"])
            if name in existing.splitlines():
                info = json.loads(run(["docker", kind, "inspect", name]))[0]
                if (info.get("Labels") or {}).get("com.docker.compose.project") != PROJECT:
                    raise ReleaseError("same-name resource has unknown ownership")

    def running(self) -> list[str]:
        """只读取本项目当前运行的常驻服务，用于失败后恢复实际启动状态。"""
        return [
            name
            for name in self.command(
                "--profile", "mineru", "ps", "--services", "--status", "running"
            ).splitlines()
            if name in (*SERVICES, MINERU_SERVICE)
        ]

    def start(self, *services: str) -> None:
        """只更新明确列出的本系统服务并等待健康；不隐式重建 Store 或管理任务。"""
        if services:
            self.command(
                "up",
                "-d",
                "--no-deps",
                "--force-recreate",
                "--wait",
                "--wait-timeout",
                "600",
                *services,
            )


def release(candidate: Deployment, compose_file: Path) -> None:
    """发布前检查 SQL 结构，随后按依赖顺序更新；失败按旧镜像结构兼容性恢复。

    旧定义仅保存在本进程内。强制终止或断电后需按 README 核对状态，不承诺自动恢复。
    SQL 迁移和账号初始化仍由管理员执行；可选 MinerU 通过官方 API 幂等登记。
    """
    old_text = compose_file.read_text() if compose_file.exists() else None
    old = Deployment(candidate.directory, old_text) if old_text else None
    if old:
        old.validate()
    running = old.running() if old else []
    mineru_enabled = bool(os.environ.get("MINERU_CLOUD_API_TOKEN", "").strip())
    # 兼容加入 MinerU 之前的旧版 Compose，停止操作不能引用不存在的服务。
    has_mineru = bool(re.search(r"^  mineru-cloud-adapter:", candidate.compose, re.MULTILINE))
    if mineru_enabled and not has_mineru:
        raise ReleaseError("candidate lacks MinerU service")
    # 先用新候选只读检查，失败时当前应用还未被改动。
    candidate.one_shot("rag-app-schema-check")
    try:
        atomic_write(compose_file, candidate.compose)
        candidate.start("ragflow-server")
        candidate.start("ragflow-task-executor")
        if mineru_enabled:
            candidate.start(MINERU_SERVICE)
            candidate.one_shot(
                "rag-app-bootstrap",
                "python",
                "-m",
                "rag_service.infrastructure.mineru_cloud.register",
            )
        candidate.one_shot("rag-app-bootstrap")
        candidate.start("rag-gateway", "rag-worker", "rag-scheduler")
        if not mineru_enabled and MINERU_SERVICE in running:
            old.command("--profile", "mineru", "stop", MINERU_SERVICE)
    except Exception as error:
        candidate.command("stop", *SERVICES, *((MINERU_SERVICE,) if has_mineru else ()))
        if old:
            atomic_write(compose_file, old.compose)
            if running:
                try:
                    old.one_shot("rag-app-schema-check")
                except ReleaseError:
                    raise ReleaseError(
                        "release failed; old schema incompatible; services stopped, data retained"
                    ) from None
                old.start(*running)
        state = "previous compatible running state restored" if old else "new services stopped"
        raise ReleaseError(f"release failed; {state}, data retained") from error


def main() -> None:
    """解析显式模式，校验候选身份并加环境锁；输出仅包含非敏感交付回执。"""
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=(
            "prepare",
            "release",
            "migrate",
            "init-ragflow",
            "bootstrap-account",
            "bootstrap-models",
        ),
        required=True,
    )
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--source-compose", type=Path, required=True)
    parser.add_argument("--source-template", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--database", choices=("rag_app", "rag_flow"))
    parser.add_argument("--sql")
    parser.add_argument("--config-file", type=Path)
    args = parser.parse_args()
    validate_environment(args.mode)
    if not re.fullmatch(r"[0-9a-f]{40}", args.revision):
        raise ReleaseError("a full reviewed Git revision is required")
    directory = args.directory
    if not directory.is_absolute() or directory.is_symlink():
        raise ReleaseError("deployment directory must be an absolute non-symlink path")
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".release.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        text = candidate_text(
            args.source_compose.read_text(), args.image, os.environ.get("RAGFLOW_IMAGE", "")
        )
        text = text.replace("${BUILD_COMMIT:-local}", args.revision)
        # 从 ECS 专用模板生成候选配置；prepare 不改变旧容器已挂载的配置。
        target = directory / "config" / args.revision / "service_conf.postgres.yaml.template"
        target.parent.mkdir(parents=True, exist_ok=True)
        template = args.source_template.read_text()
        if target.exists() and target.read_text() != template:
            raise ReleaseError("same-revision template changed")
        atomic_write(target, template)
        text = text.replace("./deploy/ecs/ragflow/service_conf.postgres.yaml.template", str(target))
        candidate = Deployment(directory, text)
        candidate.validate()
        candidate.check_ownership()
        # 镜像已固定完整 digest；复用本地同一内容，缺失时才从 Registry 下载。
        # 不忽略下载失败，也不以标签或其他版本替代不可用的候选。
        candidate.command("pull", "--policy", "missing")
        info = json.loads(run(["docker", "image", "inspect", args.image]))[0]
        if info.get("Architecture") != "amd64" or info.get("Os") != "linux":
            raise ReleaseError("candidate image platform does not match ECS")
        if (info.get("Config", {}).get("Labels") or {}).get(
            "org.opencontainers.image.revision"
        ) != args.revision:
            raise ReleaseError("candidate image revision does not match reviewed commit")
        compose_file = directory / "compose.yml"
        if args.mode == "prepare":
            # 首次准备只交付文件；已有环境保留当前 Compose，迁移用当前命令的候选输入。
            if not compose_file.exists():
                atomic_write(compose_file, text)
        elif args.mode == "migrate":
            if (
                not args.database
                or not args.sql
                or Path(args.sql).name != args.sql
                or not args.sql.endswith(".sql")
            ):
                raise ReleaseError("migrate requires a database and packaged SQL filename")
            candidate.one_shot(
                "rag-app-migrate",
                "python",
                "-m",
                "rag_service.cli",
                "schema",
                "migrate",
                "--database",
                args.database,
                "--file",
                args.sql,
            )
        elif args.mode == "init-ragflow":
            if candidate.running():
                raise ReleaseError("init-ragflow is only for first installation")
            candidate.one_shot("rag-app-schema-check")
            atomic_write(compose_file, text)
            candidate.start("ragflow-server")
            candidate.start("ragflow-task-executor")
        elif args.mode in {"bootstrap-account", "bootstrap-models"}:
            if (
                not args.config_file
                or not compose_file.exists()
                or compose_file.read_text() != text
            ):
                raise ReleaseError(
                    "bootstrap requires the same installed candidate and a configuration file"
                )
            if "rag-gateway" in candidate.running():
                raise ReleaseError(
                    "bootstrap requires a separate maintenance operation for live applications"
                )
            bootstrap(directory, args.mode, args.config_file)
        else:
            release(candidate, compose_file)
        receipt = {
            "mode": args.mode,
            "revision": args.revision,
            "image": args.image,
            "project": PROJECT,
            "status": "passed",
        }
        atomic_write(directory / "candidate.json", json.dumps(receipt, indent=2) + "\n")
        print(json.dumps(receipt))


if __name__ == "__main__":
    try:
        main()
    except ReleaseError as error:
        print(f"ECS 阶段失败：{error}", file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        print(
            "ECS 阶段失败；请按操作说明检查候选、配置、结构及健康状态，数据保留。", file=sys.stderr
        )
        raise SystemExit(1) from None
