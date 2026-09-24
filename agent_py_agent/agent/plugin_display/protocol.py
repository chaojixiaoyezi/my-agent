# LLM: 插件展示的纯协议层：面板声明、公开主题投影和展示描述校验都在这里，无 IO、无状态、不导入插件实现。
#   宿主与插件 SDK 共用同一份常量和上限；修改上限或主题须同步设计文档 PLUGIN_DISPLAY.md 与协议测试。
# 模块用途: 把插件声明的面板和插件返回的展示内容限制在可校验、可截断的小结构里，TUI 只渲染这里校验过的结果。
from __future__ import annotations

import re
from dataclasses import dataclass

DISPLAY_EXTENSION = "my-agent/display"
DISPLAY_VERSION = "1"
DISPLAY_RENDER_METHOD = "my-agent/display.render"
PANEL_KINDS = ("text", "table", "status")
PANEL_TOPICS = ("activity", "run_state")
MAX_PANELS_PER_PLUGIN = 2
MAX_TEXT_LINES = 20
MAX_LINE_CHARS = 200
MAX_TABLE_ROWS = 20
MAX_TABLE_COLUMNS = 6
MAX_STATUS_FIELDS = 8
_PANEL_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")


# LLM: 面板声明随包固定，只描述展示形状和订阅主题，不含回调、定时器或执行权限。
# 类用途: 保存一个插件面板的静态声明。
@dataclass(frozen=True)
class PanelDeclaration:
    id: str
    title: str
    kind: str
    topics: tuple[str, ...]

    # LLM: 构造即校验；主题只能取核心公开主题，不能由包新增或指向任意核心状态。
    # 函数用途: 拒绝格式错误、未知类型或未知主题的面板声明。
    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not _PANEL_ID.fullmatch(self.id):
            raise ValueError("面板编号无效")
        if not isinstance(self.title, str) or not 1 <= len(self.title) <= 40 or not _printable(self.title):
            raise ValueError("面板标题无效")
        if self.kind not in PANEL_KINDS:
            raise ValueError("面板类型无效")
        if (not isinstance(self.topics, tuple) or not self.topics
                or len(set(self.topics)) != len(self.topics)
                or any(topic not in PANEL_TOPICS for topic in self.topics)):
            raise ValueError("面板订阅主题无效")

    # LLM: 输出字段与 from_payload 一一对应，供包描述稳定序列化。
    # 函数用途: 转成可持久保存的 JSON 对象。
    def to_payload(self) -> dict:
        return {"id": self.id, "title": self.title, "kind": self.kind, "topics": list(self.topics)}

    # LLM: 严格字段集合，未知字段拒绝；数组不从字符串推断。
    # 函数用途: 从包描述 JSON 恢复面板声明。
    @classmethod
    def from_payload(cls, value: object) -> PanelDeclaration:
        if not isinstance(value, dict) or set(value) != {"id", "title", "kind", "topics"}:
            raise ValueError("面板声明字段不完整或存在未知字段")
        if not isinstance(value["topics"], list):
            raise ValueError("面板订阅主题必须是数组")
        return cls(value["id"], value["title"], value["kind"], tuple(value["topics"]))


# LLM: 数量上限和编号唯一在包层裁决；调用方仍需核对动作引用。
# 函数用途: 校验一个包的全部面板声明。
def validate_panels(panels: tuple[PanelDeclaration, ...]) -> None:
    if not isinstance(panels, tuple) or any(not isinstance(item, PanelDeclaration) for item in panels):
        raise ValueError("面板声明必须是不可变集合")
    if len(panels) > MAX_PANELS_PER_PLUGIN or len({panel.id for panel in panels}) != len(panels):
        raise ValueError("面板数量超限或编号重复")


# LLM: 插件输出是不可信数据：只接受声明类型对应的字段，逐项转成短文本并截断；
#   超限只截断并标记 truncated，不抛异常；形状完全不对才报错，由调用方把面板标为错误。
# 函数用途: 把插件返回的展示内容规范化为核心可直接渲染的结构。
def normalize_display(kind: str, value: object) -> dict:
    if not isinstance(value, dict):
        raise ValueError("展示内容必须是对象")
    truncated = False
    if kind == "text":
        lines = value.get("lines")
        if not isinstance(lines, list):
            raise ValueError("文本面板缺少 lines")
        truncated = len(lines) > MAX_TEXT_LINES
        clean = []
        for line in lines[:MAX_TEXT_LINES]:
            text, cut = _cell(line)
            truncated = truncated or cut
            clean.append(text)
        return {"kind": kind, "lines": clean, "truncated": truncated}
    if kind == "table":
        columns, rows = value.get("columns"), value.get("rows")
        if not isinstance(columns, list) or not isinstance(rows, list) or not columns:
            raise ValueError("表格面板缺少 columns 或 rows")
        truncated = len(columns) > MAX_TABLE_COLUMNS or len(rows) > MAX_TABLE_ROWS
        width = min(len(columns), MAX_TABLE_COLUMNS)
        header = []
        for cell in columns[:width]:
            text, cut = _cell(cell)
            truncated = truncated or cut
            header.append(text)
        body = []
        for row in rows[:MAX_TABLE_ROWS]:
            if not isinstance(row, list):
                raise ValueError("表格行必须是数组")
            cells = []
            for cell in (row + [""] * width)[:width]:
                text, cut = _cell(cell)
                truncated = truncated or cut
                cells.append(text)
            truncated = truncated or len(row) > width
            body.append(cells)
        return {"kind": kind, "columns": header, "rows": body, "truncated": truncated}
    if kind == "status":
        fields = value.get("fields")
        if not isinstance(fields, list):
            raise ValueError("状态面板缺少 fields")
        truncated = len(fields) > MAX_STATUS_FIELDS
        clean = []
        for item in fields[:MAX_STATUS_FIELDS]:
            if not isinstance(item, dict):
                raise ValueError("状态字段必须是对象")
            label, cut_label = _cell(item.get("label"))
            text, cut_value = _cell(item.get("value"))
            truncated = truncated or cut_label or cut_value
            clean.append({"label": label, "value": text})
        return {"kind": kind, "fields": clean, "truncated": truncated}
    raise ValueError("面板类型无效")


# LLM: 单元格只接受标量，去掉控制字符防止终端注入；布尔和数字转成字符串展示。
# 函数用途: 把一个单元格规范成不含控制字符的短文本，并返回是否被截断。
def _cell(value: object) -> tuple[str, bool]:
    if value is None:
        return "", False
    if isinstance(value, bool):
        value = "是" if value else "否"
    if not isinstance(value, (str, int, float)):
        raise ValueError("展示单元格必须是标量")
    text = "".join(ch for ch in str(value) if ch == " " or (ord(ch) >= 32 and ord(ch) != 127))
    return _clip(text, MAX_LINE_CHARS), len(text) > MAX_LINE_CHARS


# LLM: 纯截断，不附加省略号以外的内容。
# 函数用途: 把文本限制在给定长度内。
def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


# LLM: 声明文本不得含控制字符，避免终端输出被控制序列篡改。
# 函数用途: 判断声明文本是否只含可显示字符。
def _printable(text: str) -> bool:
    return all(ord(ch) >= 32 and ord(ch) != 127 for ch in text)
