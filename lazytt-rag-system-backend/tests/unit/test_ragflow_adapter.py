"""RAGFlow 适配器测试：HTTP 契约、ID 映射、重试边界、元数据翻译和跨租户过滤。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from io import BytesIO
from typing import Any
from uuid import UUID

import httpx
import pytest

from rag_service.domain.errors import RagFlowError, ResourceNotFoundError
from rag_service.domain.models import DocumentRecord, KnowledgeBase
from rag_service.domain.ports import (
    CreateDatasetCommand,
    DeleteEngineDatasetCommand,
    EngineRetrievalQuery,
    GetEngineDatasetQuery,
    ListEngineChunksQuery,
    ParseDocumentCommand,
    SetEngineDocumentVersionAvailabilityCommand,
    SetEngineVersionChunksAvailabilityCommand,
    UploadDocumentCommand,
)
from rag_service.infrastructure.ragflow.adapter import RagFlowAdapter
from rag_service.security.context import SecurityContext
from rag_service.settings import RagFlowSection


class FakeKnowledgeBaseRepository:
    def __init__(self, items: tuple[KnowledgeBase, ...]) -> None:
        self.items = items

    async def get(self, security_scope_id: str, knowledge_base_id: UUID) -> KnowledgeBase | None:
        return next(
            (
                item
                for item in self.items
                if item.security_scope_id == security_scope_id and item.id == knowledge_base_id
            ),
            None,
        )

    async def get_many(
        self, security_scope_id: str, knowledge_base_ids: list[UUID] | tuple[UUID, ...]
    ) -> tuple[KnowledgeBase, ...]:
        identifiers = set(knowledge_base_ids)
        return tuple(
            item
            for item in self.items
            if item.security_scope_id == security_scope_id and item.id in identifiers
        )

    async def add(self, security_scope_id: str, value: KnowledgeBase) -> None:
        raise NotImplementedError

    async def list(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def update(self, *args: Any, **kwargs: Any) -> bool:
        raise NotImplementedError


class FakeDocumentRepository:
    def __init__(self, items: tuple[DocumentRecord, ...] = ()) -> None:
        self.items = items

    async def get(self, security_scope_id: str, document_id: UUID) -> DocumentRecord | None:
        return next(
            (
                item
                for item in self.items
                if item.security_scope_id == security_scope_id and item.id == document_id
            ),
            None,
        )

    async def get_many(
        self, security_scope_id: str, document_ids: list[UUID] | tuple[UUID, ...]
    ) -> tuple[DocumentRecord, ...]:
        identifiers = set(document_ids)
        return tuple(
            item
            for item in self.items
            if item.security_scope_id == security_scope_id and item.id in identifiers
        )

    async def get_by_engine_ids(
        self, security_scope_id: str, engine_document_ids: list[str] | tuple[str, ...]
    ) -> tuple[DocumentRecord, ...]:
        identifiers = set(engine_document_ids)
        return tuple(
            item
            for item in self.items
            if item.security_scope_id == security_scope_id
            and item.ragflow_document_id in identifiers
        )

    async def add(self, security_scope_id: str, value: DocumentRecord) -> None:
        raise NotImplementedError


@pytest.fixture
def security() -> SecurityContext:
    return SecurityContext.create(
        subject_id="subject",
        tenant_id="tenant",
        rag_scope_id="scope",
        token_id="token",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )


def _knowledge_base(security: SecurityContext, *, mapped: bool = True) -> KnowledgeBase:
    return KnowledgeBase(
        security_scope_id=security.security_scope_id,
        name="产品 文档",
        parser_profile_ref="deepdoc-default@1",
        chunk_profile_ref="general-512@1",
        embedding_profile_ref="embedding-openai-compatible@3",
        retrieval_profile_ref="hybrid-default@1",
        ragflow_dataset_id="engine-dataset-1" if mapped else None,
    )


def _document(
    security: SecurityContext,
    knowledge_base: KnowledgeBase,
    *,
    filename: str = "fixture.txt",
    mime_type: str = "text/plain",
) -> DocumentRecord:
    """构造已映射到 RAGFlow 的测试文档，并允许测试指定真实文件类型。

    默认值继续服务普通文本测试；PPT/PPTX 和图片回归测试必须保留扩展名，因为适配器
    需要按扩展名避开 RAGFlow v0.26.4 不支持的 ``chunk_method`` 更新字段。
    """
    return DocumentRecord(
        security_scope_id=security.security_scope_id,
        knowledge_base_id=knowledge_base.id,
        filename=filename,
        safe_filename=filename,
        mime_type=mime_type,
        file_size_bytes=7,
        content_sha256=sha256(b"fixture").hexdigest(),
        parser_profile_ref="deepdoc-default@1",
        chunk_profile_ref="general-512@1",
        ragflow_document_id="engine-document-1",
    )


def _settings() -> RagFlowSection:
    return RagFlowSection(
        base_url="http://ragflow.test",
        api_key="test-key",
        embedding_model="synthetic-embedding@fixture@OpenAI-API-Compatible",
        vision_model="synthetic-vision@fixture@OpenAI-API-Compatible",
        vision_runtime_model="synthetic-vision",
        embedding_dimension=1024,
        max_attempts=1,
    )


def _client(
    handler: Any,
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url="http://ragflow.test",
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
async def test_start_parse_patches_document_profile_before_creating_chunks(
    security: SecurityContext,
) -> None:
    """文档级 Chunk Profile 必须先 PATCH 成功，然后才可启动解析。

    断言同时锁定 RAGFlow v0.26.4 的兼容形状：标准字段留在
    ``parser_config``，公开 Schema 尚未声明的重叠百分比放入 ``ext``。
    """
    knowledge_base = _knowledge_base(security)
    document = _document(security, knowledge_base)
    calls: list[tuple[str, str, dict[str, Any]]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"code": 0, "data": True})

    adapter = RagFlowAdapter(
        _settings(),
        FakeKnowledgeBaseRepository((knowledge_base,)),
        FakeDocumentRepository((document,)),
        client=_client(handler),
    )

    await adapter.start_parse(
        ParseDocumentCommand(
            security=security,
            knowledge_base_id=knowledge_base.id,
            document_id=document.id,
            chunk_method="naive",
            parser_config={
                "html4excel": True,
                "chunk_token_num": 192,
                "delimiter": "\n",
                "overlapped_percent": 5,
            },
        )
    )

    assert [call[0] for call in calls] == ["PATCH", "POST"]
    assert calls[0][1] == ("/api/v1/datasets/engine-dataset-1/documents/engine-document-1")
    assert calls[0][2]["chunk_method"] == "naive"
    assert calls[0][2]["parser_config"]["chunk_token_num"] == 192
    assert "overlapped_percent" not in calls[0][2]["parser_config"]
    assert calls[0][2]["parser_config"]["ext"]["overlapped_percent"] == 5
    assert calls[1][1] == "/api/v1/datasets/engine-dataset-1/chunks"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "mime_type"),
    [
        pytest.param("fixture.png", "image/png", id="png"),
        pytest.param("fixture.jpg", "image/jpeg", id="jpg"),
        pytest.param("fixture.jpeg", "image/jpeg", id="jpeg"),
        pytest.param("fixture.webp", "image/webp", id="webp"),
    ],
)
async def test_start_parse_visual_preserves_picture_parser(
    security: SecurityContext,
    filename: str,
    mime_type: str,
) -> None:
    """视觉文档不 PATCH 固定的 picture parser，但仍继续创建解析任务。"""
    knowledge_base = _knowledge_base(security)
    document = _document(
        security,
        knowledge_base,
        filename=filename,
        mime_type=mime_type,
    )
    calls: list[tuple[str, str, dict[str, Any]]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"code": 0, "data": True})

    adapter = RagFlowAdapter(
        _settings(),
        FakeKnowledgeBaseRepository((knowledge_base,)),
        FakeDocumentRepository((document,)),
        client=_client(handler),
    )

    await adapter.start_parse(
        ParseDocumentCommand(
            security=security,
            knowledge_base_id=knowledge_base.id,
            document_id=document.id,
            chunk_method="picture",
            parser_config={
                "chunk_token_num": 192,
                "delimiter": "\n",
                "overlapped_percent": 8,
            },
        )
    )

    assert [call[0] for call in calls] == ["PATCH", "POST"]
    assert "chunk_method" not in calls[0][2]
    assert calls[0][2]["parser_config"]["chunk_token_num"] == 192
    assert calls[0][2]["parser_config"]["ext"]["overlapped_percent"] == 8
    assert calls[1][1] == "/api/v1/datasets/engine-dataset-1/chunks"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("run_state", "delete_existing", "expected_methods"),
    [
        pytest.param("0", False, ["PATCH", "GET", "POST"], id="not-started"),
        pytest.param("1", False, ["PATCH", "GET"], id="already-running"),
        pytest.param("3", True, ["PATCH", "POST"], id="explicit-reparse"),
    ],
)
async def test_start_parse_presentation_preserves_native_parser_and_avoids_duplicates(
    security: SecurityContext,
    run_state: str,
    delete_existing: bool,
    expected_methods: list[str],
) -> None:
    """覆盖 PPTX 未启动、已运行和用户明确重解析三种解析分支。

    RAGFlow v0.26.4 会拒绝给 PPT/PPTX PATCH ``chunk_method``，所以三个分支都只
    下发 ``visual-192`` 对应的 ``parser_config``。普通解析在文档已经运行时不重复
    POST；用户明确重解析时不做该拦截。
    """
    knowledge_base = _knowledge_base(security)
    document = _document(
        security,
        knowledge_base,
        filename="fixture.pptx",
        mime_type=(
            "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        ),
    )
    calls: list[tuple[str, str, dict[str, Any] | None]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body))
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "docs": [
                            {
                                "id": "engine-document-1",
                                "dataset_id": "engine-dataset-1",
                                "name": "fixture.pptx",
                                "run": run_state,
                                "progress": 0.25,
                                "chunk_count": 0,
                                "token_count": 0,
                            }
                        ]
                    },
                },
            )
        return httpx.Response(200, json={"code": 0, "data": True})

    adapter = RagFlowAdapter(
        _settings(),
        FakeKnowledgeBaseRepository((knowledge_base,)),
        FakeDocumentRepository((document,)),
        client=_client(handler),
    )
    await adapter.start_parse(
        ParseDocumentCommand(
            security=security,
            knowledge_base_id=knowledge_base.id,
            document_id=document.id,
            delete_existing=delete_existing,
            chunk_method="naive",
            parser_config={
                "chunk_token_num": 192,
                "delimiter": "\n",
                "overlapped_percent": 8,
            },
        )
    )

    assert [call[0] for call in calls] == expected_methods
    assert calls[0][2] is not None
    assert "chunk_method" not in calls[0][2]
    assert calls[0][2]["parser_config"]["chunk_token_num"] == 192
    assert calls[0][2]["parser_config"]["ext"]["overlapped_percent"] == 8


@pytest.mark.asyncio
async def test_version_chunk_availability_uses_official_scoped_patch(
    security: SecurityContext,
) -> None:
    """退役版本只按显式 Chunk ID 停用，不能发送含义危险的空集合或全量命令。"""
    knowledge_base = _knowledge_base(security)
    document = _document(security, knowledge_base)
    calls: list[tuple[str, str, dict[str, Any]]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        """记录官方 PATCH 地址与 JSON，并返回 RAGFlow 成功信封。"""
        calls.append((request.method, request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"code": 0, "data": True})

    adapter = RagFlowAdapter(
        _settings(),
        FakeKnowledgeBaseRepository((knowledge_base,)),
        FakeDocumentRepository((document,)),
        client=_client(handler),
    )

    await adapter.set_document_version_chunks_available(
        SetEngineVersionChunksAvailabilityCommand(
            security=security,
            knowledge_base_id=knowledge_base.id,
            document_id=document.id,
            engine_document_id="engine-version-2",
            chunk_ids=("chunk-a", "chunk-b"),
            available=False,
        )
    )

    assert calls == [
        (
            "PATCH",
            "/api/v1/datasets/engine-dataset-1/documents/engine-version-2/chunks",
            {"chunk_ids": ["chunk-a", "chunk-b"], "available": False},
        )
    ]


@pytest.mark.asyncio
async def test_version_document_availability_uses_official_status_endpoint(
    security: SecurityContext,
) -> None:
    """版本发布必须切换整份物理文档，使 UI 开关和全部 Chunk 状态保持一致。"""
    knowledge_base = _knowledge_base(security)
    document = _document(security, knowledge_base)
    calls: list[tuple[str, str, dict[str, Any]]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        """模拟 RAGFlow 文档开关，并记录停用、启用两种状态的精确请求体。"""
        body = json.loads(request.content)
        calls.append((request.method, request.url.path, body))
        engine_document_id = body["doc_ids"][0]
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {engine_document_id: {"status": str(body["status"])}},
            },
        )

    adapter = RagFlowAdapter(
        _settings(),
        FakeKnowledgeBaseRepository((knowledge_base,)),
        FakeDocumentRepository((document,)),
        client=_client(handler),
    )
    command_fields = {
        "security": security,
        "knowledge_base_id": knowledge_base.id,
        "document_id": document.id,
        "engine_document_id": "engine-version-2",
    }

    await adapter.set_document_version_available(
        SetEngineDocumentVersionAvailabilityCommand(
            **command_fields,
            available=False,
        )
    )
    await adapter.set_document_version_available(
        SetEngineDocumentVersionAvailabilityCommand(
            **command_fields,
            available=True,
        )
    )

    expected_path = "/api/v1/datasets/engine-dataset-1/documents/batch-update-status"
    assert calls == [
        (
            "POST",
            expected_path,
            {"doc_ids": ["engine-version-2"], "status": 0},
        ),
        (
            "POST",
            expected_path,
            {"doc_ids": ["engine-version-2"], "status": 1},
        ),
    ]


@pytest.mark.asyncio
async def test_create_dataset_uses_safe_name_and_required_configuration(
    security: SecurityContext,
) -> None:
    knowledge_base = _knowledge_base(security, mapped=False)
    captured: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers["Authorization"]
        captured["body"] = request.content
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "id": "engine-dataset-1",
                    "name": f"{str(knowledge_base.id)[:8]}-产品-文档",
                    "chunk_method": "naive",
                },
            },
        )

    adapter = RagFlowAdapter(
        _settings(),
        FakeKnowledgeBaseRepository((knowledge_base,)),
        FakeDocumentRepository(),
        client=_client(handler),
    )
    result = await adapter.create_dataset(
        CreateDatasetCommand(
            security=security,
            knowledge_base_id=knowledge_base.id,
            name=knowledge_base.name,
            description="synthetic",
            chunk_method="naive",
            parser_config={"chunk_token_num": 256, "overlapped_percent": 10},
            embedding_model="embedding@test",
        )
    )

    assert result.engine_id == "engine-dataset-1"
    assert captured["authorization"] == "Bearer test-key"
    body = captured["body"].decode()
    assert '"delete_all"' not in body
    assert '"permission":"me"' in body
    assert '"chunk_method":"naive"' in body
    # v0.26.4 的公开 ParserConfig 尚未声明重叠字段：标准副本负责 Schema 校验，
    # ext 内的完整副本由 RAGFlow 服务层展开，最终交给解析内核。
    payload = json.loads(body)
    assert payload["parser_config"]["chunk_token_num"] == 256
    assert "overlapped_percent" not in payload["parser_config"]
    assert payload["ext"]["parser_config"]["overlapped_percent"] == 10


@pytest.mark.asyncio
async def test_scope_rejection_happens_before_http_call(
    security: SecurityContext,
) -> None:
    knowledge_base = _knowledge_base(security)
    calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    adapter = RagFlowAdapter(
        _settings(),
        FakeKnowledgeBaseRepository((knowledge_base,)),
        FakeDocumentRepository(),
        client=_client(handler),
    )

    with pytest.raises(ResourceNotFoundError):
        await adapter.get_dataset(
            GetEngineDatasetQuery(
                security=SecurityContext.create(
                    subject_id="other",
                    tenant_id="other",
                    rag_scope_id="other",
                    token_id="other",
                    expires_at=datetime.now(UTC) + timedelta(minutes=5),
                ),
                knowledge_base_id=knowledge_base.id,
            )
        )
    assert calls == 0


@pytest.mark.asyncio
async def test_delete_dataset_never_requests_delete_all(
    security: SecurityContext,
) -> None:
    knowledge_base = _knowledge_base(security)

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.content == (b'{"ids":["engine-dataset-1"],"delete_all":false}')
        return httpx.Response(200, json={"code": 0, "data": {"success_count": 1}})

    adapter = RagFlowAdapter(
        _settings(),
        FakeKnowledgeBaseRepository((knowledge_base,)),
        FakeDocumentRepository(),
        client=_client(handler),
    )

    await adapter.delete_dataset(
        DeleteEngineDatasetCommand(
            security=security,
            knowledge_base_id=knowledge_base.id,
        )
    )


@pytest.mark.asyncio
async def test_upload_deletes_size_drift_and_retries_with_verified_content(
    security: SecurityContext,
) -> None:
    """远端多出 CRLF 时删除损坏副本，第二次回读完全一致后才返回成功。"""
    knowledge_base = _knowledge_base(security)
    document = _document(security, knowledge_base)

    captured: list[tuple[str, Any]] = []
    post_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        captured.append(
            (
                request.method,
                (json.loads(request.content) if request.method in {"PUT", "DELETE"} else None),
            )
        )
        if request.method == "GET":
            if request.url.path.endswith("/uploaded-document-valid"):
                return httpx.Response(200, content=b"fixture")
            return httpx.Response(
                200,
                json={"code": 0, "data": {"docs": [], "total": 0}},
            )
        if request.method == "PUT":
            return httpx.Response(200, json={"code": 0, "data": True})
        if request.method == "DELETE":
            return httpx.Response(200, json={"code": 0, "data": {"success_count": 1}})
        post_count += 1
        document_id = "uploaded-document-corrupt" if post_count == 1 else "uploaded-document-valid"
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": [
                    {
                        "id": document_id,
                        "dataset_id": "engine-dataset-1",
                        "name": "fixture.txt",
                        "run": "UNSTART",
                        # 第一次模拟 multipart 在 ZIP 正文前增加 CRLF；第二次恢复正常。
                        "size": 9 if post_count == 1 else 7,
                    }
                ],
            },
        )

    adapter = RagFlowAdapter(
        _settings(),
        FakeKnowledgeBaseRepository((knowledge_base,)),
        FakeDocumentRepository((document,)),
        client=_client(handler),
    )
    result = await adapter.upload_document(
        UploadDocumentCommand(
            security=security,
            knowledge_base_id=knowledge_base.id,
            document_id=document.id,
            filename="fixture.txt",
            content=BytesIO(b"fixture"),
            metadata={"language": "en"},
        )
    )

    assert result.engine_id == "uploaded-document-valid"
    assert result.size_bytes == 7
    assert captured == [
        ("GET", None),
        ("POST", None),
        ("DELETE", {"ids": ["uploaded-document-corrupt"]}),
        ("POST", None),
        ("GET", None),
        ("PUT", {"meta_fields": {"language": "en"}}),
    ]


@pytest.mark.asyncio
async def test_upload_recovers_existing_exact_document_without_duplicate_post(
    security: SecurityContext,
) -> None:
    knowledge_base = _knowledge_base(security)
    document = _document(security, knowledge_base)
    methods: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "GET" and request.url.path.endswith("/existing-document"):
            return httpx.Response(200, content=b"fixture")
        if request.method == "PUT":
            assert json.loads(request.content) == {"meta_fields": {"language": "en"}}
            return httpx.Response(200, json={"code": 0, "data": True})
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "docs": [
                        {
                            "id": "existing-document",
                            "dataset_id": "engine-dataset-1",
                            "name": "stable-fixture.txt",
                            "run": "UNSTART",
                            "size": 7,
                        }
                    ],
                    "total": 1,
                },
            },
        )

    adapter = RagFlowAdapter(
        _settings(),
        FakeKnowledgeBaseRepository((knowledge_base,)),
        FakeDocumentRepository((document,)),
        client=_client(handler),
    )
    result = await adapter.upload_document(
        UploadDocumentCommand(
            security=security,
            knowledge_base_id=knowledge_base.id,
            document_id=document.id,
            filename="stable-fixture.txt",
            content=BytesIO(b"fixture"),
            metadata={"language": "en"},
        )
    )

    assert result.engine_id == "existing-document"
    assert methods == ["GET", "GET", "PUT"]


@pytest.mark.asyncio
async def test_upload_recovery_prefers_exact_size_duplicate_name_without_post(
    security: SecurityContext,
) -> None:
    """失败重试复用 ``(1)`` 文档，避免 Office 上传继续制造 ``(2)`` 副本。"""
    knowledge_base = _knowledge_base(security)
    document = _document(security, knowledge_base)
    methods: list[str] = []
    deleted_ids: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "GET" and request.url.path.endswith("/second-exact-size"):
            return httpx.Response(200, content=b"fixture")
        if request.method == "PUT":
            return httpx.Response(200, json={"code": 0, "data": True})
        if request.method == "DELETE":
            deleted_ids.extend(json.loads(request.content)["ids"])
            return httpx.Response(200, json={"code": 0, "data": {"success_count": 1}})
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "docs": [
                        {
                            "id": "first-size-drift",
                            "dataset_id": "engine-dataset-1",
                            "name": "stable-fixture.txt",
                            "run": "UNSTART",
                            "size": 9,
                        },
                        {
                            "id": "second-exact-size",
                            "dataset_id": "engine-dataset-1",
                            "name": "stable-fixture(1).txt",
                            "run": "UNSTART",
                            "size": 7,
                        },
                    ],
                    "total": 2,
                },
            },
        )

    adapter = RagFlowAdapter(
        _settings(),
        FakeKnowledgeBaseRepository((knowledge_base,)),
        FakeDocumentRepository((document,)),
        client=_client(handler),
    )
    result = await adapter.upload_document(
        UploadDocumentCommand(
            security=security,
            knowledge_base_id=knowledge_base.id,
            document_id=document.id,
            filename="stable-fixture.txt",
            content=BytesIO(b"fixture"),
            metadata={"language": "en"},
        )
    )

    assert result.engine_id == "second-exact-size"
    assert result.name == "stable-fixture(1).txt"
    assert result.size_bytes == 7
    assert methods == ["GET", "GET", "DELETE", "PUT"]
    assert deleted_ids == ["first-size-drift"]


@pytest.mark.asyncio
async def test_upload_retries_when_remote_hash_differs_at_same_size(
    security: SecurityContext,
) -> None:
    """即使 RAGFlow 报告大小正确，回读摘要不同也不得把损坏文档送去解析。"""
    knowledge_base = _knowledge_base(security)
    document = _document(security, knowledge_base)
    methods: list[str] = []
    post_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_count
        methods.append(request.method)
        if request.method == "GET":
            if request.url.path.endswith("/same-size-corrupt"):
                return httpx.Response(200, content=b"corrupt")
            if request.url.path.endswith("/same-size-valid"):
                return httpx.Response(200, content=b"fixture")
            return httpx.Response(
                200,
                json={"code": 0, "data": {"docs": [], "total": 0}},
            )
        if request.method == "DELETE":
            assert json.loads(request.content) == {"ids": ["same-size-corrupt"]}
            return httpx.Response(200, json={"code": 0, "data": {"success_count": 1}})
        post_count += 1
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": [
                    {
                        "id": "same-size-corrupt" if post_count == 1 else "same-size-valid",
                        "dataset_id": "engine-dataset-1",
                        "name": "stable-fixture.txt",
                        "run": "UNSTART",
                        "size": 7,
                    }
                ],
            },
        )

    adapter = RagFlowAdapter(
        _settings(),
        FakeKnowledgeBaseRepository((knowledge_base,)),
        FakeDocumentRepository((document,)),
        client=_client(handler),
    )
    result = await adapter.upload_document(
        UploadDocumentCommand(
            security=security,
            knowledge_base_id=knowledge_base.id,
            document_id=document.id,
            filename="stable-fixture.txt",
            content=BytesIO(b"fixture"),
            metadata={},
        )
    )

    assert result.engine_id == "same-size-valid"
    assert methods == ["GET", "POST", "GET", "DELETE", "POST", "GET"]


@pytest.mark.asyncio
async def test_upload_still_rejects_unrelated_response_name(
    security: SecurityContext,
) -> None:
    """兼容大小漂移不能放宽身份边界，无关文件名仍必须报告协议无效。"""
    knowledge_base = _knowledge_base(security)
    document = _document(security, knowledge_base)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"code": 0, "data": {"docs": [], "total": 0}},
            )
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": [
                    {
                        "id": "unrelated-document",
                        "dataset_id": "engine-dataset-1",
                        "name": "another-file.txt",
                        "run": "UNSTART",
                        "size": 7,
                    }
                ],
            },
        )

    adapter = RagFlowAdapter(
        _settings(),
        FakeKnowledgeBaseRepository((knowledge_base,)),
        FakeDocumentRepository((document,)),
        client=_client(handler),
    )
    with pytest.raises(RagFlowError) as error:
        await adapter.upload_document(
            UploadDocumentCommand(
                security=security,
                knowledge_base_id=knowledge_base.id,
                document_id=document.id,
                filename="stable-fixture.txt",
                content=BytesIO(b"fixture"),
                metadata={},
            )
        )

    assert error.value.code == "RAG-RF-004"


@pytest.mark.asyncio
async def test_chunks_and_retrieval_map_back_to_stable_local_ids(
    security: SecurityContext,
) -> None:
    knowledge_base = _knowledge_base(security)
    document = _document(security, knowledge_base)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "chunks": [
                            {
                                "id": "chunk-1",
                                "content": "synthetic evidence",
                                "positions": [[1, 0, 1, 2, 3]],
                            }
                        ],
                        "total": 1,
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    "chunks": [
                        {
                            "id": "chunk-1",
                            "dataset_id": "engine-dataset-1",
                            "document_id": "engine-document-1",
                            "document_keyword": "fixture.txt",
                            "content": "synthetic evidence",
                            "similarity": 0.9,
                            "vector_similarity": 0.8,
                            "term_similarity": 0.7,
                            "positions": [{"page": 1, "bbox": [0, 1, 2, 3]}],
                        }
                    ],
                    "total": 1,
                },
            },
        )

    adapter = RagFlowAdapter(
        _settings(),
        FakeKnowledgeBaseRepository((knowledge_base,)),
        FakeDocumentRepository((document,)),
        client=_client(handler),
    )
    chunks = await adapter.list_chunks(
        ListEngineChunksQuery(
            security=security,
            knowledge_base_id=knowledge_base.id,
            document_id=document.id,
        )
    )
    retrieval = await adapter.retrieve(
        EngineRetrievalQuery(
            security=security,
            knowledge_base_ids=(knowledge_base.id,),
            document_ids=(document.id,),
            question="What is synthetic?",
        )
    )

    assert chunks.items[0].document_id == document.id
    assert chunks.items[0].positions[0].bbox == (0.0, 1.0, 2.0, 3.0)
    assert retrieval.candidates[0].knowledge_base_id == knowledge_base.id
    assert retrieval.candidates[0].document_id == document.id
    assert retrieval.candidates[0].engine_chunk_id == "chunk-1"
    assert retrieval.candidates[0].chunk_id != "chunk-1"
    # RAGFlow 返回的 document_keyword 可能带引擎内部 UUID 前缀；对外名称必须来自
    # 当前 Scope 已验证的本地 DocumentRecord，确保基础路线和侧车路线显示一致。
    assert retrieval.candidates[0].document_name == document.filename


@pytest.mark.asyncio
async def test_retrieval_translates_stable_metadata_filter_to_ragflow_protocol(
    security: SecurityContext,
) -> None:
    knowledge_base = _knowledge_base(security)
    captured: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"code": 0, "data": {"chunks": [], "total": 0}},
        )

    adapter = RagFlowAdapter(
        _settings(),
        FakeKnowledgeBaseRepository((knowledge_base,)),
        FakeDocumentRepository(),
        client=_client(handler),
    )
    public_operators = (
        "eq",
        "ne",
        "contains",
        "not_contains",
        "starts_with",
        "empty",
        "not_empty",
        "gt",
        "gte",
        "lt",
        "lte",
    )

    await adapter.retrieve(
        EngineRetrievalQuery(
            security=security,
            knowledge_base_ids=(knowledge_base.id,),
            question="metadata fixture",
            metadata_filter={
                "logic": "and",
                "conditions": [
                    {"field": f"field_{index}", "operator": operator, "value": "fixture"}
                    for index, operator in enumerate(public_operators)
                ],
            },
        )
    )

    assert captured["body"]["metadata_condition"] == {
        "logic": "and",
        "conditions": [
            {"name": "field_0", "comparison_operator": "is", "value": "fixture"},
            {"name": "field_1", "comparison_operator": "not is", "value": "fixture"},
            {"name": "field_2", "comparison_operator": "contains", "value": "fixture"},
            {
                "name": "field_3",
                "comparison_operator": "not contains",
                "value": "fixture",
            },
            {"name": "field_4", "comparison_operator": "start with", "value": "fixture"},
            {"name": "field_5", "comparison_operator": "empty", "value": "fixture"},
            {"name": "field_6", "comparison_operator": "not empty", "value": "fixture"},
            {"name": "field_7", "comparison_operator": ">", "value": "fixture"},
            {"name": "field_8", "comparison_operator": ">=", "value": "fixture"},
            {"name": "field_9", "comparison_operator": "<", "value": "fixture"},
            {"name": "field_10", "comparison_operator": "<=", "value": "fixture"},
        ],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "payload", "expected_code"),
    [
        (401, {"code": 0}, "RAG-RF-006"),
        (500, {"code": 0}, "RAG-RF-002"),
        (200, {"code": 1, "message": "raw downstream message"}, "RAG-RF-002"),
        (200, {"code": 0, "data": {}}, "RAG-RF-004"),
    ],
)
async def test_adapter_normalizes_downstream_failures(
    security: SecurityContext,
    status_code: int,
    payload: dict[str, Any],
    expected_code: str,
) -> None:
    knowledge_base = _knowledge_base(security)

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload)

    adapter = RagFlowAdapter(
        _settings(),
        FakeKnowledgeBaseRepository((knowledge_base,)),
        FakeDocumentRepository(),
        client=_client(handler),
    )

    with pytest.raises(RagFlowError) as captured:
        await adapter.get_dataset(
            GetEngineDatasetQuery(
                security=security,
                knowledge_base_id=knowledge_base.id,
            )
        )
    assert captured.value.code == expected_code
    assert "raw downstream message" not in captured.value.message


@pytest.mark.asyncio
async def test_health_accepts_unwrapped_official_response() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "ok",
                "db": "ok",
                "redis": "ok",
                "doc_engine": "ok",
                "storage": "ok",
            },
        )

    adapter = RagFlowAdapter(
        _settings(),
        FakeKnowledgeBaseRepository(()),
        FakeDocumentRepository(),
        client=_client(handler),
    )

    health = await adapter.health()

    assert health.status == "ok"
    assert health.dependencies["db"] == "ok"


@pytest.mark.asyncio
async def test_vision_pdf_resolves_model_for_dataset_and_document(
    security: SecurityContext,
) -> None:
    """视觉策略在建库与解析 PATCH 中均绑定当前模型，且保留原始 Profile。"""
    knowledge_base = _knowledge_base(security)
    document = _document(security, knowledge_base, filename="fixture.pdf")
    calls: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"code": 0, "data": {
            "id": "engine-dataset-1", "name": "synthetic", "chunk_method": "naive",
        }})

    config = {"layout_recognize": "${RAGFLOW_VISION_MODEL}", "task_page_size": 12}
    async with _client(handler) as client:
        adapter = RagFlowAdapter(
            _settings(), FakeKnowledgeBaseRepository((knowledge_base,)),
            FakeDocumentRepository((document,)), client=client,
        )
        await adapter.create_dataset(CreateDatasetCommand(
            security=security, knowledge_base_id=knowledge_base.id,
            name=knowledge_base.name, description="synthetic", chunk_method="naive",
            parser_config=config, embedding_model="embedding@test",
        ))
        await adapter.start_parse(ParseDocumentCommand(
            security=security, knowledge_base_id=knowledge_base.id,
            document_id=document.id, chunk_method="naive", parser_config=config,
        ))
    assert calls[0]["parser_config"]["layout_recognize"] == _settings().vision_model
    assert calls[0]["ext"]["parser_config"]["layout_recognize"] == _settings().vision_model
    assert calls[1]["parser_config"]["layout_recognize"] == _settings().vision_model
    assert calls[1]["parser_config"]["task_page_size"] == 12
    assert config["layout_recognize"] == "${RAGFLOW_VISION_MODEL}"


@pytest.mark.asyncio
async def test_mineru_image_upload_recovery_and_parse(security: SecurityContext) -> None:
    """图片按 PDF 上传并回读校验，重试复用副本；解析必须设置 naive 而非 picture。"""
    from email.parser import BytesParser
    from email.policy import default

    import pikepdf
    from PIL import Image

    knowledge_base = _knowledge_base(security)
    document = _document(security, knowledge_base, filename='fixture.png', mime_type='image/png')
    source = BytesIO()
    Image.new('RGB', (64, 32), 'white').save(source, format='PNG')
    original = source.getvalue()
    document.file_size_bytes = len(original)
    document.content_sha256 = sha256(original).hexdigest()
    document.parser_profile_ref = 'mineru-image@1'
    uploaded: list[bytes] = []
    patched: list[dict[str, Any]] = []

    def record() -> dict[str, Any]:
        return {'id': 'engine-document-1', 'dataset_id': 'engine-dataset-1',
                'name': 'fixture.png.pdf', 'run': 'UNSTART', 'size': len(uploaded[0])}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == 'GET' and request.url.path.endswith('/engine-document-1'):
            return httpx.Response(200, content=uploaded[0])
        if request.method == 'GET':
            assert request.url.params['keywords'] == 'fixture.png.pdf'
            return httpx.Response(200, json={'code': 0, 'data': {
                'docs': [record()] if uploaded else [], 'total': len(uploaded),
            }})
        if request.method == 'POST' and request.url.path.endswith('/documents'):
            message = BytesParser(policy=default).parsebytes(
                b'Content-Type: ' + request.headers['content-type'].encode() + b'\r\n\r\n'
                + request.content,
            )
            part = next(message.iter_parts())
            assert part.get_filename() == 'fixture.png.pdf'
            assert part.get_content_type() == 'application/pdf'
            uploaded.append(part.get_payload(decode=True))
            return httpx.Response(200, json={'code': 0, 'data': [record()]})
        if request.method == 'PATCH':
            patched.append(json.loads(request.content))
        return httpx.Response(200, json={'code': 0, 'data': True})

    async with _client(handler) as client:
        adapter = RagFlowAdapter(
            _settings(), FakeKnowledgeBaseRepository((knowledge_base,)),
            FakeDocumentRepository((document,)), client=client,
        )
        for _ in range(2):
            await adapter.upload_document(UploadDocumentCommand(
                security=security, knowledge_base_id=knowledge_base.id, document_id=document.id,
                filename='fixture.png', content=BytesIO(original),
                parser_profile_ref='mineru-image@1',
            ))
        await adapter.start_parse(ParseDocumentCommand(
            security=security, knowledge_base_id=knowledge_base.id, document_id=document.id,
            chunk_method='naive',
            parser_config={'layout_recognize': 'mineru-cloud@mineru-cloud@MinerU'},
        ))
    assert len(uploaded) == 1
    assert patched[0]['chunk_method'] == 'naive'
    assert patched[0]['parser_config']['layout_recognize'] == 'mineru-cloud@mineru-cloud@MinerU'
    with pikepdf.Pdf.open(BytesIO(uploaded[0])) as pdf:
        assert len(pdf.pages) == 1
        assert pdf.attachments['original-image'].get_file().read_bytes() == original
    assert document.filename == 'fixture.png'
    assert document.content_sha256 == sha256(original).hexdigest()
