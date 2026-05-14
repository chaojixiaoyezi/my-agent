"""orchestration_tools.py 单元测试。

测试编排工具注册、权限控制、执行边界等功能。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# LLM: test_create_subagents_tool_spec_uses_template_index_not_full_prompt protects startup token budget.
# 函数用途: create_subagents 工具说明只暴露角色模板索引，不把完整角色系统提示词放进主代理常驻工具说明。
def test_create_subagents_tool_spec_uses_template_index_not_full_prompt():
    from agent_py_agent.agent.agent_core.orchestration_tool_specs import build_create_subagents_spec

    spec = build_create_subagents_spec()
    role_detail = spec.parameter_details["role"]

    assert "模板位置" in role_detail
    assert "worker" in role_detail
    assert "你是执行子代理" not in role_detail
    assert "不同工作切片不要用 count" in spec.parameter_details["count"]

class TestSubagentBoardToolExecute:
    """测试 SubagentBoardTool.execute() 方法。"""

    def test_returns_board_summary(self):
        """验证返回看板摘要。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import SubagentBoardTool

        mock_agent = MagicMock()
        mock_agent.subagents.workspace = Path("/tmp/workspace")

        mock_board = MagicMock()
        mock_board.summary = {"total": 5, "running": 2}
        mock_item = MagicMock()
        mock_item.id = "item_1"
        mock_item.root_id = "item_1"
        mock_item.parent_id = ""
        mock_item.depth = 0
        mock_item.agent_name = "root"
        mock_item.role = "coordinator"
        mock_item.goal = "目标"
        mock_item.status = "RUNNING"
        mock_item.verification_status = "PENDING"
        mock_item.channel_status = "OK"
        mock_item.risk_flags = []
        mock_item.evidence_count = 0
        mock_item.child_count = 0
        mock_item.child_status_counts = {}
        mock_item.open_request_count = 0
        mock_item.open_gap_count = 0
        mock_item.latest_summary = ""
        mock_item.blocker_count = 0
        mock_item.target_tokens = []
        mock_item.task_dir = "/tmp/item"
        mock_item.output_json = "/tmp/item/output.json"
        mock_board.items = [mock_item]
        mock_agent.subagents.write_board.return_value = mock_board

        tool = SubagentBoardTool(mock_agent)
        result = tool.execute({"limit": 10})

        assert result.ok is True
        assert "summary" in result.output

    def test_status_filter_works(self):
        """状态过滤参数生效。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import SubagentBoardTool

        mock_agent = MagicMock()
        mock_agent.subagents.workspace = Path("/tmp/workspace")

        mock_item = MagicMock()
        mock_item.id = "run_1"
        mock_item.root_id = "run_1"
        mock_item.parent_id = ""
        mock_item.depth = 0
        mock_item.agent_name = "root"
        mock_item.role = "coordinator"
        mock_item.goal = "测试"
        mock_item.status = "RUNNING"
        mock_item.verification_status = "PENDING"
        mock_item.channel_status = "OK"
        mock_item.risk_flags = []
        mock_item.evidence_count = 0
        mock_item.child_count = 0
        mock_item.child_status_counts = {}
        mock_item.open_request_count = 0
        mock_item.open_gap_count = 0
        mock_item.latest_summary = ""
        mock_item.blocker_count = 0
        mock_item.target_tokens = []
        mock_item.task_dir = "/tmp"
        mock_item.output_json = "/tmp/output.json"

        mock_board = MagicMock()
        mock_board.summary = {"total": 1}
        mock_board.items = [mock_item]
        mock_agent.subagents.write_board.return_value = mock_board

        tool = SubagentBoardTool(mock_agent)
        result = tool.execute({"status": "RUNNING", "limit": 10})

        assert result.ok is True

class TestScheduleChildSubagentsTool:
    """测试当前 runner 创建下一层子节点的安全边界。"""

    def test_rejects_without_current_runner_context(self):
        """没有当前 runner id 时，不能绕过主节点直接挂 child。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import ScheduleChildSubagentsTool

        mock_agent = MagicMock()
        mock_agent._current_subagent_run_id = ""
        tool = ScheduleChildSubagentsTool(mock_agent)

        result = tool.execute({"children": [{"goal": "leaf"}], "apply": True})

        assert not result.ok
        assert "顶层派工请使用 create_subagents" in result.output

    def test_runner_context_max_depth_can_mean_one_more_layer(self, tmp_path):
        """模型在 depth=1 传 max_depth=1 时，按“再开一层”兼容处理。"""
        import json

        from agent_py_agent.agent.agent_core.orchestration_tools import ScheduleChildSubagentsTool
        from agent_py_agent.agent.config import AgentConfig
        from agent_py_agent.agent.core import SimpleAgent

        agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
        root = agent.subagents.create_run(goal="root", thought="root", plan=["root"])
        child = agent.subagents.create_run(
            goal="child", thought="child", plan=["child"], parent_id=root.id, root_id=root.id, depth=1,
        )
        agent._current_subagent_run_id = child.id
        tool = ScheduleChildSubagentsTool(agent)

        result = tool.execute(
            {
                "apply": True,
                "max_depth": 1,
                "children": [{"goal": "leaf", "role": "leaf", "agent_name": "leaf"}],
            }
        )
        payload = json.loads(result.output)
        leaf = agent.subagents.load(payload["created_run_ids"][0])

        assert result.ok
        assert leaf.parent_id == child.id
        assert leaf.depth == 2

    def test_runner_context_schedule_defaults_to_apply_direct_child(self, tmp_path):
        """runner 内部省略 apply 时，应真实创建当前节点的直接 child。"""
        import json

        from agent_py_agent.agent.agent_core.orchestration_tools import ScheduleChildSubagentsTool
        from agent_py_agent.agent.config import AgentConfig
        from agent_py_agent.agent.core import SimpleAgent

        agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
        root = agent.subagents.create_run(goal="root", thought="root", plan=["root"])
        agent._current_subagent_run_id = root.id
        tool = ScheduleChildSubagentsTool(agent)

        result = tool.execute({"children": [{"goal": "leaf", "role": "leaf_worker", "agent_name": "leaf"}]})
        payload = json.loads(result.output)

        assert result.ok
        assert payload["dry_run"] is False
        assert len(payload["created_run_ids"]) == 1
        assert agent.subagents.load(root.id).child_ids == payload["created_run_ids"]

    def test_runner_context_schedule_accepts_orchestration_wrapper(self, tmp_path):
        """真实模型常把 children 包进 orchestration；工具入口要展开后再调度。"""
        import json

        from agent_py_agent.agent.agent_core.orchestration_tools import ScheduleChildSubagentsTool
        from agent_py_agent.agent.config import AgentConfig
        from agent_py_agent.agent.core import SimpleAgent

        agent = SimpleAgent(AgentConfig(model_backend="echo", subagent_workspace="subs"), tmp_path)
        root = agent.subagents.create_run(goal="root", thought="root", plan=["root"])
        agent._current_subagent_run_id = root.id
        tool = ScheduleChildSubagentsTool(agent)

        result = tool.execute({
            "tool": "schedule_child_subagents",
            "orchestration": {
                "apply": True,
                "children": [{"goal": "grandchild coordinator", "role": "coordinator", "agent_name": "页面组"}],
            },
        })
        payload = json.loads(result.output)
        child = agent.subagents.load(payload["created_run_ids"][0])

        assert result.ok
        assert payload["dry_run"] is False
        assert child.parent_id == root.id
        assert child.agent_name == "小傻妞-页面组"
