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
    """鍒涘缓妯℃嫙鐨勯厤缃璞°€?"""
    config = Mock()
    config.notification_store_path = str(tmp_path / "notifications")
    config.notification_channel_timeout_seconds = 300
    config.session_workspace = str(tmp_path / "sessions")
    config.adapter_workspace = str(tmp_path / "adapters")
    config.user_id = "test_user"
    return config


@pytest.fixture
def router(mock_config):
    """鍒涘缓 NotificationRouter 瀹炰緥銆?"""
    return NotificationRouter(mock_config)


@pytest.fixture
def manager(mock_config):
    """鍒涘缓 NotificationManager 瀹炰緥銆?"""
    return NotificationManager(mock_config)


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
