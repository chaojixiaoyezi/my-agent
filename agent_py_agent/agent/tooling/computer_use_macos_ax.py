# LLM: J16 片 G 的 macOS 无障碍（AX）读取，只用 pyobjc ApplicationServices 的公开接口，给 MacBackend 的 ui_* 方法提供事实，判定在核心。
#   - 权限：先 AXIsProcessTrusted()（只查不弹窗）；没授权 / 绑定缺失 → UiScan(unavailable)，观察只出 OCR 候选，不失败。
#   - 找窗口：公开接口没有"窗口号 → AX 窗口"的映射（_AXUIElementGetWindow 是私有 API，不用），按 AXPosition + AXSize 和 CG 外框比，
#     多个再比标题；0 个或多个都不猜 → window_unmatched。
#   - 遍历：广度优先，深度 ≤ AX_TREE_DEPTH_MAX_COUNT、读的节点 ≤ AX_TREE_NODES_MAX_COUNT、总时间 ≤ AX_TREE_BUDGET_SECONDS，每次 AX 消息
#     超时 AX_MESSAGING_TIMEOUT_SECONDS（对本进程全局生效）；碰到上限停下、已读的照用（truncated）。外框和可见范围（窗口与各级祖先
#     外框的交集）不相交的子树整棵跳过、不读子节点；没有外框的容器不裁剪、照常往下读。只读属性和动作名，绝不调 AXPerformAction。
#   - 值的隐私（ae 定稿）：密码框（subrole 是 kAXSecureTextFieldSubrole）一律不读 AXValue；其它控件的 AXValue 只用来判"是不是文字"，
#     读完即丢，不存、不记、不算哈希、不进结果。label 只取 AXTitle / AXDescription / AXPlaceholderValue。
#   - 写操作只有一个：清空前用 AXSelectedTextRange 全选（再读回核对）；真正的输入走键盘事件（computer_text_input）。
#   真实框架只经 computer_use_macos.load_real_macos_frameworks 拿，测试注入假 AX；改动同步 test_computer_use_macos_ax 与设计稿第 6 节。
# 模块用途: 读 macOS 窗口里的按钮、输入框等控件，给出能点、能输入的候选事实。
from __future__ import annotations

import hashlib
import json
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .screen_observation import WindowInfo
from .screen_observation_store import UiFacts
from .screen_ui_candidates import UiElement, UiScan

# 控件树广度优先遍历的最大深度（窗口本身是第 0 层；网页内容的树通常更深）
AX_TREE_DEPTH_MAX_COUNT = 16
# 一次观察最多读的控件节点数
AX_TREE_NODES_MAX_COUNT = 600
# 每条 AX 消息最多等多久（应用卡住也不会把观察拖死）
AX_MESSAGING_TIMEOUT_SECONDS = 0.5
# 一次读整棵控件树的总时间预算
AX_TREE_BUDGET_SECONDS = 2.0

# 一个矩形：全局点 (x, y, w, h)
Rect = tuple[int, int, int, int]


# 类用途: 读到的一个节点：可能是候选的控件、子节点、给子节点用的可见范围。
@dataclass(frozen=True)
class _Node:
    candidate: UiElement | None
    children: tuple[Any, ...]
    clip: Rect


# LLM: 构造时把本进程的 AX 消息超时设成 AX_MESSAGING_TIMEOUT_SECONDS（系统级元素上设置只影响本进程）；读失败一律当作没有。
# 类用途: AX 属性读取的薄封装（错误码不是成功就返回 None / False / 空）。
class _AxReader:
    def __init__(self, ax: Any) -> None:
        self.ax = ax
        ax.AXUIElementSetMessagingTimeout(ax.AXUIElementCreateSystemWide(), AX_MESSAGING_TIMEOUT_SECONDS)

    # 函数用途: 读一个属性，返回 (错误码, 值)。
    def read(self, element: Any, name: str) -> tuple[int, Any]:
        error, value = self.ax.AXUIElementCopyAttributeValue(element, name, None)
        return error, value

    # 函数用途: 读一个属性，失败返回 None。
    def attr(self, element: Any, name: str) -> Any:
        error, value = self.read(element, name)
        return value if error == self.ax.kAXErrorSuccess else None

    # 函数用途: 属性是否可写（不读它的值）。
    def settable(self, element: Any, name: str) -> bool:
        error, settable = self.ax.AXUIElementIsAttributeSettable(element, name, None)
        return error == self.ax.kAXErrorSuccess and bool(settable)

    # 函数用途: 控件支持的动作名（只读名字，不执行）。
    def actions(self, element: Any) -> tuple[str, ...]:
        error, names = self.ax.AXUIElementCopyActionNames(element, None)
        return tuple(str(name) for name in names or ()) if error == self.ax.kAXErrorSuccess else ()

    # 函数用途: 控件外框（全局点，取整）：AXPosition + AXSize，读不到返回 None。
    def frame(self, element: Any) -> Rect | None:
        position, size = self.attr(element, self.ax.kAXPositionAttribute), self.attr(element, self.ax.kAXSizeAttribute)
        if position is None or size is None:
            return None
        point_ok, point = self.ax.AXValueGetValue(position, self.ax.kAXValueCGPointType, None)
        size_ok, extent = self.ax.AXValueGetValue(size, self.ax.kAXValueCGSizeType, None)
        if not (point_ok and size_ok):
            return None
        return (round(point.x), round(point.y), round(extent.width), round(extent.height))


