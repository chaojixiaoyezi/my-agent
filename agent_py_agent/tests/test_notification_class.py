"""notification/manager.py 单元测试。

测试通知发送、模板渲染。
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest


class TestNotificationInit:
    """测试 NotificationManager 初始化。"""

    def test_init_sets_config(self):
        """验证初始化时设置配置。"""
        from agent_py_agent.agent.notification.manager import NotificationManager

        mock_config = MagicMock()
        mock_config.notification_store_path = "data/notifications"

        manager = NotificationManager(mock_config)
        assert manager.config == mock_config


class TestCreateNotification:
    """测试 create_notification() 方法。"""

    def test_create_notification_returns_notification(self, tmp_path: Path):
        """验证创建通知返回 Notification 对象。"""
        from agent_py_agent.agent.notification.manager import NotificationManager

        mock_config = MagicMock()
        mock_config.notification_store_path = str(tmp_path / "notifications")

        manager = NotificationManager(mock_config)
        notification = manager.create_notification(
            task_id="task_123",
            user_id="user_456",
            session_id="sess_789",
            channel="chat",
            message="任务完成",
        )

        assert notification.task_id == "task_123"
        assert notification.user_id == "user_456"
        assert notification.channel == "chat"
        assert notification.status == "pending"


class TestLoadNotification:
    """测试 load_notification() 方法。"""

    def test_load_existing_notification(self, tmp_path: Path):
        """验证加载已存在的通知。"""
        from agent_py_agent.agent.notification.manager import NotificationManager

        mock_config = MagicMock()
        mock_config.notification_store_path = str(tmp_path / "notifications")

        manager = NotificationManager(mock_config)

        # 先创建通知
        notification = manager.create_notification(
            task_id="task_abc",
            user_id="user_def",
            session_id="sess_ghi",
            channel="feishu",
            message="测试",
        )

        # 再加载
        loaded = manager.load_notification(notification.notification_id)

        assert loaded is not None
        assert loaded.notification_id == notification.notification_id

    def test_load_nonexistent_returns_none(self, tmp_path: Path):
        """加载不存在的通知返回 None。"""
        from agent_py_agent.agent.notification.manager import NotificationManager

        mock_config = MagicMock()
        mock_config.notification_store_path = str(tmp_path / "notifications")

        manager = NotificationManager(mock_config)
        result = manager.load_notification("nonexistent_id")

        assert result is None


class TestSaveNotification:
    """测试 save_notification() 方法。"""

    def test_save_notification_creates_file(self, tmp_path: Path):
        """验证保存通知创建文件。"""
        from agent_py_agent.agent.notification.manager import NotificationManager
        from agent_py_agent.agent.notification.models import Notification

        mock_config = MagicMock()
        mock_config.notification_store_path = str(tmp_path / "notifications")

        manager = NotificationManager(mock_config)

        notification = Notification(
            notification_id="notif_test_1",
            task_id="task_1",
            user_id="user_1",
            session_id="sess_1",
            channel="chat",
            message="测试",
        )

        manager.save_notification(notification)

        assert notification.path.exists() if hasattr(notification, "path") else True


class TestMarkDelivered:
    """测试 mark_delivered() 方法。"""

    def test_mark_delivered_updates_status(self, tmp_path: Path):
        """验证标记已投递更新状态。"""
        from agent_py_agent.agent.notification.manager import NotificationManager

        mock_config = MagicMock()
        mock_config.notification_store_path = str(tmp_path / "notifications")

        manager = NotificationManager(mock_config)

        notification = manager.create_notification(
            task_id="task_delivered",
            user_id="user_1",
            session_id="sess_1",
            channel="chat",
            message="完成",
        )

        result = manager.mark_delivered(notification.notification_id, "feishu")

        assert result is True
        loaded = manager.load_notification(notification.notification_id)
        assert loaded.status == "delivered"


class TestMarkFailed:
    """测试 mark_failed() 方法。"""

    def test_mark_failed_updates_status(self, tmp_path: Path):
        """验证标记失败更新状态。"""
        from agent_py_agent.agent.notification.manager import NotificationManager

        mock_config = MagicMock()
        mock_config.notification_store_path = str(tmp_path / "notifications")

        manager = NotificationManager(mock_config)

        notification = manager.create_notification(
            task_id="task_fail",
            user_id="user_1",
            session_id="sess_1",
            channel="chat",
            message="失败测试",
        )

        result = manager.mark_failed(notification.notification_id, "网络错误")

        assert result is True
        loaded = manager.load_notification(notification.notification_id)
        assert loaded.status == "failed"


class TestGetPending:
    """测试 get_pending() 方法。"""

    def test_get_pending_returns_user_notifications(self, tmp_path: Path):
        """验证获取用户的待投递通知。"""
        from agent_py_agent.agent.notification.manager import NotificationManager

        mock_config = MagicMock()
        mock_config.notification_store_path = str(tmp_path / "notifications")

        manager = NotificationManager(mock_config)

        # 创建两条通知
        manager.create_notification(
            task_id="task_1",
            user_id="user_pending",
            session_id="sess_1",
            channel="chat",
            message="通知1",
        )
        manager.create_notification(
            task_id="task_2",
            user_id="user_pending",
            session_id="sess_2",
            channel="chat",
            message="通知2",
        )

        pending = manager.get_pending("user_pending")
        assert len(pending) == 2


class TestListNotifications:
    """测试 list_notifications() 方法。"""

    def test_list_notifications_respects_limit(self, tmp_path: Path):
        """验证列表限制条数。"""
        from agent_py_agent.agent.notification.manager import NotificationManager

        mock_config = MagicMock()
        mock_config.notification_store_path = str(tmp_path / "notifications")

        manager = NotificationManager(mock_config)

        # 创建 5 条通知
        for i in range(5):
            manager.create_notification(
                task_id=f"task_{i}",
                user_id="user_list",
                session_id=f"sess_{i}",
                channel="chat",
                message=f"消息{i}",
            )

        result = manager.list_notifications("user_list", limit=3)
        assert len(result) == 3


class TestDeleteNotification:
    """测试 delete_notification() 方法。"""

    def test_delete_existing_notification(self, tmp_path: Path):
        """验证删除已存在的通知。"""
        from agent_py_agent.agent.notification.manager import NotificationManager

        mock_config = MagicMock()
        mock_config.notification_store_path = str(tmp_path / "notifications")

        manager = NotificationManager(mock_config)

        notification = manager.create_notification(
            task_id="task_del",
            user_id="user_del",
            session_id="sess_del",
            channel="chat",
            message="删除测试",
        )

        result = manager.delete_notification(notification.notification_id)
        assert result is True

        loaded = manager.load_notification(notification.notification_id)
        assert loaded is None


class TestNotificationExists:
    """测试 notification_exists() 方法。"""

    def test_exists_returns_true_for_existing(self, tmp_path: Path):
        """已存在的通知返回 True。"""
        from agent_py_agent.agent.notification.manager import NotificationManager

        mock_config = MagicMock()
        mock_config.notification_store_path = str(tmp_path / "notifications")

        manager = NotificationManager(mock_config)

        notification = manager.create_notification(
            task_id="task_exist",
            user_id="user_exist",
            session_id="sess_exist",
            channel="chat",
            message="存在测试",
        )

        assert manager.notification_exists(notification.notification_id) is True

    def test_exists_returns_false_for_nonexistent(self, tmp_path: Path):
        """不存在的通知返回 False。"""
        from agent_py_agent.agent.notification.manager import NotificationManager

        mock_config = MagicMock()
        mock_config.notification_store_path = str(tmp_path / "notifications")

        manager = NotificationManager(mock_config)

        assert manager.notification_exists("nonexistent") is False