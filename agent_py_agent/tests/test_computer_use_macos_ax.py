"""J16 片 G：macOS 无障碍（AX）读取与输入，全用假 AX / 假 Quartz / 假 ScreenCaptureKit，不碰真实屏幕、不触发任何授权弹窗。

锁定（ae 设计评审 2026-10-02）：
1. 先 AXIsProcessTrusted（只查不弹窗），没授权 / 绑定缺失只降级为 OCR 候选；AX 窗口按外框、再按标题对上 CG 窗口，0 个或多个不猜。
2. 广度优先，深度 16、节点 600、总预算 2 秒、单条消息超时 0.5 秒；外框和可见范围不相交的子树整棵跳过、不读子节点；只读，唯一的写是全选。
3. 密码框一律不读 AXValue、不给输入；任何控件的值都不进结果、快照、哈希，label 只取 AXTitle / AXDescription / AXPlaceholderValue。
4. 输入走 Quartz Unicode 键盘路径（假的 type_text 记录），清空用 AX 全选，空文字时按删除键（键码 51）。
"""
from __future__ import annotations

import json

import pytest

from agent_py_agent.agent.tooling.computer_use_macos import (
    MACOS_DELETE_KEY_CODE_PROTOCOL_VALUE,
    MacBackend,
    MacFrameworks,
)
from agent_py_agent.agent.tooling.computer_use_macos_ax import (
    AX_MESSAGING_TIMEOUT_SECONDS,
    AX_TREE_DEPTH_MAX_COUNT,
    AX_TREE_NODES_MAX_COUNT,
    element_facts,
    element_focused,
    scan_window,
    select_all,
)
from agent_py_agent.agent.tooling.screen_observation import (
    ObservationError,
    ScreenObserver,
    WindowInfo,
)
from agent_py_agent.agent.tooling.screen_observation_store import (
    WindowGeometry,
    WindowInstanceRegistry,
)
from agent_py_agent.tests._fake_macos_ax import FakeAx, ax_element
from agent_py_agent.tests.test_computer_use_macos import (
    FORM,
    FORM_RECT,
    _Grabber,
    _Kit,
    _ocr,
    _Quartz,
)
from agent_py_agent.tests.test_screen_observation_core import _meta

PID = 500
INFO = WindowInfo(native_id=(FORM, PID), title="表单", geometry=WindowGeometry(FORM_RECT[:2], FORM_RECT[2:], (2.0, 2.0)),
                  viewable=True, hidden=False, desktop=None, current_desktop=None)
SECRET, EMAIL, NOTE = "hunter2-秘密", "user@example.com", "一段说明文字"


# 函数用途: 在假 AX 里挂一个和 CG 表单窗口对得上的 AX 窗口：按钮、输入框、密码框、静态文字、窗口外的分组（带子节点）、没有外框的分组。
def _install_form(ax, *extra):
    button = ax_element("button", "AXButton", (120, 210, 40, 12), pressable=True, title="提交")
    field = ax_element("field", "AXTextField", (110, 240, 100, 20), editable=True, placeholder="邮箱", value=EMAIL, characters=16,
                       focused=True)
    secret = ax_element("secret", "AXTextField", (110, 265, 100, 20), editable=True, subrole="AXSecureTextField", description="密码",
                        value=SECRET, focused=True)
    note = ax_element("note", "AXStaticText", (170, 210, 60, 12), value=NOTE)
    hidden = ax_element("hidden-child", "AXButton", (2000, 2000, 10, 10), pressable=True, title="看不见")
    outside = ax_element("outside", "AXGroup", (1000, 1000, 50, 50), children=[hidden])
    group = ax_element("group", "AXGroup", None, children=[button, field, secret, note, outside, *extra])
    ax.install_windows(PID, ax_element("window", "AXWindow", FORM_RECT, children=[group], title="表单"))
    return {"button": button, "field": field, "secret": secret}


# ---------------------------------------------------------------------------
# 权限与窗口匹配
# ---------------------------------------------------------------------------

def test_without_permission_nothing_is_read_and_nothing_prompts():
    ax = FakeAx(trusted=False)
    _install_form(ax)
    assert scan_window(ax, INFO).status == "unavailable" and scan_window(ax, INFO).reason == "accessibility_not_permitted"
    assert ax.reads == [] and ax.writes == [], "没授权就一个属性都不读"
    missing = scan_window(None, INFO)
    assert (missing.status, missing.reason, missing.elements) == ("unavailable", "ax_unavailable", ())


