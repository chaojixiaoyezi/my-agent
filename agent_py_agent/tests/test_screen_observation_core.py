"""J16 片 B 核心（无桌面依赖）：窗口实例登记与快照环、候选区域像素摘要口径、观察载荷能过宿主校验、动作前五项复核的放行与各项不通过。"""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.plugin_observation import ObservationHostContext, parse_observation
from agent_py_agent.agent.tooling.screen_observation import (
    CLICK_ACTION,
    SCREEN_REGION_CAPTURE,
    WINDOW_IMAGE_CAPTURE,
    ObservationError,
    ScreenCapture,
    ScreenObserver,
    TextRegion,
    WindowInfo,
    click_point,
    sanitize_label,
)
from agent_py_agent.agent.tooling.screen_observation_store import (
    SNAPSHOT_RETAIN_COUNT,
    TRACKED_WINDOWS_MAX_COUNT,
    CandidateSnapshot,
    SnapshotStore,
    WindowGeometry,
    WindowInstanceRegistry,
    WindowSnapshot,
)
from agent_py_agent.agent.tooling.screen_region_digest import (
    DIGEST_CHANGED_CELLS_MAX_COUNT,
    DIGEST_GRID_COLUMNS_COUNT,
    DIGEST_GRID_ROWS_COUNT,
    PixelBuffer,
    grid_digest,
    grid_unchanged,
    region_grid,
)
from agent_py_agent.agent.tooling.screen_ui_candidates import UiScan

GEOMETRY = WindowGeometry(origin=(100, 50), size=(320, 200), scale=(1, 1))


# 函数用途: 造一张纯色窗口截图；patches 是 {(x, y, w, h): (r, g, b)} 的覆盖块。
def _buffer(width=320, height=200, base=(240, 240, 240), patches=None) -> PixelBuffer:
    rows = bytearray(bytes(base) * (width * height))
    for (x, y, w, h), color in (patches or {}).items():
        for yy in range(y, y + h):
            rows[(yy * width + x) * 3:(yy * width + x + w) * 3] = bytes(color) * w
    return PixelBuffer(width, height, bytes(rows))


SUBMIT, CANCEL = (20, 30, 60, 18), (120, 30, 60, 18)
DEFAULT_PATCHES = {SUBMIT: (30, 30, 30), CANCEL: (30, 30, 30)}


# 类用途: 假桌面后端：可变的窗口列表、每窗截图、上层矩形、OCR 结果与点击记录；默认不给控件候选（片 G 的用例另配控件树）。
class _Backend:
    ui_candidates_supported = False

    def __init__(self):
        self.windows = [WindowInfo(native_id=0x1a, title="表单\x07 窗口", geometry=GEOMETRY, viewable=True, hidden=False, desktop=0, current_desktop=0)]
        self.buffers = {0x1a: _buffer(patches=DEFAULT_PATCHES)}
        self.above = {0x1a: []}
        self.ocr_rows = [TextRegion("提交", SUBMIT), TextRegion("取消", CANCEL)]
        self.clicks = []
        self.fail_capture = self.fail_ocr = False
        self.capture_kind, self.fallback_reason, self.capture_error = SCREEN_REGION_CAPTURE, None, None
        self.click_permission_error, self.list_calls = None, 0
        self.ui = UiScan()

    def list_windows(self):
        self.list_calls += 1
        return list(self.windows)

    def above_rects(self, native_id):
        return list(self.above.get(native_id, []))

    def capture(self, info):
        if self.fail_capture:
            raise RuntimeError("no display")
        if self.capture_error is not None:
            raise self.capture_error
        return ScreenCapture(self.buffers[info.native_id], self.capture_kind, self.fallback_reason)

    def ui_scan(self, info):
        return self.ui

    def ocr(self, buffer):
        if self.fail_ocr:
            raise RuntimeError("onnx")
        return list(self.ocr_rows)

    def click(self, x, y):
        self.clicks.append((x, y))

    def ensure_click_permitted(self):
        if self.click_permission_error is not None:
            raise self.click_permission_error

    # 函数用途: 替换唯一窗口的字段。
    def replace_window(self, **changes):
        self.windows[0] = WindowInfo(**{**self.windows[0].__dict__, **changes})


