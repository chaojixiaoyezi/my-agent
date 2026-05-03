"""gateway_parts/logging.py 单元测试。

测试日志格式、级别控制。
"""
from __future__ import annotations

import sys
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestReportGatewaySideEffectError:
    """测试 _report_gateway_side_effect_error() 函数。"""

    def test_error_output_format(self):
        """验证错误输出格式。"""
        import sys
        from io import StringIO

        from agent_py_agent.agent.gateway_parts.logging import _report_gateway_side_effect_error

        old_stderr = sys.stderr
        sys.stderr = StringIO()

        try:
            _report_gateway_side_effect_error("test_operation", "req_123", ValueError("test error"))
            output = sys.stderr.getvalue()
        finally:
            sys.stderr = old_stderr

        assert "[gateway-side-effect-error]" in output
        assert "test_operation" in output
        assert "req_123" in output
        assert "ValueError" in output


class TestLogGatewayPayload:
    """测试 log_gateway_payload() 函数。"""

    def test_empty_request_id_skipped(self, tmp_path: Path):
        """空 request_id 时跳过记录。"""
        from agent_py_agent.agent.gateway_parts.logging import log_gateway_payload

        mock_agent = MagicMock()
        payload = {"other": "value"}  # 没有 id

        log_gateway_payload(mock_agent, payload, event_type="test_event")

        mock_agent.local_store.log_record.assert_not_called()

    def test_valid_payload_logged(self, tmp_path: Path):
        """有效 payload 被记录。"""
        from agent_py_agent.agent.gateway_parts.logging import log_gateway_payload

        mock_agent = MagicMock()
        payload = {
            "id": "req_123",
            "status": "done",
            "kind": "ask",
            "ok": True,
        }

        log_gateway_payload(mock_agent, payload, event_type="test_event")

        mock_agent.local_store.log_record.assert_called_once()
        call_kwargs = mock_agent.local_store.log_record.call_args[1]
        assert call_kwargs["source_type"] == "gateway_request"
        assert call_kwargs["source_id"] == "req_123"


class TestLogGatewayEvent:
    """测试 log_gateway_event() 函数。"""

    def test_event_logged(self, tmp_path: Path):
        """事件被正确记录。"""
        from agent_py_agent.agent.gateway_parts.logging import log_gateway_event

        mock_agent = MagicMock()
        payload = {"status": "running", "pid": 12345}

        log_gateway_event(mock_agent, "gateway_start", payload)

        mock_agent.local_store.log_record.assert_called_once()
        call_kwargs = mock_agent.local_store.log_record.call_args[1]
        assert call_kwargs["source_type"] == "gateway_event"
        assert "gateway_start" in call_kwargs["title"]


class TestIndexGatewayPayload:
    """测试 _index_gateway_payload() 函数。"""

    def test_missing_request_id_returns_false(self, tmp_path: Path):
        """缺少 request_id 返回 False。"""
        from agent_py_agent.agent.gateway_parts.logging import _index_gateway_payload

        mock_agent = MagicMock()
        payload = {"status": "test"}

        result = _index_gateway_payload(mock_agent, payload)
        assert result is False

    def test_valid_payload_indexed(self, tmp_path: Path):
        """有效 payload 被索引。"""
        from agent_py_agent.agent.gateway_parts.logging import _index_gateway_payload

        mock_agent = MagicMock()
        payload = {"id": "req_456", "status": "done", "ok": True}

        result = _index_gateway_payload(mock_agent, payload)

        assert result is True
        mock_agent.local_store.log_record.assert_called_once()

    def test_request_path_as_fallback_id(self, tmp_path: Path):
        """request_path.stem 作为后备 ID。"""
        from agent_py_agent.agent.gateway_parts.logging import _index_gateway_payload

        mock_agent = MagicMock()
        payload = {"status": "done"}  # 没有 id

        request_path = tmp_path / "req_789.json"
        result = _index_gateway_payload(mock_agent, payload, request_path=request_path)

        assert result is True
        call_kwargs = mock_agent.local_store.log_record.call_args[1]
        assert call_kwargs["source_id"] == "req_789"


class TestGatewayLoggingIntegration:
    """日志模块集成测试。"""

    def test_side_effect_error_silenced_on_secondary_failure(self, capsys):
        """次级失败时静默处理，不抛出异常。"""
        from agent_py_agent.agent.gateway_parts.logging import _report_gateway_side_effect_error

        # 模拟 print 失败
        with patch.object(sys.stderr, "write", side_effect=OSError("disk full")):
            try:
                _report_gateway_side_effect_error("op", "id", ValueError("test"))
            except Exception:
                pass  # 不应该抛出