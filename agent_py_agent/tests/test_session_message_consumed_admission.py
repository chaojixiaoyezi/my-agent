"""消息唤醒的"已消费"判据：只有 consumed 算已消费（ae 复审必须改）。

回执状态全集是 5 个（store_guidance_records.py）：pending / reserved / submitted / consumed / rejected。
- reserved、submitted 可逆：调用失败会退回 reserved，release_reserved 会把提交前死掉的预留写回 pending；
- rejected 不等于送达：B 忙时消息已注入、在途调用未返回就被 /stop，终结时 reject_reserved 把它改成 rejected，
  这条消息仍要靠唤醒回合交给 B。
把它们当已消费，唤醒会被跳过并结案，B 空闲时这条消息就一直送不到。
判定为已消费时，run_claimed 经 retire_source 结案唤醒（来源已处理完），其它准入码不动来源。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.background_claim import (
    SESSION_MESSAGE_CONSUMED_ADMISSION,
    BackgroundClaimDependencies,
    run_claimed,
)
from agent_py_agent.agent.conversation.models import (
    SESSION_MESSAGE_WAKE_REASON,
    WakeSignal,
)
from agent_py_agent.agent.conversation.runtime import _session_message_consumed_admission

_SENDER = "thread-sender"
_TARGET = "thread-target"
_KEY = f"session_message:{_SENDER}->{_TARGET}"


# 函数用途: 造一个只实现 receipt() 的 guidance 替身，按 dedupe_key 返回给定状态。
class _Guidance:
    def __init__(self, status):
        self.status = status
        self.seen_keys: list[str] = []

    def receipt(self, dedupe_key):
        self.seen_keys.append(dedupe_key)
        if self.status is None:
            return None
        return SimpleNamespace(status=self.status)


def _signal() -> WakeSignal:
    return WakeSignal(
        wake_signal_id="wake-msg",
        thread_id=_TARGET,
        reason=SESSION_MESSAGE_WAKE_REASON,
        root_task_id="",
        metadata={"origin_kind": SESSION_MESSAGE_WAKE_REASON, "origin_thread_id": _SENDER},
    )


def test_consumed_status_skips_the_turn() -> None:
    guidance = _Guidance("consumed")
    assert _session_message_consumed_admission(_signal(), guidance_store=guidance) == SESSION_MESSAGE_CONSUMED_ADMISSION
    assert guidance.seen_keys == [_KEY], "判据应当按会话对键查回执"


@pytest.mark.parametrize("status", ["submitted", "rejected"])
def test_submitted_and_rejected_are_not_treated_as_consumed(status) -> None:
    """submitted 调用失败会退回 reserved；rejected 的消息仍要靠唤醒回合交给目标——两者都得照常开回合。"""
    assert _session_message_consumed_admission(_signal(), guidance_store=_Guidance(status)) == ""


def test_reserved_is_not_treated_as_consumed() -> None:
    """reserved 是可逆预留：必须放行，否则认领回合提交前失败时会丢消息。"""
    assert _session_message_consumed_admission(_signal(), guidance_store=_Guidance("reserved")) == ""


def test_pending_is_not_treated_as_consumed() -> None:
    assert _session_message_consumed_admission(_signal(), guidance_store=_Guidance("pending")) == ""


def test_missing_receipt_fails_open() -> None:
    """读不到回执一律照常开回合：宁可多跑一轮，也不误吞真实工作。"""
    assert _session_message_consumed_admission(_signal(), guidance_store=_Guidance(None)) == ""


def test_other_wake_reasons_are_ignored() -> None:
    signal = WakeSignal(
        wake_signal_id="wake-other", thread_id=_TARGET, reason="session_task",
        root_task_id="", metadata={"origin_thread_id": _SENDER},
    )
    assert _session_message_consumed_admission(signal, guidance_store=_Guidance("consumed")) == ""


def test_receipt_read_error_fails_open() -> None:
    """回执读取抛异常（坏账、IO）时同样放行：读不到不能当成"已消费"而吞掉真实工作。"""

    class _Broken(_Guidance):
        def receipt(self, dedupe_key):
            self.seen_keys.append(dedupe_key)
            raise OSError("receipt unreadable")

    guidance = _Broken("consumed")
    assert _session_message_consumed_admission(_signal(), guidance_store=guidance) == ""
    assert guidance.seen_keys == [_KEY]


# 函数用途: 造一个只走到"领取后来源准入"的最小依赖：拿到租约、准入返回给定码，记录 claim 结算与来源结案。
def _dependencies(admission):
    finished: list[dict] = []
    retired: list[dict] = []
    claims = SimpleNamespace(acquire=lambda _payload: {"claim_id": "claim-1"}, finish=finished.append)
    dependencies = BackgroundClaimDependencies(
        claims=claims, claim_scope_id=lambda thread_id, _task: thread_id, child_owns_task=lambda _task: False,
        terminal_task=lambda _kwargs: False, recovery_block=lambda _task: None,
        source_admission=lambda _signal: admission, retire_source=retired.append,
        run_once=lambda _kwargs: pytest.fail("来源准入不通过时不应开模型回合"), runtime_facts=dict,
        record_policy_failure=lambda _kwargs: None, lease_seconds=60, heartbeat_interval_seconds=10.0,
    )
    return dependencies, finished, retired


@pytest.mark.parametrize(("admission", "retire"), [
    (SESSION_MESSAGE_CONSUMED_ADMISSION, True),
    ("wake_source_changed", False), ("wake_source_unreadable", False), ("wake_source_not_pending", False),
])
def test_only_a_consumed_source_is_retired(admission, retire) -> None:
    """已消费 = 来源已处理完：结 claim 之外经 retire_source 结案唤醒；变化、读不出、已不在队列只结 claim，交还原队列。"""
    dependencies, finished, retired = _dependencies(admission)
    kwargs = {"thread_id": _TARGET, "task_id": "", "reason": SESSION_MESSAGE_WAKE_REASON, "wake_signal": _signal()}
    assert run_claimed(dependencies, kwargs) is None
    assert [(row["status"], row["runtime_facts"]["admission"]) for row in finished] == [("cancelled", admission)]
    assert retired == ([kwargs] if retire else [])
