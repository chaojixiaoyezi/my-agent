# LLM: 屏幕观察候选里与来源无关的部分（J16 片 G，ae 定稿）：动作工具名、OCR 角色、label 清洗，以及"无障碍控件 + OCR 文字区域"
#   的合并去重。
#   - 去重：OCR 区域与某个控件区域的交集 ≥ OCR 面积的 UI_DEDUPE_OCR_INSIDE_MIN_PERCENT% 就算同一个，归给面积最小（最里层）的控件，这条
#     OCR 不再单列；两个控件换算后像素区域完全相同时留后读到的（广度优先下更深的那个）。
#   - label：后端给的控件 label（AXTitle / AXDescription / AXPlaceholderValue 第一个非空），没有就用被它吸收的 OCR 文字，
#     再没有就用 role；控件的值绝不当 label（后端根本不交出值）。
#   - 动作：可点 = enabled 且（能按下或可编辑）；可输入 = 可点、可编辑且不是密码框（密码框第一期不给输入：工具参数会原样进归档）。
#     role 不合宿主短标识规则的控件丢掉（它的 OCR 文字照常单列），免得一个怪 role 让整份观察被拒。
#   - key 带来源前缀 ax:<n> / ocr:<n>，先控件后 OCR，合计不超过 MAX_CANDIDATE_COUNT。
#   纯计算、无副作用，不 import 任何桌面库；改动同步 test_screen_ui_candidates 与设计稿第 6 节片 G 定稿。
# 模块用途: 把系统给的控件和 OCR 认出的文字合成一份不重复的候选清单，并定下每个候选能做哪些动作。
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from ..plugin_observation import MAX_CANDIDATE_COUNT, MAX_LABEL_CHARS, observation_token_ok
from .screen_observation_store import UiFacts, WindowGeometry

# 候选动作工具名（观察载荷 actions 里的提供方工具名）
CLICK_ACTION = "click_candidate"
TYPE_ACTION = "type_into_candidate"
# OCR 文字区域候选的固定 role
OCR_ROLE = "ocr_text"
# OCR 区域至少有百分之几的面积落在控件里就算同一个候选（按 OCR 面积算，不用 IoU：文字框比按钮、输入框小得多）
UI_DEDUPE_OCR_INSIDE_MIN_PERCENT = 50


# LLM: 后端给核心的一个控件：native 是后端句柄（只在适配器进程内存里，不进任何载荷）；visible 是可见外框（全局点，已和窗口、
#   各级祖先外框求交）；editable 由后端按"值可写且是文字（密码框不读值，只看可写）"判；secure 是密码框。不含控件的值。
# 类用途: 一个无障碍控件的结构化事实。
@dataclass(frozen=True)
class UiElement:
    native: object
    label: str
    visible: tuple[int, int, int, int]
    facts: UiFacts
    pressable: bool
    editable: bool
    secure: bool = False


# LLM: status 为 None 表示读全了；truncated 表示碰到上限、已读到的照用；unavailable 表示一个都没读（只出 OCR 候选）。
#   reason 是开放的短原因码，进观察结果顶层的 ui_tree{status, reason}。
# 类用途: 一次读控件树的结果。
@dataclass(frozen=True)
class UiScan:
    elements: tuple[UiElement, ...] = ()
    status: str | None = None
    reason: str | None = None


# 类用途: 合并后的一个候选（截图像素外框）；element 为 None 表示 OCR 候选。
@dataclass(frozen=True)
class MergedCandidate:
    key: str
    role: str
    label: str
    region: tuple[int, int, int, int]
    actions: tuple[str, ...]
    element: UiElement | None = None


# 类用途: 合并过程中的一个控件槽位，记下被它吸收的 OCR 文字。
@dataclass
class _ControlSlot:
    element: UiElement
    region: tuple[int, int, int, int]
    actions: tuple[str, ...]
    absorbed: list[str] = field(default_factory=list)


# 函数用途: 标签只作外部数据：空白类控制字符当空格、其它控制字符去掉、折叠空白、截到 MAX_LABEL_CHARS；空串表示丢弃。
def sanitize_label(text: object) -> str:
    kept = "".join(" " if ch in "\t\n\r\x0b\x0c" else ch for ch in str(text or "") if ch in "\t\n\r\x0b\x0c" or (ord(ch) >= 32 and ord(ch) != 127))
    return " ".join(kept.split())[:MAX_LABEL_CHARS]


