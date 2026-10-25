"""``rag-service`` 管理命令入口。

该命令供容器启动流程使用：``schema prepare-local`` 只在 local 模式幂等初始化应用库，
``schema check`` 只读校验最终数据库结构；``bootstrap all`` 校验依赖、导入配置并创建
语义索引；``preflight dependencies`` 只做只读连通性和模型契约检查。结果以单行 JSON
输出，便于 Compose/CI 判断成功。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence

from redis.asyncio import Redis

from rag_service.bootstrap.cache_index import RedisSemanticIndexBootstrap
from rag_service.bootstrap.database_schema import (
    check_app_database_schema,
    check_database_schemas,
    execute_migration,
    prepare_local_app_database_schema,
    prepare_local_ragflow_schema,
)
from rag_service.bootstrap.profiles import (
    SqlAlchemyProfileStore,
    import_profiles,
    load_profile_seeds,
)
from rag_service.bootstrap.runtime import RuntimeDependencyPreflight
from rag_service.infrastructure.postgres.session import create_engine, create_session_factory
from rag_service.settings import get_settings


async def bootstrap_all() -> dict[str, object]:
    """按依赖预检、Profile 导入、Redis 索引初始化的顺序完成一次性启动准备。"""
    settings = get_settings()
    # 先验证模型与 RAGFlow，避免数据库已写入一半配置后才发现外部契约错误。
    preflight = RuntimeDependencyPreflight(settings)
    try:
        runtime_result = await preflight.check()
    finally:
        await preflight.close()
    # Profile 导入在单个数据库事务中执行；任何冲突都会使整批导入回滚。
    seeds = load_profile_seeds(settings.profiles)
    engine = create_engine(settings.database)
    factory = create_session_factory(engine)
    try:
        async with factory() as session, session.begin():
            profile_result = await import_profiles(
                SqlAlchemyProfileStore(session),
                seeds,
                settings.profiles.required_refs,
            )
    finally:
        await engine.dispose()

    # 最后验证或创建向量索引，其字段和维度必须与当前 Embedding 模型完全一致。
    redis_client = Redis.from_url(
        settings.cache.url,
        decode_responses=False,
        socket_connect_timeout=settings.redis.socket_connect_timeout_seconds,
        socket_timeout=settings.redis.socket_timeout_seconds,
    )
    try:
        cache_result = await RedisSemanticIndexBootstrap(
            redis_client, settings.cache.semantic
        ).ensure()
    finally:
        await redis_client.aclose()

    return {
        "runtime_preflight": {
            "model_api": runtime_result.model_api,
            "chat_model": runtime_result.chat_model,
            "embedding_model": runtime_result.embedding_model,
            "vision_model": runtime_result.vision_model,
            "media_asr": runtime_result.media_asr,
            "media_vision_model": runtime_result.media_vision_model,
            "embedding_dimension": runtime_result.embedding_dimension,
            "ragflow": runtime_result.ragflow,
            "ragflow_api_key": runtime_result.ragflow_api_key,
            "rerank": runtime_result.rerank,
        },
        "profiles": {
            "inserted": profile_result.inserted,
            "skipped": profile_result.skipped,
            "total": profile_result.total,
        },
        "semantic_cache_index": {
            "created": cache_result.created,
            "name": cache_result.index_name,
            "dimension": cache_result.dimension,
        },
    }


def build_parser() -> argparse.ArgumentParser:
    """构造命令行语法；使用受限 choices，避免接受未实现的目标名称。"""
    parser = argparse.ArgumentParser(prog="rag-service")
    commands = parser.add_subparsers(dest="command", required=True)
    bootstrap = commands.add_parser("bootstrap")
    bootstrap.add_argument("target", choices=("all",))
    preflight = commands.add_parser("preflight")
    preflight.add_argument("target", choices=("dependencies",))
    schema = commands.add_parser("schema")
    schema.add_argument(
        "target",
        choices=("prepare-local", "prepare-local-ragflow", "check", "check-app", "migrate"),
    )
    # migrate 是管理员明确选择的写入入口，必须指定候选镜像内的数据库及 SQL 文件。
    schema.add_argument("--database", choices=("rag_app", "rag_flow"))
    schema.add_argument("--file")
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """解析参数、运行对应异步流程，并把机器可读结果写到标准输出。"""
    args = build_parser().parse_args(argv)
    if args.command == "schema" and args.target == "migrate":
        if not args.database or not args.file:
            build_parser().error("schema migrate requires --database and --file")
        print(json.dumps(asyncio.run(execute_migration(args.database, args.file)), sort_keys=True))
        return
    if args.command == "schema" and args.target == "prepare-local-ragflow":
        print(json.dumps(asyncio.run(prepare_local_ragflow_schema()), sort_keys=True))
        return
    if args.command == "bootstrap" and args.target == "all":
        print(json.dumps(asyncio.run(bootstrap_all()), ensure_ascii=False, sort_keys=True))
    elif args.command == "preflight" and args.target == "dependencies":
        settings = get_settings()

        async def run_preflight() -> dict[str, object]:
            """执行只读依赖预检，并确保无论成功失败都关闭 HTTP 客户端。"""
            preflight = RuntimeDependencyPreflight(settings)
            try:
                result = await preflight.check()
            finally:
                await preflight.close()
            return {
                "model_api": result.model_api,
                "chat_model": result.chat_model,
                "embedding_model": result.embedding_model,
                "vision_model": result.vision_model,
                "media_asr": result.media_asr,
                "media_vision_model": result.media_vision_model,
                "embedding_dimension": result.embedding_dimension,
                "ragflow": result.ragflow,
                "ragflow_api_key": result.ragflow_api_key,
                "rerank": result.rerank,
            }

        print(
            json.dumps(
                asyncio.run(run_preflight()),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    elif args.command == "schema" and args.target == "prepare-local":
        # 这是唯一允许写 PostgreSQL Schema 的 CLI 路径；实现层会再次校验 local，
        # 防止调用方只靠命令名称绕过 external 的 DBA 管理边界。
        print(
            json.dumps(
                asyncio.run(prepare_local_app_database_schema()),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    elif args.command == "schema" and args.target == "check":
        print(
            json.dumps(
                asyncio.run(check_database_schemas()),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    elif args.command == "schema" and args.target == "check-app":
        print(
            json.dumps(
                asyncio.run(check_app_database_schema()),
                ensure_ascii=False,
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # 配置校验或数据库连接异常可能带 DSN/密码；CLI 只报告失败，不打印原始对象。
        print("管理命令失败：请检查目标库、配置、迁移前置条件及依赖状态。", file=sys.stderr)
        raise SystemExit(1) from None
