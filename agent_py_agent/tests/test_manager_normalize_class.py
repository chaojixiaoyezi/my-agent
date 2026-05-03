"""subagents/manager_normalize.py 单元测试。

测试数据归一化、字段校验、默认值填充。
"""
from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest


class TestFieldNames:
    """测试 _field_names() 函数。"""

    def test_returns_field_names_set(self):
        """验证返回字段名集合。"""
        from agent_py_agent.agent.subagents.manager_normalize import _field_names
        from agent_py_agent.agent.subagents.models import QualityContract

        result = _field_names(QualityContract)
        assert isinstance(result, set)


class TestListValue:
    """测试 _list_value() 函数。"""

    def test_none_returns_empty_list(self):
        """None 返回空列表。"""
        from agent_py_agent.agent.subagents.manager_normalize import _list_value

        assert _list_value(None) == []

    def test_list_unchanged(self):
        """列表保持不变。"""
        from agent_py_agent.agent.subagents.manager_normalize import _list_value

        assert _list_value([1, 2, 3]) == [1, 2, 3]

    def test_tuple_converted_to_list(self):
        """元组转换为列表。"""
        from agent_py_agent.agent.subagents.manager_normalize import _list_value

        assert _list_value((1, 2)) == [1, 2]

    def test_other_values_wrapped(self):
        """其它值包装成单元素列表。"""
        from agent_py_agent.agent.subagents.manager_normalize import _list_value

        assert _list_value("string") == ["string"]
        assert _list_value(42) == [42]


class TestStringListValue:
    """测试 _string_list_value() 函数。"""

    def test_filters_none_and_empty(self):
        """过滤 None 和空字符串。"""
        from agent_py_agent.agent.subagents.manager_normalize import _string_list_value

        result = _string_list_value([None, "", "valid"])
        assert result == ["valid"]

    def test_converts_to_strings(self):
        """转换为字符串。"""
        from agent_py_agent.agent.subagents.manager_normalize import _string_list_value

        result = _string_list_value([1, 2, 3])
        assert result == ["1", "2", "3"]


class TestNormalizeQualityContract:
    """测试 _normalize_quality_contract() 函数。"""

    def test_returns_same_instance(self):
        """已是 QualityContract 直接返回。"""
        from agent_py_agent.agent.subagents.manager_normalize import _normalize_quality_contract
        from agent_py_agent.agent.subagents.models import QualityContract

        original = QualityContract()
        result = _normalize_quality_contract(original)
        assert result is original

    def test_returns_default_for_non_dict(self):
        """非字典返回默认实例。"""
        from agent_py_agent.agent.subagents.manager_normalize import _normalize_quality_contract
        from agent_py_agent.agent.subagents.models import QualityContract

        result = _normalize_quality_contract(123)
        assert isinstance(result, QualityContract)

    def test_normalizes_dict_fields(self):
        """规范化和过滤字典字段。"""
        from agent_py_agent.agent.subagents.manager_normalize import _normalize_quality_contract

        data = {
            "failure_conditions": ["cond1", "cond2"],
            "unknown_field": "should be ignored",
            "cannot_self_accept": False,
        }
        result = _normalize_quality_contract(data)

        assert result.failure_conditions == ["cond1", "cond2"]
        assert result.cannot_self_accept is False


class TestNormalizeContextManifest:
    """测试 _normalize_context_manifest() 函数。"""

    def test_returns_same_instance(self):
        """已是 ContextManifest 直接返回。"""
        from agent_py_agent.agent.subagents.manager_normalize import _normalize_context_manifest
        from agent_py_agent.agent.subagents.models import ContextManifest

        original = ContextManifest()
        result = _normalize_context_manifest(original)
        assert result is original

    def test_returns_default_for_non_dict(self):
        """非字典返回默认实例。"""
        from agent_py_agent.agent.subagents.manager_normalize import _normalize_context_manifest
        from agent_py_agent.agent.subagents.models import ContextManifest

        result = _normalize_context_manifest("invalid")
        assert isinstance(result, ContextManifest)

    def test_token_budget_to_int(self):
        """token_budget 转换为整数。"""
        from agent_py_agent.agent.subagents.manager_normalize import _normalize_context_manifest

        result = _normalize_context_manifest({"token_budget": "5000"})
        assert result.token_budget == 5000


class TestNormalizeContextPacks:
    """测试 _normalize_context_packs() 函数。"""

    def test_dict_wrapped_in_list(self):
        """字典包装成列表。"""
        from agent_py_agent.agent.subagents.manager_normalize import _normalize_context_packs

        result = _normalize_context_packs({"key": "value"})
        assert result == [{"key": "value"}]

    def test_list_passes_through(self):
        """列表原样返回。"""
        from agent_py_agent.agent.subagents.manager_normalize import _normalize_context_packs

        data = [{"key": "val1"}, {"key": "val2"}]
        result = _normalize_context_packs(data)
        assert len(result) == 2

    def test_non_dict_filtered(self):
        """非字典元素被过滤。"""
        from agent_py_agent.agent.subagents.manager_normalize import _normalize_context_packs

        result = _normalize_context_packs([{"key": "val"}, "string", None, 123])
        assert len(result) == 1


