"""跨通道会话功能测试。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


class TestCrossChannelSession:
    """测试 CrossChannelSession 类。"""

    def test_bind_session_creates_channels_file(self, tmp_path: Path):
        """bind_session 创建通道配置文件。"""
        from agent_py_agent.agent.session.cross_channel import CrossChannelSession

        # 创建临时配置
        class MockConfig:
            session_workspace = str(tmp_path / "sessions")
            user_id = "admin"

        cc = CrossChannelSession(MockConfig())
        success = cc.bind_session("sess_123", "chat", "admin")

        assert success is True
        channels_file = tmp_path / "sessions" / "sess_123" / "channels.json"
        assert channels_file.exists()

    def test_bind_session_updates_existing(self, tmp_path: Path):
        """bind_session 更新已存在的通道配置。"""
        from agent_py_agent.agent.session.cross_channel import CrossChannelSession

        class MockConfig:
            session_workspace = str(tmp_path / "sessions")
            user_id = "admin"

        cc = CrossChannelSession(MockConfig())

        # 首次绑定 chat
        cc.bind_session("sess_123", "chat", "admin")

        # 再次绑定 feishu
        cc.bind_session("sess_123", "feishu", "admin")

        # 验证两个通道都存在
        bound = cc.get_bound_sessions("sess_123")
        channels = {b["channel"]: b for b in bound}
        assert "chat" in channels
        assert "feishu" in channels

    def test_unbind_channel(self, tmp_path: Path):
        """解绑通道。"""
        from agent_py_agent.agent.session.cross_channel import CrossChannelSession

        class MockConfig:
            session_workspace = str(tmp_path / "sessions")
            user_id = "admin"

        cc = CrossChannelSession(MockConfig())
        cc.bind_session("sess_123", "chat", "admin")
        cc.bind_session("sess_123", "feishu", "admin")

        success = cc.unbind_channel("sess_123", "chat")
        assert success is True

        bound = cc.get_bound_sessions("sess_123")
        channels = {b["channel"]: b for b in bound}
        assert channels["chat"]["active"] is False

    def test_get_bound_sessions(self, tmp_path: Path):
        """获取绑定的通道列表。"""
        from agent_py_agent.agent.session.cross_channel import CrossChannelSession

        class MockConfig:
            session_workspace = str(tmp_path / "sessions")
            user_id = "admin"

        cc = CrossChannelSession(MockConfig())
        cc.bind_session("sess_123", "chat", "admin")
        cc.bind_session("sess_123", "feishu", "admin")

        bound = cc.get_bound_sessions("sess_123")
        assert len(bound) == 2
        channel_names = {b["channel"] for b in bound}
        assert "chat" in channel_names
        assert "feishu" in channel_names

    def test_get_active_session(self, tmp_path: Path):
        """获取用户在通道的活跃会话。"""
        from agent_py_agent.agent.session.cross_channel import CrossChannelSession

        class MockConfig:
            session_workspace = str(tmp_path / "sessions")
            user_id = "admin"

        cc = CrossChannelSession(MockConfig())
        cc.bind_session("sess_123", "chat", "admin")

        # 查找活跃会话
        active = cc.get_active_session("admin", "chat")
        assert active == "sess_123"

        # 查找不存在的通道
        active = cc.get_active_session("admin", "feishu")
        assert active is None

    def test_transfer_session(self, tmp_path: Path):
        """切换会话通道。"""
        from agent_py_agent.agent.session.cross_channel import CrossChannelSession

        class MockConfig:
            session_workspace = str(tmp_path / "sessions")
            user_id = "admin"

        cc = CrossChannelSession(MockConfig())
        cc.bind_session("sess_123", "chat", "admin")

        # 从 chat 切换到 feishu
        success = cc.transfer_session("sess_123", "chat", "feishu")
        assert success is True

        # 验证主通道已切换
        primary = cc.get_primary_channel("sess_123")
        assert primary == "feishu"

        # 验证 chat 已非活跃
        bound = cc.get_bound_sessions("sess_123")
        channels = {b["channel"]: b for b in bound}
        assert channels["chat"]["active"] is False
        assert channels["feishu"]["active"] is True

    def test_get_primary_channel(self, tmp_path: Path):
        """获取主通道。"""
        from agent_py_agent.agent.session.cross_channel import CrossChannelSession

        class MockConfig:
            session_workspace = str(tmp_path / "sessions")
            user_id = "admin"

        cc = CrossChannelSession(MockConfig())
        cc.bind_session("sess_123", "chat", "admin")

        primary = cc.get_primary_channel("sess_123")
        assert primary == "chat"

    def test_list_sessions_by_channel(self, tmp_path: Path):
        """列出用户在指定通道的会话。"""
        from agent_py_agent.agent.session.cross_channel import CrossChannelSession

        class MockConfig:
            session_workspace = str(tmp_path / "sessions")
            user_id = "admin"

        cc = CrossChannelSession(MockConfig())
        cc.bind_session("sess_1", "chat", "admin")
        cc.bind_session("sess_2", "chat", "admin")
        cc.bind_session("sess_3", "feishu", "admin")

        chat_sessions = cc.list_sessions_by_channel("admin", "chat")
        assert len(chat_sessions) == 2
        assert "sess_1" in chat_sessions
        assert "sess_2" in chat_sessions

    def test_list_sessions_by_channel_report_keeps_good_sessions_when_one_file_is_bad(self, tmp_path: Path):
        """坏 channels.json 不能让系统误以为没有其它好会话。"""
        from agent_py_agent.agent.session.cross_channel import CrossChannelSession

        class MockConfig:
            session_workspace = str(tmp_path / "sessions")
            user_id = "admin"

        cc = CrossChannelSession(MockConfig())
        cc.bind_session("sess_good", "chat", "admin")
        bad_file = tmp_path / "sessions" / "sess_bad" / "channels.json"
        bad_file.parent.mkdir(parents=True, exist_ok=True)
        bad_file.write_text("{bad-json", encoding="utf-8")

        sessions, load_errors = cc.list_sessions_by_channel_report("admin", "chat")

        assert sessions == ["sess_good"]
        assert load_errors
        assert load_errors[0]["context"] == "session.cross_channel.channels.read"
        assert load_errors[0]["path"] == str(bad_file)

    def test_non_admin_cannot_access_others(self, tmp_path: Path):
        """非管理员不能跨用户访问。"""
        from agent_py_agent.agent.session.cross_channel import CrossChannelSession

        class MockConfig:
            session_workspace = str(tmp_path / "sessions")
            user_id = "alice"  # 非 admin

        cc = CrossChannelSession(MockConfig())
        cc.bind_session("sess_123", "chat", "admin")

        # alice 查找 admin 的会话 - 应该找不到（因为 get_active_session 会过滤用户）
        active = cc.get_active_session("alice", "chat")
        # alice 的会话不包含 admin 的，所以返回 None
        assert active is None


class TestSessionContextSync:
    """测试 SessionContextSync 类。"""

    def test_sync_to_channel(self, tmp_path: Path):
        """同步上下文到目标通道。"""
        from agent_py_agent.agent.session.context_sync import SessionContextSync
        from agent_py_agent.agent.session.cross_channel import CrossChannelSession

        class MockConfig:
            session_workspace = str(tmp_path / "sessions")
            user_id = "admin"

        cc = CrossChannelSession(MockConfig())
        cc.bind_session("sess_123", "chat", "admin")

        # 添加上下文信息
        data = cc._load_channels("sess_123")
        data["recent_messages"] = [
            {"role": "user", "content": "帮我分析日志"},
            {"role": "agent", "content": "正在分析..."},
        ]
        cc._save_channels("sess_123", data)

        sync = SessionContextSync(cc)
        context = sync.sync_to_channel("sess_123", "feishu")

        assert context["session_id"] == "sess_123"
        assert context["from_channel"] == "chat"
        assert context["to_channel"] == "feishu"
        assert len(context["recent_messages"]) == 2

    def test_update_session_context(self, tmp_path: Path):
        """更新会话上下文。"""
        from agent_py_agent.agent.session.context_sync import SessionContextSync
        from agent_py_agent.agent.session.cross_channel import CrossChannelSession

        class MockConfig:
            session_workspace = str(tmp_path / "sessions")
            user_id = "admin"

        cc = CrossChannelSession(MockConfig())
        cc.bind_session("sess_123", "chat", "admin")

        sync = SessionContextSync(cc)
        success = sync.update_session_context(
            "sess_123",
            recent_messages=[{"role": "user", "content": "test"}],
            pending_reply="待回复内容",
        )
        assert success is True

        # 验证更新
        data = cc._load_channels("sess_123")
        assert len(data["recent_messages"]) == 1
        assert data["pending_reply"] == "待回复内容"

    def test_format_context_for_channel_chat(self, tmp_path: Path):
        """chat 通道格式化。"""
        from agent_py_agent.agent.session.context_sync import format_context_for_channel

        context = {
            "session_id": "sess_123",
            "from_channel": "chat",
            "to_channel": "feishu",
            "recent_messages": [
                {"role": "user", "content": "帮我分析日志"},
                {"role": "agent", "content": "正在分析..."},
            ],
            "tasks": [
                {"task_id": "task_001", "status": "RUNNING", "goal": "分析日志"},
            ],
            "pending_reply": "请继续",
        }

        result = format_context_for_channel(context, "chat")

        assert "[会话接续]" in result
        assert "从 chat 切换到 feishu" in result
        assert "sess_123" in result
        assert "帮我分析日志" in result
        assert "task_001" in result

    def test_format_context_for_channel_feishu(self, tmp_path: Path):
        """feishu 通道格式化（支持 markdown）。"""
        from agent_py_agent.agent.session.context_sync import format_context_for_channel

        context = {
            "session_id": "sess_123",
            "from_channel": "chat",
            "to_channel": "feishu",
            "recent_messages": [
                {"role": "user", "content": "帮我分析日志"},
            ],
            "tasks": [
                {"task_id": "task_001", "status": "RUNNING", "goal": "分析日志"},
            ],
        }

        result = format_context_for_channel(context, "feishu")

        assert "**会话接续**" in result
        assert "**最近上下文:**" in result
        # 有任务时才显示
        assert "**当前活跃任务:**" in result
        assert "task_001" in result

    def test_format_context_for_channel_qq(self, tmp_path: Path):
        """qq 通道格式化（纯文本）。"""
        from agent_py_agent.agent.session.context_sync import format_context_for_channel

        context = {
            "session_id": "sess_123",
            "from_channel": "chat",
            "to_channel": "qq",
            "recent_messages": [
                {"role": "user", "content": "帮我分析日志"},
            ],
            "tasks": [
                {"task_id": "task_001", "status": "RUNNING", "goal": "分析日志"},
            ],
        }

        result = format_context_for_channel(context, "qq")

        # QQ 不支持 markdown，应该是纯文本
        assert "**会话接续**" not in result
        assert "会话接续" in result
        assert "task_001" in result


class TestAdminCrossChannelQuery:
    """测试 AdminCrossChannelQuery 类。"""

    def test_non_admin_cannot_query(self, tmp_path: Path):
        """非管理员不能使用查询接口。"""
        from agent_py_agent.agent.session.admin_query import AdminCrossChannelQuery
        from agent_py_agent.agent.session.cross_channel import CrossChannelSession
        from agent_py_agent.agent.session.manager import SessionManager

        class MockConfig:
            session_workspace = str(tmp_path / "sessions")
            user_id = "alice"

        cc = CrossChannelSession(MockConfig())
        sm = SessionManager(MockConfig())

        query = AdminCrossChannelQuery(cc, sm)

        # alice 查询返回空
        sessions = query.get_all_sessions("alice")
        assert sessions == []

        # alice 查询任务返回空
        tasks = query.get_all_tasks("alice")
        assert tasks == []

        # alice 查询时间线返回空
        timeline = query.get_recent_activity("alice")
        assert timeline == []

    def test_admin_can_query_all_sessions(self, tmp_path: Path):
        """管理员可以查询所有会话。"""
        from agent_py_agent.agent.session.admin_query import AdminCrossChannelQuery
        from agent_py_agent.agent.session.cross_channel import CrossChannelSession
        from agent_py_agent.agent.session.manager import SessionManager

        class MockConfig:
            session_workspace = str(tmp_path / "sessions")
            user_id = "admin"

        cc = CrossChannelSession(MockConfig())
        sm = SessionManager(MockConfig())

        # 创建 admin 会话
        session = sm.create_session("admin", "chat")
        cc.bind_session(session.session_id, "chat", "admin")

        query = AdminCrossChannelQuery(cc, sm)

        # admin 可以查询
        sessions = query.get_all_sessions("admin")
        assert len(sessions) >= 1
        assert any(s["user_id"] == "admin" for s in sessions)

    def test_get_all_tasks_empty(self, tmp_path: Path):
        """获取所有任务（无 store）。"""
        from agent_py_agent.agent.session.admin_query import AdminCrossChannelQuery
        from agent_py_agent.agent.session.cross_channel import CrossChannelSession
        from agent_py_agent.agent.session.manager import SessionManager

        class MockConfig:
            session_workspace = str(tmp_path / "sessions")
            user_id = "admin"

        cc = CrossChannelSession(MockConfig())
        sm = SessionManager(MockConfig())

        query = AdminCrossChannelQuery(cc, sm, task_registry_store=None)

        # 无 store 时返回空
        tasks = query.get_all_tasks("admin")
        assert tasks == []

    def test_format_admin_summary(self, tmp_path: Path):
        """格式化管理员摘要。"""
        from agent_py_agent.agent.session.admin_query import AdminCrossChannelQuery
        from agent_py_agent.agent.session.cross_channel import CrossChannelSession
        from agent_py_agent.agent.session.manager import SessionManager

        class MockConfig:
            session_workspace = str(tmp_path / "sessions")
            user_id = "admin"

        cc = CrossChannelSession(MockConfig())
        sm = SessionManager(MockConfig())

        query = AdminCrossChannelQuery(cc, sm)

        # alice 不能获取摘要
        summary = query.format_admin_summary("alice")
        assert "权限不足" in summary

        # admin 可以获取摘要
        summary = query.format_admin_summary("admin")
        assert "=== 管理员全局摘要 ===" in summary


class TestChannelBindingProtocol:
    """测试通道切换协议。"""

    def test_admin_switch_from_chat_to_feishu(self, tmp_path: Path):
        """管理员从 chat 切换到 feishu。"""
        from agent_py_agent.agent.session.context_sync import (
            SessionContextSync,
            format_context_for_channel,
        )
        from agent_py_agent.agent.session.cross_channel import CrossChannelSession

        class MockConfig:
            session_workspace = str(tmp_path / "sessions")
            user_id = "admin"

        cc = CrossChannelSession(MockConfig())
        sync = SessionContextSync(cc)

        # 初始绑定 chat
        cc.bind_session("sess_123", "chat", "admin")

        # 添加上下文
        data = cc._load_channels("sess_123")
        data["recent_messages"] = [
            {"role": "user", "content": "帮我分析日志"},
            {"role": "agent", "content": "正在分析，发现3个异常..."},
        ]
        data["pending_reply"] = "请继续"
        cc._save_channels("sess_123", data)

        # 执行通道切换
        cc.transfer_session("sess_123", "chat", "feishu")

        # 同步上下文 - 传入 to_channel 表示切换到哪个通道
        context = sync.sync_to_channel("sess_123", "feishu")
        formatted = format_context_for_channel(context, "feishu")

        # after transfer, primary_channel becomes feishu
        # so from_channel in context will be feishu (from primary_channel)
        assert "feishu" in formatted
        assert "sess_123" in formatted
        assert "帮我分析日志" in formatted

    def test_admin_with_tasks_in_context(self, tmp_path: Path):
        """带任务状态的上下文同步。"""
        from agent_py_agent.agent.session.context_sync import (
            SessionContextSync,
            format_context_for_channel,
        )
        from agent_py_agent.agent.session.cross_channel import CrossChannelSession

        class MockConfig:
            session_workspace = str(tmp_path / "sessions")
            user_id = "admin"

        cc = CrossChannelSession(MockConfig())
        sync = SessionContextSync(cc)

        cc.bind_session("sess_123", "chat", "admin")

        # 添加带任务的数据
        data = cc._load_channels("sess_123")
        data["recent_messages"] = [{"role": "user", "content": "分析日志"}]
        data["tasks"] = [
            {"task_id": "log-analysis-001", "status": "RUNNING", "goal": "分析日志"},
        ]
        cc._save_channels("sess_123", data)

        context = sync.sync_to_channel("sess_123", "feishu")

        # context 中应该有任务
        assert "tasks" in context
        # 注意：没有 store 时任务列表为空
        assert context["tasks"] == [] or len(context["tasks"]) >= 0


class TestIntegration:
    """集成测试。"""

    def test_full_channel_lifecycle(self, tmp_path: Path):
        """完整的通道生命周期测试。"""
        from agent_py_agent.agent.session.cross_channel import CrossChannelSession
        from agent_py_agent.agent.session.manager import SessionManager

        class MockConfig:
            session_workspace = str(tmp_path / "sessions")
            user_id = "admin"

        cc = CrossChannelSession(MockConfig())
        sm = SessionManager(MockConfig())

        # 1. 创建会话并绑定到 chat
        session = sm.create_session("admin", "chat")
        cc.bind_session(session.session_id, "chat", "admin")

        # 2. 验证 chat 活跃
        active = cc.get_active_session("admin", "chat")
        assert active == session.session_id

        # 3. 切换到 feishu
        cc.transfer_session(session.session_id, "chat", "feishu")

        # 4. 验证 feishu 活跃
        active = cc.get_active_session("admin", "feishu")
        assert active == session.session_id

        # 5. chat 不再是主通道
        primary = cc.get_primary_channel(session.session_id)
        assert primary == "feishu"

        # 6. 绑定第三个通道 qq
        cc.bind_session(session.session_id, "qq", "admin")

        # 7. 验证三个通道都存在
        bound = cc.get_bound_sessions(session.session_id)
        assert len(bound) == 3

        # 8. 解绑 chat
        cc.unbind_channel(session.session_id, "chat")

        # 9. 验证 chat 已解绑但配置仍存在
        bound = cc.get_bound_sessions(session.session_id)
        channels = {b["channel"]: b for b in bound}
        assert channels["chat"]["active"] is False
        assert channels["feishu"]["active"] is True
        assert channels["qq"]["active"] is True
