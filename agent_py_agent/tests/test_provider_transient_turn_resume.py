"""供应商临时故障的回合级自动续跑（tresume）：失败不写终态，请求重排回队列，下一轮从已落盘历史继续。

规则：失败错误码属于白名单（MODEL_STREAM_INCOMPLETE 需本回合已有完成工具往返；
PROVIDER_TRANSIENT_RETRY_TIME_BUDGET_EXCEEDED 无条件）+ 配置开关开启 + 未达续跑上限
（provider_resume_count 跨 Gateway 重启累计）时，回合不写失败终态；worker 收尾把请求重排回
inbox（status=pending、not_before_at 退避、active_turn_recovery 标记），下一轮从会话历史继续、
不重放工具。请求拒绝类、零产出流中断、用户已停止、用满上限都照常失败收口。
"""
from __future__ import annotations

import json
import time
import uuid
from types import SimpleNamespace

from agent_py_agent.agent.backends.errors import (
    ProviderRequestRejectedError,
    ProviderTransientError,
)
from agent_py_agent.agent.conversation.turn_resume_notice import PROVIDER_RESUME_LIMIT_NOTICE
from agent_py_agent.agent.gateway_parts import recovery, request_execution
from agent_py_agent.agent.gateway_parts.paths import gateway_paths
from agent_py_agent.agent.gateway_parts.request_worker import _finish_claimed_gateway_request
from agent_py_agent.tests.test_gateway_safe_restart import _agent as _echo_agent

_BUDGET_CODE = "PROVIDER_TRANSIENT_RETRY_TIME_BUDGET_EXCEEDED"


def _gateway(agent):
    paths = gateway_paths(agent)
    for folder in (paths.inbox, paths.processing, paths.done, paths.failed, paths.responses):
        folder.mkdir(parents=True, exist_ok=True)
    return paths


def _processing(paths, request_id: str, **extra):
    path = paths.processing / f"{request_id}.json"
    path.write_text(json.dumps({"id": request_id, "kind": "ask", "prompt": "长任务", "attempts": 1,
                                "status": "processing", "turn_phase": "open",
                                "lease_started_at": time.time() - 10,
                                "execution_attempt_id": f"attempt-{uuid.uuid4().hex}", **extra}),
                    encoding="utf-8")
    return path


# 函数用途: 造一条与真实失败结果同形状的运行结果（流未完整结束/预算耗尽），供失败收口判定使用。
def _failed_result(reason: str, *, tool_rounds: int):
    return SimpleNamespace(
        channel_delivery={"content": ""}, response="", backend="test", used_memories=0,
        tool_rounds=tool_rounds, prompt="", prompt_token_estimate=0,
        runtime_injection_token_estimate=0, turn_token_estimate=0, cumulative_token_estimate=0,
        memory_resume_context_injected=False, memory_resume_context_query="",
        memory_resume_context_matches=0, memory_resume_context_token_estimate=0,
        memory_resume_context_error="", runtime_status="error", runtime_reason=reason,
        runtime_source="model_provider", turn_end_reason="error",
    )


def _budget_error():
    exc = ProviderTransientError("retry budget exhausted")
    exc.error_code = _BUDGET_CODE
    return exc


def _raise(exc):
    def run(_context):
        raise exc
    return run


# 函数用途: 读出重排后请求在 inbox 里的 payload（不存在即测试失败）。
def _inbox_payload(paths, request_id: str) -> dict:
    path = paths.inbox / f"{request_id}.json"
    assert path.exists(), "重排后请求必须回到 inbox"
    return json.loads(path.read_text(encoding="utf-8"))


# 函数用途: 断言这个请求没有留在 processing、也没有被重排回 inbox（即按失败正常收口）。
def _assert_failed_closeout(paths, request_id: str) -> None:
    assert not (paths.processing / f"{request_id}.json").exists()
    assert not (paths.inbox / f"{request_id}.json").exists()


