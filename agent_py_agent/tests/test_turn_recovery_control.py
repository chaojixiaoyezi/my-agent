"""/recover 会话控制：未知执行轮的查看与显式恢复。

钉住四件事：
1. 解析只认结构化处置值，无参数是只读查看。
2. 查看只列结构化操作事实，不改运行库。
3. 显式处置走唯一出口 recover_attempt_unknown，解除阻塞后下一轮可以挂载。
4. 被 unknown 拒绝挂载时抛带错误码的异常，客户端文案指向 /recover。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.gateway_parts import control_service
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.request_errors import gateway_client_error_message
from agent_py_agent.agent.gateway_parts.turn_recovery_control import execute_turn_recovery_control
from agent_py_agent.agent.runtime_db.operations import (
    RuntimeConflictError,
    RuntimeRecoveryRequiredError,
)
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.cli.chat_parts import control_runtime

_DEAD_PID = 999999999


def _dead_pid_or_skip() -> int:
    try:
        os.kill(_DEAD_PID, 0)
    except ProcessLookupError:
        return _DEAD_PID
    pytest.skip("测试用 pid 竟然存活")


def _insert_operation(repo, rec, *, status: str, started_at: float, tool: str = "run_command") -> str:
    operation_id = f"tool_operation:{uuid.uuid4().hex}"
    now = time.time()
    with repo.transaction() as conn:
        conn.execute(
            "INSERT INTO tool_operations(operation_id, agent_run_id, attempt_id, attempt_generation, "
            "tool_operation_generation, operation_type, status, handler_started_at, created_at, updated_at) "
            "VALUES(?,?,?,1,1,?,?,?,?,?)",
            (operation_id, rec["agent_run_id"], rec["attempt_id"], tool, status, started_at, now, now),
        )
    return operation_id


def _unknown_turn(tmp_path, task_id: str = "task-recover"):
    """造一条执行中断的主代理执行轮：一条已成功操作 + 一条执行中操作，再按进程死亡归 unknown。"""
    repo = RuntimeRepository(str(tmp_path / f"runtime-{uuid.uuid4().hex[:8]}.db"))
    rec = repo.record_run_creation(
        owner_id="local/main",
        goal="排查通道",
        conversation_task_id=task_id,
        thread_id="thread-recover",
        run_id=f"run-{uuid.uuid4().hex[:8]}",
        role="main",
    )
    with repo.transaction() as conn:
        conn.execute(
            "UPDATE agent_attempts SET metadata_json = ? WHERE attempt_id = ?",
            (json.dumps({"runner_pid": _dead_pid_or_skip(), "runner_start_time": None}), rec["attempt_id"]),
        )
    _insert_operation(repo, rec, status="SUCCEEDED", started_at=time.time() - 60)
    executing = _insert_operation(repo, rec, status="EXECUTING", started_at=time.time() - 5)
    assert repo.recover_stale_attempts() == [rec["agent_run_id"]]
    owner = SimpleNamespace(subagents=SimpleNamespace(runtime_db=repo))
    thread = SimpleNamespace(workspace_task_id=task_id)
    return repo, rec, owner, thread, executing


def _status(repo, rec) -> tuple[str, str]:
    with repo._runtime_connection() as conn:
        run = conn.execute("SELECT status FROM agent_runs WHERE agent_run_id=?", (rec["agent_run_id"],)).fetchone()
        attempt = conn.execute("SELECT status FROM agent_attempts WHERE attempt_id=?", (rec["attempt_id"],)).fetchone()
    return str(run["status"]), str(attempt["status"])


def test_recover_parses_view_apply_and_rejects_free_text():
    view = parse_conversation_control("/recover")
    assert (view.kind, view.operation, view.valid) == ("recover", "view", True)
    applied = parse_conversation_control("/recover  RECORDED ")
    assert (applied.kind, applied.operation, applied.value, applied.valid) == ("recover", "apply", "recorded", True)
    for value in ("confirmed_noop", "abandoned"):
        assert parse_conversation_control(f"/recover {value}").valid is True
    rejected = parse_conversation_control("/recover 已经好了")
    assert rejected.kind == "recover" and rejected.valid is False
    assert "recorded|confirmed_noop|abandoned" in rejected.usage


def test_view_lists_unsettled_operations_without_changing_state(tmp_path):
    repo, rec, owner, thread, _executing = _unknown_turn(tmp_path)

    result = execute_turn_recovery_control(owner, thread, parse_conversation_control("/recover"))

    assert result.ok is True and result.kind == "recover"
    assert "run_command｜执行中断，结果未回传" in result.message
    assert result.message.count("run_command") == 1, "已成功的操作不算未确认"
    assert "/recover recorded" in result.message and "/recover abandoned" in result.message
    assert _status(repo, rec) == ("unknown", "unknown")


def test_mount_is_refused_with_typed_code_until_explicit_recovery(tmp_path):
    repo, rec, owner, thread, executing = _unknown_turn(tmp_path)
    with pytest.raises(RuntimeRecoveryRequiredError) as refused:
        repo.create_attempt(rec["agent_run_id"])
    assert isinstance(refused.value, RuntimeConflictError)
    assert refused.value.error_code == "RUN_RECOVERY_REQUIRED"
    assert "/recover" in gateway_client_error_message("RUN_RECOVERY_REQUIRED")
    assert "/recover" in gateway_client_error_message("ACTIVE_TURN_OUTCOME_UNCERTAIN")

    result = execute_turn_recovery_control(owner, thread, parse_conversation_control("/recover recorded"))

    assert result.ok is True and "已核实生效并记下" in result.message
    assert _status(repo, rec) == ("created", "recovered")
    assert repo.main_agent_recovery_block_for_task("task-recover") is None
    with repo._runtime_connection() as conn:
        event = conn.execute(
            "SELECT payload_json FROM runtime_events WHERE event_type='attempt_recovered' AND attempt_id=?",
            (rec["attempt_id"],),
        ).fetchone()
    payload = json.loads(event["payload_json"])
    assert payload["operator"] == "conversation-control:/recover"
    assert payload["effect_disposition"] == "recorded"
    next_attempt = repo.create_attempt(rec["agent_run_id"])
    assert next_attempt["attempt_id"] != rec["attempt_id"]
    again = execute_turn_recovery_control(owner, thread, parse_conversation_control("/recover recorded"))
    assert again.ok is True and "无需恢复" in again.message
    assert executing not in again.message


def test_nothing_to_recover_without_thread_task_or_block(tmp_path):
    repo, _rec, owner, _thread, _executing = _unknown_turn(tmp_path)
    view = parse_conversation_control("/recover")
    assert "无需恢复" in execute_turn_recovery_control(owner, None, view).message
    other = SimpleNamespace(workspace_task_id="task-without-runs")
    assert "无需恢复" in execute_turn_recovery_control(owner, other, view).message
    assert repo.main_agent_recovery_block_for_task("task-recover") is not None


def test_block_that_is_not_an_unknown_attempt_is_refused(tmp_path):
    repo, rec, owner, thread, _executing = _unknown_turn(tmp_path)
    with repo.transaction() as conn:
        conn.execute("UPDATE agent_attempts SET status='running', ended_at=0 WHERE attempt_id=?", (rec["attempt_id"],))

    result = execute_turn_recovery_control(owner, thread, parse_conversation_control("/recover abandoned"))

    assert result.ok is False and result.error_code == "RUN_RECOVERY_REJECTED"
    assert _status(repo, rec) == ("unknown", "running")


def test_gateway_dispatch_resolves_scope_thread_for_recover(tmp_path, monkeypatch):
    repo, rec, owner, thread, _executing = _unknown_turn(tmp_path)
    seen: dict[str, object] = {}

    def resolve(**kwargs):
        seen.update(kwargs)
        return thread

    owner.conversation_store = SimpleNamespace(threads=SimpleNamespace(resolve=resolve))
    monkeypatch.setattr(control_service, "_request_agent_for_scope", lambda _base, _scope: owner)
    scope = control_service.GatewayControlScope(user_id="local-agent", channel="chat", conversation_id="sess-1")

    result = control_service.execute_gateway_conversation_control(
        object(), gateway_paths_from_root(tmp_path), parse_conversation_control("/recover confirmed_noop"), scope,
    )

    assert result.ok is True and "已核实没有生效" in result.message
    assert seen == {"channel": "chat", "channel_conversation_id": "sess-1", "channel_user_id": "local-agent"}
    assert _status(repo, rec) == ("created", "recovered")


def test_tui_serializes_recover_and_local_mode_requires_gateway():
    assert control_runtime._command_text(parse_conversation_control("/recover")) == "/recover"
    applied = parse_conversation_control("/recover abandoned")
    assert control_runtime._command_text(applied) == "/recover abandoned"
    execution = control_runtime.ChatControlExecution(
        agent=SimpleNamespace(config=SimpleNamespace()),
        use_gateway=False,
        state=control_runtime.ChatControlState(
            running=False, queued_count=0, prompt="", started_at=0.0, session_id="sess-local",
        ),
    )
    local = control_runtime.execute_chat_control(execution, applied)
    assert local.ok is False and "Gateway" in local.message
