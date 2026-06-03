"""subagents/manager_base.py 单元测试。

测试基类方法、任务加载保存、目录管理。
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestFieldNames:
    """测试 _field_names() 函数。"""

    def test_returns_field_names(self):
        """验证返回数据类的字段名集合。"""
        from agent_py_agent.agent.subagents.manager_base import _field_names
        from agent_py_agent.agent.subagents.models import QualityContract

        result = _field_names(QualityContract)
        assert isinstance(result, set)
        assert "failure_conditions" in result
        assert "evidence_required" in result


class TestListValue:
    """测试 _list_value() 函数。"""

    def test_none_returns_empty_list(self):
        """None 返回空列表。"""
        from agent_py_agent.agent.subagents.manager_base import _list_value

        assert _list_value(None) == []

    def test_list_returns_same(self):
        """列表原样返回。"""
        from agent_py_agent.agent.subagents.manager_base import _list_value

        assert _list_value([1, 2, 3]) == [1, 2, 3]

    def test_tuple_converted_to_list(self):
        """元组转列表。"""
        from agent_py_agent.agent.subagents.manager_base import _list_value

        assert _list_value((1, 2, 3)) == [1, 2, 3]

    def test_other_wraps_in_list(self):
        """其它类型包装成单元素列表。"""
        from agent_py_agent.agent.subagents.manager_base import _list_value

        assert _list_value("hello") == ["hello"]
        assert _list_value(42) == [42]


class TestStringListValue:
    """测试 _string_list_value() 函数。"""

    def test_filters_none_and_empty(self):
        """过滤 None 和空字符串。"""
        from agent_py_agent.agent.subagents.manager_base import _string_list_value

        assert _string_list_value([None, "", "valid"]) == ["valid"]

    def test_converts_to_strings(self):
        """转换为字符串列表。"""
        from agent_py_agent.agent.subagents.manager_base import _string_list_value

        assert _string_list_value([1, 2, 3]) == ["1", "2", "3"]


class TestNormalizeQualityContract:
    """测试 _normalize_quality_contract() 函数。"""

    def test_returns_same_if_quality_contract(self):
        """已是 QualityContract 时直接返回。"""
        from agent_py_agent.agent.subagents.manager_base import _normalize_quality_contract
        from agent_py_agent.agent.subagents.models import QualityContract

        original = QualityContract()
        result = _normalize_quality_contract(original)
        assert result is original

    def test_returns_default_for_non_dict(self):
        """非字典返回默认实例。"""
        from agent_py_agent.agent.subagents.manager_base import _normalize_quality_contract

        result = _normalize_quality_contract("invalid")
        assert result.failure_conditions == []

    def test_normalizes_dict_to_quality_contract(self):
        """字典规范化为 QualityContract。"""
        from agent_py_agent.agent.subagents.manager_base import _normalize_quality_contract

        data = {
            "failure_conditions": ["cond1", "cond2"],
            "evidence_required": ["ev1"],
        }
        result = _normalize_quality_contract(data)
        assert result.evidence_required == ["ev1"]


class TestNormalizeContextManifest:
    """测试 _normalize_context_manifest() 函数。"""

    def test_returns_same_if_context_manifest(self):
        """已是 ContextManifest 时直接返回。"""
        from agent_py_agent.agent.subagents.manager_base import _normalize_context_manifest
        from agent_py_agent.agent.subagents.models import ContextManifest

        original = ContextManifest()
        result = _normalize_context_manifest(original)
        assert result is original

    def test_normalizes_token_budget_to_int(self):
        """token_budget 转换为整数。"""
        from agent_py_agent.agent.subagents.manager_base import _normalize_context_manifest

        data = {"token_budget": "1000"}
        result = _normalize_context_manifest(data)
        assert result.token_budget == 1000


class TestNormalizeContextPacks:
    """测试 _normalize_context_packs() 函数。"""

    def test_single_dict_wraps_in_list(self):
        """单个字典包装成列表。"""
        from agent_py_agent.agent.subagents.manager_base import _normalize_context_packs

        assert _normalize_context_packs({"key": "value"}) == [{"key": "value"}]

    def test_list_passes_through(self):
        """列表原样返回。"""
        from agent_py_agent.agent.subagents.manager_base import _normalize_context_packs

        data = [{"key": "value1"}, {"key": "value2"}]
        assert _normalize_context_packs(data) == data

    def test_filters_non_dict_items(self):
        """过滤非字典元素。"""
        from agent_py_agent.agent.subagents.manager_base import _normalize_context_packs

        data = [{"key": "value"}, "invalid", None, 123]
        result = _normalize_context_packs(data)
        assert len(result) == 1


class TestNoNaturalWriteDirExtraction:
    """确认 manager_base 不再从自然语言目标里抽写入根。"""

    def test_extract_write_dirs_api_removed(self):
        """写入根必须来自 extra_write_roots/workspace/refs，不再有自然语言兜底 API。"""
        import agent_py_agent.agent.subagents.manager_base as manager_base

        assert not hasattr(manager_base, "_extract_write_dirs")


class TestBuildWorkOrderPaths:
    """测试 _build_work_order_paths() 函数。"""

    def test_creates_standard_paths(self, tmp_path: Path):
        """验证生成标准工单路径。"""
        from agent_py_agent.agent.subagents.manager_base import SubAgentBaseMixin

        mixin = SubAgentBaseMixin(workspace=tmp_path)
        paths = mixin._build_work_order_paths("run_123")

        assert paths["task_dir"] == str(tmp_path / "run_123")
        assert paths["data_dir"] == str(tmp_path / "run_123" / "data")
        assert paths["logs_dir"] == str(tmp_path / "run_123" / "logs")
        assert "allowed_write_roots" in paths
        assert "forbidden_write_roots" in paths

    def test_with_extra_write_roots(self, tmp_path: Path):
        """验证包含额外写入目录。"""
        from agent_py_agent.agent.subagents.manager_base import SubAgentBaseMixin

        mixin = SubAgentBaseMixin(workspace=tmp_path)
        paths = mixin._build_work_order_paths("run_456", extra_write_roots=["/tmp/extra"])

        assert str(tmp_path / "run_456") in paths["allowed_write_roots"]
        assert "/tmp/extra" in paths["allowed_write_roots"]


class TestEnsureWorkOrderFiles:
    """测试 _ensure_work_order_files() 方法。"""

    def test_creates_directories(self, tmp_path: Path):
        """验证创建必要目录。"""
        from agent_py_agent.agent.subagents.manager_base import SubAgentBaseMixin
        from agent_py_agent.agent.subagents.models import SubAgentTask

        mixin = SubAgentBaseMixin(workspace=tmp_path)
        task = SubAgentTask(
            id="test_run",
            goal="测试任务",
            thought="思考",
            plan=["步骤1"],
            agent_name="test",
            created_at=1234567890.0,
            updated_at=1234567890.0,
            **mixin._build_work_order_paths("test_run"),
        )

        mixin._ensure_work_order_files(task)

        assert Path(task.data_dir).exists()
        assert Path(task.output_dir).exists()
        assert Path(task.logs_dir).exists()

    def test_creates_minimal_files(self, tmp_path: Path):
        """验证创建最小文件集合。"""
        from agent_py_agent.agent.subagents.manager_base import SubAgentBaseMixin
        from agent_py_agent.agent.subagents.models import SubAgentTask

        mixin = SubAgentBaseMixin(workspace=tmp_path)
        task = SubAgentTask(
            id="test_run_files",
            goal="测试任务",
            thought="思考",
            plan=["步骤1"],
            agent_name="test",
            created_at=1234567890.0,
            updated_at=1234567890.0,
            **mixin._build_work_order_paths("test_run_files"),
        )

        mixin._ensure_work_order_files(task)

        assert Path(task.status_file).exists()
        assert Path(task.work_log_file).exists()
        assert Path(task.output_json).exists()

    def test_creates_machine_readable_default_json_files(self, tmp_path: Path):
        from agent_py_agent.agent.subagents.manager_base import SubAgentBaseMixin
        from agent_py_agent.agent.subagents.models import SubAgentTask

        mixin = SubAgentBaseMixin(workspace=tmp_path)
        task = SubAgentTask(
            id="test_json_files",
            goal="测试任务",
            thought="思考",
            plan=["步骤1"],
            agent_name="test",
            created_at=1234567890.0,
            updated_at=1234567890.0,
            **mixin._build_work_order_paths("test_json_files"),
        )

        mixin._ensure_work_order_files(task)

        output = json.loads(Path(task.output_json).read_text(encoding="utf-8"))
        status = json.loads(Path(task.status_report_json).read_text(encoding="utf-8"))
        deps = json.loads(Path(task.dependencies_json).read_text(encoding="utf-8"))
        assert isinstance(output, dict)
        assert output["run_id"] == "test_json_files"
        assert isinstance(status, dict)
        assert status["run_id"] == "test_json_files"
        assert isinstance(deps, dict)
        assert deps["dependencies"] == []
