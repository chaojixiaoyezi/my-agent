"""日志工具类测试 - tool_classes.py BaseTool 子类、参数过滤、工具规格。"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestRequiredText:
    """_required_text 函数测试。"""

    def test_returns_value(self):
        """有效值直接返回。"""
        from agent_py_agent.agent.log_analysis.tools.tool_classes import _required_text

        result = _required_text({"key": "value"}, "key")
        assert result == "value"

    def test_returns_string_coerced(self):
        """非字符串被转成字符串。"""
        from agent_py_agent.agent.log_analysis.tools.tool_classes import _required_text

        result = _required_text({"key": 42}, "key")
        assert result == "42"

    def test_missing_raises(self):
        """缺失字段抛出 ValueError。"""
        from agent_py_agent.agent.log_analysis.tools.tool_classes import _required_text

        with pytest.raises(ValueError, match="ip is required"):
            _required_text({}, "ip")

    def test_empty_string_raises(self):
        """空字符串抛出 ValueError。"""
        from agent_py_agent.agent.log_analysis.tools.tool_classes import _required_text

        with pytest.raises(ValueError, match="case_id is required"):
            _required_text({"case_id": ""}, "case_id")

    def test_none_raises(self):
        """None 值抛出 ValueError。"""
        from agent_py_agent.agent.log_analysis.tools.tool_classes import _required_text

        with pytest.raises(ValueError, match="field is required"):
            _required_text({"field": None}, "field")


class TestPromptJson:
    """_prompt_json 函数测试。"""

    def test_formats_json(self):
        """格式化 JSON 输出。"""
        from agent_py_agent.agent.log_analysis.tools.tool_classes import _prompt_json

        result = _prompt_json({"key": "value"})
        parsed = json.loads(result)
        assert parsed["key"] == "value"

    def test_sorts_keys(self):
        """键被排序。"""
        from agent_py_agent.agent.log_analysis.tools.tool_classes import _prompt_json

        result = _prompt_json({"z": 1, "a": 2})
        assert result.find('"a"') < result.find('"z"')

    def test_preserves_unicode(self):
        """保留 Unicode 字符。"""
        from agent_py_agent.agent.log_analysis.tools.tool_classes import _prompt_json

        result = _prompt_json({"name": "你好", "emoji": "🎉"})
        assert "你好" in result
        assert "🎉" in result


class TestQueryParams:
    """_query_params 函数测试。"""

    def test_extracts_valid_keys(self):
        """提取有效参数键。"""
        from agent_py_agent.agent.log_analysis.tools.tool_classes import _query_params

        params = {
            "attacker_ip": "1.2.3.4",
            "victim_ip": "5.6.7.8",
            "domain": "evil.com",
            "uri": "/api",
            "alert_type": "SQL注入",
            "start_time": "2024-01-01T00:00:00Z",
            "end_time": "2024-01-02T00:00:00Z",
            "limit": 50,
            "max_limit": 100,
            "extra_key": "ignored",
        }
        result = _query_params(params)
        assert result["attacker_ip"] == "1.2.3.4"
        assert result["victim_ip"] == "5.6.7.8"
        assert "extra_key" not in result

    def test_empty_params(self):
        """空参数返回空字典。"""
        from agent_py_agent.agent.log_analysis.tools.tool_classes import _query_params

        result = _query_params({})
        assert result == {}


class TestHuntParams:
    """_hunt_params 函数测试。"""

    def test_extracts_hunt_keys(self):
        """提取 IP hunt 相关参数。"""
        from agent_py_agent.agent.log_analysis.tools.tool_classes import _hunt_params

        params = {
            "role": "attacker",
            "start_time": "2024-01-01T00:00:00Z",
            "end_time": "2024-01-02T00:00:00Z",
            "limit": 25,
            "max_limit": 50,
            "attacker_ip": "should_be_ignored",
        }
        result = _hunt_params(params)
        assert result["role"] == "attacker"
        assert "attacker_ip" not in result


class TestTraceParams:
    """_trace_params 函数测试。"""

    def test_extracts_trace_keys(self):
        """提取 case trace 相关参数。"""
        from agent_py_agent.agent.log_analysis.tools.tool_classes import _trace_params

        params = {
            "start_time": "2024-01-01T00:00:00Z",
            "end_time": "2024-01-02T00:00:00Z",
            "limit": 25,
            "max_limit": 50,
            "max_queries": 10,
            "case_id": "should_be_ignored",
        }
        result = _trace_params(params)
        assert result["max_queries"] == 10
        assert "case_id" not in result


class TestSecurityQueryTool:
    """SecurityQueryTool 类测试。"""

    def test_init_sets_store_root(self, tmp_path: Path):
        """初始化设置 store_root。"""
        from agent_py_agent.agent.log_analysis.tools.tool_classes import SecurityQueryTool

        tool = SecurityQueryTool(tmp_path)
        assert tool.store_root == tmp_path

    def test_spec_has_correct_name(self, tmp_path: Path):
        """ToolSpec 有正确名称。"""
        from agent_py_agent.agent.log_analysis.tools.tool_classes import SecurityQueryTool

        tool = SecurityQueryTool(tmp_path)
        assert tool.spec.name == "security_query"
        assert tool.spec.category == "log_analysis"

    def test_spec_has_keywords(self, tmp_path: Path):
        """ToolSpec 包含关键词。"""
        from agent_py_agent.agent.log_analysis.tools.tool_classes import SecurityQueryTool

        tool = SecurityQueryTool(tmp_path)
        assert "log" in tool.spec.keywords
        assert "security" in tool.spec.keywords
        assert "query" in tool.spec.keywords

    def test_execute_returns_result(self, tmp_path: Path):
        """execute 返回 ToolExecutionResult。"""
        from agent_py_agent.agent.log_analysis.tools.tool_classes import SecurityQueryTool

        tool = SecurityQueryTool(tmp_path)
        with patch("agent_py_agent.agent.log_analysis.tools.tool_classes.security_query") as mock_q:
            mock_q.return_value = {"tool": "security_query", "row_count": 0}
            result = tool.execute({})
            assert result.ok is True
            assert "security_query" in result.output


class TestSecurityHuntIpTool:
    """SecurityHuntIpTool 类测试。"""

    def test_init_sets_store_root(self, tmp_path: Path):
        """初始化设置 store_root。"""
        from agent_py_agent.agent.log_analysis.tools.tool_classes import SecurityHuntIpTool

        tool = SecurityHuntIpTool(tmp_path)
        assert tool.store_root == tmp_path

    def test_spec_name(self, tmp_path: Path):
        """ToolSpec 名称正确。"""
        from agent_py_agent.agent.log_analysis.tools.tool_classes import SecurityHuntIpTool

        tool = SecurityHuntIpTool(tmp_path)
        assert tool.spec.name == "security_hunt_ip"

    def test_execute_requires_ip(self, tmp_path: Path):
        """execute 缺少 ip 时报错。"""
        from agent_py_agent.agent.log_analysis.tools.tool_classes import SecurityHuntIpTool

        tool = SecurityHuntIpTool(tmp_path)
        with pytest.raises(ValueError, match="ip is required"):
            tool.execute({})

    def test_execute_with_ip(self, tmp_path: Path):
        """有 ip 时正常执行。"""
        from agent_py_agent.agent.log_analysis.tools.tool_classes import SecurityHuntIpTool

        tool = SecurityHuntIpTool(tmp_path)
        with patch("agent_py_agent.agent.log_analysis.tools.tool_classes.security_hunt_ip") as mock_hunt:
            mock_hunt.return_value = {"tool": "security_hunt_ip", "row_count": 0}
            result = tool.execute({"ip": "1.2.3.4"})
            assert result.ok is True


class TestSecurityTraceCaseTool:
    """SecurityTraceCaseTool 类测试。"""

    def test_init_sets_store_root(self, tmp_path: Path):
        """初始化设置 store_root。"""
        from agent_py_agent.agent.log_analysis.tools.tool_classes import SecurityTraceCaseTool

        tool = SecurityTraceCaseTool(tmp_path)
        assert tool.store_root == tmp_path

    def test_spec_name(self, tmp_path: Path):
        """ToolSpec 名称正确。"""
        from agent_py_agent.agent.log_analysis.tools.tool_classes import SecurityTraceCaseTool

        tool = SecurityTraceCaseTool(tmp_path)
        assert tool.spec.name == "security_trace_case"

    def test_execute_requires_case_id(self, tmp_path: Path):
        """execute 缺少 case_id 时报错。"""
        from agent_py_agent.agent.log_analysis.tools.tool_classes import SecurityTraceCaseTool

        tool = SecurityTraceCaseTool(tmp_path)
        with pytest.raises(ValueError, match="case_id is required"):
            tool.execute({})

    def test_execute_with_case_id(self, tmp_path: Path):
        """有 case_id 时正常执行。"""
        from agent_py_agent.agent.log_analysis.tools.tool_classes import SecurityTraceCaseTool

        tool = SecurityTraceCaseTool(tmp_path)
        with patch("agent_py_agent.agent.log_analysis.tools.tool_classes.security_trace_case") as mock_trace:
            mock_trace.return_value = {"tool": "security_trace_case", "row_count": 0}
            result = tool.execute({"case_id": "case-1"})
            assert result.ok is True


class TestToolExamples:
    """工具示例测试。"""

    def test_security_query_example(self, tmp_path: Path):
        """security_query 有示例。"""
        from agent_py_agent.agent.log_analysis.tools.tool_classes import SecurityQueryTool

        tool = SecurityQueryTool(tmp_path)
        assert len(tool.spec.examples) > 0
        example = json.loads(tool.spec.examples[0])
        assert example["tool"] == "security_query"

    def test_security_hunt_ip_example(self, tmp_path: Path):
        """security_hunt_ip 有示例。"""
        from agent_py_agent.agent.log_analysis.tools.tool_classes import SecurityHuntIpTool

        tool = SecurityHuntIpTool(tmp_path)
        assert len(tool.spec.examples) > 0
        example = json.loads(tool.spec.examples[0])
        assert example["tool"] == "security_hunt_ip"

    def test_security_trace_case_example(self, tmp_path: Path):
        """security_trace_case 有示例。"""
        from agent_py_agent.agent.log_analysis.tools.tool_classes import SecurityTraceCaseTool

        tool = SecurityTraceCaseTool(tmp_path)
        assert len(tool.spec.examples) > 0
        example = json.loads(tool.spec.examples[0])
        assert example["tool"] == "security_trace_case"
