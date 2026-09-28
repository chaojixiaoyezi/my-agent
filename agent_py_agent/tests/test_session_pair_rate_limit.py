"""每对会话每小时限额的结构化计数合同测试（无真实模型请求、无 Gateway）。

覆盖 dev 13:15 的第 4 片要求：限额要真的生效，而且**回报消息（任务结果、取消通知）也计入**，
不能靠"只统计模型发起的发送"来绕过。这里只测计数与超限判定本身；工具侧的接线由各工具测试覆盖。
"""

from __future__ import annotations

from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation.session_pair_rate import (
    WINDOW_SECONDS,
    SessionPairRateLimiter,
    pair_limit_reached,
    record_pair_message,
)

_SENDER = "thread-A"
_TARGET = "thread-B"


def _limiter(tmp_path) -> SessionPairRateLimiter:
    return SessionPairRateLimiter(ConversationStore(tmp_path).storage)


def test_count_starts_at_zero_and_increments(tmp_path) -> None:
    limiter = _limiter(tmp_path)
    assert limiter.count(_SENDER, _TARGET) == 0
    assert limiter.record(_SENDER, _TARGET) == 1
    assert limiter.record(_SENDER, _TARGET) == 2
    assert limiter.count(_SENDER, _TARGET) == 2


def test_pairs_are_bucketed_separately(tmp_path) -> None:
    limiter = _limiter(tmp_path)
    limiter.record(_SENDER, _TARGET)
    limiter.record(_SENDER, "thread-C")
    assert limiter.count(_SENDER, _TARGET) == 1
    assert limiter.count(_SENDER, "thread-C") == 1
    assert limiter.count(_TARGET, _SENDER) == 0


def test_window_rollover_resets_count(tmp_path) -> None:
    limiter = _limiter(tmp_path)
    start = 1_700_000_000.0
    limiter.record(_SENDER, _TARGET, now=start)
    limiter.record(_SENDER, _TARGET, now=start)
    assert limiter.count(_SENDER, _TARGET, now=start) == 2
    # 下一个小时窗口：计数归零，重新从 1 开始。
    assert limiter.count(_SENDER, _TARGET, now=start + WINDOW_SECONDS) == 0
    assert limiter.record(_SENDER, _TARGET, now=start + WINDOW_SECONDS) == 1


def test_over_limit_uses_configured_limit_and_zero_means_unlimited(tmp_path) -> None:
    limiter = _limiter(tmp_path)
    limiter.record(_SENDER, _TARGET)
    assert limiter.over_limit(_SENDER, _TARGET, 1) is True
    assert limiter.over_limit(_SENDER, _TARGET, 2) is False
    assert limiter.over_limit(_SENDER, _TARGET, 0) is False


def test_blank_thread_ids_are_not_counted(tmp_path) -> None:
    limiter = _limiter(tmp_path)
    assert limiter.record("", _TARGET) == 0
    assert limiter.record(_SENDER, "") == 0
    assert limiter.count("", _TARGET) == 0


def test_helper_functions_tolerate_store_without_limiter() -> None:
    assert pair_limit_reached(object(), _SENDER, _TARGET, 1) is False
    assert record_pair_message(object(), _SENDER, _TARGET) == 0


def test_helpers_read_and_write_the_store_limiter(tmp_path) -> None:
    store = ConversationStore(tmp_path)
    assert record_pair_message(store, _SENDER, _TARGET) == 1
    assert pair_limit_reached(store, _SENDER, _TARGET, 1) is True
    assert pair_limit_reached(store, _SENDER, _TARGET, 2) is False
