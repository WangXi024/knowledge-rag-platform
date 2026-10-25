#!/usr/bin/env python3
"""从示例与 RAGFlow 环境生成完整本地 ``deploy/local/.env.full``，并填入随机开发密钥。"""

from __future__ import annotations

import argparse
import os
import secrets
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

# S104 在普通服务中阻止全网卡绑定；本工具正是为用户显式开启受信
# 局域网入口，并且后续 Compose 校验会继续禁止 RAGFlow 管理端口使用它。
ALLOWED_GATEWAY_BIND_ADDRESSES = frozenset(
    {"127.0.0.1", "0.0.0.0"}  # noqa: S104
)

# 完整运行环境每次启动都会重新生成，因此不能只依赖字典当前的插入顺序。
# 这里把用户最常编辑的模型配置固定拆成职责组，并让每组始终按照
# BASE_URL -> API_KEY -> MODEL 排列；基础设施、直连身份和自动生成字段也各自归类。
FULL_ENV_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "存储部署模式与共享网络",
        ("RAG_STORAGE_MODE", "RAG_DOCKER_NETWORK"),
    ),
    (
        "应用服务",
        ("RAG_APP_IMAGE", "RAG_APP_BIND_ADDRESS", "RAG_APP_PORT", "WORKER_CONCURRENCY"),
    ),
    (
        "共享 PostgreSQL（两个逻辑数据库）",
        (
            "POSTGRES_IMAGE",
            "POSTGRES_ADMIN_USER",
            "POSTGRES_ADMIN_PASSWORD",
            "APP_POSTGRES_DATABASE",
            "APP_POSTGRES_USER",
            "APP_POSTGRES_PASSWORD",
            "APP_DATABASE_DSN",
            "RAGFLOW_POSTGRES_DATABASE",
            "RAGFLOW_POSTGRES_USER",
            "RAGFLOW_POSTGRES_PASSWORD",
            "RAGFLOW_POSTGRES_HOST",
            "RAGFLOW_POSTGRES_PORT",
        ),
    ),
    (
        "统一 Redis Stack（逻辑 DB 隔离）",
        (
            "REDIS_STACK_IMAGE",
            "REDIS_STACK_PASSWORD",
            "REDIS_STACK_MAXMEMORY",
            "REDIS_STACK_HOST",
            "REDIS_STACK_PORT",
            "APP_REDIS_URL",
            "CACHE_REDIS_URL",
        ),
    ),
    (
        "RAGFlow 服务与基础设施",
        (
            "RAGFLOW_IMAGE",
            "RAGFLOW_PLATFORM",
            "RAGFLOW_WEB_PORT",
            "RAGFLOW_API_PORT",
            "ELASTICSEARCH_IMAGE",
            "ELASTIC_PASSWORD",
            "ES_JAVA_OPTS",
            "MINIO_IMAGE",
            "MINIO_USER",
            "MINIO_PASSWORD",
            "RAGFLOW_BASE_URL",
            "RAGFLOW_ES_SCHEME",
            "RAGFLOW_ES_HOST",
            "RAGFLOW_ES_PORT",
            "RAGFLOW_MINIO_HOST",
            "RAGFLOW_MINIO_PORT",
        ),
    ),
    (
        "RAGFlow 管理账号与自动模型引用",
        (
            "RAGFLOW_REGISTER_ENABLED",
            "RAGFLOW_ADMIN_EMAIL",
            "RAGFLOW_ADMIN_PASSWORD",
            "RAGFLOW_API_KEY",
            "RAGFLOW_EMBEDDING_MODEL",
            "RAGFLOW_VISION_RUNTIME_MODEL",
            "RAGFLOW_VISION_MODEL",
        ),
    ),
    (
        "Chat 与 Query Rewrite",
        (
            "LLM_BASE_URL",
            "LLM_API_KEY",
            "LLM_MODEL",
            "REWRITE_MODEL",
            "LLM_OMIT_SAMPLING_PARAMETERS",
            "LLM_MAX_OUTPUT_TOKENS_FIELD",
            "LLM_EXTRA_BODY_JSON",
        ),
    ),
    (
        "Embedding 与语义缓存",
        (
            "EMBEDDING_BASE_URL",
            "EMBEDDING_API_KEY",
            "EMBEDDING_MODEL",
            "EMBEDDING_DIMENSION",
        ),
    ),
    ("图片理解与 OCR", ("VISION_BASE_URL", "VISION_API_KEY", "VISION_MODEL")),
    (
        "视频抽帧理解",
        ("MEDIA_VISION_BASE_URL", "MEDIA_VISION_API_KEY", "MEDIA_VISION_MODEL"),
    ),
    (
        "Cross-Encoder Rerank",
        ("RERANK_ENABLED", "RERANK_BASE_URL", "RERANK_API_KEY", "RERANK_MODEL"),
    ),
    (
        "音视频 ASR",
        ("MEDIA_PROCESSING_ENABLED", "ASR_BASE_URL", "ASR_API_KEY", "ASR_MODEL"),
    ),
    # 可选云端解析与其他配置统一存放，Compose 只向适配服务传递这两项。
    ("MinerU 云端解析", ("MINERU_CLOUD_API_TOKEN", "MINERU_CLOUD_MODEL",
        "MINERU_CLOUD_MAX_CONCURRENCY", "MINERU_CLOUD_RESULT_CONCURRENCY",
        "RAGFLOW_MAX_CONCURRENT_TASKS", "RAGFLOW_MAX_CONCURRENT_CHUNK_BUILDERS")),
    ("构建信息", ("BUILD_COMMIT", "BUILD_TIME", "TZ")),
    (
        "仅供显式 Smoke Profile 使用",
        ("RAGFLOW_SMOKE_EMBEDDING_IMAGE", "RAGFLOW_SMOKE_EMBEDDING_BASE_URL"),
    ),
)


