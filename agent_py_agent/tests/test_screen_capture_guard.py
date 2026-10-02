"""真实屏幕防线自检（J16 片 E，ae 设计评审定的两层）。

1. 本进程：conftest 已在会话级把两个桌面后端唯一的真实库加载入口换成直接失败（X11 的在 Linux 车道容器里不换）。不注入假库时，
   列窗口、上层矩形、截图、点击、点击权限确认，以及片 G 的读控件树、复核控件、查焦点、全选、输入、删除键都当场失败；失败是 BaseException，观察核心截图外层的 except Exception 吞不掉，
   不会变成 capture_failed。真实库的模块名先换成一碰就炸的绊线：防线哪天退化了，这里也只会碰到绊线，不会真的点到屏幕。
2. 子进程：tests/ 下（含子目录、辅助模块）同时“打开屏幕观察”和“拉起 MCP 子进程”的文件必须带 Linux 车道跳过标记；
   样例文本覆盖违规与不违规。样例用防线模块里的常量拼出来，本文件源码里不出现标记字面量，免得扫描到自己。
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

from agent_py_agent.agent.tooling.computer_use_macos import MacBackend
from agent_py_agent.agent.tooling.computer_use_x11 import X11Backend
from agent_py_agent.agent.tooling.screen_observation import ScreenObserver, WindowInfo
from agent_py_agent.agent.tooling.screen_observation_store import WindowGeometry
from agent_py_agent.tests._screen_capture_guard import (
    LANE_SKIP_MARKER,
    MCP_SPAWN_MARKERS,
    OBSERVATION_SWITCH_MARKERS,
    RealScreenAccessForbidden,
    lane_marker_violations,
    needs_lane_marker,
)

_REAL_LIBRARIES = ("Quartz", "ScreenCaptureKit", "ApplicationServices", "mss", "pyautogui", "Xlib.display")
_INFO = WindowInfo(native_id=(101, 500), title="t", geometry=WindowGeometry((0, 0), (10, 10), (1.0, 1.0)), viewable=True,
                   hidden=False, desktop=None, current_desktop=None)
_IN_LANE = os.environ.get(LANE_SKIP_MARKER) == "1"


# 类用途: 碰到了真实桌面库（防线已退化）；与 RealScreenAccessForbidden 区分开，测试照样失败。
class _RealLibraryTouched(BaseException):
    pass


# 类用途: 一碰就炸的假模块：放在真实库的模块名上，取任何公开属性都失败，真实库永远不会被调用。
class _Tripwire(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        raise _RealLibraryTouched(f"{self.__name__}.{name}")


@pytest.fixture
def tripwires(monkeypatch):
    for name in _REAL_LIBRARIES:
        monkeypatch.setitem(sys.modules, name, _Tripwire(name))


@pytest.mark.parametrize("call", [
    lambda: MacBackend().list_windows(),
    lambda: MacBackend().above_rects((101, 500)),
    lambda: MacBackend().capture(_INFO),
    lambda: MacBackend().click(1, 2),
    lambda: MacBackend().ensure_click_permitted(),
    lambda: MacBackend().ui_scan(_INFO),
    lambda: MacBackend().ui_facts(object()),
    lambda: MacBackend().ui_focused(object()),
    lambda: MacBackend().ui_select_all(object()),
    lambda: MacBackend().type_text("x"),
    lambda: MacBackend().press_delete(),
], ids=["list_windows", "above_rects", "capture", "click", "ensure_click_permitted", "ui_scan", "ui_facts", "ui_focused",
        "ui_select_all", "type_text", "press_delete"])
def test_real_macos_frameworks_are_never_loaded_under_pytest(call, tripwires):
    with pytest.raises(RealScreenAccessForbidden):
        call()


@pytest.mark.skipif(_IN_LANE, reason="车道容器里 X11 后端本来就用 Xvfb 假桌面")
@pytest.mark.parametrize("call", [
    lambda: X11Backend().list_windows(),
    lambda: X11Backend().above_rects(0x10),
    lambda: X11Backend().capture(_INFO),
    lambda: X11Backend().click(1, 2),
    lambda: ScreenObserver(X11Backend())._capture(_INFO),
], ids=["list_windows", "above_rects", "capture", "click", "core_capture"])
def test_real_x11_libraries_are_never_loaded_outside_the_lane(call, tripwires):
    with pytest.raises(RealScreenAccessForbidden):
        call()


def test_the_guard_is_not_swallowed_into_a_structured_capture_failure(tripwires):
    observer = ScreenObserver(MacBackend())
    with pytest.raises(RealScreenAccessForbidden):
        observer.observe()
    with pytest.raises(RealScreenAccessForbidden):
        observer._capture(_INFO)
    assert not issubclass(RealScreenAccessForbidden, Exception), "继承 BaseException，产品代码的 except Exception 吞不掉"


def test_no_test_file_spawns_the_observation_adapter_outside_the_lane():
    assert lane_marker_violations(Path(__file__).parent) == []


def test_lane_marker_scan_covers_subdirectories_and_helper_modules(tmp_path):
    body = f"{OBSERVATION_SWITCH_MARKERS[0]}\n{MCP_SPAWN_MARKERS[2]}\n"
    (tmp_path / "test_tools").mkdir()
    (tmp_path / "test_tools" / "test_nested.py").write_text(body, encoding="utf-8")
    (tmp_path / "_spawn_helper.py").write_text(body, encoding="utf-8")
    (tmp_path / "test_in_lane.py").write_text(body + f"os.environ.get('{LANE_SKIP_MARKER}')\n", encoding="utf-8")
    assert lane_marker_violations(tmp_path) == ["_spawn_helper.py", "test_tools/test_nested.py"], "子目录和辅助模块都要扫到"


def test_lane_marker_rule_on_sample_sources():
    switch, spawn = OBSERVATION_SWITCH_MARKERS[2], MCP_SPAWN_MARKERS[0]
    assert needs_lane_marker(f"servers = {switch}(servers, enabled=True)\nclients = {spawn}(registry, servers)\n"), "开观察又拉子进程，没车道标记"
    assert not needs_lane_marker(f"{switch}(servers)\n{spawn}(registry, servers)\nos.environ.get('{LANE_SKIP_MARKER}')\n")
    assert not needs_lane_marker(f"{switch}(servers, enabled=True)\n"), "只在进程内打开观察，不拉子进程"
    assert not needs_lane_marker(f"{spawn}(registry, servers)\n"), "拉子进程但没打开观察"
    assert all(needs_lane_marker(f"{marker}\n{MCP_SPAWN_MARKERS[1]}\n") for marker in OBSERVATION_SWITCH_MARKERS)
    assert all(needs_lane_marker(f"{OBSERVATION_SWITCH_MARKERS[0]}\n{marker}\n") for marker in MCP_SPAWN_MARKERS)
