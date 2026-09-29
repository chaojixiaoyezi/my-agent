"""进程工具只读查询在权威读不出时是「已知没开始」，不能记成 unknown 触发主代理收口。

来源（2026-09-29，dev 01:15 任务）：75 的 47bd37d20 给进程/PTY 补了 reported_error_code，但
effect_outcome 仍一律 unknown。unknown 会让 contracts/required_actions.settle_required_action
把动作标成 blocked，主代理据此收口、整轮停止调用工具——可 status/wait/network_status 这类
只读查询根本没有副作用，结果其实已知（就是没开始）；stop 如果在发出任何信号之前就失败，
同样是已知的「没开始」。这是和今天 my-agent-2/4 停工同一类的「不必要停摆」。

口径：错误码保持 TOOL_OPERATION_OUTCOME_UNKNOWN 不变，只改 effect_outcome；
区分只靠两个结构化事实——动作枚举，以及失败发生在发信号之前还是之后（不看错误文本）。
"""
from __future__ import annotations

import pytest

from agent_py_agent.agent.contracts.effective_contract_snapshot import (
    build_effective_contract_snapshot,
)
from agent_py_agent.agent.contracts.required_actions import RequiredAction, settle_required_action
from agent_py_agent.agent.tooling.process_registry import (
    ProcessSessionAuthorityError,
    process_registry,
)
from agent_py_agent.agent.tooling.process_session_cleanup import ProcessSessionCleanupError
from agent_py_agent.agent.tooling.process_session_records import LEGACY_PROCESS_SESSION_SCHEMA
from agent_py_agent.agent.tooling.process_sessions import ProcessSessionTool
from agent_py_agent.agent.tooling.runtime_contracts import ToolCall, ToolResult

UNKNOWN = "TOOL_OPERATION_OUTCOME_UNKNOWN"
SCOPE = {"owner_id": "owner-a", "session_id": "thread-a", "run_id": "run-a", "root_task_id": "task-a"}
READ_ONLY_ACTIONS = ("list", "status", "wait", "network_status")


@pytest.fixture(autouse=True)
def _clear_sessions():
    process_registry.clear()
    yield
    process_registry.clear()


def _authority_unreadable(*_args, **_kwargs):
    raise ProcessSessionAuthorityError({"error_type": "authority_missing"})


def _raising(error):
    def killer(*_args, **_kwargs):
        raise error

    return killer


@pytest.fixture
def authority_unreadable(monkeypatch):
    """让只读方法都读不出权威，但不改 stop（stop 另有分支）。"""
    for name in ("list_report", "status", "wait", "get"):
        monkeypatch.setattr(process_registry, name, _authority_unreadable)
    return monkeypatch


@pytest.mark.parametrize("action", READ_ONLY_ACTIONS)
def test_read_only_actions_are_not_started_when_authority_is_unreadable(action, authority_unreadable):
    """只读动作没有副作用：权威读不出 = 已知「没开始」，不是「结果未知」。"""
    params = {"action": action, "session_id": "bg-test", "__run_scope": SCOPE}
    outcome = ProcessSessionTool().execute(params)
    assert (outcome.ok, outcome.error_code) == (False, UNKNOWN)
    assert outcome.effect_outcome == "not_started"
    assert outcome.reported_error_code == "PROCESS_SESSION_AUTHORITY_UNREADABLE"


def _settle(effect_outcome: str):
    """把一次 process_session 失败结果喂给真实收口函数，返回那个 action 行。"""
    action = RequiredAction(
        action_id="required-1", source_turn_id="turn-1", kind="execute",
        allowed_tools=("process_session",), effect_ceiling="read_only",
    )
    contract = build_effective_contract_snapshot(run_id="run-1", layers=(), required_actions=(action,))
    call = ToolCall(call_id="call-1", tool_name="process_session", arguments={},
                    source_protocol="native", schema_hash="sha256:test", run_id="run-1",
                    turn_id="turn-1", attempt_id="attempt-1",
                    required_action_id="required-1")
    result = ToolResult(call_id="call-1", tool_name="process_session", status="failed",
                        error_code=UNKNOWN, effect_outcome=effect_outcome)
    settle_required_action(contract, call, result)
    return action


@pytest.mark.parametrize("action", READ_ONLY_ACTIONS)
def test_read_only_failure_does_not_block_the_required_action(action, authority_unreadable):
    """经真实收口函数走一遍：not_started 不该把动作标 blocked。

    settle_required_action 只对 effect_outcome == "unknown" 落 blocked；
    那是主代理「结果未知就收口」的判定点，所以这里必须断言它没被触发。
    """
    outcome = ProcessSessionTool().execute(
        {"action": action, "session_id": "bg-test", "__run_scope": SCOPE}
    )
    settled = _settle(outcome.effect_outcome)
    assert settled.status != "blocked"
    assert settled.blocked_reason != UNKNOWN


def test_unknown_still_blocks_the_required_action():
    """对照组：unknown 仍然会落 blocked —— 收口机制本身没被放宽。"""
    settled = _settle("unknown")
    assert settled.status == "blocked"
    assert settled.blocked_reason == UNKNOWN