def _observer(backend=None, clock=lambda: 1790000000.5):
    backend = backend or _Backend()
    return backend, ScreenObserver(backend, registry=WindowInstanceRegistry(boot="b00t"), clock=clock)


def _context(**overrides) -> ObservationHostContext:
    values = dict(run_id="run-1", task_id="task-1", operation_id="op-1", activation_id="mcp:computer_use:abc", provider_id="mcp:computer_use",
                  tool_name="mcp__computer_use__observe_window", target_kind="window", max_candidates=64,
                  action_tools={CLICK_ACTION: "mcp__computer_use__click_candidate"})
    values.update(overrides)
    return ObservationHostContext(**values)


def _meta(result, index=0):
    observation = result["my_agent_observation"]
    return {"version": "1", "observation_id": "obs-" + "0" * 24, "key": observation["candidates"][index]["key"],
            "target": dict(observation["target"])}


# ---------------------------------------------------------------------------
# 登记与快照环
# ---------------------------------------------------------------------------

def test_registry_numbers_instances_and_treats_reappearing_ids_as_new():
    registry = WindowInstanceRegistry(boot="b00t")
    assert registry.observe_listing([7, 8]) == [] and registry.ref_for(7) == "win:b00t:1" and registry.ref_for(8) == "win:b00t:2"
    assert registry.observe_listing([8]) == ["win:b00t:1"], "消失的窗口被遗忘并返回其引用"
    assert registry.observe_listing([8, 7]) == [] and registry.ref_for(7) == "win:b00t:3", "同一 XID 再出现算新实例"
    assert registry.native_for("win:b00t:3") == 7 and registry.native_for("win:b00t:1") is None and registry.instance_of(9) is None


def test_store_keeps_a_bounded_ring_per_window_and_evicts_by_recency():
    store = SnapshotStore()
    for index in range(SNAPSHOT_RETAIN_COUNT + 1):
        generation = store.next_generation("win:b00t:1")
        store.record(WindowSnapshot("win:b00t:1", generation, 1, 0.0, GEOMETRY, 0, False))
    assert store.find("win:b00t:1", "b00t-1-1") is None and store.find("win:b00t:1", "b00t-1-5") is not None, "超过保留代数的最早一代被挤掉"
    for index in range(2, TRACKED_WINDOWS_MAX_COUNT + 2):
        ref = f"win:b00t:{index}"
        store.record(WindowSnapshot(ref, store.next_generation(ref), index, 0.0, GEOMETRY, 0, False))
    assert "win:b00t:1" not in store.tracked() and len(store.tracked()) == TRACKED_WINDOWS_MAX_COUNT, "窗口数超限按最近采样淘汰"
    store.forget(["win:b00t:3"])
    assert store.find("win:b00t:3", "b00t-3-1") is None and store.next_generation("win:b00t:3") == "b00t-3-1", "忘掉后代次从头计"


# ---------------------------------------------------------------------------
# 摘要口径
# ---------------------------------------------------------------------------

def test_region_grid_is_deterministic_and_tolerates_a_caret_but_not_content_changes():
    before = region_grid(_buffer(patches=DEFAULT_PATCHES), SUBMIT)
    assert len(before) == DIGEST_GRID_COLUMNS_COUNT * DIGEST_GRID_ROWS_COUNT and all(level < 16 for level in before)
    assert region_grid(_buffer(patches=DEFAULT_PATCHES), SUBMIT) == before and len(grid_digest(before)) == 16
    dim = dict(DEFAULT_PATCHES)
    dim[(22, 31, 1, 16)] = (50, 50, 50)  # 一条很淡的细竖线：那一列每格只动不到两级
    assert grid_unchanged(before, region_grid(_buffer(patches=dim), SUBMIT)), "淡光标在容差内"
    bright = dict(DEFAULT_PATCHES)
    bright[(22, 31, 1, 16)] = (120, 120, 120)  # 一条亮的细竖线：缩成 16×8 后整列 8 格都动 ≥2 级，超过 4 格
    assert not grid_unchanged(before, region_grid(_buffer(patches=bright), SUBMIT)), "亮光标会判变（ae 记下的已知风险，片 C 统计误判率，现在不调容差）"
    replaced = dict(DEFAULT_PATCHES)
    replaced[SUBMIT] = (240, 240, 240)  # 文字没了
    after = region_grid(_buffer(patches=replaced), SUBMIT)
    assert not grid_unchanged(before, after) and sum(abs(a - b) >= 2 for a, b in zip(before, after)) > DIGEST_CHANGED_CELLS_MAX_COUNT
    assert region_grid(_buffer(), (0, 0, 3, 2)) == bytes([15] * 128), "比格子还小的区域退化成重复像素"
    with pytest.raises(ValueError):
        region_grid(_buffer(), (300, 190, 40, 20))


