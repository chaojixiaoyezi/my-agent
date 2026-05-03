"""adapter/manager.py 单元测试。

测试适配器注册、启停管理。
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestChannelManagerInit:
    """测试 ChannelManager 初始化。"""

    def test_default_gateway_port(self):
        """验证默认 gateway 端口。"""
        from agent_py_agent.agent.adapter.manager import ChannelManager

        manager = ChannelManager()
        assert manager.gateway_port == 8420

    def test_custom_gateway_port(self):
        """验证自定义 gateway 端口。"""
        from agent_py_agent.agent.adapter.manager import ChannelManager

        manager = ChannelManager(gateway_port=9000)
        assert manager.gateway_port == 9000

    def test_empty_adapters_dict(self):
        """验证初始适配器字典为空。"""
        from agent_py_agent.agent.adapter.manager import ChannelManager

        manager = ChannelManager()
        assert manager._adapters == {}


class TestAdapterRegistration:
    """测试适配器注册。"""

    def test_register_adapter(self):
        """验证注册适配器。"""
        from agent_py_agent.agent.adapter.manager import ChannelManager

        manager = ChannelManager()
        adapter = MagicMock()
        adapter.adapter_name = "test_adapter"

        manager.register_adapter(adapter)

        assert "test_adapter" in manager._adapters

    def test_register_replaces_existing(self):
        """验证重复注册时替换。"""
        from agent_py_agent.agent.adapter.manager import ChannelManager

        manager = ChannelManager()
        adapter1 = MagicMock()
        adapter1.adapter_name = "test"

        adapter2 = MagicMock()
        adapter2.adapter_name = "test"

        manager.register_adapter(adapter1)
        manager.register_adapter(adapter2)

        assert manager._adapters["test"] is adapter2

    def test_get_adapter(self):
        """验证获取适配器。"""
        from agent_py_agent.agent.adapter.manager import ChannelManager

        manager = ChannelManager()
        adapter = MagicMock()
        adapter.adapter_name = "feishu"

        manager.register_adapter(adapter)
        result = manager.get_adapter("feishu")

        assert result is adapter

    def test_get_nonexistent_adapter(self):
        """验证获取不存在的适配器返回 None。"""
        from agent_py_agent.agent.adapter.manager import ChannelManager

        manager = ChannelManager()
        result = manager.get_adapter("nonexistent")

        assert result is None

    def test_list_adapters(self):
        """验证列出所有适配器。"""
        from agent_py_agent.agent.adapter.manager import ChannelManager

        manager = ChannelManager()

        adapter1 = MagicMock()
        adapter1.adapter_name = "feishu"

        adapter2 = MagicMock()
        adapter2.adapter_name = "qq"

        manager.register_adapter(adapter1)
        manager.register_adapter(adapter2)

        result = manager.list_adapters()

        assert "feishu" in result
        assert "qq" in result


class TestStartStopAll:
    """测试启动和停止所有适配器。"""

    def test_start_all(self):
        """验证启动所有适配器。"""
        from agent_py_agent.agent.adapter.manager import ChannelManager

        manager = ChannelManager()
        adapter = MagicMock()
        adapter.adapter_name = "test"
        adapter.running = False

        manager.register_adapter(adapter)
        manager.start_all()

        adapter.start.assert_called_once()

    def test_stop_all(self):
        """验证停止所有适配器。"""
        from agent_py_agent.agent.adapter.manager import ChannelManager

        manager = ChannelManager()
        adapter = MagicMock()
        adapter.adapter_name = "test"
        adapter.running = True

        manager.register_adapter(adapter)
        manager.stop_all()

        adapter.stop.assert_called_once()

    def test_start_all_handles_exception(self):
        """验证启动失败时不抛出异常。"""
        from agent_py_agent.agent.adapter.manager import ChannelManager

        manager = ChannelManager()
        adapter = MagicMock()
        adapter.adapter_name = "test"
        adapter.running = False
        adapter.start.side_effect = RuntimeError("connection failed")

        manager.register_adapter(adapter)
        manager.start_all()  # 不应该抛出

        adapter.start.assert_called_once()


class TestActiveChannel:
    """测试活跃通道查询。"""

    def test_no_session_file(self):
        """无会话文件时返回 None。"""
        from agent_py_agent.agent.adapter.manager import ChannelManager

        manager = ChannelManager()
        result = manager.get_active_channel("user_123")

        assert result is None

    def test_get_active_channel(self, tmp_path: Path):
        """验证获取活跃通道。"""
        from agent_py_agent.agent.adapter.manager import ChannelManager
        import json

        session_file = tmp_path / "active_channels.json"
        session_file.write_text(json.dumps({"user_123": "feishu"}), encoding="utf-8")

        manager = ChannelManager()
        manager._session_channel_file = session_file

        result = manager.get_active_channel("user_123")
        assert result == "feishu"

    def test_get_active_channel_missing_user(self, tmp_path: Path):
        """用户不存在时返回 None。"""
        from agent_py_agent.agent.adapter.manager import ChannelManager
        import json

        session_file = tmp_path / "active_channels.json"
        session_file.write_text(json.dumps({"other_user": "feishu"}), encoding="utf-8")

        manager = ChannelManager()
        manager._session_channel_file = session_file

        result = manager.get_active_channel("user_123")
        assert result is None