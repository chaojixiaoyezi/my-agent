"""工具结果未知时保留原始结论：unknown_reason、重放回执与受管账本都不能只剩通用的 TOOL_OPERATION_OUTCOME_UNKNOWN。

来源（2026-09-29 my-agent-2/4 定时任务停摆）：两条 run_command 操作的 unknown_reason 都是
effect_outcome_unknown:TOOL_OPERATION_OUTCOME_UNKNOWN、outcome_json.result 为空——handler 自己回报了通用码，
受管账本 UNKNOWN 分支又不写 result，诊断全丢。正反成对验证三处修复。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.local_storage import LocalStore
from agent_py_agent.agent.local_storage.tool_operations import (
    TOOL_OPERATION_UNKNOWN,
    ToolOperationClaim,
    ToolOperationClaimRequest,
    ToolOperationCompletionRequest,
    ToolOperationRecord,
    new_tool_operation_holder,
)
from agent_py_agent.agent.runtime_db.operation_store_selector import select_operation_store
from agent_py_agent.agent.tooling import shell
from agent_py_agent.agent.tooling.models import ToolHandlerOutcome
from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions
from agent_py_agent.agent.tooling.tool_operation_coordinator import _unknown_claim_result
from agent_py_agent.tests.test_tool_operation_idempotency import (
    _call,
    _CountingTool,
    _execute,
    _registry,
)
from agent_py_agent.tests.test_tool_operation_managed_gate import (
    _agent,
    _direct_register_chain,
    _repo,
    _store,
)

UNKNOWN = "TOOL_OPERATION_OUTCOME_UNKNOWN"


def _local_case(tmp_path, result):
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    registry = _registry(tmp_path, store, _CountingTool(result=result))
    call = _call("run-1", "call-1", 1)
    first, second = _execute(registry, call), _execute(registry, call)
    record = store.get_tool_operation(owner_id="owner-a", run_id="run-1", operation_id=call.operation_id)
    return first, second, record


def test_blocked_replay_keeps_the_original_reported_code(tmp_path):
    first, second, _record = _local_case(
        tmp_path, ToolHandlerOutcome("counting_write", False, "provider timed out", error_code="TOOL_TIMEOUT"))
    assert (first.error_code, first.reported_error_code) == (UNKNOWN, "TOOL_TIMEOUT")
    assert (second.error_code, second.handler_executed) == (UNKNOWN, False)
    assert second.reported_error_code == "TOOL_TIMEOUT"


@pytest.mark.parametrize(("reported", "reason"), [
    ("PROCESS_CLEANUP_UNCONFIRMED", "effect_outcome_unknown:PROCESS_CLEANUP_UNCONFIRMED"),
    ("", f"effect_outcome_unknown:{UNKNOWN}"),
], ids=["specific-cause", "generic-only"])
def test_handler_level_unknown_carries_its_specific_cause_into_the_ledger(tmp_path, reported, reason):
    first, second, record = _local_case(tmp_path, ToolHandlerOutcome(
        "counting_write", False, "cleanup pending", error_code=UNKNOWN, effect_outcome="unknown",
        reported_error_code=reported))
    assert record.status == "unknown" and record.unknown_reason == reason
    assert first.reported_error_code == (reported or UNKNOWN)
    assert second.reported_error_code == (reported or UNKNOWN)


# 函数用途: 造一条已是 UNKNOWN 的操作记录：result 是上一次提供方回报的结果，error_code 是记录层的码。
def _unknown_record(result, error_code):
    return ToolOperationRecord(
        owner_id="owner-a", run_id="run-1", task_id="", operation_id="op-1", tool="counting_write", args_hash="h",
        idempotency_key="k", idempotency_scope="business", idempotency_namespace="n", status="unknown",
        holder_id="h1", holder_host="h", holder_pid=1, holder_process_start_token="t", generation=1,
        lease_expires_at=0.0, result=result, error_code=error_code)


_PRIOR = {"schema_version": "tool_execution_result.v1", "tool": "counting_write", "ok": False, "output": "x",
          "error_code": UNKNOWN, "reported_error_code": "PROCESS_CLEANUP_UNCONFIRMED", "effect_outcome": "unknown"}


# 取码顺序：上一次回报的具体码 → 上一次的 error_code → 记录层的码；通用未知码一律跳过，都没有才回落通用码。
@pytest.mark.parametrize(("result", "record_code", "expected"), [
    (_PRIOR, "TOOL_TIMEOUT", "PROCESS_CLEANUP_UNCONFIRMED"),
    ({}, "TOOL_TIMEOUT", "TOOL_TIMEOUT"),
    ({**_PRIOR, "reported_error_code": UNKNOWN}, UNKNOWN, UNKNOWN),
    ({}, "", UNKNOWN),
], ids=["reported-beats-record", "generic-prior-falls-to-record", "all-generic", "nothing-known"])
def test_unknown_claim_result_takes_the_first_specific_code(result, record_code, expected):
    request = SimpleNamespace(tool_name="counting_write", operation_id="op-1", run_id="run-1",
                              idempotency_scope="business")
    claim = ToolOperationClaim(action="blocked", record=_unknown_record(result, record_code))
    outcome = _unknown_claim_result(request, claim, diagnostic="no_reconciler")
    assert (outcome.error_code, outcome.effect_outcome) == (UNKNOWN, "not_started")
    assert outcome.reported_error_code == expected


def _managed_unknown(tmp_path, result, error_code):
    repo = _repo(tmp_path)
    _run, attempt_id = _direct_register_chain(repo, "run-u", task_id="task-u")
    store_obj = select_operation_store(_agent(repo=repo, tools=None, store=_store(tmp_path), root=tmp_path))
    claim = store_obj.claim_tool_operation(ToolOperationClaimRequest(
        owner_id="owner-a", run_id="run-u", task_id="task-u", operation_id="tool_call:attempt-u:call-1",
        tool="u_write", args_hash="sha256:ab", idempotency_key="key-u-1", idempotency_scope="business",
        idempotency_namespace="u_write", holder=new_tool_operation_holder(), lease_expires_at=9999999999.0,
        resource_scopes=("workspace:/u-out",), attempt_id=attempt_id))
    return store_obj.finish_tool_operation(ToolOperationCompletionRequest(
        owner_id="owner-a", run_id="run-u", operation_id="tool_call:attempt-u:call-1",
        holder_id=claim.record.holder_id, generation=claim.record.generation, status=TOOL_OPERATION_UNKNOWN,
        result=result, error_code=error_code,
        unknown_reason="effect_outcome_unknown:PROCESS_CLEANUP_UNCONFIRMED"))


def test_managed_unknown_keeps_the_reported_result_and_code(tmp_path):
    result = {"schema_version": "x", "reported_error_code": "PROCESS_CLEANUP_UNCONFIRMED", "error_code": UNKNOWN,
              "result_envelope": {"process": {"return_code": 0, "command_succeeded": True}}}
    record = _managed_unknown(tmp_path, result, UNKNOWN)
    assert record.status == TOOL_OPERATION_UNKNOWN
    assert record.unknown_reason == "effect_outcome_unknown:PROCESS_CLEANUP_UNCONFIRMED"
    assert record.result == result and record.error_code == UNKNOWN


def test_managed_unknown_without_a_result_stays_empty(tmp_path):
    record = _managed_unknown(tmp_path, {}, "")
    assert (record.result, record.error_code) == ({}, "")


def test_background_launch_with_unconfirmed_cleanup_reports_its_cause():
    error = SimpleNamespace(record={"status": "unknown", "session_id": "s1", "revision": 1, "child_launch_started": True},
                            cleanup_confirmed=False, cause=RuntimeError("boom"), cleanup_error="")
    outcome = shell._background_launch_failure("run_command", error)
    assert (outcome.error_code, outcome.reported_error_code) == (UNKNOWN, "BACKGROUND_LAUNCH_CLEANUP_UNCONFIRMED")
    confirmed = SimpleNamespace(record={"status": "exited", "child_launch_started": False}, cleanup_confirmed=True,
                                cause=RuntimeError("boom"), cleanup_error="")
    control = shell._background_launch_failure("run_command", confirmed)
    assert control.error_code == "COMMAND_FAILED" and control.reported_error_code == "COMMAND_FAILED"


@pytest.mark.parametrize(("state", "reported"), [
    ({"status": "unknown"}, "BACKGROUND_SESSION_STATUS_UNCONFIRMED"),
    ({"status": "exited"}, "BACKGROUND_SESSION_STATUS_UNCONFIRMED"),
])
def test_background_start_with_unconfirmed_status_reports_its_cause(monkeypatch, tmp_path, state, reported):
    monkeypatch.setattr(shell.process_registry, "status", lambda *_args: dict(state))
    record = SimpleNamespace(session_id="s1", access_scope=None, store_root=str(tmp_path))
    outcome = shell._background_start_outcome(tool_name="run_command", record=record, log_path=tmp_path / "log")
    assert (outcome.error_code, outcome.reported_error_code) == (UNKNOWN, reported)
    assert outcome.result_envelope["process"]["session_id"] == "s1"


def test_background_start_running_is_not_unknown(monkeypatch, tmp_path):
    monkeypatch.setattr(shell.process_registry, "status", lambda *_args: {"status": "running"})
    record = SimpleNamespace(session_id="s1", access_scope=None, store_root=str(tmp_path))
    outcome = shell._background_start_outcome(tool_name="run_command", record=record, log_path=tmp_path / "log")
    assert outcome.ok is True and outcome.reported_error_code == ""


def test_background_attach_failure_reports_its_cause(monkeypatch, tmp_path):
    hosted = SimpleNamespace(record={"session_id": "s1"}, process=None, store_root=str(tmp_path))
    monkeypatch.setattr(shell, "_background_command_argv", lambda *_a, **_k: ["/usr/bin/true"])
    monkeypatch.setattr(shell, "start_background_process", lambda _request: hosted)

    # 函数用途: 模拟交接后查询持久 session 失败。
    def broken_attach(*_args):
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(shell.process_registry, "attach", broken_attach)
    tool = ShellTool(tmp_path, options=ShellToolOptions(owner_scope_root=str(tmp_path)))
    outcome = tool.execute({"command": "sleep 5", "run_in_background": True, "working_dir": str(tmp_path)})
    assert (outcome.error_code, outcome.effect_outcome) == (UNKNOWN, "unknown")
    assert outcome.reported_error_code == "BACKGROUND_SESSION_ATTACH_UNCONFIRMED"