# LLM: ax 为 None 表示 ApplicationServices 绑定导入失败。info.native_id 是 (窗口号, PID)。clock 可注入（测试用假时钟）。
# 函数用途: 读一个窗口的控件树，返回候选控件与读取状态（读不到只降级，不抛错）。
def scan_window(ax: Any, info: WindowInfo, clock: Callable[[], float] = time.monotonic) -> UiScan:
    if ax is None:
        return UiScan(status="unavailable", reason="ax_unavailable")
    if not ax.AXIsProcessTrusted():
        return UiScan(status="unavailable", reason="accessibility_not_permitted")
    reader = _AxReader(ax)
    error, windows = reader.read(ax.AXUIElementCreateApplication(int(info.native_id[1])), ax.kAXWindowsAttribute)
    if error != ax.kAXErrorSuccess:
        return UiScan(status="unavailable", reason="ax_timeout" if error == ax.kAXErrorCannotComplete else "ax_failed")
    window = _match_window(reader, tuple(windows or ()), info)
    if window is None:
        return UiScan(status="unavailable", reason="window_unmatched")
    elements, truncated = _walk(reader, window, (*info.geometry.origin, *info.geometry.size), clock)
    return UiScan(tuple(elements), "truncated" if truncated else None, truncated)


# LLM: 复核用：与观察时用同一个函数算事实（role、enabled、外框、label 来源 sha、值可写、选区可写），整份相等才算没变；元素已失效
#   （读不到 role 或外框）返回 None。不读控件的值。
# 函数用途: 重新读一个控件的结构化事实。
def element_facts(ax: Any, element: Any) -> UiFacts | None:
    return _facts(_AxReader(ax), element) if ax is not None else None


# 函数用途: 控件现在有没有焦点（AXFocused）。
def element_focused(ax: Any, element: Any) -> bool:
    return ax is not None and _truthy(_AxReader(ax).attr(element, ax.kAXFocusedAttribute))


# LLM: 唯一的 AX 写操作：把 AXSelectedTextRange 设成 (0, 字符数)，再读回核对；字符数是计数，不是内容。任一步失败返回 False。
# 函数用途: 在已拿到焦点的输入框里全选原有内容（随后的键盘输入会替换选区）。
def select_all(ax: Any, element: Any) -> bool:
    reader = _AxReader(ax)
    count = reader.attr(element, ax.kAXNumberOfCharactersAttribute)
    if not isinstance(count, int) or isinstance(count, bool):
        return False
    wanted = ax.AXValueCreate(ax.kAXValueCFRangeType, (0, count))
    if ax.AXUIElementSetAttributeValue(element, ax.kAXSelectedTextRangeAttribute, wanted) != ax.kAXErrorSuccess:
        return False
    current = reader.attr(element, ax.kAXSelectedTextRangeAttribute)
    selected_ok, selected = ax.AXValueGetValue(current, ax.kAXValueCFRangeType, None) if current is not None else (False, None)
    return bool(selected_ok) and (selected.location, selected.length) == (0, count)


# 函数用途: 在应用的窗口里找外框与 CG 窗口相同的那一个（多个再比标题）；找不到或分不清返回 None。
def _match_window(reader: _AxReader, windows: tuple[Any, ...], info: WindowInfo) -> Any:
    rect = (*info.geometry.origin, *info.geometry.size)
    matches = [window for window in windows if reader.frame(window) == rect]
    if len(matches) > 1 and info.title:
        matches = [window for window in matches if reader.attr(window, reader.ax.kAXTitleAttribute) == info.title]
    return matches[0] if len(matches) == 1 else None


