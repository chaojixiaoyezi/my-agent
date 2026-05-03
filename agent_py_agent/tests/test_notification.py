"""通知功能测试。

测试通知模型、管理器、路由器和通道状态检测功能。
"""
import time
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from agent_py_agent.agent.notification import (
    ChannelStatusChecker,
    Notification,
    NotificationDelivery,
    NotificationManager,
    NotificationRouter,
    generate_notification_id,
)


@pytest.fixture
def mock_config(tmp_path):
    """创建模拟的配置对象。"""
    config = Mock()
    config.notification_store_path = str(tmp_path / "notifications")
    config.notification_channel_timeout_seconds = 300
    config.session_workspace = str(tmp_path / "sessions")
    config.adapter_workspace = str(tmp_path / "adapters")
    config.user_id = "test_user"
    return config


@pytest.fixture
def manager(mock_config):
    """创建 NotificationManager 实例。"""
    return NotificationManager(mock_config)


@pytest.fixture
def router(mock_config):
    """创建 NotificationRouter 实例。"""
    return NotificationRouter(mock_config)


@pytest.fixture
def channel_checker(mock_config):
    """创建 ChannelStatusChecker 实例。"""
    return ChannelStatusChecker(mock_config)


class TestNotificationModel:
    """测试 Notification 数据类。"""

    def test_generate_notification_id_format(self):
        """测试 notification_id 格式。"""
        notif_id = generate_notification_id()
        assert notif_id.startswith("notif_")
        parts = notif_id.split("_")
        assert len(parts) == 3
        # 第二部分是时间戳
        assert parts[1].isdigit()
        # 第三部分是 4 位十六进制
        assert len(parts[2]) == 4

    def test_notification_creation(self):
        """测试 Notification 创建。"""
        notification = Notification(
            notification_id="notif_1234567890_abcd",
            task_id="task-001",
            user_id="admin",
            session_id="sess_1234567890_abcd",
            channel="chat",
            message="任务已完成",
            status="pending",
            created_at=1234567890.0,
        )
        assert notification.notification_id == "notif_1234567890_abcd"
        assert notification.task_id == "task-001"
        assert notification.user_id == "admin"
        assert notification.channel == "chat"
        assert notification.status == "pending"

    def test_notification_touch_delivered(self):
        """测试 touch_delivered 方法。"""
        notification = Notification(
            notification_id="notif_1234567890_abcd",
            task_id="task-001",
            user_id="admin",
            session_id="sess_1234567890_abcd",
            channel="chat",
            message="任务已完成",
            status="pending",
        )
        time.sleep(0.01)
        notification.touch_delivered("feishu")
        assert notification.status == "delivered"
        assert notification.delivered_at > 0
        assert notification.delivery_channel == "feishu"

    def test_notification_touch_stored(self):
        """测试 touch_stored 方法。"""
        notification = Notification(
            notification_id="notif_1234567890_abcd",
            task_id="task-001",
            user_id="admin",
            session_id="sess_1234567890_abcd",
            channel="chat",
            message="任务已完成",
            status="pending",
        )
        notification.touch_stored()
        assert notification.status == "stored"

    def test_notification_to_dict(self):
        """测试 to_dict 方法。"""
        notification = Notification(
            notification_id="notif_1234567890_abcd",
            task_id="task-001",
            user_id="admin",
            session_id="sess_1234567890_abcd",
            channel="chat",
            message="任务已完成",
            status="pending",
            created_at=1234567890.0,
        )
        data = notification.to_dict()
        assert data["notification_id"] == "notif_1234567890_abcd"
        assert data["task_id"] == "task-001"
        assert data["channel"] == "chat"

    def test_notification_from_dict(self):
        """测试 from_dict 方法。"""
        data = {
            "notification_id": "notif_1234567890_abcd",
            "task_id": "task-001",
            "user_id": "admin",
            "session_id": "sess_1234567890_abcd",
            "channel": "chat",
            "message": "任务已完成",
            "status": "pending",
            "created_at": 1234567890.0,
            "delivered_at": 0.0,
            "delivery_channel": "",
        }
        notification = Notification.from_dict(data)
        assert notification.notification_id == "notif_1234567890_abcd"
        assert notification.task_id == "task-001"


