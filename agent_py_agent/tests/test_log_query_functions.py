"""日志查询函数测试 - query_functions.py 有边界查询执行、IP/域名追踪、case 扩展。"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


class TestToolResponse:
    """_tool_response 辅助函数测试。"""

    def test_tool_response_fields(self):
        """验证工具响应字段。"""
        from agent_py_agent.agent.log_analysis.tools.query_functions import _tool_response

        mock_result = MagicMock()
        mock_result.query_id = "q-1"
        mock_result.parameters = {"attacker_ip": "1.2.3.4"}
        mock_result.row_count = 10
        mock_result.truncated = False
        mock_result.evidence_path = "/path/to/evidence"
        mock_result.summary = "查询摘要"
        mock_result.preview_rows = [{"id": 1}]

        resp = _tool_response(mock_result)

        assert resp["tool"] == "security_query"
        assert resp["query_id"] == "q-1"
        assert resp["row_count"] == 10
        assert resp["truncated"] is False
        assert resp["evidence_path"] == "/path/to/evidence"
        assert resp["evidence_refs"] == ["q-1"]
        assert resp["summary"] == "查询摘要"
        assert resp["preview_rows"] == [{"id": 1}]


class TestExtendSeed:
    """_extend_seed 辅助函数测试。"""

    def test_extend_seed_single_value(self):
        """追加单个值。"""
        from agent_py_agent.agent.log_analysis.tools.query_functions import _extend_seed

        target: list[str] = []
        _extend_seed(target, "value1")
        assert target == ["value1"]

    def test_extend_seed_list(self):
        """追加列表值。"""
        from agent_py_agent.agent.log_analysis.tools.query_functions import _extend_seed

        target: list[str] = []
        _extend_seed(target, ["a", "b"])
        assert target == ["a", "b"]

    def test_extend_seed_tuple(self):
        """追加元组值。"""
        from agent_py_agent.agent.log_analysis.tools.query_functions import _extend_seed

        target: list[str] = []
        _extend_seed(target, ("x", "y"))
        assert target == ["x", "y"]

    def test_extend_seed_set(self):
        """追加集合值。"""
        from agent_py_agent.agent.log_analysis.tools.query_functions import _extend_seed

        target: list[str] = []
        _extend_seed(target, {"s1", "s2"})
        assert set(target) == {"s1", "s2"}

    def test_extend_seed_none_ignored(self):
        """None 被忽略。"""
        from agent_py_agent.agent.log_analysis.tools.query_functions import _extend_seed

        target: list[str] = ["existing"]
        _extend_seed(target, None)
        assert target == ["existing"]

    def test_extend_seed_empty_string_ignored(self):
        """空字符串被忽略。"""
        from agent_py_agent.agent.log_analysis.tools.query_functions import _extend_seed

        target: list[str] = ["existing"]
        _extend_seed(target, "")
        assert target == ["existing"]

    def test_extend_seed_no_duplicates(self):
        """不重复追加。"""
        from agent_py_agent.agent.log_analysis.tools.query_functions import _extend_seed

        target: list[str] = ["value"]
        _extend_seed(target, "value")
        assert target == ["value"]


class TestCaseSeeds:
    """_case_seeds 辅助函数测试。"""

    def test_case_seeds_from_top_level(self):
        """从顶层字段提取种子。"""
        from agent_py_agent.agent.log_analysis.tools.query_functions import _case_seeds

        case = {
            "attacker_ip": "1.2.3.4",
            "victim_ip": "5.6.7.8",
            "domain": "evil.com",
        }
        seeds = _case_seeds(case)
        assert seeds["attacker_ip"] == ["1.2.3.4"]
        assert seeds["victim_ip"] == ["5.6.7.8"]
        assert seeds["domain"] == ["evil.com"]

    def test_case_seeds_from_entities(self):
        """从 entities 嵌套提取种子。"""
        from agent_py_agent.agent.log_analysis.tools.query_functions import _case_seeds

        case = {
            "entities": {
                "attacker_ip": ["10.0.0.1"],
                "victim_ip": ["10.0.0.2"],
            }
        }
        seeds = _case_seeds(case)
        assert seeds["attacker_ip"] == ["10.0.0.1"]
        assert seeds["victim_ip"] == ["10.0.0.2"]

    def test_case_seeds_from_attributes(self):
        """从 attributes 嵌套提取种子。"""
        from agent_py_agent.agent.log_analysis.tools.query_functions import _case_seeds

        case = {
            "attributes": {
                "domain": ["bad.domain.com"],
            }
        }
        seeds = _case_seeds(case)
        assert seeds["domain"] == ["bad.domain.com"]

    def test_case_seeds_removes_empty(self):
        """移除空种子。"""
        from agent_py_agent.agent.log_analysis.tools.query_functions import _case_seeds

        case = {
            "attacker_ip": "1.2.3.4",
            "victim_ip": "",
            "domain": None,
        }
        seeds = _case_seeds(case)
        assert "victim_ip" not in seeds
        assert "domain" not in seeds

    def test_case_seeds_handles_nested_lists(self):
        """处理嵌套列表。"""
        from agent_py_agent.agent.log_analysis.tools.query_functions import _case_seeds

        case = {
            "entities": {
                "uri": [["/api/v1", "/api/v2"]],
            }
        }
        seeds = _case_seeds(case)
        assert "/api/v1" in seeds["uri"]
        assert "/api/v2" in seeds["uri"]


class TestStore:
    """_store 辅助函数测试。"""

    def test_store_uses_provided(self):
        """优先使用传入的 store。"""
        from agent_py_agent.agent.log_analysis.tools.query_functions import _store

        mock_store = MagicMock()
        result = _store(mock_store, "/any/path")
        assert result is mock_store

    def test_store_creates_from_root(self):
        """未提供 store 时按 root 创建。"""
        from agent_py_agent.agent.log_analysis.tools.query_functions import _store

        with patch("agent_py_agent.agent.log_analysis.tools.query_functions.LocalLogStore") as MockStore:
            _store(None, "/tmp/root")
            MockStore.assert_called_once_with("/tmp/root")


class TestHuntIpRole:
    """hunt_ip 函数 role 参数测试。"""

    def test_invalid_role_raises(self):
        """无效 role 抛出 ValueError。"""
        from agent_py_agent.agent.log_analysis.tools.query_functions import hunt_ip

        with pytest.raises(ValueError, match="role must be one of"):
            hunt_ip("1.2.3.4", role="invalid_role")

    def test_role_attacker(self):
        """role=attacker 只查 attacker_ip。"""
        from agent_py_agent.agent.log_analysis.tools.query_functions import hunt_ip

        with patch("agent_py_agent.agent.log_analysis.tools.query_functions.security_query") as mock_query:
            mock_query.return_value = {"tool": "security_query", "row_count": 5}
            result = hunt_ip("1.2.3.4", role="attacker", store=MagicMock())
            mock_query.assert_called_once()
            call_args = mock_query.call_args[0]
            params = call_args[0]
            assert params.attacker_ip == "1.2.3.4"

    def test_role_victim(self):
        """role=victim 只查 victim_ip。"""
        from agent_py_agent.agent.log_analysis.tools.query_functions import hunt_ip

        with patch("agent_py_agent.agent.log_analysis.tools.query_functions.security_query") as mock_query:
            mock_query.return_value = {"tool": "security_query", "row_count": 3}
            result = hunt_ip("5.6.7.8", role="victim", store=MagicMock())
            call_args = mock_query.call_args[0]
            params = call_args[0]
            assert params.victim_ip == "5.6.7.8"

    def test_role_any_returns_combined(self):
        """role=any 返回组合结果。"""
        from agent_py_agent.agent.log_analysis.tools.query_functions import hunt_ip

        with patch("agent_py_agent.agent.log_analysis.tools.query_functions.execute_security_query") as mock_exec:
            mock_attacker = MagicMock()
            mock_attacker.row_count = 2
            mock_attacker.query_id = "q-attacker"
            mock_victim = MagicMock()
            mock_victim.row_count = 3
            mock_victim.query_id = "q-victim"
            mock_exec.side_effect = [mock_attacker, mock_victim]

            result = hunt_ip("1.2.3.4", role="any", store=MagicMock())
            assert result["tool"] == "security_hunt_ip"
            assert result["seed"]["ip"] == "1.2.3.4"
            assert result["seed"]["role"] == "any"
            assert result["row_count"] == 5


class TestSecurityHuntDomain:
    """security_hunt_domain 函数测试。"""

    def test_delegates_to_security_query(self):
        """委托给 security_query。"""
        from agent_py_agent.agent.log_analysis.tools.query_functions import security_hunt_domain

        with patch("agent_py_agent.agent.log_analysis.tools.query_functions.security_query") as mock_query:
            mock_query.return_value = {"tool": "security_query", "row_count": 1}
            result = security_hunt_domain("evil.com")
            mock_query.assert_called_once()
            call_args = mock_query.call_args[0]
            params = call_args[0]
            assert params.domain == "evil.com"


class TestSecurityHuntIp:
    """security_hunt_ip 函数测试。"""

    def test_delegates_to_hunt_ip(self):
        """委托给 hunt_ip。"""
        from agent_py_agent.agent.log_analysis.tools.query_functions import security_hunt_ip

        with patch("agent_py_agent.agent.log_analysis.tools.query_functions.hunt_ip") as mock_hunt:
            mock_hunt.return_value = {"tool": "security_hunt_ip", "row_count": 7}
            result = security_hunt_ip("1.2.3.4", store=MagicMock())
            mock_hunt.assert_called_once()
            call_args = mock_hunt.call_args
            assert call_args[0][0] == "1.2.3.4"


class TestSecurityTraceCase:
    """security_trace_case 函数测试。"""

    def test_delegates_to_trace_case(self):
        """委托给 trace_case。"""
        from agent_py_agent.agent.log_analysis.tools.query_functions import security_trace_case

        with patch("agent_py_agent.agent.log_analysis.tools.query_functions.trace_case") as mock_trace:
            mock_trace.return_value = {"tool": "security_trace_case", "row_count": 0}
            result = security_trace_case("case-1", store=MagicMock())
            mock_trace.assert_called_once()
            call_args = mock_trace.call_args
            assert call_args[0][0] == "case-1"


class TestTraceCaseNotFound:
    """trace_case 找不到 case 时测试。"""

    def test_raises_key_error(self):
        """找不到 case 抛出 KeyError。"""
        from agent_py_agent.agent.log_analysis.tools.query_functions import trace_case

        mock_store = MagicMock()
        mock_store.get_case.return_value = None

        with pytest.raises(KeyError, match="case not found"):
            trace_case("nonexistent-case", store=mock_store)
