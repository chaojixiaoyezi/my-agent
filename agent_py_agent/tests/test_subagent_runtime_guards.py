"""Runtime guard tests for stale runner attempts and dispatch handoff."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core._tool_loop_service import ToolLoopService
from agent_py_agent.agent.agent_core.subagent.attempt_guard import (
    stale_subagent_attempt_message,
    stale_subagent_attempt_result,
)
from agent_py_agent.agent.agent_core.tool_loop.completion import (
    ToolRoundCompletionRequest,
    completion_response_after_tool_round,
)
from agent_py_agent.agent.agent_core.tool_loop.natural_user_reply import (
    pending_natural_user_reply,
)
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.manager_runner_result_payload import (
    RecordRunnerResultParams,
)


class _ExplodingBackend:
    name = "exploding_backend"

    def generate(self, prompt: str, on_chunk=None):  # noqa: ARG002
        raise AssertionError("stale runner attempt must not call the model again")


def test_runner_recovery_rebinds_pending_user_guidance_to_new_attempt(tmp_path):
    agent = SimpleAgent(
        AgentConfig(model_backend="echo", subagent_workspace="subs"),
        tmp_path,
    )
    manager = agent.subagents
    task = manager.create_run(goal="长任务", thought="", plan=["继续"])
    first = manager.lifecycle.prepare_runner_attempt(task.id)
    first_attempt = first.runner_active_attempt_id
    entry = agent.conversation_store.append_guidance_once(
        {
            "target_type": "agent_run",
            "target_id": task.id,
            "message": "恢复后继续处理这条插话。",
            "metadata": {"expected_turn_id": first_attempt},
        },
        dedupe_key="runner-recovery/message-1",
    )
    run = manager.runtime_db.agent_run_for_run_id(task.id)
    assert run is not None
    settled = manager.runtime_db.settle_agent_attempt(
        agent_run_id=str(run["agent_run_id"]),
        attempt_id=first_attempt,
    )
    assert settled["settled"] is True
    manager.lifecycle.abandon_runner_attempt(task.id, first_attempt, reason="gateway_restart")

    recovered = manager.lifecycle.prepare_runner_attempt(task.id, retry_reason="gateway_restart")

    assert recovered.runner_active_attempt_id != first_attempt
    pending = agent.conversation_store.pending_guidance("agent_run", task.id)
    assert [item.guidance_id for item in pending] == [entry.guidance_id]
    assert pending[0].metadata["expected_turn_id"] == recovered.runner_active_attempt_id
    recovery = recovered.attributes["guidance_recovery"]
    assert recovery["rebound"] == 1
    assert recovery["dead_attempt_ids"] == [first_attempt]


def test_stale_attempt_guard_blocks_abandoned_runner_tools(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    task = manager.create_run(
        goal="写一个文件",
        thought="模拟超时旧线程",
        plan=["write"],
        allowed_tools=["write_file"],
    )
    prepared = manager.lifecycle.prepare_runner_attempt(task.id)
    manager.lifecycle.abandon_runner_attempt(task.id, prepared.runner_active_attempt_id, reason="timeout")
    agent = SimpleNamespace(
        subagents=manager,
        _current_subagent_run_id=task.id,
        _current_subagent_attempt_id=prepared.runner_active_attempt_id,
    )

    result = stale_subagent_attempt_result(agent, {"tool": "write_file", "path": "out.txt"})

    assert result is not None
    assert result.ok is False
    assert result.tool == "write_file"
    assert result.error_code == "RUNNER_ATTEMPT_STALE"
    assert result.reported_error_code == "RUNNER_ATTEMPT_STALE"
    assert result.retryable is False
    assert "已被废弃或超时" in result.output


def test_attempt_guard_blocks_when_runner_state_unreadable() -> None:
    def broken_load(_run_id):
        raise RuntimeError("attempt ledger unreadable")

    agent = SimpleNamespace(
        subagents=SimpleNamespace(load=broken_load),
        _current_subagent_run_id="run-1",
        _current_subagent_attempt_id="attempt-1",
    )

    result = stale_subagent_attempt_result(agent, {"tool": "write_file", "path": "out.txt"})
    message = stale_subagent_attempt_message(agent)

    assert result is not None
    assert result.ok is False
    assert result.error_code == "RUNNER_ATTEMPT_STALE"
    assert "attempt 状态读取失败" in result.output
    assert "attempt ledger unreadable" in result.output
    assert message is not None
    assert "attempt 状态读取失败" in message


def test_attempt_guard_blocks_after_canonical_attempt_is_cleared(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    task = manager.create_run(goal="写一个文件", thought="模拟已收口轮次", plan=["write"])
    prepared = manager.lifecycle.prepare_runner_attempt(task.id)
    attempt_id = prepared.runner_active_attempt_id
    prepared.runner_active_attempt_id = ""
    manager.save(prepared)
    agent = SimpleNamespace(
        subagents=manager,
        _current_subagent_run_id=task.id,
        _current_subagent_attempt_id=attempt_id,
    )

    result = stale_subagent_attempt_result(agent, {"tool": "write_file", "path": "out.txt"})

    assert result is not None
    assert result.error_code == "RUNNER_ATTEMPT_STALE"
    assert "已结束或不再活动" in result.output


def test_stale_attempt_guard_stops_tool_loop_before_next_model_call(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    task = manager.create_run(
        goal="写一个文件",
        thought="模拟超时旧线程",
        plan=["write"],
        allowed_tools=["write_file"],
    )
    prepared = manager.lifecycle.prepare_runner_attempt(task.id)
    manager.lifecycle.abandon_runner_attempt(task.id, prepared.runner_active_attempt_id, reason="timeout")
    agent = SimpleNamespace(
        config=SimpleNamespace(max_tool_rounds=0),
        backend=_ExplodingBackend(),
        subagents=manager,
        _current_subagent_run_id=task.id,
        _current_subagent_attempt_id=prepared.runner_active_attempt_id,
    )

    message = stale_subagent_attempt_message(agent)
    final_prompt, response, tool_rounds = ToolLoopService(agent).execute(_tool_loop_params("继续写文件"))

    assert message is not None
    assert final_prompt == ""
    assert tool_rounds == 0
    assert response is not None
    assert "旧 runner attempt 已停止" in response.text
    assert "父级接管" in response.text


def test_duplicate_runner_result_is_rejected_after_first_projection(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    task = manager.create_run(goal="长任务", thought="模拟重复收口", plan=["执行"])
    prepared = manager.lifecycle.prepare_runner_attempt(task.id)
    params = RecordRunnerResultParams(
        run_id=task.id,
        attempt_id=prepared.runner_active_attempt_id,
        dry_run=False,
        ok=True,
        message="done",
        status="DONE",
    )

    first = manager.runner_result.record_runner_result(params)
    second = manager.runner_result.record_runner_result(params)

    assert first.ok is True
    assert second.ok is False
    assert "inactive attempt" in second.message
    assert manager.load(task.id).status == "DONE"


def test_late_done_result_cannot_overwrite_runtime_cancelled_attempt(tmp_path):
    manager = SubAgentManager(
        tmp_path / "subs",
        owner_home_dir=str(tmp_path / "owner"),
    )
    task = manager.create_run(goal="长任务", thought="模拟取消后迟到回复", plan=["执行"])
    prepared = manager.lifecycle.prepare_runner_attempt(task.id)
    attempt_id = prepared.runner_active_attempt_id
    run = manager.runtime_db.agent_run_for_run_id(task.id)
    assert run is not None
    manager.runtime_db.settle_agent_run(
        agent_run_id=str(run["agent_run_id"]),
        status="cancelled",
        attempt_id=attempt_id,
    )
    prepared.status = "CANCELLED"
    manager.save(prepared)

    result = manager.runner_result.record_runner_result(
        RecordRunnerResultParams(
            run_id=task.id,
            attempt_id=attempt_id,
            dry_run=False,
            ok=True,
            message="late done",
            status="DONE",
        )
    )

    assert result.ok is False
    assert "conflicting with runtime terminal fact" in result.message
    assert manager.load(task.id).status == "CANCELLED"


def test_stale_runner_snapshot_cannot_reopen_cancelled_canonical_task(tmp_path):
    manager = SubAgentManager(
        tmp_path / "subs",
        owner_home_dir=str(tmp_path / "owner"),
    )
    task = manager.create_run(goal="长任务", thought="模拟取消竞态", plan=["执行"])
    prepared = manager.lifecycle.prepare_runner_attempt(task.id)
    stale = manager.load(task.id)
    cancelled = manager.load(task.id)
    cancelled.status = "CANCELLED"
    cancelled.failure_type = "cancelled"
    cancelled.runner_active_attempt_id = ""
    cancelled.ended_at = 123.0
    cancelled.attributes = {
        **dict(cancelled.attributes or {}),
        "cancel_subagents": {
            "reason": "conversation_user_stop",
            "previous_status": "RUNNING",
        },
    }
    manager.save(cancelled)

    stale.status = "RUNNING"
    stale.runner_active_attempt_id = prepared.runner_active_attempt_id
    stale.heartbeat_at = 456.0
    manager.save(stale)

    loaded = manager.load(task.id)
    assert loaded.status == "CANCELLED"
    assert loaded.failure_type == "cancelled"
    assert loaded.runner_active_attempt_id == ""
    assert loaded.ended_at == 123.0
    assert loaded.attributes["cancel_subagents"]["reason"] == "conversation_user_stop"
    assert stale.status == "CANCELLED"


def test_structured_user_stop_can_explicitly_reactivate_same_run(tmp_path):
    manager = SubAgentManager(
        tmp_path / "subs",
        owner_home_dir=str(tmp_path / "owner"),
    )
    task = manager.create_run(goal="长任务", thought="用户停止后续跑", plan=["执行"])
    task.status = "CANCELLED"
    task.failure_type = "cancelled"
    task.attributes = {
        **dict(task.attributes or {}),
        "cancel_subagents": {
            "reason": "conversation_user_stop",
            "previous_status": "RUNNING",
        },
    }
    manager.save(task)

    resumed = manager.lifecycle.prepare_runner_attempt(task.id)

    assert resumed.status == "RUNNING"
    assert resumed.failure_type == ""
    assert resumed.runner_active_attempt_id
    assert manager.load(task.id).status == "RUNNING"


def test_matching_done_result_projects_after_runtime_settlement(tmp_path):
    manager = SubAgentManager(
        tmp_path / "subs",
        owner_home_dir=str(tmp_path / "owner"),
    )
    task = manager.create_run(goal="长任务", thought="模拟正常完成", plan=["执行"])
    prepared = manager.lifecycle.prepare_runner_attempt(task.id)
    attempt_id = prepared.runner_active_attempt_id
    run = manager.runtime_db.agent_run_for_run_id(task.id)
    assert run is not None
    manager.runtime_db.settle_agent_run(
        agent_run_id=str(run["agent_run_id"]),
        status="done",
        attempt_id=attempt_id,
    )

    result = manager.runner_result.record_runner_result(
        RecordRunnerResultParams(
            run_id=task.id,
            attempt_id=attempt_id,
            dry_run=False,
            ok=True,
            message="done",
            status="DONE",
        )
    )

    assert result.ok is True
    assert manager.load(task.id).status == "DONE"


def test_clean_runtime_completion_can_project_blocked_task_outcome(tmp_path):
    """执行轮正常结束不等于任务完成；exact 结果仍可投影为等待授权。"""
    manager = SubAgentManager(
        tmp_path / "subs",
        owner_home_dir=str(tmp_path / "owner"),
    )
    task = manager.create_run(goal="等待授权", thought="申请能力", plan=["继续"])
    prepared = manager.lifecycle.prepare_runner_attempt(task.id)
    attempt_id = prepared.runner_active_attempt_id
    run = manager.runtime_db.agent_run_for_run_id(task.id)
    assert run is not None
    manager.runtime_db.settle_agent_run(
        agent_run_id=str(run["agent_run_id"]),
        status="done",
        attempt_id=attempt_id,
    )

    result = manager.runner_result.record_runner_result(
        RecordRunnerResultParams(
            run_id=task.id,
            attempt_id=attempt_id,
            dry_run=False,
            ok=False,
            message="等待授权",
            status="BLOCKED",
        )
    )

    assert result.ok is False
    assert manager.load(task.id).status == "BLOCKED"


def test_settled_max_token_slice_projects_pending_and_releases_launch(tmp_path):
    manager = SubAgentManager(
        tmp_path / "subs",
        owner_home_dir=str(tmp_path / "owner"),
    )
    task = manager.create_run(goal="长任务", thought="模拟上下文截断", plan=["继续"])
    prepared = manager.lifecycle.prepare_runner_attempt(task.id)
    attempt_id = prepared.runner_active_attempt_id
    prepared.attributes = {
        **dict(prepared.attributes or {}),
        "background_start": {
            "launch_id": "shared-batch",
            "status": "running",
            "pid": 9876,
        },
    }
    manager.save(prepared)
    run = manager.runtime_db.agent_run_for_run_id(task.id)
    assert run is not None
    settled = manager.runtime_db.settle_agent_attempt(
        agent_run_id=str(run["agent_run_id"]),
        attempt_id=attempt_id,
        payload={
            "status": "attempt_done",
            "runtime_status": "unfinished",
            "runtime_reason": "MODEL_RESPONSE_TRUNCATED",
        },
    )
    assert settled["settled"] is True

    result = manager.runner_result.record_runner_result(
        RecordRunnerResultParams(
            run_id=task.id,
            attempt_id=attempt_id,
            dry_run=False,
            ok=False,
            message="runner 本轮结束: max-tokens",
            status="PENDING",
            turn_end_reason="max-tokens",
            failure_type="model_error",
        )
    )

    persisted = manager.load(task.id)
    authority = manager.runtime_db.runner_result_commit_authority(
        run_id=task.id,
        attempt_id=attempt_id,
    )
    assert result.status == "PENDING"
    assert persisted.status == "PENDING"
    assert persisted.current_step == "等待继续"
    assert persisted.runner_active_attempt_id == ""
    assert persisted.attributes["background_start"]["status"] == "reclaimed"
    assert authority is not None
    assert authority["run_status"] == "created"
    assert authority["attempt_status"] == "done"

    continued = manager.lifecycle.prepare_runner_attempt(task.id)
    next_attempt = manager.runtime_db.current_attempt(str(run["agent_run_id"]))
    assert continued.runner_active_attempt_id != attempt_id
    assert next_attempt is not None
    assert next_attempt["attempt_generation"] == 2
    assert next_attempt["status"] == "running"


def test_settled_nonterminal_slice_rejects_mismatched_done_projection(tmp_path):
    manager = SubAgentManager(
        tmp_path / "subs",
        owner_home_dir=str(tmp_path / "owner"),
    )
    task = manager.create_run(goal="长任务", thought="模拟伪完成", plan=["继续"])
    prepared = manager.lifecycle.prepare_runner_attempt(task.id)
    attempt_id = prepared.runner_active_attempt_id
    run = manager.runtime_db.agent_run_for_run_id(task.id)
    assert run is not None
    settled = manager.runtime_db.settle_agent_attempt(
        agent_run_id=str(run["agent_run_id"]),
        attempt_id=attempt_id,
        payload={"status": "attempt_done", "runtime_status": "unfinished"},
    )
    assert settled["settled"] is True

    result = manager.runner_result.record_runner_result(
        RecordRunnerResultParams(
            run_id=task.id,
            attempt_id=attempt_id,
            dry_run=False,
            ok=True,
            message="late done",
            status="DONE",
            turn_end_reason="max-tokens",
        )
    )

    assert result.ok is False
    assert "conflicting with runtime terminal fact" in result.message
    assert manager.load(task.id).status == "RUNNING"


def test_settled_blocked_slice_projects_matching_host_wait_state(tmp_path):
    manager = SubAgentManager(
        tmp_path / "subs",
        owner_home_dir=str(tmp_path / "owner"),
    )
    task = manager.create_run(goal="等待授权", thought="模拟宿主阻塞", plan=["等待"])
    prepared = manager.lifecycle.prepare_runner_attempt(task.id)
    attempt_id = prepared.runner_active_attempt_id
    run = manager.runtime_db.agent_run_for_run_id(task.id)
    assert run is not None
    settled = manager.runtime_db.settle_agent_attempt(
        agent_run_id=str(run["agent_run_id"]),
        attempt_id=attempt_id,
        payload={"status": "attempt_done", "runtime_status": "blocked"},
    )
    assert settled["settled"] is True

    result = manager.runner_result.record_runner_result(
        RecordRunnerResultParams(
            run_id=task.id,
            attempt_id=attempt_id,
            dry_run=False,
            ok=False,
            message="runner 本轮结束: blocked",
            status="BLOCKED",
            turn_end_reason="blocked",
            failure_type="status_blocked",
        )
    )

    assert result.status == "BLOCKED"
    assert manager.load(task.id).current_step == "等待处理"


def test_dispatch_round_returns_to_parent_when_child_report_exists(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    task = manager.create_run(goal="deliver web artifact", thought="await parent", plan=["report"])
    task.status = "DONE"
    task.verification_status = "VERIFIED"
    manager.save(task)
    _write_json(task.reports_dir, "runner_result.json", {"decision": "REJECT", "ok": False})
    agent = SimpleNamespace(subagents=manager, _current_subagent_run_id="")
    params = _tool_loop_params("请安排小傻妞完成并汇报。")

    first = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL dispatch_subagents]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )
    second = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="[TOOL_CALL dispatch_subagents]", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )

    assert first is None
    assert second is None
    assert params.tool_context == []


def test_root_create_keeps_same_turn_open_for_immediate_child_guidance() -> None:
    params = _tool_loop_params("创建孩子后立刻补充要求。")
    params.executed_tools.append("create_subagents")
    agent = SimpleNamespace(
        subagent_run_ids_for_request=lambda _task_id: ["child-1"],
        subagents=SimpleNamespace(
            list_runs=lambda: [SimpleNamespace(id="child-1", status="RUNNING")]
        ),
    )

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=agent,
            params=params,
            response=ModelResponse(text="已创建孩子", backend="test"),
            before_executed_count=1,
            subagent_output_written=False,
        )
    )

    assert response is None
    assert pending_natural_user_reply(params) is None


def test_task_local_context_refresh_ends_slice_without_another_model_round() -> None:
    params = _tool_loop_params("绑定来源", context_scope="task_local")
    params.live_archive_state["pending_runtime_transition"] = {
        "kind": "context_refresh",
        "reason": "durable_tool_scope_changed",
        "resume": "next_durable_slice",
        "tool": "watch_stream",
    }

    response = completion_response_after_tool_round(
        ToolRoundCompletionRequest(
            agent=SimpleNamespace(),
            params=params,
            response=ModelResponse(text="旧上下文中的草稿", backend="test"),
            before_executed_count=0,
            subagent_output_written=False,
        )
    )

    assert response is not None
    assert response.text == "工具已提交耐久状态更新；当前工作片已结束，下一工作片从最新状态继续。"
    assert "SUBAGENT_RESULT" not in response.text
    assert "旧上下文中的草稿" not in response.text
    assert "pending_runtime_transition" not in params.live_archive_state


def _write_json(root: str, filename: str, payload: dict[str, object]) -> None:
    path = Path(root) / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _tool_loop_params(
    prompt: str,
    *,
    context_scope: str = "default",
) -> ToolLoopExecuteParams:
    return ToolLoopExecuteParams(
        user_prompt=prompt,
        memories=[],
        runtime_injections=[],
        prompt_files=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_context=[],
        effective_on_chunk=None,
        allowed_tools=None,
        write_boundary=None,
        task_attributes=None,
        request_id="req",
        run_id="run",
        task_id="task",
        one_shot_tool_calls=set(),
        executed_tools=["dispatch_subagents"],
        archive_tool_calls=[],
        context_scope=context_scope,
    )
