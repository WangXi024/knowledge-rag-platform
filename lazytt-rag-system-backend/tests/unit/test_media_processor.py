"""音视频预处理器测试：验证时间轴、模型协议、视频画面和错误脱敏。

测试不会启动 FFmpeg 或访问公网，而是用确定性的临时音频/图片替代工具产物，再通过
HTTP Mock 验证真正发往 OpenAI-compatible 接口的请求结构。这样既覆盖核心编排，
也不会把云端额度或本机编解码器状态带入单元测试。
"""

from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path

import httpx
import pytest

from rag_service.domain.errors import RagError
from rag_service.infrastructure.media.openai_compatible import (
    OpenAICompatibleMediaProcessor,
)
from rag_service.settings import MediaSection, VisionSection


def _media_settings() -> MediaSection:
    """返回不含真实密钥的最小启用配置，所有网络请求都会进入 MockTransport。"""
    return MediaSection(
        enabled=True,
        asr_base_url="https://asr.invalid/v1",
        asr_api_key="synthetic-asr-key",
        asr_model="synthetic-asr-model",
        segment_seconds=60,
        segment_overlap_seconds=2,
        frame_interval_seconds=10,
        max_video_frames=20,
    )


def _media_vision_settings() -> VisionSection:
    """返回只供视频抽帧描述使用的合成视觉配置，避免与文档 OCR 混淆。"""
    return VisionSection(
        base_url="https://media-vision.invalid/v1",
        api_key="synthetic-media-vision-key",
        model="synthetic-media-vision-model",
    )


class StubMediaProcessor(OpenAICompatibleMediaProcessor):
    """跳过真实 FFmpeg，只生成可被父类模型编排消费的确定性临时文件。"""

    async def _probe_duration(self, source: Path) -> float:
        """模拟一个 61 秒媒体，确保测试能观察两个时间段。"""
        del source
        return 61.0

    async def _extract_audio_segments(
        self,
        source: Path,
        workdir: Path,
        duration: float,
    ) -> tuple[tuple[Path, float, float], ...]:
        """创建两个合成 WAV 占位文件；正文不含真实录音。"""
        del source, duration
        first = workdir / "audio-00000.wav"
        second = workdir / "audio-00001.wav"
        first.write_bytes(b"first synthetic wav")
        second.write_bytes(b"second synthetic wav")
        return (first, 0.0, 60.0), (second, 58.0, 61.0)

    async def _extract_video_frames(
        self,
        source: Path,
        workdir: Path,
        duration: float,
    ) -> tuple[tuple[Path, float], ...]:
        """创建两个合成 JPEG 占位文件，覆盖视频画面描述路线。"""
        del source, duration
        first = workdir / "frame-00000.jpg"
        second = workdir / "frame-00001.jpg"
        first.write_bytes(b"first synthetic jpeg")
        second.write_bytes(b"second synthetic jpeg")
        return (first, 0.0), (second, 30.0)


class RecordingMediaProcessor(OpenAICompatibleMediaProcessor):
    """记录本地工具调用并生成小型产物，用于验证分段时间而不启动 FFmpeg。"""

    async def _run_tool(self, executable: str, *arguments: str) -> bytes:
        """把输出路径写成合成文件，其他参数由受测编排逻辑负责生成。"""
        del executable
        await asyncio.to_thread(
            Path(arguments[-1]).write_bytes,
            b"synthetic media output",
        )
        return b""


@pytest.mark.asyncio
async def test_media_processor_builds_timestamped_video_markdown() -> None:
    """视频会合并两段 ASR 与两帧描述，并记录可追溯的处理元数据。"""
    asr_calls = 0
    vision_calls = 0

    async def asr_handler(request: httpx.Request) -> httpx.Response:
        """验证 ASR 使用 input_audio 而不是把本地路径泄漏给云端。"""
        nonlocal asr_calls
        asr_calls += 1
        payload = request.read().decode("utf-8")
        assert '"type":"input_audio"' in payload
        assert "data:audio/wav;base64," in payload
        assert "source.mp4" not in payload
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": f"第 {asr_calls} 段语音"}}]},
        )

    async def vision_handler(request: httpx.Request) -> httpx.Response:
        """验证视频帧按 data URI 发送，并返回确定性的画面说明。"""
        nonlocal vision_calls
        vision_calls += 1
        payload = request.read().decode("utf-8")
        assert "data:image/jpeg;base64," in payload
        assert '"model":"synthetic-media-vision-model"' in payload
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": f"第 {vision_calls} 帧画面"}}]},
        )

    processor = StubMediaProcessor(
        _media_settings(),
        _media_vision_settings(),
        asr_client=httpx.AsyncClient(
            base_url="https://asr.invalid/v1/",
            transport=httpx.MockTransport(asr_handler),
        ),
        vision_client=httpx.AsyncClient(
            base_url="https://vision.invalid/v1/",
            transport=httpx.MockTransport(vision_handler),
        ),
    )
    try:
        prepared = await processor.prepare(
            BytesIO(b"synthetic video bytes"),
            filename="演示视频.mp4",
            mime_type="video/mp4",
        )
    finally:
        await processor._asr_client.aclose()
        await processor._vision_client.aclose()

    markdown = prepared.content.decode("utf-8")
    assert prepared.filename == "演示视频.media-transcript.md"
    assert prepared.mime_type == "text/markdown"
    assert "[00:00:00–00:01:00]" in markdown
    assert "[00:00:58–00:01:01]" in markdown
    assert "[00:00:30]" in markdown
    assert "第 2 段语音" in markdown
    assert "第 2 帧画面" in markdown
    assert prepared.metadata["media_source_type"] == "video"
    assert prepared.metadata["media_segment_count"] == 2
    assert prepared.metadata["media_frame_count"] == 2
    assert prepared.metadata["media_vision_model"] == "synthetic-media-vision-model"
    assert asr_calls == 2
    assert vision_calls == 2


