# LLM: 工作区只读；输入文件一律经逐次读取上下文授权并用 SDK no-follow 打开，按 max_input_bytes 有界读取。
#   数据只接受两种格式（对象数组；{"columns", "rows"}），单元格只接受标量；不从文件内容推断权限或路径。
# 模块用途: 读取并校验 JSON 表格数据，生成 Markdown 表格、文字条形图和图表数值列。

from __future__ import annotations

import json
import math
import os
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from my_agent_plugin_api.nofollow_fs import open_readonly_file_beneath
from my_agent_plugin_api.workspace_read_context import WorkspaceReadContext

BAR_WIDTH = 30
_SCALARS = (str, int, float, bool, type(None))


# LLM: code 是稳定的业务失败分类；正文是给用户看的中文说明，不含原始异常、配置或被拒绝文件内容。
# 类用途: 让协议入口把渲染/导出失败返回为工具结果，不终止整个插件进程。
class GenuiError(ValueError):
    # LLM: extra 只放结构化事实（如输出路径），协议入口原样并入错误结果；不能放文件正文。
    # 函数用途: 保存错误码、中文说明和可选的附加结构化字段。
    def __init__(self, code: str, message: str, **extra: object):
        super().__init__(message)
        self.code = code
        self.extra = extra


# LLM: rows 已按 max_rows 截断，total_rows 是截断前行数；列名唯一且非空。
# 类用途: 汇总一份规范化后的表格数据及截断事实。
@dataclass(frozen=True)
class Table:
    columns: tuple[str, ...]
    rows: tuple[tuple[object, ...], ...]
    total_rows: int

    # LLM: 纯派生属性，由 rows 与 total_rows 推出，不单独存状态。
    # 函数用途: 判断是否因行数上限截断。
    @property
    def truncated(self) -> bool:
        return len(self.rows) < self.total_rows

    # LLM: 纯展示属性，table 结果与 HTML 页脚共用同一句摘要。
    # 函数用途: 给出行列计数摘要，截断时注明显示行数。
    @property
    def summary(self) -> str:
        text = f"共 {self.total_rows} 行 {len(self.columns)} 列"
        return text + (f"，仅显示前 {len(self.rows)} 行。" if self.truncated else "。")


