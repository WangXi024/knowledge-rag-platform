"""基于 FFmpeg、OpenAI-compatible ASR 和 Vision 的音视频预处理器。

RAGFlow v0.26.4 继续只负责派生 Markdown 的切块、Embedding 与检索；本适配器不修改
RAGFlow 源码，也不把本地路径或媒体正文写入日志。长媒体先被切成带少量重叠的
16 kHz 单声道 WAV，以满足当前项目 ASR 协议的单请求五分钟/10 MB 边界。视频另外
按覆盖完整时间轴的有限时间点抽取 JPEG，交给独立媒体视觉模型描述屏幕文字和关键
画面。文档图片仍由 RAGFlow 使用专用 OCR 模型处理，两项职责不会共享模型名。
"""

from __future__ import annotations

import asyncio
import base64
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, BinaryIO

import httpx

from rag_service.domain.errors import RagError
from rag_service.domain.ports import PreparedMediaDocument
from rag_service.settings import MediaSection, VisionSection

_AUDIO_EXTENSIONS = frozenset({".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"})
_VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".mkv", ".webm", ".avi"})
_RETRY_STATUSES = frozenset({429, 502, 503, 504})
_MAX_ASR_SEGMENT_BYTES = 10 * 1024 * 1024


def _files_within_size_limit(paths: tuple[Path, ...], limit: int) -> bool:
    """在线程中读取文件元信息，确认每个 ASR 分段都满足协议字节上限。"""
    return all(path.stat().st_size <= limit for path in paths)


def _media_error(message: str, *, retryable: bool = False) -> RagError:
    """把工具、协议和无内容错误统一为不会泄漏媒体正文的稳定领域错误。"""
    return RagError(
        "RAG-DOC-013",
        message,
        http_status=503 if retryable else 422,
        retryable=retryable,
    )