def test_the_ax_window_is_matched_by_frame_then_title_and_never_guessed():
    ax = FakeAx()
    assert scan_window(ax, INFO).reason == "window_unmatched", "应用没有窗口"
    twin = ax_element("twin", "AXWindow", FORM_RECT, title="别的窗口")
    real = ax_element("real", "AXWindow", FORM_RECT, title="表单", children=[ax_element("b", "AXButton", (120, 210, 40, 12), pressable=True)])
    ax.install_windows(PID, twin, real)
    assert [e.native.name for e in scan_window(ax, INFO).elements] == ["b"], "外框相同再比标题"
    ax.install_windows(PID, twin, ax_element("twin2", "AXWindow", FORM_RECT, title="别的窗口"))
    assert scan_window(ax, INFO).reason == "window_unmatched", "外框对上、标题都对不上"
    ax.install_windows(PID, real, ax_element("clone", "AXWindow", FORM_RECT, title="表单"))
    assert scan_window(ax, INFO).reason == "window_unmatched", "外框和标题都一样的两个窗口分不清，不猜第一个"
    ax.install_windows(PID, real)
    ax.windows_error = FakeAx.kAXErrorCannotComplete
    assert scan_window(ax, INFO).reason == "ax_timeout"
    ax.windows_error = -25200
    assert scan_window(ax, INFO).reason == "ax_failed"


# ---------------------------------------------------------------------------
# 读控件树
# ---------------------------------------------------------------------------

def test_controls_are_read_breadth_first_with_structured_facts_and_no_writes():
    ax = FakeAx()
    nodes = _install_form(ax)
    scan = scan_window(ax, INFO)
    assert scan.status is None and [e.native.name for e in scan.elements] == ["button", "field", "secret"], "静态文字不能按也不能编辑，不出候选"
    button, field, secret = scan.elements
    assert (button.pressable, button.editable, button.label) == (True, False, "提交")
    assert (field.editable, field.secure, field.label, field.visible) == (True, False, "邮箱", (110, 240, 100, 20))
    assert (secret.editable, secret.secure, secret.label) == (True, True, "密码")
    assert (field.facts.role, field.facts.enabled, field.facts.frame, field.facts.value_settable, field.facts.selection_settable) == \
        ("AXTextField", True, (110, 240, 100, 20), True, True)
    assert element_facts(ax, nodes["field"]) == field.facts, "复核与观察用同一套事实"
    assert ax.timeouts and set(ax.timeouts) == {AX_MESSAGING_TIMEOUT_SECONDS} and ax.writes == [], "设了消息超时；遍历只读"


def test_secret_values_are_never_read_and_no_control_value_reaches_any_output():
    ax = FakeAx()
    nodes = _install_form(ax)
    scan_window(ax, INFO)
    assert ("secret", "AXValue") not in ax.reads, "密码框一律不读 AXValue"
    assert ("note", "AXValue") not in ax.reads and ("field", "AXValue") in ax.reads, "只有可写的普通输入框读值、且只判类型"
    observer, _quartz, _kit, _typed = _observer(ax)
    result = observer.observe()
    snapshot = observer.store.find(result["window"], result["generation"])
    dumped = json.dumps(result, ensure_ascii=False) + repr(snapshot)
    assert all(value not in dumped for value in (SECRET, EMAIL, NOTE)), "控件的值不进结果、快照、哈希"
    nodes["field"].attrs["AXValue"] = "用户刚改的内容"
    assert observer.click_candidate(_meta_for(result, "ax:2"))["clicked"]["key"] == "ax:2", "值变了不算控件变了（事实里没有值）"


def test_subtrees_outside_the_visible_area_are_skipped_without_reading_their_children():
    ax = FakeAx()
    inside = ax_element("in-scroll", "AXButton", (110, 260, 100, 20), pressable=True, title="半露")
    below = ax_element("below-scroll", "AXButton", (110, 280, 100, 20), pressable=True, title="滚出去了", children=[ax_element("deep", "AXButton", (1, 1, 1, 1))])
    scroll = ax_element("scroll", "AXScrollArea", (110, 230, 100, 40), children=[inside, below])
    _install_form(ax, scroll)
    scan = scan_window(ax, INFO)
    assert "hidden-child" not in [name for name, _ in ax.reads] and ("outside", "AXChildren") not in ax.reads, "窗口外的子树整棵跳过"
    assert ("below-scroll", "AXChildren") not in ax.reads and "deep" not in [name for name, _ in ax.reads], "滚出可见范围的也跳过"
    clipped = {e.native.name: e.visible for e in scan.elements}
    assert clipped["in-scroll"] == (110, 260, 100, 10) and "below-scroll" not in clipped, "可见范围 = 窗口 ∩ 各级祖先外框"


