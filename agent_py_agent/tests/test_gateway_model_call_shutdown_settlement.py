# LLM: 只验证账本批量终态、停机准入栅栏、停机结清 facade 与 Gateway 收尾接线；进程准入表由 tests/conftest.py 按用例隔离；文件/心跳/事件副作用全部替换，决策取消换成空操作以免
#   置位进程级关闭标记，不启动真实 Gateway，不发网络请求。改 _cmd_gateway_run_cleanup 顺序或事件名时同步这里与
#   test_gateway_decision_shutdown_cancel.py。
# 模块用途: 验证 Gateway 停止时仍在途的模型调用会被记成"被停机中断、未结算"，并写出结构化停机事件（用户决定第 4 项）。
from __future__ import annotations

import gc
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.model import call_runtime
from agent_py_agent.agent.agent_core.model.call_runtime import (
    MODEL_CALL_INTERRUPTED_ERROR_CODE,
    MODEL_CALL_INTERRUPTED_ERROR_TYPE,
    model_call_ledger,
    model_call_summary,
    settle_open_model_calls_for_shutdown,
)
from agent_py_agent.agent.contracts import model_call_ledger as ledger_module
from agent_py_agent.agent.contracts.model_call_ledger import (
    ModelCallAdmissionClosedError,
    ModelCallAdmissionClosure,
    ModelCallFailureParams,
    ModelCallFinishParams,
    ModelCallFirstTokenParams,
    ModelCallLedger,
    ModelCallLedgerOptions,
    ModelCallProviderAttemptParams,
    ModelCallStartedParams,
    close_model_call_admission,
)
from agent_py_agent.agent.conversation import decision_policy
from agent_py_agent.cli import gateway_process

_TERMINAL = {"failed", "finished", "timed_out"}


_PROJECTION_FIELDS = {
    "call_id", "request_id", "run_id", "backend", "model", "purpose", "first_token_seen", "elapsed_seconds",
    "estimated_input_tokens", "provider_attempt_count", "is_probe", "error_code", "settlement",
}


# 函数用途: 登记一次模型调用；带 auxiliary 元数据的样例模拟 Curator 这类挂在 lease run 下的后台辅助调用。
def _start(ledger: ModelCallLedger, call_id: str, *, run_id: str = "", request_id: str = "", **metadata) -> None:
    ledger.started(ModelCallStartedParams(
        call_id=call_id, backend="anthropic_compatible", model="MiniMax-M2.7", input_tokens=26600,
        run_id=run_id, request_id=request_id, metadata=dict(metadata),
    ))


def test_closing_admission_only_fails_active_records():
    ledger = ModelCallLedger()
    for call_id in ("waiting", "streaming", "done", "broken"):
        _start(ledger, call_id)
    ledger.first_token(ModelCallFirstTokenParams(call_id="streaming"))
    ledger.finished(ModelCallFinishParams(call_id="done", output_tokens=5, provider_usage_reported=True))
    ledger.failed(ModelCallFailureParams(call_id="broken", error_type="ProviderRequestRejectedError", error_code="HTTP_400"))
    closure = ModelCallAdmissionClosure(error_type="HostShutdownInterrupted", error_code="TEST_INTERRUPTED")
    interrupted = close_model_call_admission(closure)
    assert sorted(record.call_id for record in interrupted) == ["streaming", "waiting"]
    assert all(record.status == "failed" and record.error_code == "TEST_INTERRUPTED" for record in interrupted)
    by_id = {record.call_id: record for record in ledger.records()}
    assert by_id["done"].status == "finished" and by_id["broken"].error_code == "HTTP_400", "已终态记录不动"
    assert close_model_call_admission(closure) == ()