def _timestamp(seconds: float) -> str:
    """把秒数转换为固定的 HH:MM:SS，便于回答引用定位回媒体时间轴。"""
    total = max(0, round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, second = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{second:02d}"


class OpenAICompatibleMediaProcessor:
    """将音视频可靠地转换为可追溯 Markdown 的生产适配器。"""

    def __init__(
        self,
        settings: MediaSection,
        media_vision: VisionSection,
        *,
        asr_client: httpx.AsyncClient | None = None,
        vision_client: httpx.AsyncClient | None = None,
    ) -> None:
        """保存强类型配置，并为 ASR/视觉创建相互独立的有限超时客户端。"""
        if not settings.enabled:
            raise ValueError("media processor requires enabled settings")
        self._settings = settings
        self._vision = media_vision
        self._owns_asr_client = asr_client is None
        self._owns_vision_client = vision_client is None
        self._asr_client = asr_client or httpx.AsyncClient(
            base_url=settings.asr_base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {settings.asr_api_key}"},
            timeout=httpx.Timeout(
                connect=settings.connect_timeout_seconds,
                read=settings.read_timeout_seconds,
                write=settings.read_timeout_seconds,
                pool=settings.connect_timeout_seconds,
            ),
            trust_env=False,
        )
        self._vision_client = vision_client or httpx.AsyncClient(
            base_url=media_vision.base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {media_vision.api_key}"},
            timeout=httpx.Timeout(
                connect=media_vision.connect_timeout_seconds,
                read=media_vision.read_timeout_seconds,
                write=media_vision.read_timeout_seconds,
                pool=media_vision.connect_timeout_seconds,
            ),
            trust_env=False,
        )
        self._semaphore = asyncio.Semaphore(settings.max_concurrency)

    def supports(self, filename: str) -> bool:
        """只接受配置契约明确覆盖的常见音频和视频容器。"""
        return Path(filename).suffix.lower() in _AUDIO_EXTENSIONS | _VIDEO_EXTENSIONS

    async def close(self) -> None:
        """仅关闭由本适配器自己创建的 HTTP 客户端。"""
        if self._owns_asr_client:
            await self._asr_client.aclose()
        if self._owns_vision_client:
            await self._vision_client.aclose()

    async def prepare(
        self,
        content: BinaryIO,
        *,
        filename: str,
        mime_type: str,
    ) -> PreparedMediaDocument:
        """分段转写媒体并返回带时间戳 Markdown；取消信号会终止子进程。

        原始文件只存在于每个任务独占的系统临时目录中。无论成功、失败还是取消，
        ``TemporaryDirectory`` 都会回收源文件、WAV 和 JPEG，不进入 Docker 数据卷。
        """
        del mime_type  # 文件真实性由上传暂存魔数与 FFmpeg 双重确认，而非信任 MIME。
        suffix = Path(filename).suffix.lower()
        if suffix not in _AUDIO_EXTENSIONS | _VIDEO_EXTENSIONS:
            raise _media_error("音视频文件类型不支持")

        with tempfile.TemporaryDirectory(prefix="lazytt-media-") as directory:
            workdir = Path(directory)
            source_path = workdir / f"source{suffix}"
            await asyncio.to_thread(self._copy_source, content, source_path)
            duration = await self._probe_duration(source_path)
            audio_segments = await self._extract_audio_segments(source_path, workdir, duration)
            transcripts = await self._transcribe_segments(audio_segments)
            frame_descriptions: list[tuple[float, str]] = []
            if suffix in _VIDEO_EXTENSIONS:
                frames = await self._extract_video_frames(source_path, workdir, duration)
                frame_descriptions = await self._describe_frames(frames)

        if not transcripts and not frame_descriptions:
            raise _media_error("音视频中没有可索引的语音或画面文字")
        markdown = self._markdown(filename, transcripts, frame_descriptions)
        safe_stem = Path(filename).stem[:120] or "media"
        media_type = "video" if suffix in _VIDEO_EXTENSIONS else "audio"
        return PreparedMediaDocument(
            filename=f"{safe_stem}.media-transcript.md",
            content=markdown.encode("utf-8"),
            mime_type="text/markdown",
            metadata={
                "media_source_type": media_type,
                "media_duration_seconds": round(duration, 3),
                "media_asr_model": self._settings.asr_model,
                "media_vision_model": self._vision.model if frame_descriptions else "",
                "media_segment_count": len(transcripts),
                "media_frame_count": len(frame_descriptions),
                "media_timeline_available": True,
                "lazytt_media_processing_version": 2,
                "lazytt_media_processing_status": "completed",
            },
        )

    @staticmethod
    def _copy_source(content: BinaryIO, destination: Path) -> None:
        """从已由暂存层复验的句柄复制媒体，不一次性把大文件读入内存。"""
        content.seek(0)
        with destination.open("xb") as target:
            shutil.copyfileobj(content, target, length=1024 * 1024)

    async def _probe_duration(self, source: Path) -> float:
        """使用 ffprobe 读取容器时长，并拒绝缺失、负数和非 JSON 输出。"""
        output = await self._run_tool(
            self._settings.ffprobe_binary,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(source),
        )
        try:
            value = json.loads(output.decode("utf-8"))
            duration = float(value["format"]["duration"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise _media_error("无法读取音视频时长") from exc
        if not 0 < duration <= 24 * 60 * 60:
            raise _media_error("音视频时长无效或超过 24 小时上限")
        return duration

    async def _extract_audio_segments(
        self,
        source: Path,
        workdir: Path,
        duration: float,
    ) -> tuple[tuple[Path, float, float], ...]:
        """抽取带重叠的 16 kHz 单声道 WAV；无音轨视频允许继续处理画面。"""
        window = self._settings.segment_seconds
        step = window - self._settings.segment_overlap_seconds
        segments: list[tuple[Path, float, float]] = []
        start = 0.0
        while start < duration:
            end = min(duration, start + window)
            path = workdir / f"audio-{len(segments):05d}.wav"
            try:
                await self._run_tool(
                    self._settings.ffmpeg_binary,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-ss",
                    f"{start:.3f}",
                    "-i",
                    str(source),
                    "-t",
                    f"{end - start:.3f}",
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    str(path),
                )
            except RagError:
                if source.suffix.lower() in _VIDEO_EXTENSIONS:
                    break
                raise
            segments.append((path, start, end))
            if end >= duration:
                break
            start += step
        within_limit = await asyncio.to_thread(
            _files_within_size_limit,
            tuple(path for path, _, _ in segments),
            _MAX_ASR_SEGMENT_BYTES,
        )
        if not within_limit:
            raise _media_error("ASR 音频分段超过 10 MB 协议上限")
        return tuple(segments)

    def _video_frame_timestamps(self, duration: float) -> tuple[float, ...]:
        """在帧数上限内覆盖首尾；短视频使用配置的细粒度基础间隔。"""
        final = max(0.0, duration - min(0.5, duration / 2))
        if self._settings.max_video_frames == 1:
            return (final,)
        minimum_interval = self._settings.frame_interval_seconds
        coverage_interval = int(final / (self._settings.max_video_frames - 1)) + 1
        interval = max(minimum_interval, coverage_interval)
        timestamps = [float(second) for second in range(0, int(final) + 1, interval)]
        if not timestamps or final - timestamps[-1] >= 1:
            timestamps.append(final)
        else:
            timestamps[-1] = final
        return tuple(timestamps[: self._settings.max_video_frames])

    async def _extract_video_frames(
        self,
        source: Path,
        workdir: Path,
        duration: float,
    ) -> tuple[tuple[Path, float], ...]:
        """按覆盖完整时间轴的时间点抽取 JPEG，并严格限制视觉调用数量。"""
        frames: list[tuple[Path, float]] = []
        for index, second in enumerate(self._video_frame_timestamps(duration)):
            path = workdir / f"frame-{index:05d}.jpg"
            await self._run_tool(
                self._settings.ffmpeg_binary,
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{second:.3f}",
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-q:v",
                "3",
                str(path),
            )
            frames.append((path, second))
        return tuple(frames)

    async def _transcribe_segments(
        self,
        segments: tuple[tuple[Path, float, float], ...],
    ) -> list[tuple[float, float, str]]:
        """逐段调用 ASR，并用原媒体时间轴记录段落起止时间。"""
        result: list[tuple[float, float, str]] = []
        for path, start, end in segments:
            encoded = base64.b64encode(await asyncio.to_thread(path.read_bytes)).decode("ascii")
            payload = {
                "model": self._settings.asr_model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_audio",
                                # 本项目兼容 ASR 契约接受完整 Base64 Data URI；只传
                                # 裸 Base64 可能被服务端当成无效 URL。
                                "input_audio": {
                                    "data": f"data:audio/wav;base64,{encoded}",
                                    "format": "wav",
                                },
                            }
                        ],
                    }
                ],
                "stream": False,
                "asr_options": {"enable_itn": False},
            }
            text = await self._completion(self._asr_client, payload)
            if text.strip():
                result.append((float(start), float(end), text.strip()))
        return result

    async def _describe_frames(
        self,
        frames: tuple[tuple[Path, float], ...],
    ) -> list[tuple[float, str]]:
        """调用现有 Vision 模型提取画面文字和对检索有用的事实描述。"""
        result: list[tuple[float, str]] = []
        for path, second in frames:
            encoded = base64.b64encode(await asyncio.to_thread(path.read_bytes)).decode("ascii")
            payload = {
                "model": self._vision.model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/jpeg;base64,{encoded}"},
                            },
                            {
                                "type": "text",
                                "text": (
                                    "请逐字提取画面中可读文字，并客观描述与检索有关的"
                                    "图表、对象和动作。不要推测画面外信息。"
                                ),
                            },
                        ],
                    }
                ],
                "stream": False,
            }
            text = await self._completion(self._vision_client, payload)
            if text.strip():
                result.append((second, text.strip()))
        return result

    async def _completion(
        self,
        client: httpx.AsyncClient,
        payload: dict[str, Any],
    ) -> str:
        """有限重试 Chat Completions，并严格读取首个文本结果。"""
        for attempt in range(self._settings.max_attempts):
            try:
                async with self._semaphore:
                    response = await client.post("chat/completions", json=payload)
            except (httpx.TimeoutException, httpx.RequestError) as exc:
                if attempt + 1 < self._settings.max_attempts:
                    await asyncio.sleep(0.25 * (attempt + 1))
                    continue
                raise _media_error("音视频模型服务不可用", retryable=True) from exc
            if (
                response.status_code in _RETRY_STATUSES
                and attempt + 1 < self._settings.max_attempts
            ):
                await asyncio.sleep(0.25 * (attempt + 1))
                continue
            if not response.is_success:
                raise _media_error("音视频模型服务请求失败", retryable=False)
            try:
                body = response.json()
                text = body["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise _media_error("音视频模型响应协议无效", retryable=True) from exc
            if not isinstance(text, str):
                raise _media_error("音视频模型响应协议无效", retryable=True)
            return text
        raise _media_error("音视频模型服务不可用", retryable=True)

    async def _run_tool(self, executable: str, *arguments: str) -> bytes:
        """无 Shell 启动固定工具参数，并在超时/取消时杀死子进程。"""
        try:
            process = await asyncio.create_subprocess_exec(
                executable,
                *arguments,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise _media_error("音视频处理工具不可用") from exc
        try:
            stdout, _ = await asyncio.wait_for(
                process.communicate(),
                timeout=self._settings.process_timeout_seconds,
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise _media_error("音视频本地预处理超时", retryable=True) from exc
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            raise
        if process.returncode != 0:
            raise _media_error("音视频容器或轨道无法解析")
        return stdout

    @staticmethod
    def _markdown(
        filename: str,
        transcripts: list[tuple[float, float, str]],
        frames: list[tuple[float, str]],
    ) -> str:
        """生成确定性 Markdown；标题与时间戳让 RAGFlow Chunk 保留媒体定位信息。"""
        lines = [f"# {Path(filename).name} 音视频解析文本", ""]
        if transcripts:
            lines.extend(["## 音频转写", ""])
            for start, end, text in transcripts:
                lines.extend([f"### [{_timestamp(start)}–{_timestamp(end)}]", text, ""])
        if frames:
            lines.extend(["## 视频画面", ""])
            for second, text in frames:
                lines.extend([f"### [{_timestamp(second)}]", text, ""])
        return "\n".join(lines).strip() + "\n"
