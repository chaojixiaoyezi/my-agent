"""log_analysis_commands CLI 命令测试。

测试 log-analysis status/ingest/query/hunt-ip/trace-case 命令。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestCmdLogs:
    """测试 cmd_logs 命令。"""

    def test_cmd_logs_help(self, capsys):
        """验证 logs 命令显示帮助信息。"""
        from agent_py_agent.cli.logs import cmd_logs

        args = MagicMock()

        result = cmd_logs(args)

        assert result == 2
        captured = capsys.readouterr()
        assert "Usage" in captured.out


class TestCmdLogsStatus:
    """测试 cmd_logs_status 命令。"""

    def test_logs_status_basic(self, tmp_path: Path):
        """正常获取日志分析状态。"""
        from agent_py_agent.cli.logs import cmd_logs_status

        args = MagicMock()
        args.json = False

        mock_status = {
            "state": "running",
            "capability_level": 3,
            "paths": {"base": {"path": str(tmp_path)}},
            "config": {"effective": {"worker_enabled": True}, "warnings": []}
        }

        mock_config = MagicMock()
        mock_config.query_default_limit = 100
        mock_config.query_max_limit = 10000

        with patch("agent_py_agent.cli.logs.collect_doctor_status", return_value=mock_status), \
             patch("agent_py_agent.cli.logs.load_log_analysis_config", return_value=mock_config):
            result = cmd_logs_status(args)
            assert result == 0

    def test_logs_status_json_output(self, tmp_path: Path):
        """JSON 输出格式。"""
        from agent_py_agent.cli.logs import cmd_logs_status

        args = MagicMock()
        args.json = True

        mock_status = {
            "state": "running",
            "capability_level": 3,
            "paths": {"base": {"path": str(tmp_path)}},
            "config": {"effective": {"worker_enabled": True}, "warnings": []}
        }

        mock_config = MagicMock()
        mock_config.query_default_limit = 100
        mock_config.query_max_limit = 10000

        with patch("agent_py_agent.cli.logs.collect_doctor_status", return_value=mock_status), \
             patch("agent_py_agent.cli.logs.load_log_analysis_config", return_value=mock_config):
            result = cmd_logs_status(args)
            assert result == 0


class TestCmdLogsIngest:
    """测试 cmd_logs_ingest 命令。"""

    def test_logs_ingest_basic(self, tmp_path: Path):
        """正常摄入日志文件。"""
        from agent_py_agent.cli.logs import cmd_logs_ingest

        args = MagicMock()
        args.file = str(tmp_path / "test.jsonl")
        args.root = None
        args.source_id = "test_source"
        args.format = "jsonl"
        args.json = False

        mock_config = MagicMock()
        mock_config.data_dir = str(tmp_path)
        mock_config.payload_preview_max_chars = 200

        mock_result = {
            "status": "ok",
            "parsed_count": 100,
            "stored_count": 100,
            "duplicate_count": 0,
            "dead_letter_count": 0,
            "skipped_count": 0,
            "manifest_path": str(tmp_path / "manifest.json"),
            "checkpoint_path": str(tmp_path / "checkpoint.json")
        }

        with patch("agent_py_agent.cli.logs.load_log_analysis_config", return_value=mock_config), \
             patch("agent_py_agent.cli.logs.ingest_file", return_value=mock_result):
            result = cmd_logs_ingest(args)
            assert result == 0


class TestCmdLogsQuery:
    """测试 cmd_logs_query 命令。"""

    def test_logs_query_basic(self, tmp_path: Path):
        """正常查询日志。"""
        from agent_py_agent.cli.logs import cmd_logs_query

        args = MagicMock()
        args.root = None
        args.attacker_ip = None
        args.victim_ip = None
        args.domain = None
        args.uri = None
        args.alert_type = None
        args.start_time = None
        args.end_time = None
        args.limit = None
        args.json = False

        mock_config = MagicMock()
        mock_config.data_dir = str(tmp_path)
        mock_config.query_default_limit = 100
        mock_config.query_max_limit = 10000

        mock_response = {
            "query_id": "q_001",
            "row_count": 10,
            "truncated": False,
            "preview_rows": [{"id": 1, "message": "test"}]
        }

        with patch("agent_py_agent.cli.logs.load_log_analysis_config", return_value=mock_config), \
             patch("agent_py_agent.cli.logs.security_query", return_value=mock_response):
            result = cmd_logs_query(args)
            assert result == 0

    def test_logs_query_with_filters(self, tmp_path: Path):
        """带过滤条件查询。"""
        from agent_py_agent.cli.logs import cmd_logs_query

        args = MagicMock()
        args.root = None
        args.attacker_ip = "192.168.1.1"
        args.victim_ip = "10.0.0.1"
        args.domain = "example.com"
        args.uri = "/api/test"
        args.alert_type = "sql_injection"
        args.start_time = "2024-01-01"
        args.end_time = "2024-12-31"
        args.limit = 50
        args.json = False

        mock_config = MagicMock()
        mock_config.data_dir = str(tmp_path)
        mock_config.query_default_limit = 100
        mock_config.query_max_limit = 10000

        mock_response = {
            "query_id": "q_002",
            "row_count": 5,
            "truncated": False,
            "preview_rows": []
        }

        with patch("agent_py_agent.cli.logs.load_log_analysis_config", return_value=mock_config), \
             patch("agent_py_agent.cli.logs.security_query", return_value=mock_response):
            result = cmd_logs_query(args)
            assert result == 0


class TestCmdLogsHuntIp:
    """测试 cmd_logs_hunt_ip 命令。"""

    def test_logs_hunt_ip_basic(self, tmp_path: Path):
        """正常追踪 IP。"""
        from agent_py_agent.cli.logs import cmd_logs_hunt_ip

        args = MagicMock()
        args.ip = "192.168.1.1"
        args.root = None
        args.role = "any"
        args.start_time = None
        args.end_time = None
        args.limit = None
        args.json = False

        mock_config = MagicMock()
        mock_config.data_dir = str(tmp_path)
        mock_config.query_default_limit = 100
        mock_config.query_max_limit = 10000

        mock_response = {
            "query_id": "hunt_001",
            "row_count": 3,
            "truncated": False,
            "preview_rows": []
        }

        with patch("agent_py_agent.cli.logs.load_log_analysis_config", return_value=mock_config), \
             patch("agent_py_agent.cli.logs.hunt_ip", return_value=mock_response):
            result = cmd_logs_hunt_ip(args)
            assert result == 0


class TestCmdLogsTraceCase:
    """测试 cmd_logs_trace_case 命令。"""

    def test_logs_trace_case_basic(self, tmp_path: Path):
        """正常追踪案件。"""
        from agent_py_agent.cli.logs import cmd_logs_trace_case

        args = MagicMock()
        args.case_id = "case_001"
        args.root = None
        args.start_time = None
        args.end_time = None
        args.limit = None
        args.json = False

        mock_config = MagicMock()
        mock_config.data_dir = str(tmp_path)
        mock_config.query_default_limit = 100
        mock_config.query_max_limit = 10000

        mock_response = {
            "query_id": "trace_001",
            "row_count": 8,
            "truncated": False,
            "preview_rows": []
        }

        with patch("agent_py_agent.cli.logs.load_log_analysis_config", return_value=mock_config), \
             patch("agent_py_agent.cli.logs.trace_case", return_value=mock_response):
            result = cmd_logs_trace_case(args)
            assert result == 0


class TestResolveQueryLimit:
    """测试 _resolve_query_limit 辅助函数。"""

    def test_resolve_query_limit_default(self):
        """测试默认 limit。"""
        from agent_py_agent.cli.logs import _resolve_query_limit

        limit, warnings = _resolve_query_limit(None, 100, 10000)

        assert limit == 100
        assert len(warnings) == 0

    def test_resolve_query_limit_explicit(self):
        """测试显式指定 limit。"""
        from agent_py_agent.cli.logs import _resolve_query_limit

        limit, warnings = _resolve_query_limit(50, 100, 10000)

        assert limit == 50
        assert len(warnings) == 0

    def test_resolve_query_limit_negative(self):
        """测试负数 limit 回退到默认值。"""
        from agent_py_agent.cli.logs import _resolve_query_limit

        limit, warnings = _resolve_query_limit(-5, 100, 10000)

        assert limit == 100
        assert len(warnings) == 1

    def test_resolve_query_limit_exceeds_max(self):
        """测试超过最大值的 limit 被截断。"""
        from agent_py_agent.cli.logs import _resolve_query_limit

        limit, warnings = _resolve_query_limit(20000, 100, 10000)

        assert limit == 10000
        assert len(warnings) == 1
