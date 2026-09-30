"""run_once 之后的二次中断检查：只对**绑定了会话任务**的回合生效（dev 01:11 裁定的回归修复）。

背景：取消修复在 background_claim.run_with_heartbeat 里新加了一道「run_once 之后 is_interrupted()
就抛 InterruptedError」。它对**所有**后台运行都生效，而普通后台运行（带 task_id、注册
conversation-request: 名字）是能被普通 /stop 打到的——停止恰好落在「答复已在 run_once 里交付之后」时，
报告会被丢弃、认领记成 cancelled、唤醒可能留 pending 被重跑。全仓
test_thread_interrupt::test_background_main_run_registers_the_durable_task_control_name 抓到了这个回归。

修法：二次检查加前置条件——只有 _host_delivery_bound_turn(kwargs)（唤醒信封里的 session_task_id）
非空时才适用；其它来源逐字保持原来的 return dependencies.run_once(kwargs)。

本文件两条用例互为对照（dev 要求）。两条都让 run_once 自己把停止旗立到**当前线程**上，
精确复现"答复已交付之后停止才到"这个时序：
- 绑定回合 → 认领结算成 cancelled、返回 None；
- 未绑定的普通后台运行 → 仍是 finished，并原样返回报告。
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from agent_py_agent.agent.concurrency.interrupt import is_interrupted, set_interrupt
from agent_py_agent.agent.conversation import background_claim as claim_module
from agent_py_agent.agent.conversation.models import WakeSignal
from agent_py_agent.agent.conversation.runtime import (
    BackgroundMainAgentScheduler,
    _background_claim_dependencies,
)

# 派活回合的专用可中断名（control_commands.session_task_interrupt_name 的格式）。
_BOUND_TURN = "stask-bound-after-run-once"


def setup_function(_function) -> None:
    """每条用例都以"本线程没有停止旗"开始：上一条用例可能把它留在立着。"""
    set_interrupt(False)


def teardown_function(_function) -> None:
    """用例自己会 set_interrupt(True)；同一线程里不清理会污染后面别的测试文件。"""
    set_interrupt(False)


class _Heartbeat:
    def __init__(self, observed):
        self._observed = observed

    def stop(self):
        self._observed["heartbeat_stopped"] = True


# 函数用途: 造一个调度器，run_once 里**先立停止旗再返回报告**（真实链路里报告此时已落账交付）。
def _scheduler(observed: dict):
    class Runtime:
        agent = SimpleNamespace()

        def run_once(self, _kwargs):
            observed["ran"] = True
            # 停止就落在这个窗口：报告已经产生，但这一片还没返回。
            set_interrupt(True)
            observed["interrupted_after_run_once"] = is_interrupted()
            return "done"

    class Claims:
        def finish(self, payload):
            observed["finish"] = payload

    class Store:
        claims = Claims()
        wakes = SimpleNamespace(pending_one=lambda _signal_id: None)
        guidance = None
        # 夹具补全：领取后准入按任务号读 store.session_tasks（派活正文是否已放弃），替身也得有这个属性。
        session_tasks = None

    scheduler = BackgroundMainAgentScheduler({"runtime": Runtime(), "store": Store()})
    scheduler._runtime_facts = lambda: {}
    return scheduler


def _deps_for(scheduler):
    return replace(
        _background_claim_dependencies(scheduler),
        child_owns_task=lambda _task: False,
        claim_scope_id=lambda _thread, _task: "lane",
        terminal_task=lambda _kwargs: False,
        recovery_block=lambda _task: None,
    )


def _patch_heartbeat(monkeypatch, observed) -> None:
    monkeypatch.setattr(
        claim_module, "_start_heartbeat",
        lambda _d, _c, _t, *, claim_scope_id="": _Heartbeat(observed),
    )


def test_bound_turn_is_settled_cancelled_when_interrupted_after_run_once(monkeypatch) -> None:
    """绑定回合：run_once 之后才被中断 → 认领结算成 cancelled、返回 None。"""
    observed: dict[str, object] = {}
    signal = WakeSignal(
        wake_signal_id="wake-bound", thread_id="thread-bound", reason="session_task",
        root_task_id="", metadata={"session_task_id": _BOUND_TURN},
    )
    _patch_heartbeat(monkeypatch, observed)

    result = claim_module.run_with_heartbeat(
        _deps_for(_scheduler(observed)), "claim-bound",
        {"thread_id": "thread-bound", "task_id": "", "reason": "session_task", "wake_signal": signal},
        claim_scope_id="lane",
    )

    _require(observed)
    assert result is None, "绑定回合在 run_once 之后被中断，不应再交出报告"
    assert observed["finish"]["status"] == "cancelled"


def test_unbound_background_run_keeps_report_after_run_once(monkeypatch) -> None:
    """未绑定的普通后台运行：同样情形 → 仍是 finished，并原样返回报告（回归修复的正向对照）。"""
    observed: dict[str, object] = {}
    _patch_heartbeat(monkeypatch, observed)

    result = claim_module.run_with_heartbeat(
        _deps_for(_scheduler(observed)), "claim-unbound",
        {"thread_id": "thread-plain", "task_id": "req-plain"},
        claim_scope_id="lane",
    )

    _require(observed)
    assert result == "done", "普通后台运行不得因为二次检查丢掉已交付的报告"
    assert observed["finish"]["status"] == "finished"


# 函数用途: 两条用例共同的时序前提——run_once 确实跑了，且它跑完之后停止旗是立着的。
def _require(observed: dict) -> None:
    assert observed.get("ran") is True, "前提不成立：run_once 没有被调用"
    assert observed.get("interrupted_after_run_once") is True, (
        "前提不成立：run_once 返回前停止旗没有立到本线程上"
    )