def _run_turn(agent, paths, request_id: str):
    path = paths.processing / f"{request_id}.json"
    response = request_execution._handle_gateway_request(agent, path)
    _finish_claimed_gateway_request(paths, path, request_id, response)
    return response


def test_incomplete_with_tool_rounds_requeues_for_resume(tmp_path, monkeypatch):
    agent = _echo_agent(tmp_path, provider_transient_turn_resume_max_count=2)
    paths = _gateway(agent)
    _processing(paths, "gwreq-incomplete")
    monkeypatch.setattr(request_execution, "_run_gateway_ask",
                        lambda _context: _failed_result("MODEL_STREAM_INCOMPLETE", tool_rounds=3))

    response = _run_turn(agent, paths, "gwreq-incomplete")

    marker = response["provider_transient_resume"]
    assert marker["error_code"] == "MODEL_STREAM_INCOMPLETE"
    assert marker["resume_count"] == 1 and marker["tool_rounds"] == 3
    payload = _inbox_payload(paths, "gwreq-incomplete")
    assert payload["status"] == "pending" and payload["priority"] == "recovery"
    assert payload["not_before_at"] > time.time()
    marker_in_file = payload["active_turn_recovery"]
    assert marker_in_file["cause"] == "provider_transient_resume"
    assert marker_in_file["provider_resume_count"] == 1
    assert not payload.get("execution_attempt_id") and not payload.get("lease_owner")
    assert recovery.provider_resume_count(payload) == 1


def test_retry_budget_exceeded_requeues_for_resume(tmp_path, monkeypatch):
    agent = _echo_agent(tmp_path, provider_transient_turn_resume_max_count=2)
    paths = _gateway(agent)
    _processing(paths, "gwreq-budget")
    monkeypatch.setattr(request_execution, "_run_gateway_ask", _raise(_budget_error()))

    response = _run_turn(agent, paths, "gwreq-budget")

    assert response["provider_transient_resume"]["error_code"] == _BUDGET_CODE
    assert _inbox_payload(paths, "gwreq-budget")["status"] == "pending"


def test_request_rejected_does_not_requeue(tmp_path, monkeypatch):
    agent = _echo_agent(tmp_path, provider_transient_turn_resume_max_count=2)
    paths = _gateway(agent)
    _processing(paths, "gwreq-403")
    monkeypatch.setattr(request_execution, "_run_gateway_ask",
                        _raise(ProviderRequestRejectedError("HTTP 403")))

    response = _run_turn(agent, paths, "gwreq-403")

    assert "provider_transient_resume" not in response
    assert response["status"] == "failed"
    _assert_failed_closeout(paths, "gwreq-403")


def test_zero_output_stream_incomplete_does_not_requeue(tmp_path, monkeypatch):
    agent = _echo_agent(tmp_path, provider_transient_turn_resume_max_count=2)
    paths = _gateway(agent)
    _processing(paths, "gwreq-empty")
    monkeypatch.setattr(request_execution, "_run_gateway_ask",
                        lambda _context: _failed_result("MODEL_STREAM_INCOMPLETE", tool_rounds=0))

    response = _run_turn(agent, paths, "gwreq-empty")

    assert "provider_transient_resume" not in response
    assert response["status"] == "failed"
    _assert_failed_closeout(paths, "gwreq-empty")


def test_resume_limit_exhausted_fails_normally(tmp_path, monkeypatch):
    agent = _echo_agent(tmp_path, provider_transient_turn_resume_max_count=2)
    paths = _gateway(agent)
    _processing(paths, "gwreq-cap", active_turn_recovery={
        "schema_version": "gateway_active_turn_recovery.v1", "provider_resume_count": 2})
    monkeypatch.setattr(request_execution, "_run_gateway_ask",
                        lambda _context: _failed_result("MODEL_STREAM_INCOMPLETE", tool_rounds=5))

    response = _run_turn(agent, paths, "gwreq-cap")

    assert "provider_transient_resume" not in response
    assert response["status"] == "failed"
    _assert_failed_closeout(paths, "gwreq-cap")


