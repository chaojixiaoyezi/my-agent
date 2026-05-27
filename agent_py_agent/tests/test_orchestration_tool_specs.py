"""orchestration tool spec tests.

函数/模块用途: 单独验证 orchestration 工具规格，避免执行行为测试文件继续膨胀。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock


# LLM: TestOrchestrationToolsSpec keeps model-facing tool schemas stable.
# 类用途: 验证 create/board/dispatch/schedule 这些 orchestration 工具暴露给模型的参数和说明。
class TestOrchestrationToolsSpec:
    """测试工具规格定义。"""

    def test_create_subagents_spec_defined(self):
        """CreateSubagentsTool 工具规格已定义。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10

        tool = CreateSubagentsTool(mock_agent)
        spec = tool.spec

        assert spec.name == "create_subagents"
        assert spec.category == "orchestration"
        assert "role" in spec.parameters
        assert "bug_finder" in spec.parameter_details["role"]
        assert "writer" in spec.parameter_details["role"]
        assert "最小必要信息" in spec.parameter_details["items"]
        assert "不要由 root 先读完所有正文再派工" in spec.parameter_details["items"]

    def test_subagent_board_spec_defined(self):
        """SubagentBoardTool 工具规格已定义。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import SubagentBoardTool

        mock_agent = MagicMock()
        mock_agent.subagents.workspace = Path("/tmp")

        tool = SubagentBoardTool(mock_agent)
        spec = tool.spec

        assert spec.name == "subagent_board"
        assert spec.category == "orchestration"

    def test_dispatch_subagents_spec_defined(self):
        """DispatchSubagentsTool 工具规格已定义。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import DispatchSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.subagent_workflow_mode = "off"
        mock_agent.tools.specs.return_value = []

        tool = DispatchSubagentsTool(mock_agent)
        spec = tool.spec

        assert spec.name == "dispatch_subagents"
        assert spec.category == "orchestration"

    def test_dispatch_subagents_spec_documents_leadership_recovery_params(self):
        """dispatch_subagents 暴露 leader 接管参数，避免恢复能力只存在于代码里。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import DispatchSubagentsTool

        mock_agent = MagicMock()
        mock_agent.config.subagent_workflow_mode = "off"
        mock_agent.tools.specs.return_value = []

        tool = DispatchSubagentsTool(mock_agent)
        spec = tool.spec

        assert "take_over_by" in spec.parameters
        assert "locked_files" in spec.parameters
        assert "leader" in spec.parameter_details["take_over_by"]
        assert any("take_over_by" in example for example in spec.examples)

    def test_schedule_child_subagents_spec_defined(self):
        """ScheduleChildSubagentsTool 工具规格已定义。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import ScheduleChildSubagentsTool

        mock_agent = MagicMock()
        tool = ScheduleChildSubagentsTool(mock_agent)
        spec = tool.spec

        assert spec.name == "schedule_child_subagents"
        assert spec.category == "orchestration"
        assert "当前 subagent runner" in spec.description
        assert "tester" in spec.parameter_details["children"]
        assert "找茬子代理" in spec.parameter_details["children"]
