"""基于本地/共享文件系统的安全上传暂存区。

文件以随机 UUID 命名，先写 ``.uploading``，完成大小、SHA-256、扩展名和图片/媒体
魔数校验后原子改为 ``.ready``。Worker 打开时会再次校验，防止落盘后被替换；
清理器只删除超时且未被活动任务引用的文件。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import unicodedata
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import BinaryIO
from uuid import UUID, uuid4

from rag_service.domain.errors import RagError
from rag_service.domain.ports import (
    CleanupResult,
    StagedFile,
    StagedFileMetadata,
)
from rag_service.settings import UploadSection

_DANGEROUS_SUFFIXES = frozenset(
    {
        ".app",
        ".bat",
        ".cmd",
        ".com",
        ".dmg",
        ".exe",
        ".jar",
        ".js",
        ".msi",
        ".php",
        ".pkg",
        ".ps1",
        ".scr",
        ".sh",
    }
)

_IMAGE_MEDIA_TYPES = {
    ".png": frozenset({"image/png"}),
    ".jpg": frozenset({"image/jpeg", "image/jpg"}),
    ".jpeg": frozenset({"image/jpeg", "image/jpg"}),
    ".webp": frozenset({"image/webp"}),
}
_AUDIO_MEDIA_TYPES = {
    ".mp3": frozenset({"audio/mpeg", "audio/mp3"}),
    ".wav": frozenset({"audio/wav", "audio/x-wav", "audio/wave"}),
    ".m4a": frozenset({"audio/mp4", "audio/x-m4a"}),
    ".aac": frozenset({"audio/aac", "audio/x-aac"}),
    ".flac": frozenset({"audio/flac", "audio/x-flac"}),
    ".ogg": frozenset({"audio/ogg", "application/ogg"}),
}
_VIDEO_MEDIA_TYPES = {
    ".mp4": frozenset({"video/mp4"}),
    ".mov": frozenset({"video/quicktime"}),
    ".mkv": frozenset({"video/x-matroska"}),
    ".webm": frozenset({"video/webm"}),
    ".avi": frozenset({"video/x-msvideo", "video/avi"}),
}


def _valid_image_signature(suffix: str, prefix: bytes) -> bool:
    """用文件头魔数确认常见图片扩展名与真实内容类型一致。"""
    if suffix == ".png":
        return prefix.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix in {".jpg", ".jpeg"}:
        return prefix.startswith(b"\xff\xd8\xff")
    if suffix == ".webp":
        return (
            len(prefix) >= 12
            and prefix.startswith(b"RIFF")
            and prefix[8:12] == b"WEBP"
        )
    return True


def _valid_media_signature(suffix: str, prefix: bytes) -> bool:
    """用稳定容器魔数拒绝把任意文件伪装成音频或视频后交给 FFmpeg。"""
    if suffix == ".mp3":
        return prefix.startswith(b"ID3") or (
            len(prefix) >= 2 and prefix[0] == 0xFF and prefix[1] & 0xE0 == 0xE0
        )
    if suffix == ".wav":
        return len(prefix) >= 12 and prefix.startswith(b"RIFF") and prefix[8:12] == b"WAVE"
    if suffix in {".m4a", ".mp4", ".mov"}:
        return len(prefix) >= 12 and prefix[4:8] == b"ftyp"
    if suffix == ".aac":
        return len(prefix) >= 2 and prefix[0] == 0xFF and prefix[1] & 0xF6 == 0xF0
    if suffix == ".flac":
        return prefix.startswith(b"fLaC")
    if suffix == ".ogg":
        return prefix.startswith(b"OggS")
    if suffix in {".mkv", ".webm"}:
        return prefix.startswith(b"\x1aE\xdf\xa3")
    if suffix == ".avi":
        return len(prefix) >= 12 and prefix.startswith(b"RIFF") and prefix[8:12] == b"AVI "
    return True


def _upload_error(
    code: str,
    message: str,
    *,
    http_status: int,
    retryable: bool = False,
) -> RagError:
    """构造带稳定错误码、HTTP 状态和可重试属性的上传错误。"""
    return RagError(
        code=code,
        message=message,
        http_status=http_status,
        retryable=retryable,
    )


class FileSystemUploadStaging:
    """实现流式写入、完整性复验、删除和过期清理的暂存端口。"""
    def __init__(self, settings: UploadSection) -> None:
        """保存目录/容量/扩展名配置，并初始化进程内容量检查锁。"""
        self._settings = settings
        self._root = settings.staging_dir.resolve()

    async def initialize(self) -> None:
        """创建暂存目录并拒绝符号链接目录，避免写入被重定向到意外位置。"""
        await asyncio.to_thread(self._root.mkdir, parents=True, exist_ok=True)
        if not await asyncio.to_thread(os.access, self._root, os.W_OK):
            raise _upload_error(
                "RAG-DOC-010",
                "上传暂存卷不可写",
                http_status=503,
                retryable=True,
            )

    def _paths(self, staged_file_id: UUID) -> tuple[Path, Path]:
        """由 UUID 计算同目录的临时路径和就绪路径。"""
        uploading = self._root / (
            f"{staged_file_id}{self._settings.uploading_suffix}"
        )
        ready = self._root / f"{staged_file_id}{self._settings.ready_suffix}"
        return uploading, ready

    def _safe_filename(self, original: str) -> str:
        """去除目录成分并校验长度、扩展名、双扩展和控制字符，生成安全展示名。"""
        normalized = unicodedata.normalize("NFKC", original)
        if (
            not normalized
            or "\x00" in normalized
            or Path(normalized).name != normalized
            or normalized in {".", ".."}
        ):
            raise _upload_error(
                "RAG-DOC-003",
                "文件名无效",
                http_status=400,
            )
        cleaned = "".join(
            character
            for character in normalized
            if unicodedata.category(character) not in {"Cc", "Cf"}
        ).strip()
        if not cleaned or len(cleaned.encode("utf-8")) > 255:
            raise _upload_error(
                "RAG-DOC-003",
                "文件名无效",
                http_status=400,
            )
        suffixes = [suffix.lower() for suffix in Path(cleaned).suffixes]
        if not suffixes or suffixes[-1] not in self._settings.allowed_extensions:
            raise _upload_error(
                "RAG-DOC-002",
                "文件类型不支持",
                http_status=415,
            )
        if self._settings.reject_double_extensions and any(
            suffix in _DANGEROUS_SUFFIXES for suffix in suffixes[:-1]
        ):
            raise _upload_error(
                "RAG-DOC-003",
                "文件名无效",
                http_status=400,
            )
        return cleaned

    async def _current_bytes(self) -> int:
        """在线程中统计暂存目录普通文件总字节数。"""
        def calculate() -> int:
            total = 0
            for path in self._root.iterdir():
                if path.is_file() and not path.is_symlink():
                    total += path.stat().st_size
            return total

        return await asyncio.to_thread(calculate)

    async def save(
        self,
        stream: AsyncIterator[bytes],
        metadata: StagedFileMetadata,
    ) -> StagedFile:
        """流式写文件并计算摘要，全部校验通过后原子发布为 ready。

        进程内锁把“检查容量 + 写入”串行化，避免并发上传都看到足够空间后共同超限；
        任何异常都会尽力删除半成品，客户端永远拿不到 ``.uploading`` 文件。
        """
        await self.initialize()
        safe_filename = self._safe_filename(metadata.original_filename)
        if (
            metadata.expected_size is not None
            and metadata.expected_size > self._settings.max_file_bytes
        ):
            raise _upload_error(
                "RAG-COMMON-006",
                "文件过大",
                http_status=413,
            )
        current_bytes = await self._current_bytes()
        if current_bytes >= self._settings.max_staging_bytes:
            raise _upload_error(
                "RAG-DOC-010",
                "上传暂存卷容量不足",
                http_status=503,
                retryable=True,
            )

        staged_file_id = uuid4()
        uploading_path, ready_path = self._paths(staged_file_id)
        digest = hashlib.sha256()
        size_bytes = 0
        signature_prefix = bytearray()
        handle: BinaryIO | None = None
        try:
            handle = await asyncio.to_thread(uploading_path.open, "xb")
            async for chunk in stream:
                if not isinstance(chunk, bytes):
                    raise _upload_error(
                        "RAG-DOC-003",
                        "上传内容无效",
                        http_status=400,
                    )
                if not chunk:
                    continue
                if len(signature_prefix) < 16:
                    signature_prefix.extend(chunk[: 16 - len(signature_prefix)])
                size_bytes += len(chunk)
                if size_bytes > self._settings.max_file_bytes:
                    raise _upload_error(
                        "RAG-COMMON-006",
                        "文件过大",
                        http_status=413,
                    )
                if current_bytes + size_bytes > self._settings.max_staging_bytes:
                    raise _upload_error(
                        "RAG-DOC-010",
                        "上传暂存卷容量不足",
                        http_status=503,
                        retryable=True,
                    )
                digest.update(chunk)
                await asyncio.to_thread(handle.write, chunk)
            await asyncio.to_thread(handle.flush)
            await asyncio.to_thread(os.fsync, handle.fileno())
            await asyncio.to_thread(handle.close)
            handle = None
            checksum = digest.hexdigest()
            if size_bytes == 0:
                raise _upload_error(
                    "RAG-DOC-003",
                    "文件为空",
                    http_status=400,
                )
            suffix = Path(safe_filename).suffix.lower()
            if suffix in _IMAGE_MEDIA_TYPES:
                media_type = (metadata.media_type or "").lower().split(";", 1)[0].strip()
                if media_type not in {"", "application/octet-stream"} and media_type not in (
                    _IMAGE_MEDIA_TYPES[suffix]
                ):
                    raise _upload_error(
                        "RAG-DOC-003",
                        "图片 MIME 类型与文件扩展名不一致",
                        http_status=415,
                    )
                if not _valid_image_signature(suffix, bytes(signature_prefix)):
                    raise _upload_error(
                        "RAG-DOC-003",
                        "图片内容与文件扩展名不一致",
                        http_status=415,
                    )
            media_types = _AUDIO_MEDIA_TYPES.get(suffix) or _VIDEO_MEDIA_TYPES.get(suffix)
            if media_types is not None:
                media_type = (metadata.media_type or "").lower().split(";", 1)[0].strip()
                if media_type not in {"", "application/octet-stream"} and media_type not in (
                    media_types
                ):
                    raise _upload_error(
                        "RAG-DOC-003",
                        "音视频 MIME 类型与文件扩展名不一致",
                        http_status=415,
                    )
                if not _valid_media_signature(suffix, bytes(signature_prefix)):
                    raise _upload_error(
                        "RAG-DOC-003",
                        "音视频内容与文件扩展名不一致",
                        http_status=415,
                    )
            if (
                metadata.expected_size is not None
                and metadata.expected_size != size_bytes
            ):
                raise _upload_error(
                    "RAG-DOC-011",
                    "暂存文件大小不一致",
                    http_status=409,
                    retryable=True,
                )
            if (
                metadata.expected_sha256 is not None
                and metadata.expected_sha256.lower() != checksum
            ):
                raise _upload_error(
                    "RAG-DOC-011",
                    "暂存文件校验值不一致",
                    http_status=409,
                    retryable=True,
                )
            await asyncio.to_thread(os.replace, uploading_path, ready_path)
            return StagedFile(
                staged_file_id=staged_file_id,
                safe_filename=safe_filename,
                size_bytes=size_bytes,
                content_sha256=checksum,
                ready_path=ready_path,
            )
        except BaseException:
            if handle is not None:
                await asyncio.to_thread(handle.close)
            await asyncio.to_thread(uploading_path.unlink, missing_ok=True)
            raise

    @asynccontextmanager
    async def open(
        self,
        staged_file_id: UUID,
        *,
        expected_size: int,
        expected_sha256: str,
    ) -> AsyncIterator[BinaryIO]:
        """打开 ready 文件，复核类型、大小和摘要，并在上下文结束时关闭句柄。"""
        _, ready_path = self._paths(staged_file_id)
        if (
            not await asyncio.to_thread(ready_path.is_file)
            or await asyncio.to_thread(ready_path.is_symlink)
        ):
            raise _upload_error(
                "RAG-DOC-011",
                "暂存文件缺失或未就绪",
                http_status=409,
                retryable=True,
            )

        def verify_and_open() -> BinaryIO:
            handle = ready_path.open("rb")
            digest = hashlib.sha256()
            size = 0
            while chunk := handle.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
            handle.seek(0)
            if size != expected_size or digest.hexdigest() != expected_sha256.lower():
                handle.close()
                raise _upload_error(
                    "RAG-DOC-011",
                    "暂存文件大小或校验值不一致",
                    http_status=409,
                    retryable=True,
                )
            return handle

        handle = await asyncio.to_thread(verify_and_open)
        try:
            yield handle
        finally:
            await asyncio.to_thread(handle.close)

    async def delete(self, staged_file_id: UUID) -> None:
        """幂等删除指定 UUID 的临时和就绪文件。"""
        uploading_path, ready_path = self._paths(staged_file_id)
        await asyncio.to_thread(uploading_path.unlink, missing_ok=True)
        await asyncio.to_thread(ready_path.unlink, missing_ok=True)

    async def cleanup_expired(
        self,
        now: datetime,
        limit: int,
        active_staged_file_ids: frozenset[UUID],
    ) -> CleanupResult:
        """按年龄和批量上限清理孤立临时文件及未被活动 Job 引用的 ready 文件。"""
        await self.initialize()
        cutoff = now - timedelta(seconds=self._settings.retain_failed_seconds)

        def cleanup() -> CleanupResult:
            removed_uploading = 0
            removed_ready = 0
            bytes_reclaimed = 0
            removed = 0
            for path in sorted(self._root.iterdir(), key=lambda item: item.name):
                if removed >= limit or not path.is_file() or path.is_symlink():
                    continue
                suffix = path.suffix
                if suffix not in {
                    self._settings.uploading_suffix,
                    self._settings.ready_suffix,
                }:
                    continue
                try:
                    staged_file_id = UUID(path.stem)
                except ValueError:
                    continue
                if staged_file_id in active_staged_file_ids:
                    continue
                stat = path.stat()
                modified = datetime.fromtimestamp(stat.st_mtime, tz=now.tzinfo)
                if modified >= cutoff:
                    continue
                path.unlink()
                removed += 1
                bytes_reclaimed += stat.st_size
                if suffix == self._settings.uploading_suffix:
                    removed_uploading += 1
                else:
                    removed_ready += 1
            return CleanupResult(
                removed_uploading=removed_uploading,
                removed_ready=removed_ready,
                bytes_reclaimed=bytes_reclaimed,
            )

        return await asyncio.to_thread(cleanup)
