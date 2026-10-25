"""Profile 测试：幂等导入、内容冲突、必需引用和图片解析配置合并。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from rag_service.application.profiles import ProfileCatalog
from rag_service.bootstrap.profiles import (
    ExistingProfile,
    ProfileSeed,
    import_profiles,
    load_profile_seeds,
)
from rag_service.domain.errors import ProfileConflictError, ProfileMissingError, RagError
from rag_service.settings import ServiceSettings


class FakeProfileStore:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str, int], ExistingProfile] = {}

    async def get(
        self, profile_type: str, name: str, version: int
    ) -> ExistingProfile | None:
        return self.items.get((profile_type, name, version))

    async def add(self, profile: ProfileSeed) -> None:
        self.items[(profile.profile_type, profile.name, profile.version)] = ExistingProfile(
            profile_type=profile.profile_type,
            name=profile.name,
            version=profile.version,
            checksum_sha256=profile.checksum_sha256,
            enabled=profile.enabled,
        )

    async def enabled_refs(self) -> frozenset[str]:
        return frozenset(
            item.ref for item in self.items.values() if item.enabled
        )


@pytest.mark.asyncio
async def test_profile_import_is_idempotent(service_settings: ServiceSettings) -> None:
    seeds = load_profile_seeds(service_settings.profiles)
    store = FakeProfileStore()

    first = await import_profiles(
        store, seeds, service_settings.profiles.required_refs
    )
    second = await import_profiles(
        store, seeds, service_settings.profiles.required_refs
    )

    assert first.inserted == len(seeds)
    assert first.skipped == 0
    assert second.inserted == 0
    assert second.skipped == len(seeds)
    assert first.total == second.total


@pytest.mark.asyncio
async def test_profile_import_rejects_checksum_conflict(
    service_settings: ServiceSettings,
) -> None:
    seeds = load_profile_seeds(service_settings.profiles)
    store = FakeProfileStore()
    await store.add(replace(seeds[0], checksum_sha256="0" * 64))

    with pytest.raises(ProfileConflictError):
        await import_profiles(store, seeds, ())


@pytest.mark.asyncio
async def test_profile_import_requires_enabled_references(
    service_settings: ServiceSettings,
) -> None:
    store = FakeProfileStore()

    with pytest.raises(ProfileMissingError):
        await import_profiles(store, (), ("missing@1",))


def test_versioned_retrieval_profiles_keep_immutable_rerank_reference(
    service_settings: ServiceSettings,
) -> None:
    """已落库的检索版本必须继续引用原 Rerank Profile，避免启动校验冲突。

    `hybrid-default@1` 到结构感知 `@5` 都已经进入真实部署的 Profile 表。后续供应商
    迁移只能新增版本，不能把其中的旧引用直接替换成通用 @2；实际模型仍由 Git 忽略
    环境变量决定，所以保留历史引用不会把运行时模型锁定为 BGE。
    """
    seeds = load_profile_seeds(service_settings.profiles)
    by_key = {(seed.profile_type, seed.ref): seed for seed in seeds}

    legacy_rerank = by_key[("RERANK", "bge-reranker-v2-m3@1")]
    immutable_retrieval_refs = (
        "hybrid-default@1",
        "hybrid-default@2",
        "hybrid-adaptive@3",
        "hybrid-multigranular@4",
        "hybrid-structure-aware@5",
    )

    for retrieval_ref in immutable_retrieval_refs:
        retrieval = by_key[("RETRIEVAL", retrieval_ref)]
        assert (
            retrieval.config_json["ragflow"]["rerank_profile"]
            == "bge-reranker-v2-m3@1"
        )
    assert legacy_rerank.config_json["provider"] == "openai_compatible_rerank"
    assert ("RERANK", "rerank-compatible@2") in by_key


def test_structure_aware_v6_adds_only_sidecar_similarity_threshold(
    service_settings: ServiceSettings,
) -> None:
    """v6 必须新增 0.40 侧车门槛，同时保持已发布 v5 内容不变。

    Profile 按引用和内容摘要不可变。这个测试既防止后续误把门槛补进 v5 导致启动
    校验和冲突，也确保 v6 除 Summary 索引门槛外继续沿用 v5 的召回、RRF、动态
    预算、实体、Parent-Child、后处理、回退和客户端覆盖策略。
    """
    seeds = load_profile_seeds(service_settings.profiles)
    by_key = {(seed.profile_type, seed.ref): seed for seed in seeds}
    v5 = by_key[("RETRIEVAL", "hybrid-structure-aware@5")].config_json
    v6 = by_key[("RETRIEVAL", "hybrid-structure-aware@6")].config_json

    assert "similarity_threshold" not in v5["summary_index"]
    assert v6["summary_index"]["similarity_threshold"] == pytest.approx(0.40)
    assert {
        key: value
        for key, value in v6["summary_index"].items()
        if key != "similarity_threshold"
    } == v5["summary_index"]
    for key in (
        "ragflow",
        "rrf",
        "adaptive_top_k",
        "parent_child",
        "entity_graph",
        "postprocess",
        "fallback",
        "client_overrides",
    ):
        assert v6[key] == v5[key]


def test_picture_profile_supports_images_and_preserves_chunk_method(
    service_settings: ServiceSettings,
) -> None:
    """图片解析保持 picture 路由，同时也能收到统一的分块长度与重叠百分比。"""
    catalog = ProfileCatalog.load(service_settings.profiles)

    for extension in (".png", ".jpg", ".jpeg", ".webp"):
        catalog.validate_document_profiles(
            parser_ref="picture-default@1",
            chunk_ref="general-512@1",
            extension=extension,
        )

    configuration = catalog.dataset_configuration(
        parser_ref="picture-default@1",
        chunk_ref="general-512@1",
        embedding_ref="embedding-openai-compatible@3",
        embedding_model="synthetic-embedding@fixture@OpenAI-API-Compatible",
    )

    assert configuration.chunk_method == "picture"
    assert configuration.parser_config["chunk_token_num"] == 512
    assert configuration.parser_config["overlapped_percent"] == 10


def test_default_chunk_profile_maps_ratio_to_ragflow_percent(
    service_settings: ServiceSettings,
) -> None:
    """新默认 Profile 必须向 RAGFlow 发送 256 Token 和整数 10% 重叠。"""
    catalog = ProfileCatalog.load(service_settings.profiles)

    configuration = catalog.dataset_configuration(
        parser_ref="deepdoc-default@1",
        chunk_ref="general-256@2",
        embedding_ref="embedding-openai-compatible@3",
        embedding_model="synthetic-embedding@fixture@OpenAI-API-Compatible",
    )

    assert configuration.chunk_method == "naive"
    assert configuration.parser_config["chunk_token_num"] == 256
    assert configuration.parser_config["overlapped_percent"] == 10


def test_file_families_select_immutable_chunk_profiles(
    service_settings: ServiceSettings,
) -> None:
    """文件类型自动路由必须只选已导入的不可变 Profile。

    这个测试不需要 RAGFlow，它锁定上传前的纯配置决策：正文、
    表格、视觉文档和媒体转写不会再全部套用同一个固定长度。
    """
    catalog = ProfileCatalog.load(service_settings.profiles)

    expected = {
        ".pdf": "prose-384@1",
        ".docx": "prose-384@1",
        ".json": "prose-384@1",
        ".jsonl": "prose-384@1",
        ".ldjson": "prose-384@1",
        ".xlsx": "table-192@1",
        ".csv": "table-192@1",
        ".pptx": "visual-192@1",
        ".png": "visual-192@1",
        ".mp3": "media-320@1",
        ".mp4": "media-320@1",
        ".unknown": "general-256@2",
    }
    assert {
        extension: catalog.recommended_document_chunk_profile(
            extension=extension,
            fallback_ref="general-256@2",
        )
        for extension in expected
    } == expected


def test_parser_routing_prefers_compatible_default_then_configured_file_rule(
    service_settings: ServiceSettings,
) -> None:
    """未显式选 Parser 时先保留兼容默认值，不兼容时才按配置规则切换。

    这个测试锁定混合知识库的核心决策：管理员为 PDF 选择 MinerU 时继续生效；
    Office 默认库收到 PDF、图片或媒体时则分别切到 DeepDOC、Picture 和 Media。
    未支持扩展名必须明确失败，不能把任意文件交给知识库默认解析器。
    """
    catalog = ProfileCatalog.load(service_settings.profiles)

    assert catalog.recommended_document_parser_profile(
        extension=".docx",
        preferred_ref="office-default@1",
    ) == "office-default@1"
    assert catalog.recommended_document_parser_profile(
        extension=".pdf",
        preferred_ref="mineru-default@1",
    ) == "mineru-default@1"
    assert catalog.recommended_document_parser_profile(
        extension=".pdf",
        preferred_ref="office-default@1",
    ) == "deepdoc-default@1"
    assert catalog.recommended_document_parser_profile(
        extension=".pdf",
        preferred_ref="office-default@1",
        document_hint="complex_formula_or_layout",
    ) == "mineru-default@1"
    assert catalog.recommended_document_parser_profile(
        extension=".png",
        preferred_ref="deepdoc-default@1",
    ) == "picture-default@1"
    assert catalog.recommended_document_parser_profile(
        extension=".mp3",
        preferred_ref="office-default@1",
    ) == "media-default@1"
    assert catalog.recommended_document_parser_profile(
        extension=".json",
        preferred_ref="office-default@1",
    ) == "json-default@1"
    assert catalog.recommended_document_parser_profile(
        extension=".jsonl",
        preferred_ref="deepdoc-default@1",
    ) == "json-default@1"
    assert catalog.recommended_document_parser_profile(
        extension=".ldjson",
        preferred_ref="picture-default@1",
    ) == "json-default@1"

    with pytest.raises(RagError) as unsupported:
        catalog.recommended_document_parser_profile(
            extension=".unknown",
            preferred_ref="office-default@1",
        )
    assert unsupported.value.code == "RAG-KB-004"


def test_table_and_prose_profiles_produce_distinct_engine_parameters(
    service_settings: ServiceSettings,
) -> None:
    """表格与长文 Profile 必须向 RAGFlow 产生不同长度和重叠百分比。"""
    catalog = ProfileCatalog.load(service_settings.profiles)

    table = catalog.parser_configuration(
        parser_ref="office-default@1",
        chunk_ref="table-192@1",
    )
    prose = catalog.parser_configuration(
        parser_ref="deepdoc-default@1",
        chunk_ref="prose-384@1",
    )

    assert table.parser_config["chunk_token_num"] == 192
    assert table.parser_config["overlapped_percent"] == 5
    assert prose.parser_config["chunk_token_num"] == 384
    assert prose.parser_config["overlapped_percent"] == 12


def test_vision_pdf_is_opt_in(service_settings: ServiceSettings) -> None:
    """新增视觉策略可显式选用，PDF 默认路由及旧 DeepDOC 配置不变。"""
    catalog = ProfileCatalog.load(service_settings.profiles)
    result = catalog.parser_configuration(parser_ref="vision-pdf@1", chunk_ref="prose-384@1")
    assert result.chunk_method == "naive"
    assert result.parser_config["layout_recognize"] == "${RAGFLOW_VISION_MODEL}"
    assert result.parser_config["task_page_size"] == 12
    assert catalog.recommended_document_parser_profile(
        extension=".pdf", preferred_ref="deepdoc-default@1",
    ) == "deepdoc-default@1"
    assert catalog.recommended_document_parser_profile(
        extension=".pdf", preferred_ref="vision-pdf@1",
    ) == "vision-pdf@1"


def test_local_pdf_default_override(service_settings: ServiceSettings) -> None:
    """本地默认覆盖仅影响自动 PDF 路由，不修改策略本身或其他文件族。"""
    settings = service_settings.profiles.model_copy(
        update={"pdf_default_parser_ref": "mineru-cloud@1"}
    )
    catalog = ProfileCatalog.load(settings)
    for preferred in ("deepdoc-default@1", "office-default@1", "vision-pdf@1"):
        assert catalog.recommended_document_parser_profile(
            extension=".PDF", preferred_ref=preferred,
        ) == "mineru-cloud@1"
    assert catalog.recommended_document_parser_profile(
        extension=".docx", preferred_ref="office-default@1",
    ) == "office-default@1"
    assert catalog.parser_configuration(
        parser_ref="deepdoc-default@1", chunk_ref="prose-384@1",
    ).parser_config["layout_recognize"] == "DeepDOC"


def test_mineru_image_is_opt_in(service_settings: ServiceSettings) -> None:
    """图片新策略可显式选择，默认图片和既有 PDF 策略保持原行为。"""
    catalog = ProfileCatalog.load(service_settings.profiles)
    for extension in ('.png', '.jpg', '.jpeg'):
        catalog.validate_document_profiles(
            parser_ref='mineru-image@1', chunk_ref='visual-192@1', extension=extension,
        )
        assert catalog.recommended_document_parser_profile(
            extension=extension, preferred_ref='picture-default@1',
        ) == 'picture-default@1'
    with pytest.raises(RagError):
        catalog.validate_document_profiles(
            parser_ref='mineru-cloud@1', chunk_ref='visual-192@1', extension='.png',
        )
