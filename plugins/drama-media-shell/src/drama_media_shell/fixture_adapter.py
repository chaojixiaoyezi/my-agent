# LLM: 这是唯一随包生产适配器，只生成固定离线字节；不得加入网络、凭据、子进程或真实供应商分支。
# 模块用途: 为 image、video、tts、music 作业提供可重复的离线夹具媒体字节。

from __future__ import annotations

import base64
import io
import struct
import wave

from .errors import DramaShellError

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 24


# LLM: 字节必须固定，使 run、collect 和审计能用摘要复核；它们只用于管线测试，绝不代表媒体生成质量。
# 函数用途: 按模态返回一份确定性的最小夹具媒体。
def fixture_bytes(modality: str) -> bytes:
    if modality == "image":
        return PNG
    if modality == "video":
        return MP4
    if modality in {"tts", "music"}:
        return _silent_wav()
    raise DramaShellError("INVALID_MODALITY", "夹具不支持该媒体模态。")


# LLM: wave 只写内存缓冲区，不打开工作区路径；真正落盘必须由 production 的 SDK 写入上下文完成。
# 函数用途: 生成十毫秒单声道静音 WAV 夹具。
def _silent_wav() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(8000)
        handle.writeframes(struct.pack("<h", 0) * 80)
    return buffer.getvalue()