# 函数用途: 一个控件能做的动作：禁用或既不能按也不能编辑 → 没有；可编辑且不是密码框 → 点击 + 输入；其余只能点击。
def ui_actions(element: UiElement) -> tuple[str, ...]:
    if not element.facts.enabled or not (element.pressable or element.editable):
        return ()
    if element.editable and not element.secure:
        return (CLICK_ACTION, TYPE_ACTION)
    return (CLICK_ACTION,)


# 函数用途: 控件可见外框（全局点）换成截图像素外框并裁到截图范围；空了返回 None。bounds 是截图 (宽, 高) 像素。
def ui_region(visible: tuple[int, int, int, int], geometry: WindowGeometry, bounds: tuple[int, int]) -> tuple[int, int, int, int] | None:
    (ox, oy), (sx, sy) = geometry.origin, geometry.scale
    x0, y0 = max(0, round((visible[0] - ox) * sx)), max(0, round((visible[1] - oy) * sy))
    x1, y1 = min(bounds[0], round((visible[0] + visible[2] - ox) * sx)), min(bounds[1], round((visible[1] + visible[3] - oy) * sy))
    return (x0, y0, x1 - x0, y1 - y0) if x1 > x0 and y1 > y0 else None


# 函数用途: inner 有多大比例的面积落在 outer 里（0..1；inner 面积为 0 时算 0）。
def inside_ratio(inner: tuple[int, int, int, int], outer: tuple[int, int, int, int]) -> float:
    width = min(inner[0] + inner[2], outer[0] + outer[2]) - max(inner[0], outer[0])
    height = min(inner[1] + inner[3], outer[1] + outer[3]) - max(inner[1], outer[1])
    area = inner[2] * inner[3]
    return width * height / area if width > 0 and height > 0 and area > 0 else 0.0


# LLM: ocr_rows 是已清洗、已裁剪的 (label, 截图像素外框)，按 OCR 给出的顺序；返回的候选顺序与 key 编号就是对外顺序。
# 函数用途: 合并控件与 OCR 文字区域，去重、定 label 和动作，返回最多 MAX_CANDIDATE_COUNT 个候选。
def merge_candidates(elements: Iterable[UiElement], ocr_rows: Iterable[tuple[str, tuple[int, int, int, int]]],
                     geometry: WindowGeometry, bounds: tuple[int, int]) -> list[MergedCandidate]:
    slots = _control_slots(elements, geometry, bounds)
    kept = [(label, region) for label, region in ocr_rows if not _absorb_into(slots, label, region)]
    merged = [MergedCandidate(f"ax:{index}", slot.element.facts.role, _control_label(slot), slot.region, slot.actions, slot.element)
              for index, slot in enumerate(slots, 1)]
    merged += [MergedCandidate(f"ocr:{index}", OCR_ROLE, label, region, (CLICK_ACTION,)) for index, (label, region) in enumerate(kept, 1)]
    return merged[:MAX_CANDIDATE_COUNT]


# 函数用途: 有动作、role 合规、换算后区域不空的控件排成槽位；区域完全相同的留后读到的那个（位置按先出现的）。
def _control_slots(elements: Iterable[UiElement], geometry: WindowGeometry, bounds: tuple[int, int]) -> list[_ControlSlot]:
    slots: dict[tuple[int, int, int, int], _ControlSlot] = {}
    for element in elements:
        actions = ui_actions(element)
        region = ui_region(element.visible, geometry, bounds) if actions and observation_token_ok(element.facts.role) else None
        if region is not None:
            slots[region] = _ControlSlot(element, region, actions)
    return list(slots.values())


# 函数用途: 这条 OCR 文字落在某个控件里就归给面积最小的那个（记下文字）并返回 True；不重复返回 False。
def _absorb_into(slots: list[_ControlSlot], label: str, region: tuple[int, int, int, int]) -> bool:
    owners = [slot for slot in slots if inside_ratio(region, slot.region) * 100 >= UI_DEDUPE_OCR_INSIDE_MIN_PERCENT]
    if not owners:
        return False
    min(owners, key=lambda slot: slot.region[2] * slot.region[3]).absorbed.append(label)
    return True


# 函数用途: 控件候选的 label：控件自己的优先，其次被吸收的 OCR 文字，再次 role。
def _control_label(slot: _ControlSlot) -> str:
    return sanitize_label(slot.element.label) or sanitize_label(" ".join(slot.absorbed)) or slot.element.facts.role


__all__ = [
    "CLICK_ACTION", "OCR_ROLE", "TYPE_ACTION", "UI_DEDUPE_OCR_INSIDE_MIN_PERCENT", "MergedCandidate", "UiElement", "UiScan",
    "inside_ratio", "merge_candidates", "sanitize_label", "ui_actions", "ui_region",
]
