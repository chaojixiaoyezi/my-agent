from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core._runtime_params import ToolLoopExecuteParams
from agent_py_agent.agent.agent_core.main_agent_delivery_closeout import (
    MainAgentDeliveryCloseoutRequest,
    main_agent_delivery_closeout_response,
)
from agent_py_agent.agent.tooling.models import BaseTool, ToolExecutionResult, ToolSpec
from agent_py_agent.agent.tooling.registry_execution import (
    ExecuteRegistryCallParams,
    execute_registry_call,
)


class EchoTool(BaseTool):
    spec = ToolSpec(
        name="echo",
        category="utility",
        description="Return params for tests.",
        use_cases=[],
        avoid_when=[],
        keywords=[],
        parameters={},
    )

    def execute(self, params):
        return ToolExecutionResult("echo", True, json.dumps(params, sort_keys=True))


def test_registry_execution_returns_runtime_gate_denial_for_unknown_tool(tmp_path):
    result = execute_registry_call(
        ExecuteRegistryCallParams(
            payload={"tool": "magic_tool", "value": 1},
            tools={"echo": EchoTool()},
            workspace_root=tmp_path,
            workspace_roots=[tmp_path],
            expose_security_tools=False,
            security_tool_names=set(),
        )
    )

    assert result.ok is False
    assert result.result_envelope["runtime_gate"]["status"] == "DENY"
    assert result.result_envelope["runtime_gate"]["findings"][0]["code"] == "TOOL_NOT_REGISTERED"


def test_registry_execution_records_runtime_gate_allow_for_executed_tool(tmp_path):
    result = execute_registry_call(
        ExecuteRegistryCallParams(
            payload={"tool": "echo", "value": 1},
            tools={"echo": EchoTool()},
            workspace_root=tmp_path,
            workspace_roots=[tmp_path],
            expose_security_tools=False,
            security_tool_names=set(),
        )
    )

    assert result.ok is True
    assert result.result_envelope["runtime_gate"]["status"] == "ALLOW"
    assert result.result_envelope["runtime_gate"]["evidence"]["tool_name"] == "echo"


def test_registry_execution_blocks_dangerous_real_tool_without_approval(tmp_path):
    result = execute_registry_call(
        ExecuteRegistryCallParams(
            payload={"tool": "echo", "value": 1, "mode": "real", "idempotency_key": "idem-echo-dangerous"},
            tools={"echo": EchoTool()},
            workspace_root=tmp_path,
            workspace_roots=[tmp_path],
            expose_security_tools=False,
            security_tool_names=set(),
            write_boundary={"tool_effects": {"echo": "dangerous"}},
        )
    )

    assert result.ok is False
    assert result.result_envelope["runtime_gate"]["status"] == "NEED_APPROVAL"
    assert result.result_envelope["runtime_gate"]["findings"][0]["code"] == "APPROVAL_REQUIRED"


def test_delivery_closeout_report_contains_runtime_gate_decision(tmp_path):
    output = tmp_path / "out.txt"
    output.write_text("finished artifact", encoding="utf-8")
    params = ToolLoopExecuteParams(
        user_prompt="make artifact",
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
        task_attributes=None,
        request_id="req-1",
        run_id="run-1",
        task_id="task-1",
        one_shot_tool_calls=set(),
        executed_tools=[],
        archive_tool_calls=[],
        delivery_contract={
            "case_id": "generic-artifact",
            "artifacts": [{"artifact_id": "out", "path": "out.txt", "kind": "txt"}],
        },
    )
    agent = SimpleNamespace(root=tmp_path, tools=SimpleNamespace(workspace_root=tmp_path))

    response = main_agent_delivery_closeout_response(
        MainAgentDeliveryCloseoutRequest(agent=agent, params=params, backend="test")
    )
    report = json.loads((Path(tmp_path) / ".agent_delivery" / "closeout.json").read_text(encoding="utf-8"))

    assert response is not None
    assert report["runtime_gate"]["status"] == "ALLOW"
    assert report["acceptance_gate"]["status"] == "ALLOW"
    assert report["runtime_gate"]["evidence"]["artifact_count"] == 1
