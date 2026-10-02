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


# 函数用途: 关门后在一本新账本上登记调用，取回真实的准入拒绝异常（与运行时同一条路径产生）。
def _admission_error() -> ModelCallAdmissionClosedError:
    settle_open_model_calls_for_shutdown()
    with pytest.raises(ModelCallAdmissionClosedError) as caught:
        _start(ModelCallLedger(), "late-call", run_id="subagent-late")
    return caught.value


def test_subagent_run_rejected_at_admission_keeps_the_stop_reason():
    """sol2 复审 J17 栅栏（2026-10-02）：子代理回合的新模型调用被准入拒绝，改前 _subagent_run_failure_type 兜底成 runner_error，
    写进结果和恢复快照的 error_code，还进了自动重跑名单。现在经 handle_run_failure 记成 model_call_admission_closed，不自动重跑。"""
    from agent_py_agent.agent.agent_core.subagent.lifecycle_service import SubagentLifecycleService
    from agent_py_agent.agent.agent_core.subagent.params import SubagentRunFailureParams
    from agent_py_agent.agent.agent_core.subagent_mixin import SimpleAgentSubagentMixin
    from agent_py_agent.agent.subagents.models import RETRYABLE_RUNNER_FAILURE_TYPES, FailureType
    from agent_py_agent.agent.subagents.runner_display_projection import runner_display_label

    # 类用途: 只替换结果账与快照落盘，失败分类与写入顺序走产品的 mixin。
    class _Agent(SimpleAgentSubagentMixin):
        # 函数用途: 记下写入的结果参数与快照参数。
        def __init__(self):
            self.results, self.snapshots = [], []
            self.subagents = SimpleNamespace(runner_result=SimpleNamespace(record_runner_result=self._record))

        # 函数用途: 记下结果参数，按产品结果对象的字段返回。
        def _record(self, params):
            self.results.append(params)
            return SimpleNamespace(status=params.status, message=params.message, failure_type=params.failure_type)

        # 函数用途: 记下恢复快照参数，不落盘。
        def _write_subagent_recovery_snapshot(self, params):
            self.snapshots.append(params)

    error = _admission_error()
    wrapped = RuntimeError("subagent model turn failed")
    wrapped.__cause__ = error
    for exc in (error, wrapped):
        agent = _Agent()
        result = SubagentLifecycleService(agent).handle_run_failure(SubagentRunFailureParams(
            "subagent-late", "attempt-1", exc, SimpleNamespace(goal="整理资料"), "prompt"))
        expected = FailureType.MODEL_CALL_ADMISSION_CLOSED.value
        assert [item.failure_type for item in agent.results] == [expected] and result.status == "FAILED"
        assert [(item.status, item.error_code) for item in agent.snapshots] == [("FAILED", expected)]
    assert expected not in RETRYABLE_RUNNER_FAILURE_TYPES, "本进程正在停机，自动重跑只会再被拒"
    assert runner_display_label("FAILED", expected) == "宿主停机中断"


def test_runtime_error_report_names_host_stopping_with_structured_codes():
    """sol2 建议：准入拒绝不能掉进 programmer_bug 兜底；报 host_stopping，原因码取异常的结构化属性，不读消息文本。"""
    from agent_py_agent.agent.runtime_errors import runtime_error_report

    error = _admission_error()
    wrapped = RuntimeError("wrapped")
    wrapped.__cause__ = error
    for exc in (error, wrapped):
        report = runtime_error_report(exc, context="subagent.run")
        assert (report["category"], report["recoverable"]) == ("host_stopping", False)
        assert (report["error_code"], report["reason_code"]) == ("MODEL_CALL_ADMISSION_CLOSED", MODEL_CALL_INTERRUPTED_ERROR_CODE)
    unrelated = RuntimeError("model call admission closed: MODEL_CALL_INTERRUPTED_HOST_SHUTDOWN")
    assert runtime_error_report(unrelated)["category"] == "programmer_bug", "只认结构化异常，不从消息文本反推"