# ---------------------------------------------------------------------------
# 观察
# ---------------------------------------------------------------------------

def test_observe_builds_a_payload_the_host_accepts_and_records_a_snapshot():
    backend, observer = _observer()
    result = observer.observe()
    assert result["window"] == "win:b00t:1" and result["generation"] == "b00t-1-1" and result["candidate_count"] == 2
    assert result["title"] == "表单 窗口" and result["frame"]["occluded"] is False and result["frame"]["captured_at"] == 1790000000.5
    payload = result["my_agent_observation"]
    assert payload["target"] == {"ref": "win:b00t:1", "generation": "b00t-1-1"}
    assert [c["key"] for c in payload["candidates"]] == ["ocr:1", "ocr:2"] and payload["candidates"][0]["region"] == [20, 30, 60, 18]
    record = parse_observation(json.loads(json.dumps(payload)), _context())
    assert record.candidates[0].region == (20, 30, 60, 18) and record.frame["space"] == "screen_points"
    assert observer.store.find("win:b00t:1", "b00t-1-1").candidates["ocr:2"].region == CANCEL
    second = observer.observe("win:b00t:1")
    assert second["generation"] == "b00t-1-2", "同一窗口每次采样代次递增"
    assert click_point(GEOMETRY, SUBMIT) == (150, 89)


def test_observe_reports_structured_failures_and_never_invents_candidates():
    backend, observer = _observer()
    backend.ocr_rows = []
    plain = observer.observe()
    assert plain["candidate_count"] == 0 and "my_agent_observation" not in plain, "没有候选就不造候选"
    backend.ocr_rows = [TextRegion("\x01\x02", SUBMIT), TextRegion("x" * 200, (-5, -5, 10, 10)), TextRegion("零面积", (10, 10, 0, 5)), TextRegion("越界", (300, 190, 40, 40))]
    payload = observer.observe()["my_agent_observation"]
    assert [(c["key"], len(c["label"]), c["region"]) for c in payload["candidates"]] == [("ocr:1", 120, [0, 0, 5, 5]), ("ocr:2", 2, [300, 190, 20, 10])], \
        "key 按留下的 OCR 区域连续编号，带来源前缀"
    with pytest.raises(ObservationError) as unknown:
        observer.observe("win:b00t:9")
    assert unknown.value.code == "window_not_found"
    backend.above[0x1a] = [(100, 50, 320, 200)]
    with pytest.raises(ObservationError) as covered:
        observer.observe()
    assert covered.value.code == "occluded"
    backend.above[0x1a] = [(300, 100, 50, 50)]
    assert observer.observe()["frame"]["occluded"] is True, "部分遮挡只记事实"
    backend.above[0x1a] = []
    backend.replace_window(hidden=True)
    with pytest.raises(ObservationError) as hidden:
        observer.observe("win:b00t:1")
    assert hidden.value.code == "not_viewable"
    backend.replace_window(hidden=False)
    backend.fail_capture = True
    with pytest.raises(ObservationError) as capture:
        observer.observe()
    assert capture.value.code == "capture_failed"
    backend.fail_capture, backend.fail_ocr = False, True
    with pytest.raises(ObservationError) as ocr:
        observer.observe()
    assert ocr.value.code == "ocr_failed"
    assert sanitize_label(None) == "" and sanitize_label("  a\tb\n c ") == "a b c"


