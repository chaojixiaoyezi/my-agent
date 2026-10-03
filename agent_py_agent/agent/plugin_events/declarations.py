# LLM: v8 清单的纯协议层，只有不可变声明、严格字段校验与独立 JSON 投影；不查询工具目录、owner 或宿主配置。
#   第一期观察正文仅提示文字（3a 2026-10-03 裁定）；同步设计第4节、plugin_manifest、确认码及合同测试，不授运行权限。
# 模块用途: 说明插件想观察什么、收紧什么及是否联网，在读取安装包时就拒绝无效或过宽的参数范围。
from __future__ import annotations

import re
from dataclasses import dataclass

PLUGIN_EVENT_TYPES = (
    "prompt_submitted", "turn_started", "turn_ended", "tool_call_started",
    "tool_call_finished", "command_executed",
)
_TEXT_EVENT_TYPES = ("prompt_submitted",)
# 每包最多订阅六类事件，与第一期六种事件类型一致，限制声明和待投递事实的范围。
MAX_PLUGIN_EVENT_COUNT = 6
# 每包最多四条收紧门，按第一期设计限制一次工具调用需要合并的插件规则量。
MAX_PLUGIN_TOOL_GATE_COUNT = 4
# 每条收紧门最多列十六个精确工具名，按第一期设计限制声明及确认预览的体积。
MAX_PLUGIN_GATE_TOOL_COUNT = 16
_GATE_ID = re.compile(r"[a-z0-9-]{1,32}\Z")
_EXACT_TOOL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}\Z")


# LLM: content 仅声明数据范围，正文授权仍需用户确认并由 B4 按范围投影；不提供工具输出或隐式回调。
# 类用途: 冻结一类观察事件及其正文需求。
@dataclass(frozen=True)
class PluginEventDeclaration:
    type: str
    content: str = "none"

    # LLM: 第一期固定六事件，text 仅提示提交；工具参数只走精确 full 收紧门，同源校验不读取正文或授予权限。
    # 函数用途: 在直接构造与 JSON 读包前拒绝工具观察正文等过宽声明。
    def __post_init__(self) -> None:
        if self.type not in PLUGIN_EVENT_TYPES or self.content not in ("none", "text"):
            raise ValueError("事件类型或正文范围无效")
        if self.content == "text" and self.type not in _TEXT_EVENT_TYPES:
            raise ValueError("这类事件不提供正文")

    # LLM: 返回独立可序列化对象，与 from_payload 同源；缺省 content 显式输出为 none。
    # 函数用途: 生成清单与确认码共用的事件事实。
    def to_payload(self) -> dict:
        return {"type": self.type, "content": self.content}

    # LLM: type 必填，content 可省略；未知字段拒绝，不从其他键推断正文授权。
    # 函数用途: 从 JSON 恢复不可变事件声明。
    @classmethod
    def from_payload(cls, value: object) -> PluginEventDeclaration:
        row = _fields(value, {"type"}, {"content"})
        return cls(row["type"], row.get("content", "none"))


# LLM: tools 是格式合法的精确工具名，不校验当前存在性；effects 只能收紧写入/危险工具，不能代表放宽权限。
# 类用途: 冻结一个工具收紧门及用户能在确认预览中看清的参数范围。
@dataclass(frozen=True)
class PluginToolGateDeclaration:
    id: str
    tools: tuple[str, ...]
    effects: tuple[str, ...]
    arguments: str

    # LLM: full 必须只匹配精确工具；效果匹配范围不固定，不能向其交付完整参数。直接构造和 JSON 共用此硬门。
    # 函数用途: 校验编号、精确工具列表、效果和参数范围，拒绝通配与过宽的完整参数订阅。
    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not _GATE_ID.fullmatch(self.id):
            raise ValueError("收紧门编号无效")
        if (not isinstance(self.tools, tuple) or len(self.tools) > MAX_PLUGIN_GATE_TOOL_COUNT
                or any(not isinstance(name, str) or not _EXACT_TOOL_NAME.fullmatch(name) for name in self.tools)):
            raise ValueError("收紧工具必须是精确工具名列表")
        if not isinstance(self.effects, tuple) or any(effect not in ("mutating", "dangerous") for effect in self.effects):
            raise ValueError("收紧效果无效")
        if not (self.tools or self.effects) or self.arguments not in ("none", "full"):
            raise ValueError("收紧范围或参数范围无效")
        if self.arguments == "full" and (not self.tools or self.effects):
            raise ValueError("完整参数只能提供给精确列出的工具")

    # LLM: 数组每次新建，调用者修改投影不改变不可变清单或确认事实。
    # 函数用途: 生成清单与确认码共用的收紧门事实。
    def to_payload(self) -> dict:
        return {"id": self.id, "tools": list(self.tools), "effects": list(self.effects), "arguments": self.arguments}

    # LLM: 四项必填，严格拒绝未知字段与字符串伪装列表；参数范围不默认为 full。
    # 函数用途: 从 JSON 恢复工具收紧门。
    @classmethod
    def from_payload(cls, value: object) -> PluginToolGateDeclaration:
        row = _fields(value, {"id", "tools", "effects", "arguments"})
        return cls(row["id"], tuple(_array(row["tools"])), tuple(_array(row["effects"])), row["arguments"])


