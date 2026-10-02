"""J16 片 G 核心（无桌面依赖）：控件 + OCR 合并去重、动作判定、控件候选的结构化复核、type_into_candidate 的顺序与边界。

锁定（ae 设计评审 2026-10-02）：
1. OCR 区域一半以上面积落在控件里就算重复，归给最里层控件；label 控件优先，其次被吸收的 OCR 文字，再次 role；key 带 ax: / ocr: 前缀。
2. 可点 = enabled 且（能按下或可编辑）；可输入 = 可编辑且不是密码框；禁用控件不出候选。
3. 控件候选不比像素摘要（输入框光标会闪），比 role、enabled、外框、label 来源 sha、值是否可写；OCR 候选照旧比像素。
4. 输入：≤500 字、超了拒绝不截断，控制字符（含换行、Tab）与孤立代理码点拒绝，全部在任何副作用之前；点击 → 等焦点 → 全选 → 打字，
   焦点确认后、真正打字前各看一次取消；点击之后的失败带 clicked=true；不读回控件的值，application_verified=False。
5. 控件树读不全 / 读不到只降级：结果顶层 ui_tree{status, reason}，OCR 候选照出。
"""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.plugin_observation import parse_observation
from agent_py_agent.agent.tooling import screen_observation
from agent_py_agent.agent.tooling.screen_observation import (
    TYPE_INTO_TEXT_MAX_CHARS,
    ObservationError,
    TextRegion,
)
from agent_py_agent.agent.tooling.screen_observation_store import UiFacts, WindowGeometry
from agent_py_agent.agent.tooling.screen_ui_candidates import (
    CLICK_ACTION,
    OCR_ROLE,
    TYPE_ACTION,
    UiElement,
    UiScan,
    merge_candidates,
    ui_region,
)
from agent_py_agent.tests.test_screen_observation_core import (
    GEOMETRY,
    _Backend,
    _buffer,
    _context,
    _meta,
    _observer,
)

RETINA = WindowGeometry(origin=(100, 50), size=(320, 200), scale=(2.0, 2.0))
BOUNDS = (640, 400)


# 函数用途: 造一个控件（visible 是全局点）；facts 由参数推出，可编辑的控件值可写、能全选。
def _control(native, label, visible, **flags):
    flags = {"role": "AXButton", "pressable": True, "editable": False, "secure": False, "enabled": True, **flags}
    facts = UiFacts(flags["role"], flags["enabled"], visible, f"sha:{label}", flags["editable"], flags["editable"])
    return UiElement(native, label, visible, facts, flags["pressable"], flags["editable"], flags["secure"])


# ---------------------------------------------------------------------------
# 合并去重（纯计算）
# ---------------------------------------------------------------------------

def test_ui_region_converts_global_points_to_capture_pixels_and_clips():
    assert ui_region((110, 60, 20, 10), RETINA, BOUNDS) == (20, 20, 40, 20), "（全局点 - 原点）× scale"
    assert ui_region((400, 230, 100, 100), RETINA, BOUNDS) == (600, 360, 40, 40), "裁到截图范围"
    assert ui_region((0, 0, 50, 40), RETINA, BOUNDS) is None, "完全在窗口外"


def test_ocr_text_inside_a_control_is_absorbed_by_the_innermost_control():
    group = _control("g", "", (100, 50, 200, 100), role="AXGroup")
    button = _control("b", "", (110, 60, 40, 20))
    merged = merge_candidates([group, button], [("提交", (24, 24, 20, 10)), ("外面", (500, 300, 20, 10))], RETINA, BOUNDS)
    assert [(c.key, c.label) for c in merged] == [("ax:1", "AXGroup"), ("ax:2", "提交"), ("ocr:1", "外面")], "落在两个控件里归最里层"
    assert merged[0].element is group and merged[1].element is button and merged[2].element is None
    edge = merge_candidates([button], [("一半", (90, 30, 20, 10)), ("不到一半", (91, 50, 20, 10))], RETINA, BOUNDS)
    assert [(c.key, c.label) for c in edge] == [("ax:1", "一半"), ("ocr:1", "不到一半")], "恰好一半算重复、少一点不算（按 OCR 面积，不用 IoU）"