def test_observe_reports_the_capture_kind_and_a_top_level_fallback_reason_the_host_accepts():
    backend, observer = _observer()
    plain = observer.observe()
    assert plain["frame"]["capture"] == SCREEN_REGION_CAPTURE and "capture_fallback" not in plain
    backend.capture_kind, backend.fallback_reason = SCREEN_REGION_CAPTURE, "screencapturekit_timeout"
    result = observer.observe()
    assert result["capture_fallback"] == {"reason": "screencapturekit_timeout"}, "回退原因在结果顶层"
    payload = result["my_agent_observation"]
    assert "capture_fallback" not in payload and "capture_fallback" not in payload["frame"], "不进观察载荷与 frame"
    record = parse_observation(json.loads(json.dumps(payload)), _context())
    assert record.frame["capture"] == SCREEN_REGION_CAPTURE, "宿主照常接受（frame 多一个键会整份拒绝）"
    backend.capture_kind, backend.fallback_reason = WINDOW_IMAGE_CAPTURE, None
    assert observer.observe()["frame"]["capture"] == WINDOW_IMAGE_CAPTURE


def test_frame_scale_is_measured_from_the_capture_not_taken_from_the_listing():
    backend, observer = _observer()
    backend.buffers[0x1a] = _buffer(640, 400, patches={(40, 60, 120, 36): (30, 30, 30)})
    backend.ocr_rows = [TextRegion("提交", (40, 60, 120, 36))]
    result = observer.observe()
    assert result["frame"]["scale"] == [2.0, 2.0] and result["frame"]["size"] == [320, 200], "列表给的是 1，截图是 2 倍像素"
    clicked = observer.click_candidate(_meta(result))
    assert clicked["point"] == [150, 89] and backend.clicks == [(150, 89)], "点击点 = 原点 + 区域中心 / 截图 scale"
    backend.buffers[0x1a] = _buffer(640, 300)
    with pytest.raises(ObservationError) as uneven:
        observer.observe()
    assert uneven.value.code == "capture_failed", "宽高两个方向的像素/点比对不上"


def test_non_integer_scale_still_lets_the_host_accept_a_candidate_touching_the_edge():
    backend, observer = _observer()
    backend.replace_window(geometry=WindowGeometry((100, 50), (71, 71), (1, 1)))
    edge = (94, 112, 30, 12)  # 贴着右下边缘：x+w = y+h = 124
    backend.buffers[0x1a] = _buffer(124, 124, patches={edge: (30, 30, 30)})
    backend.ocr_rows = [TextRegion("确定", edge)]
    assert 124 / 71 * 71 < 124, "前提：原始比值乘回来比像素数小一点"
    result = observer.observe()
    frame = result["frame"]
    assert all(size * scale >= 124 for size, scale in zip(frame["size"], frame["scale"])), "两轴都保证 点数 × scale ≥ 像素数"
    assert all(abs(scale - 124 / 71) < 1e-12 for scale in frame["scale"])
    record = parse_observation(json.loads(json.dumps(result["my_agent_observation"])), _context())
    assert [list(item.region) for item in record.candidates] == [[94, 112, 30, 12]], "宿主照常接受贴边候选，不整份拒绝"
    assert observer.click_candidate(_meta(result))["clicked"]["key"] == "ocr:1", "复核时同一张图算出同一个 scale"


def test_click_asks_the_backend_for_click_permission_before_rechecking():
    backend, observer = _observer()
    meta = _meta(observer.observe())
    backend.list_calls = 0
    backend.click_permission_error = ObservationError("accessibility_not_permitted", "没有辅助功能权限")
    with pytest.raises(ObservationError) as denied:
        observer.click_candidate(meta)
    assert denied.value.code == "accessibility_not_permitted"
    assert backend.list_calls == 0 and backend.clicks == [], "复核之前就拒绝：不重新列窗口、不点击"
    backend.click_permission_error = None
    assert observer.click_candidate(meta)["clicked"]["key"] == "ocr:1" and backend.list_calls == 1