def _parse_env(path: Path) -> dict[str, str]:
    """读取简单 KEY=VALUE 环境文件，跳过注释与空行。"""
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name.strip()] = value.strip()
    return values


def _required(values: dict[str, str], name: str) -> str:
    """取得非空配置，否则用字段名报告缺失。"""
    value = values.get(name, "")
    if not value or value.startswith("change-me"):
        raise SystemExit(f"{name} must be configured in the RAGFlow environment first")
    return value


def _boolean(values: dict[str, str], name: str, *, default: str) -> str:
    """读取严格布尔配置并统一为小写文本，避免 Compose 接收含糊值。

    环境文件最终会传入多个不同语言编写的服务；统一只允许 ``true`` 与 ``false``，
    可以在容器启动前发现 ``1``、``yes`` 或拼写错误导致的跨语言解析差异。
    """
    value = values.get(name, default).strip().lower()
    if value not in {"true", "false"}:
        raise SystemExit(f"{name} must be exactly true or false")
    return value


def _require_capability(
    values: dict[str, str],
    *,
    enabled_name: str,
    field_names: tuple[str, str, str],
) -> None:
    """能力开启时要求 Base URL、API Key 和模型名同时存在。

    Rerank 与 ASR 都是可选能力。关闭时允许三元组为空；开启后缺少任意一项都必须
    在生成运行环境时失败，防止服务启动成功却在第一次真实请求时才返回 500。
    """
    if values[enabled_name] == "true":
        for name in field_names:
            _required(values, name)


def _local_secret() -> str:
    """生成适合环境变量的高熵 URL-safe 本地密钥。"""
    return secrets.token_urlsafe(32)


def _render_grouped_environment(values: dict[str, str]) -> str:
    """按稳定章节渲染完整环境，同时保留未来新增但尚未归类的字段。

    输出中的注释只描述字段类别，不包含任何配置值。每个已知字段只渲染一次；如果
    后续新增字段忘记加入章节，它会落在“其他配置”中而不会被静默丢弃，随后可由测试
    和代码审阅把它移动到合适位置。
    """
    rendered: list[str] = []
    emitted: set[str] = set()
    for title, names in FULL_ENV_SECTIONS:
        present_names = [name for name in names if name in values]
        if not present_names:
            continue
        if rendered:
            rendered.append("")
        rendered.append(f"# {title}")
        for name in present_names:
            rendered.append(f"{name}={values[name]}")
            emitted.add(name)

    remaining_names = [name for name in values if name not in emitted]
    if remaining_names:
        rendered.extend(("", "# 其他配置"))
        rendered.extend(f"{name}={values[name]}" for name in remaining_names)
    return "\n".join(rendered) + "\n"


