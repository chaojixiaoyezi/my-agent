from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.tool_loop.recovery import tool_payload_with_run_scope


def _params(**overrides) -> ToolLoopExecuteParams:
    data = dict(
        user_prompt="继续任务",
        memories=[],
        runtime_injections=[],
        prompt_files=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_context=[],
        effective_on_chunk=None,
        allowed_tools=None,
        granted_capabilities=[],
        write_boundary=None,
        task_attributes={},
        request_id="request-1",
        run_id="child-1",
        task_id="task-1",
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
    )
    data.update(overrides)
    return ToolLoopExecuteParams(**data)


def test_tool_call_scope_reports_subagent_task_load_error() -> None:
    def broken_load(_run_id):
        raise RuntimeError("subagent ledger unreadable")

    agent = SimpleNamespace(subagents=SimpleNamespace(load=broken_load))

    envelope = tool_payload_with_run_scope(
        agent,
        _params(),
        {"tool": "read_file", "path": "README.md"},
        call_id="call-1",
    )

    assert envelope.scope.run_id == "child-1"
    assert envelope.scope.task_load_error["context"] == "tool_call_scope.subagents.load"
    assert "subagent ledger unreadable" in envelope.scope.task_load_error["message"]
