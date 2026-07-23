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
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.subagents.manager import SubAgentManager


class _ExplodingBackend:
    name = "exploding_backend"

    def generate(self, prompt: str, on_chunk=None):  # noqa: ARG002
        raise AssertionError("stale runner attempt must not call the model again")


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
    assert "attempt 状态读取失败" in result.output
    assert "attempt ledger unreadable" in result.output
    assert message is not None
    assert "attempt 状态读取失败" in message


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


def _write_json(root: str, filename: str, payload: dict[str, object]) -> None:
    path = Path(root) / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _tool_loop_params(prompt: str) -> ToolLoopExecuteParams:
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
    )