class TestExtractWriteDirs:
    """测试 _extract_write_dirs() 函数。"""

    def test_extracts_absolute_paths(self):
        """提取绝对路径。"""
        from agent_py_agent.agent.subagents.manager_normalize import _extract_write_dirs

        goal = "在 /Users/test/project 和 /tmp/output 创建文件"
        result = _extract_write_dirs(goal)

        assert "/Users/test/project" in result
        assert "/tmp/output" in result

    def test_extracts_home_paths(self):
        """提取 home 目录路径。"""
        from agent_py_agent.agent.subagents.manager_normalize import _extract_write_dirs

        goal = "在 ~/Documents 目录下操作"
        result = _extract_write_dirs(goal)

        assert "~/Documents" in result

    def test_deduplicates(self):
        """去重。"""
        from agent_py_agent.agent.subagents.manager_normalize import _extract_write_dirs

        goal = "/tmp/dir /tmp/dir /tmp/dir"
        result = _extract_write_dirs(goal)

        assert len(result) == 1

    def test_empty_goal(self):
        """空目标返回空列表。"""
        from agent_py_agent.agent.subagents.manager_normalize import _extract_write_dirs

        result = _extract_write_dirs("")
        assert result == []


class TestWorkflowModeNormalization:
    """测试 manager_base.py 中的工作流模式相关函数。"""

    def test_normalize_workflow_mode_value_off(self):
        """off 模式。"""
        from agent_py_agent.agent.subagents.manager_base import _normalize_workflow_mode_value

        assert _normalize_workflow_mode_value("off") == "off"

    def test_normalize_workflow_mode_value_plan(self):
        """plan 模式。"""
        from agent_py_agent.agent.subagents.manager_base import _normalize_workflow_mode_value

        assert _normalize_workflow_mode_value("plan") == "plan"

    def test_normalize_workflow_mode_value_auto(self):
        """auto 模式。"""
        from agent_py_agent.agent.subagents.manager_base import _normalize_workflow_mode_value

        assert _normalize_workflow_mode_value("auto") == "auto"

    def test_normalize_workflow_mode_invalid(self):
        """无效值默认 off。"""
        from agent_py_agent.agent.subagents.manager_base import _normalize_workflow_mode_value

        assert _normalize_workflow_mode_value("invalid") == "off"
        assert _normalize_workflow_mode_value(None) == "off"


class TestWorkflowWorkerTools:
    """测试 manager_base.py 中的 _workflow_worker_tools() 函数。"""

    def test_returns_parent_tools_if_provided(self):
        """有父工具时使用父工具。"""
        from agent_py_agent.agent.subagents.manager_base import _workflow_worker_tools

        result = _workflow_worker_tools(["custom_tool"], "worker")
        assert result == ["custom_tool"]

    def test_read_only_for_review_kind(self):
        """review 类型返回只读工具。"""
        from agent_py_agent.agent.subagents.manager_base import _workflow_worker_tools

        result = _workflow_worker_tools([], "review")
        assert "read_file" in result
        assert "write_file" not in result

    def test_coding_for_worker_kind(self):
        """worker 类型返回编码工具。"""
        from agent_py_agent.agent.subagents.manager_base import _workflow_worker_tools

        result = _workflow_worker_tools([], "worker")
        assert "write_file" in result
        assert "read_file" in result


class TestWorkflowModes:
    """测试 manager_base.py 中的工作流模式常量。"""

    def test_workflow_modes_defined(self):
        """验证工作流模式已定义。"""
        from agent_py_agent.agent.subagents.manager_base import _WORKFLOW_MODES

        assert "off" in _WORKFLOW_MODES
        assert "plan" in _WORKFLOW_MODES
        assert "auto" in _WORKFLOW_MODES


class TestReadOnlySubagentTools:
    """测试 manager_base.py 中的只读工具常量。"""

    def test_read_only_tools_defined(self):
        """验证只读工具列表已定义。"""
        from agent_py_agent.agent.subagents.manager_base import _READ_ONLY_SUBAGENT_TOOLS

        assert "list_files" in _READ_ONLY_SUBAGENT_TOOLS
        assert "read_file" in _READ_ONLY_SUBAGENT_TOOLS
        assert "search_text" in _READ_ONLY_SUBAGENT_TOOLS


class TestCodingSubagentTools:
    """测试 manager_base.py 中的编码工具常量。"""

    def test_coding_tools_defined(self):
        """验证编码工具列表已定义。"""
        from agent_py_agent.agent.subagents.manager_base import _CODING_SUBAGENT_TOOLS

        assert "write_file" in _CODING_SUBAGENT_TOOLS
        assert "replace_in_file" in _CODING_SUBAGENT_TOOLS
        assert "append_file" in _CODING_SUBAGENT_TOOLS