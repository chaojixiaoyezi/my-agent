"""管理员 owner 级历史 unknown 执行轮恢复（C9）。

全部用临时 runtime.db 钉住：不挂会话线程的根/子代理只读投影、可信管理员边界、
目标集合确认码、集合变化失效、共享 unknown→recovered CAS、事件字段和重复确认幂等。
测试不读取真实运行库，也不读取任务或会话正文。
"""
from __future__ import annotations

import json
import time
import uuid
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.gateway_parts import control_service
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.turn_recovery_control import execute_turn_recovery_control
from agent_py_agent.agent.runtime_db import owner_recovery
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository

_ADMIN = SimpleNamespace(owner_provider="local", owner_kind="main", owner_id="local/main")
_NON_ADMIN = SimpleNamespace(owner_provider="feishu", owner_kind="user", owner_id="providers/feishu/users/ou-x")


@pytest.fixture
def owner_world(tmp_path):
    repo = RuntimeRepository(tmp_path / "runtime.db")
    root = repo.record_run_creation(
        owner_id="local/main", run_id="owner-root", role="main", goal="owner-secret-must-not-render",
    )
    child = repo.record_run_creation(
        owner_id="local/main", run_id="owner-child", role="worker", parent_run_id="owner-root",
    )
    threaded = repo.record_run_creation(
        owner_id="local/main", run_id="threaded-root", role="main", thread_id="thread-live",
    )
    foreign = repo.record_run_creation(
        owner_id="providers/feishu/users/ou-x", run_id="foreign-root", role="main",
    )
    for record in (root, child, threaded, foreign):
        _mark_unknown(repo, record)
    # 历史 TaskRun 可能已有关闭事实；owner 入口仍应以 current attempt=unknown 为准列出并处置。
    with repo.transaction() as conn:
        conn.execute("UPDATE task_runs SET closed_at=1 WHERE task_run_id=?", (root["task_run_id"],))
    _operation(repo, root, "EXECUTING")
    _operation(repo, root, "SUCCEEDED")
    owner = SimpleNamespace(
        subagents=SimpleNamespace(runtime_db=repo),
        home_paths=_ADMIN,
        conversation_store=SimpleNamespace(threads=SimpleNamespace(resolve=lambda **_kwargs: None)),
    )
    thread = SimpleNamespace(thread_id="thread-command", workspace_task_id="")
    return SimpleNamespace(
        repo=repo, owner=owner, thread=thread, root=root, child=child, threaded=threaded, foreign=foreign,
    )


def _mark_unknown(repo, record) -> None:
    with repo.transaction() as conn:
        conn.execute(
            "UPDATE agent_attempts SET status='unknown', ended_at=? WHERE attempt_id=?",
            (time.time(), record["attempt_id"]),
        )
        conn.execute(
            "UPDATE agent_runs SET status='unknown' WHERE agent_run_id=?",
            (record["agent_run_id"],),
        )


def _operation(repo, record, status: str) -> None:
    now = time.time()
    with repo.transaction() as conn:
        conn.execute(
            "INSERT INTO tool_operations(operation_id, agent_run_id, attempt_id, attempt_generation, "
            "tool_operation_generation, operation_type, status, handler_started_at, created_at, updated_at) "
            "VALUES(?,?,?,1,1,'write_file',?,?,?,?)",
            (f"tool_operation:{uuid.uuid4().hex}", record["agent_run_id"], record["attempt_id"],
             status, now - 2, now, now),
        )


def _run(world, text: str, *, owner=None):
    command = parse_conversation_control(text)
    assert command is not None
    return execute_turn_recovery_control(owner or world.owner, world.thread, command)


def _status(repo, record) -> tuple[str, str]:
    with repo._runtime_connection() as conn:
        row = conn.execute(
            "SELECT ar.status AS run_status, aa.status AS attempt_status FROM agent_runs ar "
            "JOIN agent_attempts aa ON aa.attempt_id=ar.current_attempt_id WHERE ar.agent_run_id=?",
            (record["agent_run_id"],),
        ).fetchone()
    return str(row["run_status"]), str(row["attempt_status"])


