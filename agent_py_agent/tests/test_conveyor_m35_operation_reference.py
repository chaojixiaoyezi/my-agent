"""工作流业务工具的显示名和操作账本身份可独立核验。"""

import pytest

from agent_py_agent.agent.agent_core.self_map_bootstrap import _WorkflowToolDispatcher
from agent_py_agent.agent.contracts.tool_operation_reference import ToolOperationReference
from agent_py_agent.agent.gateway_parts import business_api
from agent_py_agent.agent.runtime_db.managed_operation_store import ManagedOperationStore
from agent_py_agent.agent.workflows import run_store as run_records
from agent_py_agent.agent.workflows.run_store import WorkflowRunStore
from agent_py_agent.tests.test_acceptance_workflow_native_ports import (  # noqa: F401
    flow,
    host,
    local_run,
)


def _enable_business_tool(agent, *, allowed_action=None):
    agent.config.business_host_ports_enabled = True
    agent.business_operation_store = agent.tools.operation_store
    business_api.register_business_tools(agent)
    agent._current_run_params.allowed_tools.append("business_space")
    if allowed_action is not None:
        agent._current_run_params.allowed_tools.append(f"business_space:{allowed_action}")
    assert isinstance(agent.tools.operation_store, ManagedOperationStore)


def _space_definition(tool_name="business_space", *, action="spaces.create"):
    return flow("m35-space-create", [{
        "step_id": "create",
        "name": "create",
        "mode": "tool_call",
        "tool_spec": {
            "tool_name": tool_name,
            "arguments": {
                **({"action": action} if action is not None else {}),
                "request_id": "m35-create-request",
                "idempotency_key": "m35-create-idempotency",
                "params": {"name": "M35 workspace"},
            },
        },
    }])


@pytest.mark.parametrize("tool_name,action", [
    ("business_space", "spaces.create"),
    ("business_spaces_create", None),
])
def test_space_create_binds_original_operation(host, tool_name, action):  # noqa: F811
    _enable_business_tool(host)
    result, run = local_run(host, _space_definition(tool_name, action=action))

    assert result.ok and run.status == "succeeded", run.to_dict()
    facts = run.steps[0].escalation_result
    assert "tool_operation_binding" in facts, facts
    binding = facts["tool_operation_binding"]
    receipt = facts["tool_receipt"]
    ref = ToolOperationReference.from_dict(binding["operation_reference"])
    assert ref == ToolOperationReference.from_dict(receipt["operation_reference"])
    assert ref == ToolOperationReference(
        display_name="business_space", domain="business_space", action="spaces.create",
        storage_namespace="business_spaces_create", operation_id=binding["operation_id"], args_hash=binding["args_hash"],
    )
    assert binding["tool_name"] == ref.storage_tool_name
    assert receipt["tool_name"] == ref.display_name
    frozen = WorkflowRunStore(host.home_paths.owner_home_dir).load_workflow_snapshot(run.workflow_run_id)
    assert frozen["steps"][0]["tool_spec"]["tool_name"] == tool_name


@pytest.mark.parametrize("tool_name,action", [
    ("business_space", "spaces.create"),
    ("business_spaces_create", None),
    ("business_spaces_create", "spaces.list"),
])
def test_legacy_decode_keeps_action_authorization(host, monkeypatch, tool_name, action):  # noqa: F811
    _enable_business_tool(host, allowed_action="spaces.list")
    writes = []
    original = business_api.execute_business

    def count_write(request, *args, **kwargs):
        if request.get("action") == "spaces.create":
            writes.append(request)
        return original(request, *args, **kwargs)

    monkeypatch.setattr(business_api, "execute_business", count_write)
    result, run = local_run(host, _space_definition(tool_name, action=action))
    assert not result.ok and run.status == "failed"
    assert run.steps[0].escalation_result["effect_outcome"] == "not_started"
    assert writes == []


def test_space_create_resumes_after_interrupted_result(host, monkeypatch):  # noqa: F811
    _enable_business_tool(host)
    dispatch = _WorkflowToolDispatcher.execute_scoped
    calls = []

    def interrupted_result(self, tool_name, arguments, **scope):
        result = dispatch(self, tool_name, arguments, **scope)
        assert result["ok"] and result["effect_outcome"] == "confirmed", result
        calls.append(result["operation_id"])
        # 模拟原写入完成后、工作流收到终态前的中断，保留原操作身份。
        return {**result, "ok": False, "status": "failed", "effect_outcome": "unknown",
                "error_code": "TOOL_OPERATION_OUTCOME_UNKNOWN"}

    with monkeypatch.context() as fault:
        fault.setattr(_WorkflowToolDispatcher, "execute_scoped", interrupted_result)
        result, run = local_run(host, _space_definition())
    assert not result.ok and run.status == "failed"
    receipt = run.steps[0].escalation_result["tool_receipt"]
    assert receipt["effect_outcome"] == "unknown" and receipt["operation_id"] == calls[0]

    def unexpected_dispatch(*args, **kwargs):
        pytest.fail("resume dispatched the original operation again")

    monkeypatch.setattr(_WorkflowToolDispatcher, "execute_scoped", unexpected_dispatch)
    resume = {
        "action": "workflow_runs.resume", "workflow_run_id": run.workflow_run_id,
        "idempotency_key": "m35-resume-request", "expected_revision": run.revision,
    }
    workflows = host.tools.tools["workflows"]
    resumed = workflows.execute(resume)
    assert resumed.ok, resumed.output
    restored = WorkflowRunStore(host.home_paths.owner_home_dir).load_run(run.workflow_run_id)
    assert restored.status == "succeeded"
    output = restored.steps[0].output_snapshot
    assert output["operation_id"] == calls[0]
    assert output["tool_name"] == "business_space"
    assert output["metadata"]["operation_reference"] == receipt["operation_reference"]
    assert workflows.execute(resume).ok


def test_space_create_resumes_after_terminal_write_error(host, monkeypatch):  # noqa: F811
    _enable_business_tool(host)
    write = run_records._write_json
    failed_publications = []

    def fail_terminal_once(root, parts, data):
        if (parts[-1] == "run.json" and data.get("status") == "succeeded"
                and not failed_publications):
            receipt = data["steps"][0]["escalation_result"]["tool_receipt"]
            failed_publications.append(receipt)
            raise OSError("simulated terminal publication error")
        return write(root, parts, data)

    monkeypatch.setattr(run_records, "_write_json", fail_terminal_once)
    result, run = local_run(host, _space_definition())
    assert not result.ok and run.status == "paused_for_resume", run.to_dict()
    assert failed_publications == [run.steps[0].escalation_result["tool_receipt"]]

    def unexpected_dispatch(*args, **kwargs):
        pytest.fail("terminal recovery dispatched the original operation again")

    monkeypatch.setattr(_WorkflowToolDispatcher, "execute_scoped", unexpected_dispatch)
    workflows = host.tools.tools["workflows"]
    resumed = workflows.execute({
        "action": "workflow_runs.resume", "workflow_run_id": run.workflow_run_id,
        "idempotency_key": "m35-terminal-resume", "expected_revision": run.revision,
    })
    assert resumed.ok, resumed.output
    restored = WorkflowRunStore(host.home_paths.owner_home_dir).load_run(run.workflow_run_id)
    assert restored.status == "succeeded"
    assert restored.steps[0].escalation_result["tool_receipt"] == failed_publications[0]