def test_audit_source_rejected_at_admission_keeps_the_reason_and_is_not_redispatched(tmp_path, monkeypatch):
    """sol2 复审 38d7c8615 的必须修（2026-10-02）：Audit 来源子代理还有工作时，结果归并把 FAILED/model_call_admission_closed
    改成 PENDING 并清空失败类型，session 收尾随即 auto_start_orphan_run → auto_start_tasks 续派，绕过“停机后不自动重跑”，
    停机原因也丢了。走真实分类、结果归并、session 收尾续派和自动派发入口，只截获最终启动副作用：关门后不再请求启动新 attempt，
    run 保持 PENDING（重启后照常接续）并留着结构化停机原因。"""
    from agent_py_agent.agent.agent_core.orchestration.background import (
        dispatch as background_dispatch,
    )
    from agent_py_agent.agent.agent_core.runner.worker import _continue_pending_run_after_session
    from agent_py_agent.agent.agent_core.subagent.params import SubagentRunFailureParams
    from agent_py_agent.agent.agent_core.subagent_mixin import _handle_subagent_run_failure
    from agent_py_agent.agent.subagents.manager import SubAgentManager
    from agent_py_agent.tests.test_dispatch_liveness_and_revive import (
        _agent,
        _audit_source_binding_attributes,
        _make_child,
        _session,
    )

    manager = SubAgentManager(tmp_path / "subagents")
    session = {**_session(age_seconds=120.0), "attempt_id": "attempt-live-1"}
    task = _make_child(manager, status="RUNNING", session=session,
                       attributes=_audit_source_binding_attributes(audit_id="audit-1"))
    agent, snapshots, started = _agent(tmp_path, manager), [], []
    agent._write_subagent_recovery_snapshot = lambda params: snapshots.append(params)
    agent.dispatch_subagents = lambda *_args, **_kwargs: None  # 让真实 auto_start_tasks 走得到启动边界
    monkeypatch.setattr(background_dispatch, "_start_background_dispatch",
                        lambda _agent, run_ids, **_kwargs: started.append(list(run_ids)) or {"status": "started", "run_ids": run_ids})

    _handle_subagent_run_failure(agent, SubagentRunFailureParams(
        task.id, "attempt-live-1", _admission_error(), SimpleNamespace(goal="整理来源"), "prompt"))
    loaded = manager.load(task.id)
    assert (loaded.status, loaded.failure_type) == ("PENDING", "model_call_admission_closed"), "来源仍有工作：保持可接续，原因不丢"
    _continue_pending_run_after_session(agent, task.id, attempt_id="attempt-live-1")

    assert started == [], "关门后不再请求启动新 attempt"
    assert manager.load(task.id).status == "PENDING"


def test_auto_start_entry_refuses_new_runners_once_admission_closes(tmp_path, monkeypatch):
    """自动派发公共入口 auto_start_tasks：关门前照常启动；关门后返回 blocked/host_shutdown（带结构化码），不请求启动、不改 run。"""
    from agent_py_agent.agent.agent_core.orchestration.background import (
        dispatch as background_dispatch,
    )
    from agent_py_agent.agent.subagents.manager import SubAgentManager
    from agent_py_agent.tests.test_dispatch_liveness_and_revive import _agent, _make_child

    manager = SubAgentManager(tmp_path / "subagents")
    task = _make_child(manager, status="PENDING")
    agent, started = _agent(tmp_path, manager), []
    agent.dispatch_subagents = lambda *_args, **_kwargs: None
    monkeypatch.setattr(background_dispatch, "_start_background_dispatch",
                        lambda _agent, run_ids, **_kwargs: started.append(list(run_ids)) or {"status": "started", "run_ids": run_ids})

    assert background_dispatch.auto_start_tasks(agent, [manager.load(task.id)], {})["status"] == "started"
    settle_open_model_calls_for_shutdown()
    refused = background_dispatch.auto_start_tasks(agent, [manager.load(task.id)], {})

    assert started == [[task.id]], "只有关门前那一次请求了启动"
    assert (refused["status"], refused["reason"], refused["run_ids"], refused["skipped_run_ids"]) == (
        "blocked", "host_shutdown", [], [task.id])
    assert (refused["error_code"], refused["reason_code"]) == ("MODEL_CALL_ADMISSION_CLOSED", MODEL_CALL_INTERRUPTED_ERROR_CODE)
    assert manager.load(task.id).status == "PENDING"


