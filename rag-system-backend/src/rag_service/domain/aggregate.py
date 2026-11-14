"""Gateway 生成的社区摘要及其真实原文来源。

摘要属于派生证据，不能放进普通 Citation.quote。持久化载荷保留资源范围、活动
版本与原文摘要，供每次展示和缓存命中重新核验；公共 DTO 另行去除内部定位字段。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from uuid import UUID

from rag_service.domain.models import SourcePosition
from rag_service.security.context import SecurityContext

EvidenceMode = Literal["original", "community"]
AGGREGATE_PROFILE_REF = "rag-answer-community-localized@1"
AGGREGATE_STRATEGY_REF = "gateway-connected-community@1"
MAX_AGGREGATE_EVIDENCE = 4
MAX_AGGREGATE_SOURCES = 16
_DIGEST = re.compile(r"^[a-f0-9]{64}$")


def _text(value: Any, name: str, maximum: int, *, empty: bool = False) -> str:
    """拒绝隐式 str() 转换，防止损坏载荷把列表或布尔值变成可信文本。"""
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise ValueError(f"invalid aggregate {name}")
    return value


def _integer(value: Any, name: str) -> int:
    """JSON 的布尔值不是修订号；不接受浮点数或数字字符串。"""
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value < 2**63:
        raise ValueError(f"invalid aggregate {name}")
    return value


def _uuid(value: Any, name: str) -> UUID:
    """只接受 UUID 文本，不把任意对象先转成字符串。"""
    return UUID(_text(value, name, 36))


def _digest(value: Any, name: str) -> str:
    """哈希须为完整 SHA-256 小写十六进制，不接受缩略指纹。"""
    digest = _text(value, name, 64)
    if not _DIGEST.fullmatch(digest):
        raise ValueError(f"invalid aggregate {name}")
    return digest


def _position(payload: Any) -> SourcePosition:
    """原文坐标只接受有限数值与合法页码，禁止 bool 冒充数字。"""
    if not isinstance(payload, dict):
        raise ValueError("invalid aggregate position")
    page = payload.get("page")
    if page is not None and (type(page) is not int or page < 1):
        raise ValueError("invalid aggregate page")
    bbox = payload.get("bbox")
    if bbox is None:
        return SourcePosition(page=page)
    if (
        not isinstance(bbox, list | tuple)
        or len(bbox) != 4
        or any(type(item) not in {int, float} or not math.isfinite(item) for item in bbox)
    ):
        raise ValueError("invalid aggregate bbox")
    return SourcePosition(page=page, bbox=(bbox[0], bbox[1], bbox[2], bbox[3]))


@dataclass(frozen=True, slots=True)
class AggregateSource:
    """一个确实回读过的原文块；修订、版本和范围不是由浏览器提交的授权凭据。"""

    knowledge_base_id: UUID
    document_id: UUID
    document_name: str
    engine_chunk_id: str
    quote: str
    resource_scope_id: str
    document_revision: int
    active_version_id: UUID | None
    engine_document_id: str
    quote_sha256: str
    positions: tuple[SourcePosition, ...] = ()


@dataclass(frozen=True, slots=True)
class AggregateEvidence:
    """明确标记 Gateway 生成的多文档摘要，A 编号仅在某次回答选中后赋予。"""

    aggregate_id: UUID
    label: str
    knowledge_base_id: UUID
    community_id: str
    title: str
    summary: str
    graph_generation: int
    content_revision: int
    configuration_version: int
    graph_profile_ref: str
    report_sha256: str
    sources: tuple[AggregateSource, ...]
    resource_scope_id: str
    generator_model_ref: str
    strategy_ref: str = AGGREGATE_STRATEGY_REF
    origin: Literal["gateway_generated"] = "gateway_generated"


class AggregateCommunityPort(Protocol):
    """返回已授权的可用报告，并在输出前再次核验图代数和每个真实来源。"""

    async def list_available(
        self, security: SecurityContext, kb_ids: tuple[UUID, ...]
    ) -> tuple[AggregateEvidence, ...]:
        """报告不可用时返回空集合；不得把原生图描述冒充原文来源。"""
        ...

    async def revalidate(
        self, security: SecurityContext, evidences: tuple[AggregateEvidence, ...]
    ) -> bool:
        """任一来源删除、修订、换版本或图状态变化都返回 False。"""
        ...


def source_to_payload(source: AggregateSource) -> dict[str, Any]:
    """生成仅用于受控持久化和缓存的完整来源载荷。"""
    return {
        "knowledge_base_id": str(source.knowledge_base_id),
        "document_id": str(source.document_id),
        "document_name": source.document_name,
        "engine_chunk_id": source.engine_chunk_id,
        "quote": source.quote,
        "resource_scope_id": source.resource_scope_id,
        "document_revision": source.document_revision,
        "active_version_id": str(source.active_version_id) if source.active_version_id else None,
        "engine_document_id": source.engine_document_id,
        "quote_sha256": source.quote_sha256,
        "positions": [{"page": item.page, "bbox": item.bbox} for item in source.positions],
    }


def source_from_payload(payload: dict[str, Any]) -> AggregateSource:
    """恢复完整来源；缺少来源绑定字段的旧记录不能获得默认可信身份。"""
    if not isinstance(payload, dict):
        raise ValueError("aggregate source must be an object")
    raw_positions = payload["positions"]
    if not isinstance(raw_positions, list) or len(raw_positions) > 128:
        raise ValueError("invalid aggregate positions")
    positions = tuple(_position(item) for item in raw_positions)
    version = payload["active_version_id"]
    quote = _text(payload["quote"], "quote", 32768)
    quote_sha256 = _digest(payload["quote_sha256"], "quote_sha256")
    if hashlib.sha256(quote.encode("utf-8")).hexdigest() != quote_sha256:
        raise ValueError("aggregate quote hash mismatch")
    return AggregateSource(
        knowledge_base_id=_uuid(payload["knowledge_base_id"], "knowledge_base_id"),
        document_id=_uuid(payload["document_id"], "document_id"),
        document_name=_text(payload["document_name"], "document_name", 1024),
        engine_chunk_id=_text(payload["engine_chunk_id"], "chunk_id", 512),
        quote=quote,
        resource_scope_id=_text(payload["resource_scope_id"], "resource_scope_id", 128),
        document_revision=_integer(payload["document_revision"], "document_revision"),
        active_version_id=_uuid(version, "active_version_id") if version is not None else None,
        engine_document_id=_text(payload["engine_document_id"], "engine_document_id", 512),
        quote_sha256=quote_sha256,
        positions=positions,
    )


def to_payload(evidence: AggregateEvidence) -> dict[str, Any]:
    """保存报告正文与全部来源快照，不依赖当前可变文档名称重新生成证据。"""
    return {
        "aggregate_id": str(evidence.aggregate_id),
        "label": evidence.label,
        "knowledge_base_id": str(evidence.knowledge_base_id),
        "community_id": evidence.community_id,
        "title": evidence.title,
        "summary": evidence.summary,
        "graph_generation": evidence.graph_generation,
        "content_revision": evidence.content_revision,
        "configuration_version": evidence.configuration_version,
        "graph_profile_ref": evidence.graph_profile_ref,
        "report_sha256": evidence.report_sha256,
        "sources": [source_to_payload(source) for source in evidence.sources],
        "resource_scope_id": evidence.resource_scope_id,
        "origin": evidence.origin,
        "generator_model_ref": evidence.generator_model_ref,
        "strategy_ref": evidence.strategy_ref,
    }


def from_payload(payload: dict[str, Any]) -> AggregateEvidence:
    """恢复完整报告；上层仍必须执行范围和实时来源复核。"""
    if not isinstance(payload, dict):
        raise ValueError("aggregate evidence must be an object")
    if payload["origin"] != "gateway_generated":
        raise ValueError("unsupported aggregate origin")
    raw_sources = payload["sources"]
    if not isinstance(raw_sources, list) or not 2 <= len(raw_sources) <= MAX_AGGREGATE_SOURCES:
        raise ValueError("invalid aggregate sources")
    evidence = AggregateEvidence(
        aggregate_id=_uuid(payload["aggregate_id"], "aggregate_id"),
        label=_text(payload["label"], "label", 16, empty=True),
        knowledge_base_id=_uuid(payload["knowledge_base_id"], "knowledge_base_id"),
        community_id=_text(payload["community_id"], "community_id", 512),
        title=_text(payload["title"], "title", 512),
        summary=_text(payload["summary"], "summary", 6000),
        graph_generation=_integer(payload["graph_generation"], "graph_generation"),
        content_revision=_integer(payload["content_revision"], "content_revision"),
        configuration_version=_integer(payload["configuration_version"], "configuration_version"),
        graph_profile_ref=_text(payload["graph_profile_ref"], "graph_profile_ref", 128),
        report_sha256=_digest(payload["report_sha256"], "report_sha256"),
        sources=tuple(source_from_payload(item) for item in raw_sources),
        resource_scope_id=_text(payload["resource_scope_id"], "resource_scope_id", 128),
        generator_model_ref=_text(payload["generator_model_ref"], "generator_model_ref", 128),
        strategy_ref=_text(payload["strategy_ref"], "strategy_ref", 128),
    )
    if evidence.label and not re.fullmatch(r"A[1-9][0-9]*", evidence.label):
        raise ValueError("invalid aggregate label")
    if evidence.strategy_ref != AGGREGATE_STRATEGY_REF:
        raise ValueError("unsupported aggregate strategy")
    if evidence.report_sha256 != hashlib.sha256(evidence.summary.encode("utf-8")).hexdigest():
        raise ValueError("aggregate summary hash mismatch")
    if len({item.document_id for item in evidence.sources}) < 2:
        raise ValueError("aggregate evidence requires multiple documents")
    identities = {(item.document_id, item.engine_chunk_id) for item in evidence.sources}
    if len(identities) != len(evidence.sources):
        raise ValueError("duplicate aggregate source")
    if len({item.engine_chunk_id for item in evidence.sources}) < 2:
        raise ValueError("aggregate evidence requires multiple chunks")
    if any(
        item.knowledge_base_id != evidence.knowledge_base_id
        or item.resource_scope_id != evidence.resource_scope_id
        for item in evidence.sources
    ):
        raise ValueError("aggregate source scope mismatch")
    return evidence


def fingerprint(evidences: tuple[AggregateEvidence, ...]) -> str:
    """缓存计划摘要不含每次请求重分配的 ID/标签，包含正文及全部来源快照。"""
    payloads = []
    for evidence in evidences:
        payload = to_payload(evidence)
        payload.pop("aggregate_id")
        payload.pop("label")
        payloads.append(payload)
    canonical = json.dumps(payloads, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def evidences_from_payload(value: Any) -> tuple[AggregateEvidence, ...]:
    """集合与序列化总量也有上限，缓存不能用很多小报告绕开单报告预算。"""
    if not isinstance(value, list) or len(value) > MAX_AGGREGATE_EVIDENCE:
        raise ValueError("invalid aggregate evidence collection")
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    if len(encoded.encode("utf-8")) > 1024 * 1024:
        raise ValueError("aggregate evidence collection exceeds budget")
    evidences = tuple(from_payload(item) for item in value)
    labels = [item.label for item in evidences if item.label]
    if len(labels) != len(set(labels)):
        raise ValueError("duplicate aggregate label")
    return evidences
