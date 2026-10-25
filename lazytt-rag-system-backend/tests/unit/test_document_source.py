"""PDF 原文件只读预览：验证范围、修订、二进制协议与连接清理；只使用内存合成文件。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from hashlib import sha256
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from tests.unit.test_offline_service import _security, _service
from tests.unit.test_ragflow_adapter import (
    FakeDocumentRepository,
    FakeKnowledgeBaseRepository,
    _document,
    _knowledge_base,
    _settings,
)

from rag_service.api.app import create_app
from rag_service.domain.errors import RagError
from rag_service.domain.models import DocumentStatus, KnowledgeBaseScopeType
from rag_service.domain.ports import DocumentSource, GetDocumentSourceQuery, ListEngineChunksQuery
from rag_service.infrastructure.ragflow.adapter import RagFlowAdapter
from rag_service.security.context import derive_system_resource_scope_id
from rag_service.settings import ServiceSettings


class TrackedStream(httpx.AsyncByteStream):
    """按小段供给合成 PDF，记录连接是否关闭，防止把整个文件预先缓冲的回归。"""

    def __init__(self, content: bytes) -> None:
        self.content = content
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        """模拟下游分段响应，不访问文件系统。"""
        for offset in range(0, len(self.content), 4096):
            yield self.content[offset : offset + 4096]

    async def aclose(self) -> None:
        """记录资源释放，供完成、错误和取消测试断言。"""
        self.closed = True


@pytest.mark.asyncio
async def test_source_stream_uses_mapped_ids_and_closes_after_complete_or_cancel() -> None:
    """下载仅用服务端映射 ID；正常消费和提前关闭均释放连接，不发送写请求。"""
    principal = _security()
    kb = _knowledge_base(principal)
    document = _document(principal, kb, filename="合成.pdf", mime_type="application/pdf")
    body = b"%PDF-1.7\n" + b"synthetic" * 15000
    document.file_size_bytes = len(body)
    document.content_sha256 = sha256(body).hexdigest()
    document.status = DocumentStatus.READY
    streams: list[TrackedStream] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/api/v1/datasets/engine-dataset-1/documents/engine-document-1"
        streams.append(TrackedStream(body))
        return httpx.Response(200, headers={"content-type": "application/pdf"}, stream=streams[-1])

    async with httpx.AsyncClient(
        base_url="http://ragflow.test", transport=httpx.MockTransport(handler)
    ) as client:
        adapter = RagFlowAdapter(
            _settings(),
            FakeKnowledgeBaseRepository((kb,)),
            FakeDocumentRepository((document,)),
            client=client,
        )
        query = GetDocumentSourceQuery(principal, kb.id, document.id, 1)
        source = await adapter.get_document_source(query)
        assert not streams[-1].closed
        assert b"".join([part async for part in source.content]) == body
        assert streams[-1].closed
        source = await adapter.get_document_source(query)
        await source.close()
        assert streams[-1].closed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case,status",
    [
        ("revision", 409),
        ("parsing", 409),
        ("non_pdf", 415),
        ("too_large", 413),
        ("unknown", 404),
        ("other_scope", 404),
    ],
)
async def test_source_rejects_invalid_target_before_network(case: str, status: int) -> None:
    """跨范围、不存在、非 PDF、超限和过期修订都不能发起下游下载。"""
    principal = _security()
    kb = _knowledge_base(principal)
    document = _document(principal, kb, filename="fixture.pdf", mime_type="application/pdf")
    document.status = DocumentStatus.PARSING if case == "parsing" else DocumentStatus.READY
    if case == "non_pdf":
        document.filename = "fixture.docx"
    if case == "too_large":
        document.file_size_bytes = 101 * 1024 * 1024

    def handler(_request: httpx.Request) -> httpx.Response:
        pytest.fail("invalid source target must not contact RAGFlow")

    async with httpx.AsyncClient(
        base_url="http://ragflow.test", transport=httpx.MockTransport(handler)
    ) as client:
        adapter = RagFlowAdapter(
            _settings(),
            FakeKnowledgeBaseRepository((kb,)),
            FakeDocumentRepository((document,)),
            client=client,
        )
        query = GetDocumentSourceQuery(
            _security("other") if case == "other_scope" else principal,
            kb.id,
            uuid4() if case == "unknown" else document.id,
            2 if case == "revision" else 1,
        )
        with pytest.raises(RagError) as error:
            await adapter.get_document_source(query)
        assert error.value.http_status == status


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["redirect", "json", "html", "size", "hash", "truncated", "timeout", "missing"]
)
async def test_source_does_not_accept_error_envelopes_or_corrupt_files(case: str) -> None:
    """下游错误、重定向、伪 PDF、错误长度/摘要均不被当作可预览源文件，连接必须释放。"""
    principal = _security()
    kb = _knowledge_base(principal)
    document = _document(principal, kb, filename="fixture.pdf", mime_type="application/pdf")
    document.status = DocumentStatus.READY
    body = b"%PDF-1.7\nsynthetic"
    document.file_size_bytes = len(body)
    document.content_sha256 = sha256(body).hexdigest()
    if case == "hash":
        document.content_sha256 = "0" * 64
    stream = TrackedStream(
        b"<html>error" if case == "html" else body[:-1] if case == "truncated" else body
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if case == "timeout":
            raise httpx.ReadTimeout("synthetic", request=request)
        return httpx.Response(
            302 if case == "redirect" else 404 if case == "missing" else 200,
            headers={
                "content-type": "application/json" if case == "json" else "application/pdf",
                **({"content-length": "99"} if case == "size" else {}),
            },
            stream=stream,
        )

    async with httpx.AsyncClient(
        base_url="http://ragflow.test", transport=httpx.MockTransport(handler)
    ) as client:
        adapter = RagFlowAdapter(
            _settings(),
            FakeKnowledgeBaseRepository((kb,)),
            FakeDocumentRepository((document,)),
            client=client,
        )
        with pytest.raises(RagError):
            source = await adapter.get_document_source(
                GetDocumentSourceQuery(principal, kb.id, document.id, 1)
            )
            _ = [part async for part in source.content]
        assert case == "timeout" or stream.closed


@pytest.mark.asyncio
async def test_fixed_revision_chunks_refuse_a_changed_document() -> None:
    """预览期间文档换版时，切块与文件使用同一修订检查，不静默套用新版本位置。"""
    principal = _security()
    kb = _knowledge_base(principal)
    document = _document(principal, kb)
    document.status = DocumentStatus.READY
    document.document_revision = 2
    adapter = RagFlowAdapter(
        _settings(), FakeKnowledgeBaseRepository((kb,)), FakeDocumentRepository((document,))
    )
    try:
        with pytest.raises(RagError, match="版本或解析状态"):
            await adapter.list_chunks(
                ListEngineChunksQuery(principal, kb.id, document.id, document_revision=1)
            )
    finally:
        await adapter.close()


@pytest.mark.parametrize("system_scope", [False, True])
def test_source_api_checks_access_and_returns_uncached_pdf(
    service_settings: ServiceSettings,
    system_scope: bool,
) -> None:
    """真实路由/应用层验证共享 Scope；无权与不存在都是 404，成功只返回 PDF 和安全头。"""
    service, kbs, documents, _, staging, engine = _service(service_settings)
    principal = _security("tenant-a")
    resource_scope = (
        derive_system_resource_scope_id() if system_scope else principal.tenant_resource_scope_id
    )
    resource = principal.for_resource_scope(resource_scope)
    kb = _knowledge_base(resource)
    kb.scope_type = KnowledgeBaseScopeType.SYSTEM if system_scope else KnowledgeBaseScopeType.TENANT
    document = _document(resource, kb, filename="合成.pdf", mime_type="application/pdf")
    document.status = DocumentStatus.READY
    kbs.items[kb.id] = kb
    documents.items[document.id] = document
    closed = AsyncMock()

    async def content() -> AsyncIterator[bytes]:
        yield b"%PDF-1.7\nsynthetic"

    source = DocumentSource(document.filename, 18, content(), closed)
    engine.get_document_source = AsyncMock(return_value=source)
    app = create_app(service_settings, offline_service=service)
    path = f"/v1/management/documents/{document.id}/source?document_revision=1"
    with TestClient(app) as client:
        # 当前 Direct Identity 契约对缺失 Header 返回 400，而非生产登录态的 401。
        assert client.get(path).status_code == 400
        headers = {"X-Tenant-ID": "tenant-a", "X-User-ID": "member"}
        assert (
            client.get(path.replace(str(document.id), str(uuid4())), headers=headers).status_code
            == 404
        )
        if not system_scope:
            assert client.get(path, headers={**headers, "X-Tenant-ID": "other"}).status_code == 404
        assert engine.get_document_source.await_count == 0
        # 参数校验统一映射为 400；缺少修订号时也不能读取原文件。
        assert client.get(path.split("?")[0], headers=headers).status_code == 400
        assert engine.get_document_source.await_count == 0
        response = client.get(path, headers=headers)
        assert response.status_code == 200
        assert response.content.startswith(b"%PDF-")
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["content-type"] == "application/pdf"
        assert "filename*=UTF-8''" in response.headers["content-disposition"]
    query = engine.get_document_source.await_args.args[0]
    assert query.security.security_scope_id == resource_scope
    assert query.document_revision == 1
    assert closed.await_count == 1
    assert staging.saved == 0 and staging.deleted == []


@pytest.mark.asyncio
async def test_source_honors_management_policy(service_settings: ServiceSettings) -> None:
    """管理授权拒绝时立即退出，不能直接复用共享读取绕过将来的管理员策略。"""
    service, _, _, _, _, engine = _service(service_settings)
    policy = AsyncMock()
    policy.require_access.side_effect = RagError("RAG-AUTH-003", "synthetic denied", 403)
    service._management_access = policy
    engine.get_document_source = AsyncMock()
    with pytest.raises(RagError):
        await service.get_document_source(_security(), uuid4(), document_revision=1)
    policy.require_access.assert_awaited_once()
    engine.get_document_source.assert_not_awaited()
