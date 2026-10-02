"""真实屏幕防线自检（J16 片 E，ae 设计评审定的两层）。

1. 本进程：conftest 已在会话级把 macOS 后端唯一的真实框架加载入口换成直接失败。不注入假框架时，列窗口、截图、点击都当场失败，
   不 import 任何系统框架；失败是 BaseException，观察核心截图外层的 except Exception 吞不掉，不会变成 capture_failed。
2. 子进程：tests/ 里同时“打开屏幕观察”和“拉起 MCP 子进程”的文件必须带 Linux 车道跳过标记；样例文本覆盖违规与不违规。
   样例用防线模块里的常量拼出来，本文件源码里不出现标记字面量，免得扫描到自己。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agent_py_agent.agent.tooling.computer_use_macos import MacBackend
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

_REAL_FRAMEWORKS = ("Quartz", "ScreenCaptureKit", "mss", "pyautogui")
_INFO = WindowInfo(native_id=(101, 500), title="t", geometry=WindowGeometry((0, 0), (10, 10), (1.0, 1.0)), viewable=True,
                   hidden=False, desktop=None, current_desktop=None)


@pytest.mark.parametrize("call", [
    lambda backend: backend.list_windows(),
    lambda backend: backend.above_rects((101, 500)),
    lambda backend: backend.capture(_INFO),
    lambda backend: backend.click(1, 2),
], ids=["list_windows", "above_rects", "capture", "click"])
def test_real_macos_frameworks_are_never_loaded_under_pytest(call):
    loaded_before = {name for name in _REAL_FRAMEWORKS if name in sys.modules}
    with pytest.raises(RealScreenAccessForbidden):
        call(MacBackend())
    assert {name for name in _REAL_FRAMEWORKS if name in sys.modules} == loaded_before, "没有 import 任何真实系统框架"


def test_the_guard_is_not_swallowed_into_a_structured_capture_failure():
    observer = ScreenObserver(MacBackend())
    with pytest.raises(RealScreenAccessForbidden):
        observer.observe()
    with pytest.raises(RealScreenAccessForbidden):
        observer._capture(_INFO)
    assert not issubclass(RealScreenAccessForbidden, Exception), "继承 BaseException，产品代码的 except Exception 吞不掉"


def test_no_test_file_spawns_the_observation_adapter_outside_the_lane():
    assert lane_marker_violations(Path(__file__).parent) == []


def test_lane_marker_rule_on_sample_sources():
    switch, spawn = OBSERVATION_SWITCH_MARKERS[2], MCP_SPAWN_MARKERS[0]
    assert needs_lane_marker(f"servers = {switch}(servers, enabled=True)\nclients = {spawn}(registry, servers)\n"), "开观察又拉子进程，没车道标记"
    assert not needs_lane_marker(f"{switch}(servers)\n{spawn}(registry, servers)\nos.environ.get('{LANE_SKIP_MARKER}')\n")
    assert not needs_lane_marker(f"{switch}(servers, enabled=True)\n"), "只在进程内打开观察，不拉子进程"
    assert not needs_lane_marker(f"{spawn}(registry, servers)\n"), "拉子进程但没打开观察"
    assert all(needs_lane_marker(f"{marker}\n{MCP_SPAWN_MARKERS[1]}\n") for marker in OBSERVATION_SWITCH_MARKERS)
    assert all(needs_lane_marker(f"{OBSERVATION_SWITCH_MARKERS[0]}\n{marker}\n") for marker in MCP_SPAWN_MARKERS)