def resolve_gateway_bind_address(
    source_values: dict[str, str],
    existing_values: dict[str, str],
) -> str:
    """解析 Gateway 的宿主机绑定地址，并只允许经过评审的两种模式。

    ``deploy/local/.env.ragflow`` 是用户持久保存的私有配置，因此它优先于每次
    启动都会重建的 ``deploy/local/.env.full``。为了兼容旧用户曾经直接改过完整
    环境文件的情况，源文件没有该字段时会复用旧值；两个文件都没配置则
    安全默认为仅本机可访问的 ``127.0.0.1``。

    只允许 ``127.0.0.1`` 和 ``0.0.0.0``：前者是回环模式，后者是用户显式
    开启的局域网模式。拒绝任意字符串可以在 Docker 启动前发现拼写错误，
    也避免误把未经评审的公网接口当成普通本地配置。
    """
    value = (
        source_values.get("RAG_APP_BIND_ADDRESS")
        or existing_values.get("RAG_APP_BIND_ADDRESS")
        or "127.0.0.1"
    )
    if value not in ALLOWED_GATEWAY_BIND_ADDRESSES:
        allowed = ", ".join(sorted(ALLOWED_GATEWAY_BIND_ADDRESSES))
        raise SystemExit(f"RAG_APP_BIND_ADDRESS must be one of: {allowed}")
    return value


