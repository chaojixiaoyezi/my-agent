"""gateway_parts/adapter.py 单元测试。

测试文件协议适配器：消息解析、请求投递、响应接收。
"""
from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _gateway_paths(tmp_path: Path):
    from agent_py_agent.agent.gateway_parts.paths import GatewayPaths

    return GatewayPaths(
        root=tmp_path / "gateway",
        pid=tmp_path / "gateway/gateway.pid",
        adapter_pid=tmp_path / "gateway/adapter.pid",
        state=tmp_path / "gateway/state.json",
        heartbeat=tmp_path / "gateway/heartbeat.json",
        stop_request=tmp_path / "gateway/stop.request",
        log=tmp_path / "gateway/gateway.log",
        inbox=tmp_path / "gateway/requests/pending",
        processing=tmp_path / "gateway/requests/processing",
        done=tmp_path / "gateway/requests/done",
        failed=tmp_path / "gateway/requests/failed",
        responses=tmp_path / "gateway/responses",
        history=tmp_path / "gateway/history.jsonl",
    )


def _adapter_paths(root: Path):
    from agent_py_agent.agent.gateway_parts.paths import AdapterPaths

    return AdapterPaths(
        root=root,
        inbox=root / "inbox",
        processing=root / "processing",
        done=root / "done",
        failed=root / "failed",
        outbox=root / "outbox",
    )


def _write_adapter_messages(adapter_paths, count: int) -> None:
    adapter_paths.inbox.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        (adapter_paths.inbox / f"msg_{i}.json").write_text(
            '{"message": "test"}', encoding="utf-8"
        )


class TestAdapterMessageId:
    """测试 _adapter_message_id() 函数。"""

    def test_id_from_payload(self):
        """验证从 payload 获取 id。"""
        from agent_py_agent.agent.gateway_parts.adapter import _adapter_message_id

        payload = {"id": "msg_123"}
        result = _adapter_message_id(payload, Path("/tmp/test.json"))
        assert result == "msg_123"

    def test_message_id_fallback(self):
        """验证 message_id 作为后备。"""
        from agent_py_agent.agent.gateway_parts.adapter import _adapter_message_id

        payload = {"message_id": "msg_456"}
        result = _adapter_message_id(payload, Path("/tmp/test.json"))
        assert result == "msg_456"

    def test_filename_fallback(self):
        """验证文件名作为后备。"""
        from agent_py_agent.agent.gateway_parts.adapter import _adapter_message_id

        payload = {}
        result = _adapter_message_id(payload, Path("/tmp/my_message.json"))
        assert result == "my_message"

    def test_empty_payload_uses_filename(self):
        """空 payload 时使用文件名。"""
        from agent_py_agent.agent.gateway_parts.adapter import _adapter_message_id

        payload = {"id": ""}
        result = _adapter_message_id(payload, Path("/tmp/test.json"))
        assert result == "test"


class TestAdapterMessagePrompt:
    """测试 _adapter_message_prompt() 函数。"""

    def test_prompt_field(self):
        """验证 prompt 字段优先。"""
        from agent_py_agent.agent.gateway_parts.adapter import _adapter_message_prompt

        payload = {"prompt": "测试 prompt"}
        result = _adapter_message_prompt(payload)
        assert result == "测试 prompt"

    def test_text_field(self):
        """验证 text 字段。"""
        from agent_py_agent.agent.gateway_parts.adapter import _adapter_message_prompt

        payload = {"text": "测试 text"}
        result = _adapter_message_prompt(payload)
        assert result == "测试 text"

    def test_message_field(self):
        """验证 message 字段。"""
        from agent_py_agent.agent.gateway_parts.adapter import _adapter_message_prompt

        payload = {"message": "测试 message"}
        result = _adapter_message_prompt(payload)
        assert result == "测试 message"

    def test_content_field(self):
        """验证 content 字段。"""
        from agent_py_agent.agent.gateway_parts.adapter import _adapter_message_prompt

        payload = {"content": "测试 content"}
        result = _adapter_message_prompt(payload)
        assert result == "测试 content"

    def test_empty_returns_empty_string(self):
        """无有效字段时返回空字符串。"""
        from agent_py_agent.agent.gateway_parts.adapter import _adapter_message_prompt

        payload = {"other": "value"}
        result = _adapter_message_prompt(payload)
        assert result == ""


class TestAdapterOutputPath:
    """测试 _adapter_output_path() 函数。"""

    def test_output_path_format(self):
        """验证输出路径格式。"""
        from agent_py_agent.agent.gateway_parts.adapter import _adapter_output_path
        from agent_py_agent.agent.gateway_parts.paths import AdapterPaths

        paths = AdapterPaths(
            root=Path("/tmp/adapter"),
            inbox=Path("/tmp/adapter/inbox"),
            processing=Path("/tmp/adapter/processing"),
            done=Path("/tmp/adapter/done"),
            failed=Path("/tmp/adapter/failed"),
            outbox=Path("/tmp/adapter/outbox"),
        )

        result = _adapter_output_path(paths, "msg_123")
        assert result == Path("/tmp/adapter/outbox/msg_123.json")


