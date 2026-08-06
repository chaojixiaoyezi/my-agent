from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.tool_loop.response_decision import (
    ToolLoopRepairCounters,
    ToolLoopResponseDecisionRequest,
    tool_loop_response_decision,
)
from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.tests._tool_runtime_harness import (
    make_test_model_spec,
    make_test_protocol_snapshot,
    runtime_snapshot_for_model_specs,
)


def test_bootstrap_contract_does_not_redirect_pure_inspection_tool_calls(tmp_path: Path):
    params = _params()
    decision = tool_loop_response_decision(
        ToolLoopResponseDecisionRequest(
            agent=_agent(tmp_path),
            params=params,
            response=ModelResponse(
                text='[TOOL_CALL]\n{"tool":"list_files","path":"."}\n[/TOOL_CALL]',
                backend="fake",
            ),
            counters=ToolLoopRepairCounters(),
        )
    )

    assert decision.action == "run_tools"
    assert len(decision.calls) == 1
    assert decision.calls[0].tool_name == "list_files"
    assert decision.calls[0].arguments == {"path": "."}
    assert not any("bootstrap-materialization" in item for item in params.tool_context)


def test_bootstrap_contract_does_not_block_repeated_evidence_calls(tmp_path: Path):
    params = _params()
    agent = _agent(tmp_path)

    for _ in range(8):
        decision = tool_loop_response_decision(
            ToolLoopResponseDecisionRequest(
                agent=agent,
                params=params,
                response=ModelResponse(
                    text=(
                        '[TOOL_CALL]\n{"tool":"web_search",'
                        '"query":"project weekly growth","limit":5}\n[/TOOL_CALL]'
                    ),
                    backend="fake",
                ),
                counters=ToolLoopRepairCounters(),
            )
        )

        assert decision.action == "run_tools"
        assert len(decision.calls) == 1
        assert decision.calls[0].tool_name == "web_search"
        assert not any(
            "BOOTSTRAP_MATERIALIZATION_BLOCKED" in str(item)
            for item in params.tool_context
        )


def _params() -> ToolLoopExecuteParams:
    specs = (
        make_test_model_spec(
            "list_files",
            input_schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "additionalProperties": False,
            },
        ),
        make_test_model_spec(
            "web_search",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["query"],
                "additionalProperties": False,
            },
        ),
    )
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
        write_boundary=None,
        task_attributes={},
        request_id="req-1",
        run_id="run-1",
        task_id="task-1",
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
        tool_runtime_snapshot=runtime_snapshot_for_model_specs(specs, run_id="run-1"),
        tool_protocol_snapshot=make_test_protocol_snapshot(
            run_id="run-1", source_protocol="text"
        ),
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


def _agent(root: Path):
    return SimpleNamespace(
        backend=SimpleNamespace(name="fake"),
        config=SimpleNamespace(enable_tools=True),
        root=root,
    )