@pytest.mark.parametrize(("failure_type", "kept"), [
    ("model_call_admission_closed", "model_call_admission_closed"),
    ("host_shutdown_interrupted", "host_shutdown_interrupted"),
    ("runner_error", ""),
])
def test_source_merge_keeps_only_the_shutdown_reason_when_it_turns_pending(tmp_path, failure_type, kept):
    """结果归并规则：来源仍有工作时改回 PENDING 以便接续；停机准入拒绝的原因留着，其它失败类型照旧清空。"""
    from agent_py_agent.agent.subagents.manager import SubAgentManager
    from agent_py_agent.agent.subagents.runner_result_state import _apply_status_fields
    from agent_py_agent.tests.test_dispatch_liveness_and_revive import (
        _audit_source_binding_attributes,
        _make_child,
    )

    manager = SubAgentManager(tmp_path / "subagents")
    task = _make_child(manager, status="RUNNING", attributes=_audit_source_binding_attributes(audit_id="audit-1"))
    _apply_status_fields(task, {"status": "FAILED", "verification_status": "UNVERIFIED", "failure_type": failure_type},
                         SimpleNamespace(failure_type=""))

    assert (task.status, task.failure_type) == ("PENDING", kept)


def test_admission_error_lookup_follows_explicit_causes_only():
    """sol2 建议：原因链边界钉成长期用例——无匹配的环能终止、很深的显式原因链也能找到、只有 __context__ 的不算停机。"""
    error = _admission_error()
    first, second = RuntimeError("a"), RuntimeError("b")
    first.__cause__, second.__cause__ = second, first
    assert ledger_module.find_model_call_admission_error(first) is None

    deep = error
    for _ in range(10_000):
        wrapper = RuntimeError("wrapped")
        wrapper.__cause__ = deep
        deep = wrapper
    assert ledger_module.find_model_call_admission_error(deep) is error

    implicit = RuntimeError("raised while handling")
    implicit.__context__ = error
    assert ledger_module.find_model_call_admission_error(implicit) is None



