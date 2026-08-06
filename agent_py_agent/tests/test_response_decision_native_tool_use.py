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


def _agent(root: Path, *, enable_tools: bool = True):
    return SimpleNamespace(
        backend=SimpleNamespace(name="fake"),
        config=SimpleNamespace(enable_tools=enable_tools),
        root=root,
    )


def _runtime_snapshot():
    return runtime_snapshot_for_model_specs(
        (
            make_test_model_spec(
                "read_file",
                input_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                    "additionalProperties": False,
                },
            ),
            make_test_model_spec(
                "search_text",
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
            ),
        ),
        run_id="run-1",
    )


def _params(source_protocol: str = "native") -> ToolLoopExecuteParams:
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
        delivery_contract={},
        tool_runtime_snapshot=_runtime_snapshot(),
        tool_protocol_snapshot=make_test_protocol_snapshot(
            run_id="run-1",
            source_protocol=source_protocol,
        ),
    )


def _decide(agent, response: ModelResponse, *, source_protocol: str = "native"):
    return tool_loop_response_decision(
        ToolLoopResponseDecisionRequest(
            agent=agent,
            params=_params(source_protocol),
            response=response,
            counters=ToolLoopRepairCounters(),
        )
    )


def test_native_tool_use_blocks_become_canonical_calls_without_text_parse(tmp_path: Path):
    response = ModelResponse(
        text="",
        backend="fake",
        tool_use_blocks=[
            {"id": "toolu_1", "name": "read_file", "input": {"path": "README.md"}},
        ],
    )

    decision = _decide(_agent(tmp_path), response)

    assert decision.action == "run_tools"
    assert len(decision.calls) == 1
    call = decision.calls[0]
    assert call.call_id == "toolu_1"
    assert call.tool_name == "read_file"
    assert call.arguments == {"path": "README.md"}
    assert call.source_protocol == "native"


def test_multiple_native_tool_use_blocks_keep_order(tmp_path: Path):
    response = ModelResponse(
        text="",
        backend="fake",
        tool_use_blocks=[
            {"id": "a", "name": "read_file", "input": {"path": "x"}},
            {"id": "b", "name": "search_text", "input": {"query": "foo"}},
        ],
    )

    decision = _decide(_agent(tmp_path), response)

    assert decision.action == "run_tools"
    assert [call.tool_name for call in decision.calls] == ["read_file", "search_text"]
    assert decision.calls[1].arguments == {"query": "foo"}


def test_native_run_never_falls_back_to_text_tool_blocks(tmp_path: Path):
    response = ModelResponse(
        text='[TOOL_CALL]\n{"tool":"read_file","path":"README.md"}\n[/TOOL_CALL]',
        backend="fake",
    )

    decision = _decide(_agent(tmp_path), response, source_protocol="native")

    assert decision.action == "continue"
    assert decision.calls == []
    assert decision.counters.protocol_repairs == 1


def test_explicit_text_run_accepts_one_complete_standalone_block(tmp_path: Path):
    response = ModelResponse(
        text='[TOOL_CALL]\n{"tool":"read_file","path":"README.md"}\n[/TOOL_CALL]',
        backend="fake",
    )

    decision = _decide(_agent(tmp_path), response, source_protocol="text")

    assert decision.action == "run_tools"
    assert len(decision.calls) == 1
    assert decision.calls[0].tool_name == "read_file"
    assert decision.calls[0].arguments == {"path": "README.md"}
    assert decision.calls[0].source_protocol == "text"


def test_no_blocks_and_plain_text_breaks(tmp_path: Path):
    response = ModelResponse(text="任务完成。", backend="fake")

    decision = _decide(_agent(tmp_path), response)

    assert decision.action == "break"
    assert decision.response.text == "任务完成。"


def test_tool_use_input_tool_key_does_not_override_provider_name(tmp_path: Path):
    response = ModelResponse(
        text="",
        backend="fake",
        tool_use_blocks=[
            {"id": "z", "name": "read_file", "input": {"tool": "evil", "path": "p"}},
        ],
    )

    decision = _decide(_agent(tmp_path), response)

    assert decision.calls[0].tool_name == "read_file"
    assert decision.calls[0].arguments == {"tool": "evil", "path": "p"}


def test_disabled_tools_short_circuits_before_native_blocks(tmp_path: Path):
    response = ModelResponse(
        text="done",
        backend="fake",
        tool_use_blocks=[{"id": "1", "name": "read_file", "input": {"path": "x"}}],
    )

    decision = _decide(_agent(tmp_path, enable_tools=False), response)

    assert decision.action == "break"
    assert decision.calls == []