def test_recheck_is_stale_when_the_capture_scale_changes_even_if_pixels_look_alike():
    backend, observer = _observer()
    backend.buffers[0x1a] = _buffer(640, 400)
    backend.ocr_rows = [TextRegion("提交", (40, 60, 120, 36))]
    meta = _meta(observer.observe())
    backend.buffers[0x1a] = _buffer(320, 200)  # 复核时换成名义分辨率（比如主路径超时退回区域截图）
    with pytest.raises(ObservationError) as info:
        observer.click_candidate(meta)
    assert info.value.code == "stale" and backend.clicks == [], "scale 变了，区域摘要不在同一套像素里"


def test_backend_structured_errors_pass_through_and_bare_pixel_buffers_are_rejected():
    backend, observer = _observer()
    backend.capture_error = ObservationError("screen_recording_not_permitted", "没有屏幕录制权限")
    with pytest.raises(ObservationError) as denied:
        observer.observe()
    assert denied.value.code == "screen_recording_not_permitted", "后端给的结构化码不能被吞成 capture_failed"
    backend.capture_error = None
    backend.capture = lambda info: backend.buffers[info.native_id]
    with pytest.raises(ObservationError) as bare:
        observer.observe()
    assert bare.value.code == "capture_failed", "只返回像素的旧形状不再接受"


def test_observe_picks_the_topmost_normal_visible_window_when_no_alias_is_given():
    backend, observer = _observer()
    dock = WindowInfo(native_id=0x2b, title="dock", geometry=GEOMETRY, viewable=True, hidden=False, desktop=0, current_desktop=0, normal=False)
    other_desktop = WindowInfo(native_id=0x3c, title="别处", geometry=GEOMETRY, viewable=True, hidden=False, desktop=1, current_desktop=0)
    backend.windows = [backend.windows[0], other_desktop, dock]
    backend.buffers.update({0x2b: _buffer(), 0x3c: _buffer()})
    assert observer.observe()["window"] == "win:b00t:1", "最顶的普通可见窗口是表单窗口（dock 与别的桌面不算）"


# ---------------------------------------------------------------------------
# 动作前五项复核（ae 定稿）：找不到 → not_found；找到、全过 → 放行；任一项不过 → stale 且零副作用
# ---------------------------------------------------------------------------

def test_click_requires_the_snapshot_and_key_and_passes_when_nothing_changed():
    backend, observer = _observer()
    first = observer.observe()
    meta = _meta(first)
    with pytest.raises(ObservationError) as missing:
        observer.click_candidate({**meta, "target": {"ref": "win:b00t:1", "generation": "b00t-1-9"}})
    assert missing.value.code == "not_found"
    with pytest.raises(ObservationError) as no_key:
        observer.click_candidate({**meta, "key": "ocr:9"})
    assert no_key.value.code == "not_found"
    with pytest.raises(ObservationError) as other_boot:
        observer.click_candidate({**meta, "target": {"ref": "win:other:1", "generation": "other-1-1"}})
    assert other_boot.value.code == "not_found", "别的 boot（适配器重启前）的观察在环里找不到"
    with pytest.raises(ObservationError) as no_meta:
        observer.click_candidate(None)
    assert no_meta.value.code == "missing_context" and backend.clicks == []
    for broken in ({**meta, "version": "2"}, {k: v for k, v in meta.items() if k != "version"}, {**meta, "target": "win:b00t:1"}, {**meta, "key": 1}):
        with pytest.raises(ObservationError) as bad_meta:
            observer.click_candidate(broken)
        assert bad_meta.value.code == "missing_context", broken
    assert backend.clicks == [], "上下文形状不对一律不点"
    observer.observe()  # 更新的观察不影响同一代、核对全过的候选
    clicked = observer.click_candidate(meta)
    assert clicked == {"clicked": {"window": "win:b00t:1", "generation": "b00t-1-1", "key": "ocr:1"}, "point": [150, 89]}
    assert backend.clicks == [(150, 89)]