def test_stop_before_any_signal_is_not_started(monkeypatch):
    """stop 在读记录阶段就失败，等于还没碰过进程树 → 已知的「没开始」。

    ProcessSessionAuthorityError 来自 `_visible_record_locked`（kill 的第一件事），
    此时进程树还没被碰过，不可能已有副作用。
    """
    monkeypatch.setattr(process_registry, "kill", _authority_unreadable)
    tool = ProcessSessionTool()
    outcome = tool.execute({"action": "stop", "session_id": "bg-test", "__run_scope": SCOPE})
    assert (outcome.ok, outcome.error_code) == (False, UNKNOWN)
    assert outcome.effect_outcome == "not_started"
    assert outcome.reported_error_code == "PROCESS_SESSION_AUTHORITY_UNREADABLE"


def test_read_only_failure_does_not_halt_the_loop(authority_unreadable):
    """经真正收口的入口：process_session 只读失败不该让这一轮停摆。

    `_mark_unknown_outcome_halt` 只看 effect_outcome != "unknown"，
    这是「结果未知就收口」的实际判定点（必做动作的 blocked 判定只是另一条）。
    这里把真实的 ProcessSessionTool 结果喂进真实入口，断言它没有收口。
    """
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core._tool_loop_service import _mark_unknown_outcome_halt

    outcome = ProcessSessionTool().execute(
        {"action": "status", "session_id": "bg-test", "__run_scope": SCOPE}
    )
    assert outcome.effect_outcome == "not_started"

    params = SimpleNamespace(repeated_failure_halt=None, unknown_outcome_halt=None, tool_context=[])
    record = SimpleNamespace(
        params=params,
        call=SimpleNamespace(tool_name="process_session"),
        result=SimpleNamespace(
            effect_outcome=outcome.effect_outcome,
            reported_error_code=outcome.reported_error_code,
            error_code=outcome.error_code,
            handler_executed=False,
        ),
    )
    _mark_unknown_outcome_halt(None, record)
    assert params.unknown_outcome_halt is None, "只读失败不该触发收口"
    assert params.tool_context == [], "不该追加收口提示"


def test_unknown_effect_still_halts_the_loop():
    """对照：effect=unknown 时收口照旧触发 —— 收口机制本身没被放宽。"""
    from types import SimpleNamespace

    from agent_py_agent.agent.agent_core._tool_loop_service import _mark_unknown_outcome_halt

    params = SimpleNamespace(repeated_failure_halt=None, unknown_outcome_halt=None, tool_context=[])
    record = SimpleNamespace(
        params=params,
        call=SimpleNamespace(tool_name="process_session"),
        result=SimpleNamespace(
            effect_outcome="unknown",
            reported_error_code="PROCESS_SESSION_CLEANUP_UNCONFIRMED",
            error_code=UNKNOWN,
            handler_executed=False,
        ),
    )
    _mark_unknown_outcome_halt(None, record)
    assert params.unknown_outcome_halt is not None
    assert params.tool_context, "unknown 应当追加收口提示"


def test_legacy_stop_failure_after_signal_is_unknown(monkeypatch):
    """v1（legacy）路径：发信号之后的读记录失败必须仍是 unknown。

    _kill_legacy 的顺序是 _refresh_locked（发信号前）→ terminate_process_tree（发信号）
    → 第二次 _visible_record_locked。第二次既可能因读取失败抛错，也可能因记录已不在而显式抛错，
    两者都发生在发信号之后；registry 会把它们翻译成 ProcessSessionCleanupError，
    这样工具层「AuthorityError 即发信号前」的判定才成立。
    """
    from agent_py_agent.agent.tooling.process_registry import (
        BackgroundProcess,
        ProcessTerminationReceipt,
    )
    from agent_py_agent.agent.tooling.process_registry import (
        process_registry as registry,
    )

    record = BackgroundProcess(
        session_id="legacy-1",
        command="true",
        pid=99999998,
        started_at=0.0,
        persisted_snapshot={"schema": LEGACY_PROCESS_SESSION_SCHEMA},
    )
    monkeypatch.setattr(registry, "_refresh_locked", lambda _record: None)
    monkeypatch.setattr(
        "agent_py_agent.agent.tooling.process_registry.terminate_process_tree",
        lambda *a, **k: ProcessTerminationReceipt("SIGTERM", True, 0, 1, ()),
    )
    monkeypatch.setattr(registry, "_visible_record_locked", _authority_unreadable)
    with pytest.raises(ProcessSessionCleanupError) as excinfo:
        registry._kill_legacy(record, None)
    assert excinfo.value.report["committed"] is True
    assert len(excinfo.value.report["termination_receipts"]) == 1


def test_stop_with_cleanup_failure_stays_unknown(monkeypatch):
    """stop 已提交停止意图、清理结果未定 → 仍未知（可能已有副作用，禁止自动重做）。

    ProcessSessionCleanupError 的语义就是「已提交意图与已经发过信号均不可降为未发生」。
    """
    error = ProcessSessionCleanupError(RuntimeError("commit failed"), {"session_id": "bg-test"}, (), False)
    monkeypatch.setattr(process_registry, "kill", _raising(error))
    tool = ProcessSessionTool()
    outcome = tool.execute({"action": "stop", "session_id": "bg-test", "__run_scope": SCOPE})
    assert outcome.error_code == UNKNOWN
    assert outcome.effect_outcome == "unknown"
    assert outcome.reported_error_code == "PROCESS_SESSION_CLEANUP_UNCONFIRMED"
