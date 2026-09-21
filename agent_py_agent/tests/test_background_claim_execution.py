"""固定后台执行领取、恢复阻断、续租和收尾顺序；不调用真实模型或网络。"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.backends.errors import (
    ModelNotConfiguredError,
    ProviderQuotaExhaustedError,
    ProviderRequestRejectedError,
    ProviderTransientError,
    ProviderUsageLimitError,
)
from agent_py_agent.agent.conversation import background_claim as claim_module
from agent_py_agent.agent.conversation import runtime as runtime_module
from agent_py_agent.agent.conversation.background_execution import BackgroundCompactSliceYield
from agent_py_agent.agent.conversation.models import WakeSignal
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.settings.model_profiles import ModelProfileError


# LLM: 使用原准入与来源消费函数，假领域仅记录调用和模拟领取间隙的状态改变；迁移时只改入口绑定。
# 函数用途: 为竞态、错误分类和顺序断言提供可控依赖，不启动模型、线程或真实持久任务。
def _claim_fixture(monkeypatch):
    trace = []
    records = {"acquire": [], "finish": [], "policy": [], "retire": []}
    state = SimpleNamespace(
        owned=False, busy=False, terminal=False, detached=True, block=None,
        error=None, result=object(), after_acquire=lambda: None, broken_stage="",
    )

    def event(name):
        trace.append(name)
        if state.broken_stage == name:
            raise RuntimeError(name)

    def load_child(task_id):
        assert task_id == "task"
        event("owner")
        if state.owned:
            return SimpleNamespace(id=task_id)
        raise FileNotFoundError(task_id)

    def links(thread_id):
        assert thread_id == "thread"
        event("tasks")
        return [SimpleNamespace(
            task_id="task", status="completed" if state.terminal else "active",
            cancellation_scope="detached" if state.detached else "thread",
        )], []

    def acquire(payload):
        event("acquire")
        records["acquire"].append(dict(payload))
        state.after_acquire()
        return None if state.busy else {"claim_id": "claim"}

    def finish(payload):
        event("finish")
        records["finish"].append(dict(payload))

    def recovery(task_id):
        assert task_id == "task"
        event("recovery")
        if isinstance(state.block, Exception):
            raise state.block
        return state.block

    def run_once(params):
        event("run")
        assert params is request
        if state.error is not None:
            raise state.error
        return state.result

    def facts():
        event("facts")
        return {"stage": "after_execution"}

    def policy_failure(params):
        event("policy")
        records["policy"].append(params)

    def retire(identifier, *, now):
        event("retire")
        records["retire"].append((identifier, now))

    class Heartbeat:
        def __init__(self, config):
            assert config["store"].claims is store.claims
            assert config["claim_id"] == "claim"
            assert config["claim_scope_id"] == "thread.task.task"
            assert config["lease_seconds"] == 7
            assert config["interval_seconds"] == 0.1

        def start(self):
            event("start")

        def stop(self):
            event("stop")

    store = SimpleNamespace(
        claims=SimpleNamespace(acquire=acquire, finish=finish),
        tasks=SimpleNamespace(list_report=links),
        wakes=SimpleNamespace(mark_handled=retire),
        progress=SimpleNamespace(disable=retire),
    )
    agent = SimpleNamespace(subagents=SimpleNamespace(
        load=load_child, runtime_db=SimpleNamespace(main_agent_recovery_block_for_task=recovery),
    ))
    scheduler = runtime_module.BackgroundMainAgentScheduler({
        "runtime": SimpleNamespace(agent=agent, run_once=run_once), "store": store,
        "claim_ttl_seconds": 7, "claim_heartbeat_interval_seconds": 0.1,
    })
    scheduler._runtime_facts = facts
    scheduler._record_policy_failure = policy_failure
    request = {"thread_id": "thread", "task_id": "task", "reason": "sample", "now": 10.0}
    monkeypatch.setattr(claim_module, "ConversationRunClaimHeartbeat", Heartbeat)
    monkeypatch.setattr(runtime_module, "now", lambda value=None: 400.0 if value is None else value)
    monkeypatch.setattr(claim_module, "now", lambda value=None: 400.0 if value is None else value)
    return SimpleNamespace(scheduler=scheduler, state=state, trace=trace, records=records, request=request)


# LLM: 场景通过实际组装函数进入独立 claim 模块，不替换准入或结算实现。
# 函数用途: 调用一个受控后台工作片，保留旧测试断言的完整执行范围。
def _run_case(case):
    return claim_module.run_claimed(runtime_module._background_claim_dependencies(case.scheduler), case.request)


@pytest.mark.parametrize("result", [None, False, "report"])
def test_claim_execution_order_identity_and_clock_sources(monkeypatch, result):
    case = _claim_fixture(monkeypatch)
    case.state.result = result
    assert _run_case(case) is result
    assert case.trace == ["owner", "tasks", "acquire", "tasks", "recovery", "start", "run", "stop", "facts", "finish"]
    assert case.records["acquire"] == [{
        "thread_id": "thread", "claim_scope_id": "thread.task.task", "task_id": "task",
        "reason": "sample", "lease_seconds": 7, "now": 10.0,
    }]
    assert case.records["finish"] == [{
        "thread_id": "thread", "claim_scope_id": "thread.task.task", "claim_id": "claim",
        "task_id": "task", "status": "finished", "error": None,
        "runtime_facts": {"stage": "after_execution"}, "now": 400.0,
    }]


@pytest.mark.parametrize("error,status,policy", [
    (InterruptedError("stop"), "cancelled", False),
    (BackgroundCompactSliceYield("continue"), "finished", False),
    (RuntimeError("ordinary"), "failed", True),
    (KeyboardInterrupt(), "failed", True),
    (ProviderTransientError("retry"), "failed", False),
    (ProviderUsageLimitError("limit"), "failed", False),
    (ModelNotConfiguredError(), "failed", False),
    (ModelProfileError("profile"), "failed", False),
    (ProviderQuotaExhaustedError("quota"), "failed", True),
    (ProviderRequestRejectedError("rejected"), "failed", True),
])
def test_claim_exit_partition_keeps_original_error_and_cleanup_order(monkeypatch, error, status, policy):
    case = _claim_fixture(monkeypatch)
    case.state.error = error
    if status == "failed":
        with pytest.raises(type(error)) as raised:
            _run_case(case)
        assert raised.value is error
    else:
        assert _run_case(case) is None
    tail = ["stop", "facts", "finish"] + (["policy"] if policy else [])
    assert case.trace[case.trace.index("run") + 1:] == tail
    finished = case.records["finish"][0]
    assert finished["status"] == status
    assert finished["error"] is (error if status == "failed" else None)
    assert case.records["policy"] == ([case.request] if policy else [])
    assert not case.records["retire"]


@pytest.mark.parametrize("source", ["wake", "policy", "observation"])
def test_terminal_after_acquire_finishes_exact_claim_before_retiring_source(monkeypatch, source):
    case = _claim_fixture(monkeypatch)
    case.request["wake_signal"] = (
        WakeSignal("wake", "thread", root_task_id="task", reason="sample") if source == "wake"
        else {"policy_id": "policy"} if source == "policy" else {"observation_ids": ["observation"]}
    )
    case.state.after_acquire = lambda: setattr(case.state, "terminal", True)
    assert _run_case(case) is None
    assert case.trace == ["owner", "tasks", "acquire", "tasks", "finish"] + ([] if source == "observation" else ["retire"])
    assert case.records["finish"] == [{
        "thread_id": "thread", "claim_scope_id": "thread.task.task", "claim_id": "claim",
        "task_id": "task", "status": "cancelled", "runtime_facts": {"admission": "terminal_task_link"}, "now": 400.0,
    }]
    assert case.records["retire"] == ([] if source == "observation" else [(source, 400.0)])


@pytest.mark.parametrize("block", [{"reason": "unknown", "task_id": "task"}, OSError("unreadable")])
@pytest.mark.parametrize("source", ["wake", "policy", "observation"])
def test_recovery_change_after_acquire_keeps_all_sources_pending(monkeypatch, block, source):
    case = _claim_fixture(monkeypatch)
    assert case.scheduler._recovery_guard.block_for_task("task") is None
    case.trace.clear()
    case.state.after_acquire = lambda: setattr(case.state, "block", block)
    case.request["wake_signal"] = (
        WakeSignal("wake", "thread", root_task_id="task", reason="sample") if source == "wake"
        else {"policy_id": "policy"} if source == "policy" else {"observation_ids": ["observation"]}
    )
    assert _run_case(case) is None
    assert case.trace == ["owner", "tasks", "acquire", "tasks", "recovery", "finish"]
    finished = case.records["finish"][0]
    assert finished["status"] == "cancelled" and finished["claim_id"] == "claim"
    assert finished["runtime_facts"]["admission"] == "authority_recovery_required"
    recorded = finished["runtime_facts"]["recovery_block"]
    if isinstance(block, dict):
        assert recorded == block and recorded is not block
    else:
        assert recorded["reason"] == "authority_state_unreadable" and recorded["error_type"] == "OSError"
    assert not case.records["retire"] and not case.records["policy"]


@pytest.mark.parametrize("owned,busy", [(True, False), (False, True)])
def test_child_ownership_and_busy_claim_never_start_execution(monkeypatch, owned, busy):
    case = _claim_fixture(monkeypatch)
    case.state.owned, case.state.busy = owned, busy
    result = _run_case(case)
    if owned:
        assert result.wake_handled and result.delivery_reason == "subagent_runner_owns_continuation"
        assert case.trace == ["owner"]
    else:
        assert result is None and case.trace == ["owner", "tasks", "acquire"]
    assert not case.records["finish"]


@pytest.mark.parametrize("stage", ["start", "stop", "facts", "finish"])
def test_cleanup_dependency_errors_keep_existing_propagation_boundary(monkeypatch, stage):
    case = _claim_fixture(monkeypatch)
    case.state.broken_stage = stage
    with pytest.raises(RuntimeError, match=f"^{stage}$"):
        _run_case(case)
    full_order = ["owner", "tasks", "acquire", "tasks", "recovery", "start", "run", "stop", "facts", "finish"]
    assert case.trace == full_order[:full_order.index(stage) + 1]
    assert not case.records["policy"]


def test_real_heartbeat_renews_while_run_is_blocked_and_prevents_takeover(tmp_path, monkeypatch):
    store = ConversationStore(tmp_path / "conversations")
    thread = store.threads.get_or_create({
        "canonical_user_id": "owner", "channel": "internal", "channel_conversation_id": "conversation", "channel_user_id": "owner",
    })
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": "task", "goal": "保持独占执行"})
    entered, release, renewed = threading.Event(), threading.Event(), threading.Event()
    observed, errors = [], []
    original_renew = store.claims.renew
    original_finish = store.claims.finish
    finished = []

    def renew(payload):
        result = original_renew(payload)
        if result is not None:
            observed.append(result)
            renewed.set()
        return result

    def finish(payload):
        finished.append(payload)
        return original_finish(payload)

    def run_once(_request):
        entered.set()
        assert release.wait(3), "测试必须主动释放受控回合"

    monkeypatch.setattr(store.claims, "renew", renew)
    monkeypatch.setattr(store.claims, "finish", finish)
    scheduler = runtime_module.BackgroundMainAgentScheduler({
        "runtime": SimpleNamespace(agent=SimpleNamespace(), run_once=run_once), "store": store,
        "claim_ttl_seconds": 1, "claim_heartbeat_interval_seconds": 0.05,
    })
    scheduler._runtime_facts = lambda: {}
    started_at = time.time()

    def run():
        try:
            claim_module.run_claimed(runtime_module._background_claim_dependencies(scheduler), {"thread_id": thread.thread_id, "task_id": "task", "reason": "test", "now": started_at})
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert entered.wait(2) and renewed.wait(2)
        assert worker.is_alive() and not finished
        snapshot = store.claims.load(thread.thread_id)
        assert snapshot["status"] == "running"
        assert snapshot["heartbeat_at"] > started_at
        assert snapshot["expires_at"] > started_at + 1.001
        assert observed[0]["claim_id"] == snapshot["claim_id"]
        assert store.claims.acquire({
            "thread_id": thread.thread_id, "task_id": "competitor", "reason": "test",
            "lease_seconds": 1, "now": started_at + 1.001,
        }) is None
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive() and not errors
    assert len(finished) == 1
    assert store.claims.load(thread.thread_id)["status"] == "finished"
