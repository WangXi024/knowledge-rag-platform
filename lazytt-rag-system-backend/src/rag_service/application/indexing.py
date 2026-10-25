"""不修改 RAGFlow 的层级、摘要、实体与轻量关系侧车索引构建算法。

RAGFlow 继续负责解析并保存最细粒度 Chunk。本模块只把连续 Chunk 分组为 Parent，
再从各 Parent 抽取代表句组成文档摘要；所有节点都保留真实源 Chunk ID，因此在线
命中高层节点后能够回读原文引用，而不是把机器生成或抽取摘要冒充原始证据。

实体图同样只保存“名称/显式关系到真实源 Chunk”的导航信息。它不调用 LLM 猜测
隐含关系，也不承担通用知识图谱推理；这样即使抽取遗漏，基础向量、BM25 与 RRF
路线仍覆盖全文，并且不会把模型幻觉写成长期索引。
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from rag_service.domain.models import (
    DocumentEntity,
    DocumentIndexNode,
    DocumentRecord,
    EntityRelation,
    IndexNodeType,
)
from rag_service.domain.ports import EngineChunk

_SENTENCE_BOUNDARY = re.compile(r"(?<=[。！？.!?])\s+|\n+")
_MODEL_OR_ACRONYM = re.compile(
    r"(?<![A-Za-z0-9_-])(?:[A-Z][A-Za-z0-9]*(?:[._/-][A-Za-z0-9]+)+|"
    r"[A-Z][A-Z0-9-]{1,31})(?![A-Za-z0-9_-])"
)
_CAMEL_TECHNOLOGY = re.compile(
    r"(?<![A-Za-z0-9_-])(?:[A-Z]{2,}[A-Za-z0-9-]*|"
    r"[A-Z][a-z0-9]+(?:[A-Z][A-Za-z0-9]*)+)(?![A-Za-z0-9_-])"
)
_BOOK_TITLE = re.compile(r"《([^》\n]{2,64})》")
_MARKDOWN_OR_NUMBERED_HEADING = re.compile(
    r"^(?:#{1,6}\s+|第[一二三四五六七八九十百零〇0-9]+[章节篇部]\s*|"
    r"(?:\d+(?:\.\d+){0,3}|[一二三四五六七八九十]+)[、.．)]\s*)"
)
_ISO_DATE = re.compile(
    r"(?<!\d)(?P<year>19\d{2}|20\d{2})[-/.](?P<month>0?[1-9]|1[0-2])"
    r"[-/.](?P<day>0?[1-9]|[12]\d|3[01])(?!\d)"
)
_CHINESE_DATE = re.compile(
    r"(?<!\d)(?P<year>19\d{2}|20\d{2})年(?P<month>0?[1-9]|1[0-2])月"
    r"(?P<day>0?[1-9]|[12]\d|3[01])日"
)
_VERSION = re.compile(
    r"(?i)(?<![A-Za-z0-9_.-])(?:v(?:ersion)?\s*)?\d+(?:\.\d+){1,3}"
    r"(?![A-Za-z0-9_.-])|"
    r"版本\s*[：:]?\s*[A-Za-z]?\d+(?:\.\d+){0,3}"
)
_CHINESE_ORGANIZATION = re.compile(
    r"[一-鿿A-Za-z0-9·&（）()]{2,40}"
    r"(?:股份有限公司|有限公司|大学|研究院|研究所|实验室|委员会|事业部|部门|中心)"
)
_ENGLISH_ORGANIZATION = re.compile(
    r"\b(?:[A-Z][A-Za-z0-9&.-]*\s+){0,5}"
    r"(?:Inc\.?|Corp\.?|Corporation|Ltd\.?|University|Institute|Laboratory)\b"
)
_CHINESE_RELATION = re.compile(
    r"(?P<subject>[A-Za-z][A-Za-z0-9._/-]{1,31}|[\u4e00-\u9fff]{2,16})"
    r"\s*(?P<predicate>使用|采用|依赖|连接|支持|包括|属于|基于)\s*"
    r"(?P<object>[A-Za-z][A-Za-z0-9._/-]{1,31}|[\u4e00-\u9fff]{2,16})"
)
_ENGLISH_RELATION = re.compile(
    r"(?P<subject>[A-Z][A-Za-z0-9._/-]{1,31})\s+"
    r"(?P<predicate>uses|depends on|connects to|supports|includes|is part of|is based on)\s+"
    r"(?P<object>[A-Z][A-Za-z0-9._/-]{1,31})",
    re.IGNORECASE,
)
_ENTITY_STOPWORDS = frozenset(
    {
        "API",
        "HTTP",
        "HTTPS",
        "JSON",
        "PDF",
        "WORD",
        "EXCEL",
        "POWERPOINT",
    }
)


def build_hierarchical_nodes(
    document: DocumentRecord,
    chunks: Sequence[EngineChunk],
    *,
    parent_size: int = 4,
    parents_per_section: int = 4,
    max_chunks: int = 1000,
    total_chunk_count: int | None = None,
) -> tuple[DocumentIndexNode, ...]:
    """构建结构感知的“摘要 → Section → Parent → 原始 Chunk”侧车索引。

    ``parent_size`` 现在是单个 Parent 的最大子块数，不再是必须凑满的固定窗口：真实
    标题会立即开始新 Parent，累计正文达到文档类型对应的字符预算也会提前收束；表格
    还会限制更小行组并重复可识别表头。每个侧车节点只把文档名、章节和类型作为检索
    前缀写入自身 Embedding，最终答案仍回读 ``source_chunk_ids`` 指向的 RAGFlow 原文，
    因此增强召回不会污染引用正文、页码或评分。

    为避免异常超大表格制造数千次 Embedding 请求，单次最多处理前 1,000 个块并在
    Metadata 中明确 ``partial``；RAGFlow 原始全文索引不会被截断。
    """
    if parent_size <= 0 or parents_per_section <= 0 or max_chunks <= 0:
        raise ValueError("parent_size, parents_per_section and max_chunks must be positive")
    usable = tuple(chunk for chunk in chunks[:max_chunks] if chunk.content.strip())
    # Worker 可能已经按保护上限只读取了前 1,000 块，因此不能只比较传入序列长度；
    # 同时接收 RAGFlow 状态报告的总块数，才能如实标记超大文档侧车索引是部分覆盖。
    observed_total = total_chunk_count if total_chunk_count is not None else len(chunks)
    partial = observed_total > len(usable)
    table_header = _table_header_from_chunks(usable) if _is_table_document(document) else None
    raw_parents: list[DocumentIndexNode] = []
    for offset, children in _adaptive_parent_groups(
        document,
        usable,
        max_children=parent_size,
    ):
        content = _merge_child_contents(tuple(child.content for child in children))
        header_probe = content[: max(len(table_header or "") * 2, 256)]
        if table_header is not None and table_header not in header_probe:
            # 只在侧车 Parent 重复表头，RAGFlow 原始 Chunk 保持逐字不变。这样命中
            # 表格中段的行组时，Embedding 仍知道各列含义，而引用不会出现人工前缀。
            content = f"表头：{table_header}\n{content}"
        segment_kind = "table" if table_header is not None else "prose"
        raw_parents.append(
            DocumentIndexNode(
                security_scope_id=document.security_scope_id,
                knowledge_base_id=document.knowledge_base_id,
                document_id=document.id,
                node_type=IndexNodeType.PARENT,
                ordinal=len(raw_parents),
                content=content,
                source_chunk_ids=tuple(child.engine_chunk_id for child in children),
                metadata={
                    "child_count": len(children),
                    "character_count": len(content),
                    "partial": partial,
                    "algorithm": "heading-length-parent-v3",
                    "first_chunk_ordinal": offset,
                    "segment_kind": segment_kind,
                    "table_header": table_header,
                    "context_prefix_version": 1,
                },
                index_version=3,
            )
        )
    if not raw_parents:
        return ()

    section_groups = _group_parents_into_sections(
        tuple(raw_parents),
        parents_per_section=parents_per_section,
    )
    parents: list[DocumentIndexNode] = []
    raw_sections: list[DocumentIndexNode] = []
    for section_ordinal, (title, grouped_parents) in enumerate(section_groups):
        parent_ordinals = tuple(parent.ordinal for parent in grouped_parents)
        parents.extend(
            replace(
                parent,
                content=_contextualize_index_content(
                    document,
                    parent.content,
                    level="parent",
                    section_title=title,
                    table_header=(
                        str(parent.metadata["table_header"])
                        if parent.metadata.get("table_header")
                        else None
                    ),
                ),
                metadata={
                    **parent.metadata,
                    "section_ordinal": section_ordinal,
                    "section_title": title,
                },
            )
            for parent in grouped_parents
        )
        section_content, source_ids = _extract_document_summary(grouped_parents)
        if (
            title
            and not title.startswith("逻辑章节 ")
            and not section_content.startswith(title)
        ):
            section_content = f"{title}\n{section_content}"
        raw_sections.append(
            DocumentIndexNode(
                security_scope_id=document.security_scope_id,
                knowledge_base_id=document.knowledge_base_id,
                document_id=document.id,
                node_type=IndexNodeType.SECTION,
                ordinal=section_ordinal,
                content=section_content,
                source_chunk_ids=source_ids,
                metadata={
                    "title": title,
                    "parent_ordinals": parent_ordinals,
                    "parent_count": len(grouped_parents),
                    "partial": partial,
                    "summary_level": "section",
                    "algorithm": "heading-or-window-section-v3",
                    "context_prefix_version": 1,
                },
                index_version=3,
            )
        )

    # Section 正文已经是从各 Parent 抽取的短摘要；文档层直接顺序合并这些章节摘要，
    # 避免只有一个逻辑章节时再次“只取首句”而丢掉该章节后半部分的代表事实。
    summary_content = "\n".join(section.content for section in raw_sections)[:3000]
    # Section 已经从各 Parent 选择代表源块；文档摘要保留所有章节的代表 ID，既覆盖
    # 全文，又避免直接把每个子块都挂到最高层节点。顺序去重确保重解析结果稳定。
    representative_ids = tuple(
        dict.fromkeys(
            source_chunk_id
            for section in raw_sections
            for source_chunk_id in section.source_chunk_ids
        )
    )[:64]
    summary = DocumentIndexNode(
        security_scope_id=document.security_scope_id,
        knowledge_base_id=document.knowledge_base_id,
        document_id=document.id,
        node_type=IndexNodeType.SUMMARY,
        ordinal=0,
        content=summary_content,
        source_chunk_ids=representative_ids,
        metadata={
            "parent_count": len(parents),
            "section_count": len(raw_sections),
            "partial": partial,
            "summary_level": "document",
            "algorithm": "hierarchical-extractive-summary-v3",
            "context_prefix_version": 1,
        },
        index_version=3,
    )
    # Context 前缀只参与侧车节点的向量化。Section 和 Summary 先从无前缀原文生成，
    # 再统一附加短标签，避免“文档：...”被抽取算法误当成每一节的代表句。
    sections = tuple(
        replace(
            section,
            content=_contextualize_index_content(
                document,
                section.content,
                level="section",
                section_title=str(section.metadata["title"]),
                table_header=table_header,
            ),
        )
        for section in raw_sections
    )
    contextual_summary = replace(
        summary,
        content=_contextualize_index_content(
            document,
            summary.content,
            level="document-summary",
            table_header=table_header,
        ),
    )
    return (*parents, *sections, contextual_summary)


def extract_structured_metadata(
    document: DocumentRecord,
    chunks: Sequence[EngineChunk],
    *,
    max_values_per_field: int = 50,
    max_characters: int = 200_000,
) -> dict[str, Any]:
    """从原始 Chunk 确定性抽取可过滤的结构化 Metadata。

    当前版本不调用 LLM，也不做知识图谱推理，只识别可由原文逐字复核的日期、年份、
    版本号、组织名称、技术/型号和书名。每类值排序、去重并限制数量，避免异常表格把
    PostgreSQL/RAGFlow Metadata 膨胀成第二份正文。返回字段由 Worker 使用
    ``setdefault`` 合并，因此用户显式填写的同名业务字段永远优先。
    """
    if max_values_per_field <= 0 or max_characters <= 0:
        raise ValueError("metadata extraction limits must be positive")
    sample = "\n".join(chunk.content for chunk in chunks)[:max_characters]

    dates = {
        f"{match.group('year')}-{int(match.group('month')):02d}-{int(match.group('day')):02d}"
        for pattern in (_ISO_DATE, _CHINESE_DATE)
        for match in pattern.finditer(sample)
    }
    years = {value[:4] for value in dates}
    years.update(re.findall(r"(?<!\d)(?:19\d{2}|20\d{2})(?!\d)", sample))
    versions = {" ".join(match.group(0).split()) for match in _VERSION.finditer(sample)}
    organizations = {
        match.group(0).strip()
        for pattern in (_CHINESE_ORGANIZATION, _ENGLISH_ORGANIZATION)
        for match in pattern.finditer(sample)
    }
    technologies = {
        match.group(0).strip()
        for pattern in (_MODEL_OR_ACRONYM, _CAMEL_TECHNOLOGY)
        for match in pattern.finditer(sample)
        if match.group(0).upper() not in _ENTITY_STOPWORDS
    }
    titles = {match.group(1).strip() for match in _BOOK_TITLE.finditer(sample)}

    def limited(values: set[str]) -> list[str]:
        """稳定排序并应用字段级数量上限，使重解析结果可重复。"""
        return sorted(values, key=lambda value: (value.casefold(), value))[
            :max_values_per_field
        ]

    return {
        "lazytt_structured_metadata_version": 2,
        "lazytt_structured_metadata_status": "completed",
        "document_kind": _document_kind(document, sample),
        "document_dates": limited(dates),
        "document_years": limited(years),
        "document_versions": limited(versions),
        "entity_organizations": limited(organizations),
        "entity_technologies": limited(technologies),
        "entity_titles": limited(titles),
    }


def attach_embeddings(
    nodes: Sequence[DocumentIndexNode],
    vectors: Sequence[Sequence[float]],
) -> tuple[DocumentIndexNode, ...]:
    """把批量模型结果按原顺序附加到节点，并严格校验一一对应关系。"""
    if len(nodes) != len(vectors):
        raise ValueError("index node and embedding counts must match")
    return tuple(
        replace(node, embedding=tuple(float(value) for value in vector))
        for node, vector in zip(nodes, vectors, strict=True)
    )


def build_lightweight_graph(
    document: DocumentRecord,
    chunks: Sequence[EngineChunk],
    *,
    max_entities: int = 500,
    max_relations: int = 1000,
) -> tuple[tuple[DocumentEntity, ...], tuple[EntityRelation, ...]]:
    """从原始 Chunk 抽取实体和文本明确陈述的关系，并保留可回查来源。

    当前轻量实现面向产品名、系统名、模型名、缩写、书名号主题以及中英文显式
    “主体—谓词—客体”句式。它刻意不做开放域 NER 或隐含推理，避免把普通词和 LLM
    猜测写进长期索引。数量上限保护超大表格/日志；达到上限只停止侧车抽取，不改变
    RAGFlow 已完成的全文索引，也不会让文档解析任务失败。
    """
    if max_entities <= 0 or max_relations <= 0:
        raise ValueError("entity and relation limits must be positive")
    entity_sources: defaultdict[str, list[str]] = defaultdict(list)
    entity_aliases: defaultdict[str, set[str]] = defaultdict(set)
    entity_mentions: defaultdict[str, int] = defaultdict(int)
    entity_names: dict[str, str] = {}
    entity_types: dict[str, str] = {}
    relation_sources: defaultdict[tuple[str, str, str], list[str]] = defaultdict(list)
    relation_names: dict[tuple[str, str, str], tuple[str, str]] = {}

    def remember_entity(name: str, source_chunk_id: str, entity_type: str) -> str | None:
        """规范化候选并聚合同名实体；返回值供关系端复用统一键。"""
        cleaned = name.strip(" \t\r\n.,;:!?，。；：！？()（）[]【】\"'")
        normalized = _normalize_entity_name(cleaned)
        if (
            len(cleaned) < 2
            or len(cleaned) > 64
            or normalized.upper() in _ENTITY_STOPWORDS
            or (normalized not in entity_names and len(entity_names) >= max_entities)
        ):
            return None
        entity_names.setdefault(normalized, cleaned)
        entity_types.setdefault(normalized, entity_type)
        entity_aliases[normalized].add(cleaned)
        entity_mentions[normalized] += 1
        if source_chunk_id not in entity_sources[normalized]:
            entity_sources[normalized].append(source_chunk_id)
        return normalized

    for chunk in chunks:
        content = chunk.content[:100_000]
        for match in _MODEL_OR_ACRONYM.finditer(content):
            remember_entity(match.group(0), chunk.engine_chunk_id, "TECHNOLOGY")
        for match in _BOOK_TITLE.finditer(content):
            remember_entity(match.group(1), chunk.engine_chunk_id, "TITLE")
        for pattern in (_CHINESE_RELATION, _ENGLISH_RELATION):
            for match in pattern.finditer(content):
                subject = match.group("subject")
                object_name = match.group("object")
                subject_key = remember_entity(subject, chunk.engine_chunk_id, "CONCEPT")
                object_key = remember_entity(object_name, chunk.engine_chunk_id, "CONCEPT")
                if subject_key is None or object_key is None or subject_key == object_key:
                    continue
                relation_key = (
                    subject_key,
                    " ".join(match.group("predicate").casefold().split()),
                    object_key,
                )
                if relation_key not in relation_sources and len(relation_sources) >= max_relations:
                    continue
                relation_names.setdefault(relation_key, (subject.strip(), object_name.strip()))
                if chunk.engine_chunk_id not in relation_sources[relation_key]:
                    relation_sources[relation_key].append(chunk.engine_chunk_id)

    entities = tuple(
        DocumentEntity(
            security_scope_id=document.security_scope_id,
            knowledge_base_id=document.knowledge_base_id,
            document_id=document.id,
            canonical_name=entity_names[normalized],
            normalized_name=normalized,
            entity_type=entity_types[normalized],
            aliases=tuple(sorted(entity_aliases[normalized])),
            mention_count=entity_mentions[normalized],
            source_chunk_ids=tuple(entity_sources[normalized][:64]),
            metadata={"algorithm": "explicit-entity-v1"},
        )
        for normalized in sorted(entity_names)
    )
    relations = tuple(
        EntityRelation(
            security_scope_id=document.security_scope_id,
            knowledge_base_id=document.knowledge_base_id,
            document_id=document.id,
            subject_name=relation_names[key][0],
            subject_normalized=key[0],
            predicate=key[1],
            object_name=relation_names[key][1],
            object_normalized=key[2],
            source_chunk_ids=tuple(relation_sources[key][:64]),
            metadata={"algorithm": "explicit-relation-v1"},
        )
        for key in sorted(relation_sources)
    )
    return entities, relations


def _normalize_entity_name(value: str) -> str:
    """统一大小写和空白，使查询与索引名称能稳定做精确包含匹配。"""
    return " ".join(value.casefold().split())


def _adaptive_parent_groups(
    document: DocumentRecord,
    chunks: tuple[EngineChunk, ...],
    *,
    max_children: int,
) -> tuple[tuple[int, tuple[EngineChunk, ...]], ...]:
    """按标题、文本长度和文件族生成不跨结构边界的动态 Parent 分组。

    返回值中的整数是首个子块在原序列中的位置，供 Metadata 和重解析稳定排序使用。
    ``max_children`` 只是硬上限：标题出现、累计字符达到预算或表格/视觉内容达到更小
    文件族上限时都会提前结束。函数不调用模型、不写数据库，也不会修改传入 Chunk；
    无法识别结构时自然退化为最多四块的连续窗口。
    """
    if max_children <= 0:
        raise ValueError("max_children must be positive")
    suffix = Path(document.filename).suffix.lower()
    if suffix in {".xls", ".xlsx", ".csv"}:
        family_max_children = min(max_children, 3)
        target_characters = 3_600
    elif suffix in {".ppt", ".pptx", ".png", ".jpg", ".jpeg", ".webp"}:
        family_max_children = min(max_children, 2)
        target_characters = 2_600
    elif suffix in {".mp3", ".wav", ".m4a", ".mp4", ".mov", ".mkv", ".webm"}:
        family_max_children = max_children
        target_characters = 5_200
    else:
        family_max_children = max_children
        target_characters = 4_800

    groups: list[tuple[int, tuple[EngineChunk, ...]]] = []
    current: list[EngineChunk] = []
    current_start = 0
    current_characters = 0
    for ordinal, chunk in enumerate(chunks):
        content = chunk.content.strip()
        starts_new_heading = _heading_from_content(content) is not None
        exceeds_budget = current_characters + len(content) > target_characters
        if current and (
            starts_new_heading
            or len(current) >= family_max_children
            or exceeds_budget
        ):
            groups.append((current_start, tuple(current)))
            current = []
            current_characters = 0
        if not current:
            current_start = ordinal
        current.append(chunk)
        current_characters += len(content)
    if current:
        groups.append((current_start, tuple(current)))
    return tuple(groups)


def _is_table_document(document: DocumentRecord) -> bool:
    """只把原生电子表格视为整篇表格，避免含单个表格的 PDF 被错误整体改写。"""
    return Path(document.filename).suffix.lower() in {".xls", ".xlsx", ".csv"}


def _table_header_from_chunks(chunks: tuple[EngineChunk, ...]) -> str | None:
    """从电子表格前部选择可复核表头，无法确认时返回 ``None``。

    RAGFlow 可能把工作表输出成 TSV、CSV 或 Markdown 表格。本函数只检查前十个
    Chunk、每个前二十行，并要求至少两个列分隔符；Markdown 的 ``---`` 分隔行和
    超长数据行会被排除。误判只影响 Gateway 侧车 Embedding，不会修改 RAGFlow 原文。
    """
    for chunk in chunks[:10]:
        for raw_line in chunk.content.splitlines()[:20]:
            # 不能先用 ``split()`` 归一化空白：它会把 TSV 的制表符也
            # 吃掉，使真实 Excel 表头无法被识别。这里保留原始列分隔
            # 符，只移除行首尾空白以进行长度和空行判定。
            line = raw_line.strip()
            if not line or len(line) > 500 or re.fullmatch(r"[|:\-\s]+", line):
                continue
            if line.count("\t") >= 2 or line.count("|") >= 2 or line.count(",") >= 2:
                return line.strip(" |	")
    return None


def _contextualize_index_content(
    document: DocumentRecord,
    content: str,
    *,
    level: str,
    section_title: str | None = None,
    table_header: str | None = None,
) -> str:
    """给侧车向量增加短上下文前缀，原始引用正文保持不变。

    前缀只包含本地稳定文件名、粗粒度文档类型、层级、章节和用户已提供的语言，避免
    发送额外正文或生成新事实。在线命中节点后只使用 ``source_chunk_ids`` 回读
    RAGFlow Chunk，所以这些标签不会显示在引用中，也不会改变文档页码。字符限制
    防止异常文件名或 Metadata 主导 Embedding。
    """
    prefix = [
        f"文档：{Path(document.filename).stem[:160]}",
        f"内容类型：{_document_kind(document, '')}",
        f"索引层级：{level}",
    ]
    if section_title:
        prefix.append(f"章节：{section_title[:160]}")
    language = document.metadata.get("language", document.metadata.get("lang"))
    if isinstance(language, str) and language.strip():
        prefix.append(f"语言：{language.strip()[:32]}")
    if table_header and table_header not in content[: max(len(table_header) * 2, 256)]:
        prefix.append(f"表头：{table_header[:500]}")
    # Python 不允许在 f-string 的大括号表达式中直接出现 ``\n`` 转义字符，
    # 因此先拼出前缀文本再组装完整索引内容。这只是语法层面的拆分，
    # 不会在前缀中增加任何新信息。
    prefix_text = "\n".join(prefix)
    return f"{prefix_text}\n正文：\n{content.strip()}"


def _heading_from_content(content: str) -> str | None:
    """从 Parent 首行识别可解释章节标题，无法确认时返回 ``None``。

    标题必须较短，且满足 Markdown、中文“第 X 章”或数字编号格式。普通正文即使首行
    很短也不会被猜成标题，避免错误边界把连续上下文切碎。
    """
    first_line = next((line.strip() for line in content.splitlines() if line.strip()), "")
    if not first_line or len(first_line) > 160:
        return None
    # “第一节事实。”虽然以章节字样开头，但已经是完整正文句。标题行允许冒号，
    # 不允许句号、问号或分号，从而减少 DeepDoc OCR 文本的误切分。
    if _MARKDOWN_OR_NUMBERED_HEADING.match(first_line) and not re.search(
        r"[。！？!?；;]", first_line
    ):
        return first_line.lstrip("# ").strip()
    return None


def _group_parents_into_sections(
    parents: tuple[DocumentIndexNode, ...],
    *,
    parents_per_section: int,
) -> tuple[tuple[str, tuple[DocumentIndexNode, ...]], ...]:
    """按真实标题优先、固定窗口兜底，把 Parent 划分为有界 Section。

    标题只会在 Parent 边界上切分；没有标题的解析器仍每 ``parents_per_section`` 个
    Parent 形成逻辑章节，使扫描 PDF、表格和纯文本同样具备稳定多粒度索引。
    """
    groups: list[tuple[str, tuple[DocumentIndexNode, ...]]] = []
    current: list[DocumentIndexNode] = []
    current_title = ""
    for parent in parents:
        heading = _heading_from_content(parent.content)
        if current and (heading is not None or len(current) >= parents_per_section):
            groups.append((current_title or f"逻辑章节 {len(groups) + 1}", tuple(current)))
            current = []
            current_title = ""
        if heading is not None and not current:
            current_title = heading
        current.append(parent)
    if current:
        groups.append((current_title or f"逻辑章节 {len(groups) + 1}", tuple(current)))
    return tuple(groups)


def _document_kind(document: DocumentRecord, sample: str) -> str:
    """结合扩展名和可复核关键词生成粗粒度文档类别，供 Metadata Filter 使用。"""
    suffix = Path(document.filename).suffix.lower()
    lowered = sample.casefold()
    if suffix in {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}:
        return "audio_transcript"
    if suffix in {".mp4", ".mov", ".mkv", ".webm", ".avi"}:
        return "video_transcript"
    if any(keyword in lowered for keyword in ("会议纪要", "meeting minutes")):
        return "meeting_minutes"
    if any(keyword in lowered for keyword in ("实验报告", "experiment report")):
        return "experiment_report"
    if any(keyword in lowered for keyword in ("技术规范", "technical specification")):
        return "technical_specification"
    return {
        ".pdf": "pdf_document",
        ".doc": "office_document",
        ".docx": "office_document",
        ".ppt": "presentation",
        ".pptx": "presentation",
        ".xls": "spreadsheet",
        ".xlsx": "spreadsheet",
        ".csv": "spreadsheet",
        ".png": "image",
        ".jpg": "image",
        ".jpeg": "image",
        ".webp": "image",
    }.get(suffix, "text_document")


def _merge_child_contents(contents: tuple[str, ...]) -> str:
    """按顺序拼接子块，并删除分块重叠造成的最长逐字重复尾首文本。"""
    merged = ""
    for raw_content in contents:
        content = raw_content.strip()
        if not merged:
            merged = content
            continue
        maximum = min(len(merged), len(content), 512)
        overlap = 0
        for size in range(maximum, 15, -1):
            if merged[-size:] == content[:size]:
                overlap = size
                break
        merged = f"{merged}\n{content[overlap:]}" if overlap else f"{merged}\n{content}"
    return merged


def _extract_document_summary(
    parents: tuple[DocumentIndexNode, ...],
    *,
    max_chars: int = 3000,
    max_sources: int = 64,
) -> tuple[str, tuple[str, ...]]:
    """从每个 Parent 取首个有效句子，形成覆盖全文位置的可追溯摘要。

    这是抽取式摘要，不调用 LLM，也不会添加原文不存在的事实。源 ID 从不同 Parent
    顺序采样，在线命中摘要后可以回读这些代表块；字符和来源上限防止摘要节点本身
    变成另一个超长文档。
    """
    sentences: list[str] = []
    sources: list[str] = []
    current_length = 0
    for parent in parents:
        first_sentence = next(
            (
                sentence.strip()
                for sentence in _SENTENCE_BOUNDARY.split(parent.content)
                if sentence.strip()
            ),
            "",
        )
        if not first_sentence:
            continue
        remaining = max_chars - current_length
        if remaining <= 0:
            break
        selected = first_sentence[:remaining]
        sentences.append(selected)
        current_length += len(selected) + 1
        if parent.source_chunk_ids and len(sources) < max_sources:
            sources.append(parent.source_chunk_ids[0])
    return "\n".join(sentences), tuple(sources)