def test_switch_off_does_not_requeue(tmp_path, monkeypatch):
    agent = _echo_agent(tmp_path, provider_transient_turn_resume_max_count=0)
    paths = _gateway(agent)
    _processing(paths, "gwreq-off")
    monkeypatch.setattr(request_execution, "_run_gateway_ask",
                        lambda _context: _failed_result("MODEL_STREAM_INCOMPLETE", tool_rounds=3))

    response = _run_turn(agent, paths, "gwreq-off")

    assert "provider_transient_resume" not in response
    assert response["status"] == "failed"
    _assert_failed_closeout(paths, "gwreq-off")


def test_resume_count_accumulates_and_survives_restart_marker(tmp_path, monkeypatch):
    agent = _echo_agent(tmp_path, provider_transient_turn_resume_max_count=3)
    paths = _gateway(agent)
    _processing(paths, "gwreq-acc")
    monkeypatch.setattr(request_execution, "_run_gateway_ask",
                        lambda _context: _failed_result("MODEL_STREAM_INCOMPLETE", tool_rounds=2))

    _run_turn(agent, paths, "gwreq-acc")
    first = _inbox_payload(paths, "gwreq-acc")
    assert recovery.provider_resume_count(first) == 1

    # 模拟下一轮再失败：把请求从 inbox 取回记成 processing（换新执行编号），计数累计到 2。
    pending = paths.inbox / "gwreq-acc.json"
    payload = {**json.loads(pending.read_text(encoding="utf-8")), "status": "processing",
               "execution_attempt_id": f"attempt-{uuid.uuid4().hex}"}
    (paths.processing / "gwreq-acc.json").write_text(json.dumps(payload), encoding="utf-8")
    pending.unlink()
    _run_turn(agent, paths, "gwreq-acc")
    second = _inbox_payload(paths, "gwreq-acc")
    assert recovery.provider_resume_count(second) == 2

    # 跨 Gateway 重启的重排（_active_turn_recovery_marker）必须原样保留这个计数。
    marker = recovery._active_turn_recovery_marker(
        "gwreq-acc", "dead-attempt", second,
        recovery._RecoveryContext(now=time.time(), max_attempts=2, timeout_seconds=900,
                                  startup=True, agent=agent, lease_stale_seconds=None),
    )
    assert marker["provider_resume_count"] == 2
    assert marker["unplanned_resume_count"] == 1


# ds10 初审补钉：取消、坏计数、重排失败退回三条原先没有用例（变异删检查能全绿）。
# 函数用途: 钉住“执行中用户发停止的回合不重排回队列，按取消收口”。
def test_mid_turn_cancel_does_not_requeue(tmp_path, monkeypatch):
    agent = _echo_agent(tmp_path, provider_transient_turn_resume_max_count=2)
    paths = _gateway(agent)
    _processing(paths, "gwreq-cancel")

    def _fail_and_cancel(context):
        payload = json.loads(context.request_path.read_text(encoding="utf-8"))
        payload["cancel_requested"] = True
        context.request_path.write_text(json.dumps(payload), encoding="utf-8")
        return _failed_result("MODEL_STREAM_INCOMPLETE", tool_rounds=3)

    monkeypatch.setattr(request_execution, "_run_gateway_ask", _fail_and_cancel)

    response = _run_turn(agent, paths, "gwreq-cancel")

    assert "provider_transient_resume" not in response
    assert response["status"] == "interrupted"
    assert not (paths.inbox / "gwreq-cancel.json").exists()