# LLM: 静态权限需求不等于沙箱已施加；实际 owner、开关、强制沙箱与断网由 B7 裁决。
# 类用途: 保存用户确认码必须覆盖的网络需求，默认禁止联网。
@dataclass(frozen=True)
class PluginEventPermissions:
    network: bool = False

    # LLM: 严格真布尔，不能让 0/1 或字符串绕过权限声明。
    # 函数用途: 拒绝不明确的网络权限需求。
    def __post_init__(self) -> None:
        if type(self.network) is not bool:
            raise ValueError("网络权限必须是布尔值")

    # LLM: 与清单、确认码共用同一投影，不加入宿主状态或私有设置。
    # 函数用途: 生成规范网络权限对象。
    def to_payload(self) -> dict:
        return {"network": self.network}

    # LLM: 仅允许可省略的 network，未来权限不能通过未知键静默生效。
    # 函数用途: 从 JSON 恢复默认断网的权限需求。
    @classmethod
    def from_payload(cls, value: object) -> PluginEventPermissions:
        row = _fields(value, set(), {"network"})
        return cls(row.get("network", False))


# LLM: 空集是旧协议的内存默认；非空必须另由包层验证 v8 文件入口、权限与 host_api 互斥，不在这里猜协议。
# 函数用途: 校验订阅集合的类型、上限和唯一身份。
def validate_subscriptions(events: tuple, tool_gates: tuple) -> None:
    _declarations(events, PluginEventDeclaration, MAX_PLUGIN_EVENT_COUNT)
    _declarations(tool_gates, PluginToolGateDeclaration, MAX_PLUGIN_TOOL_GATE_COUNT)
    if len({event.type for event in events}) != len(events) or len({gate.id for gate in tool_gates}) != len(tool_gates):
        raise ValueError("事件类型或收紧门编号重复")


# LLM: 只负责 v8 的三个新增字段；包层先检查完整顶层字段集合，默认值不会把空 v8 降为 v6。
# 函数用途: 为包读取器生成经过验证的不可变订阅与权限。
def subscriptions_from_payload(row: dict) -> dict:
    return {"events": tuple(PluginEventDeclaration.from_payload(item) for item in _array(row["events"])),
            "tool_gates": tuple(PluginToolGateDeclaration.from_payload(item) for item in _array(row["tool_gates"])),
            "permissions": PluginEventPermissions.from_payload(row["permissions"])}


# LLM: 严格字段集合，不读取自然语言、不丢弃未知权限或回调字段。
# 函数用途: 校验声明对象的必填和可选字段。
def _fields(value: object, required: set, optional: set = frozenset()) -> dict:
    if not isinstance(value, dict) or not required <= set(value) or set(value) - required - optional:
        raise ValueError("事件声明字段无效")
    return value


# LLM: JSON 数组不能由字符串、对象或空值代替，避免逐字符转换成声明。
# 函数用途: 验证订阅中的数组形状。
def _array(value: object) -> list:
    if not isinstance(value, list):
        raise ValueError("事件声明集合必须是数组")
    return value


# LLM: 不可变元组和已验证记录是内部唯一形态，防止直接构造绕过 JSON 的约束。
# 函数用途: 检查订阅元组类型及数量上限。
def _declarations(items: tuple, kind: type, limit: int) -> None:
    if not isinstance(items, tuple) or len(items) > limit or any(not isinstance(item, kind) for item in items):
        raise ValueError("事件声明集合类型无效或数量超限")