class TestRecordLatePending:
    """测试 _record_late_pending() 函数。"""

    def test_record_late_pending_creates_file(self, tmp_path: Path):
        """验证记录迟到请求到 late_pending.jsonl。"""
        from agent_py_agent.agent.gateway_parts.adapter import _record_late_pending
        from agent_py_agent.agent.gateway_parts.paths import AdapterPaths

        adapter_paths = AdapterPaths(
            root=tmp_path,
            inbox=tmp_path / "inbox",
            processing=tmp_path / "processing",
            done=tmp_path / "done",
            failed=tmp_path / "failed",
            outbox=tmp_path / "outbox",
        )

        _record_late_pending(adapter_paths, "req_123", 30.0)

        late_path = tmp_path / "late_pending.jsonl"
        assert late_path.exists()
        content = late_path.read_text(encoding="utf-8")
        assert "req_123" in content
        assert "30.0" in content


class TestCheckLateResponses:
    """测试 check_late_responses() 函数。"""

    def test_no_late_pending_file(self, tmp_path: Path):
        """无 late_pending.jsonl 时返回空列表。"""
        from agent_py_agent.agent.gateway_parts.adapter import check_late_responses
        from agent_py_agent.agent.gateway_parts.paths import AdapterPaths

        adapter_paths = AdapterPaths(
            root=tmp_path,
            inbox=tmp_path / "inbox",
            processing=tmp_path / "processing",
            done=tmp_path / "done",
            failed=tmp_path / "failed",
            outbox=tmp_path / "outbox",
        )

        result = check_late_responses(adapter_paths)
        assert result == []

    def test_late_pending_file_empty(self, tmp_path: Path):
        """空 late_pending.jsonl 时返回空列表。"""
        from agent_py_agent.agent.gateway_parts.adapter import check_late_responses
        from agent_py_agent.agent.gateway_parts.paths import AdapterPaths

        late_path = tmp_path / "late_pending.jsonl"
        late_path.write_text("", encoding="utf-8")

        adapter_paths = AdapterPaths(
            root=tmp_path,
            inbox=tmp_path / "inbox",
            processing=tmp_path / "processing",
            done=tmp_path / "done",
            failed=tmp_path / "failed",
            outbox=tmp_path / "outbox",
        )

        result = check_late_responses(adapter_paths)
        assert result == []


class TestProcessFileAdapterOnce:
    """测试 process_file_adapter_once() 函数。"""

    def test_empty_inbox_returns_zero(self, tmp_path: Path):
        """空 inbox 返回 0。"""
        from agent_py_agent.agent.gateway_parts.adapter import process_file_adapter_once
        gateway_paths = _gateway_paths(tmp_path)
        adapter_paths = _adapter_paths(tmp_path / "adapter")

        mock_agent = MagicMock()

        result = process_file_adapter_once(
            mock_agent,
            gateway_paths_obj=gateway_paths,
            adapter_paths_obj=adapter_paths,
            timeout=5.0,
            limit=20,
        )

        assert result == 0

    def test_limit_parameter(self, tmp_path: Path):
        """验证 limit 参数限制处理数量。"""
        from agent_py_agent.agent.gateway_parts.adapter import process_file_adapter_once

        gateway_paths = _gateway_paths(tmp_path)
        adapter_paths = _adapter_paths(tmp_path / "adapter")

        # 创建 3 条消息
        _write_adapter_messages(adapter_paths, 3)

        mock_agent = MagicMock()

        result = process_file_adapter_once(
            mock_agent,
            gateway_paths_obj=gateway_paths,
            adapter_paths_obj=adapter_paths,
            timeout=5.0,
            limit=1,  # 只处理 1 条
        )

        assert result == 1


class TestAdapterCliBundles:
    """Adapter CLI helper bundle regressions."""

    def test_adapter_options_from_args_normalizes_paths(self, tmp_path: Path):
        from agent_py_agent.cli.adapter import _adapter_options_from_args

        args = argparse.Namespace(
            root=str(tmp_path / "adapter"),
            inbox=str(tmp_path / "custom_inbox"),
            outbox=str(tmp_path / "custom_outbox"),
            timeout=None,
            limit=7,
            once=True,
            watch=False,
            poll_interval=0.5,
            no_start_gateway=True,
            channel="qq",
            pid_file=str(tmp_path / "adapter.pid"),
            daemon=False,
        )

        options = _adapter_options_from_args(args)

        assert options.root == tmp_path / "adapter"
        assert options.inbox == tmp_path / "custom_inbox"
        assert options.outbox == tmp_path / "custom_outbox"
        assert options.stop_timeout == 10.0
        assert options.channel == "qq"

    def test_process_file_adapter_loop_uses_options_not_args(self, tmp_path: Path):
        from agent_py_agent.cli.adapter import _process_file_adapter_loop
        from agent_py_agent.cli.models import AdapterOptions

        gpaths = _gateway_paths(tmp_path)
        apaths = _adapter_paths(tmp_path / "adapter")
        options = AdapterOptions(
            root=None,
            inbox=None,
            outbox=None,
            timeout=3.0,
            limit=9,
            once=True,
            watch=True,
            poll_interval=0.2,
            no_start_gateway=True,
            channel="all",
            pid_file=None,
            daemon=False,
            stop_timeout=10.0,
        )

        with patch("agent_py_agent.cli.adapter.process_file_adapter_once", return_value=2) as mock_once:
            total = _process_file_adapter_loop(MagicMock(), options, gpaths, apaths, timeout=3.0)

        assert total == 2
        assert mock_once.call_args.kwargs["limit"] == 9
        assert mock_once.call_args.kwargs["timeout"] == 3.0
