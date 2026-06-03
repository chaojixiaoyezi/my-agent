"""LLM: focused tests for formal subagent capability requests.

函数/模块用途: 验证 runner 能通过 capability_request 工具记录正式能力申请，且不能替其他 run_id 越权写入。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.capability_request_tool import CapabilityRequestTool
from agent_py_agent.agent.config import AgentConfig
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.role_templates import ROLE_BASE_TOOLS
from agent_py_agent.agent.subagents.services.hierarchy.scheduler import (
    HierarchyChildSpec,
    HierarchyScheduleRequest,
)


def _agent_with_current_run(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    parent = manager.create_run(goal="root", thought="decide", plan=["delegate"])
    task = manager.create_run(
        goal="need controlled exec",
        thought="ask parent",
        plan=["request"],
        parent_id=parent.id,
        root_id=parent.id,
        depth=1,
    )
    return SimpleNamespace(subagents=manager, _current_subagent_run_id=task.id), manager, task


def test_capability_request_tool_records_open_request(tmp_path):
    agent, manager, task = _agent_with_current_run(tmp_path)

    result = CapabilityRequestTool(agent).execute(
        {
            "problem": "需要执行 pwd 和 python3 生成真实环境证据。",
            "needed_capability": "controlled_exec",
            "capability_type": "shell",
            "requested_tools": ["controlled_exec"],
            "requested_commands": ["pwd", "python3"],
            "path_scope": [str(tmp_path)],
            "output_budget": {"stdout_bytes": 1024, "stderr_bytes": 512},
            "risk_level": "low",
        }
    )

    assert result.ok is True
    payload = json.loads(result.output)
    loaded = manager.load(task.id)
    request = loaded.capability_requests[0]
    assert payload["request_id"] == request.id
    assert payload["next_action"] == "route_capability_request"
    assert request.status == "OPEN"
    assert request.needed_capability == "controlled_exec"
    assert request.requested_tools == ["controlled_exec"]
    assert request.requested_commands == ["pwd", "python3"]
    assert request.output_budget["stdout_bytes"] == 1024


def test_capability_request_tool_scopes_cross_run_writes_to_current_runner(tmp_path):
    agent, manager, task = _agent_with_current_run(tmp_path)
    other = manager.create_run(goal="other", thought="separate", plan=["noop"])

    result = CapabilityRequestTool(agent).execute(
        {
            "run_id": other.id,
            "problem": "试图替别的 run 申请工具。",
            "needed_capability": "controlled_exec",
        }
    )

    payload = json.loads(result.output)
    assert result.ok is True
    assert payload["run_id"] == task.id
    assert "explicit_scope_overridden_by_current_runner" in payload["scope_warnings"]
    assert payload["scope_resolution"]["ignored_explicit"]["run_id"] == other.id
    assert len(manager.load(task.id).capability_requests) == 1
    assert manager.load(other.id).capability_requests == []


def test_capability_request_tool_blocks_root_run_requests(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    root = manager.create_run(
        goal="root owns decisions",
        thought="decide",
        plan=["delegate"],
        role="coordinator",
    )
    agent = SimpleNamespace(subagents=manager, _current_subagent_run_id=root.id)

    result = CapabilityRequestTool(agent).execute(
        {
            "problem": "root 想删除任务目录里的临时文件。",
            "needed_capability": "delete_file",
            "requested_tools": ["delete_file"],
        }
    )

    assert result.ok is False
    assert "root run 不走 capability_request" in result.output
    assert manager.load(root.id).capability_requests == []


def test_top_level_worker_can_request_capability(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    task = manager.create_run(
        goal="top-level child needs network",
        thought="ask main parent",
        plan=["request"],
        role="worker",
    )
    agent = SimpleNamespace(subagents=manager, _current_subagent_run_id=task.id)

    result = CapabilityRequestTool(agent).execute(
        {
            "problem": "需要 web_fetch 核验网页。",
            "needed_capability": "network",
            "requested_tools": ["web_fetch"],
        }
    )

    assert result.ok is True
    assert manager.load(task.id).capability_requests[0].requested_tools == ["web_fetch"]


def test_capability_request_tool_is_registered_for_simple_agent(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)

    specs = {spec.name: spec for spec in agent.tools.specs(include_orchestration=True)}

    assert "capability_request" in specs
    assert specs["capability_request"].category == "orchestration"


def test_capability_request_tool_is_available_to_role_and_leaf_defaults(tmp_path):
    assert "capability_request" in ROLE_BASE_TOOLS
    manager = SubAgentManager(tmp_path / "subs")
    deliverables = tmp_path / "deliverables"
    parent = manager.create_run(
        goal="parent",
        thought="split",
        plan=["delegate"],
        allowed_tools=["schedule_child_subagents", "dispatch_subagents", "inspect_agent_tree"],
        extra_write_roots=[str(deliverables)],
    )

    result = manager.schedule_child_runs(
        params=HierarchyScheduleRequest(
            parent_run_id=parent.id,
            child_specs=[
                HierarchyChildSpec(
                    goal=f"写入 {deliverables}/leaf/solution.py，并在缺 shell 时申请 controlled_exec。",
                    agent_name="leaf-capability",
                    role="worker",
                )
            ],
            apply=True,
        )
    )

    leaf = manager.load(result.created_run_ids[0])
    assert "capability_request" in leaf.allowed_tools


def test_root_execution_context_hides_capability_request_tool(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    root = manager.create_run(
        goal="root 负责调度",
        thought="root 没有上级",
        plan=["派工"],
        role="coordinator",
        allowed_tools=["schedule_child_subagents", "dispatch_subagents", "capability_request"],
    )
    child = manager.create_run(
        goal="child 需要可申请能力",
        thought="child 有父级",
        plan=["执行"],
        parent_id=root.id,
        root_id=root.id,
        depth=1,
        role="worker",
        allowed_tools=["read_file", "write_file", "capability_request"],
    )

    root_context = manager.build_execution_context(root.id)
    child_context = manager.build_execution_context(child.id)

    assert "capability_request" not in root_context.allowed_tools
    assert "capability_request" in child_context.allowed_tools


def test_top_level_worker_context_keeps_capability_request(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    worker = manager.create_run(
        goal="主代理直接派的小傻妞要查网页",
        thought="缺能力就向主代理申请",
        plan=["执行"],
        role="worker",
        allowed_tools=["read_file", "write_file", "capability_request"],
    )

    context = manager.build_execution_context(worker.id)

    assert "capability_request" in context.allowed_tools
