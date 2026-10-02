"""J16 片 B 核心（无桌面依赖）：窗口实例登记与快照环、候选区域像素摘要口径、观察载荷能过宿主校验、动作前五项复核的放行与各项不通过。"""
from __future__ import annotations

import json

import pytest

from agent_py_agent.agent.plugin_observation import ObservationHostContext, parse_observation
from agent_py_agent.agent.tooling.screen_observation import (
    CLICK_ACTION,
    ObservationError,
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


# 类用途: 假桌面后端：可变的窗口列表、每窗截图、上层矩形、OCR 结果与点击记录。
class _Backend:
    def __init__(self):
        self.windows = [WindowInfo(native_id=0x1a, title="表单\x07 窗口", geometry=GEOMETRY, viewable=True, hidden=False, desktop=0, current_desktop=0)]
        self.buffers = {0x1a: _buffer(patches=DEFAULT_PATCHES)}
        self.above = {0x1a: []}
        self.ocr_rows = [TextRegion("提交", SUBMIT), TextRegion("取消", CANCEL)]
        self.clicks = []
        self.fail_capture = self.fail_ocr = False

    def list_windows(self):
        return list(self.windows)

    def above_rects(self, native_id):
        return list(self.above.get(native_id, []))

    def capture(self, info):
        if self.fail_capture:
            raise RuntimeError("no display")
        return self.buffers[info.native_id]

    def ocr(self, buffer):
        if self.fail_ocr:
            raise RuntimeError("onnx")
        return list(self.ocr_rows)

    def click(self, x, y):
        self.clicks.append((x, y))

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
    assert [c["key"] for c in payload["candidates"]] == ["t1", "t2"] and payload["candidates"][0]["region"] == [20, 30, 60, 18]
    record = parse_observation(json.loads(json.dumps(payload)), _context())
    assert record.candidates[0].region == (20, 30, 60, 18) and record.frame["space"] == "screen_points"
    assert observer.store.find("win:b00t:1", "b00t-1-1").candidates["t2"].region == CANCEL
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
    assert [(c["key"], len(c["label"]), c["region"]) for c in payload["candidates"]] == [("t2", 120, [0, 0, 5, 5]), ("t4", 2, [300, 190, 20, 10])]
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
        observer.click_candidate({**meta, "key": "t9"})
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
    assert clicked == {"clicked": {"window": "win:b00t:1", "generation": "b00t-1-1", "key": "t1"}, "point": [150, 89]}
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
    meta = _meta(observer.observe())  # 候选 t1 = Submit
    changed = dict(DEFAULT_PATCHES)
    changed[(20, 120, 200, 30)] = (0, 0, 0)  # 状态行那块变了（在候选外框之外）
    backend.buffers[0x1a] = _buffer(patches=changed)
    assert observer.click_candidate(meta)["clicked"]["key"] == "t1" and backend.clicks == [(150, 89)], "只比候选区域，不比整窗"


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
    assert observer.click_candidate(meta, cancelled=lambda: False)["clicked"]["key"] == "t1" and backend.clicks == [(150, 89)]
