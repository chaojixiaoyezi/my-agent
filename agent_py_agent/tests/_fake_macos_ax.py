"""假的 macOS 无障碍（AX）框架（J16 片 G 测试共用）：常量与 pyobjc ApplicationServices 同名，元素是带属性字典的 Python 对象。

记下每次属性读取（元素名, 属性名）、每次写入和消息超时设置，供用例断言"密码框的 AXValue 从没被读过""屏外子树的子节点没读过"
"只读遍历、唯一的写是全选"。绝不弹授权框：AXIsProcessTrustedWithOptions 与 AXUIElementPerformAction 一被调用就失败。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace


# 类用途: 假 AXValueRef：类型 + 结构（点 / 尺寸 / 范围）。
@dataclass(frozen=True)
class FakeAxValue:
    kind: int
    payload: object


# 类用途: 假 AXUIElement：名字（断言用）、属性、可写属性、动作名；invalid 模拟元素已失效。repr 只给名字，和真实的 AXUIElementRef
#   一样不带任何属性值（快照里存的是句柄，用例要能断言"值不进快照"）。
@dataclass(eq=False, repr=False)
class FakeElement:
    name: str
    attrs: dict = field(default_factory=dict)
    settable: set = field(default_factory=set)
    actions: tuple = ()
    invalid: bool = False
    reject_writes: bool = False

    def __repr__(self):
        return f"<FakeElement {self.name}>"


# 类用途: 假 ApplicationServices 模块。
class FakeAx:
    kAXErrorSuccess = 0
    kAXErrorInvalidUIElement = -25202
    kAXErrorCannotComplete = -25204
    kAXErrorNoValue = -25212
    kAXRoleAttribute, kAXSubroleAttribute, kAXTitleAttribute = "AXRole", "AXSubrole", "AXTitle"
    kAXDescriptionAttribute, kAXPlaceholderValueAttribute, kAXValueAttribute = "AXDescription", "AXPlaceholderValue", "AXValue"
    kAXEnabledAttribute, kAXFocusedAttribute, kAXChildrenAttribute = "AXEnabled", "AXFocused", "AXChildren"
    kAXPositionAttribute, kAXSizeAttribute, kAXWindowsAttribute = "AXPosition", "AXSize", "AXWindows"
    kAXSelectedTextRangeAttribute, kAXNumberOfCharactersAttribute = "AXSelectedTextRange", "AXNumberOfCharacters"
    kAXPressAction, kAXSecureTextFieldSubrole = "AXPress", "AXSecureTextField"
    kAXValueCGPointType, kAXValueCGSizeType, kAXValueCFRangeType = 1, 2, 4

    def __init__(self, trusted=True):
        self.trusted = trusted
        self.apps: dict[int, FakeElement] = {}
        self.reads: list[tuple[str, str]] = []
        self.writes: list[tuple[str, str]] = []
        self.timeouts: list[float] = []
        self.windows_error = 0

    def AXIsProcessTrusted(self):
        return self.trusted

    def AXIsProcessTrustedWithOptions(self, options):
        raise AssertionError("绝不能触发辅助功能授权弹窗")

    def AXUIElementPerformAction(self, element, action):
        raise AssertionError("只读遍历，不能执行 AX 动作")

    def AXUIElementCreateSystemWide(self):
        return FakeElement("system-wide")

    def AXUIElementSetMessagingTimeout(self, element, seconds):
        self.timeouts.append(seconds)
        return self.kAXErrorSuccess

    def AXUIElementCreateApplication(self, pid):
        return self.apps.setdefault(pid, FakeElement(f"app:{pid}", {self.kAXWindowsAttribute: []}))

    def AXUIElementCopyAttributeValue(self, element, name, _out):
        self.reads.append((element.name, name))
        if element.invalid:
            return self.kAXErrorInvalidUIElement, None
        if name == self.kAXWindowsAttribute and self.windows_error:
            return self.windows_error, None
        return (self.kAXErrorSuccess, element.attrs[name]) if name in element.attrs else (self.kAXErrorNoValue, None)

    def AXUIElementIsAttributeSettable(self, element, name, _out):
        return (self.kAXErrorInvalidUIElement, False) if element.invalid else (self.kAXErrorSuccess, name in element.settable)

    def AXUIElementCopyActionNames(self, element, _out):
        return (self.kAXErrorInvalidUIElement, None) if element.invalid else (self.kAXErrorSuccess, list(element.actions))

    def AXValueGetValue(self, value, kind, _out):
        return (True, value.payload) if isinstance(value, FakeAxValue) and value.kind == kind else (False, None)

    def AXValueCreate(self, kind, payload):
        location, length = payload
        return FakeAxValue(kind, SimpleNamespace(location=location, length=length))

    def AXUIElementSetAttributeValue(self, element, name, value):
        self.writes.append((element.name, name))
        if element.reject_writes:
            return self.kAXErrorCannotComplete
        element.attrs[name] = value
        return self.kAXErrorSuccess

    # 函数用途: 给某个 PID 的应用挂上窗口列表。
    def install_windows(self, pid, *windows):
        self.apps[pid] = FakeElement(f"app:{pid}", {self.kAXWindowsAttribute: list(windows)})


# 函数用途: 造一个假控件：frame 是全局点 (x, y, w, h)，None 表示没有外框；facts 可给 children / title / description / placeholder /
#   value / subrole / enabled / focused / characters，pressable=True 给 AXPress，editable=True 让 AXValue 与选区可写。
def ax_element(name, role, frame, **facts):
    attrs = {FakeAx.kAXRoleAttribute: role, FakeAx.kAXChildrenAttribute: list(facts.get("children", ())),
             FakeAx.kAXEnabledAttribute: facts.get("enabled", True)}
    if frame is not None:
        attrs[FakeAx.kAXPositionAttribute] = FakeAxValue(FakeAx.kAXValueCGPointType, SimpleNamespace(x=frame[0], y=frame[1]))
        attrs[FakeAx.kAXSizeAttribute] = FakeAxValue(FakeAx.kAXValueCGSizeType, SimpleNamespace(width=frame[2], height=frame[3]))
    keys = {"title": FakeAx.kAXTitleAttribute, "description": FakeAx.kAXDescriptionAttribute, "subrole": FakeAx.kAXSubroleAttribute,
            "placeholder": FakeAx.kAXPlaceholderValueAttribute, "value": FakeAx.kAXValueAttribute, "focused": FakeAx.kAXFocusedAttribute,
            "characters": FakeAx.kAXNumberOfCharactersAttribute}
    attrs.update({keys[key]: value for key, value in facts.items() if key in keys})
    settable = {FakeAx.kAXValueAttribute, FakeAx.kAXSelectedTextRangeAttribute} if facts.get("editable") else set()
    return FakeElement(name, attrs, settable, (FakeAx.kAXPressAction,) if facts.get("pressable") else ())


__all__ = ["FakeAx", "FakeAxValue", "FakeElement", "ax_element"]