@pytest.mark.parametrize("closed", [False, True], ids=["admission-open", "admission-closed"])
def test_child_closeout_keeps_the_waiting_parent_pending_after_admission_closes(tmp_path, monkeypatch, closed):
    """75 复审 38d7c8615（2026-10-02）：嵌套子代理收尾时 _resume_direct_parent_after_session 先删父级的等待标记，再在停机进程里
    启动父级——父级第一次调用被拒成 FAILED/model_call_admission_closed、尝试次数 +1，还逐层往上传；进程直接退出反而保留
    PENDING + 等待标记，重启后等待调和照常续上。走真实等待调和、孤儿续派和自动派发入口，只截获最终启动副作用：
    准入开着时照常释放并启动（对照）；关门后不释放标记、不请求启动，父级保持 PENDING + 等待标记。
    I5（2026-10-02）补上重启这一步：关门期间等待调和照样不释放（reason=host_shutdown）；换一张开着的准入表（新进程）后，
    reconcile_all_parent_waits 释放父级的等待标记，父级重新可派。"""
    from agent_py_agent.agent.agent_core.orchestration.background import (
        dispatch as background_dispatch,
    )
    from agent_py_agent.agent.agent_core.runner.worker import _resume_direct_parent_after_session
    from agent_py_agent.agent.subagents.direct_parent_lifecycle import (
        mark_parent_waiting_for_direct_children,
        parent_wait_blocks_dispatch,
        reconcile_all_parent_waits,
    )
    from agent_py_agent.agent.subagents.manager import SubAgentManager
    from agent_py_agent.tests.test_dispatch_liveness_and_revive import _agent

    manager = SubAgentManager(tmp_path / "subagents")
    parent = manager.create_run(goal="父", thought="t", plan=["p"], role="coordinator")
    parent.status = "PENDING"
    manager.save(parent)
    child = manager.create_run(goal="子", thought="t", plan=["p"], role="worker")
    child.parent_id, child.root_id, child.depth, child.status = parent.id, parent.id, 1, "RUNNING"
    manager.save(child)
    manager.add_child(parent.id, child.id)
    mark_parent_waiting_for_direct_children(manager, parent.id, [child.id])
    child = manager.load(child.id)
    child.status, child.failure_type = "FAILED", "model_call_admission_closed"
    manager.save(child)
    agent, started = _agent(tmp_path, manager), []
    agent.dispatch_subagents = lambda *_args, **_kwargs: None
    monkeypatch.setattr(background_dispatch, "_start_background_dispatch",
                        lambda _agent, run_ids, **_kwargs: started.append(list(run_ids)) or {"status": "started", "run_ids": run_ids})
    if closed:
        settle_open_model_calls_for_shutdown()

    _resume_direct_parent_after_session(agent, child.id)

    loaded = manager.load(parent.id)
    assert started == ([] if closed else [[parent.id]])
    assert loaded.status == "PENDING" and parent_wait_blocks_dispatch(loaded) is closed, "关门后等待标记必须还在"
    if not closed:
        return
    assert reconcile_all_parent_waits(manager)["released_run_ids"] == [], "关门期间的等待调和也不释放"
    monkeypatch.setattr(ledger_module, "_ADMISSION_REGISTRY", ledger_module._ModelCallAdmissionRegistry())

    reconciled = reconcile_all_parent_waits(manager)

    assert (reconciled["checked"], reconciled["released_run_ids"]) == (1, [parent.id]), "重启后等待调和释放父级"
    released = manager.load(parent.id)
    assert released.status == "PENDING" and not parent_wait_blocks_dispatch(released)


def test_gateway_cleanup_marks_in_process_executors_after_settlement(monkeypatch):
    """自然停机（step17c 预演观察 1）：关门结清之后给本进程在跑的子代理执行器打停机记号，个数写进收尾载荷。"""
    from agent_py_agent.agent.runtime_db import executor_liveness

    order = []
    original = call_runtime.settle_open_model_calls_for_shutdown
    monkeypatch.setattr(call_runtime, "settle_open_model_calls_for_shutdown", lambda: order.append("settle") or original())
    monkeypatch.setattr(executor_liveness, "mark_in_process_executors_host_shutdown", lambda: order.append("mark") or 2)
    request, events = _cleanup_request(monkeypatch, order, SimpleNamespace())

    report = gateway_process._cmd_gateway_run_cleanup(request)

    assert order.index("settle") < order.index("mark") < order.index("heartbeat"), "先关门结清，再打记号，最后写心跳"
    assert report["host_shutdown_executors"] == 2 and events[-1][1]["host_shutdown_executors"] == 2


def test_gateway_cleanup_survives_a_failing_executor_mark(monkeypatch):
    from agent_py_agent.agent.runtime_db import executor_liveness

    def broken():
        raise RuntimeError("secret detail that must not be logged")

    order = []
    monkeypatch.setattr(executor_liveness, "mark_in_process_executors_host_shutdown", broken)
    request, events = _cleanup_request(monkeypatch, order, SimpleNamespace())
    report = gateway_process._cmd_gateway_run_cleanup(request)

    assert report["host_shutdown_executors"] == 0 and "heartbeat" in order
    failed = [payload for name, payload in events if name == "gateway_executor_shutdown_mark_failed"]
    assert failed == [{"error_type": "RuntimeError"}], "只记异常类型，不记异常正文"
