"""侧车索引纯算法测试：层级摘要、重叠删除、实体关系与向量附加。"""

from __future__ import annotations

from uuid import uuid4

from rag_service.application.indexing import (
    attach_embeddings,
    build_hierarchical_nodes,
    build_lightweight_graph,
    extract_structured_metadata,
)
from rag_service.domain.models import DocumentRecord, IndexNodeType
from rag_service.domain.ports import EngineChunk


def test_build_hierarchical_nodes_preserves_real_source_chunk_ids() -> None:
    """四个连续子块组成 Parent，摘要只引用真实源块且不会重复 overlap 文本。"""
    document = DocumentRecord(
        security_scope_id="a" * 64,
        knowledge_base_id=uuid4(),
        filename="fixture.pdf",
        safe_filename="fixture.pdf",
        mime_type="application/pdf",
        file_size_bytes=7,
        content_sha256="b" * 64,
        parser_profile_ref="deepdoc-default@1",
        chunk_profile_ref="general-256@2",
    )
    overlap = "这是两个相邻子块之间足够长的共同文字。"
    chunks = (
        EngineChunk("chunk-1", document.id, f"第一节事实。{overlap}"),
        EngineChunk("chunk-2", document.id, f"{overlap}第一节后续。"),
        EngineChunk("chunk-3", document.id, "第二节事实。"),
        EngineChunk("chunk-4", document.id, "第三节事实。"),
        EngineChunk("chunk-5", document.id, "第四节事实。"),
    )

    nodes = build_hierarchical_nodes(document, chunks, parent_size=4)

    parents = tuple(node for node in nodes if node.node_type == IndexNodeType.PARENT)
    sections = tuple(node for node in nodes if node.node_type == IndexNodeType.SECTION)
    summary = next(node for node in nodes if node.node_type == IndexNodeType.SUMMARY)
    assert len(parents) == 2
    assert len(sections) == 1
    assert parents[0].source_chunk_ids == ("chunk-1", "chunk-2", "chunk-3", "chunk-4")
    assert parents[0].content.count(overlap) == 1
    assert sections[0].source_chunk_ids == ("chunk-1", "chunk-5")
    assert summary.source_chunk_ids == ("chunk-1", "chunk-5")
    assert "第一节事实" in summary.content
    assert "第四节事实" in summary.content


def test_attach_embeddings_keeps_node_order() -> None:
    """批量 Embedding 结果必须按节点原顺序写回，避免摘要向量串到其他文档。"""
    document = DocumentRecord(
        security_scope_id="a" * 64,
        knowledge_base_id=uuid4(),
        filename="fixture.txt",
        safe_filename="fixture.txt",
        mime_type="text/plain",
        file_size_bytes=7,
        content_sha256="c" * 64,
        parser_profile_ref="office-default@1",
        chunk_profile_ref="general-256@2",
    )
    nodes = build_hierarchical_nodes(
        document,
        (EngineChunk("chunk-1", document.id, "第一条事实。"),),
    )

    embedded = attach_embeddings(
        nodes,
        ((1.0, 0.0), (0.0, 1.0), (0.5, 0.5)),
    )

    assert embedded[0].embedding == (1.0, 0.0)
    assert embedded[1].embedding == (0.0, 1.0)
    assert embedded[2].embedding == (0.5, 0.5)


def test_hierarchical_nodes_use_real_headings_before_window_fallback() -> None:
    """章节标题应形成 Section 边界，没有标题的后续内容仍保留在对应章节。"""
    document = DocumentRecord(
        security_scope_id="a" * 64,
        knowledge_base_id=uuid4(),
        filename="manual.md",
        safe_filename="manual.md",
        mime_type="text/markdown",
        file_size_bytes=7,
        content_sha256="e" * 64,
        parser_profile_ref="office-default@1",
        chunk_profile_ref="general-256@2",
    )
    chunks = (
        EngineChunk("chunk-1", document.id, "# 第一章 安装\n安装事实。"),
        EngineChunk("chunk-2", document.id, "安装补充。"),
        EngineChunk("chunk-3", document.id, "# 第二章 使用\n使用事实。"),
    )

    nodes = build_hierarchical_nodes(document, chunks, parent_size=1)

    sections = tuple(node for node in nodes if node.node_type == IndexNodeType.SECTION)
    assert [section.metadata["title"] for section in sections] == [
        "第一章 安装",
        "第二章 使用",
    ]
    assert sections[0].source_chunk_ids == ("chunk-1", "chunk-2")
    assert sections[1].source_chunk_ids == ("chunk-3",)


