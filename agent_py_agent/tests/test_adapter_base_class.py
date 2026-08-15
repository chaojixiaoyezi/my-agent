"""adapter/base.py 单元测试。

测试适配器基类、生命周期。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


class TestBaseChannelAdapter:
    """测试 BaseChannelAdapter 抽象基类。"""

    def test_adapter_name_default(self):
        """验证默认适配器名称。"""
        from agent_py_agent.agent.adapter.base import BaseChannelAdapter

        class TestAdapter(BaseChannelAdapter):
            def start(self) -> None:
                pass

            def stop(self) -> None:
                pass

            def send_message(self, user_id: str, message) -> bool:
                return True

        adapter = TestAdapter({})
        assert adapter.adapter_name == "base"

    def test_running_initial_false(self):
        """验证初始 running 状态为 False。"""
        from agent_py_agent.agent.adapter.base import BaseChannelAdapter

        class TestAdapter(BaseChannelAdapter):
            def start(self) -> None:
                pass

            def stop(self) -> None:
                pass

            def send_message(self, user_id: str, message) -> bool:
                return True

        adapter = TestAdapter({})
        assert adapter.running is False

    def test_config_stored(self):
        """验证配置被存储。"""
        from agent_py_agent.agent.adapter.base import BaseChannelAdapter

        class TestAdapter(BaseChannelAdapter):
            def start(self) -> None:
                pass

            def stop(self) -> None:
                pass

            def send_message(self, user_id: str, message) -> bool:
                return True

        config = {"token": "abc", "secret": "xyz"}
        adapter = TestAdapter(config)
        assert adapter.config == config


class TestAdapterMessageCallback:
    """测试消息回调机制。"""

    def test_on_message_registers_callback(self):
        """验证注册消息回调。"""
        from agent_py_agent.agent.adapter.base import BaseChannelAdapter

        class TestAdapter(BaseChannelAdapter):
            def start(self) -> None:
                pass

            def stop(self) -> None:
                pass

            def send_message(self, user_id: str, message) -> bool:
                return True

        adapter = TestAdapter({})
        callback = MagicMock()
        adapter.on_message(callback)
        assert adapter._message_callback is callback

    def test_dispatch_calls_callback(self):
        """验证 _dispatch 调用注册的回调。"""
        from agent_py_agent.agent.adapter.base import BaseChannelAdapter
        from agent_py_agent.agent.adapter.protocol import IncomingMessage

        class TestAdapter(BaseChannelAdapter):
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
            user_id="user_123",
            message_id="msg_456",
            content="hello",
        )
        adapter._dispatch(msg)

        callback.assert_called_once_with(msg)


class TestAbstractMethods:
    """测试抽象方法子类必须实现。"""

    def test_start_is_abstract(self):
        """验证 start 是抽象方法。"""
        from agent_py_agent.agent.adapter.base import BaseChannelAdapter

        with pytest.raises(TypeError):
            adapter = BaseChannelAdapter({})

    def test_stop_is_abstract(self):
        """验证 stop 是抽象方法。"""
        from agent_py_agent.agent.adapter.base import BaseChannelAdapter

        with pytest.raises(TypeError):
            adapter = BaseChannelAdapter({})

    def test_send_message_is_abstract(self):
        """验证 send_message 是抽象方法。"""
        from agent_py_agent.agent.adapter.base import BaseChannelAdapter

        with pytest.raises(TypeError):
            adapter = BaseChannelAdapter({})


class TestConcreteAdapter:
    """测试具体适配器实现。"""

    def test_concrete_adapter_implements_abstract_methods(self):
        """验证具体适配器实现了抽象方法。"""
        from agent_py_agent.agent.adapter.base import BaseChannelAdapter
        from agent_py_agent.agent.adapter.protocol import IncomingMessage, OutgoingMessage

        class ConcreteAdapter(BaseChannelAdapter):
            adapter_name = "test"

            def start(self) -> None:
                self._running = True

            def stop(self) -> None:
                self._running = False

            def send_message(self, user_id: str, message: OutgoingMessage) -> bool:
                return True

        adapter = ConcreteAdapter({})
        assert adapter.adapter_name == "test"
        assert adapter.running is False

        adapter.start()
        assert adapter.running is True

        adapter.stop()
        assert adapter.running is False

    def test_adapter_name_property(self):
        """验证适配器名称属性。"""
        from agent_py_agent.agent.adapter.base import BaseChannelAdapter

        class NamedAdapter(BaseChannelAdapter):
            adapter_name = "custom_name"

            def start(self) -> None:
                pass

            def stop(self) -> None:
                pass

            def send_message(self, user_id: str, message) -> bool:
                return True

        adapter = NamedAdapter({})
        assert adapter.adapter_name == "custom_name"