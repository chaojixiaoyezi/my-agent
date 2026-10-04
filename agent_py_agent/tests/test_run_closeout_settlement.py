"""rco：回合收口统一推进 agent_run/task_run（3a 裁定，2026-10-04）。

修法（统一收口，不为单条路径打补丁）：
- 主代理 `_settle_main_agent_run_status`：非终态统一分族——不可续跑族（blocked/
  协议违规/执行错误/超时等，共享 gate `should_continue_task` 判定 False）收口
  failed；可续跑族（技术续跑 + 等用户族 needs_user_input/approval_required）
  保留非终态等续跑。runtime_db 没有 waiting 类状态，等用户族如实保持 created。
- 子代理 `closeout_target_run_status`：失败族（`SUBAGENT_FAILURE_STATUSES` 减去可恢复
  等待 BLOCKED）随 FAILED 一并收口 failed；BLOCKED 是可恢复等待，保持非终态（rcob 修正）。

用例层次：Gateway 回合走真实 `_run_with_params` 收口链路（source=gateway），
"假模型"结果由 `_run_once_with_params` 替身按脚本产出（沿用
test_r103_run_reuse_no_split 的成熟替身模式）；执行错误走真实
`_run_once_with_params` 异常分支。统一收口点：`_nonterminal_run_closeout_status`
（runtime_mixin）与 `_RUN_STATUS_FOR_TASK_STATUS`（runtime_closeout）——变异去掉
任一收口点，本文件对应用例必须变红。
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core import runtime_mixin
from agent_py_agent.agent.agent_core.runtime.loop_models import RunParams
from agent_py_agent.agent.runtime_db.operations import AGENT_RUN_TERMINAL_STATUSES
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository


@pytest.fixture
def repo(tmp_path):
    return RuntimeRepository(tmp_path / "home" / "runtime.db")


def _agent(repo):
    """Gateway 回合最小 agent 替身：权威运行库 + 已终态会话任务关联（task_run 可关）。"""
    store = SimpleNamespace(tasks=SimpleNamespace(load=lambda task_id: SimpleNamespace(
        task_id=task_id, status="completed",
    )))
    return SimpleNamespace(
        config=SimpleNamespace(enable_tools=False, auto_save_memory=False),
        subagents=SimpleNamespace(runtime_db=repo),
        home_paths=SimpleNamespace(owner_id="local/main"),
        conversation_store=store,
    )


def _patch_turn_outer(monkeypatch, fake_run_once):
    """只保留真实收口：外围副作用（会话绑定、工作区、交付、Compact 决策）换替身。"""
    monkeypatch.setattr(runtime_mixin, "bind_cli_run_conversation", lambda _agent, params, _prompt: params)
    monkeypatch.setattr(runtime_mixin, "attach_run_task_workspace_context", lambda _agent, params, _prompt: params)
    monkeypatch.setattr(runtime_mixin, "_run_once_with_params", fake_run_once)
    monkeypatch.setattr(
        runtime_mixin, "compact_auto_continuation_decision",
        lambda _result, depth: SimpleNamespace(should_continue=False),
    )
    monkeypatch.setattr(runtime_mixin, "finish_run_task_workspace_if_needed", lambda *_: None)
    monkeypatch.setattr(runtime_mixin, "persist_cli_run_assistant", lambda *_: None)


def _run_gateway_turn(repo, task_id, fake_run_once, monkeypatch):
    _patch_turn_outer(monkeypatch, fake_run_once)
    return runtime_mixin._run_with_params(
        _agent(repo),
        "完成任务",
        RunParams(
            request_id=task_id,
            run_id=task_id,
            task_id=task_id,
            attempt_id="gateway-transport-attempt",
            root_user_prompt="完成任务",
            source="gateway",
        ),
    )


def _main_run(repo, task_id):
    return repo.main_agent_run_for_task(task_id)


def _events(repo, agent_run_id, event_type):
    return repo._runtime_connect().execute(
        "SELECT * FROM runtime_events WHERE agent_run_id = ? AND event_type = ? ORDER BY seq",
        (agent_run_id, event_type),
    ).fetchall()


def _task_run(repo, task_id):
    rows = repo.task_runs_for_task(task_id)
    assert len(rows) == 1
    return rows[0]


def _fake_result(status, reason=""):
    """假模型结果替身：只需状态与原因；其余字段给固定值。"""
    return SimpleNamespace(
        runtime_status=status, runtime_reason=reason,
        runtime_source="tool_loop" if reason else "gateway",
        tool_rounds=1, response="完成。",
    )


def test_gateway_normal_completion_settles_run_and_task_run_done(repo, monkeypatch):
    """正常完成：run→done、attempt→done、task_run 关闭；completed/closed 事件齐全。"""
    def fake_run_once(_agent, _prompt, params):
        return _fake_result("ok")

    result = _run_gateway_turn(repo, "gwreq-rco-done", fake_run_once, monkeypatch)
    assert result.runtime_status == "ok"

    run = _main_run(repo, "gwreq-rco-done")
    assert str(run["status"]) == "done"
    attempt = repo.current_attempt(str(run["agent_run_id"]))
    assert attempt["status"] == "done" and attempt["ended_at"] > 0
    completed = _events(repo, str(run["agent_run_id"]), "agent_run.completed")
    assert len(completed) == 1
    task_run = _task_run(repo, "gwreq-rco-done")
    assert task_run["status"] == "done" and task_run["closed_at"] > 0
    closed = repo._runtime_connect().execute(
        "SELECT * FROM runtime_events WHERE task_run_id = ? AND event_type = 'task_run.closed'",
        (task_run["task_run_id"],),
    ).fetchall()
    assert len(closed) == 1


def test_gateway_protocol_violation_blocked_settles_failed(repo, monkeypatch):
    """协议违规导致 blocked：收口 run=failed、task_run=failed——本 bug 的修复核心。"""
    def fake_run_once(_agent, _prompt, params):
        return _fake_result("blocked", "TOOL_PROTOCOL_VIOLATION")

    _run_gateway_turn(repo, "gwreq-rco-blocked", fake_run_once, monkeypatch)

    run = _main_run(repo, "gwreq-rco-blocked")
    assert str(run["status"]) == "failed"
    attempt = repo.current_attempt(str(run["agent_run_id"]))
    assert attempt["status"] == "failed" and attempt["ended_at"] > 0
    completed = _events(repo, str(run["agent_run_id"]), "agent_run.completed")
    assert len(completed) == 1
    assert '"runtime_status": "blocked"' in completed[0]["payload_json"]
    task_run = _task_run(repo, "gwreq-rco-blocked")
    assert task_run["status"] == "failed" and task_run["closed_at"] > 0


def test_gateway_user_stop_settles_cancelled(repo, monkeypatch):
    """取消（user_stop）：收口 run=cancelled、task_run=cancelled。"""
    def fake_run_once(_agent, _prompt, params):
        return _fake_result("user_stop")

    _run_gateway_turn(repo, "gwreq-rco-stop", fake_run_once, monkeypatch)

    run = _main_run(repo, "gwreq-rco-stop")
    assert str(run["status"]) == "cancelled"
    task_run = _task_run(repo, "gwreq-rco-stop")
    assert task_run["status"] == "cancelled" and task_run["closed_at"] > 0


def test_gateway_execution_error_settles_failed(repo, monkeypatch):
    """执行错误：模型循环前抛错，真实 _run_once_with_params 异常收口 → failed。"""
    monkeypatch.setattr(runtime_mixin, "bind_cli_run_conversation", lambda _agent, params, _prompt: params)
    monkeypatch.setattr(runtime_mixin, "attach_run_task_workspace_context", lambda _agent, params, _prompt: params)

    def fail_before_model(*_args, **_kwargs):
        raise ValueError("model loop blew up")

    monkeypatch.setattr(runtime_mixin, "_prepare_runtime_context", fail_before_model)

    with pytest.raises(ValueError):
        runtime_mixin._run_with_params(
            _agent(repo),
            "完成任务",
            RunParams(
                request_id="gwreq-rco-error",
                run_id="gwreq-rco-error",
                task_id="gwreq-rco-error",
                attempt_id="gateway-transport-attempt",
                root_user_prompt="完成任务",
                source="gateway",
            ),
        )

    run = _main_run(repo, "gwreq-rco-error")
    assert str(run["status"]) == "failed"
    attempt = repo.current_attempt(str(run["agent_run_id"]))
    assert attempt["status"] == "failed" and attempt["ended_at"] > 0
    assert len(_events(repo, str(run["agent_run_id"]), "agent_run.completed")) == 1


@pytest.mark.parametrize("status", ["needs_user_input", "approval_required"])
def test_gateway_waiting_user_keeps_run_nonterminal(repo, monkeypatch, status):
    """等用户族：保留 run 非终态、只关 attempt（状态如实——runtime_db 无 waiting
    状态、不新造）；task_run 保持打开等用户动作。"""
    def fake_run_once(_agent, _prompt, params):
        return _fake_result(status)

    task_id = f"gwreq-rco-wait-{status}"
    _run_gateway_turn(repo, task_id, fake_run_once, monkeypatch)

    run = _main_run(repo, task_id)
    assert str(run["status"]) == "created"  # 保留非终态等用户，不收口
    attempt = repo.current_attempt(str(run["agent_run_id"]))
    assert attempt["status"] == "done" and attempt["ended_at"] > 0
    assert _events(repo, str(run["agent_run_id"]), "agent_run.completed") == []
    assert _task_run(repo, task_id)["closed_at"] == 0


def test_gateway_continuable_unfinished_keeps_run_nonterminal(repo, monkeypatch):
    """可续跑族（TOOL_ROUND_LIMIT_REACHED）：保留非终态等 resume/Goal 续跑。"""
    def fake_run_once(_agent, _prompt, params):
        return _fake_result("unfinished", "TOOL_ROUND_LIMIT_REACHED")

    _run_gateway_turn(repo, "gwreq-rco-cont", fake_run_once, monkeypatch)

    run = _main_run(repo, "gwreq-rco-cont")
    assert str(run["status"]) == "created"
    attempt = repo.current_attempt(str(run["agent_run_id"]))
    assert attempt["status"] == "done" and attempt["ended_at"] > 0
    assert _events(repo, str(run["agent_run_id"]), "agent_run.completed") == []


def test_subagent_closeout_maps_failure_family_to_failed():
    """子代理 runner 结论：失败族 = `SUBAGENT_FAILURE_STATUSES` 减去可恢复等待（BLOCKED）
    → failed（rcob：BLOCKED 是可恢复等待，保持非终态）；取消 → cancelled；完成 → done；
    其余可恢复形态（BLOCKED/PENDING/RUNNING 等）不收口。"""
    from agent_py_agent.agent.subagents.models import SUBAGENT_FAILURE_STATUSES
    from agent_py_agent.agent.subagents.services.runtime_closeout import closeout_target_run_status

    params = SimpleNamespace(status="", dry_run=False)
    task = SimpleNamespace(status="")

    def mapped(status):
        return closeout_target_run_status(params, SimpleNamespace(status=status), task)

    failure_family = SUBAGENT_FAILURE_STATUSES - {"BLOCKED"}
    assert failure_family == {"FAILED", "CHANNEL_ERROR", "TIMEOUT"}
    for status in sorted(failure_family):
        assert mapped(status) == "failed", status
    assert mapped("BLOCKED") == ""  # 可恢复等待：等批复/续派，不是最终失败（rcob）
    assert mapped("CANCELLED") == "cancelled"
    assert mapped("DONE") == "done"
    assert mapped("PENDING") == ""
    assert mapped("RUNNING") == ""
    assert mapped("PLANNING") == ""
    assert mapped("PAUSED") == ""


def test_subagent_blocked_result_keeps_run_nonterminal(repo):
    """rcob：子代理 runner 以 BLOCKED 结束（可恢复等待）——settle_runtime_run_for_result
    不收口、不写 agent_run.completed，run 保持非终态（与 test_dispatch_liveness_and_revive
    的可恢复等待口径一致）。"""
    from agent_py_agent.agent.subagents.services.runtime_closeout import (
        settle_runtime_run_for_result,
    )

    rec = repo.record_run_creation(owner_id="local/main", goal="child", run_id="child-rco", role="worker")
    task = SimpleNamespace(id="child-rco", status="BLOCKED", attributes={})
    params = SimpleNamespace(
        attempt_id=rec["attempt_id"], dry_run=False, status="BLOCKED", turn_end_reason="blocked",
    )
    result = SimpleNamespace(status="BLOCKED", message="")

    outcome = settle_runtime_run_for_result(repo, task, params, result)

    assert outcome["state"] == "not_applicable"
    assert outcome["target_run_status"] == ""
    run = repo.agent_run_for_run_id("child-rco")
    assert str(run["status"]) not in AGENT_RUN_TERMINAL_STATUSES
    assert _events(repo, str(run["agent_run_id"]), "agent_run.completed") == []


def test_nonterminal_judgement_failure_logs_and_settles_failed(repo, monkeypatch, caplog):
    """rco-f4：分族判据（should_continue_task）故障时不静默悬挂——记 error 日志并按
    不可续跑收口 failed（收口可逆：create_attempt 对终态放行）。"""
    import agent_py_agent.agent.turn_end as turn_end_module

    def boom(*_args, **_kwargs):
        raise RuntimeError("gate unavailable")

    monkeypatch.setattr(turn_end_module, "should_continue_task", boom)

    rec = repo.record_run_creation(owner_id="local/main", goal="rco-f4", run_id="run-f4", role="main")
    result = SimpleNamespace(
        runtime_status="blocked",
        runtime_reason="MISSING_EVIDENCE",
        runtime_source="acceptance_gate",
        tool_rounds=1,
    )
    with caplog.at_level(logging.ERROR, logger="agent_py_agent.agent.agent_core.runtime_mixin"):
        runtime_mixin._settle_main_agent_run(
            SimpleNamespace(subagents=SimpleNamespace(runtime_db=repo)),
            SimpleNamespace(run_id="run-f4", attempt_id=rec["attempt_id"]),
            result,
        )

    run = repo.agent_run_for_run_id("run-f4")
    assert str(run["status"]) == "failed"
    assert any("分族判据不可用" in record.getMessage() for record in caplog.records)