def _events(repo, event_type: str) -> list[dict[str, object]]:
    with repo._runtime_connection() as conn:
        rows = conn.execute(
            "SELECT payload_json FROM runtime_events WHERE event_type=? ORDER BY seq", (event_type,),
        ).fetchall()
    return [json.loads(row["payload_json"]) for row in rows]


def _preview(world, disposition: str = "recorded"):
    result = _run(world, f"/recover owner {disposition}")
    details = getattr(result, "details", {}) or {}
    assert result.ok is True and details.get("confirmation_code")
    return result, str(details["confirmation_code"])


def test_owner_view_is_read_only_and_lists_only_unthreaded_unknowns(owner_world):
    before = {name: _status(owner_world.repo, getattr(owner_world, name))
              for name in ("root", "child", "threaded", "foreign")}

    result = _run(owner_world, "/recover owner")

    assert result.ok is True
    assert "owner-root" in result.message and "根代理" in result.message
    assert "owner-child" in result.message and "子代理" in result.message
    assert "未确认操作 1" in result.message and "未确认操作 0" in result.message
    assert "threaded-root" not in result.message and "foreign-root" not in result.message
    assert "owner-secret-must-not-render" not in result.message
    assert getattr(result, "details", {}).get("target_count") == 2
    assert before == {name: _status(owner_world.repo, getattr(owner_world, name))
                      for name in ("root", "child", "threaded", "foreign")}
    assert _events(owner_world.repo, "attempt_recovered") == []


def test_owner_recovery_requires_complete_trusted_local_admin_identity(owner_world):
    denied_owner = SimpleNamespace(
        subagents=owner_world.owner.subagents,
        home_paths=_NON_ADMIN,
        conversation_store=owner_world.owner.conversation_store,
    )

    denied = _run(owner_world, "/recover owner", owner=denied_owner)

    assert denied.ok is False and denied.error_code == "RUN_RECOVERY_REJECTED"
    assert getattr(denied, "details", {}).get("reason") == "admin_required"
    incomplete_owner = SimpleNamespace(
        subagents=owner_world.owner.subagents,
        home_paths=SimpleNamespace(owner_provider="", owner_kind="", owner_id="local/main"),
        conversation_store=owner_world.owner.conversation_store,
    )
    incomplete = _run(owner_world, "/recover owner recorded", owner=incomplete_owner)
    assert incomplete.ok is False and getattr(incomplete, "details", {}).get("reason") == "admin_required"
    assert _events(owner_world.repo, "attempt_recovered") == []


def test_targeted_recovery_without_thread_cannot_reach_owner_history(owner_world):
    with owner_world.repo.transaction() as conn:
        conn.execute(
            "UPDATE task_runs SET closed_at=0 WHERE task_run_id=?",
            (owner_world.root["task_run_id"],),
        )
    denied_owner = SimpleNamespace(
        subagents=owner_world.owner.subagents,
        home_paths=_NON_ADMIN,
        conversation_store=owner_world.owner.conversation_store,
    )
    command = parse_conversation_control("/recover recorded owner-root")
    assert command is not None and command.valid is True

    denied = execute_turn_recovery_control(denied_owner, None, command)

    assert denied.ok is False and denied.error_code == "RUN_RECOVERY_REJECTED"
    assert getattr(denied, "details", {}).get("reason") == "target_out_of_scope"
    assert _status(owner_world.repo, owner_world.root) == ("unknown", "unknown")
    assert _status(owner_world.repo, owner_world.child) == ("unknown", "unknown")
    assert _events(owner_world.repo, "attempt_recovered") == []


def test_owner_confirmation_code_expires_when_target_set_changes(owner_world):
    _preview_result, code = _preview(owner_world)
    added = owner_world.repo.record_run_creation(
        owner_id="local/main", run_id="owner-added", role="main",
    )
    _mark_unknown(owner_world.repo, added)

    stale = _run(owner_world, f"/recover owner recorded --confirm {code}")

    assert stale.ok is False and stale.error_code == "RUN_RECOVERY_REJECTED"
    assert getattr(stale, "details", {}).get("reason") == "target_set_changed"
    assert _status(owner_world.repo, owner_world.root) == ("unknown", "unknown")
    assert _status(owner_world.repo, owner_world.child) == ("unknown", "unknown")
    assert _status(owner_world.repo, added) == ("unknown", "unknown")
    assert _events(owner_world.repo, "attempt_recovered") == []


