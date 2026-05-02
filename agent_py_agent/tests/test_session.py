"""会话持久化功能测试。

测试 Session 数据类、SessionManager 和 resume_session 功能。
"""
import json
from pathlib import Path
from unittest.mock import Mock, patch
import pytest
import time

from agent_py_agent.agent.session import Session, generate_session_id, SessionManager
from agent_py_agent.agent.session.manager import SessionManager
from agent_py_agent.agent.session.resume import resume_session, format_resume_context


@pytest.fixture
def mock_config(tmp_path):
    """创建模拟的配置对象。"""
    config = Mock()
    config.session_workspace = str(tmp_path / "sessions")
    config.user_id = "test_user"
    return config


@pytest.fixture
def manager(mock_config):
    """创建 SessionManager 实例。"""
    return SessionManager(mock_config)


class TestSessionModel:
    """测试 Session 数据类。"""

    def test_generate_session_id_format(self):
        """测试 session_id 格式。"""
        session_id = generate_session_id()
        assert session_id.startswith("sess_")
        parts = session_id.split("_")
        assert len(parts) == 3
        # 第二部分是时间戳
        assert parts[1].isdigit()
        # 第三部分是 4 位十六进制
        assert len(parts[2]) == 4

    def test_session_creation(self):
        """测试 Session 创建。"""
        session = Session(
            session_id="sess_1234567890_abcd",
            user_id="admin",
            created_at=1234567890.0,
            updated_at=1234567891.0,
            last_active_channel="chat",
            metadata={"key": "value"},
        )
        assert session.session_id == "sess_1234567890_abcd"
        assert session.user_id == "admin"
        assert session.created_at == 1234567890.0
        assert session.updated_at == 1234567891.0
        assert session.last_active_channel == "chat"
        assert session.metadata == {"key": "value"}

    def test_session_touch(self):
        """测试 touch 方法更新时间。"""
        session = Session(
            session_id="sess_1234567890_abcd",
            user_id="admin",
            created_at=1234567890.0,
            updated_at=1234567891.0,
        )
        old_updated = session.updated_at
        time.sleep(0.01)  # 确保时间戳不同
        session.touch(channel="gateway")
        assert session.updated_at > old_updated
        assert session.last_active_channel == "gateway"

    def test_session_to_dict(self):
        """测试 to_dict 方法。"""
        session = Session(
            session_id="sess_1234567890_abcd",
            user_id="admin",
            created_at=1234567890.0,
            updated_at=1234567891.0,
        )
        data = session.to_dict()
        assert data["session_id"] == "sess_1234567890_abcd"
        assert data["user_id"] == "admin"
        assert data["created_at"] == 1234567890.0
        assert data["updated_at"] == 1234567891.0

    def test_session_from_dict(self):
        """测试 from_dict 方法。"""
        data = {
            "session_id": "sess_1234567890_abcd",
            "user_id": "admin",
            "created_at": 1234567890.0,
            "updated_at": 1234567891.0,
            "last_active_channel": "chat",
            "metadata": {"key": "value"},
        }
        session = Session.from_dict(data)
        assert session.session_id == "sess_1234567890_abcd"
        assert session.user_id == "admin"
        assert session.metadata == {"key": "value"}

    def test_session_post_init_corrects_timestamp(self):
        """测试 __post_init__ 修正时间戳。"""
        session = Session(
            session_id="sess_1234567890_abcd",
            user_id="admin",
            created_at=1234567891.0,  # updated_at 比 created_at 大
            updated_at=1234567890.0,
        )
        assert session.updated_at == session.created_at


