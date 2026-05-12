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
from agent_py_agent.agent.subagents.services.hierarchy_scheduler import (
    HierarchyChildSpec,
    HierarchyScheduleRequest,
)


# LLM: _agent_with_current_run builds the minimal facade CapabilityRequestTool needs.
# 函数用途: 创建真实 SubAgentManager 和当前 run_id，避免测试依赖完整模型后端。
def _agent_with_current_run(tmp_path):
    manager = SubAgentManager(tmp_path / "subs")
    task = manager.create_run(goal="need controlled exec", thought="ask parent", plan=["request"])
    return SimpleNamespace(subagents=manager, _current_subagent_run_id=task.id), manager, task


# LLM: test_capability_request_tool_records_open_request covers the model-callable formal lane.
# 函数用途: 子代理调用 capability_request 后，任务记录里必须出现 OPEN 请求和 scoped shell 字段。
def test_capability_request_tool_records_open_request(tmp_path):
    agent, manager, task = _agent_with_current_run(tmp_path)

    result = CapabilityRequestTool(agent).execute(
        {
            "orchestration": {
                "problem": "需要执行 pwd 和 python3 生成真实环境证据。",
                "needed_capability": "controlled_exec",
                "capability_type": "shell",
                "requested_tools": ["controlled_exec"],
                "requested_commands": ["pwd", "python3"],
                "path_scope": [str(tmp_path)],
                "output_budget": {"stdout_bytes": 1024, "stderr_bytes": 512},
                "risk_level": "low",
            },
            "filesystem": {"write_root": str(tmp_path)},
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


# LLM: test_capability_request_tool_blocks_cross_run_writes locks hierarchy authority.
# 函数用途: 当前 runner 不能通过显式 run_id 替 sibling 或其他分支写能力申请。
def test_capability_request_tool_blocks_cross_run_writes(tmp_path):
    agent, manager, _task = _agent_with_current_run(tmp_path)
    other = manager.create_run(goal="other", thought="separate", plan=["noop"])

    result = CapabilityRequestTool(agent).execute(
        {
            "run_id": other.id,
            "problem": "试图替别的 run 申请工具。",
            "needed_capability": "controlled_exec",
        }
    )

    assert result.ok is False
    assert "只能为当前 runner" in result.output
    assert manager.load(other.id).capability_requests == []


# LLM: test_capability_request_tool_is_registered_for_simple_agent proves runners can see the tool.
# 函数用途: SimpleAgent 初始化后，Tool Catalog 能在 orchestration 工具里展示 capability_request。
def test_capability_request_tool_is_registered_for_simple_agent(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)

    specs = {spec.name: spec for spec in agent.tools.specs(include_orchestration=True)}

    assert "capability_request" in specs
    assert specs["capability_request"].category == "orchestration"


# LLM: test_capability_request_tool_is_available_to_role_and_leaf_defaults keeps users from hand-picking it.
# 函数用途: 默认角色和自动推断叶子工具都带 capability_request，不需要用户配置每个子代理工具。
def test_capability_request_tool_is_available_to_role_and_leaf_defaults(tmp_path):
    assert "capability_request" in ROLE_BASE_TOOLS
    manager = SubAgentManager(tmp_path / "subs")
    deliverables = tmp_path / "deliverables"
    parent = manager.create_run(
        goal="parent",
        thought="split",
        plan=["delegate"],
        allowed_tools=["schedule_child_subagents", "dispatch_subagents", "subagent_board"],
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