def test_settle_facade_projects_structured_facts():
    agent = SimpleNamespace()
    _start(model_call_ledger(agent), "curator", run_id="memory-curator-run-1", auxiliary=True)
    facts = settle_open_model_calls_for_shutdown()
    assert len(facts) == 1 and set(facts[0]) == _PROJECTION_FIELDS
    fact = facts[0]
    assert (fact["call_id"], fact["run_id"], fact["purpose"]) == ("curator", "memory-curator-run-1", "auxiliary")
    assert (fact["error_code"], fact["settlement"]) == (MODEL_CALL_INTERRUPTED_ERROR_CODE, "unsettled")
    assert fact["first_token_seen"] is False and fact["estimated_input_tokens"] == 26600
    record = model_call_ledger(agent).records()[0]
    assert record.status == "failed" and record.error_type == MODEL_CALL_INTERRUPTED_ERROR_TYPE
    counts = model_call_summary(agent, run_id="memory-curator-run-1")["status_counts"]
    assert counts.get("failed") == 1 and not counts.get("started"), "在途调用结成 failed，不再算运行中"
    assert settle_open_model_calls_for_shutdown() == (), "重复结清没有第二份事实"


# 函数用途: 构造一次 Gateway 收尾请求：替换文件/心跳/事件副作用与决策取消，记录关键步骤顺序，返回请求与事件列表。
def _cleanup_request(monkeypatch, order, agent):
    events = []
    monkeypatch.setattr(gateway_process, "remove_pid_file_if_owned", lambda *_args: None)
    monkeypatch.setattr(gateway_process, "remove_gateway_stop_request_if_owned", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(gateway_process, "_gateway_context_process_identity", lambda _context: "identity")
    monkeypatch.setattr(gateway_process, "_write_gateway_heartbeat", lambda *_args, **_kwargs: order.append("heartbeat"))
    monkeypatch.setattr(gateway_process, "log_gateway_event", lambda _agent, name, payload: events.append((name, payload)))
    monkeypatch.setattr(decision_policy, "cancel_active_decisions_for_shutdown", lambda: 0)
    http = SimpleNamespace(stop=lambda: order.append("http_stop"))
    context = SimpleNamespace(paths=SimpleNamespace(pid="pid", stop_request="stop"), agent=agent, process_started_at=0.0)
    request = SimpleNamespace(
        context=context, pid=7, stop_event=threading.Event(), heartbeat_thread=threading.Thread(target=None),
        request_thread=threading.Thread(target=None), background_thread=threading.Thread(target=None),
        http_server=http, termination_status="stopped", termination_reason="test",
    )
    return request, events


def test_gateway_cleanup_records_interrupted_calls_after_the_drain_window(monkeypatch):
    order = []
    agent = SimpleNamespace()
    _start(model_call_ledger(agent), "curator", run_id="memory-curator-run-1", auxiliary=True)
    original = call_runtime.settle_open_model_calls_for_shutdown
    monkeypatch.setattr(
        call_runtime, "settle_open_model_calls_for_shutdown", lambda: order.append("settle") or original()
    )
    request, events = _cleanup_request(monkeypatch, order, agent)
    report = gateway_process._cmd_gateway_run_cleanup(request)
    assert order == ["http_stop", "settle", "heartbeat"], "停 HTTP、收完循环之后才结清，再写心跳"
    assert report["interrupted_model_calls"] == 1 and report["drain_complete"] is True
    assert [name for name, _payload in events] == ["gateway_model_calls_interrupted", "gateway_run_cleanup"]
    payload = events[0][1]
    assert (payload["count"], payload["drain_complete"], payload["pid"], payload["status"]) == (1, True, 7, "cleanup")
    assert payload["calls"][0]["call_id"] == "curator"
    assert payload["calls"][0]["error_code"] == MODEL_CALL_INTERRUPTED_ERROR_CODE
    assert model_call_ledger(agent).records()[0].status == "failed"
    assert events[1][1]["interrupted_model_calls"] == 1


def test_gateway_cleanup_without_open_calls_writes_no_extra_event(monkeypatch):
    agent = SimpleNamespace()
    ledger = model_call_ledger(agent)
    _start(ledger, "done", request_id="req-1")
    ledger.finished(ModelCallFinishParams(call_id="done", output_tokens=3, provider_usage_reported=True))
    request, events = _cleanup_request(monkeypatch, [], agent)
    report = gateway_process._cmd_gateway_run_cleanup(request)
    assert report["interrupted_model_calls"] == 0 and [name for name, _payload in events] == ["gateway_run_cleanup"]
    bare_request, bare_events = _cleanup_request(monkeypatch, [], object())
    assert gateway_process._cmd_gateway_run_cleanup(bare_request)["interrupted_model_calls"] == 0
    assert [name for name, _payload in bare_events] == ["gateway_run_cleanup"], "没有账本的 agent 不新建账本、不写事件"


def test_gateway_cleanup_survives_a_failing_settlement(monkeypatch):
    def broken():
        raise RuntimeError("secret detail that must not be logged")

    monkeypatch.setattr(call_runtime, "settle_open_model_calls_for_shutdown", broken)
    order = []
    request, events = _cleanup_request(monkeypatch, order, SimpleNamespace())
    report = gateway_process._cmd_gateway_run_cleanup(request)
    assert report["interrupted_model_calls"] == 0 and "heartbeat" in order
    failed = [payload for name, payload in events if name == "gateway_model_call_settlement_failed"]
    assert failed == [{"error_type": "RuntimeError"}], "只记异常类型，不记异常正文"
    assert [name for name, _payload in events][-1] == "gateway_run_cleanup"


# 函数用途: 进程内 runner worker 的最小启动参数（只用来走一遍 _attach_worker_runtime）。
def _worker_params():
    from agent_py_agent.agent.agent_core.runner.worker import RunSubagentWorkerParams

    return RunSubagentWorkerParams(config=None, root=Path("."), run_id="subagent-1", instruction="", dry_run=False,
                                   max_cards=0, probe=False, retry_reason="")


def test_runner_worker_and_owner_agent_ledgers_are_settled_with_the_gateway(monkeypatch):
    """J17（2026-10-02）：非 local/main owner 走进程内线程派工，每个 runner worker 和 owner 池里的作用域 agent 各有一本账，
    和网关同进程，停机会切断它们的在途调用。改前停机只结清网关 agent 自己的账本，这些调用永远停在 started。
    停机准入栅栏之后，账本构造即登记，不再靠这两处构建路径显式登记。"""
    from agent_py_agent.agent import core, owner_scoped_pool
    from agent_py_agent.agent.agent_core.runner.worker import _attach_worker_runtime
    from agent_py_agent.agent.settings.config import AgentConfig

    gateway = SimpleNamespace()
    _start(model_call_ledger(gateway), "gateway-call", request_id="gwreq-1")
    worker = SimpleNamespace()
    _start(model_call_ledger(worker), "worker-call", run_id="subagent-1")
    _attach_worker_runtime(worker, _worker_params())
    monkeypatch.setattr(core, "SimpleAgent",
                        lambda config, root, **_kwargs: SimpleNamespace(config=config, _model_call_ledger=ModelCallLedger()))
    owner = SimpleNamespace(provider="local", owner_kind="user", owner_id="u-1")
    scoped = owner_scoped_pool.build_owner_scoped_agent(AgentConfig(), Path("."), owner, None)
    _start(scoped._model_call_ledger, "owner-call", request_id="gwreq-2")

    facts = settle_open_model_calls_for_shutdown()

    assert sorted(fact["call_id"] for fact in facts) == ["gateway-call", "owner-call", "worker-call"]
    assert {fact["error_code"] for fact in facts} == {MODEL_CALL_INTERRUPTED_ERROR_CODE}
    assert model_call_ledger(worker).records()[0].status == "failed"
    assert settle_open_model_calls_for_shutdown() == (), "同一本账只结一次"


def test_tracked_ledgers_are_weak_and_vanish_with_their_agent():
    from agent_py_agent.agent.agent_core.runner.worker import _attach_worker_runtime

    worker = SimpleNamespace()
    _start(model_call_ledger(worker), "gone-call", run_id="subagent-2")
    _attach_worker_runtime(worker, _worker_params())
    del worker
    gc.collect()
    assert settle_open_model_calls_for_shutdown() == ()


def test_gateway_cleanup_reports_runner_worker_calls_in_the_same_event(monkeypatch):
    from agent_py_agent.agent.agent_core.runner.worker import _attach_worker_runtime

    agent = SimpleNamespace()
    _start(model_call_ledger(agent), "curator", run_id="memory-curator-run-1", auxiliary=True)
    worker = SimpleNamespace()
    _start(model_call_ledger(worker), "worker-call", run_id="subagent-3")
    _attach_worker_runtime(worker, _worker_params())
    request, events = _cleanup_request(monkeypatch, [], agent)

    report = gateway_process._cmd_gateway_run_cleanup(request)

    assert report["interrupted_model_calls"] == 2
    calls = events[0][1]["calls"]
    assert sorted((call["call_id"], call["run_id"]) for call in calls) == [
        ("curator", "memory-curator-run-1"), ("worker-call", "subagent-3")]


# 函数用途: 列出账本里还没有终态的调用编号（停机结清之后必须为空）。
def _still_open(ledger: ModelCallLedger) -> list[str]:
    return [record.call_id for record in ledger.records() if record.status not in _TERMINAL]


def test_started_after_settlement_is_rejected_and_nothing_stays_open(monkeypatch):
    """J17 必须修（sol2 复现，2026-10-02）：排空窗口耗尽后 worker 还活着，Gateway 结清之后它才登记新调用。
    改前得到 settled=[visible-before-shutdown]、still_open=[started-after-shutdown-snapshot]；栅栏之后新调用被拒、不建记录。"""
    agent = SimpleNamespace()
    ledger = model_call_ledger(agent)
    _start(ledger, "visible-before-shutdown", request_id="gwreq-1")
    resume, outcome = threading.Event(), {}

    def late_worker():
        assert resume.wait(5)
        try:
            _start(ledger, "started-after-shutdown-snapshot", request_id="gwreq-1")
        except ModelCallAdmissionClosedError as exc:
            outcome["error"] = exc

    worker = threading.Thread(target=late_worker)
    worker.start()
    request, events = _cleanup_request(monkeypatch, [], agent)
    report = gateway_process._cmd_gateway_run_cleanup(request)
    resume.set()
    worker.join(5)

    assert report["interrupted_model_calls"] == 1
    assert [call["call_id"] for call in events[0][1]["calls"]] == ["visible-before-shutdown"]
    error = outcome["error"]
    assert error.error_code == ledger_module.MODEL_CALL_ADMISSION_CLOSED_ERROR_CODE == "MODEL_CALL_ADMISSION_CLOSED"
    assert error.closure.error_code == MODEL_CALL_INTERRUPTED_ERROR_CODE
    assert _still_open(ledger) == [] and [record.call_id for record in ledger.records()] == ["visible-before-shutdown"]
    counts = model_call_summary(agent, request_id="gwreq-1")["status_counts"]
    assert counts.get("failed") == 1 and not counts.get("started"), "被拒的调用不进累计数"
    _start(ledger, "visible-before-shutdown", request_id="gwreq-1")  # 重复登记同一 call_id 只返回原记录，不算新调用
    assert ledger.records()[0].status == "failed"


def test_ledger_registered_after_the_snapshot_cannot_bypass_the_fence(monkeypatch):
    """J17 必须修（sol2）：快照之后才建好的账本（迟到完成构建的 runner worker、owner 池 agent、直接新建的账本）
    改前不在那次遍历里，可以照常登记调用；现在构造时就登记进已关门的准入表，新调用一律被拒。"""
    from agent_py_agent.agent import core, owner_scoped_pool
    from agent_py_agent.agent.agent_core.runner.worker import _attach_worker_runtime
    from agent_py_agent.agent.settings.config import AgentConfig

    gateway = SimpleNamespace()
    _start(model_call_ledger(gateway), "gateway-call", request_id="gwreq-1")
    assert [fact["call_id"] for fact in settle_open_model_calls_for_shutdown()] == ["gateway-call"]

    worker = SimpleNamespace()
    late_worker_ledger = model_call_ledger(worker)
    _attach_worker_runtime(worker, _worker_params())
    monkeypatch.setattr(core, "SimpleAgent",
                        lambda config, root, **_kwargs: SimpleNamespace(config=config, _model_call_ledger=ModelCallLedger()))
    owner = SimpleNamespace(provider="local", owner_kind="user", owner_id="u-late")
    scoped = owner_scoped_pool.build_owner_scoped_agent(AgentConfig(), Path("."), owner, None)
    for ledger in (late_worker_ledger, scoped._model_call_ledger, ModelCallLedger()):
        with pytest.raises(ModelCallAdmissionClosedError):
            _start(ledger, "late-call", run_id="subagent-late")
        assert ledger.records() == ()
    assert settle_open_model_calls_for_shutdown() == (), "关门之后没有新的在途调用可结"


def test_calls_racing_an_in_progress_settlement_are_settled_or_rejected(monkeypatch):
    """结清进行到一半（已关门、还没轮到某本账）时：那本账里关门前已登记的调用照样被结清，新调用（含新建账本）被拒。
    用确定的交错顺序，不靠再扫一次。"""
    first, second = ModelCallLedger(), ModelCallLedger()
    _start(first, "first-open", request_id="gwreq-1")
    _start(second, "second-open", request_id="gwreq-2")
    original, paused, resume = ledger_module._fail_open_calls, threading.Event(), threading.Event()

    def pausing(ledger, closure):
        if ledger is first:
            paused.set()
            assert resume.wait(5)
        return original(ledger, closure)

    monkeypatch.setattr(ledger_module, "_fail_open_calls", pausing)
    facts = []
    settler = threading.Thread(target=lambda: facts.extend(settle_open_model_calls_for_shutdown()))
    settler.start()
    assert paused.wait(5)
    for ledger in (second, first, ModelCallLedger()):
        with pytest.raises(ModelCallAdmissionClosedError):
            _start(ledger, "during-settlement", request_id="gwreq-3")
    resume.set()
    settler.join(5)

    assert sorted(fact["call_id"] for fact in facts) == ["first-open", "second-open"]
    assert _still_open(first) == [] and _still_open(second) == []
    assert all(record.call_id != "during-settlement" for record in first.records() + second.records())


def test_shutdown_failure_keeps_late_transport_facts_until_retention_is_released():
    """sol2 建议 2：停机结清只定逻辑终态，物理线程可能还在退出。结清后迟到的 HTTP 观察照记、迟到的成功不重开终态；
    caller 和 worker 的保留权都放掉之后，这条明细才按原裁剪规则离开账本，累计数一直对得上。"""
    ledger = ModelCallLedger(ModelCallLedgerOptions(max_records=1))
    _record, caller = ledger.started_retained(ModelCallStartedParams(
        call_id="long-call", backend="anthropic_compatible", model="MiniMax-M2.7", input_tokens=26600, request_id="gwreq-9",
    ))
    worker = ledger.retain_call("long-call")
    _start(ledger, "short-call", request_id="gwreq-10")
    ledger.finished(ModelCallFinishParams(call_id="short-call", output_tokens=3, provider_usage_reported=True))

    assert [fact["call_id"] for fact in settle_open_model_calls_for_shutdown()] == ["long-call"]
    ledger.provider_attempt(ModelCallProviderAttemptParams("long-call", "late-http", "response_opened"))
    ledger.finished(ModelCallFinishParams(call_id="long-call", output_tokens=7, provider_usage_reported=True))

    record = {item.call_id: item for item in ledger.records()}["long-call"]
    assert (record.status, record.error_code) == ("failed", MODEL_CALL_INTERRUPTED_ERROR_CODE), "迟到的成功不重开终态"
    assert record.provider_attempt_count == 1, "迟到的 HTTP 观察照记"
    counts = ledger.cumulative_summary(request_id="gwreq-9")["status_counts"]
    assert counts.get("failed") == 1 and not counts.get("finished") and not counts.get("started")
    caller.release()
    assert "long-call" in [item.call_id for item in ledger.records()], "worker 还没退出，明细要留着"
    worker.release()
    assert [item.call_id for item in ledger.records()] == ["short-call"]
    assert ledger.cumulative_summary(request_id="gwreq-9")["status_counts"].get("failed") == 1
