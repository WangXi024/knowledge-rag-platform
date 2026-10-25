"""运行时 Profile 目录和到 RAGFlow 数据集配置的转换。

Profile 是带名称与版本的不可变配置。应用层只通过引用读取已启用版本；返回深拷贝
避免调用方修改目录中的共享字典。解析 Profile 与分块 Profile 最终在这里合并成
RAGFlow 能理解的数据集参数。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from math import isfinite
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from rag_service.bootstrap.profiles import (
    ParserSelectionRule,
    ProfileSeed,
    load_parser_selection_rules,
    load_profile_seeds,
)
from rag_service.domain.errors import RagError
from rag_service.infrastructure.postgres.models import ConfigProfileRow
from rag_service.settings import ProfileSection

_TABLE_EXTENSIONS = frozenset({".xls", ".xlsx", ".csv"})
_VISUAL_EXTENSIONS = frozenset({".ppt", ".pptx", ".png", ".jpg", ".jpeg", ".webp"})
_MEDIA_EXTENSIONS = frozenset(
    {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".mp4", ".mov", ".mkv", ".webm", ".avi"}
)
_PROSE_EXTENSIONS = frozenset(
    {
        ".pdf",
        ".doc",
        ".docx",
        ".txt",
        ".md",
        ".html",
        ".htm",
        ".json",
        ".jsonl",
        ".ldjson",
    }
)


@dataclass(frozen=True, slots=True)
class ParserEngineConfiguration:
    """单个 RAGFlow 文档解析前需要应用的解析器与分块参数。

    Dataset 仍保存创建知识库时的安全默认值；上传文件可以根据格式选择更合适的
    Chunk Profile，Worker 再通过 RAGFlow 官方文档更新接口应用本对象。这样表格、
    演示文稿和长篇正文能够共存于同一知识库，而不会修改 RAGFlow 源码，也不会让
    本地 DocumentRecord 记录一个实际没有生效的 Profile。
    """

    chunk_method: str
    parser_config: dict[str, Any]


@dataclass(frozen=True, slots=True)
class DatasetEngineConfiguration:
    """创建/更新外部数据集需要的分块方法、解析参数和 Embedding 模型。"""
    chunk_method: str
    parser_config: dict[str, Any]
    embedding_model: str


class ProfileCatalog:
    """按“类型 + 名称@版本”索引的只读、已启用 Profile 集合。"""
    def __init__(
        self,
        profiles: tuple[ProfileSeed, ...],
        parser_selection_rules: tuple[ParserSelectionRule, ...] = (),
        pdf_default_parser_ref: str | None = None,
    ) -> None:
        """过滤禁用项，构造常数时间查找表并验证 Parser 路由契约。"""
        self._profiles = {
            (profile.profile_type, profile.ref): profile for profile in profiles if profile.enabled
        }
        self._parser_selection_rules = parser_selection_rules
        self._validate_parser_selection_rules()
        # 默认覆盖必须引用已启用且支持 PDF 的策略，配置错误在启动时暴露。
        self._pdf_default_parser_ref = pdf_default_parser_ref
        if pdf_default_parser_ref and ".pdf" not in self._supported_parser_extensions(
            pdf_default_parser_ref
        ):
            raise RagError("RAG-CONFIG-002", "PDF 默认策略不支持 PDF", http_status=422)

    @classmethod
    def load(cls, settings: ProfileSection) -> ProfileCatalog:
        """从配置种子文件加载目录，主要用于启动和无数据库场景。"""
        return cls(
            load_profile_seeds(settings),
            load_parser_selection_rules(settings),
            settings.pdf_default_parser_ref,
        )

    @classmethod
    async def from_database(
        cls,
        session: AsyncSession,
        settings: ProfileSection,
    ) -> ProfileCatalog:
        """从数据库读取启用版本，并加载配置文件中的有序 Parser 路由规则。"""
        rows = (
            await session.scalars(
                select(ConfigProfileRow).where(ConfigProfileRow.enabled.is_(True))
            )
        ).all()
        return cls(
            tuple(
                ProfileSeed(
                    id=row.id,
                    profile_type=row.profile_type,
                    name=row.name,
                    version=row.version,
                    description=row.description,
                    config_json=row.config_json,
                    checksum_sha256=row.checksum_sha256,
                    enabled=row.enabled,
                )
                for row in rows
            ),
            load_parser_selection_rules(settings),
            settings.pdf_default_parser_ref,
        )

    def _supported_parser_extensions(self, parser_ref: str) -> frozenset[str]:
        """返回 Parser 声明的规范扩展名集合，并拒绝无效 Profile Schema。"""
        parser = self.get("PARSER", parser_ref)
        supported = parser.get("supported_extensions")
        if not isinstance(supported, list) or not all(
            isinstance(item, str) and item.startswith(".") for item in supported
        ):
            raise RagError(
                "RAG-CONFIG-002",
                "Parser Profile Schema 无效",
                http_status=422,
                retryable=False,
            )
        return frozenset(item.lower() for item in supported)

    def _validate_parser_selection_rules(self) -> None:
        """保证每条路由规则只把文件交给已启用且声明兼容的 Parser。"""
        for rule in self._parser_selection_rules:
            try:
                supported = self._supported_parser_extensions(rule.parser_ref)
            except RagError as exc:
                raise RagError(
                    "RAG-CONFIG-002",
                    "Parser 自动选择规则引用了无效 Profile",
                    http_status=422,
                    retryable=False,
                ) from exc
            if not rule.extensions.issubset(supported):
                raise RagError(
                    "RAG-CONFIG-002",
                    "Parser 自动选择规则与 Profile 支持类型不一致",
                    http_status=422,
                    retryable=False,
                )

    def get(self, profile_type: str, ref: str) -> dict[str, Any]:
        """读取指定配置的深拷贝；不存在或禁用时返回不可重试的 422。"""
        profile = self._profiles.get((profile_type, ref))
        if profile is None:
            raise RagError(
                "RAG-CONFIG-001",
                "配置 Profile 不存在",
                http_status=422,
                retryable=False,
            )
        return deepcopy(profile.config_json)

    def dataset_configuration(
        self,
        *,
        parser_ref: str,
        chunk_ref: str,
        embedding_ref: str,
        embedding_model: str,
    ) -> DatasetEngineConfiguration:
        """校验并合并 Parser、Chunk、Embedding 引用为引擎数据集配置。

        Parser 决定 RAGFlow 的解析器和基础配置，Chunk 覆盖块大小、分隔符和相邻块
        重叠比例；Embedding 引用在此确认存在，而真实运行模型由经过启动契约校验的
        配置传入。

        项目配置使用便于阅读的 ``overlap_ratio``（例如 0.10 表示 10%），RAGFlow
        v0.26.4 的解析内核则读取 ``overlapped_percent``（例如 10）。这里完成业务
        比例到引擎百分比的单位转换；RAGFlow 公开 Dataset API 的版本兼容封装由
        Adapter 统一处理，调用方不需要理解其 ``ext`` 传输字段。
        """
        document_configuration = self.parser_configuration(
            parser_ref=parser_ref,
            chunk_ref=chunk_ref,
        )
        self.get("EMBEDDING", embedding_ref)
        return DatasetEngineConfiguration(
            chunk_method=document_configuration.chunk_method,
            parser_config=document_configuration.parser_config,
            embedding_model=embedding_model,
        )

    def parser_configuration(
        self,
        *,
        parser_ref: str,
        chunk_ref: str,
    ) -> ParserEngineConfiguration:
        """把版本化 Parser 与 Chunk Profile 合并为文档级 RAGFlow 配置。

        该方法不读取模型或数据库，也不会发起外部请求。OfflineService 先把自动选择
        结果保存进 DocumentRecord；Worker 在真正开始解析前重新从数据库 Profile
        目录调用本方法，因此任务重试、服务重启和显式重解析都会得到同一不可变配置。
        非法引用、Schema、长度或重叠比例继续以稳定配置错误失败，绝不静默回退。
        """
        parser = self.get("PARSER", parser_ref)
        chunk = self.get("CHUNK", chunk_ref)
        ragflow = parser.get("ragflow")
        if not isinstance(ragflow, dict):
            raise RagError(
                "RAG-CONFIG-002",
                "Parser Profile Schema 无效",
                http_status=422,
                retryable=False,
            )
        raw_parser_config = ragflow.get("parser_config")
        chunk_method = ragflow.get("chunk_method")
        if not isinstance(chunk_method, str) or not chunk_method.strip():
            raise RagError(
                "RAG-CONFIG-002",
                "Parser Profile Schema 无效",
                http_status=422,
                retryable=False,
            )
        if not isinstance(raw_parser_config, dict):
            raise RagError(
                "RAG-CONFIG-002",
                "Parser Profile Schema 无效",
                http_status=422,
                retryable=False,
            )
        parser_config = deepcopy(raw_parser_config)
        chunk_token_num = chunk.get("chunk_token_num")
        delimiter = chunk.get("delimiter")
        overlap_ratio = chunk.get("overlap_ratio")
        if (
            not isinstance(chunk_token_num, int)
            or isinstance(chunk_token_num, bool)
            or chunk_token_num <= 0
            or not isinstance(delimiter, str)
            or not delimiter
            or not isinstance(overlap_ratio, (int, float))
            or isinstance(overlap_ratio, bool)
        ):
            raise RagError(
                "RAG-CONFIG-002",
                "Chunk Profile Schema 无效",
                http_status=422,
                retryable=False,
            )

        # RAGFlow 会把重叠百分比限制在 0～90。项目层只接受 0～0.90 的比例值，
        # 因而 0.10 会稳定转换为整数 10；超出范围或 NaN/Infinity 会在启动/请求阶段
        # 明确失败，不能静默退回到无重叠切块。
        normalized_overlap_ratio = float(overlap_ratio)
        if not isfinite(normalized_overlap_ratio) or not 0.0 <= normalized_overlap_ratio <= 0.90:
            raise RagError(
                "RAG-CONFIG-002",
                "Chunk Profile Schema 无效",
                http_status=422,
                retryable=False,
            )
        parser_config["chunk_token_num"] = chunk_token_num
        parser_config["delimiter"] = delimiter
        parser_config["overlapped_percent"] = round(normalized_overlap_ratio * 100)
        return ParserEngineConfiguration(
            chunk_method=chunk_method,
            parser_config=parser_config,
        )

    def recommended_document_chunk_profile(
        self,
        *,
        extension: str,
        fallback_ref: str,
    ) -> str:
        """按文件格式选择不可变 Chunk Profile，显式调用方仍可覆盖结果。

        选择只依赖扩展名，不读取正文或用户数据，因此上传前即可确定且具备幂等性：
        表格按行边界使用较小块，PPT/图片使用视觉短块，音视频转写使用较长时间段，
        PDF/Word/文本使用能保留完整段落的正文块。若未来精简配置导致推荐版本未导入，
        会保留已经由知识库验证的 ``fallback_ref``，不会令旧部署突然无法上传。
        """
        normalized = extension.lower()
        if normalized in _TABLE_EXTENSIONS:
            recommended = "table-192@1"
        elif normalized in _VISUAL_EXTENSIONS:
            recommended = "visual-192@1"
        elif normalized in _MEDIA_EXTENSIONS:
            recommended = "media-320@1"
        elif normalized in _PROSE_EXTENSIONS:
            recommended = "prose-384@1"
        else:
            recommended = fallback_ref
        return (
            recommended
            if ("CHUNK", recommended) in self._profiles
            else fallback_ref
        )

    def recommended_document_parser_profile(
        self,
        *,
        extension: str,
        preferred_ref: str,
        document_hint: str | None = None,
    ) -> str:
        """为未显式指定 Parser 的上传选择有效版本。

        环境显式配置 PDF 默认覆盖时优先采用；否则保留知识库或当前文档的兼容默认值，
        使管理员为 PDF 配置的 MinerU 等策略
        继续生效；默认值不兼容时才按 YAML 顺序匹配文件扩展名。带内容提示的高成本
        规则只有在调用方明确传入相同提示时命中。没有任何匹配时返回稳定 422，绝不
        把未知文件强行交给默认解析器。
        """
        normalized = extension.lower()
        if normalized == ".pdf" and self._pdf_default_parser_ref:
            return self._pdf_default_parser_ref
        if normalized in self._supported_parser_extensions(preferred_ref):
            return preferred_ref
        for rule in self._parser_selection_rules:
            if normalized not in rule.extensions:
                continue
            if rule.document_hint is not None and rule.document_hint != document_hint:
                continue
            return rule.parser_ref
        raise RagError(
            "RAG-KB-004",
            "没有支持该文件类型的 Parser Profile",
            http_status=422,
            retryable=False,
        )

    def validate_document_profiles(
        self,
        *,
        parser_ref: str,
        chunk_ref: str,
        extension: str,
    ) -> None:
        """确认文档选择的 Parser/Chunk 存在，且 Parser 支持文件扩展名。"""
        self.get("CHUNK", chunk_ref)
        if extension.lower() not in self._supported_parser_extensions(parser_ref):
            raise RagError(
                "RAG-KB-004",
                "Parser Profile 不支持该文件类型",
                http_status=422,
                retryable=False,
            )
