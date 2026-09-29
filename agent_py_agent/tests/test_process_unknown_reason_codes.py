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


# LLM: authority-unreadable 走的是「读记录阶段就失败」→ effect 是已知的没开始；
#   cleanup-unresolved 说明已提交停止意图、可能已发信号 → 保持 unknown。
@pytest.mark.parametrize(("error", "reported", "effect"), [
    (ProcessSessionAuthorityError({"error_type": "authority_missing"}), "PROCESS_SESSION_AUTHORITY_UNREADABLE",
     "not_started"),
    (ProcessSessionCleanupError(RuntimeError("commit failed"), {"session_id": "bg-test"}, (), False),
     "PROCESS_SESSION_CLEANUP_UNCONFIRMED", "unknown"),
], ids=["authority-unreadable", "cleanup-unresolved"])
def test_process_session_failures_report_their_cause(monkeypatch, error, reported, effect):
    outcome = _stop(monkeypatch, _raising(error))
    assert (outcome.ok, outcome.error_code, outcome.effect_outcome) == (False, UNKNOWN, effect)
    assert outcome.reported_error_code == reported
    assert outcome.result_envelope["load_error"] == error.report
    # 清理结果未定和权威读不出给模型的说法不同（ae 复审建议 3）。
    cleanup = reported == "PROCESS_SESSION_CLEANUP_UNCONFIRMED"
    assert ("停止或清理没有完成确认" in outcome.output) is cleanup
    assert ("权威暂不可读取" in outcome.output) is not cleanup


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
    # 终止回执结构化附在 process.termination（ae 复审建议 2），与后台进程停止未确认同一形状。
    facts = outcome.result_envelope["process"]
    assert facts["session_id"] == session.session_id
    assert (facts["termination"]["method"], facts["termination"]["confirmed"]) == ("SIGTERM", False)
    assert tuple(facts["termination"]["unresolved_pids"]) == (session.process.pid,)
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


# ae 复审建议 4：托管停止在第一个事务里发现权威记录已不存在时，一个信号都没发，按“权威读不出”处理，
# 与旧版路径（记录消失抛 ProcessSessionAuthorityError）同一口径，不再报成“停止未确认”。
def test_managed_stop_of_a_vanished_record_sends_nothing_and_reports_authority_missing(tmp_path, monkeypatch):
    from agent_py_agent.agent.tooling import process_session_cleanup as cleanup_module
    from agent_py_agent.tests.test_background_handoff import _bound_record

    store, record = _bound_record(tmp_path, "running")
    monkeypatch.setattr(cleanup_module, "_terminate_frozen_instances",
                        lambda *_args: pytest.fail("record is gone; no signal may be sent"))
    result = cleanup_module.stop_process_session(store, {**record, "session_id": "bg-vanished"})
    assert (result.authority_missing, result.confirmed, result.terminations) == (True, False, ())


def test_registry_stop_raises_authority_missing_and_the_tool_reports_it(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from agent_py_agent.agent.tooling import process_session_cleanup as cleanup_module
    from agent_py_agent.agent.tooling.process_session_cleanup import ProcessSessionCleanup
    from agent_py_agent.tests.test_background_handoff import _bound_record

    _store, record = _bound_record(tmp_path, "running")
    visible = SimpleNamespace(to_record=lambda: dict(record), process=None, store_root=str(tmp_path))
    monkeypatch.setattr(process_registry, "_visible_record_locked", lambda *_args: visible)
    monkeypatch.setattr(cleanup_module, "stop_process_session",
                        lambda *_args, **_kwargs: ProcessSessionCleanup(record, False, authority_missing=True))
    with pytest.raises(ProcessSessionAuthorityError):
        process_registry.kill(record["session_id"])
    outcome = ProcessSessionTool().execute({"action": "stop", "session_id": record["session_id"], "__run_scope": SCOPE})
    assert outcome.reported_error_code == "PROCESS_SESSION_AUTHORITY_UNREADABLE"
    assert outcome.effect_outcome == "not_started", "第一事务发现记录不在：没发信号，结果已知是没开始"
    assert outcome.result_envelope["load_error"] == {"error_type": "authority_missing"}