def test_parent_groups_close_on_heading_and_character_budget() -> None:
    """Parent 不再必须固定四块：标题和长文都能提前形成边界。"""
    document = DocumentRecord(
        security_scope_id="a" * 64,
        knowledge_base_id=uuid4(),
        filename="manual.md",
        safe_filename="manual.md",
        mime_type="text/markdown",
        file_size_bytes=7,
        content_sha256="1" * 64,
        parser_profile_ref="office-default@1",
        chunk_profile_ref="prose-384@1",
    )
    chunks = (
        EngineChunk("chunk-1", document.id, "# 第一章\n安装说明。"),
        EngineChunk("chunk-2", document.id, "A" * 4_900),
        EngineChunk("chunk-3", document.id, "B" * 300),
        EngineChunk("chunk-4", document.id, "# 第二章\n使用说明。"),
    )

    nodes = build_hierarchical_nodes(document, chunks, parent_size=4)
    parents = tuple(node for node in nodes if node.node_type == IndexNodeType.PARENT)

    assert [parent.source_chunk_ids for parent in parents] == [
        ("chunk-1",),
        ("chunk-2",),
        ("chunk-3",),
        ("chunk-4",),
    ]
    assert all(parent.metadata["algorithm"] == "heading-length-parent-v3" for parent in parents)
    assert all(parent.index_version == 3 for parent in parents)


def test_spreadsheet_parents_repeat_header_only_in_sidecar_content() -> None:
    """电子表格按小行组切 Parent，中段组携带表头但源 Chunk 不变。"""
    document = DocumentRecord(
        security_scope_id="a" * 64,
        knowledge_base_id=uuid4(),
        filename="battery.xlsx",
        safe_filename="battery.xlsx",
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        file_size_bytes=7,
        content_sha256="2" * 64,
        parser_profile_ref="office-default@1",
        chunk_profile_ref="table-192@1",
        metadata={"language": "zh-CN"},
    )
    original_contents = (
        "型号\t容量\t温度\nA1\t100\t25",
        "A2\t101\t26",
        "A3\t102\t27",
        "A4\t103\t28",
        "A5\t104\t29",
        "A6\t105\t30",
        "A7\t106\t31",
    )
    chunks = tuple(
        EngineChunk(f"chunk-{index}", document.id, content)
        for index, content in enumerate(original_contents, start=1)
    )

    nodes = build_hierarchical_nodes(document, chunks, parent_size=4)
    parents = tuple(node for node in nodes if node.node_type == IndexNodeType.PARENT)

    assert [len(parent.source_chunk_ids) for parent in parents] == [3, 3, 1]
    assert all(parent.metadata["segment_kind"] == "table" for parent in parents)
    assert all(parent.metadata["table_header"] == "型号\t容量\t温度" for parent in parents)
    assert "表头：型号\t容量\t温度" in parents[1].content
    assert "文档：battery" in parents[1].content
    assert "索引层级：parent" in parents[1].content
    # 侧车增强是纯函数新建的文本，不得回写或改动 RAGFlow 源块。
    assert tuple(chunk.content for chunk in chunks) == original_contents


def test_extract_structured_metadata_is_deterministic_and_user_verifiable() -> None:
    """日期、组织、技术名与文档类别只从原文抽取，并产生稳定可过滤字段。"""
    document = DocumentRecord(
        security_scope_id="a" * 64,
        knowledge_base_id=uuid4(),
        filename="实验报告.pdf",
        safe_filename="实验报告.pdf",
        mime_type="application/pdf",
        file_size_bytes=7,
        content_sha256="f" * 64,
        parser_profile_ref="deepdoc-default@1",
        chunk_profile_ref="general-256@2",
    )
    chunks = (
        EngineChunk(
            "chunk-1",
            document.id,
            "实验报告：LazyAI研究院于2026年7月30日使用RAGFlow v0.26.4完成验证。",
        ),
    )

    metadata = extract_structured_metadata(document, chunks)

    assert metadata["document_kind"] == "experiment_report"
    assert metadata["document_dates"] == ["2026-07-30"]
    assert "2026" in metadata["document_years"]
    assert "LazyAI研究院" in metadata["entity_organizations"]
    assert "RAGFlow" in metadata["entity_technologies"]


def test_lightweight_graph_extracts_only_explicit_relations_with_sources() -> None:
    """显式中英文关系应建立实体图，且每条结果都能回查真实 RAGFlow Chunk。"""
    document = DocumentRecord(
        security_scope_id="a" * 64,
        knowledge_base_id=uuid4(),
        filename="architecture.txt",
        safe_filename="architecture.txt",
        mime_type="text/plain",
        file_size_bytes=7,
        content_sha256="d" * 64,
        parser_profile_ref="office-default@1",
        chunk_profile_ref="general-256@2",
    )
    chunks = (
        EngineChunk(
            "chunk-entity-1",
            document.id,
            "RAGFlow 使用 Elasticsearch 进行索引。Gateway 依赖 RAGFlow。",
        ),
        EngineChunk(
            "chunk-entity-2",
            document.id,
            "Gateway supports PostgreSQL。普通说明句不会被猜成关系。",
        ),
    )

    entities, relations = build_lightweight_graph(document, chunks)

    names = {entity.canonical_name.casefold() for entity in entities}
    assert {"ragflow", "elasticsearch", "gateway", "postgresql"} <= names
    triples = {
        (relation.subject_normalized, relation.predicate, relation.object_normalized)
        for relation in relations
    }
    assert ("ragflow", "使用", "elasticsearch") in triples
    assert ("gateway", "依赖", "ragflow") in triples
    assert ("gateway", "supports", "postgresql") in triples
    assert all(relation.source_chunk_ids for relation in relations)
