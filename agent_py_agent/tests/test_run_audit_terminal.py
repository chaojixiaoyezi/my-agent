"""修复①+② 单测：run 级审计终态收口 + 假完成如实收口。

覆盖：
- Repository.settle_agent_run：单事务 CAS、幂等、先到先得、多 attempt 收口、
  事件绑定 current attempt（A.8）、旧 attempt 换代收口、查无 run noop。
- runtime_mixin._settle_main_agent_run / _settle_main_agent_run_exception：
  ok→done、非终态（unfinished 等）不落账（R1-03）、cancelled 族别名、
  InterruptedError→cancelled、LOCAL_UNMANAGED noop、repo 异常 fail-silent。
- _finalization_service._honest_unfinished_response_text：ok 原样、
  非 ok 如实标注（附状态/原因/来源 + 模型原文）。
- _schedule_typed_unfinished_continuation：返工门 unfinished → 预算内续跑；
  blocked / 非门来源不续跑。
"""

from __future__ import annotations

import json
import time
import uuid
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core._finalization_service import (
    _schedule_typed_unfinished_continuation,
)
from agent_py_agent.agent.agent_core._runtime_params import FinalizeContext
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.agent_core.runtime_mixin import (
    _run_once_with_params,
    _settle_main_agent_run,
    _settle_main_agent_run_exception,
)
from agent_py_agent.agent.agent_core.tool_loop.response_decision import (
    _persist_protocol_violation_event,
)
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository


@pytest.fixture
def repo(tmp_path):
    return RuntimeRepository(tmp_path / "home" / "runtime.db")


def _record(repo, run_id="run-1"):
    return repo.record_run_creation(
        owner_id="local/main",
        goal="audit-terminal",
        run_id=run_id,
        role="main",
    )


def _run_row(repo, agent_run_id):
    return repo._runtime_connect().execute(
        "SELECT * FROM agent_runs WHERE agent_run_id = ?", (agent_run_id,)
    ).fetchone()


def _attempts(repo, agent_run_id):
    return repo._runtime_connect().execute(
        "SELECT * FROM agent_attempts WHERE agent_run_id = ? ORDER BY started_at",
        (agent_run_id,),
    ).fetchall()


def _task_run_row(repo, task_run_id):
    return repo._runtime_connect().execute(
        "SELECT * FROM task_runs WHERE task_run_id = ?", (task_run_id,)
    ).fetchone()


def _completed_events(repo, agent_run_id):
    return repo._runtime_connect().execute(
        "SELECT * FROM runtime_events WHERE agent_run_id = ? AND event_type = 'agent_run.completed' "
        "ORDER BY seq",
        (agent_run_id,),
    ).fetchall()


