"""版本化 Profile 种子的读取、规范化、校验和幂等导入。

同名同版本 Profile 内容一旦入库就不可静默改变：规范 JSON 的 SHA-256 是内容身份，
再次导入相同内容会跳过，不同内容则启动失败，迫使配置作者创建新版本。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID, uuid4

import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rag_service.domain.errors import ProfileConflictError, ProfileMissingError
from rag_service.domain.models import ProfileRef
from rag_service.infrastructure.postgres.models import ConfigProfileRow
from rag_service.settings import ProfileSection

PROFILE_TYPES = frozenset(
    {"PARSER", "CHUNK", "EMBEDDING", "RETRIEVAL", "RERANK", "PROMPT", "MODEL", "CACHE"}
)


class SeedItem(BaseModel):
    """种子文件中的通用 Profile 项；额外字段就是该类型的实际配置。"""
    model_config = ConfigDict(extra="allow")
    ref: str
    enabled: bool = True
    description: str | None = None


class ParserSeedFile(BaseModel):
    """解析 Profile 文件的顶层 Schema。"""
    model_config = ConfigDict(extra="forbid")
    schema_version: int = Field(ge=1)
    profiles: list[SeedItem]
    selection_rules: list[dict[str, Any]] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ParserSelectionRule:
    """已规范化的 Parser 自动选择规则。

    规则顺序沿用 YAML 声明顺序；带 ``document_hint`` 的规则只有在调用方明确提供
    同名提示时才会命中，避免普通上传误选需要额外能力或成本的解析器。
    """

    extensions: frozenset[str]
    parser_ref: str
    document_hint: str | None = None


class RetrievalSeedFile(BaseModel):
    """分块与检索 Profile 文件的顶层 Schema。"""
    model_config = ConfigDict(extra="forbid")
    schema_version: int = Field(ge=1)
    chunk_profiles: list[SeedItem]
    retrieval_profiles: list[SeedItem]


class ModelSeedFile(BaseModel):
    """Embedding、Rerank 与 LLM Profile 文件的顶层 Schema。"""
    model_config = ConfigDict(extra="forbid")
    schema_version: int = Field(ge=1)
    embedding_profiles: list[SeedItem]
    rerank_profiles: list[SeedItem]
    llm_profiles: list[SeedItem]


class PromptSeedFile(BaseModel):
    """Prompt 模板 Profile 文件的顶层 Schema。"""
    model_config = ConfigDict(extra="forbid")
    schema_version: int = Field(ge=1)
    profiles: list[SeedItem]


@dataclass(frozen=True, slots=True)
class ProfileSeed:
    """完成解析、规范化与摘要计算后可写入数据库的 Profile。"""
    id: UUID
    profile_type: str
    name: str
    version: int
    description: str | None
    config_json: dict[str, Any]
    checksum_sha256: str
    enabled: bool

    @property
    def ref(self) -> str:
        """返回供配置与 API 使用的 ``name@version`` 引用。"""
        return f"{self.name}@{self.version}"


@dataclass(frozen=True, slots=True)
class ExistingProfile:
    """判断数据库现有版本是否与种子一致所需的最小视图。"""
    profile_type: str
    name: str
    version: int
    checksum_sha256: str
    enabled: bool

    @property
    def ref(self) -> str:
        """返回现有记录的标准版本引用。"""
        return f"{self.name}@{self.version}"


@dataclass(frozen=True, slots=True)
class ProfileImportResult:
    """一次导入新增、跳过和总种子数量。"""
    inserted: int
    skipped: int
    total: int


class ProfileStore(Protocol):
    """Profile 导入只需要的查询、插入和已启用引用集合接口。"""

    async def get(
        self, profile_type: str, name: str, version: int
    ) -> ExistingProfile | None:
        """按类型、名称和版本查找现有配置；不存在时返回 ``None``。"""
        ...

    async def add(self, profile: ProfileSeed) -> None:
        """把已校验且尚不存在的配置种子加入当前事务。"""
        ...

    async def enabled_refs(self) -> frozenset[str]:
        """返回所有已启用配置的 ``名称@版本`` 引用，用于核对必需配置。"""
        ...


class SqlAlchemyProfileStore:
    """基于当前 SQLAlchemy 事务的 ProfileStore 实现。"""
    def __init__(self, session: AsyncSession) -> None:
        """保存由调用方管理事务生命周期的异步 Session。"""
        self._session = session

    async def get(
        self, profile_type: str, name: str, version: int
    ) -> ExistingProfile | None:
        """按类型、名称、版本唯一键读取现有摘要与启用状态。"""
        row = await self._session.scalar(
            select(ConfigProfileRow).where(
                ConfigProfileRow.profile_type == profile_type,
                ConfigProfileRow.name == name,
                ConfigProfileRow.version == version,
            )
        )
        if row is None:
            return None
        return ExistingProfile(
            profile_type=row.profile_type,
            name=row.name,
            version=row.version,
            checksum_sha256=row.checksum_sha256,
            enabled=row.enabled,
        )

    async def add(self, profile: ProfileSeed) -> None:
        """插入 Profile 并立即 flush，使唯一键等数据库错误在当前步骤暴露。"""
        self._session.add(
            ConfigProfileRow(
                id=profile.id,
                profile_type=profile.profile_type,
                name=profile.name,
                version=profile.version,
                description=profile.description,
                config_json=profile.config_json,
                checksum_sha256=profile.checksum_sha256,
                enabled=profile.enabled,
            )
        )
        await self._session.flush()

    async def enabled_refs(self) -> frozenset[str]:
        """返回所有类型中已启用的 ``name@version`` 集合，用于启动必需项检查。"""
        rows = (
            await self._session.execute(
                select(ConfigProfileRow.name, ConfigProfileRow.version).where(
                    ConfigProfileRow.enabled.is_(True)
                )
            )
        ).all()
        return frozenset(f"{name}@{version}" for name, version in rows)


def _canonicalize(value: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """用排序键、紧凑 JSON 和禁用 NaN 生成稳定内容与 SHA-256。"""
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return json.loads(encoded), hashlib.sha256(encoded).hexdigest()


def _make_seed(profile_type: str, item: SeedItem) -> ProfileSeed:
    """验证 Profile 类型与引用格式，移除 Pydantic 差异并生成可导入记录。"""
    if profile_type not in PROFILE_TYPES:
        raise ValueError(f"unsupported profile type: {profile_type}")
    ref = ProfileRef.parse(item.ref)
    raw = item.model_dump(mode="json", exclude_none=False)
    canonical, checksum = _canonicalize(raw)
    return ProfileSeed(
        id=uuid4(),
        profile_type=profile_type,
        name=ref.name,
        version=ref.version,
        description=item.description,
        config_json=canonical,
        checksum_sha256=checksum,
        enabled=item.enabled,
    )


def _resolve_seed_path(settings: ProfileSection, filename: str) -> Path:
    """把白名单种子文件解析为安全路径，并拒绝逃逸配置目录。"""
    path = (settings.seed_dir / filename).resolve()
    try:
        path.relative_to(settings.seed_dir.resolve())
    except ValueError as exc:
        raise ValueError(f"profile seed path escapes seed directory: {filename}") from exc
    return path


def _normalize_extension(value: Any) -> str:
    """把规则扩展名规范为小写 ``.ext``，拒绝模糊或空值。"""
    if not isinstance(value, str) or len(value) < 2 or not value.startswith("."):
        raise ValueError("parser selection rule extension must use .ext format")
    return value.lower()


@lru_cache(maxsize=8)
def load_parser_selection_rules(
    settings: ProfileSection,
) -> tuple[ParserSelectionRule, ...]:
    """读取并严格规范 Parser 路由规则，作为运行时唯一的自动选择配置。

    该函数只缓存不含密钥的本地策略文件结果。配置错误会阻止目录创建，不能静默
    回退到硬编码映射，否则 API 与 Worker 可能在不同进程中选出不同解析器。
    """
    filename = "parser_profiles.example.yaml"
    if filename not in settings.seed_files:
        raise ValueError("parser profile seed file is required for parser routing")
    raw = yaml.safe_load(
        _resolve_seed_path(settings, filename).read_text(encoding="utf-8")
    )
    parser_file = ParserSeedFile.model_validate(raw)
    rules: list[ParserSelectionRule] = []
    for raw_rule in parser_file.selection_rules:
        if set(raw_rule) != {"when", "use"}:
            raise ValueError("parser selection rule must contain only when and use")
        when = raw_rule["when"]
        parser_ref = raw_rule["use"]
        if not isinstance(when, dict) or not isinstance(parser_ref, str):
            raise ValueError("parser selection rule when/use schema is invalid")
        if set(when) - {"extension", "extension_in", "document_hint"}:
            raise ValueError("parser selection rule contains unsupported conditions")
        extension = when.get("extension")
        extension_in = when.get("extension_in")
        if (extension is None) == (extension_in is None):
            raise ValueError(
                "parser selection rule must define exactly one extension selector"
            )
        if extension is not None:
            extensions = frozenset({_normalize_extension(extension)})
        else:
            if not isinstance(extension_in, list) or not extension_in:
                raise ValueError("parser selection rule extension_in must be a non-empty list")
            extensions = frozenset(_normalize_extension(item) for item in extension_in)
        document_hint = when.get("document_hint")
        if document_hint is not None and (
            not isinstance(document_hint, str) or not document_hint.strip()
        ):
            raise ValueError("parser selection rule document_hint must be non-empty")
        # 这里解析引用格式，但不检查是否启用或支持扩展名；后者必须和数据库中实际
        # 启用的 Profile 对照，由 ProfileCatalog 在每个进程中统一验证。
        parsed_ref = ProfileRef.parse(parser_ref)
        rules.append(
            ParserSelectionRule(
                extensions=extensions,
                parser_ref=f"{parsed_ref.name}@{parsed_ref.version}",
                document_hint=document_hint,
            )
        )
    return tuple(rules)


def load_profile_seeds(settings: ProfileSection) -> tuple[ProfileSeed, ...]:
    """按白名单文件名解析所有种子，并拒绝目录穿越和重复版本键。"""
    seeds: list[ProfileSeed] = []
    for filename in settings.seed_files:
        path = _resolve_seed_path(settings, filename)
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if filename == "parser_profiles.example.yaml":
            parser_file = ParserSeedFile.model_validate(raw)
            seeds.extend(_make_seed("PARSER", item) for item in parser_file.profiles)
        elif filename == "retrieval_profiles.example.yaml":
            retrieval_file = RetrievalSeedFile.model_validate(raw)
            seeds.extend(_make_seed("CHUNK", item) for item in retrieval_file.chunk_profiles)
            seeds.extend(
                _make_seed("RETRIEVAL", item) for item in retrieval_file.retrieval_profiles
            )
        elif filename == "model_profiles.example.yaml":
            model_file = ModelSeedFile.model_validate(raw)
            seeds.extend(
                _make_seed("EMBEDDING", item) for item in model_file.embedding_profiles
            )
            seeds.extend(_make_seed("RERANK", item) for item in model_file.rerank_profiles)
            seeds.extend(_make_seed("MODEL", item) for item in model_file.llm_profiles)
        elif filename == "prompts.example.yaml":
            prompt_file = PromptSeedFile.model_validate(raw)
            seeds.extend(_make_seed("PROMPT", item) for item in prompt_file.profiles)
        else:
            raise ValueError(f"unrecognized profile seed file: {filename}")

    keys = [(seed.profile_type, seed.name, seed.version) for seed in seeds]
    TypeAdapter(list[tuple[str, str, int]]).validate_python(keys)
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate profile type/name/version in seed files")
    return tuple(seeds)


async def import_profiles(
    store: ProfileStore,
    seeds: tuple[ProfileSeed, ...],
    required_refs: tuple[str, ...],
) -> ProfileImportResult:
    """幂等导入种子，检测不可变版本冲突，并确认所有运行必需引用已启用。"""
    inserted = 0
    skipped = 0
    for seed in seeds:
        existing = await store.get(seed.profile_type, seed.name, seed.version)
        if existing is None:
            await store.add(seed)
            inserted += 1
            continue
        if existing.checksum_sha256 != seed.checksum_sha256:
            raise ProfileConflictError(f"{seed.profile_type}:{seed.ref}")
        skipped += 1

    enabled = await store.enabled_refs()
    missing = sorted(set(required_refs) - enabled)
    if missing:
        raise ProfileMissingError(missing)
    return ProfileImportResult(inserted=inserted, skipped=skipped, total=len(seeds))
