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


def _agent(root: Path, *, enable_tools: bool = True):
    class _Tools:
        workspace_root = root

        def __init__(self) -> None:
            self.parsed_texts: list[str] = []

        def parse_tool_calls(self, text: str):
            self.parsed_texts.append(text)
            if "[TOOL_CALL]" in text:
                return [{"tool": "read_file", "path": "from_text.md"}]
            return []

    return SimpleNamespace(
        backend=SimpleNamespace(name="fake"),
        config=SimpleNamespace(enable_tools=enable_tools),
        root=root,
        tools=_Tools(),
    )


def _params() -> ToolLoopExecuteParams:
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
        delivery_contract={},
    )


def _decide(agent, response: ModelResponse):
    return tool_loop_response_decision(
        ToolLoopResponseDecisionRequest(
            agent=agent,
            params=_params(),
            response=response,
            counters=ToolLoopRepairCounters(),
        )
    )


def test_tool_use_blocks_flatten_to_run_tools_without_text_parse(tmp_path: Path):
    agent = _agent(tmp_path)
    response = ModelResponse(
        text="",
        backend="fake",
        tool_use_blocks=[
            {"id": "toolu_1", "name": "read_file", "input": {"path": "README.md"}},
        ],
    )

    decision = _decide(agent, response)

    assert decision.action == "run_tools"
    assert decision.calls == [{"path": "README.md", "tool": "read_file", "call_id": "toolu_1"}]
    # native path must NOT fall back to text parsing
    assert agent.tools.parsed_texts == []


def test_multiple_tool_use_blocks_all_flatten(tmp_path: Path):
    agent = _agent(tmp_path)
    response = ModelResponse(
        text="",
        backend="fake",
        tool_use_blocks=[
            {"id": "a", "name": "read_file", "input": {"path": "x"}},
            {"id": "b", "name": "search_text", "input": {"query": "foo"}},
        ],
    )

    decision = _decide(agent, response)

    assert decision.action == "run_tools"
    assert [c["tool"] for c in decision.calls] == ["read_file", "search_text"]
    assert decision.calls[1] == {"query": "foo", "tool": "search_text", "call_id": "b"}


def test_no_tool_use_blocks_falls_back_to_text_protocol(tmp_path: Path):
    agent = _agent(tmp_path)
    response = ModelResponse(text='[TOOL_CALL]\n{"tool":"read_file"}\n[/TOOL_CALL]', backend="fake")

    decision = _decide(agent, response)

    assert decision.action == "run_tools"
    assert decision.calls == [{"tool": "read_file", "path": "from_text.md"}]
    # fall-back path DID parse the text
    assert agent.tools.parsed_texts == [response.text]


def test_no_blocks_and_plain_text_breaks(tmp_path: Path):
    agent = _agent(tmp_path)
    response = ModelResponse(text="任务完成。", backend="fake")

    decision = _decide(agent, response)

    assert decision.action == "break"
    assert decision.response.text == "任务完成。"


def test_tool_use_input_tool_key_does_not_override_name(tmp_path: Path):
    agent = _agent(tmp_path)
    response = ModelResponse(
        text="",
        backend="fake",
        tool_use_blocks=[
            {"id": "z", "name": "read_file", "input": {"tool": "evil", "path": "p"}},
        ],
    )

    decision = _decide(agent, response)

    assert decision.calls[0]["tool"] == "read_file"
    assert decision.calls[0]["path"] == "p"


def test_disabled_tools_short_circuits_before_native_blocks(tmp_path: Path):
    agent = _agent(tmp_path, enable_tools=False)
    response = ModelResponse(
        text="done",
        backend="fake",
        tool_use_blocks=[{"id": "1", "name": "read_file", "input": {"path": "x"}}],
    )

    decision = _decide(agent, response)

    assert decision.action == "break"
    assert decision.calls == []