class TestSessionManager:
    """测试 SessionManager 类。"""

    def test_create_session(self, manager):
        """测试创建会话。"""
        session = manager.create_session(
            user_id="test_user",
            channel="chat",
            metadata={"test": "data"},
        )
        assert session.session_id.startswith("sess_")
        assert session.user_id == "test_user"
        assert session.last_active_channel == "chat"
        assert session.metadata == {"test": "data"}
        # 验证文件被创建
        session_file = manager._get_session_path(session.session_id)
        assert session_file.exists()

    def test_create_session_default_user(self, manager):
        """测试使用默认 user_id 创建会话。"""
        session = manager.create_session()
        assert session.user_id == manager.config.user_id

    def test_load_session(self, manager):
        """测试加载会话。"""
        session = manager.create_session(user_id="test_user")
        loaded = manager.load_session(session.session_id)
        assert loaded is not None
        assert loaded.session_id == session.session_id
        assert loaded.user_id == "test_user"

    def test_load_nonexistent_session(self, manager):
        """测试加载不存在的会话。"""
        loaded = manager.load_session("sess_nonexistent")
        assert loaded is None

    def test_save_session(self, manager):
        """测试保存会话。"""
        session = Session(
            session_id="sess_test_save",
            user_id="test_user",
            created_at=1234567890.0,
            updated_at=1234567890.0,
        )
        session.metadata = {"saved": True}
        manager.save_session(session)
        loaded = manager.load_session("sess_test_save")
        assert loaded is not None
        assert loaded.metadata == {"saved": True}

    def test_touch_session(self, manager):
        """测试更新会话活跃时间。"""
        session = manager.create_session(user_id="test_user")
        old_updated = session.updated_at
        time.sleep(0.01)
        result = manager.touch_session(session.session_id, channel="gateway")
        assert result is True
        loaded = manager.load_session(session.session_id)
        assert loaded.updated_at > old_updated
        assert loaded.last_active_channel == "gateway"

    def test_touch_nonexistent_session(self, manager):
        """测试更新不存在的会话。"""
        result = manager.touch_session("sess_nonexistent")
        assert result is False

    def test_list_sessions(self, manager):
        """测试列出会话。"""
        session1 = manager.create_session(user_id="test_user")
        time.sleep(0.01)
        session2 = manager.create_session(user_id="test_user")
        time.sleep(0.01)
        session3 = manager.create_session(user_id="other_user")

        sessions = manager.list_sessions("test_user")
        assert len(sessions) == 2
        # 验证按时间倒序
        assert sessions[0].session_id == session2.session_id
        assert sessions[1].session_id == session1.session_id

    def test_list_sessions_default_user(self, manager):
        """测试使用默认 user_id 列出会话。"""
        session1 = manager.create_session()
        session2 = manager.create_session()
        sessions = manager.list_sessions()
        assert len(sessions) == 2

    def test_session_exists(self, manager):
        """测试检查会话是否存在。"""
        session = manager.create_session(user_id="test_user")
        assert manager.session_exists(session.session_id) is True
        assert manager.session_exists("sess_nonexistent") is False

    def test_delete_session(self, manager):
        """测试删除会话。"""
        session = manager.create_session(user_id="test_user")
        assert manager.session_exists(session.session_id) is True

        result = manager.delete_session(session.session_id)
        assert result is True
        assert manager.session_exists(session.session_id) is False

    def test_delete_nonexistent_session(self, manager):
        """测试删除不存在的会话。"""
        result = manager.delete_session("sess_nonexistent")
        assert result is False


