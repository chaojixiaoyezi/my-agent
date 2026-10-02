# LLM: 屏幕观察后端共用的 OCR（J16 片 B 起 X11 用，片 E 起 macOS 也用）：只用 RapidOCR 的公开调用 RapidOCR()(image)，把每个文字框
#   的四点取外框，转成观察核心认识的 TextRegion。numpy 与 rapidocr_onnxruntime 都在调用时惰性 import（没装也能构造），
#   引擎第一次用时创建并缓存（加载模型较慢）。不做任何判定，也不碰屏幕；改动同步 test_computer_use_observation_tools 与
#   test_computer_use_macos 里的假 OCR 用例。
# 模块用途: 把一张截图里的文字区域找出来，给两个桌面后端共用，免得各写一份。
from __future__ import annotations

from typing import Any

from .screen_observation import TextRegion
from .screen_region_digest import PixelBuffer


# LLM: 输入是行优先 RGB 的 PixelBuffer；RapidOCR 吃 BGR 的 numpy 数组，这里按通道倒序转换；box 四点取最小外框，坐标是截图像素。
# 类用途: 惰性创建并复用的 RapidOCR 文字识别器。
class RapidOcrReader:
    def __init__(self) -> None:
        self._engine: Any | None = None

    # 函数用途: 识别截图里的文字区域，返回 TextRegion 列表（文字 + 截图像素外框）。
    def read(self, buffer: PixelBuffer) -> list[TextRegion]:
        import numpy

        image = numpy.frombuffer(buffer.rgb, dtype=numpy.uint8).reshape(buffer.height, buffer.width, 3)[:, :, ::-1]
        result, _elapsed = self._ocr_engine()(numpy.ascontiguousarray(image))
        regions = []
        for box, text, _score in result or []:
            xs, ys = [float(point[0]) for point in box], [float(point[1]) for point in box]
            regions.append(TextRegion(str(text), (round(min(xs)), round(min(ys)), round(max(xs) - min(xs)), round(max(ys) - min(ys)))))
        return regions

    # 函数用途: 第一次用时创建 RapidOCR 引擎并缓存。
    def _ocr_engine(self) -> Any:
        if self._engine is None:
            from rapidocr_onnxruntime import RapidOCR

            self._engine = RapidOCR()
        return self._engine


__all__ = ["RapidOcrReader"]
