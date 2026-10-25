"""文件暂存测试：流式摘要、原子发布、路径/类型防护、篡改检测和安全清理。"""

from __future__ import annotations

import hashlib
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from rag_service.domain.errors import RagError
from rag_service.domain.ports import StagedFileMetadata
from rag_service.infrastructure.staging.filesystem import FileSystemUploadStaging
from rag_service.settings import UploadSection


async def _stream(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


def _settings(tmp_path: Path) -> UploadSection:
    """允许文档、图片和媒体扩展名，供安全暂存的正反用例共用。"""
    return UploadSection(
        staging_dir=tmp_path,
        max_file_bytes=32,
        max_staging_bytes=64,
        retain_failed_seconds=60,
        allowed_extensions=(
            ".txt",
            ".pdf",
            ".json",
            ".jsonl",
            ".ldjson",
            ".png",
            ".jpg",
            ".jpeg",
            ".webp",
            ".mp3",
            ".wav",
            ".mp4",
            ".webm",
        ),
    )


def _matching(root: Path, pattern: str) -> list[Path]:
    return list(root.glob(pattern))


@pytest.mark.asyncio
async def test_staging_streams_hashes_and_atomically_publishes(tmp_path: Path) -> None:
    staging = FileSystemUploadStaging(_settings(tmp_path))
    expected = hashlib.sha256(b"synthetic").hexdigest()

    staged = await staging.save(
        _stream(b"syn", b"thetic"),
        StagedFileMetadata(
            original_filename="fixture.txt",
            expected_size=9,
            expected_sha256=expected,
        ),
    )

    assert staged.safe_filename == "fixture.txt"
    assert staged.size_bytes == 9
    assert staged.content_sha256 == expected
    assert staged.ready_path.name == f"{staged.staged_file_id}.ready"
    assert not _matching(tmp_path, "*.uploading")
    async with staging.open(
        staged.staged_file_id,
        expected_size=9,
        expected_sha256=expected,
    ) as handle:
        assert handle.read() == b"synthetic"
    await staging.delete(staged.staged_file_id)
    assert not staged.ready_path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "media_type", "content"),
    [
        ("fixture.json", "application/json", b'{"openapi":"3.1.0"}'),
        ("fixture.jsonl", "application/x-ndjson", b'{"id":1}\n{"id":2}\n'),
        ("fixture.ldjson", "application/x-ndjson", b'{"id":1}\n{"id":2}\n'),
    ],
)
async def test_staging_accepts_supported_json_extensions(
    tmp_path: Path,
    filename: str,
    media_type: str,
    content: bytes,
) -> None:
    """JSON 文件族通过扩展名白名单进入 RAGFlow 的结构化 Parser。"""
    staging = FileSystemUploadStaging(_settings(tmp_path))

    staged = await staging.save(
        _stream(content),
        StagedFileMetadata(original_filename=filename, media_type=media_type),
    )

    assert staged.safe_filename == filename
    await staging.delete(staged.staged_file_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "content", "expected_code"),
    [
        ("../fixture.txt", b"fixture", "RAG-DOC-003"),
        ("payload.exe.pdf", b"fixture", "RAG-DOC-003"),
        ("fixture.exe", b"fixture", "RAG-DOC-002"),
        ("fixture.txt", b"", "RAG-DOC-003"),
        ("fixture.txt", b"x" * 33, "RAG-COMMON-006"),
    ],
)
async def test_staging_rejects_unsafe_or_invalid_uploads(
    tmp_path: Path,
    filename: str,
    content: bytes,
    expected_code: str,
) -> None:
    staging = FileSystemUploadStaging(_settings(tmp_path))

    with pytest.raises(RagError) as captured:
        await staging.save(
            _stream(content),
            StagedFileMetadata(original_filename=filename),
        )

    assert captured.value.code == expected_code
    assert not _matching(tmp_path, "*.uploading")
    assert not _matching(tmp_path, "*.ready")


