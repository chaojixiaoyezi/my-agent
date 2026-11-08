# LLM: PERF-R2-W3 回归：TUI 无变化轮询按2倍退避、封顶复用 TUI_BACKGROUND_NOTICE_FAILURE_MAX_SECONDS；
#   连续无变化的前两拍保持原基础间隔（test_tui_threading/test_background_notice_display 锁定的健康空闲5秒），第三拍起退避；
#   前台运行、后台任务进行中或观察页打开时保持1秒，任何游标变化立即复位；名册观察同样只在投影不变时退避。
# 模块用途: 固定通知循环与后台名册观察的空转退避合同，不触网、不起 Gateway。
from __future__ import annotations

import threading
from types import SimpleNamespace

from agent_py_agent.cli.chat_parts import tui_background_agents, tui_threading
from agent_py_agent.cli.chat_parts.tui_threading import (
    TUI_BACKGROUND_NOTICE_FAILURE_MAX_SECONDS,
    TUI_BACKGROUND_NOTICE_IDLE_INTERVAL_SECONDS,
    TUI_BACKGROUND_NOTICE_INTERVAL_SECONDS,
)


# LLM: 只记录等待秒数，达到次数后让循环退出；不睡眠。
# 类用途: 替代 threading.Event 的可计数停止信号。
class _Stop:
    def __init__(self, limit):
        self.delays, self.limit = [], limit

    def is_set(self):
        return len(self.delays) >= self.limit

    def wait(self, delay):
        self.delays.append(delay)
        return self.is_set()


# 函数用途: 构造只含游标字段与活动谓词的TUI运行时替身。
def _runtime(active=False):
    return SimpleNamespace(conversation_reset_generation=0, background_message_cursor=0, background_event_stream_id="s",
                           background_message_thread_id="t", has_active_background_task=lambda: active)


# LLM: 轮询本身被替换为“成功且可选推进消息游标”，只观察循环的等待策略。
# 函数用途: 跑通知循环并返回每拍等待秒数。
def _notice_delays(monkeypatch, runtime, *, navigation=None, foreground=False, advance_on=(), limit=5):
    polls = []

    def consume(agent, session_id, tui_runtime, app_ref, seen, event_cursor, agent_navigation=None):
        polls.append(1)
        if len(polls) in advance_on:
            tui_runtime.background_message_cursor += 10
        return True

    monkeypatch.setattr(tui_threading, "_consume_background_notices", consume)
    stop = _Stop(limit)
    tui_threading._run_background_notice_loop(stop, object(), "sess", runtime, [None], navigation, [foreground])
    return stop.delays


def test_idle_and_process_only_polls_back_off_to_failure_cap(monkeypatch):
    cap, idle, fast = TUI_BACKGROUND_NOTICE_FAILURE_MAX_SECONDS, TUI_BACKGROUND_NOTICE_IDLE_INTERVAL_SECONDS, TUI_BACKGROUND_NOTICE_INTERVAL_SECONDS
    # 前两次无变化仍按原基线（保持既有健康空闲 5 秒合同），第三次起倍增并封顶。
    assert _notice_delays(monkeypatch, _runtime()) == [idle, idle] + [min(cap, idle * 2)] * 3
    processes = SimpleNamespace(snapshot=lambda: SimpleNamespace(background_process_count=2, background_watch_active=False))
    assert _notice_delays(monkeypatch, _runtime(), navigation=processes) == [fast, fast] + [
        min(cap, fast * 2 ** n) for n in range(1, 4)]
    # 长时间空闲（上千次无变化）退避状态必须有界，不能因指数溢出打断通知循环。
    assert set(_notice_delays(monkeypatch, _runtime(), limit=1100)) == {idle, min(cap, idle * 2)}


def test_foreground_task_or_watch_page_keep_one_second_and_changes_reset(monkeypatch):
    fast = TUI_BACKGROUND_NOTICE_INTERVAL_SECONDS
    assert _notice_delays(monkeypatch, _runtime(), foreground=True) == [fast] * 5
    assert _notice_delays(monkeypatch, _runtime(active=True)) == [fast] * 5
    watching = SimpleNamespace(snapshot=lambda: SimpleNamespace(background_process_count=1, background_watch_active=True))
    assert _notice_delays(monkeypatch, _runtime(), navigation=watching) == [fast] * 5
    processes = SimpleNamespace(snapshot=lambda: SimpleNamespace(background_process_count=1, background_watch_active=False))
    assert _notice_delays(monkeypatch, _runtime(), navigation=processes, advance_on=(4,), limit=7) == [
        fast, fast, fast * 2, fast, fast, fast, fast * 2]


def test_business_roster_poll_backs_off_only_while_projection_is_unchanged(monkeypatch):
    pages = [{"ok": True, "data": {"items": [{"agent_id": "a1", "work_state": "idle"}], "coverage": "complete"}}]
    calls = []

    # 类用途: 返回预置名册页的业务客户端替身。
    class _Client:
        def call(self, action, params):
            calls.append(action)
            return pages[0] if len(calls) < 4 else {
                "ok": True, "data": {"items": [{"agent_id": "a1", "work_state": "running"}], "coverage": "complete"}}

    monkeypatch.setattr(tui_background_agents, "business_client_for_agent", lambda agent, conversation_id: _Client())
    navigation = SimpleNamespace(root_runtime=SimpleNamespace(session_id="sess"), update_business_agents=lambda projection: None)
    stop = _Stop(6)
    thread = tui_background_agents.start_business_observation(object(), navigation, stop)
    thread.join(timeout=5)
    cap = max(5, min(TUI_BACKGROUND_NOTICE_FAILURE_MAX_SECONDS, 10))
    assert stop.delays == [5, cap, cap, 5, cap, cap]
    assert isinstance(thread, threading.Thread) and not thread.is_alive()
