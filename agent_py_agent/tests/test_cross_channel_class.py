"""session/cross_channel.py 单元测试。

测试跨通道消息、状态同步。
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest


class TestChannelInfo:
    """测试 ChannelInfo 数据类。"""

    def test_channel_info_default(self):
        """验证默认 ChannelInfo。"""
        from agent_py_agent.agent.session.cross_channel import ChannelInfo

        info = ChannelInfo()
        assert info.active is False
        assert info.last_active_at == 0

    def test_channel_info_to_dict(self):
        """验证转字典。"""
        from agent_py_agent.agent.session.cross_channel import ChannelInfo

        info = ChannelInfo(active=True, last_active_at=1234567890.0)
        data = info.to_dict()

        assert data["active"] is True
        assert data["last_active_at"] == 1234567890.0

    def test_channel_info_from_dict(self):
        """验证从字典创建。"""
        from agent_py_agent.agent.session.cross_channel import ChannelInfo

        data = {"active": True, "last_active_at": 1234567890.0}
        info = ChannelInfo.from_dict(data)

        assert info.active is True
        assert info.last_active_at == 1234567890.0


class TestCrossChannelSessionInit:
    """测试 CrossChannelSession 初始化。"""

    def test_init_loads_config(self):
        """验证初始化时加载配置。"""
        from agent_py_agent.agent.session.cross_channel import CrossChannelSession

        mock_config = MagicMock()
        mock_config.session_workspace = "/tmp/sessions"

        manager = CrossChannelSession(mock_config)
        assert manager.config == mock_config


class TestBindSession:
    """测试 bind_session() 方法。"""

    def test_bind_creates_new_channels_file(self, tmp_path: Path):
        """验证绑定创建通道配置文件。"""
        from agent_py_agent.agent.session.cross_channel import CrossChannelSession

        mock_config = MagicMock()
        mock_config.session_workspace = str(tmp_path)
        mock_config.user_id = "user_123"

        manager = CrossChannelSession(mock_config)
        result = manager.bind_session("session_abc", "feishu", "user_123")

        assert result is True
        channels_file = tmp_path / "session_abc" / "channels.json"
        assert channels_file.exists()

    def test_bind_updates_existing_channel(self, tmp_path: Path):
        """验证绑定更新已存在的通道。"""
        from agent_py_agent.agent.session.cross_channel import CrossChannelSession

        mock_config = MagicMock()
        mock_config.session_workspace = str(tmp_path)
        mock_config.user_id = "user_123"

        # 创建初始会话
        session_dir = tmp_path / "session_xyz"
        session_dir.mkdir(parents=True, exist_ok=True)
        channels_file = session_dir / "channels.json"
        channels_file.write_text(
            json.dumps({
                "session_id": "session_xyz",
                "user_id": "user_123",
                "channels": {"qq": {"active": False, "last_active_at": 0}},
                "primary_channel": "qq",
            }),
            encoding="utf-8"
        )

        manager = CrossChannelSession(mock_config)
        result = manager.bind_session("session_xyz", "feishu")

        assert result is True
        data = json.loads(channels_file.read_text(encoding="utf-8"))
        assert data["channels"]["feishu"]["active"] is True


class TestUnbindChannel:
    """测试 unbind_channel() 方法。"""

    def test_unbind_deactivates_channel(self, tmp_path: Path):
        """验证解绑停用通道。"""
        from agent_py_agent.agent.session.cross_channel import CrossChannelSession

        mock_config = MagicMock()
        mock_config.session_workspace = str(tmp_path)
        mock_config.user_id = "user_123"

        # 创建会话
        session_dir = tmp_path / "session_abc"
        session_dir.mkdir(parents=True, exist_ok=True)
        channels_file = session_dir / "channels.json"
        channels_file.write_text(
            json.dumps({
                "session_id": "session_abc",
                "user_id": "user_123",
                "channels": {
                    "feishu": {"active": True, "last_active_at": 1234567890.0},
                    "qq": {"active": False, "last_active_at": 0},
                },
                "primary_channel": "feishu",
            }),
            encoding="utf-8"
        )

        manager = CrossChannelSession(mock_config)
        result = manager.unbind_channel("session_abc", "feishu")

        assert result is True
        data = json.loads(channels_file.read_text(encoding="utf-8"))
        assert data["channels"]["feishu"]["active"] is False


class TestGetBoundSessions:
    """测试 get_bound_sessions() 方法。"""

    def test_get_bound_sessions(self, tmp_path: Path):
        """验证获取绑定的通道列表。"""
        from agent_py_agent.agent.session.cross_channel import CrossChannelSession

        mock_config = MagicMock()
        mock_config.session_workspace = str(tmp_path)

        # 创建会话
        session_dir = tmp_path / "session_abc"
        session_dir.mkdir(parents=True, exist_ok=True)
        channels_file = session_dir / "channels.json"
        channels_file.write_text(
            json.dumps({
                "session_id": "session_abc",
                "user_id": "user_123",
                "channels": {
                    "feishu": {"active": True, "last_active_at": 1234567890.0},
                    "qq": {"active": False, "last_active_at": 0},
                },
            }),
            encoding="utf-8"
        )

        manager = CrossChannelSession(mock_config)
        result = manager.get_bound_sessions("session_abc")

        assert len(result) == 2


class TestTransferSession:
    """测试 transfer_session() 方法。"""

    def test_transfer_switches_channel(self, tmp_path: Path):
        """验证切换通道。"""
        from agent_py_agent.agent.session.cross_channel import CrossChannelSession

        mock_config = MagicMock()
        mock_config.session_workspace = str(tmp_path)

        # 创建会话
        session_dir = tmp_path / "session_transfer"
        session_dir.mkdir(parents=True, exist_ok=True)
        channels_file = session_dir / "channels.json"
        channels_file.write_text(
            json.dumps({
                "session_id": "session_transfer",
                "user_id": "user_123",
                "channels": {
                    "feishu": {"active": True, "last_active_at": 1234567890.0},
                },
                "primary_channel": "feishu",
            }),
            encoding="utf-8"
        )

        manager = CrossChannelSession(mock_config)
        result = manager.transfer_session("session_transfer", "feishu", "qq")

        assert result is True
        data = json.loads(channels_file.read_text(encoding="utf-8"))
        assert data["channels"]["feishu"]["active"] is False
        assert data["channels"]["qq"]["active"] is True
        assert data["primary_channel"] == "qq"


class TestGetPrimaryChannel:
    """测试 get_primary_channel() 方法。"""

    def test_get_primary_channel(self, tmp_path: Path):
        """验证获取主通道。"""
        from agent_py_agent.agent.session.cross_channel import CrossChannelSession

        mock_config = MagicMock()
        mock_config.session_workspace = str(tmp_path)

        # 创建会话
        session_dir = tmp_path / "session_primary"
        session_dir.mkdir(parents=True, exist_ok=True)
        channels_file = session_dir / "channels.json"
        channels_file.write_text(
            json.dumps({
                "session_id": "session_primary",
                "user_id": "user_123",
                "channels": {},
                "primary_channel": "feishu",
            }),
            encoding="utf-8"
        )

        manager = CrossChannelSession(mock_config)
        result = manager.get_primary_channel("session_primary")

        assert result == "feishu"


class TestListSessionsByChannel:
    """测试 list_sessions_by_channel() 方法。"""

    def test_list_sessions_by_channel(self, tmp_path: Path):
        """验证按通道列出会话。"""
        from agent_py_agent.agent.session.cross_channel import CrossChannelSession

        mock_config = MagicMock()
        mock_config.session_workspace = str(tmp_path)

        # 创建两个会话
        for sid in ["s1", "s2"]:
            session_dir = tmp_path / sid
            session_dir.mkdir(parents=True, exist_ok=True)
            channels_file = session_dir / "channels.json"
            channels_file.write_text(
                json.dumps({
                    "session_id": sid,
                    "user_id": "user_123",
                    "channels": {"feishu": {"active": True, "last_active_at": 1234567890.0}},
                }),
                encoding="utf-8"
            )

        manager = CrossChannelSession(mock_config)
        result = manager.list_sessions_by_channel("user_123", "feishu")

        assert len(result) == 2

class TestCrossChannelPathEscapes:
    """G1：session_id 拼路径前必须过拒绝式校验（B.2），与 SessionManager 同款。"""

    def _manager(self, tmp_path):
        from agent_py_agent.agent.session.cross_channel import CrossChannelSession

        mock_config = MagicMock()
        mock_config.session_workspace = str(tmp_path)
        return CrossChannelSession(mock_config)

    @pytest.mark.parametrize(
        "malicious",
        [
            "../../etc/passwd",
            "/etc/passwd",
            "..",
            ".",
            "sess_1\\evil",
            "sess_1 evil",
            "sess_" + "A" * 200,
        ],
    )
    def test_malicious_session_id_rejected_before_path_join(self, tmp_path, malicious):
        from agent_py_agent.agent.session.cross_channel import CrossChannelSession

        manager = self._manager(tmp_path)
        with pytest.raises(ValueError):
            manager._get_channels_path(malicious)
        # 拒绝发生在路径拼接之前：会话根目录下不得出现任何越界痕迹。
        assert list(tmp_path.iterdir()) == []
