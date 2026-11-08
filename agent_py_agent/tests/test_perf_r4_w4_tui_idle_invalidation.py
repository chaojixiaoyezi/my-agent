# LLM: PERF-R4-W4 回归：路由（每5秒）与业务名册（5~30秒）的周期观察在投影未变化时不再调用根 store.invalidate()；
#   该调用会清掉整个对话视图的渲染缓存键、触发主线程整屏重绘并把刷新线程拉回 0.25 秒节奏，是空闲 TUI 的主要重绘来源。
# 模块用途: 只计数根 store 失效次数，不起 Gateway、不渲染终端。
from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.cli.chat_parts.tui_agent_navigation import TuiAgentNavigationState
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime


# 函数用途: 建立导航并返回根 store 的失效计数列表。
def _navigation():
    navigation = TuiAgentNavigationState(TuiRuntime("thread-main"))
    calls: list[int] = []
    navigation.root_runtime.store.subscribe(lambda: calls.append(1))
    return navigation, calls


# 函数用途: 只带名册投影字段的替身。
def _projection(state="idle", count=1, running=0):
    return SimpleNamespace(count=count, running_count=running, coverage="complete", read_denied=False,
                           rows=[{"agent_id": "a1", "work_state": state, "space_id": "sp", "thread_id": "a1"}])


def test_unchanged_route_setting_does_not_invalidate_root_view():
    navigation, calls = _navigation()
    setting = {"enabled": True, "source": "thread", "revision": 1}
    navigation.update_route_setting(setting)
    navigation.update_route_setting(dict(setting))
    navigation.update_route_setting(dict(setting))
    assert len(calls) == 1 and navigation.route_enabled is True
    navigation.update_route_setting({"enabled": False, "source": "thread", "revision": 2})
    assert len(calls) == 2 and navigation.route_enabled is False
    navigation.update_route_setting({"enabled": None, "source": "unknown", "revision": None})
    assert len(calls) == 3 and navigation.route_enabled is None


def test_unchanged_business_roster_does_not_invalidate_root_view():
    navigation, calls = _navigation()
    navigation.update_business_agents(_projection())
    navigation.update_business_agents(_projection())
    navigation.update_business_agents(_projection())
    assert len(calls) == 1
    navigation.update_business_agents(_projection("running", running=1))
    assert len(calls) == 2
