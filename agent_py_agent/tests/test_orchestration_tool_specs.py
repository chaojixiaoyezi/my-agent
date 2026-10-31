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
        # 7101c9fa2 起顶层只剩 items/batch_summary，逐项字段说明只挂在 items[].properties。
        assert set(spec.parameter_descriptions) == {"items", "batch_summary"}
        item_properties = spec.input_schema["properties"]["items"]["items"]["properties"]
        assert "bug_finder" in item_properties["role"]["description"]
        assert "writer" in item_properties["role"]["description"]
        # 2026-09-10「精简冲突提示」把工具级规则从逐参数文本挪到工具 description，
        # 所以断言要跟着搬到规则真正所在的位置，而不是继续要求参数文本里出现过这些句子。
        assert "资料线索" in item_properties["input_refs"]["description"]
        assert "所有items立即并发，文字中的先后顺序不生效" in spec.description
        assert "创建后生命周期事件自动通知父级，无需轮询" in spec.description
        assert "依赖前项结果或可能修改同一文件/模块的任务须分批" in spec.description
        assert "一项也用数组" in spec.parameter_descriptions["items"]
        assert "已有 Todo 时每项必须" not in spec.parameter_descriptions["items"]
        # 同样是一句话的措辞精简：语义保留（不得拿无关 id 顶替），句子改为"不拿无关 id 顶替"。
        assert "不拿无关 id 顶替" in item_properties["covers"]["description"]
        assert "不是权限、完整写集或创建前置条件" in item_properties["output_files"]["description"]
        assert "extra_write_roots" not in spec.parameter_descriptions["items"]
        assert "extra_write_roots" not in item_properties
        for retired in ("context_manifest", "context_packs", "output_refs"):
            assert retired not in spec.parameter_descriptions
            assert retired not in item_properties
        assert "职责短标题" in item_properties["description"]["description"]
        assert "不要复制整批说明" in item_properties["description"]["description"]
        assert "每个item都必填" in item_properties["goal"]["description"]

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
