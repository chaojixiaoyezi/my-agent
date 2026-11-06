# LLM: 此入口只识别宿主有界取得的图片字节，不读取路径、取网、改变owner或凭文件名认定MIME；接入方仍负责原refs及请求总量合同。
#   出站压缩只改供应商发送边界的内存副本：磁盘canonical原件、sha256、size_bytes及其校验合同完全不变。
# 模块用途: 在原生图片入站前核验真实格式、完整解码和像素/帧预算，防止伪后缀、损坏图片及解压资源耗尽；发送前按需缩放/重编码大图。
from __future__ import annotations

import hashlib
import io
import threading
from collections import OrderedDict

from ..settings.config import AgentConfig
from .input_media import InputMediaError

# 入站图片默认累计解码像素预算，多帧图片合计核验。
DEFAULT_IMAGE_PIXELS = AgentConfig.input_image_max_pixels
# 入站图片最多解码的帧数，防止动图占满处理资源。
MAX_IMAGE_FRAMES = 256
# 出站图片长边上限：超过时按比例缩小到该像素数再发给模型，磁盘原件不变。
_PROVIDER_IMAGE_MAX_EDGE_PIXELS = 2048
# 出站图片超过该字节数才尝试重新编码；更小且长边不超限的图片（含测试样本）原样发送。
_PROVIDER_IMAGE_RECOMPRESS_MIN_BYTES = 64 * 1024
# 出站 JPEG 编码质量（Pillow 1–95 档位），兼顾截图文字清晰度与体积。
_PROVIDER_IMAGE_JPEG_QUALITY_PERCENT = 85
# 出站压缩结果进程内 LRU 缓存最多保留的条目数。
_PROVIDER_IMAGE_CACHE_MAX_ENTRIES = 64
# 出站压缩结果进程内 LRU 缓存保存的压缩字节总量上限。
_PROVIDER_IMAGE_CACHE_MAX_BYTES = 16 * 1024 * 1024
# 可安全转换后重编码的图片模式；16 位、浮点、CMYK 等其它模式原样发送，避免截断取值或错配色彩描述。
_RECOMPRESSIBLE_MODES = frozenset({"L", "LA", "P", "PA", "RGB", "RGBA"})
_PROVIDER_IMAGE_CACHE: OrderedDict[tuple[str, str], tuple[bytes, str] | None] = OrderedDict()
_PROVIDER_IMAGE_CACHE_LOCK = threading.Lock()


# LLM: MIME来自维护中的解码器格式注册，不是路径/扩展名或封闭格式枚举；未知或需要外部解释器的格式拒绝，不调用外部进程。
# 函数用途: 对内存中的图片逐帧实际解码，返回真实MIME；超过资源预算或字节损坏时只抛安全输入错误。
def inspect_input_image(data: bytes, *, max_pixels: int = DEFAULT_IMAGE_PIXELS) -> str:
    if not isinstance(data, bytes) or not data or type(max_pixels) is not int or max_pixels < 1:
        raise InputMediaError("图片为空或像素预算无效，请重新发送图片。")
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError as exc:
        raise InputMediaError("当前安装缺少图片解码依赖，请管理员更新安装。") from exc
    try:
        with Image.open(io.BytesIO(data)) as image:
            media_type = Image.MIME.get(image.format, "")
            if not media_type.startswith("image/") or image.format == "EPS":
                raise InputMediaError("图片格式不能在当前环境中安全解码，请转换为常用图片格式。")
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            _decode_image_frames(image, max_pixels)
            return media_type
    except InputMediaError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError, SyntaxError,
            EOFError, OverflowError) as exc:
        raise InputMediaError("图片损坏、格式不可识别或超过安全解码上限，请重新发送图片。") from exc


# LLM: 不修改Pillow进程全局限制或截断容忍开关；逐帧累计像素并限制帧数，先检查后解码，不把未解码内容声称为已核验。
# 函数用途: 在不启动外部解释器的前提下验证图片各帧，限制多帧图片的内存和处理时间。
def _decode_image_frames(image: object, max_pixels: int) -> None:
    total_pixels = 0
    for frame in range(MAX_IMAGE_FRAMES + 1):
        try:
            image.seek(frame)
        except EOFError:
            return
        if frame == MAX_IMAGE_FRAMES:
            raise InputMediaError(f"图片帧数超过 {MAX_IMAGE_FRAMES} 帧，请发送静态图片或缩短动图。")
        width, height = image.size
        total_pixels += width * height
        if width < 1 or height < 1 or total_pixels > max_pixels:
            raise InputMediaError(f"图片累计像素超过 {max_pixels} 上限，请缩小图片后重新发送。")
        image.load()