# LLM: 返回 lexical 绝对路径用于 no-follow 打开；resolve 只在 SDK check 内做权限判断，不能先 resolve 擦掉链接。
# 函数用途: 组合当前工作区路径，拒绝空路径与上溯组件，再按读取上下文检查读取资格。
def workspace_target(context: WorkspaceReadContext, value: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value or ".." in Path(value).parts:
        raise GenuiError("INVALID_PATH", "路径不能为空，也不能含 .. 上溯组件。")
    target = context.cwd / value
    decision = context.check(target)
    if not decision.allowed:
        raise GenuiError(decision.code, "目标不在本次允许读取的工作区范围内。")
    return target


# LLM: 目标相对 cwd 时返回相对路径，否则返回绝对路径；只用于展示，不参与授权。
# 函数用途: 生成结果里给用户看的路径。
def display_path(cwd: Path, target: Path) -> str:
    return str(target.relative_to(cwd)) if target.is_relative_to(cwd) else str(target)


# LLM: 从文件系统根逐段 no-follow 打开（链接、多链接、非普通文件由 SDK 拒绝）；stat 与实际读取量都受 limit 约束，fd 全分支关闭。
# 函数用途: 有界读取工作区 JSON 文件的原始字节。
def read_input(target: Path, limit: int) -> bytes:
    try:
        descriptor = open_readonly_file_beneath(Path(target.anchor), target.parts[1:])
    except FileNotFoundError as exc:
        raise GenuiError("FILE_NOT_FOUND", "数据文件不存在。") from exc
    try:
        if os.fstat(descriptor).st_size > limit:
            raise GenuiError("FILE_TOO_LARGE", f"数据文件超过上限 {limit} 字节（设置 max_input_bytes）。")
        pieces, total = [], 0
        while chunk := os.read(descriptor, 65536):
            total += len(chunk)
            if total > limit:
                raise GenuiError("FILE_TOO_LARGE", f"数据文件超过上限 {limit} 字节（设置 max_input_bytes）。")
            pieces.append(chunk)
        return b"".join(pieces)
    finally:
        os.close(descriptor)


# LLM: 只认两种结构：非空对象数组（列序取第一行键序，后续行键集合必须相同）；或 columns 为非空唯一字符串列表、
#   rows 为等长列表的对象。其它结构、嵌套单元格一律拒绝，不猜测。max_rows 只截断渲染行，total_rows 保留原行数。
# 函数用途: 把 JSON 字节解析成规范化表格。
def parse_table(raw: bytes, max_rows: int) -> Table:
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise GenuiError("INVALID_JSON", "数据文件不是合法的 UTF-8 JSON。") from exc
    if isinstance(data, list) and data and all(isinstance(item, dict) for item in data):
        columns = tuple(data[0])
        if any(set(item) != set(columns) for item in data):
            raise GenuiError("COLUMN_MISMATCH", "对象数组中各行的字段不一致。")
        rows = [tuple(item[name] for name in columns) for item in data]
    elif isinstance(data, dict) and set(data) == {"columns", "rows"} and isinstance(data["rows"], list):
        columns = tuple(data["columns"]) if isinstance(data["columns"], list) else ()
        if not all(isinstance(row, list) for row in data["rows"]):
            raise GenuiError("UNSUPPORTED_FORMAT", "rows 必须是由数组组成的数组。")
        if any(len(row) != len(columns) for row in data["rows"]):
            raise GenuiError("COLUMN_MISMATCH", "rows 中有行的单元格数与 columns 不一致。")
        rows = [tuple(row) for row in data["rows"]]
    else:
        raise GenuiError("UNSUPPORTED_FORMAT", '只支持非空对象数组，或 {"columns": [...], "rows": [[...]]}。')
    if not columns or len(set(columns)) != len(columns) or not all(isinstance(name, str) and name for name in columns):
        raise GenuiError("UNSUPPORTED_FORMAT", "列名必须是非空且不重复的字符串。")
    if any(not isinstance(cell, _SCALARS) for row in rows for cell in row):
        raise GenuiError("UNSUPPORTED_FORMAT", "单元格只支持字符串、数字、布尔和空值，不支持嵌套对象或数组。")
    return Table(columns, tuple(rows[:max_rows]), len(rows))


# LLM: 纯格式化；bool/None 按 JSON 习惯写出，浮点保持 Python 最短表示，不做本地化或四舍五入。
# 函数用途: 把单元格值转成展示文本。
def cell_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


# LLM: 转义竖线并把换行压成空格，避免单元格内容破坏表格结构；截断事实由调用方在摘要里说明。
# 函数用途: 生成 Markdown 表格文本。
def markdown_table(table: Table) -> str:
    # LLM: 仅本函数使用的转义规则，先转反斜杠再转竖线。
    # 函数用途: 把单元格文本转成可安全放进 Markdown 表格的一格。
    def clean(value: object) -> str:
        return cell_text(value).replace("\\", "\\\\").replace("|", "\\|").replace("\r", " ").replace("\n", " ")
    lines = ["| " + " | ".join(clean(name) for name in table.columns) + " |",
             "|" + "|".join(" --- " for _ in table.columns) + "|"]
    lines += ["| " + " | ".join(clean(cell) for cell in row) + " |" for row in table.rows]
    return "\n".join(lines)


# LLM: 数值必须是有限的 int/float（bool 不算）且非负，否则明确报错；标签取第一个非图表列，没有则用行号。
#   只看已渲染的行，与表格截断保持一致。
# 函数用途: 取出条形图的 (标签, 数值) 序列。
def chart_values(table: Table, column: str) -> list[tuple[str, float]]:
    if column not in table.columns:
        raise GenuiError("CHART_COLUMN_NOT_FOUND", f"图表列 {column} 不存在；可用列：{'、'.join(table.columns)}。")
    index = table.columns.index(column)
    values = [row[index] for row in table.rows]
    if not all(type(value) in (int, float) and math.isfinite(value) for value in values):
        raise GenuiError("CHART_COLUMN_NOT_NUMERIC", f"图表列 {column} 必须全部是数字（不能有文本、空值或布尔）。")
    if any(value < 0 for value in values):
        raise GenuiError("CHART_COLUMN_NOT_NUMERIC", f"图表列 {column} 含负数，条形图只支持非负数值。")
    label = next((i for i, name in enumerate(table.columns) if i != index), None)
    return [(cell_text(row[label]) if label is not None else str(n), value)
            for n, (row, value) in enumerate(zip(table.rows, values), start=1)]


# LLM: 中文等宽字符按两格计算对齐；条长按最大值等比缩放到 BAR_WIDTH，非零值至少一格。
# 函数用途: 生成用 █ 画的文字条形图。
def text_bar_chart(points: list[tuple[str, float]]) -> str:
    top = max((value for _, value in points), default=0)
    width = max((_display_width(label) for label, _ in points), default=0)
    lines = []
    for label, value in points:
        length = 0 if top == 0 else max(1 if value > 0 else 0, round(value / top * BAR_WIDTH))
        lines.append(f"{label}{' ' * (width - _display_width(label))} │{'█' * length} {cell_text(value)}")
    return "\n".join(lines)


# LLM: 仅用于终端对齐，东亚宽/全角字符计两格。
# 函数用途: 估算一段文本在等宽终端里的显示宽度。
def _display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in "WF" else 1 for char in text)