# LLM: 广度优先；碰到节点数或时间上限立刻停（返回已读到的），深度上限只是不再往下读（继续读同层其它节点），都记 truncated 原因。
# 函数用途: 遍历控件树，返回 (候选控件列表, 截断原因或 None)。
def _walk(reader: _AxReader, root: Any, window: Rect, clock: Callable[[], float]) -> tuple[list[UiElement], str | None]:
    deadline, queue = clock() + AX_TREE_BUDGET_SECONDS, deque([(root, 0, window)])
    elements: list[UiElement] = []
    visited, truncated = 0, None
    while queue:
        stop = "ax_nodes_limit" if visited >= AX_TREE_NODES_MAX_COUNT else "ax_budget_exceeded" if clock() > deadline else None
        if stop:
            return elements, stop
        element, depth, clip = queue.popleft()
        visited += 1
        node = _read_node(reader, element, clip)
        if node is None:
            continue
        elements += [node.candidate] if node.candidate is not None else []
        if node.children and depth >= AX_TREE_DEPTH_MAX_COUNT:
            truncated = truncated or "ax_depth_limit"
            continue
        queue.extend((child, depth + 1, node.clip) for child in node.children)
    return elements, truncated


# LLM: 外框面积为正时与 clip 求交，交不到 → None（整棵子树跳过，子节点不读）；没有外框的容器沿用 clip、不出候选。
# 函数用途: 读一个节点：可见范围、是否候选、子节点。
def _read_node(reader: _AxReader, element: Any, clip: Rect) -> _Node | None:
    frame = reader.frame(element)
    framed = frame is not None and frame[2] > 0 and frame[3] > 0
    visible = _intersect(frame, clip) if framed else clip
    if visible is None:
        return None
    candidate = _candidate(reader, element, visible) if framed else None
    return _Node(candidate, tuple(reader.attr(element, reader.ax.kAXChildrenAttribute) or ()), visible)


# LLM: 先用最便宜的两项（动作名、值可写）筛掉大多数节点，再读其余事实。密码框先判 subrole，绝不读它的 AXValue；
#   其它控件的 AXValue 只判类型，读完即丢。
# 函数用途: 一个节点若能按下或可编辑，组装成候选控件；否则 None。
def _candidate(reader: _AxReader, element: Any, visible: Rect) -> UiElement | None:
    ax = reader.ax
    pressable = ax.kAXPressAction in reader.actions(element)
    if not pressable and not reader.settable(element, ax.kAXValueAttribute):
        return None
    facts = _facts(reader, element)
    if facts is None:
        return None
    secure = reader.attr(element, ax.kAXSubroleAttribute) == ax.kAXSecureTextFieldSubrole
    editable = facts.value_settable and (secure or isinstance(reader.attr(element, ax.kAXValueAttribute), str))
    if not (pressable or editable):
        return None
    return UiElement(element, _label_sources(reader, element)[0], visible, facts, pressable, editable, secure)


# 函数用途: 一个控件的结构化事实；读不到 role 或外框（元素已失效）返回 None。
def _facts(reader: _AxReader, element: Any) -> UiFacts | None:
    ax = reader.ax
    role, frame = reader.attr(element, ax.kAXRoleAttribute), reader.frame(element)
    if not isinstance(role, str) or frame is None:
        return None
    value_settable = reader.settable(element, ax.kAXValueAttribute)
    selection_settable = value_settable and reader.settable(element, ax.kAXSelectedTextRangeAttribute)
    return UiFacts(role, _truthy(reader.attr(element, ax.kAXEnabledAttribute)), frame, _label_sources(reader, element)[1],
                   value_settable, selection_settable)


# 函数用途: (label, label 来源的 sha)：label 取 AXTitle / AXDescription / AXPlaceholderValue 第一个非空的；sha 算三者原文。
def _label_sources(reader: _AxReader, element: Any) -> tuple[str, str]:
    ax = reader.ax
    sources = [reader.attr(element, name) for name in (ax.kAXTitleAttribute, ax.kAXDescriptionAttribute, ax.kAXPlaceholderValueAttribute)]
    texts = [str(value) if isinstance(value, str) else "" for value in sources]
    label = next((text.strip() for text in texts if text.strip()), "")
    return label, hashlib.sha256(json.dumps(texts, ensure_ascii=False).encode("utf-8")).hexdigest()


# 函数用途: 两个矩形的交集；不相交返回 None。
def _intersect(a: Rect, b: Rect) -> Rect | None:
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    return (x0, y0, x1 - x0, y1 - y0) if x1 > x0 and y1 > y0 else None


# 函数用途: AX 布尔属性是否为真（缺失或不是布尔 / 数字都算假）。
def _truthy(value: object) -> bool:
    return isinstance(value, (bool, int)) and bool(value)


__all__ = [
    "AX_MESSAGING_TIMEOUT_SECONDS", "AX_TREE_BUDGET_SECONDS", "AX_TREE_DEPTH_MAX_COUNT", "AX_TREE_NODES_MAX_COUNT",
    "element_facts", "element_focused", "scan_window", "select_all",
]