@pytest.mark.asyncio
async def test_video_frame_timestamps_cover_short_video_and_final_frame() -> None:
    """106 秒视频按 10 秒覆盖时间轴，并额外保留接近结尾的画面。"""
    asr_client = httpx.AsyncClient()
    vision_client = httpx.AsyncClient()
    processor = OpenAICompatibleMediaProcessor(
        _media_settings(),
        _media_vision_settings(),
        asr_client=asr_client,
        vision_client=vision_client,
    )

    try:
        timestamps = processor._video_frame_timestamps(106.0)
    finally:
        await asr_client.aclose()
        await vision_client.aclose()

    assert timestamps[:-1] == tuple(float(second) for second in range(0, 101, 10))
    assert timestamps[-1] == 105.5


@pytest.mark.asyncio
async def test_video_frame_timestamps_spread_long_video_within_limit() -> None:
    """长视频自动放宽间隔，既不超出模型调用上限，也不会只覆盖开头。"""
    asr_client = httpx.AsyncClient()
    vision_client = httpx.AsyncClient()
    processor = OpenAICompatibleMediaProcessor(
        _media_settings(),
        _media_vision_settings(),
        asr_client=asr_client,
        vision_client=vision_client,
    )

    try:
        timestamps = processor._video_frame_timestamps(3600.0)
    finally:
        await asr_client.aclose()
        await vision_client.aclose()

    assert len(timestamps) <= 20
    assert timestamps[0] == 0.0
    assert timestamps[-1] == 3599.5


@pytest.mark.asyncio
async def test_audio_segments_use_sixty_second_windows_with_overlap(tmp_path: Path) -> None:
    """106 秒音轨生成 0–60 和 58–106 两段，避免边界语句被截断。"""
    asr_client = httpx.AsyncClient()
    vision_client = httpx.AsyncClient()
    processor = RecordingMediaProcessor(
        _media_settings(),
        _media_vision_settings(),
        asr_client=asr_client,
        vision_client=vision_client,
    )

    try:
        segments = await processor._extract_audio_segments(
            tmp_path / "source.mp4",
            tmp_path,
            106.0,
        )
    finally:
        await asr_client.aclose()
        await vision_client.aclose()

    assert [(start, end) for _, start, end in segments] == [
        (0.0, 60.0),
        (58.0, 106.0),
    ]


@pytest.mark.asyncio
async def test_media_processor_maps_cloud_failure_without_exposing_response() -> None:
    """云端错误只映射为稳定领域错误，不把供应商响应或媒体正文回显给用户。"""

    async def failing_handler(_request: httpx.Request) -> httpx.Response:
        """模拟包含敏感细节的授权失败响应，断言它不会进入领域消息。"""
        return httpx.Response(401, text="secret provider diagnostic")

    client = httpx.AsyncClient(
        base_url="https://asr.invalid/v1/",
        transport=httpx.MockTransport(failing_handler),
    )
    processor = StubMediaProcessor(
        _media_settings(),
        _media_vision_settings(),
        asr_client=client,
        vision_client=client,
    )
    try:
        with pytest.raises(RagError) as captured:
            await processor.prepare(
                BytesIO(b"synthetic audio bytes"),
                filename="fixture.wav",
                mime_type="audio/wav",
            )
    finally:
        await client.aclose()

    assert captured.value.code == "RAG-DOC-013"
    assert captured.value.retryable is False
    assert "secret provider diagnostic" not in captured.value.message