def test_owner_confirm_uses_shared_cas_and_records_structured_events(owner_world):
    _preview_result, code = _preview(owner_world, "confirmed_noop")

    applied = _run(owner_world, f"/recover owner confirmed_noop --confirm {code}")

    details = getattr(applied, "details", {}) or {}
    assert applied.ok is True
    assert details == {
        "scope": "owner", "target_count": 2, "success_count": 2, "skipped_count": 0,
        "reason_counts": {}, "confirmation_code": code, "idempotent": False,
    }
    assert _status(owner_world.repo, owner_world.root) == ("created", "recovered")
    assert _status(owner_world.repo, owner_world.child) == ("created", "recovered")
    assert _status(owner_world.repo, owner_world.threaded) == ("unknown", "unknown")
    assert _status(owner_world.repo, owner_world.foreign) == ("unknown", "unknown")
    events = _events(owner_world.repo, "attempt_recovered")
    assert len(events) == 2
    assert {event["recovery_target"] for event in events} == {"root_agent_run", "child_agent_run"}
    assert all(event["recovery_source"] == "owner_history" for event in events)
    assert all(event["owner_id"] == "local/main" for event in events)
    assert all(event["owner_recovery_confirmation_code"] == code for event in events)


def test_owner_partial_success_reports_committed_work_as_success(owner_world, monkeypatch):
    _preview_result, code = _preview(owner_world, "recorded")
    original = owner_recovery._recover_unknown_attempt_conn
    call_count = 0

    def recover_first_only(repository, conn, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            return {"recovered": False, "reason": "not_unknown"}
        return original(repository, conn, **kwargs)

    monkeypatch.setattr(owner_recovery, "_recover_unknown_attempt_conn", recover_first_only)

    applied = _run(owner_world, f"/recover owner recorded --confirm {code}")

    assert applied.ok is True
    assert applied.message == '已处理 1 条，跳过 1 条（原因码计数：{"not_unknown": 1}）。'
    assert getattr(applied, "details", {}) == {
        "scope": "owner", "target_count": 2, "success_count": 1, "skipped_count": 1,
        "reason_counts": {"not_unknown": 1}, "confirmation_code": code, "idempotent": False,
    }
    assert _status(owner_world.repo, owner_world.root) == ("created", "recovered")
    assert _status(owner_world.repo, owner_world.child) == ("unknown", "unknown")
    assert len(_events(owner_world.repo, "attempt_recovered")) == 1
    assert len(_events(owner_world.repo, "owner_recovery.completed")) == 1


def test_owner_confirmation_is_idempotent_and_feishu_uses_same_gateway_entry(owner_world, tmp_path, monkeypatch):
    _preview_result, code = _preview(owner_world, "abandoned")
    command = parse_conversation_control(f"/recover owner abandoned --confirm {code}")
    monkeypatch.setattr(control_service, "_request_agent_for_scope", lambda _base, _scope: owner_world.owner)
    scope = control_service.GatewayControlScope(
        user_id="ou-admin", channel="feishu", conversation_id="oc-owner-recover",
    )

    first = control_service.execute_gateway_conversation_control(
        object(), gateway_paths_from_root(tmp_path), command, scope,
    )
    event_count = len(_events(owner_world.repo, "attempt_recovered"))
    repeated = control_service.execute_gateway_conversation_control(
        object(), gateway_paths_from_root(tmp_path), command, scope,
    )

    assert first.ok is True and getattr(first, "details", {}).get("success_count") == 2
    assert repeated.ok is True and getattr(repeated, "details", {}).get("idempotent") is True
    assert getattr(repeated, "details", {}).get("success_count") == 2
    assert len(_events(owner_world.repo, "attempt_recovered")) == event_count == 2
    assert len(_events(owner_world.repo, "owner_recovery.completed")) == 1