@pytest.mark.parametrize("mutate", [
    lambda b: b.windows.clear(),
    lambda b: b.replace_window(hidden=True),
    lambda b: b.replace_window(viewable=False),
    lambda b: b.replace_window(desktop=1),
    lambda b: b.replace_window(geometry=WindowGeometry((101, 50), (320, 200), (1, 1))),
    lambda b: b.replace_window(geometry=WindowGeometry((100, 50), (320, 201), (1, 1))),
    lambda b: b.above.__setitem__(0x1a, [(140, 80, 30, 30)]),
    lambda b: b.buffers.__setitem__(0x1a, _buffer(patches={SUBMIT: (240, 240, 240), CANCEL: (30, 30, 30)})),
], ids=["window-gone", "hidden", "unmapped", "desktop", "moved", "resized", "occluded-point", "pixels"])
def test_each_failed_recheck_item_is_stale_with_zero_side_effects(mutate):
    backend, observer = _observer()
    meta = _meta(observer.observe())
    mutate(backend)
    with pytest.raises(ObservationError) as info:
        observer.click_candidate(meta)
    assert info.value.code == "stale" and backend.clicks == [], "过期候选不点击"


def test_window_that_vanished_and_came_back_with_the_same_xid_is_a_new_instance():
    backend, observer = _observer()
    meta = _meta(observer.observe())
    original = backend.windows[0]
    backend.windows.clear()
    observer._refresh_listing()  # 中间有一次采样看到它消失：环被忘掉
    backend.windows.append(original)
    with pytest.raises(ObservationError) as info:
        observer.click_candidate(meta)
    assert info.value.code == "not_found" and backend.clicks == [], "XID 复用算新实例，旧代次已不在环里"
    assert observer.observe()["window"] == "win:b00t:2"


def test_a_lookalike_window_cannot_stand_in_for_the_vanished_target():
    backend, observer = _observer()
    twin = WindowInfo(native_id=0x2b, title="孪生", geometry=GEOMETRY, viewable=True, hidden=False, desktop=0, current_desktop=0)
    backend.windows.append(twin)  # 叠放更高，几何、像素都和目标一样
    backend.buffers[0x2b] = backend.buffers[0x1a]
    meta = _meta(observer.observe("win:b00t:1"))
    backend.windows[:] = [twin]  # 目标消失，只剩孪生窗口
    with pytest.raises(ObservationError) as info:
        observer.click_candidate(meta)
    assert info.value.code == "stale" and backend.clicks == [], "实例不同就不能点，哪怕长得一模一样"


def test_pixel_changes_outside_the_candidate_region_do_not_invalidate_it():
    backend, observer = _observer()
    meta = _meta(observer.observe())  # 候选 ocr:1 = Submit
    changed = dict(DEFAULT_PATCHES)
    changed[(20, 120, 200, 30)] = (0, 0, 0)  # 状态行那块变了（在候选外框之外）
    backend.buffers[0x1a] = _buffer(patches=changed)
    assert observer.click_candidate(meta)["clicked"]["key"] == "ocr:1" and backend.clicks == [(150, 89)], "只比候选区域，不比整窗"


def test_click_checks_host_cancellation_after_recheck_and_before_clicking():
    backend, observer = _observer()
    meta = _meta(observer.observe())
    flag = {"cancelled": False}
    original_capture = backend.capture

    def capture_then_cancel(info):  # 复核的重采样期间宿主取消了
        flag["cancelled"] = True
        return original_capture(info)

    backend.capture = capture_then_cancel
    with pytest.raises(ObservationError) as info:
        observer.click_candidate(meta, cancelled=lambda: flag["cancelled"])
    assert info.value.code == "cancelled" and backend.clicks == [], "复核完、点之前再看一眼取消，已取消就不点"
    backend.capture = original_capture
    assert observer.click_candidate(meta, cancelled=lambda: False)["clicked"]["key"] == "ocr:1" and backend.clicks == [(150, 89)]


# ---------------------------------------------------------------------------
# 片 F：window 参数三种解析（空 / win: 别名 / 展示标题精确唯一匹配）与 not_found / ambiguous 的可见窗口清单（ae 定规则）
# ---------------------------------------------------------------------------

def _window(native_id, title, *, hidden=False, normal=True):
    return WindowInfo(native_id=native_id, title=title, geometry=GEOMETRY, viewable=True, hidden=hidden, desktop=0, current_desktop=0, normal=normal)


def _not_found(observer, window):
    with pytest.raises(ObservationError) as caught:
        observer.observe(window)
    return caught.value