# 函数用途: 钉住坏计数 fail-closed——不重排、按失败收口，不把已用次数重置成 0。
def test_corrupt_resume_count_fails_closed(tmp_path, monkeypatch):
    agent = _echo_agent(tmp_path, provider_transient_turn_resume_max_count=2)
    paths = _gateway(agent)
    _processing(paths, "gwreq-badcount", active_turn_recovery={
        "schema_version": "gateway_active_turn_recovery.v1", "provider_resume_count": "abc"})
    monkeypatch.setattr(request_execution, "_run_gateway_ask",
                        lambda _context: _failed_result("MODEL_STREAM_INCOMPLETE", tool_rounds=3))

    response = _run_turn(agent, paths, "gwreq-badcount")

    assert "provider_transient_resume" not in response
    assert response["status"] == "failed"
    _assert_failed_closeout(paths, "gwreq-badcount")


# 函数用途: 钉住重排写盘失败退回正常失败归档——请求不留在 processing 卡死。
def test_requeue_failure_falls_back_to_failure_archive(tmp_path, monkeypatch):
    agent = _echo_agent(tmp_path, provider_transient_turn_resume_max_count=2)
    paths = _gateway(agent)
    _processing(paths, "gwreq-rqfail")
    monkeypatch.setattr(request_execution, "_run_gateway_ask",
                        lambda _context: _failed_result("MODEL_STREAM_INCOMPLETE", tool_rounds=3))
    monkeypatch.setattr(recovery, "requeue_provider_transient_processing",
                        lambda *_args, **_kwargs: False)

    _run_turn(agent, paths, "gwreq-rqfail")

    assert not (paths.processing / "gwreq-rqfail.json").exists()
    assert (paths.failed / "gwreq-rqfail.json").exists()
    # 重排失败退回时标记必须先清掉：不许落进终态响应与 responses 投影（ds10 初审小问题 1）。
    responses = json.loads((paths.responses / "gwreq-rqfail.json").read_text(encoding="utf-8"))
    terminal = json.loads((paths.terminal / "gwreq-rqfail.json").read_text(encoding="utf-8"))
    assert "provider_transient_resume" not in responses
    assert "provider_transient_resume" not in terminal


# 函数用途: 钉住用满时失败答复附“发继续接着做”提示（TUI/IM 同源 user_error/error，3a 10-05 裁定）。
def test_resume_limit_exhausted_shows_continue_notice(tmp_path, monkeypatch):
    agent = _echo_agent(tmp_path, provider_transient_turn_resume_max_count=2)
    paths = _gateway(agent)
    _processing(paths, "gwreq-limit", active_turn_recovery={
        "schema_version": "gateway_active_turn_recovery.v1", "provider_resume_count": 2})
    monkeypatch.setattr(request_execution, "_run_gateway_ask",
                        lambda _context: _failed_result("MODEL_STREAM_INCOMPLETE", tool_rounds=5))

    response = _run_turn(agent, paths, "gwreq-limit")

    assert response["user_error"] == PROVIDER_RESUME_LIMIT_NOTICE
    assert response["error"] == PROVIDER_RESUME_LIMIT_NOTICE
    assert "provider_transient_resume" not in response


# 函数用途: 钉住未用满（照常重排）时不出现“停止自动续跑”提示。
def test_not_exhausted_has_no_limit_notice(tmp_path, monkeypatch):
    agent = _echo_agent(tmp_path, provider_transient_turn_resume_max_count=3)
    paths = _gateway(agent)
    _processing(paths, "gwreq-under", active_turn_recovery={
        "schema_version": "gateway_active_turn_recovery.v1", "provider_resume_count": 1})
    monkeypatch.setattr(request_execution, "_run_gateway_ask",
                        lambda _context: _failed_result("MODEL_STREAM_INCOMPLETE", tool_rounds=5))

    response = _run_turn(agent, paths, "gwreq-under")

    assert response["provider_transient_resume"]["resume_count"] == 2
    assert response.get("user_error") != PROVIDER_RESUME_LIMIT_NOTICE