def test_label_prefers_the_control_then_absorbed_ocr_then_the_role():
    named = _control("n", "确定", (110, 60, 40, 20))
    unnamed = _control("u", "", (110, 100, 40, 20), role="AXTextField", editable=True, pressable=False)
    bare = _control("x", "  ", (200, 100, 40, 20), role="AXCheckBox")
    ocr = [("OK", (24, 24, 20, 10)), ("搜索", (24, 104, 20, 10)), ("框里", (40, 104, 20, 10))]
    labels = [(c.key, c.label) for c in merge_candidates([named, unnamed, bare], ocr, RETINA, BOUNDS)]
    assert labels == [("ax:1", "确定"), ("ax:2", "搜索 框里"), ("ax:3", "AXCheckBox")], "控件自己的 > 被吸收的 OCR（按顺序拼）> role"


def test_actions_come_from_structured_facts_and_disabled_controls_are_dropped():
    controls = [
        _control("press", "按钮", (110, 60, 20, 10)),
        _control("edit", "邮箱", (140, 60, 20, 10), role="AXTextField", pressable=False, editable=True),
        _control("secret", "密码", (170, 60, 20, 10), role="AXTextField", pressable=False, editable=True, secure=True),
        _control("off", "禁用", (200, 60, 20, 10), enabled=False),
        _control("inert", "静态", (230, 60, 20, 10), pressable=False),
    ]
    merged = merge_candidates(controls, [("禁用", (200, 20, 40, 20))], RETINA, BOUNDS)
    assert [(c.label, c.actions) for c in merged] == [
        ("按钮", (CLICK_ACTION,)), ("邮箱", (CLICK_ACTION, TYPE_ACTION)), ("密码", (CLICK_ACTION,)), ("禁用", (CLICK_ACTION,)),
    ], "密码框不给输入；禁用、既不能按也不能编辑的控件不出候选（它的 OCR 文字照常单列）"
    assert merged[-1].role == OCR_ROLE and merged[-1].element is None


