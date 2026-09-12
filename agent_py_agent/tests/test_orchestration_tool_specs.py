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
        # 2026-09-10「精简冲突提示」把工具级规则从逐参数文本挪到工具 description，
        # 所以断言要跟着搬到规则真正所在的位置，而不是继续要求参数文本里出现过这些句子。
        assert "资料线索" in spec.parameter_descriptions["input_refs"]
        assert "goal 里写‘先 A 后 B’不会形成执行顺序" in spec.description
        assert "等 A 的生命周期完成事件自动唤醒后" in spec.description
        assert "依赖顺序" in spec.parameter_descriptions["items"]
        assert "已有 Todo 时每项必须" not in spec.parameter_descriptions["items"]
        # 同样是一句话的措辞精简：语义保留（不得拿无关 id 顶替），句子改为"不拿无关 id 顶替"。
        assert "不拿无关 id 顶替" in spec.parameter_descriptions["covers"]
        assert "不是权限、完整写集或创建前置条件" in spec.parameter_descriptions["output_files"]
        assert "extra_write_roots" not in spec.parameter_descriptions["items"]
        assert "context_manifest" not in spec.parameter_descriptions
        assert "context_packs" not in spec.parameter_descriptions
        assert "output_refs" not in spec.parameter_descriptions
        item_properties = spec.input_schema["properties"]["items"]["items"]["properties"]
        assert "职责短标题" in item_properties["description"]["description"]
        assert "不要复制顶层" in item_properties["description"]["description"]
        assert "每个 item" in item_properties["goal"]["description"]

    def test_orchestration_specs_do_not_expose_role_template_paths(self):
        from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool

        mock_agent = MagicMock()

        mock_agent.capability_router = CapabilityRouter()
        mock_agent.config.enable_subagents = True
        mock_agent.config.max_subagents = 10
        store = load_role_template_store()

        spec = CreateSubagentsTool(mock_agent).model_spec
        blob = str(spec.parameter_descriptions) + str(spec.examples)
        assert "模板位置" not in blob
        for template in store.all():
            assert template.source_path not in blob

    def test_orchestration_model_spec_stays_compact(self):
        from agent_py_agent.agent.agent_core.orchestration import tool_spec_data

        source_lines = Path(tool_spec_data.__file__).read_text().splitlines()
        assert len(source_lines) <= 120