def _commit(repo_root: Path) -> str:
    """尽力读取当前 Git 提交；不可用时返回 development。"""
    result = subprocess.run(
        ["git", "rev-parse", "--short=12", "HEAD"],  # noqa: S607 - fixed arguments
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def build_parser() -> argparse.ArgumentParser:
    """定义输入环境文件、输出文件和覆盖策略。"""
    parser = argparse.ArgumentParser(
        description="Create an ignored local full-stack environment from the verified RAGFlow env."
    )
    parser.add_argument(
        "--ragflow-env",
        type=Path,
        default=Path("deploy/local/.env.ragflow"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("deploy/local/.env.full"),
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--allow-pending-ragflow-key",
        action="store_true",
        help="Only for starting RAGFlow before the account bootstrap creates an API key.",
    )
    return parser


def main() -> None:
    """合并固定本地拓扑、已有 RAGFlow 值和新随机凭据，原子生成忽略环境文件。"""
    args = build_parser().parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    source = (repo_root / args.ragflow_env).resolve()
    output = (repo_root / args.output).resolve()
    if output.exists() and not args.force:
        print(f"local environment already exists at {output}")
        return
    source_values = _parse_env(source)
    existing_values = _parse_env(output) if output.exists() else {}
    # PostgreSQL 管理员只在共享卷首次初始化时创建两个业务数据库；应用与 RAGFlow
    # 运行期永远不会收到该管理员凭据。优先从持久私有环境读取，兼容旧环境时才复用
    # 已生成的 .env.full，最后的随机回退只服务于直接调用本工具的测试场景。
    postgres_admin_password = (
        source_values.get("POSTGRES_ADMIN_PASSWORD")
        or existing_values.get("POSTGRES_ADMIN_PASSWORD")
        or _local_secret()
    )
    app_postgres_password = (
        source_values.get("APP_POSTGRES_PASSWORD")
        or existing_values.get("APP_POSTGRES_PASSWORD")
        or _local_secret()
    )
    # Redis 密码是持久私有配置，必须由 prepare_ragflow_env.py 首次生成或
    # 从旧 RAGFlow Redis 密码迁移。这里不再为 app/cache 分别生成随机值，
    # 避免每次重建 .env.full 时破坏统一实例的连接一致性。
    redis_stack_password = _required(source_values, "REDIS_STACK_PASSWORD")
    gateway_bind_address = resolve_gateway_bind_address(source_values, existing_values)
    storage_mode = source_values.get("RAG_STORAGE_MODE", "local").strip().lower()
    if storage_mode not in {"local", "external"}:
        raise SystemExit("RAG_STORAGE_MODE must be exactly local or external")
    app_user = "rag_app"
    app_database = "rag_app"
    local_app_database_dsn = (
        f"postgresql+psycopg://{app_user}:{quote(app_postgres_password, safe='')}"
        f"@postgres:5432/{app_database}"
    )
    redis_stack_host = source_values.get("REDIS_STACK_HOST", "redis-stack")
    redis_stack_port = source_values.get("REDIS_STACK_PORT", "6379")
    redis_url_prefix = (
        f"redis://:{quote(redis_stack_password, safe='')}"
        f"@{redis_stack_host}:{redis_stack_port}"
    )
    values = {
        # 仅复制管理员显式填写的官网 Token，缺失时不阻止其他本地服务启动。
        "MINERU_CLOUD_API_TOKEN": source_values.get("MINERU_CLOUD_API_TOKEN", ""),
        "MINERU_CLOUD_MODEL": source_values.get("MINERU_CLOUD_MODEL", "vlm"),
        "MINERU_CLOUD_MAX_CONCURRENCY": source_values.get("MINERU_CLOUD_MAX_CONCURRENCY", "10"),
        "MINERU_CLOUD_RESULT_CONCURRENCY": source_values.get(
            "MINERU_CLOUD_RESULT_CONCURRENCY", "2"
        ),
        "RAGFLOW_MAX_CONCURRENT_TASKS": source_values.get("RAGFLOW_MAX_CONCURRENT_TASKS", "10"),
        "RAGFLOW_MAX_CONCURRENT_CHUNK_BUILDERS": source_values.get(
            "RAGFLOW_MAX_CONCURRENT_CHUNK_BUILDERS", "10"
        ),
        "RAG_STORAGE_MODE": storage_mode,
        "RAG_DOCKER_NETWORK": source_values.get(
            "RAG_DOCKER_NETWORK", "lazytt-ragflow-poc_ragflow-backplane"
        ),
        "RAG_APP_IMAGE": "local/lazytt-rag-service:dev",
        "RAG_APP_BIND_ADDRESS": gateway_bind_address,
        "RAG_APP_PORT": "8080",
        "WORKER_CONCURRENCY": source_values.get("WORKER_CONCURRENCY", "10"),
        "POSTGRES_ADMIN_USER": source_values.get(
            "POSTGRES_ADMIN_USER", "lazytt_postgres_admin"
        ),
        "POSTGRES_ADMIN_PASSWORD": postgres_admin_password,
        "APP_POSTGRES_DATABASE": app_database,
        "APP_POSTGRES_USER": app_user,
        "APP_POSTGRES_PASSWORD": app_postgres_password,
        # 外部模式完全信任用户填写的连接地址；本地模式继续使用存储 Compose 中
        # 的服务名。两种模式最终都落成同一组运行时变量，因此业务代码无需分支。
        "APP_DATABASE_DSN": source_values.get("APP_DATABASE_DSN", local_app_database_dsn),
        # DB 0 供精确/语义缓存，DB 1 由 RAGFlow 模板固定，DB 2 供
        # Celery Broker/Backend。连接地址由一组主机、端口和密码派生，
        # 无论 local/external 模式都不允许静默回退到三个物理 Redis。
        "REDIS_STACK_PASSWORD": redis_stack_password,
        "REDIS_STACK_MAXMEMORY": source_values.get("REDIS_STACK_MAXMEMORY", "1gb"),
        "REDIS_STACK_HOST": redis_stack_host,
        "REDIS_STACK_PORT": redis_stack_port,
        "APP_REDIS_URL": f"{redis_url_prefix}/2",
        "CACHE_REDIS_URL": f"{redis_url_prefix}/0",
        "RAGFLOW_BASE_URL": source_values.get(
            "RAGFLOW_BASE_URL", "http://ragflow-server:9380"
        ),
        "RAGFLOW_POSTGRES_HOST": source_values.get(
            "RAGFLOW_POSTGRES_HOST", "postgres"
        ),
        "RAGFLOW_POSTGRES_PORT": source_values.get("RAGFLOW_POSTGRES_PORT", "5432"),
        "RAGFLOW_ES_SCHEME": source_values.get("RAGFLOW_ES_SCHEME", "http"),
        "RAGFLOW_ES_HOST": source_values.get("RAGFLOW_ES_HOST", "elasticsearch"),
        "RAGFLOW_ES_PORT": source_values.get("RAGFLOW_ES_PORT", "9200"),
        "RAGFLOW_MINIO_HOST": source_values.get("RAGFLOW_MINIO_HOST", "minio"),
        "RAGFLOW_MINIO_PORT": source_values.get("RAGFLOW_MINIO_PORT", "9000"),
        # 模型能力只使用通用的 Base URL、API Key、Model 三元组。这里不识别任何
        # 厂商别名，也不会从旧字段猜测凭据；源文件缺项会在写入前按字段名失败。
        "LLM_BASE_URL": _required(source_values, "LLM_BASE_URL"),
        "LLM_API_KEY": _required(source_values, "LLM_API_KEY"),
        "LLM_MODEL": _required(source_values, "LLM_MODEL"),
        "REWRITE_MODEL": source_values.get("REWRITE_MODEL")
        or _required(source_values, "LLM_MODEL"),
        # 下列三个非敏感字段由通用 Bootstrap 探测后写回源文件。首次启动使用标准
        # OpenAI 请求；需要关闭思考或省略采样参数时，Bootstrap 会原子更新它们。
        "LLM_OMIT_SAMPLING_PARAMETERS": source_values.get("LLM_OMIT_SAMPLING_PARAMETERS", "false"),
        "LLM_MAX_OUTPUT_TOKENS_FIELD": source_values.get(
            "LLM_MAX_OUTPUT_TOKENS_FIELD", "max_tokens"
        ),
        "LLM_EXTRA_BODY_JSON": source_values.get("LLM_EXTRA_BODY_JSON", "{}"),
        "VISION_BASE_URL": _required(source_values, "VISION_BASE_URL"),
        "VISION_API_KEY": _required(source_values, "VISION_API_KEY"),
        "VISION_MODEL": _required(source_values, "VISION_MODEL"),
        # 视频抽帧模型与文档 OCR 必须显式配置。即使两项能力共用供应商和 Key，也由
        # 用户在私有环境中明确填写，生成器不会复制或推断任何凭据。
        "MEDIA_VISION_BASE_URL": source_values.get("MEDIA_VISION_BASE_URL", ""),
        "MEDIA_VISION_API_KEY": source_values.get("MEDIA_VISION_API_KEY", ""),
        "MEDIA_VISION_MODEL": source_values.get("MEDIA_VISION_MODEL", ""),
        "MEDIA_PROCESSING_ENABLED": _boolean(
            source_values, "MEDIA_PROCESSING_ENABLED", default="false"
        ),
        "ASR_BASE_URL": source_values.get("ASR_BASE_URL", ""),
        "ASR_API_KEY": source_values.get("ASR_API_KEY", ""),
        "ASR_MODEL": source_values.get("ASR_MODEL", ""),
        "EMBEDDING_BASE_URL": _required(source_values, "EMBEDDING_BASE_URL"),
        "EMBEDDING_API_KEY": _required(source_values, "EMBEDDING_API_KEY"),
        "EMBEDDING_MODEL": _required(source_values, "EMBEDDING_MODEL"),
        # 用户无需填写维度。首次模型 Bootstrap 会用单条和批量真实请求探测后写回；
        # 已经探测的值从源文件复用，模型变更时由指纹检查要求重新生成索引世代。
        "EMBEDDING_DIMENSION": source_values.get("EMBEDDING_DIMENSION", "pending-auto-detection"),
        "RERANK_ENABLED": _boolean(source_values, "RERANK_ENABLED", default="false"),
        "RERANK_BASE_URL": source_values.get("RERANK_BASE_URL", ""),
        "RERANK_API_KEY": source_values.get("RERANK_API_KEY", ""),
        "RERANK_MODEL": source_values.get("RERANK_MODEL", ""),
        # 本 Compose 只把 RAGFlow UI/API 发布到 127.0.0.1。按本地开发需求保持注册
        # 开启，让新拉取代码的用户和后续本机测试账号无需重建容器即可注册；该设置
        # 不能直接照搬到会暴露公网的生产部署。
        "RAGFLOW_REGISTER_ENABLED": "1",
        "BUILD_COMMIT": _commit(repo_root),
        "BUILD_TIME": datetime.now(UTC).isoformat(),
        "TZ": source_values.get("TZ", "Asia/Shanghai"),
    }
    copied_names = (
        "RAGFLOW_IMAGE",
        "RAGFLOW_PLATFORM",
        "RAGFLOW_WEB_PORT",
        "RAGFLOW_API_PORT",
        "RAGFLOW_POSTGRES_DATABASE",
        "RAGFLOW_POSTGRES_USER",
        "RAGFLOW_POSTGRES_PASSWORD",
        "ELASTIC_PASSWORD",
        "ES_JAVA_OPTS",
        "MINIO_USER",
        "MINIO_PASSWORD",
        "RAGFLOW_SMOKE_EMBEDDING_IMAGE",
        "RAGFLOW_ADMIN_EMAIL",
        "RAGFLOW_ADMIN_PASSWORD",
        "RAGFLOW_API_KEY",
        "RAGFLOW_EMBEDDING_MODEL",
        "RAGFLOW_VISION_RUNTIME_MODEL",
        "RAGFLOW_VISION_MODEL",
        "RAGFLOW_SMOKE_EMBEDDING_BASE_URL",
    )
    for name in copied_names:
        values[name] = source_values.get(name, "")

    if storage_mode == "local":
        # 只有本地存储 Compose 会消费这四个镜像字段。把它们限制在 local 模式，
        # 可以让 external 模式的私有配置只描述远端地址、账号和密码；运行时不会
        # 因为缺少一个永远不会启动的本地数据库镜像而失败。默认版本仍与已验证的
        # 本地基线完全一致，所以原有 Docker 卷、回退脚本和离线开发流程不变。
        values.update(
            {
                "POSTGRES_IMAGE": source_values.get(
                    "POSTGRES_IMAGE", "postgres:18.4-bookworm"
                ),
                "REDIS_STACK_IMAGE": source_values.get(
                    "REDIS_STACK_IMAGE", "redis/redis-stack-server:7.4.0-v8"
                ),
                "ELASTICSEARCH_IMAGE": source_values.get(
                    "ELASTICSEARCH_IMAGE", "elasticsearch:8.11.3"
                ),
                "MINIO_IMAGE": source_values.get(
                    "MINIO_IMAGE", "pgsty/minio:RELEASE.2026-03-25T00-00-00Z"
                ),
            }
        )

    if storage_mode == "external":
        # 外部模式必须显式给出 PostgreSQL 连接、统一 Redis 主机和其余
        # RAGFlow 存储主机，不能悄悄
        # 回退到 Docker 服务名，否则运行时会连接一个根本不存在的本地容器。
        for name in (
            "APP_DATABASE_DSN",
            "REDIS_STACK_HOST",
            "RAGFLOW_POSTGRES_HOST",
            "RAGFLOW_ES_HOST",
            "RAGFLOW_MINIO_HOST",
        ):
            if name not in source_values:
                raise SystemExit(f"{name} must be configured when RAG_STORAGE_MODE=external")

    # RAGFlow 的完整模型引用只能在真实注册后确定。首次启动先使用明确占位值，只供
    # RAGFlow 子系统启动；Bootstrap 随后通过官方 API 注册并原子写回最终引用。
    values["RAGFLOW_EMBEDDING_MODEL"] = values["RAGFLOW_EMBEDDING_MODEL"] or (
        "pending-model-bootstrap"
    )
    values["RAGFLOW_VISION_RUNTIME_MODEL"] = (
        values["RAGFLOW_VISION_RUNTIME_MODEL"] or (values["VISION_MODEL"])
    )
    values["RAGFLOW_VISION_MODEL"] = values["RAGFLOW_VISION_MODEL"] or ("pending-model-bootstrap")
    if not values["RAGFLOW_API_KEY"] and args.allow_pending_ragflow_key:
        values["RAGFLOW_API_KEY"] = "pending-local-initialization"
    if values["RAGFLOW_API_KEY"].startswith("change-me") and args.allow_pending_ragflow_key:
        values["RAGFLOW_API_KEY"] = "pending-local-initialization"
    _required(values, "RAGFLOW_API_KEY")
    _required(values, "POSTGRES_ADMIN_PASSWORD")
    _required(values, "RAGFLOW_POSTGRES_PASSWORD")
    _required(values, "ELASTIC_PASSWORD")
    _required(values, "MINIO_PASSWORD")
    _required(values, "REDIS_STACK_PASSWORD")
    _required(values, "RAGFLOW_ADMIN_EMAIL")
    _required(values, "RAGFLOW_ADMIN_PASSWORD")
    _required(values, "LLM_API_KEY")
    _required(values, "EMBEDDING_API_KEY")
    _required(values, "VISION_API_KEY")
    _require_capability(
        values,
        enabled_name="MEDIA_PROCESSING_ENABLED",
        field_names=("ASR_BASE_URL", "ASR_API_KEY", "ASR_MODEL"),
    )
    _require_capability(
        values,
        enabled_name="MEDIA_PROCESSING_ENABLED",
        field_names=(
            "MEDIA_VISION_BASE_URL",
            "MEDIA_VISION_API_KEY",
            "MEDIA_VISION_MODEL",
        ),
    )
    _require_capability(
        values,
        enabled_name="RERANK_ENABLED",
        field_names=("RERANK_BASE_URL", "RERANK_API_KEY", "RERANK_MODEL"),
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(f"{output.suffix}.tmp")
    temporary.write_text(_render_grouped_environment(values), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(output)
    print(f"created ignored local full-stack environment at {output}")


if __name__ == "__main__":
    main()
