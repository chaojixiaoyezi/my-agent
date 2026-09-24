# LLM: 只按文件头魔数识别 PNG / JPEG / GIF / WebP，不信扩展名；宽高用标准库按各格式头部结构解析，不引入 Pillow。
#   开放格式集合在这里是能力边界而非封闭判定：新增格式要同时确认 tesseract/leptonica 能读，再在此增加解析。
# 模块用途: 从图片字节得到格式名与宽高，其他格式或损坏头部明确拒绝。

from __future__ import annotations

import struct

from .reading import ImageTextError

# JPEG 中携带帧尺寸的 SOF 标记（排除 C4 霍夫曼表、C8 保留、CC 算术编码表）
_JPEG_SOF = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}


# LLM: 返回 (format, width, height)；未知魔数抛 UNSUPPORTED_FORMAT，已识别格式但头部截断/尺寸为 0 抛 INVALID_IMAGE。
# 函数用途: 识别图片格式并解析宽高。
def image_info(data: bytes) -> tuple[str, int, int]:
    try:
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            fmt, size = "png", _png(data)
        elif data.startswith(b"\xff\xd8\xff"):
            fmt, size = "jpeg", _jpeg(data)
        elif data[:6] in (b"GIF87a", b"GIF89a"):
            fmt, size = "gif", struct.unpack("<HH", data[6:10])
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            fmt, size = "webp", _webp(data)
        else:
            raise ImageTextError("UNSUPPORTED_FORMAT", "只支持 PNG、JPEG、GIF、WebP 图片（按文件头识别，不看扩展名）。")
    except (struct.error, IndexError) as exc:
        raise ImageTextError("INVALID_IMAGE", "图片头部损坏或不完整，无法读取宽高。") from exc
    if size is None or not size[0] or not size[1]:
        raise ImageTextError("INVALID_IMAGE", "图片头部损坏或不完整，无法读取宽高。")
    return fmt, size[0], size[1]


# LLM: IHDR 必须是签名后的第一个块；宽高为大端 32 位。
# 函数用途: 解析 PNG 宽高。
def _png(data: bytes) -> tuple[int, int] | None:
    if data[12:16] != b"IHDR":
        return None
    return struct.unpack(">II", data[16:24])


# LLM: 逐段跳过标记段直到 SOF；遇到 SOS/EOI 仍无 SOF 视为损坏；段长度不足 2 视为损坏，保证循环前进。
# 函数用途: 解析 JPEG 宽高。
def _jpeg(data: bytes) -> tuple[int, int] | None:
    offset = 2
    while offset + 4 <= len(data):
        if data[offset] != 0xFF:
            return None
        marker = data[offset + 1]
        if marker == 0xFF:
            offset += 1
            continue
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            offset += 2
            continue
        if marker in (0xD9, 0xDA):
            return None
        length = struct.unpack(">H", data[offset + 2:offset + 4])[0]
        if length < 2:
            return None
        if marker in _JPEG_SOF:
            height, width = struct.unpack(">HH", data[offset + 5:offset + 9])
            return width, height
        offset += 2 + length
    return None


# LLM: 三种 WebP 头：VP8（有损，14 位宽高）、VP8L（无损，位打包 14 位宽高减一）、VP8X（扩展，24 位画布宽高减一）。
# 函数用途: 解析 WebP 宽高。
def _webp(data: bytes) -> tuple[int, int] | None:
    if len(data) < 30:
        return None
    chunk = data[12:16]
    if chunk == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
        width, height = struct.unpack("<HH", data[26:30])
        return width & 0x3FFF, height & 0x3FFF
    if chunk == b"VP8L" and data[20:21] == b"\x2f":
        bits = struct.unpack("<I", data[21:25])[0]
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8X":
        return int.from_bytes(data[24:27], "little") + 1, int.from_bytes(data[27:30], "little") + 1
    return None
