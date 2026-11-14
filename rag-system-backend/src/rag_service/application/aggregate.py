"""在线社区证据的有界选择、来源复核、提示词与标签检查。

此模块不读取引擎、数据库或 HTTP：图谱浏览服务通过独立端口提供已授权报告。
复核失败必须关闭本次派生回答，不能把旧摘要降级成看似成功的原文答案。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, replace
from uuid import UUID, uuid4

from rag_service.domain.aggregate import (
    MAX_AGGREGATE_EVIDENCE,
    AggregateCommunityPort,
    AggregateEvidence,
    fingerprint,
    from_payload,
    to_payload,
)
from rag_service.domain.models import Citation, KnowledgeBase, RetrievalCandidate
from rag_service.security.context import SecurityContext

_LABEL = re.compile(r"\[([CA][^\]\r\n]*)\]")
_AGGREGATE_TIMEOUT_SECONDS = 12.0


@dataclass(frozen=True, slots=True)
class AggregatePlan:
    """一次回答冻结的报告集合；任何失败都不会进入正常社区缓存。"""

    evidences: tuple[AggregateEvidence, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def version_hash(self) -> str:
        """全部生成正文和来源快照参与缓存身份。"""
        return fingerprint(self.evidences)


async def prepare(
    port: AggregateCommunityPort | None,
    security: SecurityContext,
    knowledge_bases: tuple[KnowledgeBase, ...],
    *,
    filtered: bool,
) -> AggregatePlan:
    """选择最多四份真正跨文档的报告，不让 Metadata 筛选泄漏未筛选的摘要。"""
    if filtered:
        return AggregatePlan(warnings=("aggregate_metadata_filter_unsupported",))
    if port is None:
        return AggregatePlan(warnings=("aggregate_unavailable",))
    try:
        async with asyncio.timeout(_AGGREGATE_TIMEOUT_SECONDS):
            reports = await port.list_available(
                security, tuple(item.id for item in knowledge_bases)
            )
            allowed = {item.id: item.security_scope_id for item in knowledge_bases}
            revisions = {item.id: item.revision for item in knowledge_bases}
            selected: list[AggregateEvidence] = []
            seen: set[tuple[UUID, str]] = set()
            for report in reports:
                # 同样执行持久化 codec 的严格检查，内存端口不能绕过类型和上限约束。
                report = from_payload(to_payload(report))
                identity = (report.knowledge_base_id, report.community_id)
                if (
                    identity in seen
                    or allowed.get(report.knowledge_base_id) != report.resource_scope_id
                    or report.graph_profile_ref != "native-graph-with-summaries@1"
                    or report.graph_generation < 1
                    or revisions.get(report.knowledge_base_id) != report.content_revision
                ):
                    continue
                seen.add(identity)
                selected.append(replace(report, aggregate_id=uuid4(), label=f"A{len(selected)+1}"))
                if len(selected) == MAX_AGGREGATE_EVIDENCE:
                    break
            evidence = tuple(selected)
            if not evidence:
                return AggregatePlan(warnings=("aggregate_no_verified_community",))
            if not await port.revalidate(security, evidence):
                return AggregatePlan(warnings=("aggregate_sources_changed",))
            return AggregatePlan(evidences=evidence)
    except Exception:
        # 可选图谱读取故障不暴露内部协议或正文；取消仍按 BaseException 向上传播。
        return AggregatePlan(warnings=("aggregate_unavailable",))


async def revalidate(
    port: AggregateCommunityPort | None,
    security: SecurityContext,
    evidences: tuple[AggregateEvidence, ...],
) -> bool:
    """生成完成和缓存重放前重新回读，设置有界预算且不吞取消。"""
    if port is None or not evidences or len(evidences) > MAX_AGGREGATE_EVIDENCE:
        return False
    try:
        async with asyncio.timeout(_AGGREGATE_TIMEOUT_SECONDS):
            for evidence in evidences:
                from_payload(to_payload(evidence))
            return await port.revalidate(security, evidences)
    except Exception:
        return False


def context_text(evidences: tuple[AggregateEvidence, ...]) -> str:
    """把派生摘要和支持它的真实原文分开标明，绝不把摘要写成原文 quote。"""
    blocks: list[str] = []
    for evidence in evidences:
        sources = "\n\n".join(
            f"Raw source {index}, document {source.document_name}:\n{source.quote}"
            for index, source in enumerate(evidence.sources, start=1)
        )
        blocks.append(
            f"[{evidence.label}] Gateway-generated community summary (derived evidence)\n"
            f"Title: {evidence.title}\nSummary: {evidence.summary}\n"
            f"Verified original sources supporting this summary:\n{sources}"
        )
    return "\n\n".join(blocks)


def prompt_context(original_text: str, evidences: tuple[AggregateEvidence, ...]) -> str:
    """统一预算估算与实际 Prompt 的内容，避免只预算摘要而漏算全部来源原文。"""
    return (
        f"Original excerpts (C labels):\n{original_text}\n\n"
        f"Derived community summaries (A labels):\n{context_text(evidences)}"
    )


def within_budget(plan: AggregatePlan, max_characters: int) -> AggregatePlan:
    """完整选取能进入既有上下文预算的报告；不截断任何已选报告的真实来源。"""
    selected: tuple[AggregateEvidence, ...] = ()
    omitted = False
    for evidence in plan.evidences:
        proposed = (*selected, replace(evidence, label=f"A{len(selected)+1}"))
        if len(prompt_context("", proposed)) > max_characters:
            omitted = True
            continue
        selected = proposed
    return AggregatePlan(
        evidences=selected,
        warnings=(
            (*plan.warnings, "aggregate_context_budget_exhausted") if omitted else plan.warnings
        ),
    )


def bind_original_context(
    context: tuple[RetrievalCandidate, ...], evidences: tuple[AggregateEvidence, ...]
) -> tuple[RetrievalCandidate, ...]:
    """社区模式的 C 原文也绑定到同一来源复核闭包，不能混入未绑定版本的候选。"""
    sources = {
        (source.knowledge_base_id, source.document_id, source.engine_chunk_id): source
        for evidence in evidences
        for source in evidence.sources
    }
    bound: list[RetrievalCandidate] = []
    for candidate in context:
        source = sources.get(
            (candidate.knowledge_base_id, candidate.document_id, candidate.engine_chunk_id)
        )
        if (
            source is None
            or candidate.resource_scope_id != source.resource_scope_id
            or source.quote not in candidate.content
        ):
            continue
        bound.append(
            replace(
                candidate,
                content=source.quote,
                positions=source.positions,
                document_name=source.document_name,
            )
        )
    return tuple(bound)


def labels_valid(
    answer: str, citations: tuple[Citation, ...], evidences: tuple[AggregateEvidence, ...]
) -> bool:
    """社区回答必须实际使用 A 证据，且所有 C/A 标签都属于本次允许集合。"""
    allowed = ({item.label for item in citations} | {item.label for item in evidences})
    labels = _LABEL.findall(answer)
    return (
        bool(answer.strip())
        and len(allowed) == len(citations) + len(evidences)
        and all(re.fullmatch(r"A[1-9][0-9]*", item.label) for item in evidences)
        and any(item.label in labels for item in evidences)
        and all(label in allowed for label in labels)
        and citations_in_sources(
            tuple(item for item in citations if item.label in labels),
            tuple(item for item in evidences if item.label in labels),
        )
    )


def citations_in_sources(
    citations: tuple[Citation, ...], evidences: tuple[AggregateEvidence, ...]
) -> bool:
    """发布、缓存和历史读取共用同一闭包：每条 C 必须由实际返回 A 的真实来源支持。"""
    sources = tuple(source for evidence in evidences for source in evidence.sources)
    for citation in citations:
        pages = tuple(position.page for position in citation.positions if position.page is not None)
        if (
            citation.page_start != (min(pages) if pages else None)
            or citation.page_end != (max(pages) if pages else None)
            or not any(
                citation.knowledge_base_id == source.knowledge_base_id
                and citation.document_id == source.document_id
                and citation.resource_scope_id == source.resource_scope_id
                and citation.engine_chunk_id == source.engine_chunk_id
                and citation.document_name == source.document_name
                and citation.quote == source.quote
                and citation.positions == source.positions
                for source in sources
            )
        ):
            return False
    return True


def used(
    answer: str, evidences: tuple[AggregateEvidence, ...]
) -> tuple[AggregateEvidence, ...]:
    """只发布回答实际引用的摘要，仍保留该摘要的全部真实来源。"""
    labels = set(_LABEL.findall(answer))
    return tuple(item for item in evidences if item.label in labels)


def public_payload(evidence: AggregateEvidence) -> dict[str, object]:
    """JSON/SSE 公共投影不输出内部 Scope、引擎文档映射或哈希校验材料。"""
    return {
        "aggregate_id": str(evidence.aggregate_id),
        "label": evidence.label,
        "kind": "community_aggregate",
        "derived": True,
        "origin": evidence.origin,
        "generator_model_ref": evidence.generator_model_ref,
        "strategy_ref": evidence.strategy_ref,
        "knowledge_base_id": str(evidence.knowledge_base_id),
        "community_id": evidence.community_id,
        "title": evidence.title,
        "summary": evidence.summary,
        "graph_generation": evidence.graph_generation,
        "content_revision": evidence.content_revision,
        "configuration_version": evidence.configuration_version,
        "graph_profile_ref": evidence.graph_profile_ref,
        "sources": [
            {
                "knowledge_base_id": str(source.knowledge_base_id),
                "document_id": str(source.document_id),
                "document_name": source.document_name,
                "chunk_id": source.engine_chunk_id,
                "quote": source.quote,
                "document_revision": source.document_revision,
                "active_version_id": (
                    str(source.active_version_id) if source.active_version_id is not None else None
                ),
                "positions": [
                    {"page": position.page, "bbox": position.bbox} for position in source.positions
                ],
            }
            for source in evidence.sources
        ],
    }
