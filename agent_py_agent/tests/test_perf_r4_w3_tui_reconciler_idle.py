# LLM: PERF-R4-W3 回归：控制/设置/补充输入三条本地对账线程在 outbox 没有任何行时不再每 3 秒读盘，改按 idle_delay
#   （tui_idle_poll_max_seconds）等待；有待办行时仍按原 maximum_delay 退避，未传 idle_delay 时行为与原来完全相同。
# 模块用途: 只观察对账循环的等待秒数，不起 Gateway、不发请求。
from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.cli.chat_parts import tui_actions
from agent_py_agent.cli.chat_parts.tui_control_delivery import TuiControlOperationReconciler
from agent_py_agent.cli.chat_parts.tui_input_delivery import TuiActiveInputReconciler


# 类用途: 记录对账线程等待秒数、第一次等待后即停止循环的唤醒替身。
class _Wake:
    def __init__(self, stop: threading.Event):
        self.stop, self.timeouts = stop, []

    def wait(self, timeout=None):
        self.timeouts.append(timeout)
        self.stop.set()
        return False

    def clear(self):
        pass

    def set(self):
        pass


# 函数用途: 无回调副作用的通用回调替身。
def _noop(*_args, **_kwargs):
    return None


def _input(tmp_path: Path, stop: threading.Event, **delays):
    return TuiActiveInputReconciler(
        path=tmp_path / "input-outbox.json", submit=_noop, status=_noop, on_restore=_noop, on_accepted=_noop,
        on_queued=_noop, on_rejected=_noop, on_auth_rejected=_noop, on_conflict=_noop, on_unconfirmed=_noop,
        on_error=_noop, stop_event=stop, initial_delay=0.25, maximum_delay=3.0, **delays)


def _control(tmp_path: Path, stop: threading.Event, **delays):
    return TuiControlOperationReconciler(
        path=tmp_path / "control-outbox.json", submit=_noop, status=_noop, on_restore=_noop, on_complete=_noop,
        on_terminal_unknown=_noop, on_conflict=_noop, on_error=_noop, stop_event=stop,
        initial_delay=0.25, maximum_delay=3.0, **delays)


def test_empty_input_outbox_waits_idle_delay(tmp_path):
    stop = threading.Event()
    reconciler = _input(tmp_path, stop, idle_delay=30.0)
    reconciler._wake = wake = _Wake(stop)
    reconciler._run()
    assert len(wake.timeouts) == 1 and 29.0 < wake.timeouts[0] <= 30.0


def test_empty_control_and_settings_lanes_wait_idle_delay(tmp_path):
    for settings in (False, True):
        stop = threading.Event()
        reconciler = _control(tmp_path, stop, idle_delay=30.0)
        wake = _Wake(stop)
        if settings:
            reconciler._settings_wake = wake
        else:
            reconciler._wake = wake
        reconciler._run(settings=settings)
        assert len(wake.timeouts) == 1 and 29.0 < wake.timeouts[0] <= 30.0


def test_default_idle_delay_keeps_original_maximum(tmp_path):
    stop = threading.Event()
    reconciler = _input(tmp_path, stop)
    reconciler._wake = wake = _Wake(stop)
    reconciler._run()
    assert reconciler.idle_delay == 3.0 and wake.timeouts[0] <= 3.0


def test_construction_reads_tui_idle_poll_config():
    assert tui_actions._tui_idle_poll_seconds(SimpleNamespace(config=SimpleNamespace(tui_idle_poll_max_seconds=30.0))) == 30.0
    assert tui_actions._tui_idle_poll_seconds(object()) == 0.0