class TestNotificationManager:
    """测试 NotificationManager 类。"""

    def test_create_notification(self, manager):
        """测试创建通知。"""
        notification = manager.create_notification(
            task_id="task-001",
            user_id="test_user",
            session_id="sess_1234567890_abcd",
            channel="chat",
            message="任务已完成",
        )
        assert notification.notification_id.startswith("notif_")
        assert notification.task_id == "task-001"
        assert notification.user_id == "test_user"
        assert notification.channel == "chat"
        assert notification.status == "pending"
        # 验证文件被创建
        assert manager.notification_exists(notification.notification_id)

    def test_load_notification(self, manager):
        """测试加载通知。"""
        notification = manager.create_notification(
            task_id="task-001",
            user_id="test_user",
            session_id="sess_1234567890_abcd",
            channel="chat",
            message="任务已完成",
        )
        loaded = manager.load_notification(notification.notification_id)
        assert loaded is not None
        assert loaded.notification_id == notification.notification_id
        assert loaded.task_id == "task-001"

    def test_load_nonexistent_notification(self, manager):
        """测试加载不存在的通知。"""
        loaded = manager.load_notification("notif_nonexistent")
        assert loaded is None

    def test_mark_delivered(self, manager):
        """测试标记已投递。"""
        notification = manager.create_notification(
            task_id="task-001",
            user_id="test_user",
            session_id="sess_1234567890_abcd",
            channel="chat",
            message="任务已完成",
        )
        result = manager.mark_delivered(notification.notification_id, "feishu")
        assert result is True
        loaded = manager.load_notification(notification.notification_id)
        assert loaded.status == "delivered"
        assert loaded.delivery_channel == "feishu"

    def test_mark_stored(self, manager):
        """测试标记为存储。"""
        notification = manager.create_notification(
            task_id="task-001",
            user_id="test_user",
            session_id="sess_1234567890_abcd",
            channel="chat",
            message="任务已完成",
        )
        result = manager.mark_stored(notification.notification_id)
        assert result is True
        loaded = manager.load_notification(notification.notification_id)
        assert loaded.status == "stored"

    def test_get_pending(self, manager):
        """测试获取待处理通知。"""
        n1 = manager.create_notification(
            task_id="task-001",
            user_id="test_user",
            session_id="sess_1234567890_abcd",
            channel="chat",
            message="任务1已完成",
        )
        n2 = manager.create_notification(
            task_id="task-002",
            user_id="test_user",
            session_id="sess_1234567890_abcd",
            channel="chat",
            message="任务2已完成",
        )
        # 标记一个为已投递
        manager.mark_delivered(n1.notification_id, "chat")

        pending = manager.get_pending("test_user")
        assert len(pending) == 1
        assert pending[0].notification_id == n2.notification_id

    def test_get_pending_count(self, manager):
        """测试获取待处理通知数量。"""
        manager.create_notification(
            task_id="task-001",
            user_id="test_user",
            session_id="sess_1234567890_abcd",
            channel="chat",
            message="任务1已完成",
        )
        manager.create_notification(
            task_id="task-002",
            user_id="test_user",
            session_id="sess_1234567890_abcd",
            channel="chat",
            message="任务2已完成",
        )
        count = manager.get_pending_count("test_user")
        assert count == 2

    def test_list_notifications(self, manager):
        """测试列出通知。"""
        n1 = manager.create_notification(
            task_id="task-001",
            user_id="test_user",
            session_id="sess_1234567890_abcd",
            channel="chat",
            message="任务1已完成",
        )
        n2 = manager.create_notification(
            task_id="task-002",
            user_id="test_user",
            session_id="sess_1234567890_abcd",
            channel="chat",
            message="任务2已完成",
        )
        # 标记一个为已投递
        manager.mark_delivered(n1.notification_id, "feishu")

        # 不包含已投递
        notifications = manager.list_notifications("test_user", include_delivered=False)
        assert len(notifications) == 1

        # 包含已投递
        notifications = manager.list_notifications("test_user", include_delivered=True)
        assert len(notifications) == 2

    def test_delete_notification(self, manager):
        """测试删除通知。"""
        notification = manager.create_notification(
            task_id="task-001",
            user_id="test_user",
            session_id="sess_1234567890_abcd",
            channel="chat",
            message="任务已完成",
        )
        assert manager.notification_exists(notification.notification_id) is True

        result = manager.delete_notification(notification.notification_id)
        assert result is True
        assert manager.notification_exists(notification.notification_id) is False


