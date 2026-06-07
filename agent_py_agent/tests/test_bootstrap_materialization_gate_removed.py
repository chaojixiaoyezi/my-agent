from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace


def test_bootstrap_contract_does_not_redirect_pure_inspection_tool_calls(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_loop.response_decision import (
        ToolLoopRepairCounters,
        ToolLoopResponseDecisionRequest,
        tool_loop_response_decision,
    )
    from agent_py_agent.agent.backends import ModelResponse

    calls = [{"tool": "list_files", "path": "."}]
    params = _params()
    decision = tool_loop_response_decision(
        ToolLoopResponseDecisionRequest(
            agent=_agent(tmp_path, {"CALL_LIST": calls}),
            params=params,
            response=ModelResponse(text="CALL_LIST", backend="fake"),
            counters=ToolLoopRepairCounters(),
        )
    )

    assert decision.action == "run_tools"
    assert decision.calls == calls
    assert not any("bootstrap-materialization" in item for item in params.tool_context)


def test_bootstrap_contract_does_not_block_repeated_evidence_calls(tmp_path: Path):
    from agent_py_agent.agent.agent_core.tool_loop.response_decision import (
        ToolLoopRepairCounters,
        ToolLoopResponseDecisionRequest,
        tool_loop_response_decision,
    )
    from agent_py_agent.agent.backends import ModelResponse

    calls = [{"tool": "web_search", "query": "project weekly growth", "limit": 5}]
    params = _params()
    agent = _agent(tmp_path, {"CALL_SEARCH": calls})

    for _ in range(8):
        decision = tool_loop_response_decision(
            ToolLoopResponseDecisionRequest(
                agent=agent,
                params=params,
                response=ModelResponse(text="CALL_SEARCH", backend="fake"),
                counters=ToolLoopRepairCounters(),
            )
        )

        assert decision.action == "run_tools"
        assert decision.calls == calls
        assert not any("BOOTSTRAP_MATERIALIZATION_BLOCKED" in str(item) for item in params.tool_context)


def _params():
    from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams

    return ToolLoopExecuteParams(
        user_prompt="test",
        memories=[],
        runtime_injections=[],
        prompt_files=[],
        tool_catalog_section="",
        tool_recommendations_section="",
        tool_context=[],
        effective_on_chunk=None,
        allowed_tools=None,
        granted_capabilities=None,
        write_boundary=None,
        task_attributes={},
        request_id="req-1",
        run_id="run-1",
        task_id="task-1",
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
        delivery_contract={
            "bootstrap_contract": {
                "enforcement": "required",
                "materialization_targets": [
                    {
                        "target_type": "checkpoint",
                        "workspace_relative_path": "outputs/report/source_data.json",
                    }
                ],
            }
        },
    )


def _agent(root: Path, calls_by_text: dict[str, list[dict[str, object]]]):
    class _Tools:
        workspace_root = root

        def parse_tool_calls(self, text: str):
            return calls_by_text.get(text, [])

    return SimpleNamespace(
        backend=SimpleNamespace(name="fake"),
        config=SimpleNamespace(enable_tools=True),
        root=root,
        tools=_Tools(),
    )
