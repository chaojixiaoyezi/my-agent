# LLM: 只验证账本批量终态、停机结清 facade 与 Gateway 收尾接线；文件/心跳/事件副作用全部替换，决策取消换成空操作以免
#   置位进程级关闭标记，不启动真实 Gateway，不发网络请求。改 _cmd_gateway_run_cleanup 顺序或事件名时同步这里与
#   test_gateway_decision_shutdown_cancel.py。
# 模块用途: 验证 Gateway 停止时仍在途的模型调用会被记成"被停机中断、未结算"，并写出结构化停机事件（用户决定第 4 项）。
from __future__ import annotations

import threading
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.model import call_runtime
from agent_py_agent.agent.agent_core.model.call_runtime import (
    MODEL_CALL_INTERRUPTED_ERROR_CODE,
    MODEL_CALL_INTERRUPTED_ERROR_TYPE,
    model_call_ledger,
    model_call_summary,
    settle_open_model_calls_for_shutdown,
)
from agent_py_agent.agent.contracts.model_call_ledger import (
    ModelCallFailureParams,
    ModelCallFinishParams,
    ModelCallFirstTokenParams,
    ModelCallLedger,
    ModelCallStartedParams,
)
from agent_py_agent.agent.conversation import decision_policy
from agent_py_agent.cli import gateway_process

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


def test_fail_open_calls_only_touches_active_records():
    ledger = ModelCallLedger()
    for call_id in ("waiting", "streaming", "done", "broken"):
        _start(ledger, call_id)
    ledger.first_token(ModelCallFirstTokenParams(call_id="streaming"))
    ledger.finished(ModelCallFinishParams(call_id="done", output_tokens=5, provider_usage_reported=True))
    ledger.failed(ModelCallFailureParams(call_id="broken", error_type="ProviderRequestRejectedError", error_code="HTTP_400"))
    interrupted = ledger.fail_open_calls(error_type="HostShutdownInterrupted", error_code="TEST_INTERRUPTED")
    assert sorted(record.call_id for record in interrupted) == ["streaming", "waiting"]
    assert all(record.status == "failed" and record.error_code == "TEST_INTERRUPTED" for record in interrupted)
    by_id = {record.call_id: record for record in ledger.records()}
    assert by_id["done"].status == "finished" and by_id["broken"].error_code == "HTTP_400", "已终态记录不动"
    assert ledger.fail_open_calls(error_type="HostShutdownInterrupted", error_code="TEST_INTERRUPTED") == ()


def test_settle_facade_never_creates_a_ledger_and_projects_structured_facts():
    bare = SimpleNamespace()
    assert settle_open_model_calls_for_shutdown(bare) == () and not hasattr(bare, "_model_call_ledger")
    agent = SimpleNamespace()
    _start(model_call_ledger(agent), "curator", run_id="memory-curator-run-1", auxiliary=True)
    facts = settle_open_model_calls_for_shutdown(agent)
    assert len(facts) == 1 and set(facts[0]) == _PROJECTION_FIELDS
    fact = facts[0]
    assert (fact["call_id"], fact["run_id"], fact["purpose"]) == ("curator", "memory-curator-run-1", "auxiliary")
    assert (fact["error_code"], fact["settlement"]) == (MODEL_CALL_INTERRUPTED_ERROR_CODE, "unsettled")
    assert fact["first_token_seen"] is False and fact["estimated_input_tokens"] == 26600
    record = model_call_ledger(agent).records()[0]
    assert record.status == "failed" and record.error_type == MODEL_CALL_INTERRUPTED_ERROR_TYPE
    counts = model_call_summary(agent, run_id="memory-curator-run-1")["status_counts"]
    assert counts.get("failed") == 1 and not counts.get("started"), "在途调用结成 failed，不再算运行中"
    assert settle_open_model_calls_for_shutdown(agent) == (), "重复结清没有第二份事实"


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
        call_runtime, "settle_open_model_calls_for_shutdown", lambda target: order.append("settle") or original(target)
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
    def broken(_agent):
        raise RuntimeError("secret detail that must not be logged")

    monkeypatch.setattr(call_runtime, "settle_open_model_calls_for_shutdown", broken)
    order = []
    request, events = _cleanup_request(monkeypatch, order, SimpleNamespace())
    report = gateway_process._cmd_gateway_run_cleanup(request)
    assert report["interrupted_model_calls"] == 0 and "heartbeat" in order
    failed = [payload for name, payload in events if name == "gateway_model_call_settlement_failed"]
    assert failed == [{"error_type": "RuntimeError"}], "只记异常类型，不记异常正文"
    assert [name for name, _payload in events][-1] == "gateway_run_cleanup"
