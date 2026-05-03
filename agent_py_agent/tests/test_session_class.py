"""session/manager.py 单元测试。

测试会话管理、状态保持。
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest


class TestSessionManagerInit:
    """测试 SessionManager 初始化。"""

    def test_init_sets_config(self):
        """验证初始化时设置配置。"""
        from agent_py_agent.agent.session.manager import SessionManager

        mock_config = MagicMock()
        mock_config.session_workspace = "/tmp/sessions"

        manager = SessionManager(mock_config)
        assert manager.config == mock_config


class TestCreateSession:
    """测试 create_session() 方法。"""

    def test_create_session_returns_session(self, tmp_path: Path):
        """验证创建会话返回 Session 对象。"""
        from agent_py_agent.agent.session.manager import SessionManager

        mock_config = MagicMock()
        mock_config.session_workspace = str(tmp_path / "sessions")
        mock_config.user_id = "default_user"

        manager = SessionManager(mock_config)
        session = manager.create_session(user_id="user_123", channel="chat")

        assert session.user_id == "user_123"
        assert session.last_active_channel == "chat"

    def test_create_session_creates_directory(self, tmp_path: Path):
        """验证创建会话时创建目录。"""
        from agent_py_agent.agent.session.manager import SessionManager

        mock_config = MagicMock()
        mock_config.session_workspace = str(tmp_path / "sessions")
        mock_config.user_id = "test_user"

        manager = SessionManager(mock_config)
        session = manager.create_session()

        session_dir = tmp_path / "sessions" / session.session_id
        assert session_dir.exists()


class TestLoadSession:
    """测试 load_session() 方法。"""

    def test_load_existing_session(self, tmp_path: Path):
        """验证加载已存在的会话。"""
        from agent_py_agent.agent.session.manager import SessionManager

        mock_config = MagicMock()
        mock_config.session_workspace = str(tmp_path / "sessions")

        manager = SessionManager(mock_config)
        original = manager.create_session(user_id="user_load")

        loaded = manager.load_session(original.session_id)

        assert loaded is not None
        assert loaded.session_id == original.session_id

    def test_load_nonexistent_returns_none(self, tmp_path: Path):
        """加载不存在的会话返回 None。"""
        from agent_py_agent.agent.session.manager import SessionManager

        mock_config = MagicMock()
        mock_config.session_workspace = str(tmp_path / "sessions")

        manager = SessionManager(mock_config)
        result = manager.load_session("nonexistent")

        assert result is None


class TestSaveSession:
    """测试 save_session() 方法。"""

    def test_save_session_updates_file(self, tmp_path: Path):
        """验证保存会话更新文件。"""
        from agent_py_agent.agent.session.manager import SessionManager
        from agent_py_agent.agent.session.models import Session

        mock_config = MagicMock()
        mock_config.session_workspace = str(tmp_path / "sessions")

        manager = SessionManager(mock_config)
        session = manager.create_session(user_id="user_save")

        # 修改后保存
        session.metadata = {"updated": True}
        manager.save_session(session)

        loaded = manager.load_session(session.session_id)
        assert loaded.metadata.get("updated") is True


class TestTouchSession:
    """测试 touch_session() 方法。"""

    def test_touch_updates_timestamp(self, tmp_path: Path):
        """验证触摸更新会话时间戳。"""
        from agent_py_agent.agent.session.manager import SessionManager

        mock_config = MagicMock()
        mock_config.session_workspace = str(tmp_path / "sessions")

        manager = SessionManager(mock_config)
        session = manager.create_session(user_id="user_touch")

        original_updated_at = session.updated_at
        result = manager.touch_session(session.session_id, channel="feishu")

        assert result is True
        loaded = manager.load_session(session.session_id)
        assert loaded.updated_at >= original_updated_at


class TestListSessions:
    """测试 list_sessions() 方法。"""

    def test_list_sessions_returns_user_sessions(self, tmp_path: Path):
        """验证列出用户的会话。"""
        from agent_py_agent.agent.session.manager import SessionManager

        mock_config = MagicMock()
        mock_config.session_workspace = str(tmp_path / "sessions")
        mock_config.user_id = "list_user"

        manager = SessionManager(mock_config)

        # 创建多个会话
        for i in range(3):
            manager.create_session(user_id="list_user")

        sessions = manager.list_sessions("list_user")
        assert len(sessions) == 3


class TestDeleteSession:
    """测试 delete_session() 方法。"""

    def test_delete_existing_session(self, tmp_path: Path):
        """验证删除已存在的会话。"""
        from agent_py_agent.agent.session.manager import SessionManager

        mock_config = MagicMock()
        mock_config.session_workspace = str(tmp_path / "sessions")

        manager = SessionManager(mock_config)
        session = manager.create_session(user_id="user_del")

        result = manager.delete_session(session.session_id)
        assert result is True

        loaded = manager.load_session(session.session_id)
        assert loaded is None


class TestSessionExists:
    """测试 session_exists() 方法。"""

    def test_exists_returns_true_for_existing(self, tmp_path: Path):
        """已存在的会话返回 True。"""
        from agent_py_agent.agent.session.manager import SessionManager

        mock_config = MagicMock()
        mock_config.session_workspace = str(tmp_path / "sessions")
        mock_config.user_id = "test_user"

        manager = SessionManager(mock_config)
        session = manager.create_session()

        assert manager.session_exists(session.session_id) is True

    def test_exists_returns_false_for_nonexistent(self, tmp_path: Path):
        """不存在的会话返回 False。"""
        from agent_py_agent.agent.session.manager import SessionManager

        mock_config = MagicMock()
        mock_config.session_workspace = str(tmp_path / "sessions")

        manager = SessionManager(mock_config)

        assert manager.session_exists("nonexistent") is False


class TestSessionModels:
    """测试 Session 数据模型。"""

    def test_session_to_dict(self):
        """验证 Session 转字典。"""
        from agent_py_agent.agent.session.models import Session

        session = Session(
            session_id="sess_123",
            user_id="user_456",
            created_at=1234567890.0,
            updated_at=1234567900.0,
        )

        data = session.to_dict()
        assert data["session_id"] == "sess_123"
        assert data["user_id"] == "user_456"

    def test_session_from_dict(self):
        """验证从字典创建 Session。"""
        from agent_py_agent.agent.session.models import Session

        data = {
            "session_id": "sess_abc",
            "user_id": "user_def",
            "created_at": 1234567890.0,
            "updated_at": 1234567900.0,
            "last_active_channel": "qq",
            "metadata": {},
        }

        session = Session.from_dict(data)
        assert session.session_id == "sess_abc"
        assert session.last_active_channel == "qq"

    def test_session_touch(self):
        """验证 touch 更新时间和通道。"""
        from agent_py_agent.agent.session.models import Session

        session = Session(
            session_id="sess_touch",
            user_id="user_touch",
            created_at=1234567890.0,
            updated_at=1234567890.0,
        )

        original_updated = session.updated_at
        session.touch(channel="feishu")

        assert session.last_active_channel == "feishu"
        assert session.updated_at >= original_updated


class TestGenerateSessionId:
    """测试 generate_session_id() 函数。"""

    def test_generate_session_id_format(self):
        """验证生成的 ID 格式。"""
        from agent_py_agent.agent.session.models import generate_session_id

        session_id = generate_session_id()
        assert session_id.startswith("sess_")
        assert "_" in session_id

    def test_generate_session_id_unique(self):
        """验证生成的 ID 唯一性。"""
        from agent_py_agent.agent.session.models import generate_session_id

        ids = [generate_session_id() for _ in range(100)]
        assert len(set(ids)) == 100