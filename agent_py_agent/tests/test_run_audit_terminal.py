"""修复①+② 单测：run 级审计终态收口 + 假完成如实收口。

覆盖：
- Repository.settle_agent_run：单事务 CAS、幂等、先到先得、多 attempt 收口、
  事件绑定 attempt（A.8）、查无 run noop。
- runtime_mixin._settle_main_agent_run / _settle_main_agent_run_exception：
  ok→done、非终态（unfinished 等）不落账（R1-03）、cancelled 族别名、
  InterruptedError→cancelled、LOCAL_UNMANAGED noop、repo 异常 fail-silent。
- _finalization_service._honest_unfinished_response_text：ok 原样、
  非 ok 如实标注（附状态/原因/来源 + 模型原文）。
- _schedule_typed_unfinished_continuation：返工门 unfinished → 预算内续跑；
  blocked / 非门来源不续跑。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core._finalization_service import (
    _honest_unfinished_response_text,
    _schedule_typed_unfinished_continuation,
)
from agent_py_agent.agent.agent_core._runtime_params import FinalizeContext
from agent_py_agent.agent.agent_core.runtime_mixin import (
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


def _completed_events(repo, agent_run_id):
    return repo._runtime_connect().execute(
        "SELECT * FROM runtime_events WHERE agent_run_id = ? AND event_type = 'agent_run.completed' "
        "ORDER BY seq",
        (agent_run_id,),
    ).fetchall()


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


def test_settle_closes_all_open_attempts_only(repo):
    rec = _record(repo)
    second = repo.create_attempt(rec["agent_run_id"])
    repo.settle_agent_run(
        agent_run_id=rec["agent_run_id"],
        status="done",
        payload={"status": "done"},
    )
    attempts = _attempts(repo, rec["agent_run_id"])
    assert len(attempts) == 2
    for attempt in attempts:
        assert attempt["status"] == "done"
        assert attempt["ended_at"] > 0
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


def _agent(repo):
    return SimpleNamespace(subagents=SimpleNamespace(runtime_db=repo))


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


def test_main_settle_unfinished_not_terminal(repo):
    """R1-03：unfinished 是任务级可恢复语义（返工门会续跑），不落账。

    run 保持 created，发现层继续驱动；不写 completed 事件、不写
    status_conflict 噪音（runtime_mixin 层直接过滤，不进 settle）。
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
    assert _attempts(repo, rec["agent_run_id"])[0]["status"] == "running"


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


# ------------------------------------------------------ 如实收口投影函数


def test_honest_text_ok_passthrough():
    assert _honest_unfinished_response_text("完成。", runtime_status="ok") == "完成。"


def test_honest_text_unfinished_wraps_model_text():
    text = _honest_unfinished_response_text(
        "任务完成，文件已创建。",
        runtime_status="unfinished",
        runtime_reason="REQUIRED_ACTION_HAS_NO_EVIDENCE",
        runtime_source="required_action_completion_gate",
    )
    assert "【任务未完成】" in text
    assert "状态：unfinished" in text
    assert "原因：REQUIRED_ACTION_HAS_NO_EVIDENCE" in text
    assert "来源：required_action_completion_gate" in text
    assert "任务完成，文件已创建。" in text
    assert "不代表任务已完成" in text


def test_honest_text_empty_model_text_only_marker():
    text = _honest_unfinished_response_text(
        "",
        runtime_status="blocked",
        runtime_reason="APPROVAL_REQUIRED",
        runtime_source="required_action_completion_gate",
    )
    assert text == "【任务未完成】本次执行未达成完整交付（状态：blocked；原因：APPROVAL_REQUIRED；来源：required_action_completion_gate）。"


def test_honest_text_blocked_also_wrapped():
    text = _honest_unfinished_response_text(
        "等待审批。",
        runtime_status="blocked",
        runtime_reason="",
        runtime_source="required_action_completion_gate",
    )
    assert "【任务未完成】" in text
    assert "等待审批。" in text


def test_honest_text_task_progress_not_wrapped():
    # TASK_PROGRESS_OPEN 是渐进式交付/续跑的常态：模型文本本身已如实，
    # 叠加机器标注反而污染交付语义（回归：test_explicit_goal_cannot_close_with_open_progress）。
    text = _honest_unfinished_response_text(
        "文件已经读取，但验证项仍未完成。",
        runtime_status="unfinished",
        runtime_reason="TASK_PROGRESS_OPEN",
        runtime_source="task_progress",
    )
    assert text == "文件已经读取，但验证项仍未完成。"


def test_honest_text_tool_round_limit_not_wrapped():
    # 轮限续跑同样不包裹：硬停时模型本轮无文本，保持空响应交给下一轮续跑接管。
    assert _honest_unfinished_response_text(
        "",
        runtime_status="unfinished",
        runtime_reason="TOOL_ROUND_LIMIT_REACHED",
        runtime_source="tool_loop",
    ) == ""


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

    ordinary = SimpleNamespace(calls=[])

    def fake_ordinary(agent, **kwargs):
        ordinary.calls.append(kwargs)

    monkeypatch.setattr(conv_runtime, "ensure_goal_progress_continuation", fake_goal)
    monkeypatch.setattr(conv_runtime, "ensure_ordinary_task_resume", fake_ordinary)
    return goal, ordinary


def test_gate_unfinished_schedules_continuation(monkeypatch):
    goal, ordinary = _patch_resume(monkeypatch)
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
    assert len(goal.calls) == 1
    assert goal.calls[0]["task_id"] == "task-1"
    assert goal.calls[0]["thread_id"] == "thread-1"
    assert goal.calls[0]["due_now"] is True
    assert ordinary.calls == []


def test_gate_blocked_does_not_schedule(monkeypatch):
    goal, ordinary = _patch_resume(monkeypatch)
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
    assert ordinary.calls == []


def test_non_gate_unknown_reason_does_not_schedule(monkeypatch):
    goal, ordinary = _patch_resume(monkeypatch)
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
    assert ordinary.calls == []


def test_gate_unfinished_ordinary_task_resumes_with_budget(monkeypatch):
    goal, ordinary = _patch_resume(monkeypatch)
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
    assert len(ordinary.calls) == 1
    assert ordinary.calls[0]["due_now"] is True


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
    violations、stage、repair 计数。
    """
    _record(repo)
    request, _ = _violation_request(
        repo,
        text='<tool_calls>\n<tool_call><name>list_files</name>'
        '<params><path>/tmp</path></params></tool_call>\n</tool_calls>',
    )
    _persist_protocol_violation_event(
        request, [{"code": "PROTOCOL_VIOLATION", "detail": "x"}], request.response.text
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