@pytest.mark.asyncio
async def test_open_detects_tampering(tmp_path: Path) -> None:
    staging = FileSystemUploadStaging(_settings(tmp_path))
    staged = await staging.save(
        _stream(b"fixture"),
        StagedFileMetadata(original_filename="fixture.txt"),
    )
    staged.ready_path.write_bytes(b"tampered")

    with pytest.raises(RagError) as captured:
        async with staging.open(
            staged.staged_file_id,
            expected_size=staged.size_bytes,
            expected_sha256=staged.content_sha256,
        ):
            pass
    assert captured.value.code == "RAG-DOC-011"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "media_type", "content"),
    [
        ("fixture.png", "image/png", b"\x89PNG\r\n\x1a\nsynthetic"),
        ("fixture.jpg", "image/jpeg", b"\xff\xd8\xffsynthetic"),
        ("fixture.jpeg", "application/octet-stream", b"\xff\xd8\xffsynthetic"),
        ("fixture.webp", "image/webp", b"RIFF\x04\x00\x00\x00WEBPsynthetic"),
    ],
)
async def test_staging_accepts_supported_image_signatures(
    tmp_path: Path,
    filename: str,
    media_type: str,
    content: bytes,
) -> None:
    staging = FileSystemUploadStaging(_settings(tmp_path))

    staged = await staging.save(
        _stream(content),
        StagedFileMetadata(
            original_filename=filename,
            media_type=media_type,
        ),
    )

    assert staged.safe_filename == filename
    await staging.delete(staged.staged_file_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "media_type", "content"),
    [
        ("renamed.png", "image/png", b"not-a-png"),
        ("renamed.jpg", "image/jpeg", b"\x89PNG\r\n\x1a\nnot-a-jpeg"),
        ("wrong-type.webp", "image/png", b"RIFF\x04\x00\x00\x00WEBP"),
    ],
)
async def test_staging_rejects_spoofed_or_mismatched_images(
    tmp_path: Path,
    filename: str,
    media_type: str,
    content: bytes,
) -> None:
    staging = FileSystemUploadStaging(_settings(tmp_path))

    with pytest.raises(RagError) as captured:
        await staging.save(
            _stream(content),
            StagedFileMetadata(
                original_filename=filename,
                media_type=media_type,
            ),
        )

    assert captured.value.code == "RAG-DOC-003"
    assert not _matching(tmp_path, "*.uploading")
    assert not _matching(tmp_path, "*.ready")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "media_type", "content"),
    [
        ("fixture.mp3", "audio/mpeg", b"ID3synthetic"),
        ("fixture.wav", "audio/wav", b"RIFF\x04\x00\x00\x00WAVEsynthetic"),
        ("fixture.mp4", "video/mp4", b"\x00\x00\x00\x18ftypisomsynthetic"),
        ("fixture.webm", "video/webm", b"\x1aE\xdf\xa3synthetic"),
    ],
)
async def test_staging_accepts_supported_media_signatures(
    tmp_path: Path,
    filename: str,
    media_type: str,
    content: bytes,
) -> None:
    """受支持媒体只有 MIME 和容器魔数同时匹配时才能进入 Worker。"""
    staging = FileSystemUploadStaging(_settings(tmp_path))

    staged = await staging.save(
        _stream(content),
        StagedFileMetadata(original_filename=filename, media_type=media_type),
    )

    assert staged.safe_filename == filename
    await staging.delete(staged.staged_file_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "media_type", "content"),
    [
        ("renamed.mp3", "audio/mpeg", b"not-an-mp3"),
        ("wrong-type.wav", "video/mp4", b"RIFF\x04\x00\x00\x00WAVEfake"),
        ("renamed.mp4", "video/mp4", b"not-an-mp4"),
    ],
)
async def test_staging_rejects_spoofed_or_mismatched_media(
    tmp_path: Path,
    filename: str,
    media_type: str,
    content: bytes,
) -> None:
    """扩展名伪装或 MIME 不匹配的媒体在调用 FFmpeg 前就会被拒绝。"""
    staging = FileSystemUploadStaging(_settings(tmp_path))

    with pytest.raises(RagError) as captured:
        await staging.save(
            _stream(content),
            StagedFileMetadata(original_filename=filename, media_type=media_type),
        )

    assert captured.value.code == "RAG-DOC-003"
    assert not _matching(tmp_path, "*.ready")


@pytest.mark.asyncio
async def test_cleanup_preserves_active_files_and_removes_expired(
    tmp_path: Path,
) -> None:
    staging = FileSystemUploadStaging(_settings(tmp_path))
    expired = await staging.save(
        _stream(b"expired"),
        StagedFileMetadata(original_filename="expired.txt"),
    )
    active = await staging.save(
        _stream(b"active"),
        StagedFileMetadata(original_filename="active.txt"),
    )
    old_timestamp = (datetime.now(UTC) - timedelta(minutes=5)).timestamp()
    os.utime(expired.ready_path, (old_timestamp, old_timestamp))
    os.utime(active.ready_path, (old_timestamp, old_timestamp))

    result = await staging.cleanup_expired(
        datetime.now(UTC),
        limit=10,
        active_staged_file_ids=frozenset({active.staged_file_id}),
    )

    assert result.removed_ready == 1
    assert result.bytes_reclaimed == len(b"expired")
    assert not expired.ready_path.exists()
    assert active.ready_path.exists()
