# LLM: PERF-R4-W2 回归：TUI 完全空闲（无前台/后台任务、后台进程、观察页）时通知、业务名册、路由观察按2倍退避到
#   tui_idle_poll_max_seconds（默认30秒），仍保留前两拍健康空闲5秒；有后台进程或名册内有运行中代理时沿原8秒上限；任何变化复位。
# 模块用途: 固定空闲轮询封顶来自配置键，不触网、不起 Gateway。
from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.cli.chat_parts import tui_background_agents, tui_threading
from agent_py_agent.cli.chat_parts.tui_threading import (
    TUI_BACKGROUND_NOTICE_FAILURE_MAX_SECONDS,
    TUI_BACKGROUND_NOTICE_IDLE_INTERVAL_SECONDS,
    TUI_BACKGROUND_NOTICE_INTERVAL_SECONDS,
)


# 类用途: 只记录等待秒数、达到次数后退出的停止信号替身。
class _Stop:
    def __init__(self, limit):
        self.delays, self.limit = [], limit

    def is_set(self):
        return len(self.delays) >= self.limit

    def wait(self, delay):
        self.delays.append(delay)
        return self.is_set()


# 函数用途: 带指定空闲上限配置的 agent 替身。
def _agent(cap=30.0):
    return SimpleNamespace(config=SimpleNamespace(tui_idle_poll_max_seconds=cap))


# 函数用途: 构造只含游标字段与活动谓词的 TUI 运行时替身。
def _runtime():
    return SimpleNamespace(conversation_reset_generation=0, background_message_cursor=0, background_event_stream_id="s",
                           background_message_thread_id="t", has_active_background_task=lambda: False)


# 函数用途: 跑通知循环（轮询替换为恒成功）并返回每拍等待秒数。
def _notice_delays(monkeypatch, agent, *, navigation=None, limit=7):
    monkeypatch.setattr(tui_threading, "_consume_background_notices", lambda *args, **kwargs: True)
    stop = _Stop(limit)
    tui_threading._run_background_notice_loop(stop, agent, "sess", _runtime(), [None], navigation, [False])
    return stop.delays


def test_config_default_and_fallback_cap():
    assert AgentConfig().tui_idle_poll_max_seconds == 30.0
    assert tui_threading._idle_poll_cap(object()) == TUI_BACKGROUND_NOTICE_FAILURE_MAX_SECONDS
    assert tui_threading._idle_poll_cap(_agent(4.0)) == TUI_BACKGROUND_NOTICE_FAILURE_MAX_SECONDS
    assert tui_threading._idle_poll_cap(_agent(60.0)) == 60.0


def test_fully_idle_notice_loop_backs_off_to_configured_cap(monkeypatch):
    idle = TUI_BACKGROUND_NOTICE_IDLE_INTERVAL_SECONDS
    assert _notice_delays(monkeypatch, _agent()) == [idle, idle, 10.0, 20.0, 30.0, 30.0, 30.0]


def test_background_processes_keep_original_cap(monkeypatch):
    fast, cap = TUI_BACKGROUND_NOTICE_INTERVAL_SECONDS, TUI_BACKGROUND_NOTICE_FAILURE_MAX_SECONDS
    processes = SimpleNamespace(snapshot=lambda: SimpleNamespace(background_process_count=1, background_watch_active=False))
    delays = _notice_delays(monkeypatch, _agent(), navigation=processes)
    assert delays == [fast, fast] + [min(cap, fast * 2 ** n) for n in range(1, 6)]


def test_route_observation_backs_off_while_unchanged(monkeypatch):
    settings = [{"enabled": True, "source": "thread", "revision": 1}] * 4 + [{"enabled": False, "source": "thread", "revision": 2}] * 2
    seen = []
    monkeypatch.setattr("agent_py_agent.cli.chat_parts.control_runtime.chat_route_setting",
                        lambda agent, session_id: settings[min(len(seen), len(settings) - 1)])
    navigation = SimpleNamespace(root_runtime=SimpleNamespace(session_id="sess"), update_route_setting=seen.append)
    stop = _Stop(6)
    tui_background_agents.start_route_observation(_agent(), navigation, stop).join(timeout=5)
    assert stop.delays == [5, 10, 20, 30, 5, 10]


def test_business_roster_idle_cap_only_without_running_agents(monkeypatch):
    def run(state):
        # 类用途: 返回固定名册页的业务客户端替身。
        class _Client:
            def call(self, action, params):
                return {"ok": True, "data": {"items": [{"agent_id": "a1", "work_state": state}], "coverage": "complete"}}

        monkeypatch.setattr(tui_background_agents, "business_client_for_agent", lambda agent, conversation_id: _Client())
        navigation = SimpleNamespace(root_runtime=SimpleNamespace(session_id="sess"), update_business_agents=lambda p: None)
        stop = _Stop(5)
        tui_background_agents.start_business_observation(_agent(), navigation, stop).join(timeout=5)
        return stop.delays

    assert run("idle") == [5, 10, 20, 30, 30]
    cap = TUI_BACKGROUND_NOTICE_FAILURE_MAX_SECONDS
    assert run("running") == [5, cap, cap, cap, cap]
