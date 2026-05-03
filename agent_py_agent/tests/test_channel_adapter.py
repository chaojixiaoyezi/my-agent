"""channel_adapter.py 单元测试。

测试通道抽象、消息路由。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestChannelAdapterAbstract:
    """测试通道适配器抽象特性。"""

    def test_base_is_abc(self):
        """验证 BaseChannelAdapter 是抽象基类。"""
        from agent_py_agent.agent.adapter.base import BaseChannelAdapter

        assert issubclass(BaseChannelAdapter, __import__("abc").ABC)


class TestChannelAdapterIntegration:
    """测试通道适配器集成。"""

    def test_send_message_returns_bool(self):
        """验证 send_message 返回布尔值。"""
        from agent_py_agent.agent.adapter.base import BaseChannelAdapter
        from agent_py_agent.agent.adapter.protocol import OutgoingMessage

        class SimpleAdapter(BaseChannelAdapter):
            adapter_name = "simple"

            def start(self) -> None:
                pass

            def stop(self) -> None:
                pass

            def send_message(self, user_id: str, message: OutgoingMessage) -> bool:
                return True

        adapter = SimpleAdapter({})
        message = OutgoingMessage(channel="test", user_id="u1", content="hello")
        result = adapter.send_message("u1", message)

        assert isinstance(result, bool)

    def test_running_property(self):
        """验证 running 属性。"""
        from agent_py_agent.agent.adapter.base import BaseChannelAdapter

        class SimpleAdapter(BaseChannelAdapter):
            adapter_name = "simple"

            def start(self) -> None:
                self._running = True

            def stop(self) -> None:
                self._running = False

            def send_message(self, user_id: str, message) -> bool:
                return True

        adapter = SimpleAdapter({})
        assert adapter.running is False

        adapter.start()
        assert adapter.running is True

        adapter.stop()
        assert adapter.running is False


class TestOutgoingMessage:
    """测试 OutgoingMessage 构造。"""

    def test_outgoing_message_creation(self):
        """验证 OutgoingMessage 创建。"""
        from agent_py_agent.agent.adapter.protocol import OutgoingMessage

        msg = OutgoingMessage(
            channel="feishu",
            user_id="user_123",
            content="test message",
            format="text",
        )

        assert msg.channel == "feishu"
        assert msg.user_id == "user_123"
        assert msg.content == "test message"


class TestIncomingMessage:
    """测试 IncomingMessage 构造。"""

    def test_incoming_message_creation(self):
        """验证 IncomingMessage 创建。"""
        from agent_py_agent.agent.adapter.protocol import IncomingMessage

        msg = IncomingMessage(
            channel="qq",
            user_id="user_456",
            message_id="msg_789",
            content="hello",
        )

        assert msg.channel == "qq"
        assert msg.user_id == "user_456"
        assert msg.message_id == "msg_789"
        assert msg.content == "hello"


class TestAdapterRouting:
    """测试适配器路由。"""

    def test_dispatch_to_callback(self):
        """验证消息分发给回调。"""
        from agent_py_agent.agent.adapter.base import BaseChannelAdapter
        from agent_py_agent.agent.adapter.protocol import IncomingMessage

        class TestAdapter(BaseChannelAdapter):
            adapter_name = "test"

            def start(self) -> None:
                pass

            def stop(self) -> None:
                pass

            def send_message(self, user_id: str, message) -> bool:
                return True

        adapter = TestAdapter({})
        callback = MagicMock()
        adapter.on_message(callback)

        msg = IncomingMessage(
            channel="test",
            user_id="u1",
            message_id="m1",
            content="test",
        )
        adapter._dispatch(msg)

        callback.assert_called_once_with(msg)

    def test_no_callback_no_dispatch(self):
        """无回调时不分发。"""
        from agent_py_agent.agent.adapter.base import BaseChannelAdapter
        from agent_py_agent.agent.adapter.protocol import IncomingMessage

        class TestAdapter(BaseChannelAdapter):
            adapter_name = "test"

            def start(self) -> None:
                pass

            def stop(self) -> None:
                pass

            def send_message(self, user_id: str, message) -> bool:
                return True

        adapter = TestAdapter({})
        # 不注册回调

        msg = IncomingMessage(
            channel="test",
            user_id="u1",
            message_id="m1",
            content="test",
        )
        # 不应该抛出
        adapter._dispatch(msg)