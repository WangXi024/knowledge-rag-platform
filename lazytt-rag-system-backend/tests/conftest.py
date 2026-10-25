"""全套测试共享夹具：完整、可覆盖的直连用户模式服务配置对象。"""

from __future__ import annotations

from pathlib import Path

import pytest

from rag_service.settings import (
    AppSection,
    CacheSection,
    DatabaseSection,
    EmbeddingSection,
    IdentitySection,
    JobsSection,
    LlmSection,
    ModelsSection,
    ProfileSection,
    RagFlowSection,
    RedisSection,
    RerankSection,
    SemanticCacheSection,
    ServiceSettings,
    UploadSection,
    VisionSection,
)


@pytest.fixture
def service_settings() -> ServiceSettings:
    repo_root = Path(__file__).resolve().parents[1]
    return ServiceSettings(
        app=AppSection(),
        identity=IdentitySection(),
        database=DatabaseSection(dsn="postgresql+psycopg://unused:unused@127.0.0.1/unused"),
        redis=RedisSection(url="redis://127.0.0.1:6379/2"),
        ragflow=RagFlowSection(
            base_url="http://127.0.0.1:9380",
            api_key="test-only",
            embedding_model=(
                "synthetic-embedding@lazytt-embedding-fixture@OpenAI-API-Compatible"
            ),
            vision_model="synthetic-vision@lazytt-vision-fixture@OpenAI-API-Compatible",
            vision_runtime_model="synthetic-vision",
            embedding_dimension=1024,
        ),
        uploads=UploadSection(
            staging_dir=repo_root / ".tmp" / "uploads",
            allowed_extensions=(".pdf", ".txt", ".md"),
        ),
        jobs=JobsSection(),
        models=ModelsSection(
            llm=LlmSection(
                base_url="https://chat.invalid/v1",
                api_key="synthetic-chat-key",
                model="synthetic-chat",
                rewrite_model="synthetic-chat",
            ),
            embedding=EmbeddingSection(
                base_url="https://embedding.invalid/v1",
                api_key="synthetic-embedding-key",
                model="synthetic-embedding",
                dimension=1024,
            ),
            vision=VisionSection(
                base_url="https://vision.invalid/v1",
                api_key="synthetic-vision-key",
                model="synthetic-vision",
            ),
            rerank=RerankSection(enabled=False),
        ),
        profiles=ProfileSection(
            seed_dir=repo_root / "rag-development-spec" / "configs",
            seed_files=(
                "parser_profiles.example.yaml",
                "retrieval_profiles.example.yaml",
                "model_profiles.example.yaml",
                "prompts.example.yaml",
            ),
            required_refs=(
                "deepdoc-default@1",
                "json-default@1",
                "general-512@1",
                "general-256@2",
                "prose-384@1",
                "table-192@1",
                "visual-192@1",
                "media-320@1",
                "embedding-openai-compatible@3",
                "answer-openai-compatible@4",
                "rewrite-openai-compatible@4",
                "rerank-compatible@2",
                "hybrid-default@1",
                "hybrid-structure-aware@5",
                "hybrid-structure-aware@6",
            ),
        ),
        # 测试环境仍使用短名称模拟运行时模型；Profile 引用已经完全迁移到供应商无关
        # 名称，二者职责不同，不能用 Profile 名称冒充真实模型标识。
        cache=CacheSection(
            url="redis://127.0.0.1:6379/3",
            semantic=SemanticCacheSection(
                embedding_model="synthetic-embedding",
                embedding_dimension=1024,
            ),
        ),
        build_commit="test-commit",
        build_time="2026-07-27T00:00:00Z",
    )
