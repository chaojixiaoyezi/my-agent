# LLM: 候选区域像素摘要的唯一口径（J16 片 B，ae 定稿）：从 RGB 像素缓冲裁出候选外框 → 灰度 → 按面积平均缩成
#   DIGEST_GRID_COLUMNS_COUNT × DIGEST_GRID_ROWS_COUNT 格 → 每格量化成 DIGEST_LEVELS_COUNT 级；比较时 sha 相等走快路径，
#   否则"量化级相差 ≥ DIGEST_CHANGED_LEVEL_MIN_COUNT 的格子不超过 DIGEST_CHANGED_CELLS_MAX_COUNT 个"算没变。纯 Python、只依赖
#   标准库，适配器与单测共用；数字是模块常数，不做配置项。
# 模块用途: 把"候选那块像素和当时一样吗"变成可复现的判定，容忍光标闪烁与抗锯齿，拦住内容变化和弹窗压住。
from __future__ import annotations

import hashlib
from dataclasses import dataclass

# 摘要格子的列数（面积平均缩放后的格子网格宽）
DIGEST_GRID_COLUMNS_COUNT = 16
# 摘要格子的行数（格子网格高）
DIGEST_GRID_ROWS_COUNT = 8
# 每格灰度量化的级数（256 级灰度 → 16 级）
DIGEST_LEVELS_COUNT = 16
# 一格要相差多少级才算"变了"
DIGEST_CHANGED_LEVEL_MIN_COUNT = 2
# 变了的格子超过多少个才算区域变了（128 格的约 3%，留给光标闪烁与抗锯齿）
DIGEST_CHANGED_CELLS_MAX_COUNT = 4
# 摘要十六进制取前多少位
DIGEST_HEX_CHARS = 16


# LLM: rgb 是行优先、每像素 3 字节的原始缓冲（mss ScreenShot.rgb 同形），长度必须等于 width*height*3。
# 类用途: 一次窗口截图的像素。
@dataclass(frozen=True)
class PixelBuffer:
    width: int
    height: int
    rgb: bytes

    # 函数用途: 拒绝尺寸与缓冲长度对不上的截图。
    def __post_init__(self) -> None:
        if self.width <= 0 or self.height <= 0 or len(self.rgb) != self.width * self.height * 3:
            raise ValueError("像素缓冲尺寸与长度不符")


# LLM: region 必须已经在截图范围内（宿主 parse_observation 已校验），这里只做裁剪与缩放；区域小于格子数时格子退化成重复像素。
# 函数用途: 把候选外框 [x, y, w, h] 缩成固定格数的量化灰度格。
def region_grid(buffer: PixelBuffer, region: tuple[int, int, int, int]) -> bytes:
    x0, y0, width, height = (int(value) for value in region)
    if width <= 0 or height <= 0 or x0 < 0 or y0 < 0 or x0 + width > buffer.width or y0 + height > buffer.height:
        raise ValueError("候选外框超出截图范围")
    cells = bytearray()
    for row in range(DIGEST_GRID_ROWS_COUNT):
        y_start, y_end = _span(y0, height, row, DIGEST_GRID_ROWS_COUNT)
        for column in range(DIGEST_GRID_COLUMNS_COUNT):
            cells.append(_cell_level(buffer, _span(x0, width, column, DIGEST_GRID_COLUMNS_COUNT), (y_start, y_end)))
    return bytes(cells)


# 函数用途: 第 index 格在该轴上覆盖的像素区间 [start, end)，至少 1 像素。
def _span(offset: int, length: int, index: int, cells: int) -> tuple[int, int]:
    start = offset + index * length // cells
    end = max(start + 1, offset + (index + 1) * length // cells)
    return start, min(end, offset + length) if index < cells - 1 else offset + length


# 函数用途: 一格（x 区间 × y 区间，均为 [start, end)）的平均灰度量化级（灰度用 Rec.601 整数权重）。
def _cell_level(buffer: PixelBuffer, x_span: tuple[int, int], y_span: tuple[int, int]) -> int:
    total, count, stride = 0, 0, buffer.width * 3
    for y in range(y_span[0], y_span[1]):
        base = y * stride
        for x in range(x_span[0], x_span[1]):
            pixel = base + x * 3
            total += buffer.rgb[pixel] * 299 + buffer.rgb[pixel + 1] * 587 + buffer.rgb[pixel + 2] * 114
            count += 1
    return (total // (count * 1000)) * DIGEST_LEVELS_COUNT // 256


# 函数用途: 格子的稳定摘要（进快照与证据，不进观察载荷）。
def grid_digest(grid: bytes) -> str:
    return hashlib.sha256(grid).hexdigest()[:DIGEST_HEX_CHARS]


# LLM: 两份格子必须同尺寸；完全相等直接通过，否则按变化格数判定。
# 函数用途: 判断候选区域从快照到现在是否"没变"。
def grid_unchanged(before: bytes, after: bytes) -> bool:
    if len(before) != len(after):
        return False
    if before == after:
        return True
    changed = sum(1 for a, b in zip(before, after) if abs(a - b) >= DIGEST_CHANGED_LEVEL_MIN_COUNT)
    return changed <= DIGEST_CHANGED_CELLS_MAX_COUNT


__all__ = [
    "DIGEST_CHANGED_CELLS_MAX_COUNT", "DIGEST_CHANGED_LEVEL_MIN_COUNT", "DIGEST_GRID_COLUMNS_COUNT", "DIGEST_GRID_ROWS_COUNT",
    "DIGEST_HEX_CHARS", "DIGEST_LEVELS_COUNT", "PixelBuffer", "grid_digest", "grid_unchanged", "region_grid",
]
