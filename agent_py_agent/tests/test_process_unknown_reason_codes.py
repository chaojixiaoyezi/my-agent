"""后台进程与终端会话结果未知时带出具体原因码：错误码仍是 TOOL_OPERATION_OUTCOME_UNKNOWN（语义不变、不自动重做），
reported_error_code 说明卡在哪一步，工具操作账的 unknown_reason 随之可归因。

来源（2026-09-29）：定时任务停摆排查时，run_command 相关操作的 unknown_reason 只剩通用码；0b04e6fbe 补了 run_command
后台三处，这里补 process_session 的权威读不出 / 清理结果未定 / 停止未确认，以及 terminal_session 关闭未确认。
每处都是先写本文件、在旧代码上看到只报通用码，再改。
"""
from __future__ import annotations

import os
import shlex
import sys

import pytest

from agent_py_agent.agent.contracts.error_taxonomy import ERROR_CONTRACTS
from agent_py_agent.agent.contracts.recovery import RecoveryAction
from agent_py_agent.agent.tooling import pty_sessions as pty
from agent_py_agent.agent.tooling.process_registry import (
    ProcessSessionAuthorityError,
    ProcessTerminationReceipt,
    process_registry,
)
from agent_py_agent.agent.tooling.process_session_cleanup import ProcessSessionCleanupError
from agent_py_agent.agent.tooling.process_sessions import ProcessSessionTool
from agent_py_agent.agent.tooling.pty_sessions import pty_session_registry
from agent_py_agent.tests.test_pty_sessions import _payload
from agent_py_agent.tests.test_pty_sessions import _tool as _terminal_tool

UNKNOWN = "TOOL_OPERATION_OUTCOME_UNKNOWN"
SCOPE = {"owner_id": "owner-a", "session_id": "thread-a", "run_id": "run-a", "root_task_id": "task-a"}
NEW_CODES = ("PROCESS_SESSION_AUTHORITY_UNREADABLE", "PROCESS_SESSION_CLEANUP_UNCONFIRMED",
             "PROCESS_STOP_UNCONFIRMED", "PTY_CLOSE_UNCONFIRMED")


@pytest.fixture(autouse=True)
def _clear_sessions():
    process_registry.clear()
    pty_session_registry.clear()
    yield
    process_registry.clear()
    pty_session_registry.clear()


# 函数用途: 用替身顶替注册表的 stop，执行一次 process_session stop；不启动任何进程。
def _stop(monkeypatch, kill):
    monkeypatch.setattr(process_registry, "kill", kill)
    return ProcessSessionTool().execute({"action": "stop", "session_id": "bg-test", "__run_scope": SCOPE})


# 函数用途: 按给定异常构造一个会抛错的注册表替身。
def _raising(error):
    def kill(*_args):
        raise error

    return kill


@pytest.mark.parametrize(("error", "reported"), [
    (ProcessSessionAuthorityError({"error_type": "authority_missing"}), "PROCESS_SESSION_AUTHORITY_UNREADABLE"),
    (ProcessSessionCleanupError(RuntimeError("commit failed"), {"session_id": "bg-test"}, (), False),
     "PROCESS_SESSION_CLEANUP_UNCONFIRMED"),
], ids=["authority-unreadable", "cleanup-unresolved"])
def test_process_session_failures_report_their_cause(monkeypatch, error, reported):
    outcome = _stop(monkeypatch, _raising(error))
    assert (outcome.ok, outcome.error_code, outcome.effect_outcome) == (False, UNKNOWN, "unknown")
    assert outcome.reported_error_code == reported
    assert outcome.result_envelope["load_error"] == error.report


def test_unconfirmed_stop_reports_its_cause_and_keeps_the_receipt(monkeypatch):
    result = {"session_id": "bg-test", "status": "running", "termination": {"confirmed": False}}
    outcome = _stop(monkeypatch, lambda *_args: dict(result))
    assert (outcome.error_code, outcome.effect_outcome) == (UNKNOWN, "unknown")
    assert outcome.reported_error_code == "PROCESS_STOP_UNCONFIRMED"
    assert outcome.result_envelope["process"]["termination"] == {"confirmed": False}


def test_confirmed_stop_is_not_unknown(monkeypatch):
    result = {"session_id": "bg-test", "status": "killed", "termination": {"confirmed": True}}
    outcome = _stop(monkeypatch, lambda *_args: dict(result))
    assert outcome.ok is True and outcome.error_code == "" and outcome.reported_error_code == ""


@pytest.mark.skipif(os.name == "nt", reason="stdlib pty is POSIX-only")
def test_unconfirmed_terminal_close_reports_its_cause(tmp_path, monkeypatch):
    tool = _terminal_tool(tmp_path)
    started = _payload(tool.execute({"action": "start", "command": f"{shlex.quote(sys.executable)} -q"}))
    session = pty_session_registry.get(started["session_id"])
    with monkeypatch.context() as patch:
        patch.setattr(pty, "terminate_process_tree", lambda pid, proc: ProcessTerminationReceipt(
            "SIGTERM", False, None, 1, (pid,)))
        outcome = tool.execute({"action": "close", "session_id": session.session_id})
    assert (outcome.error_code, outcome.effect_outcome) == (UNKNOWN, "unknown")
    assert outcome.reported_error_code == "PTY_CLOSE_UNCONFIRMED"
    pty_session_registry.close(session.session_id)
    assert session.termination is not None and session.termination.confirmed


@pytest.mark.skipif(os.name == "nt", reason="stdlib pty is POSIX-only")
def test_confirmed_terminal_close_is_not_unknown(tmp_path):
    tool = _terminal_tool(tmp_path)
    started = _payload(tool.execute({"action": "start", "command": f"{shlex.quote(sys.executable)} -q"}))
    outcome = tool.execute({"action": "close", "session_id": started["session_id"]})
    assert outcome.ok is True and outcome.reported_error_code == ""


# 新码与 0b04e6fbe 的三个后台码同一口径：登记在错误合同里、不可自动重试、需要人工核对。
@pytest.mark.parametrize("code", NEW_CODES)
def test_new_codes_are_registered_for_manual_review(code):
    contract = ERROR_CONTRACTS[code]
    assert (contract.code, contract.retryable) == (code, False)
    assert contract.recommended_action == RecoveryAction.MANUAL_REVIEW.value


# 经工具操作账走一遍：记录停在 unknown，unknown_reason 带出具体原因，而不是通用码。
def test_ledger_unknown_reason_names_the_cause(tmp_path, monkeypatch):
    from agent_py_agent.agent.local_storage import LocalStore
    from agent_py_agent.tests.test_tool_operation_idempotency import (
        _CallSpec,
        _CountingTool,
        _execute,
        _registry,
    )

    monkeypatch.setattr(process_registry, "kill", lambda *_args: {"session_id": "bg-test", "status": "running",
                                                                   "termination": {"confirmed": False}})
    store = LocalStore(tmp_path / "local.db", enable_fts=False)
    tools = _registry(tmp_path, store, _CountingTool())
    call = _CallSpec(tool_name="process_session", arguments={"action": "stop", "session_id": "bg-test"},
                     run_id="run-1", call_id="call-1")
    result = _execute(tools, call)
    record = store.get_tool_operation(owner_id="owner-a", run_id="run-1", operation_id=call.operation_id)
    assert result.reported_error_code == "PROCESS_STOP_UNCONFIRMED"
    assert (record.status, record.unknown_reason) == ("unknown", "effect_outcome_unknown:PROCESS_STOP_UNCONFIRMED")