def test_window_is_selected_by_its_displayed_title_exactly():
    backend, observer = _observer()
    backend.windows.append(_window(0x2b, "Form Window"))
    backend.buffers[0x2b] = _buffer()
    assert observer.observe("表单 窗口")["window"] == "win:b00t:1", "比的是展示形态（控制字符已清掉）"
    assert observer.observe("Form Window")["window"] == "win:b00t:2"
    for wrong in ("表单\x07 窗口", "表单", "form window", "FORM WINDOW", " Form Window", "Form Window ", "Form"):
        assert _not_found(observer, wrong).code == "window_not_found", wrong
    assert observer.observe()["title"] == "Form Window", "observe 结果里的标题就是匹配键"


def test_same_displayed_title_is_ambiguous_and_lists_both_aliases():
    backend, observer = _observer()
    backend.windows = [_window(0x1a, "A" * 70), _window(0x2b, "A" * 64 + "zzz")]
    backend.buffers[0x2b] = _buffer()
    assert observer.observe()["title"] == "A" * 64, "展示标题截到 64 字"
    error = _not_found(observer, "A" * 64)
    assert error.code == "window_ambiguous"
    assert error.details == {"windows": [{"alias": "win:b00t:2", "title": "A" * 64}, {"alias": "win:b00t:1", "title": "A" * 64}], "truncated": False}
    assert observer.observe("win:b00t:1")["window"] == "win:b00t:1", "多个同名时改用别名就行"
    assert _not_found(observer, "A" * 70).code == "window_not_found", "原始长标题不是匹配键"


def test_invisible_same_title_windows_never_match_and_are_not_listed():
    backend, observer = _observer()
    backend.windows = [_window(0x2b, "表单 窗口", hidden=True), backend.windows[0], replace(_window(0x3c, "表单 窗口"), desktop=1)]
    backend.buffers.update({0x2b: _buffer(), 0x3c: _buffer()})
    assert observer.observe("表单 窗口")["window"] == "win:b00t:2", "只有可见的那一个参与匹配，所以不算 ambiguous"
    backend.windows[1] = _window(0x1a, "表单\x07 窗口", hidden=True)
    error = _not_found(observer, "表单 窗口")
    assert error.code == "window_not_found" and error.details == {"windows": [], "truncated": False}
    assert _not_found(observer, None).code == "window_not_found" and _not_found(observer, "").details["windows"] == []
    assert _not_found(observer, "win:b00t:2").code == "not_viewable", "别名照样找得到窗口，只是不可见"


def test_alias_lookup_ignores_visibility_and_reports_not_viewable():
    backend, observer = _observer()
    backend.replace_window(hidden=True)
    with pytest.raises(ObservationError) as hidden:
        observer.observe("win:b00t:1")
    assert hidden.value.code == "not_viewable", "别名找得到窗口，只是现在不可见"


def test_not_found_listing_is_top_first_sanitized_and_capped():
    backend, observer = _observer()
    backend.windows = [_window(0x100 + index, f"窗\x07{index}\t末") for index in range(18)]
    backend.windows.append(_window(0x999, "dock", normal=False))
    error = _not_found(observer, "没有的标题")
    rows = error.details["windows"]
    assert error.code == "window_not_found" and error.details["truncated"] is True and len(rows) == 16
    assert rows[0] == {"alias": "win:b00t:18", "title": "窗17 末"} and rows[-1] == {"alias": "win:b00t:3", "title": "窗2 末"}, "顶在前、清洗过、dock 不列"
    assert all(row["alias"] == observer.registry.ref_for(backend.windows[int(row['alias'].rsplit(':', 1)[1]) - 1].native_id) for row in rows)
    backend.buffers[0x111] = _buffer()
    assert observer.observe(rows[0]["title"])["window"] == "win:b00t:18", "清单里的标题原样抄回来就能选中"


def test_titles_starting_with_the_alias_prefix_are_alias_only():
    backend, observer = _observer()
    backend.windows.append(_window(0x2b, "win:real"))
    backend.buffers[0x2b] = _buffer()
    error = _not_found(observer, "win:real")
    assert error.code == "window_not_found" and [row["title"] for row in error.details["windows"]] == ["win:real", "表单 窗口"]
    assert observer.observe("win:b00t:2")["title"] == "win:real"
