"""第4层根修:观察/urgent 上报轮跳过交付收尾门(输出即报告,别被 uncontracted 打回拦下发不出去)。"""
from __future__ import annotations

from types import SimpleNamespace

from agent.agent_core.delivery_closeout.closeout import (
    _is_report_only_wake,
    main_agent_delivery_closeout_response,
)


def test_report_only_wake_detection():
    assert _is_report_only_wake(SimpleNamespace(reason="observation_requires_main_agent")) is True
    assert _is_report_only_wake(SimpleNamespace(reason="urgent_wake_signal")) is True
    assert _is_report_only_wake(SimpleNamespace(reason="incoming_channel_message")) is False
    assert _is_report_only_wake(SimpleNamespace(reason="")) is False


def test_closeout_short_circuits_for_report_wake():
    # 上报轮:收尾门第一行就返回 None(不碰契约/workspace),报告走 run 正常投递
    req = SimpleNamespace(params=SimpleNamespace(reason="observation_requires_main_agent"), agent=None, backend=None)
    assert main_agent_delivery_closeout_response(req) is None