def test_keys_order_cap_and_odd_roles():
    controls = [_control(f"c{i}", f"按钮{i}", (100 + i % 30 * 10, 50 + i // 30 * 10, 8, 8)) for i in range(60)]
    controls.append(_control("odd", "怪", (390, 230, 8, 8), role="AX Weird Role"))
    ocr = [(f"字{i}", (600, 4 * i, 30, 4)) for i in range(10)]
    merged = merge_candidates(controls, ocr, RETINA, BOUNDS)
    assert len(merged) == 64 and [c.key for c in merged[:2]] == ["ax:1", "ax:2"] and merged[60].key == "ocr:1" and merged[-1].key == "ocr:4"
    assert all(c.label != "怪" for c in merged), "role 不合宿主短标识规则的控件丢掉，免得整份观察被拒"
    same = merge_candidates([_control("outer", "外", (110, 60, 20, 10)), _control("inner", "内", (110, 60, 20, 10))], [], RETINA, BOUNDS)
    assert [(c.key, c.label) for c in same] == [("ax:1", "内")], "像素区域完全相同时留后读到的（更深的）"


# ---------------------------------------------------------------------------
# 观察与复核（假后端）
# ---------------------------------------------------------------------------

FIELD = (120, 100, 200, 30)  # 窗口里的输入框（全局点；窗口原点 (100, 50)、缩放 1）


# 类用途: 带控件树的假后端：控件事实可改、焦点与全选结果可控，记录点击 / 全选 / 打字 / 删除的先后。
class _UiBackend(_Backend):
    ui_candidates_supported = True

    def __init__(self):
        super().__init__()
        self.button = _control("btn", "提交", (120, 80, 60, 18))
        self.field = _control("field", "邮箱", FIELD, role="AXTextField", pressable=False, editable=True)
        self.ui = UiScan((self.button, self.field))
        self.current = {"btn": self.button.facts, "field": self.field.facts}
        self.focus_ok, self.select_ok, self.type_error, self.events = True, True, None, []
        self.ocr_rows = [TextRegion("提交", (20, 30, 60, 18)), TextRegion("取消", (120, 30, 60, 18))]

    def click(self, x, y):
        super().click(x, y)
        self.events.append("click")

    def ui_facts(self, native):
        return self.current.get(native)

    def ui_focused(self, native):
        self.events.append("focus")
        return self.focus_ok

    def ui_select_all(self, native):
        self.events.append("select_all")
        return self.select_ok

    def type_text(self, text):
        if self.type_error:
            raise self.type_error
        self.events.append(("type", text))

    def press_delete(self):
        self.events.append("delete")


# 函数用途: 带控件树的观察核心，返回 (backend, observer, 观察结果)。
def _ui_observer():
    backend, observer = _observer(_UiBackend())
    return backend, observer, observer.observe()


# 函数用途: 某个 key 的 _meta。
def _meta_for(result, key):
    keys = [c["key"] for c in result["my_agent_observation"]["candidates"]]
    return _meta(result, keys.index(key))


def test_observe_merges_controls_and_ocr_into_one_payload_the_host_accepts():
    backend, observer, result = _ui_observer()
    rows = result["my_agent_observation"]["candidates"]
    assert [(c["key"], c["role"], c["label"], c["actions"]) for c in rows] == [
        ("ax:1", "AXButton", "提交", ["click_candidate"]), ("ax:2", "AXTextField", "邮箱", ["click_candidate", "type_into_candidate"]),
        ("ocr:1", "ocr_text", "取消", ["click_candidate"]),
    ], "OCR 的“提交”落在按钮里被吸收"
    assert rows[1]["region"] == [20, 50, 200, 30] and "ui_tree" not in result, "读全了就不带 ui_tree"
    record = parse_observation(json.loads(json.dumps(result["my_agent_observation"])), _context(
        action_tools={"click_candidate": "mcp__computer_use__click_candidate", "type_into_candidate": "mcp__computer_use__type_into_candidate"}))
    assert [c.key for c in record.candidates] == ["ax:1", "ax:2", "ocr:1"], "宿主接受带冒号的 key 与系统角色"
    snapshot = observer.store.find(result["window"], result["generation"])
    assert snapshot.candidates["ax:2"].grid is None and snapshot.candidates["ax:2"].facts == backend.field.facts
    assert snapshot.candidates["ocr:1"].grid is not None and snapshot.candidates["ocr:1"].facts is None


@pytest.mark.parametrize("scan,expected", [
    (UiScan((), "truncated", "ax_nodes_limit"), {"status": "truncated", "reason": "ax_nodes_limit"}),
    (UiScan((), "unavailable", "accessibility_not_permitted"), {"status": "unavailable", "reason": "accessibility_not_permitted"}),
    (RuntimeError("AX 挂了"), {"status": "unavailable", "reason": "ui_scan_failed"}),
    ("不是 UiScan", {"status": "unavailable", "reason": "ui_scan_failed"}),
], ids=["truncated", "not_permitted", "backend_raises", "bad_shape"])
def test_an_incomplete_tree_degrades_to_ocr_with_a_top_level_ui_tree(scan, expected):
    backend, observer = _observer(_UiBackend())
    if isinstance(scan, Exception):
        backend.ui_scan = lambda info: (_ for _ in ()).throw(scan)
    else:
        backend.ui = scan
    result = observer.observe()
    assert result["ui_tree"] == expected and [c["key"] for c in result["my_agent_observation"]["candidates"]] == ["ocr:1", "ocr:2"]
    assert "ui_tree" not in result["my_agent_observation"] and "ui_tree" not in result["frame"], "不进观察载荷与 frame"


def test_control_candidates_ignore_pixels_but_recheck_every_structured_fact():
    backend, observer, result = _ui_observer()
    meta = _meta_for(result, "ax:2")
    backend.buffers[0x1a] = _buffer(patches={(20, 50, 4, 30): (0, 0, 0)})  # 输入框里多了一根光标
    assert observer.click_candidate(meta)["clicked"]["key"] == "ax:2", "控件候选不比像素：光标闪动不误判过期"
    for change in ({"role": "AXStaticText"}, {"enabled": False}, {"frame": (121, 100, 200, 30)}, {"label_sha": "sha:改名"},
                   {"value_settable": False}):
        backend.current["field"] = replace(backend.field.facts, **change)
        with pytest.raises(ObservationError) as info:
            observer.click_candidate(meta)
        assert info.value.code == "stale", change
    backend.current.pop("field")
    with pytest.raises(ObservationError) as gone:
        observer.click_candidate(meta)
    assert gone.value.code == "stale" and backend.clicks == [(220, 115)], "控件没了也是 stale；只点过一次"


# ---------------------------------------------------------------------------
# type_into_candidate
# ---------------------------------------------------------------------------

def test_typing_clicks_waits_for_focus_selects_then_types_and_never_reads_back():
    backend, observer, result = _ui_observer()
    typed = observer.type_into_candidate(_meta_for(result, "ax:2"), "张三@例子.cn", True)
    assert backend.events == ["click", "focus", "select_all", ("type", "张三@例子.cn")]
    assert typed == {"typed": {"window": result["window"], "generation": result["generation"], "key": "ax:2"}, "characters": 8,
                     "clear_existing": True, "application_verified": False}, "结果不回显文字、不读回控件的值"
    backend.events.clear()
    observer.type_into_candidate(_meta_for(result, "ax:2"), "", True)
    assert backend.events == ["click", "focus", "select_all", "delete"], "空文字 + 清空 = 全选后按删除键"
    backend.events.clear()
    observer.type_into_candidate(_meta_for(result, "ax:2"), "x" * TYPE_INTO_TEXT_MAX_CHARS)
    assert backend.events == ["click", "focus", ("type", "x" * TYPE_INTO_TEXT_MAX_CHARS)], "不清空就不全选；正好 500 字可以"


@pytest.mark.parametrize("text,clear", [
    ("x" * (TYPE_INTO_TEXT_MAX_CHARS + 1), False), ("第一行\n第二行", False), ("a\tb", False), ("a\x00b", False), ("a\x7fb", False),
    ("a\u0085b", False), ("a\ud800b", False), ("", False), (None, False), ("ok", "yes"),
], ids=["too_long", "newline", "tab", "nul", "del", "c1", "lone_surrogate", "empty_without_clear", "not_text", "clear_not_bool"])
def test_bad_text_is_rejected_before_any_side_effect(text, clear):
    backend, observer, result = _ui_observer()
    listed = backend.list_calls
    with pytest.raises(ObservationError) as info:
        observer.type_into_candidate(_meta_for(result, "ax:2"), text, clear)
    assert info.value.code == "invalid_arguments" and not info.value.clicked, "超长拒绝而不截断"
    assert backend.list_calls == listed and backend.events == [], "没复核、没点击、没输入"


def test_only_editable_controls_accept_text_and_clearing_needs_a_selectable_field():
    backend, observer, result = _ui_observer()
    for key in ("ax:1", "ocr:1"):
        with pytest.raises(ObservationError) as info:
            observer.type_into_candidate(_meta_for(result, key), "x")
        assert info.value.code == "invalid_arguments", key
    no_select = replace(backend.field.facts, selection_settable=False)
    backend.field = replace(backend.field, facts=no_select)
    backend.ui, backend.current["field"] = UiScan((backend.button, backend.field)), no_select
    fresh = observer.observe()
    with pytest.raises(ObservationError) as info:
        observer.type_into_candidate(_meta_for(fresh, "ax:2"), "x", True)
    assert info.value.code == "clear_unsupported" and backend.events == [], "清空做不到就一下都不点"
    assert observer.type_into_candidate(_meta_for(fresh, "ax:2"), "x")["typed"]["key"] == "ax:2", "不清空照常输入"


def test_failures_after_the_click_say_clicked_and_never_type(monkeypatch):
    monkeypatch.setattr(screen_observation, "TYPE_INTO_FOCUS_WAIT_SECONDS", 0.01)
    backend, observer, result = _ui_observer()
    backend.focus_ok = False
    with pytest.raises(ObservationError) as info:
        observer.type_into_candidate(_meta_for(result, "ax:2"), "x", True)
    assert (info.value.code, info.value.clicked) == ("focus_not_acquired", True) and "已点击、未输入" in str(info.value)
    assert backend.events[0] == "click" and set(backend.events[1:]) == {"focus"}, "没拿到焦点：不全选、不打字"
    backend.focus_ok, backend.select_ok = True, False
    backend.events.clear()
    with pytest.raises(ObservationError) as info:
        observer.type_into_candidate(_meta_for(result, "ax:2"), "x", True)
    assert (info.value.code, info.value.clicked) == ("clear_failed", True) and backend.events == ["click", "focus", "select_all"]
    backend.select_ok, backend.type_error = True, RuntimeError("键盘事件建不出来")
    with pytest.raises(ObservationError) as info:
        observer.type_into_candidate(_meta_for(result, "ax:2"), "x")
    assert (info.value.code, info.value.clicked) == ("type_failed", True)


@pytest.mark.parametrize("cancel_at,clicked,events", [
    ("list", False, []),
    ("focus", True, ["click", "focus"]),
    ("select_all", True, ["click", "focus", "select_all"]),
], ids=["during_recheck", "after_focus", "after_select_all"])
def test_cancellation_is_checked_before_the_click_after_focus_and_right_before_typing(cancel_at, clicked, events):
    backend, observer, result = _ui_observer()
    meta, flag = _meta_for(result, "ax:2"), {"set": False}
    hooks = {"list": "list_windows", "focus": "ui_focused", "select_all": "ui_select_all"}
    original = getattr(backend, hooks[cancel_at])

    def trip(*args):
        flag["set"] = True  # 宿主恰好在这一步期间取消
        return original(*args)

    setattr(backend, hooks[cancel_at], trip)
    with pytest.raises(ObservationError) as info:
        observer.type_into_candidate(meta, "不该打进去", True, cancelled=lambda: flag["set"])
    assert (info.value.code, info.value.clicked) == ("cancelled", clicked)
    assert backend.events == events and all(not isinstance(event, tuple) for event in backend.events), "取消之后一个字都不打"


@pytest.mark.parametrize("hook,code,events", [
    ("ui_focused", "focus_not_acquired", ["click"]),
    ("ui_select_all", "clear_failed", ["click", "focus"]),
], ids=["focus_raises", "select_all_raises"])
def test_unstructured_errors_after_the_click_still_report_clicked(hook, code, events):
    backend, observer, result = _ui_observer()

    def boom(native):
        raise TypeError("pyobjc 转换失败")

    setattr(backend, hook, boom)
    with pytest.raises(ObservationError) as info:
        observer.type_into_candidate(_meta_for(result, "ax:2"), "x", True)
    assert (info.value.code, info.value.clicked) == (code, True) and "已点击、未输入" in str(info.value), "不能变成笼统失败"
    assert backend.events == events, "一个字都没打"


def test_reading_control_facts_that_raises_during_recheck_is_stale_and_does_not_click():
    backend, observer, result = _ui_observer()
    meta = _meta_for(result, "ax:2")
    backend.ui_facts = lambda native: (_ for _ in ()).throw(TypeError("pyobjc 转换失败"))
    with pytest.raises(ObservationError) as info:
        observer.type_into_candidate(meta, "x")
    assert (info.value.code, info.value.clicked) == ("stale", False) and backend.events == [], "确认不了控件还是原样就不点"
    backend.ui_facts = lambda native: (_ for _ in ()).throw(ObservationError("accessibility_not_permitted", "没授权"))
    with pytest.raises(ObservationError) as passthrough:
        observer.click_candidate(meta)
    assert passthrough.value.code == "accessibility_not_permitted" and backend.clicks == [], "结构化错误原样透传"
