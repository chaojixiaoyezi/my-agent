# LLM: 此入口只识别宿主有界取得的图片字节，不读取路径、取网、改变owner或凭文件名认定MIME；接入方仍负责原refs及请求总量合同。
# 模块用途: 在原生图片入站前核验真实格式、完整解码和像素/帧预算，防止伪后缀、损坏图片及解压资源耗尽。
from __future__ import annotations

import io

from .input_media import InputMediaError

# 入站图片默认累计解码像素预算，多帧图片合计核验。
DEFAULT_IMAGE_PIXELS = 40_000_000
# 入站图片最多解码的帧数，防止动图占满处理资源。
MAX_IMAGE_FRAMES = 256


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
