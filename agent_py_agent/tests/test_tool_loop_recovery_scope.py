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
        _params(context_scope="task_local"),
        {"tool": "read_file", "path": "README.md"},
        call_id="call-1",
    )

    assert envelope.scope.run_id == "child-1"
    assert envelope.scope.root_task_id == "task-1"
    assert envelope.scope.agent_kind == "child_agent"
    assert envelope.scope.task_load_error["context"] == "tool_call_scope.subagents.load"
    assert "subagent ledger unreadable" in envelope.scope.task_load_error["message"]


def test_background_root_scope_does_not_query_subagent_ledger() -> None:
    load_calls: list[str] = []

    def unexpected_load(run_id):
        load_calls.append(run_id)
        raise AssertionError("root runtime must not query the subagent ledger")

    agent = SimpleNamespace(subagents=SimpleNamespace(load=unexpected_load))

    envelope = tool_payload_with_run_scope(
        agent,
        _params(
            source="background_main_agent",
            run_id="bg-main-thread-1",
            task_id="request-1",
            task_attributes={"conversation_task_id": "request-1"},
        ),
        {"tool": "read_file", "path": "README.md"},
        call_id="call-root",
    )

    assert load_calls == []
    assert envelope.scope.run_id == "bg-main-thread-1"
    assert envelope.scope.task_id == "request-1"
    assert envelope.scope.root_run_id == "bg-main-thread-1"
    assert envelope.scope.root_task_id == "request-1"
    assert envelope.scope.agent_kind == "root_agent"
    assert envelope.scope.task_load_error == {}


def test_subagent_scope_uses_loaded_lineage() -> None:
    task = SimpleNamespace(parent_id="child-1", root_id="root-1", depth=2)
    agent = SimpleNamespace(subagents=SimpleNamespace(load=lambda run_id: task if run_id == "grandchild-1" else None))

    envelope = tool_payload_with_run_scope(
        agent,
        _params(
            context_scope="task_local",
            run_id="grandchild-1",
            task_id="root-1",
        ),
        {"tool": "read_file", "path": "README.md"},
        call_id="call-grandchild",
    )

    assert envelope.scope.run_id == "grandchild-1"
    assert envelope.scope.parent_run_id == "child-1"
    assert envelope.scope.root_run_id == "root-1"
    assert envelope.scope.root_task_id == "root-1"
    assert envelope.scope.depth == 2
    assert envelope.scope.agent_kind == "grandchild_agent"
    assert envelope.scope.task_load_error == {}


def test_native_provider_call_id_is_not_forwarded_as_tool_input() -> None:
    agent = SimpleNamespace(subagents=SimpleNamespace(load=lambda _run_id: None))

    envelope = tool_payload_with_run_scope(
        agent,
        _params(source="background_main_agent"),
        {
            "tool": "example_tool",
            "call_id": "provider-call-9",
            "status": "active",
            "operation_id": "legitimate-tool-argument",
        },
        call_id="provider-call-9",
    )

    assert envelope.call_id == "provider-call-9"
    assert envelope.operation_id == "tool_call:provider-call-9"
    assert envelope.input == {
        "status": "active",
        "operation_id": "legitimate-tool-argument",
    }