class TestChannelStatusChecker:
    """测试 ChannelStatusChecker 类。"""

    def test_check_chat_online_no_sessions(self, channel_checker):
        """测试 chat 在线检测（无会话）。"""
        assert channel_checker.check("chat") is False

    def test_check_chat_online_with_active_session(self, channel_checker, tmp_path):
        """测试 chat 在线检测（有活跃会话）。"""
        # 创建活跃的 chat 会话
        session_dir = tmp_path / "sessions" / "sess_1234567890_abcd"
        session_dir.mkdir(parents=True)
        import json
        session_file = session_dir / "session.json"
        session_file.write_text(json.dumps({
            "session_id": "sess_1234567890_abcd",
            "user_id": "test_user",
            "last_active_channel": "chat",
            "updated_at": time.time(),  # 最近活跃
        }))

        # 重新初始化以读取新的 session_workspace
        from agent_py_agent.agent.notification.channel_status import ChannelStatusChecker
        checker = ChannelStatusChecker(channel_checker.config)
        assert checker.check("chat", "test_user") is True

    def test_check_chat_offline_with_stale_session(self, channel_checker, tmp_path):
        """测试 chat 在线检测（会话已过期）。"""
        # 创建过期的 chat 会话
        session_dir = tmp_path / "sessions" / "sess_1234567890_abcd"
        session_dir.mkdir(parents=True)
        import json
        session_file = session_dir / "session.json"
        session_file.write_text(json.dumps({
            "session_id": "sess_1234567890_abcd",
            "user_id": "test_user",
            "last_active_channel": "chat",
            "updated_at": time.time() - 600,  # 10 分钟前，不在超时时间内
        }))

        from agent_py_agent.agent.notification.channel_status import ChannelStatusChecker
        checker = ChannelStatusChecker(channel_checker.config)
        assert checker.check("chat", "test_user") is False

    def test_register_status(self, channel_checker):
        """测试注册通道状态。"""
        channel_checker.register_status("feishu", True)
        assert channel_checker.check("feishu") is True

        channel_checker.register_status("feishu", False)
        assert channel_checker.check("feishu") is False

    def test_get_active_channels(self, channel_checker):
        """测试获取在线通道列表。"""
        channel_checker.register_status("chat", True)
        channel_checker.register_status("feishu", True)
        active = channel_checker.get_active_channels("test_user")
        assert "chat" in active
        assert "feishu" in active


