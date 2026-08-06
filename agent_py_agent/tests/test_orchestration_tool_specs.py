"""orchestration tool spec tests.

函数/模块用途: 单独验证 orchestration 工具规格，避免执行行为测试文件继续膨胀。
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from agent_py_agent.agent.capability import CapabilityRouter
from agent_py_agent.agent.subagents.role_templates import load_role_template_store


class TestOrchestrationToolsSpec:
    """测试工具规格定义。"""

    def test_create_subagents_spec_defined(self):
        """CreateSubagentsTool 工具规格已定义。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()

        mock_agent.capability_router = CapabilityRouter()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10

        tool = CreateSubagentsTool(mock_agent)
        spec = tool.model_spec

        assert spec.name == "create_subagents"
        assert spec.category == "orchestration"
        assert "role" in spec.parameter_descriptions
        assert "bug_finder" in spec.parameter_descriptions["role"]
        assert "writer" in spec.parameter_descriptions["role"]
        assert "资料线索" in spec.parameter_descriptions["items"]
        assert "root" not in spec.parameter_descriptions["items"]
        assert "context_manifest" not in spec.parameter_descriptions
        assert "context_packs" not in spec.parameter_descriptions
        assert "output_refs" not in spec.parameter_descriptions

    def test_dispatch_subagents_spec_defined(self):
        """DispatchSubagentsTool 工具规格已定义。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import DispatchSubagentsTool

        mock_agent = MagicMock()

        mock_agent.capability_router = CapabilityRouter()
        mock_agent.tools.specs.return_value = []

        tool = DispatchSubagentsTool(mock_agent)
        spec = tool.model_spec

        assert spec.name == "dispatch_subagents"
        assert spec.category == "orchestration"

    def test_dispatch_subagents_spec_hides_internal_recovery_params(self):
        """dispatch_subagents 的模型规格不暴露内部恢复接管字段。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import DispatchSubagentsTool

        mock_agent = MagicMock()

        mock_agent.capability_router = CapabilityRouter()
        mock_agent.tools.specs.return_value = []

        tool = DispatchSubagentsTool(mock_agent)
        spec = tool.model_spec

        assert "take_over_by" not in spec.parameter_descriptions
        assert "locked_files" not in spec.parameter_descriptions
        assert "workflow_mode" not in spec.parameter_descriptions
        assert not any("take_over_by" in example for example in spec.examples)
        assert not any("workflow_mode" in example for example in spec.examples)

    def test_schedule_child_subagents_spec_defined(self):
        """ScheduleChildSubagentsTool 工具规格已定义。"""
        from agent_py_agent.agent.agent_core.orchestration_tools import ScheduleChildSubagentsTool

        mock_agent = MagicMock()

        mock_agent.capability_router = CapabilityRouter()
        tool = ScheduleChildSubagentsTool(mock_agent)
        spec = tool.model_spec

        assert spec.name == "schedule_child_subagents"
        assert spec.category == "orchestration"
        assert "当前子代理" in spec.description
        assert "tester" in spec.parameter_descriptions["children"]
        assert "runner" not in spec.description

    def test_orchestration_specs_do_not_expose_role_template_paths(self):
        from agent_py_agent.agent.agent_core.orchestration_tools import (
            CreateSubagentsTool,
            ScheduleChildSubagentsTool,
        )

        mock_agent = MagicMock()

        mock_agent.capability_router = CapabilityRouter()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10
        store = load_role_template_store()

        for tool_cls in (CreateSubagentsTool, ScheduleChildSubagentsTool):
            spec = tool_cls(mock_agent).model_spec
            blob = str(spec.parameter_descriptions) + str(spec.examples)
            assert "模板位置" not in blob
            for template in store.all():
                assert template.source_path not in blob

    def test_orchestration_model_spec_stays_compact(self):
        from agent_py_agent.agent.agent_core.orchestration import tool_spec_data

        source_lines = Path(tool_spec_data.__file__).read_text().splitlines()
        assert len(source_lines) <= 120