# 函数用途: 造一条 depth 层深的分组链，链底挂一个按钮；返回 (链顶, 按钮)。
def _chain(name, depth):
    button = ax_element(f"{name}-button", "AXButton", (120, 210, 10, 10), pressable=True, title=name)
    node = button
    for level in range(depth - 1):
        node = ax_element(f"{name}-g{level}", "AXGroup", (100, 200, 160, 100), children=[node])
    return node


def test_traversal_stops_at_the_depth_node_and_time_limits():
    ax = FakeAx()
    ax.install_windows(PID, ax_element("window", "AXWindow", FORM_RECT, children=[_chain("ok", AX_TREE_DEPTH_MAX_COUNT), _chain("deep", AX_TREE_DEPTH_MAX_COUNT + 1)]))
    scan = scan_window(ax, INFO)
    assert [e.label for e in scan.elements] == ["ok"] and (scan.status, scan.reason) == ("truncated", "ax_depth_limit"), "第 16 层读、第 17 层不读"
    wide = [ax_element(f"b{i}", "AXButton", (100 + i % 16 * 10, 200 + i // 16 % 10 * 10, 8, 8), pressable=True, title=f"b{i}") for i in range(700)]
    ax.install_windows(PID, ax_element("window", "AXWindow", FORM_RECT, children=wide))
    scan = scan_window(ax, INFO)
    assert len(scan.elements) == AX_TREE_NODES_MAX_COUNT - 1 and scan.reason == "ax_nodes_limit", "连窗口一共读 600 个节点"
    assert [e.label for e in scan.elements[:3]] == ["b0", "b1", "b2"], "已读到的照用、顺序不乱"
    ticks = iter(range(100))
    scan = scan_window(ax, INFO, clock=lambda: next(ticks) * 0.5)
    assert scan.status == "truncated" and scan.reason == "ax_budget_exceeded" and 0 < len(scan.elements) < 10


def test_recheck_focus_and_select_all_helpers():
    ax = FakeAx()
    nodes = _install_form(ax)
    nodes["field"].invalid = True
    assert element_facts(ax, nodes["field"]) is None and element_facts(None, nodes["button"]) is None, "元素失效 / 绑定缺失 → 没有事实"
    assert element_focused(ax, nodes["secret"]) and not element_focused(ax, nodes["button"]) and not element_focused(None, nodes["secret"])
    nodes["field"].invalid = False
    assert select_all(ax, nodes["field"]) and ax.writes == [("field", "AXSelectedTextRange")], "唯一的写：选区"
    nodes["field"].reject_writes = True
    assert not select_all(ax, nodes["field"]), "设不上就算失败"
    assert not select_all(ax, nodes["button"]), "读不到字符数就不全选"


# ---------------------------------------------------------------------------
# MacBackend 端到端（假框架）
# ---------------------------------------------------------------------------

# 类用途: 能造、能发键盘事件的假 Quartz（记录键码与按下 / 抬起）。
class _KeyQuartz(_Quartz):
    kCGHIDEventTap = 0

    def __init__(self):
        super().__init__()
        self.keys = []

    def CGEventCreateKeyboardEvent(self, source, keycode, down):
        return (keycode, down)

    def CGEventPost(self, tap, event):
        self.keys.append(event)


# 函数用途: 组装带假 AX 的 macOS 后端与观察核心；返回 (observer, quartz, kit, typed)。
def _observer(ax):
    quartz, kit, typed = _KeyQuartz(), _Kit(), []
    backend = MacBackend(MacFrameworks(quartz, kit, lambda: _Grabber([]), lambda x, y: typed.append(("click", x, y)), (15, 1), ax, typed.append))
    backend._ocr = type("_Ocr", (), {"read": staticmethod(_ocr)})()
    return ScreenObserver(backend, registry=WindowInstanceRegistry(boot="mac0"), clock=lambda: 1790000000.5), quartz, kit, typed


# 函数用途: 某个 key 的 _meta。
def _meta_for(result, key):
    return _meta(result, [c["key"] for c in result["my_agent_observation"]["candidates"]].index(key))


def test_the_mac_backend_offers_typing_only_for_plain_text_fields_and_types_through_the_keyboard_path():
    ax = FakeAx()
    _install_form(ax)
    observer, quartz, _kit, typed = _observer(ax)
    assert observer.supports_ui_candidates is True
    result = observer.observe()
    rows = {c["key"]: (c["role"], c["label"], c["actions"], c["region"]) for c in result["my_agent_observation"]["candidates"]}
    assert rows == {"ax:1": ("AXButton", "提交", ["click_candidate"], [40, 20, 80, 24]),
                    "ax:2": ("AXTextField", "邮箱", ["click_candidate", "type_into_candidate"], [20, 80, 200, 40]),
                    "ax:3": ("AXTextField", "密码", ["click_candidate"], [20, 130, 200, 40])}, "OCR 的“提交”被按钮吸收；密码框不给输入"
    assert "ui_tree" not in result
    typed_result = observer.type_into_candidate(_meta_for(result, "ax:2"), "你好", True)
    assert typed == [("click", 160, 250), "你好"] and typed_result["application_verified"] is False
    assert ax.writes == [("field", "AXSelectedTextRange")], "先全选，再由键盘输入替换"
    with pytest.raises(ObservationError) as secret:
        observer.type_into_candidate(_meta_for(result, "ax:3"), "猜猜看")
    assert secret.value.code == "invalid_arguments" and typed == [("click", 160, 250), "你好"], "密码框第一期不给输入"
    observer.type_into_candidate(_meta_for(result, "ax:2"), "", True)
    assert quartz.keys == [(MACOS_DELETE_KEY_CODE_PROTOCOL_VALUE, True), (MACOS_DELETE_KEY_CODE_PROTOCOL_VALUE, False)], "清空 = 全选 + 删除键"


def test_without_accessibility_the_mac_backend_still_observes_with_ocr_only():
    ax = FakeAx(trusted=False)
    _install_form(ax)
    observer, _quartz, _kit, typed = _observer(ax)
    result = observer.observe()
    assert result["ui_tree"] == {"status": "unavailable", "reason": "accessibility_not_permitted"}
    assert [c["key"] for c in result["my_agent_observation"]["candidates"]] == ["ocr:1"] and ax.reads == []
    with pytest.raises(ObservationError) as denied:
        observer.click_candidate(_meta_for(result, "ocr:1"))
    assert denied.value.code == "accessibility_not_permitted" and typed == []


@pytest.mark.parametrize("change", [
    {"AXPosition": "moved"}, {"AXEnabled": False}, {"AXRole": "AXStaticText"}, {"AXPlaceholderValue": "改了名字"}, {"invalid": True},
], ids=["moved", "disabled", "role", "renamed", "gone"])
def test_a_control_that_changed_after_observe_makes_typing_stale_without_clicking(change):
    ax = FakeAx()
    nodes = _install_form(ax)
    observer, _quartz, _kit, typed = _observer(ax)
    meta = _meta_for(observer.observe(), "ax:2")
    field = nodes["field"]
    if "invalid" in change:
        field.invalid = True
    elif "AXPosition" in change:
        field.attrs["AXPosition"] = ax_element("x", "AXGroup", (111, 240, 1, 1)).attrs["AXPosition"]
    else:
        field.attrs.update(change)
    with pytest.raises(ObservationError) as info:
        observer.type_into_candidate(meta, "x")
    assert info.value.code == "stale" and not info.value.clicked and typed == []


def test_a_pyobjc_conversion_error_after_the_click_is_reported_as_clicked_not_typed(monkeypatch):
    ax = FakeAx()
    _install_form(ax)
    observer, _quartz, _kit, typed = _observer(ax)
    result = observer.observe()
    monkeypatch.setattr(ax, "AXValueCreate", lambda kind, payload: (_ for _ in ()).throw(TypeError("could not convert")))
    with pytest.raises(ObservationError) as info:
        observer.type_into_candidate(_meta_for(result, "ax:2"), "你好", True)
    assert (info.value.code, info.value.clicked) == ("clear_failed", True) and "已点击、未输入" in str(info.value)
    assert typed == [("click", 160, 250)], "点过了、一个字都没打"
