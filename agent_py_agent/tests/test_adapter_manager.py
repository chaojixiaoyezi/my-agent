"""LLM: 测试通道管理器 — 注册、路由、启停。

给人看的解释：
测试 ChannelManager 的各项能力：适配器注册/查询/列表、启停管理。
gateway HTTP 调用使用 mock，不实际发起网络请求。
"""

from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from agent_py_agent.agent.adapter.base import BaseChannelAdapter
from agent_py_agent.agent.adapter.manager import ChannelManager
from agent_py_agent.agent.adapter.protocol import IncomingMessage, OutgoingMessage


class DummyAdapter(BaseChannelAdapter):
    """测试用伪适配器。"""

    adapter_name = "dummy"

    def __init__(self) -> None:
        super().__init__({})
        self._start_called = False
        self._stop_called = False
        self._send_calls: list[tuple[str, OutgoingMessage]] = []

    def start(self) -> None:
        self._running = True
        self._start_called = True

    def stop(self) -> None:
        self._running = False
        self._stop_called = True

    def send_message(self, user_id: str, message: OutgoingMessage) -> bool:
        self._send_calls.append((user_id, message))
        return True


class TestChannelManagerRegistration:
    """测试通道管理器的适配器注册接口。"""

    def test_register_and_get(self) -> None:
        manager = ChannelManager()
        dummy = DummyAdapter()
        manager.register_adapter(dummy)
        assert manager.get_adapter("dummy") is dummy

    def test_list_adapters(self) -> None:
        manager = ChannelManager()
        a1 = DummyAdapter()
        a1.adapter_name = "one"
        a2 = DummyAdapter()
        a2.adapter_name = "two"
        manager.register_adapter(a1)
        manager.register_adapter(a2)
        assert sorted(manager.list_adapters()) == ["one", "two"]

    def test_get_nonexistent_returns_none(self) -> None:
        manager = ChannelManager()
        assert manager.get_adapter("notexist") is None

    def test_replace_existing_adapter(self) -> None:
        manager = ChannelManager()
        a1 = DummyAdapter()
        a1.adapter_name = "dup"
        a2 = DummyAdapter()
        a2.adapter_name = "dup"
        manager.register_adapter(a1)
        manager.register_adapter(a2)
        assert manager.get_adapter("dup") is a2


class TestChannelManagerLifecycle:
    """测试通道管理器启停。"""

    def test_start_all(self) -> None:
        manager = ChannelManager()
        a = DummyAdapter()
        manager.register_adapter(a)
        manager.start_all()
        assert a._start_called is True
        assert a.running is True
        a.stop()

    def test_stop_all(self) -> None:
        manager = ChannelManager()
        a = DummyAdapter()
        manager.register_adapter(a)
        a.start()
        manager.stop_all()
        assert a._stop_called is True
        assert a.running is False

    def test_start_all_then_stop_all(self) -> None:
        manager = ChannelManager()
        for name in ("x", "y"):
            a = DummyAdapter()
            a.adapter_name = name
            manager.register_adapter(a)
        manager.start_all()
        manager.stop_all()
        for name in manager.list_adapters():
            adapter = manager.get_adapter(name)
            assert adapter is not None
            assert adapter.running is False


class TestChannelManagerActiveChannel:
    """测试活跃通道查询。"""

    def test_no_session_file_returns_none(self) -> None:
        manager = ChannelManager()
        manager.session_channel_file = Path("/nonexistent/file.json")
        assert manager.get_active_channel("user_1") is None

    def test_update_and_query_active_channel(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            manager = ChannelManager()
            session_file = Path(td) / "sessions.json"
            manager.session_channel_file = session_file

            manager.update_active_channel("user_1", "feishu")
            assert manager.get_active_channel("user_1") == "feishu"

            manager.update_active_channel("user_1", "qq")
            assert manager.get_active_channel("user_1") == "qq"


class TestChannelManagerRouteMessage:
    """测试消息路由到 gateway。"""

    def test_route_calls_gateway_ask(self) -> None:
        manager = ChannelManager(gateway_port=8420)

        dummy = DummyAdapter()
        dummy.adapter_name = "feishu"
        manager.register_adapter(dummy)

        msg = IncomingMessage(
            channel="feishu",
            user_id="ou_123",
            content="hello",
            message_id="m1",
        )

        with patch("urllib.request.urlopen") as mock_urlopen:
            # Mock gateway /ask 响应
            mock_resp = MagicMock()
            mock_resp.__enter__ = MagicMock(
                return_value=MagicMock(
                    read=MagicMock(return_value=b'{"request_id": "req_1"}')
                )
            )
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp

            with patch.object(manager, "_poll_gateway_result", return_value="响应内容"):
                manager.route_message(msg)

    def test_route_falls_back_when_adapter_not_found(self) -> None:
        manager = ChannelManager()
        # 不注册任何适配器

        msg = IncomingMessage(
            channel="feishu",
            user_id="ou_123",
            content="hello",
            message_id="m1",
        )

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.__enter__ = MagicMock(
                return_value=MagicMock(
                    read=MagicMock(return_value=b'{"request_id": "req_1"}')
                )
            )
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp

            with patch.object(manager, "_poll_gateway_result", return_value="resp"):
                result = manager.route_message(msg)
                assert result is False

    def test_route_sends_placeholder_then_finalizes_with_handle(self) -> None:
        """提交后发占位拿句柄,完成后把句柄+结果交给 finalize_response 原地更新(typing 流程接线)。"""
        manager = ChannelManager(gateway_port=8420)
        dummy = DummyAdapter()
        dummy.adapter_name = "feishu"
        manager.register_adapter(dummy)
        msg = IncomingMessage(channel="feishu", user_id="ou_123", content="hi", message_id="m1")

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.__enter__ = MagicMock(
                return_value=MagicMock(read=MagicMock(return_value=b'{"request_id": "req_1"}'))
            )
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp

            with patch.object(manager, "_poll_gateway_result", return_value="答案"), \
                 patch.object(dummy, "send_progress_placeholder", return_value="om_card") as ph, \
                 patch.object(dummy, "finalize_response", return_value=True) as fin:
                manager.route_message(msg)
                ph.assert_called_once_with("ou_123")  # 提交后立即发占位
                fin.assert_called_once()
                # 把占位句柄 + 最终结果交给 finalize_response 原地更新
                assert fin.call_args.args[1] == "om_card"
                assert fin.call_args.args[2].content == "答案"
