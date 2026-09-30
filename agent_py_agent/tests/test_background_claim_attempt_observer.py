"""后台领取的尝试观察者（唤醒毒丸第 3 步 C2）。

唤醒车道要知道两件结构化事实来记尝试账：什么时候真的领到了租约（开始一次尝试），这一片怎么结束的
（领到未执行 + 准入码 / 跑完 / 被取消 / 压缩让出）。run_claimed 只在 acquire 成功后调一次 begin，结算处回调 settled；
begin 返回非空码时按这个码走"领到未执行"。观察者为 None 时（观察、策略车道）行为逐字不变。不启动 Gateway、不调用模型。
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.background_claim import (
    SESSION_MESSAGE_ABANDONED_ADMISSION,
    SESSION_TASK_BODY_ABANDONED_ADMISSION,
    BackgroundClaimDependencies,
    run_claimed,
)
from agent_py_agent.agent.conversation.background_execution import BackgroundCompactSliceYield
from agent_py_agent.agent.conversation.models import BackgroundMainAgentReport


# 类用途: 记录 begin/settled 调用顺序的观察者替身；begin 可返回指定的拦截码。
class _Observer:
    def __init__(self, block: str = "") -> None:
        self.block = block
        self.events: list[tuple] = []

    def begin(self, claim_id: str) -> str:
        self.events.append(("begin", claim_id))
        return self.block

    def settled(self, status: str, admission: str) -> None:
        self.events.append(("settled", status, admission))


def _report() -> BackgroundMainAgentReport:
    return BackgroundMainAgentReport(thread_id="thread-b", task_id="", reason="session_message", response="好",
                                     route_channel="internal", route_target="", created_at=1.0, wake_handled=True)


# 函数用途: 造一套最小依赖：acquire 按参数给租约或 None，其余判定按参数，记下 claims.finish 与 run_once 调用。
def _dependencies(observer, *, acquired=True, terminal=False, admission="", run=None):
    finished: list[dict] = []
    ran: list[dict] = []

    def run_once(kwargs):
        ran.append(kwargs)
        if isinstance(run, BaseException):
            raise run
        return run

    claims = SimpleNamespace(acquire=lambda _payload: {"claim_id": "claim-1"} if acquired else None,
                             finish=finished.append, heartbeat=lambda *_a, **_k: True, load=lambda *_a, **_k: {})
    dependencies = BackgroundClaimDependencies(
        claims=claims, claim_scope_id=lambda thread_id, _task: thread_id, child_owns_task=lambda _task: False,
        terminal_task=lambda _kwargs: terminal, recovery_block=lambda _task: None,
        source_admission=lambda _signal: admission, retire_source=lambda _kwargs: None, run_once=run_once,
        runtime_facts=dict, record_policy_failure=lambda _kwargs: None, lease_seconds=60,
        heartbeat_interval_seconds=3600.0, attempt=observer,
    )
    return dependencies, finished, ran


_KWARGS = {"thread_id": "thread-b", "task_id": "", "reason": "session_message", "wake_signal": None}


def test_no_lease_means_no_attempt() -> None:
    observer = _Observer()
    dependencies, finished, ran = _dependencies(observer, acquired=False)
    assert run_claimed(dependencies, dict(_KWARGS)) is None
    assert observer.events == [] and finished == [] and ran == []


def test_begin_happens_once_before_any_admission() -> None:
    """领到租约后先开始尝试，再做准入判定：任务已终态这类"领到未执行"也是一次尝试，带着准入码结算。"""
    observer = _Observer()
    dependencies, finished, _ran = _dependencies(observer, terminal=True)
    run_claimed(dependencies, dict(_KWARGS))
    assert observer.events == [("begin", "claim-1"), ("settled", "not_executed", "terminal_task_link")]
    assert [row["runtime_facts"]["admission"] for row in finished] == ["terminal_task_link"]


def test_blocked_attempt_is_finished_as_not_executed() -> None:
    observer = _Observer(block="wake_attempt_limit")
    dependencies, finished, ran = _dependencies(observer)
    assert run_claimed(dependencies, dict(_KWARGS)) is None
    assert ran == [], "尝试被拦下时不开模型回合"
    assert [(row["status"], row["runtime_facts"]["admission"]) for row in finished] == [("cancelled", "wake_attempt_limit")]
    assert observer.events[-1] == ("settled", "not_executed", "wake_attempt_limit")


@pytest.mark.parametrize(("run", "status"), [
    ("report", "finished"), (None, "finished"), (InterruptedError("停"), "cancelled"),
    (BackgroundCompactSliceYield("让出"), "yielded"),
], ids=["finished", "finished-no-report", "cancelled", "yielded"])
def test_executed_slice_reports_how_it_ended(run, status) -> None:
    observer = _Observer()
    dependencies, _finished, ran = _dependencies(observer, run=_report() if run == "report" else run)
    run_claimed(dependencies, dict(_KWARGS))
    assert len(ran) == 1
    assert observer.events == [("begin", "claim-1"), ("settled", status, "")]


def test_failed_slice_leaves_the_error_to_the_caller() -> None:
    """抛异常结束的片不回调 settled：异常原样抛给调用方，由唤醒车道按异常记账。"""
    observer = _Observer()
    dependencies, _finished, _ran = _dependencies(observer, run=RuntimeError("坏了"))
    with pytest.raises(RuntimeError):
        run_claimed(dependencies, dict(_KWARGS))
    assert observer.events == [("begin", "claim-1")]


@pytest.mark.parametrize(("admission", "expected"), [
    (SESSION_TASK_BODY_ABANDONED_ADMISSION, [("close_out", SESSION_TASK_BODY_ABANDONED_ADMISSION), "retire"]),
    (SESSION_MESSAGE_ABANDONED_ADMISSION, [("close_out", SESSION_MESSAGE_ABANDONED_ADMISSION), "retire"]),
    ("wake_source_changed", []),
], ids=["task-body-abandoned", "message-abandoned", "other-admission"])
def test_finished_source_is_closed_out_before_it_is_retired(admission, expected) -> None:
    """来源已处理完或已放弃时先收领域状态再结案唤醒（先结案的话，收尾失败会留下"唤醒已结、任务还挂着"）；其它准入码两者都不做。"""
    order: list = []
    dependencies, _finished, ran = _dependencies(None, admission=admission)
    dependencies = replace(dependencies, retire_source=lambda _kwargs: order.append("retire"),
                           close_out_source=lambda _kwargs, code: order.append(("close_out", code)))
    run_claimed(dependencies, dict(_KWARGS))
    assert order == expected and ran == []
