"""subagents/services/persistence/model_normalizers.py 单元测试。

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
        from agent_py_agent.agent.subagents.models import QualityContract
        from agent_py_agent.agent.subagents.services.persistence.model_normalizers import (
            _field_names,
        )

        result = _field_names(QualityContract)
        assert isinstance(result, set)


class TestListValue:
    """测试 _list_value() 函数。"""

    def test_none_returns_empty_list(self):
        """None 返回空列表。"""
        from agent_py_agent.agent.subagents.services.persistence.model_normalizers import (
            _list_value,
        )

        assert _list_value(None) == []

    def test_list_unchanged(self):
        """列表保持不变。"""
        from agent_py_agent.agent.subagents.services.persistence.model_normalizers import (
            _list_value,
        )

        assert _list_value([1, 2, 3]) == [1, 2, 3]

    def test_tuple_converted_to_list(self):
        """元组转换为列表。"""
        from agent_py_agent.agent.subagents.services.persistence.model_normalizers import (
            _list_value,
        )

        assert _list_value((1, 2)) == [1, 2]

    def test_other_values_wrapped(self):
        """其它值包装成单元素列表。"""
        from agent_py_agent.agent.subagents.services.persistence.model_normalizers import (
            _list_value,
        )

        assert _list_value("string") == ["string"]
        assert _list_value(42) == [42]


class TestStringListValue:
    """测试 _string_list_value() 函数。"""

    def test_filters_none_and_empty(self):
        """过滤 None 和空字符串。"""
        from agent_py_agent.agent.subagents.services.persistence.model_normalizers import (
            _string_list_value,
        )

        result = _string_list_value([None, "", "valid"])
        assert result == ["valid"]

    def test_converts_to_strings(self):
        """转换为字符串。"""
        from agent_py_agent.agent.subagents.services.persistence.model_normalizers import (
            _string_list_value,
        )

        result = _string_list_value([1, 2, 3])
        assert result == ["1", "2", "3"]


class TestNormalizeQualityContract:
    """测试 _normalize_quality_contract() 函数。"""

    def test_returns_same_instance(self):
        """已是 QualityContract 直接返回。"""
        from agent_py_agent.agent.subagents.models import QualityContract
        from agent_py_agent.agent.subagents.services.persistence.model_normalizers import (
            _normalize_quality_contract,
        )

        original = QualityContract()
        result = _normalize_quality_contract(original)
        assert result is original

    def test_returns_default_for_non_dict(self):
        """非字典返回默认实例。"""
        from agent_py_agent.agent.subagents.models import QualityContract
        from agent_py_agent.agent.subagents.services.persistence.model_normalizers import (
            _normalize_quality_contract,
        )

        result = _normalize_quality_contract(123)
        assert isinstance(result, QualityContract)

    def test_normalizes_dict_fields(self):
        """规范化和过滤字典字段。"""
        from agent_py_agent.agent.subagents.services.persistence.model_normalizers import (
            _normalize_quality_contract,
        )

        data = {
            "failure_conditions": ["cond1", "cond2"],
            "unknown_field": "should be ignored",
            "evidence_required": ["ev1"],
        }
        result = _normalize_quality_contract(data)

        assert result.failure_conditions == ["cond1", "cond2"]
        assert result.evidence_required == ["ev1"]


class TestNormalizeContextManifest:
    """测试 _normalize_context_manifest() 函数。"""

    def test_returns_same_instance(self):
        """已是 ContextManifest 直接返回。"""
        from agent_py_agent.agent.subagents.models import ContextManifest
        from agent_py_agent.agent.subagents.services.persistence.model_normalizers import (
            _normalize_context_manifest,
        )

        original = ContextManifest()
        result = _normalize_context_manifest(original)
        assert result is original

    def test_returns_default_for_non_dict(self):
        """非字典返回默认实例。"""
        from agent_py_agent.agent.subagents.models import ContextManifest
        from agent_py_agent.agent.subagents.services.persistence.model_normalizers import (
            _normalize_context_manifest,
        )

        result = _normalize_context_manifest("invalid")
        assert isinstance(result, ContextManifest)

    def test_token_budget_to_int(self):
        """token_budget 转换为整数。"""
        from agent_py_agent.agent.subagents.services.persistence.model_normalizers import (
            _normalize_context_manifest,
        )

        result = _normalize_context_manifest({"token_budget": "5000"})
        assert result.token_budget == 5000


class TestNormalizeContextPacks:
    """测试 _normalize_context_packs() 函数。"""

    def test_dict_wrapped_in_list(self):
        """字典包装成列表。"""
        from agent_py_agent.agent.subagents.services.persistence.model_normalizers import (
            _normalize_context_packs,
        )

        result = _normalize_context_packs({"key": "value"})
        assert result == [{"key": "value"}]

    def test_list_passes_through(self):
        """列表原样返回。"""
        from agent_py_agent.agent.subagents.services.persistence.model_normalizers import (
            _normalize_context_packs,
        )

        data = [{"key": "val1"}, {"key": "val2"}]
        result = _normalize_context_packs(data)
        assert len(result) == 2

    def test_non_dict_filtered(self):
        """非字典元素被过滤。"""
        from agent_py_agent.agent.subagents.services.persistence.model_normalizers import (
            _normalize_context_packs,
        )

        result = _normalize_context_packs([{"key": "val"}, "string", None, 123])
        assert len(result) == 1


class TestNoNaturalWriteDirExtraction:
    """确认当前 normalization 模块不再从自然语言目标里抽写入根。"""

    def test_extract_write_dirs_api_removed(self):
        """写入根必须来自 extra_write_roots/workspace/refs，不再有自然语言兜底 API。"""
        import agent_py_agent.agent.subagents.services.persistence.model_normalizers as model_normalizers

        assert not hasattr(model_normalizers, "_extract_write_dirs")


class TestReadOnlySubagentTools:
    """测试 manager workflow service 中的只读工具常量。"""

    def test_read_only_tools_defined(self):
        """验证只读工具列表已定义。"""
        from agent_py_agent.agent.agent_core.orchestration.tool_grants import (
            READ_ONLY_SUBAGENT_TOOLS,
        )

        assert "list_files" in READ_ONLY_SUBAGENT_TOOLS
        assert "read_file" in READ_ONLY_SUBAGENT_TOOLS
        assert "search_text" in READ_ONLY_SUBAGENT_TOOLS
        assert "write_file" not in READ_ONLY_SUBAGENT_TOOLS
        assert "apply_patch" not in READ_ONLY_SUBAGENT_TOOLS
        assert "run_command" not in READ_ONLY_SUBAGENT_TOOLS


class TestCodingSubagentTools:
    """测试 manager workflow service 中的编码工具常量。"""

    def test_coding_tools_defined(self):
        """验证编码工具列表已定义。"""
        from agent_py_agent.agent.agent_core.orchestration.tool_grants import CODING_SUBAGENT_TOOLS

        assert "write_file" in CODING_SUBAGENT_TOOLS
        assert "apply_patch" in CODING_SUBAGENT_TOOLS
        assert "apply_patch" in CODING_SUBAGENT_TOOLS