class TestResumeSession:
    """测试会话恢复功能。"""

    @pytest.fixture
    def mock_agent(self, tmp_path):
        """创建模拟的 Agent 实例。"""
        agent = Mock()
        config = Mock()
        config.session_workspace = str(tmp_path / "sessions")
        config.user_id = "test_user"
        config.memory_path = str(tmp_path / "memory.jsonl")
        agent.config = config
        agent.root = tmp_path
        return agent

    def test_resume_session_with_metadata(self, mock_agent):
        """测试恢复带元数据的会话。"""
        manager = SessionManager(mock_agent.config)
        session = manager.create_session(
            user_id="test_user",
            metadata={"context": "test"},
        )

        resume_data = resume_session(mock_agent, session.session_id)
        assert resume_data["session"] is not None
        assert resume_data["session"].session_id == session.session_id
        assert resume_data["session"].metadata == {"context": "test"}

    def test_resume_nonexistent_session(self, mock_agent):
        """测试恢复不存在的会话。"""
        resume_data = resume_session(mock_agent, "sess_nonexistent")
        assert resume_data["session"] is None
        assert "error" in resume_data

    def test_format_resume_context_with_session(self):
        """测试格式化恢复上下文（有会话）。"""
        resume_data = {
            "session": Mock(
                session_id="sess_test",
                created_at=1234567890.0,
                updated_at=1234567891.0,
                last_active_channel="chat",
            ),
            "recent_memories": [
                {"role": "user", "content": "测试记忆内容"},
            ],
            "subagent_context": [
                {"id": "sub-001", "goal": "测试任务", "status": "DONE"},
            ],
        }
        formatted = format_resume_context(resume_data)
        assert "会话 ID: sess_test" in formatted
        assert "最近记忆 (1 条)" in formatted
        assert "子代理任务 (1 条)" in formatted

    def test_format_resume_context_with_error(self):
        """测试格式化恢复上下文（有错误）。"""
        resume_data = {
            "session": None,
            "error": "会话不存在",
        }
        formatted = format_resume_context(resume_data)
        assert "恢复失败: 会话不存在" in formatted

    def test_format_resume_context_no_records(self):
        """测试格式化恢复上下文（无记录）。"""
        resume_data = {
            "session": Mock(
                session_id="sess_test",
                created_at=1234567890.0,
                updated_at=1234567891.0,
                last_active_channel="chat",
            ),
            "recent_memories": [],
            "subagent_context": [],
        }
        formatted = format_resume_context(resume_data)
        assert "会话 ID: sess_test" in formatted
        assert "暂无历史记录" in formatted


class TestSessionIntegration:
    """集成测试场景。"""

    def test_full_lifecycle(self, manager):
        """测试完整生命周期：创建 → 保存 → 加载 → 更新 → 删除。"""
        # 创建
        session = manager.create_session(user_id="test_user", metadata={"step": "created"})
        session_id = session.session_id

        # 验证创建
        assert manager.session_exists(session_id) is True

        # 加载
        loaded = manager.load_session(session_id)
        assert loaded.metadata["step"] == "created"

        # 更新
        loaded.metadata["step"] = "updated"
        loaded.touch()
        manager.save_session(loaded)

        # 验证更新
        updated = manager.load_session(session_id)
        assert updated.metadata["step"] == "updated"
        assert updated.updated_at > loaded.created_at

        # 列表
        sessions = manager.list_sessions("test_user")
        assert len(sessions) == 1
        assert sessions[0].session_id == session_id

        # 删除
        result = manager.delete_session(session_id)
        assert result is True
        assert manager.session_exists(session_id) is False

    def test_multiple_users_isolation(self, manager):
        """测试多用户隔离。"""
        # 创建不同用户的会话
        session1 = manager.create_session(user_id="user1")
        session2 = manager.create_session(user_id="user2")
        session3 = manager.create_session(user_id="user1")

        # 验证用户隔离
        user1_sessions = manager.list_sessions("user1")
        user2_sessions = manager.list_sessions("user2")

        assert len(user1_sessions) == 2
        assert len(user2_sessions) == 1
        assert session2.session_id in [s.session_id for s in user2_sessions]
        assert session2.session_id not in [s.session_id for s in user1_sessions]

    def test_concurrent_sessions(self, manager):
        """测试并发会话（不同会话 ID）。"""
        sessions = [manager.create_session() for _ in range(5)]
        session_ids = [s.session_id for s in sessions]

        # 验证会话 ID 都不同
        assert len(set(session_ids)) == 5

        # 验证所有会话都可以加载
        for sid in session_ids:
            assert manager.load_session(sid) is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