class TestNotificationRouter:
    """测试 NotificationRouter 类。"""

    def test_route_origin_channel_online(self, router):
        """测试路由策略：发起通道在线。"""
        from agent_py_agent.agent.notification.models import Notification

        # 注册发起通道为在线
        router._channel_checker.register_status("chat", True)

        notification = Notification(
            notification_id="notif_1234567890_abcd",
            task_id="task-001",
            user_id="test_user",
            session_id="sess_1234567890_abcd",
            channel="chat",
            message="任务已完成",
            status="pending",
        )

        target = router.route(notification)
        assert target == "chat"

    def test_route_fallback_to_active_channel(self, router, tmp_path):
        """测试路由策略：发起通道离线，转活跃通道。"""
        import json

        from agent_py_agent.agent.notification.models import Notification

        # 发起通道离线，但有活跃的 feishu 会话
        router._channel_checker.register_status("chat", False)
        router._channel_checker.register_status("feishu", True)

        notification = Notification(
            notification_id="notif_1234567890_abcd",
            task_id="task-001",
            user_id="test_user",
            session_id="sess_1234567890_abcd",
            channel="chat",
            message="任务已完成",
            status="pending",
        )

        target = router.route(notification)
        assert target == "feishu"

    def test_route_all_offline_stored(self, router):
        """测试路由策略：全部离线，需要存储。"""
        from agent_py_agent.agent.notification.models import Notification

        # 所有通道都离线
        router._channel_checker.register_status("chat", False)
        router._channel_checker.register_status("feishu", False)
        router._channel_checker.register_status("qq", False)

        notification = Notification(
            notification_id="notif_1234567890_abcd",
            task_id="task-001",
            user_id="test_user",
            session_id="sess_1234567890_abcd",
            channel="chat",
            message="任务已完成",
            status="pending",
        )

        target = router.route(notification)
        assert target is None

    def test_deliver_success(self, router, manager):
        """测试投递成功。"""
        # 注册 chat 通道在线
        router._channel_checker.register_status("chat", True)

        notification = manager.create_notification(
            task_id="task-001",
            user_id="test_user",
            session_id="sess_1234567890_abcd",
            channel="chat",
            message="任务已完成",
        )

        success, channel = router.deliver(notification.notification_id)
        assert success is True
        assert channel == "chat"

        # 验证状态已更新
        loaded = manager.load_notification(notification.notification_id)
        assert loaded.status == "delivered"

    def test_deliver_stored_when_offline(self, router, manager):
        """测试投递失败时标记为存储。"""
        # 所有通道离线
        router._channel_checker.register_status("chat", False)
        router._channel_checker.register_status("feishu", False)
        router._channel_checker.register_status("qq", False)

        notification = manager.create_notification(
            task_id="task-001",
            user_id="test_user",
            session_id="sess_1234567890_abcd",
            channel="chat",
            message="任务已完成",
        )

        success, info = router.deliver(notification.notification_id)
        assert success is False
        assert "存储" in info

        # 验证状态已更新
        loaded = manager.load_notification(notification.notification_id)
        assert loaded.status == "stored"

    def test_flush_stored(self, router, manager):
        """测试推送所有离线存储的通知。"""
        # 所有通道离线，创建存储的通知
        router._channel_checker.register_status("chat", False)
        router._channel_checker.register_status("feishu", False)
        router._channel_checker.register_status("qq", False)

        n1 = manager.create_notification(
            task_id="task-001",
            user_id="test_user",
            session_id="sess_1234567890_abcd",
            channel="chat",
            message="任务1已完成",
        )
        n2 = manager.create_notification(
            task_id="task-002",
            user_id="test_user",
            session_id="sess_1234567890_abcd",
            channel="chat",
            message="任务2已完成",
        )
        # 标记为存储
        manager.mark_stored(n1.notification_id)
        manager.mark_stored(n2.notification_id)

        # 打开 chat 通道
        router._channel_checker.register_status("chat", True)

        # flush
        count = router.flush_stored("test_user")
        assert count == 2

        # 验证都已投递
        loaded1 = manager.load_notification(n1.notification_id)
        loaded2 = manager.load_notification(n2.notification_id)
        assert loaded1.status == "delivered"
        assert loaded2.status == "delivered"


class TestNotificationIntegration:
    """集成测试场景。"""

    def test_full_notification_lifecycle(self, manager, router):
        """测试完整通知生命周期。"""
        # 1. 注册 chat 通道在线
        router._channel_checker.register_status("chat", True)

        # 2. 创建通知
        notification = manager.create_notification(
            task_id="task-001",
            user_id="test_user",
            session_id="sess_1234567890_abcd",
            channel="chat",
            message="任务已完成",
        )

        # 3. 验证状态为 pending
        assert notification.status == "pending"

        # 4. 投递
        success, channel = router.deliver(notification.notification_id)
        assert success is True
        assert channel == "chat"

        # 5. 验证状态为 delivered
        loaded = manager.load_notification(notification.notification_id)
        assert loaded.status == "delivered"
        assert loaded.delivery_channel == "chat"
        assert loaded.delivered_at > 0

        # 6. 再次投递应该返回已投递
        success, channel = router.deliver(notification.notification_id)
        assert success is True

    def test_offline_user_notification_flow(self, manager, router):
        """测试离线用户通知流程。"""
        # 所有通道离线
        router._channel_checker.register_status("chat", False)
        router._channel_checker.register_status("feishu", False)
        router._channel_checker.register_status("qq", False)

        # 创建通知
        notification = manager.create_notification(
            task_id="task-001",
            user_id="test_user",
            session_id="sess_1234567890_abcd",
            channel="chat",
            message="任务已完成",
        )

        # 投递应该失败，标记为 stored
        success, info = router.deliver(notification.notification_id)
        assert success is False

        # 验证状态为 stored
        loaded = manager.load_notification(notification.notification_id)
        assert loaded.status == "stored"

        # 用户上线，打开 chat 通道
        router._channel_checker.register_status("chat", True)

        # flush
        count = router.flush_stored("test_user")
        assert count == 1

        # 验证已投递
        loaded = manager.load_notification(notification.notification_id)
        assert loaded.status == "delivered"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