# LLM: 键为原件sha256与声明MIME；值None表示原样发送（小图、非图片、动图、超像素预算、不支持的模式或重编码不更小），
#   只占条数，不保存第二份原件。同一原件的编码结果确定，重复请求得到同一字节，保持WebSocket增量链与服务端前缀缓存稳定；
#   锁只包字典操作，解码/编码在锁外；单条结果超过缓存字节上限时直接返回、不入缓存，不挤掉其它条目。
# 函数用途: 为供应商发送边界返回缩放/重编码后的图片字节与MIME；无收益或无法处理时返回原字节与原MIME。
def compress_provider_image_bytes(data: bytes, media_type: str) -> tuple[bytes, str]:
    key = (hashlib.sha256(data).hexdigest(), media_type)
    with _PROVIDER_IMAGE_CACHE_LOCK:
        if key in _PROVIDER_IMAGE_CACHE:
            _PROVIDER_IMAGE_CACHE.move_to_end(key)
            cached = _PROVIDER_IMAGE_CACHE[key]
            return (data, media_type) if cached is None else cached
    compressed = _compressed_provider_image(data)
    if compressed is None or len(compressed[0]) <= _PROVIDER_IMAGE_CACHE_MAX_BYTES:
        with _PROVIDER_IMAGE_CACHE_LOCK:
            _PROVIDER_IMAGE_CACHE[key] = compressed
            _PROVIDER_IMAGE_CACHE.move_to_end(key)
            while _PROVIDER_IMAGE_CACHE and (len(_PROVIDER_IMAGE_CACHE) > _PROVIDER_IMAGE_CACHE_MAX_ENTRIES
                                             or _cached_provider_image_bytes() > _PROVIDER_IMAGE_CACHE_MAX_BYTES):
                _PROVIDER_IMAGE_CACHE.popitem(last=False)
    return (data, media_type) if compressed is None else compressed


# LLM: 调用方持有缓存锁；原样发送的占位(None)不保存字节，不计入总量。
# 函数用途: 统计出站压缩缓存当前保存的压缩字节总量。
def _cached_provider_image_bytes() -> int:
    return sum(len(value[0]) for value in _PROVIDER_IMAGE_CACHE.values() if value is not None)


# LLM: 只处理静态、像素不超过DEFAULT_IMAGE_PIXELS的常见模式；先按EXIF方向转正（输出不带原EXIF，否则手机照片会侧躺），
#   完全不透明的alpha去掉后编JPEG，真透明保留alpha编PNG，ICC色彩描述按原色彩空间保留。这是体积优化不是输入校验：
#   任何解码/编码异常都退回原件，原本能发送的附件不能因此失败；输出不比原件小时同样原样发送。
# 函数用途: 计算一张图片的出站缩放/重编码结果；不需要处理或没有更小时返回None。
def _compressed_provider_image(data: bytes) -> tuple[bytes, str] | None:
    try:
        from PIL import Image, ImageOps
    except ImportError:
        return None
    output = io.BytesIO()
    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            small = len(data) <= _PROVIDER_IMAGE_RECOMPRESS_MIN_BYTES and max(width, height) <= _PROVIDER_IMAGE_MAX_EDGE_PIXELS
            if (small or getattr(image, "is_animated", False) or image.mode not in _RECOMPRESSIBLE_MODES
                    or width * height > DEFAULT_IMAGE_PIXELS):
                return None
            icc_profile = image.info.get("icc_profile")
            ImageOps.exif_transpose(image, in_place=True)
            frame = _without_opaque_alpha(image)
            frame.thumbnail((_PROVIDER_IMAGE_MAX_EDGE_PIXELS, _PROVIDER_IMAGE_MAX_EDGE_PIXELS), Image.Resampling.LANCZOS)
            if frame.mode in {"RGB", "L"}:
                frame.save(output, format="JPEG", quality=_PROVIDER_IMAGE_JPEG_QUALITY_PERCENT, optimize=True,
                           icc_profile=icc_profile)
                media_type = "image/jpeg"
            else:
                frame.save(output, format="PNG", optimize=True, icc_profile=icc_profile)
                media_type = "image/png"
    except Exception:  # noqa: BLE001 体积优化失败一律原样发送，不改变附件能否发送
        return None
    encoded = output.getvalue()
    return (encoded, media_type) if len(encoded) < len(data) else None


# LLM: 调色板或带透明色键的图先展开成带alpha的同色彩空间（灰度→LA，其余→RGBA）；alpha全为255时去掉，
#   保持灰度/彩色空间不变，原ICC描述仍然匹配；有任一非不透明像素就保留alpha。
# 函数用途: 返回可直接编码的图片：完全不透明的去掉alpha，真透明的保留alpha。
def _without_opaque_alpha(image: object) -> object:
    if image.mode in {"P", "PA"} or "transparency" in image.info:
        image = image.convert("LA" if image.mode in {"L", "LA"} else "RGBA")
    if image.mode in {"RGBA", "LA"} and image.getchannel("A").getextrema() == (255, 255):
        image = image.convert("RGB" if image.mode == "RGBA" else "L")
    return image