def _insert_unknown_tool_operation(repo, rec) -> str:
    operation_id = uuid.uuid4().hex
    conn = repo._runtime_connect()
    try:
        conn.execute(
            """
            INSERT INTO tool_operations(operation_id, attempt_id, agent_run_id,
                                        attempt_generation, tool_operation_generation,
                                        operation_type, status, handler_started_at,
                                        outcome_json, created_at, updated_at)
            VALUES(?, ?, ?, 1, 0, 'test', 'UNKNOWN', 1, ?, ?, ?)
            """,
            (
                operation_id,
                rec["attempt_id"],
                rec["agent_run_id"],
                json.dumps({"side_effect": True}),
                time.time(),
                time.time(),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return operation_id


# ---------------------------------------------------------------- settle CAS


def test_settle_writes_terminal_status_and_event(repo):
    rec = _record(repo)
    result = repo.settle_agent_run(
        agent_run_id=rec["agent_run_id"],
        status="done",
        payload={"status": "done", "tool_rounds": 3},
    )
    assert result["settled"] is True
    row = _run_row(repo, rec["agent_run_id"])
    assert row["status"] == "done"
    attempts = _attempts(repo, rec["agent_run_id"])
    assert len(attempts) == 1
    assert attempts[0]["status"] == "done"
    assert attempts[0]["ended_at"] > 0
    events = _completed_events(repo, rec["agent_run_id"])
    assert len(events) == 1
    assert events[0]["attempt_id"] == rec["attempt_id"]
    assert events[0]["agent_run_id"] == rec["agent_run_id"]
    assert '"tool_rounds": 3' in events[0]["payload_json"]


def test_settle_idempotent_second_noop(repo):
    rec = _record(repo)
    first = repo.settle_agent_run(agent_run_id=rec["agent_run_id"], status="done")
    second = repo.settle_agent_run(
        agent_run_id=rec["agent_run_id"], status="failed", payload={"n": 2}
    )
    assert first["settled"] is True
    assert second == {"settled": False, "reason": "already_terminal"}
    row = _run_row(repo, rec["agent_run_id"])
    assert row["status"] == "done"
    assert len(_completed_events(repo, rec["agent_run_id"])) == 1


def test_new_attempt_reopens_terminal_run_and_can_settle_again(repo):
    """显式新 attempt 是新一轮：原子重开 run，并由新 attempt 再次收口。"""
    rec = _record(repo)
    assert repo.settle_agent_run(
        agent_run_id=rec["agent_run_id"], status="done"
    )["settled"] is True

    second = repo.create_attempt(rec["agent_run_id"])

    reopened = _run_row(repo, rec["agent_run_id"])
    assert reopened["status"] == "created"
    started = repo._runtime_connect().execute(
        "SELECT * FROM runtime_events WHERE agent_run_id = ? "
        "AND event_type = 'agent_run.started' ORDER BY seq DESC LIMIT 1",
        (rec["agent_run_id"],),
    ).fetchone()
    assert started is not None
    assert started["attempt_id"] == second["attempt_id"]
    assert '"previous_status": "done"' in started["payload_json"]

    result = repo.settle_agent_run(
        agent_run_id=rec["agent_run_id"],
        status="done",
        attempt_id=second["attempt_id"],
    )
    assert result["settled"] is True
    assert _run_row(repo, rec["agent_run_id"])["status"] == "done"
    assert len(_completed_events(repo, rec["agent_run_id"])) == 2


def test_settle_first_write_wins_cas(repo):
    rec = _record(repo)
    repo.settle_agent_run(agent_run_id=rec["agent_run_id"], status="cancelled")
    repo.settle_agent_run(agent_run_id=rec["agent_run_id"], status="done")
    assert _run_row(repo, rec["agent_run_id"])["status"] == "cancelled"


def test_settle_unknown_run_noop(repo):
    assert repo.settle_agent_run(agent_run_id="no-such", status="done") == {
        "settled": False,
        "reason": "no_such_run",
    }


def test_settle_preserves_superseded_attempt_and_closes_current(repo):
    rec = _record(repo)
    second = repo.create_attempt(rec["agent_run_id"])
    repo.settle_agent_run(
        agent_run_id=rec["agent_run_id"],
        status="done",
        payload={"status": "done"},
    )
    attempts = _attempts(repo, rec["agent_run_id"])
    assert len(attempts) == 2
    assert attempts[0]["status"] == "cancelled"
    assert attempts[0]["ended_at"] > 0
    assert attempts[1]["status"] == "done"
    assert attempts[1]["ended_at"] > 0
    # 事件绑定 current attempt（第二个）
    events = _completed_events(repo, rec["agent_run_id"])
    assert events[0]["attempt_id"] == second["attempt_id"]


def test_settle_rejects_non_terminal_status(repo):
    """R1-03：unfinished 是任务级可恢复语义，不是 agent_runs.status 合法终态。

    settle 拒写 + status_conflict 诊断事件；attempt 保持 running（发现层
    继续驱动），绝不写 settled/completed。
    """
    rec = _record(repo)
    result = repo.settle_agent_run(
        agent_run_id=rec["agent_run_id"],
        status="unfinished",
        payload={"status": "unfinished"},
    )
    assert result == {"settled": False, "reason": "invalid_status"}
    row = _run_row(repo, rec["agent_run_id"])
    assert row["status"] == "created"
    attempts = _attempts(repo, rec["agent_run_id"])
    assert len(attempts) == 1
    assert attempts[0]["status"] == "running"
    assert attempts[0]["ended_at"] == 0
    assert len(_completed_events(repo, rec["agent_run_id"])) == 0
    conflict = repo._runtime_connect().execute(
        "SELECT * FROM runtime_events WHERE agent_run_id = ? AND event_type = 'status_conflict'",
        (rec["agent_run_id"],),
    ).fetchall()
    assert len(conflict) == 1


def test_settle_event_falls_back_to_latest_attempt(repo):
    rec = _record(repo)
    second = repo.create_attempt(rec["agent_run_id"])
    conn = repo._runtime_connect()
    conn.execute(
        "UPDATE agent_runs SET current_attempt_id = '' WHERE agent_run_id = ?",
        (rec["agent_run_id"],),
    )
    conn.commit()
    conn.close()
    repo.settle_agent_run(agent_run_id=rec["agent_run_id"], status="done")
    events = _completed_events(repo, rec["agent_run_id"])
    assert events[0]["attempt_id"] == second["attempt_id"]


# ------------------------------------------------- runtime_mixin 终态挂钩


def _agent(repo, *, link_status: str = "active"):
    store = SimpleNamespace(
        load_task_link=lambda task_id: SimpleNamespace(
            task_id=task_id,
            status=link_status,
        )
    )
    return SimpleNamespace(
        subagents=SimpleNamespace(runtime_db=repo),
        conversation_store=store,
    )


def _params(run_id="run-1", attempt_id="attempt-1"):
    return SimpleNamespace(run_id=run_id, attempt_id=attempt_id)


def test_main_settle_ok_maps_to_done(repo):
    rec = _record(repo)
    result = SimpleNamespace(
        runtime_status="ok", runtime_reason="", runtime_source="", tool_rounds=2
    )
    _settle_main_agent_run(_agent(repo), _params(run_id="run-1", attempt_id=rec["attempt_id"]), result)
    row = _run_row(repo, rec["agent_run_id"])
    assert row["status"] == "done"
    payload = _completed_events(repo, rec["agent_run_id"])[0]["payload_json"]
    assert '"runtime_status": "ok"' in payload
    assert '"tool_rounds": 2' in payload


def test_persisted_completed_link_closes_task_run_without_transient_flag(repo):
    rec = _record(repo)
    result = SimpleNamespace(
        runtime_status="ok", runtime_reason="", runtime_source="", tool_rounds=1
    )
    params = SimpleNamespace(
        run_id="run-1",
        task_id=rec["task_id"],
        attempt_id=rec["attempt_id"],
        task_attributes={},
    )

    _settle_main_agent_run(_agent(repo, link_status="completed"), params, result)

    task_run = _task_run_row(repo, rec["task_run_id"])
    assert task_run["status"] == "done"
    assert task_run["closed_at"] > 0
    events = repo._runtime_connect().execute(
        "SELECT * FROM runtime_events WHERE task_run_id = ? "
        "AND event_type = 'task_run.closed'",
        (rec["task_run_id"],),
    ).fetchall()
    assert len(events) == 1
    assert '"reason": "conversation_task_completed"' in events[0]["payload_json"]


def test_active_goal_link_keeps_task_run_open_even_with_transient_completion_flag(repo):
    rec = _record(repo)
    result = SimpleNamespace(
        runtime_status="ok", runtime_reason="", runtime_source="", tool_rounds=1
    )
    params = SimpleNamespace(
        run_id="run-1",
        task_id=rec["task_id"],
        attempt_id=rec["attempt_id"],
        task_attributes={"conversation_task_completed": True},
    )

    _settle_main_agent_run(_agent(repo, link_status="active"), params, result)

    assert _task_run_row(repo, rec["task_run_id"])["closed_at"] == 0


def test_last_child_terminal_edge_closes_already_terminal_conversation_task(repo):
    rec = _record(repo)
    child = repo.record_run_creation(
        owner_id="local/main",
        goal="child",
        run_id="child-1",
        role="worker",
        parent_run_id="run-1",
    )
    repo.settle_agent_run(
        agent_run_id=rec["agent_run_id"],
        status="done",
        attempt_id=rec["attempt_id"],
    )
    result = SimpleNamespace(
        runtime_status="ok", runtime_reason="", runtime_source="", tool_rounds=1
    )
    params = SimpleNamespace(
        run_id="child-1",
        task_id="child-1",
        attempt_id=child["attempt_id"],
        task_attributes={},
    )

    _settle_main_agent_run(_agent(repo, link_status="completed"), params, result)

    task_run = _task_run_row(repo, rec["task_run_id"])
    assert task_run["status"] == "done"
    assert task_run["closed_at"] > 0


def test_task_run_closeout_preserves_unknown_tool_operation(repo):
    rec = _record(repo)
    operation_id = _insert_unknown_tool_operation(repo, rec)
    repo.settle_agent_run(
        agent_run_id=rec["agent_run_id"],
        status="done",
        attempt_id=rec["attempt_id"],
    )

    result = repo.settle_task_run_if_agent_tree_terminal(
        task_run_id=rec["task_run_id"],
        task_id=rec["task_id"],
    )

    assert result["settled"] is True
    operation = repo._runtime_connect().execute(
        "SELECT status FROM tool_operations WHERE operation_id = ?",
        (operation_id,),
    ).fetchone()
    assert operation is not None
    assert operation["status"] == "UNKNOWN"


def test_nonterminal_child_keeps_completed_conversation_task_run_open(repo):
    rec = _record(repo)
    child = repo.record_run_creation(
        owner_id="local/main",
        goal="child",
        run_id="child-1",
        role="worker",
        parent_run_id="run-1",
    )
    repo.settle_agent_run(
        agent_run_id=rec["agent_run_id"],
        status="done",
        attempt_id=rec["attempt_id"],
    )

    blocked = repo.settle_task_run_if_agent_tree_terminal(
        task_run_id=rec["task_run_id"],
        task_id=rec["task_id"],
    )

    assert blocked == {"settled": False, "reason": "agent_tree_active"}
    assert _task_run_row(repo, rec["task_run_id"])["closed_at"] == 0
    repo.settle_agent_run(
        agent_run_id=child["agent_run_id"],
        status="done",
        attempt_id=child["attempt_id"],
    )
    settled = repo.settle_task_run_if_agent_tree_terminal(
        task_run_id=rec["task_run_id"],
        task_id=rec["task_id"],
    )
    assert settled["settled"] is True
    assert settled["status"] == "done"


def test_main_settle_unfinished_not_terminal(repo):
    """R1-03：unfinished 只关闭当前 attempt，不把任务 run 写成终态。

    run 保持 created，发现层下轮显式创建新 attempt；旧轮释放执行权，
    不写 agent_run.completed 或 status_conflict。
    """
    rec = _record(repo)
    result = SimpleNamespace(
        runtime_status="unfinished",
        runtime_reason="REQUIRED_ACTION_HAS_NO_EVIDENCE",
        runtime_source="required_action_completion_gate",
        tool_rounds=0,
    )
    _settle_main_agent_run(_agent(repo), _params(run_id="run-1", attempt_id=rec["attempt_id"]), result)
    assert _run_row(repo, rec["agent_run_id"])["status"] == "created"
    assert _completed_events(repo, rec["agent_run_id"]) == []
    assert len(_attempts(repo, rec["agent_run_id"])) == 1
    assert _attempts(repo, rec["agent_run_id"])[0]["status"] == "done"
    assert _attempts(repo, rec["agent_run_id"])[0]["ended_at"] > 0
    event = repo._runtime_connect().execute(
        "SELECT * FROM runtime_events WHERE agent_run_id = ? "
        "AND event_type = 'agent_attempt.completed'",
        (rec["agent_run_id"],),
    ).fetchone()
    assert event is not None
    assert event["attempt_id"] == rec["attempt_id"]


def test_resumable_attempt_close_allows_explicit_next_attempt(repo):
    """可续跑 attempt 结束后必须先失权，再由新 attempt 原子重获执行权。"""
    rec = _record(repo)
    result = repo.settle_agent_attempt(
        agent_run_id=rec["agent_run_id"],
        attempt_id=rec["attempt_id"],
        payload={"runtime_status": "unfinished"},
    )
    assert result["settled"] is True
    assert repo.settle_agent_attempt(
        agent_run_id=rec["agent_run_id"],
        attempt_id=rec["attempt_id"],
    ) == {"settled": False, "reason": "already_terminal"}

    second = repo.create_attempt(rec["agent_run_id"])

    assert second["status"] == "running"
    assert second["attempt_id"] != rec["attempt_id"]
    assert repo.current_attempt(rec["agent_run_id"])["attempt_id"] == second["attempt_id"]


def test_main_settle_cli_oneshot_blocked_maps_failed(repo):
    """CLI 一次性 run 以 blocked 真实结束 → 兜底落 failed 终态（不悬挂 attempt）。

    问题C同族(2026-08-14 真机): aiohttp 首轮 break 收口后 attempt 仍卡
    running/ended_at=0; local/main 遗留 20+ 条 created/running。CLI one-shot
    没有 gateway 发现层再驱动, run 真实结束即必须落终态。
    """
    rec = _record(repo)
    result = SimpleNamespace(
        runtime_status="blocked",
        runtime_reason="TOOL_PROTOCOL_VIOLATION",
        runtime_source="tool_loop",
        tool_rounds=7,
    )
    params = SimpleNamespace(
        run_id="run-1", attempt_id=rec["attempt_id"], source="cli_run"
    )
    _settle_main_agent_run(_agent(repo), params, result)
    row = _run_row(repo, rec["agent_run_id"])
    assert row["status"] == "failed"
    attempts = _attempts(repo, rec["agent_run_id"])
    assert len(attempts) == 1
    assert attempts[0]["status"] == "failed"
    assert attempts[0]["ended_at"] > 0
    events = _completed_events(repo, rec["agent_run_id"])
    assert len(events) == 1
    payload = events[0]["payload_json"]
    assert '"runtime_status": "blocked"' in payload
    assert '"runtime_reason": "TOOL_PROTOCOL_VIOLATION"' in payload
    assert '"tool_rounds": 7' in payload


def test_main_settle_cli_oneshot_unfinished_maps_failed(repo):
    """缺口E(双席复核 seq1835): CLI 一次性 run 收口不可续跑族(blocked) →
    兜底 failed(问题C同族不回退)。TOOL_ROUND_LIMIT_REACHED 已属可续跑族
    (共享 gate True → 不落账, 由 resume_loop 续跑), 换 blocked 作不可续跑
    族代表场景。"""
    rec = _record(repo)
    result = SimpleNamespace(
        runtime_status="blocked",
        runtime_reason="MISSING_EVIDENCE",
        runtime_source="acceptance_gate",
        tool_rounds=43,
    )
    params = SimpleNamespace(
        run_id="run-1", attempt_id=rec["attempt_id"], source="cli_run"
    )
    _settle_main_agent_run(_agent(repo), params, result)
    row = _run_row(repo, rec["agent_run_id"])
    assert row["status"] == "failed"
    attempts = _attempts(repo, rec["agent_run_id"])
    assert attempts[0]["status"] == "failed"
    assert attempts[0]["ended_at"] > 0
    payload = _completed_events(repo, rec["agent_run_id"])[0]["payload_json"]
    assert '"runtime_status": "blocked"' in payload


def test_main_settle_user_stop_maps_cancelled(repo):
    rec = _record(repo)
    result = SimpleNamespace(
        runtime_status="user_stop", runtime_reason="", runtime_source="", tool_rounds=0
    )
    _settle_main_agent_run(_agent(repo), _params(run_id="run-1", attempt_id=rec["attempt_id"]), result)
    assert _run_row(repo, rec["agent_run_id"])["status"] == "cancelled"


def test_main_settle_exception_interrupted_maps_cancelled(repo):
    rec = _record(repo)
    _settle_main_agent_run_exception(
        _agent(repo), _params(run_id="run-1", attempt_id=rec["attempt_id"]), InterruptedError()
    )
    assert _run_row(repo, rec["agent_run_id"])["status"] == "cancelled"


def test_main_settle_exception_other_maps_failed(repo):
    rec = _record(repo)
    _settle_main_agent_run_exception(
        _agent(repo), _params(run_id="run-1", attempt_id=rec["attempt_id"]), ValueError("boom")
    )
    assert _run_row(repo, rec["agent_run_id"])["status"] == "failed"


def test_run_once_context_prepare_failure_settles_bound_attempt(repo, monkeypatch):
    """能力探针/上下文准备在模型循环前失败，也必须关闭已绑定的权威 attempt。"""
    from agent_py_agent.agent.agent_core import runtime_mixin

    rec = _record(repo)
    agent = SimpleNamespace(
        config=SimpleNamespace(enable_tools=False, auto_save_memory=False),
        subagents=SimpleNamespace(runtime_db=repo),
    )
    params = RunParams(
        request_id="req-pre-context-failure",
        run_id="run-1",
        task_id="task-1",
        attempt_id=rec["attempt_id"],
        save=False,
    )

    def fail_before_model(*_args, **_kwargs):
        raise ValueError("provider capability probe failed")

    monkeypatch.setattr(runtime_mixin, "_prepare_runtime_context", fail_before_model)

    with pytest.raises(ValueError, match="capability probe failed"):
        _run_once_with_params(agent, "请完成任务", params)

    row = _run_row(repo, rec["agent_run_id"])
    attempt = _attempts(repo, rec["agent_run_id"])[0]
    assert row["status"] == "failed"
    assert attempt["status"] == "failed"
    assert attempt["ended_at"] > 0
    assert len(_completed_events(repo, rec["agent_run_id"])) == 1
    assert not hasattr(agent, "_current_user_prompt")
    assert not hasattr(agent, "_current_run_params")


def test_main_settle_no_repo_noop():
    agent = SimpleNamespace(subagents=SimpleNamespace(runtime_db=None))
    result = SimpleNamespace(runtime_status="ok", tool_rounds=1)
    _settle_main_agent_run(agent, _params(), result)  # 不抛即通过


def test_main_settle_no_run_id_noop(repo):
    result = SimpleNamespace(runtime_status="ok", tool_rounds=1)
    _settle_main_agent_run(_agent(repo), _params(run_id="", attempt_id=""), result)  # 不抛即通过


def test_main_settle_repo_error_fails_silent(repo, monkeypatch):
    rec = _record(repo)
    result = SimpleNamespace(runtime_status="ok", tool_rounds=1)

    def boom(*args, **kwargs):
        raise RuntimeError("db locked")

    monkeypatch.setattr(repo, "settle_agent_run", boom)
    _settle_main_agent_run(_agent(repo), _params(run_id="run-1", attempt_id=rec["attempt_id"]), result)
    assert _run_row(repo, rec["agent_run_id"])["status"] == "created"


def test_main_settle_unknown_run_noop(repo):
    result = SimpleNamespace(runtime_status="ok", tool_rounds=1)
    _settle_main_agent_run(_agent(repo), _params(run_id="no-such-run", attempt_id="x"), result)


# ------------------------------------------------------ ①b 会话内纠正分支


def _ctx(agent=None, **overrides):
    fields = dict(
        user_prompt="u",
        final_prompt="f",
        final_response=SimpleNamespace(
            text="模型自报完成",
            backend="test",
            runtime_status="ok",
            runtime_reason="",
            runtime_source="",
        ),
        memories=[],
        executed_tools=[],
        archive_tool_calls=[],
        routed_context=SimpleNamespace(matches=[], required_read_paths=[], candidate_paths=[]),
        resume_context_result=None,
        runtime_injections=[],
        compression_snapshot_id="",
        compression_snapshot_path="",
        compression_applied=False,
        request_id="req-1",
        run_id="run-1",
        task_id="task-1",
        source="chat",
        do_save=True,
        task_attributes={},
        recovery_task_refs=None,
        recovery_content_paths=None,
        recovery_next_actions=None,
    )
    fields.update(overrides)
    return FinalizeContext(**fields)


def _patch_resume(monkeypatch):
    import agent_py_agent.agent.conversation.runtime as conv_runtime

    goal = SimpleNamespace(calls=[])

    def fake_goal(agent, **kwargs):
        goal.calls.append(kwargs)

    monkeypatch.setattr(conv_runtime, "ensure_goal_progress_continuation", fake_goal)
    return goal


def test_removed_required_action_gate_does_not_schedule_continuation(monkeypatch):
    goal = _patch_resume(monkeypatch)
    ctx = _ctx(
        task_attributes={"thread_goal_id": "goal-1", "conversation_thread_id": "thread-1"},
        final_response=SimpleNamespace(
            text="任务完成",
            backend="test",
            runtime_status="unfinished",
            runtime_reason="REQUIRED_ACTION_HAS_NO_EVIDENCE",
            runtime_source="required_action_completion_gate",
        ),
    )
    _schedule_typed_unfinished_continuation(SimpleNamespace(), ctx)
    assert goal.calls == []


def test_gate_blocked_does_not_schedule(monkeypatch):
    goal = _patch_resume(monkeypatch)
    ctx = _ctx(
        task_attributes={"thread_goal_id": "goal-1"},
        final_response=SimpleNamespace(
            text="需要用户输入",
            backend="test",
            runtime_status="blocked",
            runtime_reason="REQUIRED_ACTION_BLOCKED",
            runtime_source="required_action_completion_gate",
        ),
    )
    _schedule_typed_unfinished_continuation(SimpleNamespace(), ctx)
    assert goal.calls == []


def test_non_gate_unknown_reason_does_not_schedule(monkeypatch):
    goal = _patch_resume(monkeypatch)
    ctx = _ctx(
        task_attributes={"thread_goal_id": "goal-1"},
        final_response=SimpleNamespace(
            text="x",
            backend="test",
            runtime_status="unfinished",
            runtime_reason="SOMETHING_UNRELATED",
            runtime_source="",
        ),
    )
    _schedule_typed_unfinished_continuation(SimpleNamespace(), ctx)
    assert goal.calls == []


def test_removed_required_action_gate_does_not_resume_ordinary_task(monkeypatch):
    goal = _patch_resume(monkeypatch)
    ctx = _ctx(
        task_attributes={},
        final_response=SimpleNamespace(
            text="任务完成",
            backend="test",
            runtime_status="unfinished",
            runtime_reason="REQUIRED_ACTION_HAS_NO_EVIDENCE",
            runtime_source="required_action_completion_gate",
        ),
    )
    _schedule_typed_unfinished_continuation(SimpleNamespace(), ctx)
    assert goal.calls == []


@pytest.mark.parametrize(
    "sampled_phase",
    ["", "no_subagents", "subagents_active", "subagent_state_unknown"],
)
def test_background_unfinished_slice_does_not_poll_without_terminal_children(
    monkeypatch,
    sampled_phase,
):
    """普通后台回执或仍有 child 的工作片不能自行制造轮询 policy。"""
    from agent_py_agent.agent.conversation.authority import (
        CONVERSATION_BACKGROUND_SUBAGENT_PHASE_ATTR,
    )

    goal = _patch_resume(monkeypatch)
    ctx = _ctx(
        source="background_main_agent",
        do_save=False,
        task_attributes={
            "conversation_thread_id": "thread-1",
            "conversation_task_id": "task-root",
            CONVERSATION_BACKGROUND_SUBAGENT_PHASE_ATTR: sampled_phase,
        },
        final_response=SimpleNamespace(
            text="本工作片达到工具轮上限",
            backend="test",
            runtime_status="unfinished",
            runtime_reason="TOOL_ROUND_LIMIT_REACHED",
            runtime_source="tool_loop",
        ),
    )

    _schedule_typed_unfinished_continuation(SimpleNamespace(), ctx)

    assert goal.calls == []


def test_background_terminal_child_integration_does_not_create_ordinary_poll(monkeypatch):
    """全部 child 终态后的普通整合片段越过轮限也不创建第二条模型调度链。"""
    from agent_py_agent.agent.conversation.authority import (
        CONVERSATION_BACKGROUND_SUBAGENT_PHASE_ATTR,
    )

    goal = _patch_resume(monkeypatch)
    ctx = _ctx(
        source="background_main_agent",
        do_save=False,
        task_id="background-attempt-id",
        task_attributes={
            "conversation_thread_id": "thread-1",
            "conversation_task_id": "task-root",
            CONVERSATION_BACKGROUND_SUBAGENT_PHASE_ATTR: "subagents_terminal",
        },
        final_response=SimpleNamespace(
            text="本工作片达到工具轮上限",
            backend="test",
            runtime_status="unfinished",
            runtime_reason="TOOL_ROUND_LIMIT_REACHED",
            runtime_source="tool_loop",
        ),
    )

    _schedule_typed_unfinished_continuation(SimpleNamespace(), ctx)

    assert goal.calls == []


def test_explicit_goal_unfinished_turn_schedules_goal_driver(monkeypatch):
    """显式 Goal 的可继续收口仍使用独立 Goal policy，并保留 exact task/thread。"""
    goal = _patch_resume(monkeypatch)
    ctx = _ctx(
        source="gateway",
        task_attributes={
            "thread_goal_id": "goal-1",
            "conversation_thread_id": "thread-1",
            "conversation_task_id": "task-root",
        },
        final_response=SimpleNamespace(
            text="本轮达到工具边界",
            backend="test",
            runtime_status="unfinished",
            runtime_reason="TOOL_ROUND_LIMIT_REACHED",
            runtime_source="tool_loop",
        ),
    )

    _schedule_typed_unfinished_continuation(SimpleNamespace(), ctx)

    assert goal.calls == [
        {"task_id": "task-root", "thread_id": "thread-1", "due_now": True}
    ]


# ------------------------------------------------- 协议违规落账（2026-08-14 双CLI复刻实证）


def _violation_agent(repo):
    return SimpleNamespace(
        subagents=SimpleNamespace(runtime_db=repo),
        config=SimpleNamespace(enable_tools=True),
        backend=SimpleNamespace(name="fake"),
        root=None,
    )


def _violation_snapshot():
    from agent_py_agent.tests._tool_runtime_harness import (
        make_test_model_spec,
        make_test_protocol_snapshot,
        runtime_snapshot_for_model_specs,
    )

    return runtime_snapshot_for_model_specs(
        (
            make_test_model_spec(
                "list_files",
                input_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
            ),
        ),
        run_id="run-1",
    ), make_test_protocol_snapshot(run_id="run-1", source_protocol="text")


def _violation_request(repo, *, text, repairs=0, max_repairs=2, agent=None):
    from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
    from agent_py_agent.agent.agent_core.tool_loop.response_decision import (
        ToolLoopRepairCounters,
        ToolLoopResponseDecisionRequest,
        tool_loop_response_decision,
    )
    from agent_py_agent.agent.backends import ModelResponse

    agent = _violation_agent(repo) if agent is None else agent
    runtime_snapshot, protocol_snapshot = _violation_snapshot()
    params = ToolLoopExecuteParams(
        user_prompt="test",
        memories=[],
        runtime_injections=[],
        prompt_files=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_context=[],
        effective_on_chunk=None,
        allowed_tools=None,
        write_boundary=None,
        task_attributes={},
        request_id="req-1",
        run_id="run-1",
        task_id="task-1",
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
        delivery_contract={},
        tool_runtime_snapshot=runtime_snapshot,
        tool_protocol_snapshot=protocol_snapshot,
        max_protocol_repairs=max_repairs,
        attempt_id="attempt-1",
    )
    request = ToolLoopResponseDecisionRequest(
        agent=agent,
        params=params,
        response=ModelResponse(text=text, backend="fake"),
        counters=ToolLoopRepairCounters(protocol_repairs=repairs),
    )
    return request, tool_loop_response_decision(request)


def test_protocol_violation_persists_runtime_event(repo):
    """协议违规必须落 runtime_events(provider 原始响应头+结构化 violations)。

    2026-08-14 bs4 run-r2: XML <tool_calls> 被拒后磁盘上无结构化记录——
    审计断链。修复后: append-only 事件含 model_output_head(原始响应证据)、
    violations、stage、repair 计数。EXEC-31b 起决策路径内部已落账, 调用方
    不再单独补一笔(重复落账反而造成审计重复)。
    """
    _record(repo)
    _violation_request(
        repo,
        text='<tool_calls>\n<tool_call><name>list_files</name>'
        '<params><path>/tmp</path></params></tool_call>\n</tool_calls>',
    )
    rows = repo._runtime_connect().execute(
        "SELECT * FROM runtime_events WHERE event_type = 'protocol_violation'"
    ).fetchall()
    assert len(rows) == 1
    payload = rows[0]["payload_json"]
    assert "model_output_head" in payload
    assert "<tool_calls>" in payload  # provider 原始响应头保留证据
    assert '"stage": "tool_protocol_adapter"' in payload
    assert '"protocol_repairs": 0' in payload
    assert '"will_break": false' in payload
    assert '"tool_runtime_snapshot_hash":' in payload
    assert '"allowed_tools":' in payload
    assert '"available_tools":' in payload


def test_protocol_violation_break_records_repair_count(repo):
    """达到 max_repairs 的违规落账须标 will_break=true + 累计 repair 计数。"""
    _record(repo)
    request, decision = _violation_request(
        repo,
        text='[TOOL_CALL]\n{"tool": "read_file"}',
        repairs=2,
        max_repairs=2,
    )
    assert decision.action == "break"
    # decision 内部已落账（_protocol_violation_decision 调 _persist）
    rows = repo._runtime_connect().execute(
        "SELECT * FROM runtime_events WHERE event_type = 'protocol_violation'"
    ).fetchall()
    assert len(rows) == 1
    payload = rows[0]["payload_json"]
    assert '"protocol_repairs": 2' in payload
    assert '"will_break": true' in payload


def test_protocol_violation_no_repo_is_silent(repo):
    """无权威库(agent.subagents=None)时落账静默跳过,不反噬执行路径。"""
    _record(repo)
    request, _ = _violation_request(
        repo,
        text="bad",
        agent=SimpleNamespace(
            subagents=None,
            config=SimpleNamespace(enable_tools=True),
        ),
    )
    _persist_protocol_violation_event(request, [], "bad")  # 不应抛
    rows = repo._runtime_connect().execute(
        "SELECT * FROM runtime_events WHERE event_type = 'protocol_violation'"
    ).fetchall()
    assert len(rows) == 0
