"""离线 API 边界测试：认证范围、知识库映射、流式上传、元数据和请求字段校验。"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi.testclient import TestClient

from rag_service.api.app import create_app
from rag_service.application.offline import (
    CreateKnowledgeBaseInput,
    DocumentAccepted,
    DocumentVersionAccepted,
    UpdateKnowledgeBaseInput,
    UploadDocumentInput,
    UploadDocumentVersionInput,
)
from rag_service.domain.errors import ResourceNotFoundError
from rag_service.domain.models import (
    DocumentRecord,
    DocumentUpdateMode,
    DocumentVersion,
    IngestionJob,
    JobType,
    KnowledgeBase,
    KnowledgeBaseStatus,
)
from rag_service.domain.ports import Page
from rag_service.security.context import SecurityContext
from rag_service.settings import ServiceSettings


class FakeOffline:
    def __init__(self) -> None:
        self.scope: str | None = None
        self.created_input: CreateKnowledgeBaseInput | None = None
        self.uploaded_input: UploadDocumentInput | None = None
        self.uploaded_version_input: UploadDocumentVersionInput | None = None
        self.uploaded_content = b""
        self.uploaded_version_content = b""
        self.knowledge_base = KnowledgeBase(
            security_scope_id="a" * 64,
            name="Synthetic KB",
            parser_profile_ref="office-default@1",
            chunk_profile_ref="general-512@1",
            embedding_profile_ref="embedding-openai-compatible@3",
            retrieval_profile_ref="hybrid-default@1",
            ragflow_dataset_id="engine-dataset",
            status=KnowledgeBaseStatus.READY,
        )
        self.document = DocumentRecord(
            security_scope_id="a" * 64,
            knowledge_base_id=self.knowledge_base.id,
            filename="policy-v1.txt",
            safe_filename="policy-v1.txt",
            mime_type="text/plain",
            file_size_bytes=2,
            content_sha256="b" * 64,
            parser_profile_ref="office-default@1",
            chunk_profile_ref="general-512@1",
        )

    async def create_knowledge_base(
        self,
        security: SecurityContext,
        value: CreateKnowledgeBaseInput,
    ) -> KnowledgeBase:
        self.scope = security.security_scope_id
        self.created_input = value
        self.knowledge_base.security_scope_id = security.security_scope_id
        self.knowledge_base.name = value.name
        return self.knowledge_base

    async def get_knowledge_base(
        self,
        security: SecurityContext,
        knowledge_base_id: UUID,
    ) -> KnowledgeBase:
        if (
            security.security_scope_id != self.knowledge_base.security_scope_id
            or knowledge_base_id != self.knowledge_base.id
        ):
            raise ResourceNotFoundError("RAG-KB-001", "知识库不存在")
        return self.knowledge_base

    async def list_knowledge_bases(
        self,
        security: SecurityContext,
        **_filters: Any,
    ) -> Page:
        if security.security_scope_id != self.knowledge_base.security_scope_id:
            return Page(items=(), page=1, page_size=20, total=0)
        return Page(items=(self.knowledge_base,), page=1, page_size=20, total=1)

    async def list_shared_knowledge_bases(
        self,
        security: SecurityContext,
        **filters: Any,
    ) -> Page:
        """测试替身复用同一份可见数据，但绕过管理接口方法名。"""
        return await self.list_knowledge_bases(security, **filters)

    async def list_shared_documents(
        self,
        security: SecurityContext,
        knowledge_base_id: UUID,
        **_filters: Any,
    ) -> Page:
        """普通用户只能列出当前可见知识库中的文档。"""
        await self.get_knowledge_base(security, knowledge_base_id)
        return Page(items=(self.document,), page=1, page_size=20, total=1)

    async def get_shared_document(
        self,
        security: SecurityContext,
        document_id: UUID,
    ) -> DocumentRecord:
        """详情读取同样验证资源 Scope，模拟应用层防越权行为。"""
        if document_id != self.document.id:
            raise ResourceNotFoundError("RAG-DOC-001", "文档不存在")
        await self.get_knowledge_base(security, self.document.knowledge_base_id)
        return self.document

    async def list_shared_chunks(
        self,
        security: SecurityContext,
        document_id: UUID,
        **_filters: Any,
    ) -> Page:
        """本组路由测试只验证空 Chunk 页的只读响应结构。"""
        await self.get_shared_document(security, document_id)
        return Page(items=(), page=1, page_size=20, total=0)

    async def update_knowledge_base(
        self,
        security: SecurityContext,
        knowledge_base_id: UUID,
        value: UpdateKnowledgeBaseInput,
    ) -> KnowledgeBase:
        knowledge_base = await self.get_knowledge_base(security, knowledge_base_id)
        if value.name:
            knowledge_base.name = value.name
        return knowledge_base

    async def upload_document(
        self,
        security: SecurityContext,
        knowledge_base_id: UUID,
        value: UploadDocumentInput,
    ) -> DocumentAccepted:
        await self.get_knowledge_base(security, knowledge_base_id)
        self.uploaded_input = value
        self.uploaded_content = b"".join([chunk async for chunk in value.stream])
        document = DocumentRecord(
            security_scope_id=security.security_scope_id,
            knowledge_base_id=knowledge_base_id,
            filename=value.filename,
            safe_filename=value.filename,
            mime_type=value.mime_type,
            file_size_bytes=len(self.uploaded_content),
            content_sha256="a" * 64,
            parser_profile_ref="office-default@1",
            chunk_profile_ref="general-512@1",
            metadata=value.metadata,
        )
        job = IngestionJob(
            security_scope_id=security.security_scope_id,
            job_type=JobType.UPLOAD,
            knowledge_base_id=knowledge_base_id,
            document_id=document.id,
        )
        return DocumentAccepted(document=document, job=job)

    async def upload_document_version(
        self,
        security: SecurityContext,
        document_id: UUID,
        value: UploadDocumentVersionInput,
    ) -> DocumentVersionAccepted:
        """读取版本 multipart 流并返回可供路由序列化的候选和 Job。"""
        assert document_id == self.document.id
        self.document.security_scope_id = security.security_scope_id
        self.uploaded_version_input = value
        self.uploaded_version_content = b"".join([chunk async for chunk in value.stream])
        version = DocumentVersion(
            security_scope_id=security.security_scope_id,
            knowledge_base_id=self.document.knowledge_base_id,
            document_id=self.document.id,
            version_number=2,
            filename=value.filename,
            safe_filename=value.filename,
            mime_type=value.mime_type,
            file_size_bytes=len(self.uploaded_version_content),
            content_sha256="c" * 64,
            metadata=value.metadata,
            parser_profile_ref="office-default@1",
            chunk_profile_ref="general-512@1",
            requested_mode=value.mode,
            effective_mode=DocumentUpdateMode.SAFE_REPLACE,
        )
        job = IngestionJob(
            security_scope_id=security.security_scope_id,
            job_type=JobType.UPDATE_DOCUMENT,
            knowledge_base_id=self.document.knowledge_base_id,
            document_id=self.document.id,
            payload={"version_id": str(version.id)},
        )
        return DocumentVersionAccepted(version=version, job=job)


def _client(
    service_settings: ServiceSettings,
    offline: FakeOffline,
) -> TestClient:
    client = TestClient(
        create_app(
            service_settings,
            offline_service=offline,  # type: ignore[arg-type]
        )
    )
    # 大多数用例只关注业务 DTO；统一注入合法 Tenant ID，避免每个请求重复样板。
    # 单独验证租户缺失的用例会显式删除该默认 Header。
    client.headers["X-Tenant-ID"] = "test-tenant"
    return client


def test_create_knowledge_base_uses_verified_scope(
    service_settings: ServiceSettings,
) -> None:
    offline = FakeOffline()
    with _client(service_settings, offline) as client:
        response = client.post(
            "/v1/management/knowledge-bases",
            headers={
                "X-User-ID": "test-user",
                "Idempotency-Key": "create-kb-1",
            },
            json={
                "scope_type": "TENANT",
                "name": "API Synthetic KB",
                "parser_profile": "office-default@1",
                "chunk_profile": "general-512@1",
                "embedding_profile": "embedding-openai-compatible@3",
                "retrieval_profile": "hybrid-default@1",
            },
        )

    assert response.status_code == 201
    assert response.json()["data"]["name"] == "API Synthetic KB"
    assert offline.scope is not None
    assert len(offline.scope) == 64
    assert offline.created_input is not None


def test_knowledge_base_list_and_detail_expose_document_count(
    service_settings: ServiceSettings,
) -> None:
    offline = FakeOffline()
    with _client(service_settings, offline) as client:
        created = client.post(
            "/v1/management/knowledge-bases",
            headers={
                "X-User-ID": "test-user",
                "Idempotency-Key": "document-count-kb",
            },
            json={
                "scope_type": "TENANT",
                "name": "Counted KB",
                "parser_profile": "office-default@1",
                "chunk_profile": "general-512@1",
                "embedding_profile": "embedding-openai-compatible@3",
                "retrieval_profile": "hybrid-default@1",
            },
        )
        offline.knowledge_base.document_count = 2
        list_response = client.get(
            "/v1/management/knowledge-bases",
            headers={"X-User-ID": "test-user"},
        )
        detail_response = client.get(
            f"/v1/management/knowledge-bases/{created.json()['data']['id']}",
            headers={"X-User-ID": "test-user"},
        )

    assert list_response.status_code == 200
    assert list_response.json()["data"][0]["document_count"] == 2
    assert detail_response.status_code == 200
    assert detail_response.json()["data"]["document_count"] == 2


def test_shared_read_routes_expose_scope_without_management_operations(
    service_settings: ServiceSettings,
) -> None:
    """普通用户接口应能浏览共享库、文档和 Chunk，并明确返回知识库归属。"""
    offline = FakeOffline()
    with _client(service_settings, offline) as client:
        headers = {"X-User-ID": "test-user"}
        # 先经创建路由把测试知识库绑定到本请求身份计算出的资源 Scope；之后四个
        # 请求才是在同一身份下验证只读入口，而不是依赖测试替身的初始占位 Scope。
        created = client.post(
            "/v1/management/knowledge-bases",
            headers={**headers, "Idempotency-Key": "shared-read-kb"},
            json={
                "scope_type": "TENANT",
                "name": "Shared read KB",
                "parser_profile": "office-default@1",
                "chunk_profile": "general-512@1",
                "embedding_profile": "embedding-openai-compatible@3",
                "retrieval_profile": "hybrid-default@1",
            },
        )
        assert created.status_code == 201
        knowledge_bases = client.get("/v1/knowledge-bases", headers=headers)
        documents = client.get(
            f"/v1/knowledge-bases/{offline.knowledge_base.id}/documents",
            headers=headers,
        )
        document = client.get(
            f"/v1/documents/{offline.document.id}",
            headers=headers,
        )
        chunks = client.get(
            f"/v1/documents/{offline.document.id}/chunks",
            headers=headers,
        )

    assert knowledge_bases.status_code == 200
    assert knowledge_bases.json()["data"][0]["scope_type"] == "TENANT"
    assert documents.status_code == 200
    assert documents.json()["data"][0]["filename"] == "policy-v1.txt"
    assert document.status_code == 200
    assert document.json()["data"]["id"] == str(offline.document.id)
    assert chunks.status_code == 200
    assert chunks.json()["meta"]["total"] == 0


def test_upload_streams_fixture_and_parses_metadata(
    service_settings: ServiceSettings,
) -> None:
    offline = FakeOffline()
    with _client(service_settings, offline) as client:
        created = client.post(
            "/v1/management/knowledge-bases",
            headers={
                "X-User-ID": "test-user",
                "Idempotency-Key": "create-kb-2",
            },
            json={
                "scope_type": "TENANT",
                "name": "API Synthetic KB",
                "parser_profile": "office-default@1",
                "chunk_profile": "general-512@1",
                "embedding_profile": "embedding-openai-compatible@3",
                "retrieval_profile": "hybrid-default@1",
            },
        )
        knowledge_base_id = created.json()["data"]["id"]
        response = client.post(
            f"/v1/management/knowledge-bases/{knowledge_base_id}/documents",
            headers={
                "X-User-ID": "test-user",
                "Idempotency-Key": "upload-document-1",
            },
            files={"file": ("fixture.txt", b"synthetic", "text/plain")},
            data={"metadata": '{"product":"ragflow"}', "auto_parse": "true"},
        )

    assert response.status_code == 202
    assert response.json()["data"]["status"] == "RECEIVED"
    assert offline.uploaded_content == b"synthetic"
    assert offline.uploaded_input is not None
    assert offline.uploaded_input.metadata == {"product": "ragflow"}
    assert offline.uploaded_input.auto_parse is True


def test_upload_document_version_streams_candidate_and_returns_effective_mode(
    service_settings: ServiceSettings,
) -> None:
    """版本上传边界保留流式文件、Metadata 和显式安全替换协商结果。"""
    offline = FakeOffline()
    with _client(service_settings, offline) as client:
        response = client.post(
            f"/v1/management/documents/{offline.document.id}/versions",
            headers={
                "X-User-ID": "test-user",
                "Idempotency-Key": "upload-version-1",
            },
            files={"file": ("policy-v2.txt", b"v2 body", "text/plain")},
            data={"metadata": '{"version":"2"}', "mode": "AUTO"},
        )

    assert response.status_code == 202
    assert response.json()["data"]["version_number"] == 2
    assert response.json()["data"]["effective_mode"] == "SAFE_REPLACE"
    assert offline.uploaded_version_content == b"v2 body"
    assert offline.uploaded_version_input is not None
    assert offline.uploaded_version_input.metadata == {"version": "2"}


def test_business_routes_require_direct_identity_without_calling_service(
    service_settings: ServiceSettings,
) -> None:
    offline = FakeOffline()
    with _client(service_settings, offline) as client:
        response = client.post(
            "/v1/management/knowledge-bases",
            headers={"Idempotency-Key": "missing-user-id-test"},
            json={
                "scope_type": "TENANT",
                "name": "Denied",
                "parser_profile": "office-default@1",
                "chunk_profile": "general-512@1",
                "embedding_profile": "embedding-openai-compatible@3",
                "retrieval_profile": "hybrid-default@1",
            },
        )

    assert response.status_code == 400
    assert response.json()["error"]["details"] == [
        {"reason": "missing", "field": "header.X-User-ID"}
    ]
    assert offline.created_input is None


def test_business_routes_require_direct_tenant_id_without_calling_service(
    service_settings: ServiceSettings,
) -> None:
    """即使 User ID 合法，缺少 Tenant ID 也必须在调用业务服务前返回 400。"""
    offline = FakeOffline()
    with _client(service_settings, offline) as client:
        del client.headers["X-Tenant-ID"]
        response = client.post(
            "/v1/management/knowledge-bases",
            headers={
                "X-User-ID": "test-user",
                "Idempotency-Key": "missing-tenant-id-test",
            },
            json={
                "scope_type": "TENANT",
                "name": "Denied",
                "parser_profile": "office-default@1",
                "chunk_profile": "general-512@1",
                "embedding_profile": "embedding-openai-compatible@3",
                "retrieval_profile": "hybrid-default@1",
            },
        )

    assert response.status_code == 400
    assert response.json()["error"]["details"] == [
        {"reason": "missing", "field": "header.X-Tenant-ID"}
    ]
    assert offline.created_input is None


def test_api_scope_changes_when_tenant_changes_for_same_user(
    service_settings: ServiceSettings,
) -> None:
    """相同 User ID 位于两个租户时必须产生不同 Scope，防止跨租户读取资源。"""
    first_service = FakeOffline()
    second_service = FakeOffline()
    request_body = {
        "scope_type": "TENANT",
        "name": "Tenant isolated KB",
        "parser_profile": "office-default@1",
        "chunk_profile": "general-512@1",
        "embedding_profile": "embedding-openai-compatible@3",
        "retrieval_profile": "hybrid-default@1",
    }
    with _client(service_settings, first_service) as first_client:
        first_response = first_client.post(
            "/v1/management/knowledge-bases",
            headers={
                "X-Tenant-ID": "tenant-a",
                "X-User-ID": "shared-user",
                "Idempotency-Key": "tenant-a-kb",
            },
            json=request_body,
        )
    with _client(service_settings, second_service) as second_client:
        second_response = second_client.post(
            "/v1/management/knowledge-bases",
            headers={
                "X-Tenant-ID": "tenant-b",
                "X-User-ID": "shared-user",
                "Idempotency-Key": "tenant-b-kb",
            },
            json=request_body,
        )

    assert first_response.status_code == 201
    assert second_response.status_code == 201
    assert first_service.scope is not None
    assert second_service.scope is not None
    assert first_service.scope != second_service.scope


def test_unknown_create_field_is_rejected(
    service_settings: ServiceSettings,
) -> None:
    offline = FakeOffline()
    with _client(service_settings, offline) as client:
        response = client.post(
            "/v1/management/knowledge-bases",
            headers={
                "X-User-ID": "test-user",
                "Idempotency-Key": "invalid-body-1",
            },
            json={
                "scope_type": "TENANT",
                "name": "Invalid",
                "parser_profile": "office-default@1",
                "chunk_profile": "general-512@1",
                "embedding_profile": "embedding-openai-compatible@3",
                "retrieval_profile": "hybrid-default@1",
                "security_scope_id": "forged",
            },
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "RAG-COMMON-001"
    assert offline.created_input is None
