"""Tests for notification/router.py - notification routing logic."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent_py_agent.agent.notification.models import Notification
from agent_py_agent.agent.notification.router import NotificationRouter


@pytest.fixture
def mock_config():
    """Create a mock config for testing."""
    config = MagicMock()
    config.session_workspace = "/tmp/test_sessions"
    config.notification_store_path = "/tmp/test_notifications"
    config.notification_channel_timeout_seconds = 300
    return config


class TestNotificationRouter:
    """Test NotificationRouter class."""

    def test_route_returns_channel_when_online(self, mock_config):
        """Test route returns notification channel when it's online."""
        with patch("agent_py_agent.agent.notification.router.ChannelStatusChecker") as MockChecker, \
             patch("agent_py_agent.agent.notification.router.NotificationManager") as MockManager:
            router = NotificationRouter(mock_config)
            notification = MagicMock(spec=Notification)
            notification.channel = "chat"
            notification.user_id = "testuser"

            router._channel_checker.check.return_value = True

            result = router.route(notification)

            assert result == "chat"
            router._channel_checker.check.assert_called_once_with("chat", "testuser")

    def test_route_fallback_to_active_channel(self, mock_config):
        """Test route falls back to session active channel."""
        with patch("agent_py_agent.agent.notification.router.ChannelStatusChecker") as MockChecker, \
             patch("agent_py_agent.agent.notification.router.NotificationManager") as MockManager:
            router = NotificationRouter(mock_config)
            notification = MagicMock(spec=Notification)
            notification.channel = "feishu"
            notification.user_id = "testuser"

            # First check fails, second (active channel) succeeds
            router._channel_checker.check.side_effect = [False, True]
            router._find_session_active_channel = MagicMock(return_value="chat")

            result = router.route(notification)

            assert result == "chat"

    def test_route_falls_back_to_other_online_channel(self, mock_config):
        """Test route falls back to other online channels."""
        with patch("agent_py_agent.agent.notification.router.ChannelStatusChecker") as MockChecker, \
             patch("agent_py_agent.agent.notification.router.NotificationManager") as MockManager:
            router = NotificationRouter(mock_config)
            notification = MagicMock(spec=Notification)
            notification.channel = "feishu"
            notification.user_id = "testuser"

            # Primary fails, active channel fails, qq channel succeeds
            router._channel_checker.check.side_effect = [False, False, True]
            router._find_session_active_channel = MagicMock(return_value=None)

            result = router.route(notification)

            # feishu is excluded, chat returns False, qq returns True
            assert result == "qq"

    def test_route_returns_none_when_no_channel_online(self, mock_config):
        """Test route returns None when no channel is online."""
        with patch("agent_py_agent.agent.notification.router.ChannelStatusChecker") as MockChecker, \
             patch("agent_py_agent.agent.notification.router.NotificationManager") as MockManager:
            router = NotificationRouter(mock_config)
            notification = MagicMock(spec=Notification)
            notification.channel = "chat"
            notification.user_id = "testuser"

            router._channel_checker.check.return_value = False
            router._find_session_active_channel = MagicMock(return_value=None)

            result = router.route(notification)

            assert result is None


class TestFindSessionActiveChannel:
    """Test _find_session_active_channel method."""

    def test_returns_none_when_workspace_missing(self, mock_config):
        """Test returns None when session workspace doesn't exist."""
        mock_config.session_workspace = "/nonexistent/workspace"
        with patch("agent_py_agent.agent.notification.router.ChannelStatusChecker"), \
             patch("agent_py_agent.agent.notification.router.NotificationManager"):
            router = NotificationRouter(mock_config)

            result = router._find_session_active_channel("testuser")

            assert result is None

    def test_returns_none_on_json_decode_error(self, mock_config, tmp_path: Path):
        """Test handles JSON decode error in session file."""
        mock_config.session_workspace = str(tmp_path)
        with patch("agent_py_agent.agent.notification.router.ChannelStatusChecker"), \
             patch("agent_py_agent.agent.notification.router.NotificationManager"):
            router = NotificationRouter(mock_config)

            session_dir = tmp_path / "session-001"
            session_dir.mkdir()
            session_file = session_dir / "session.json"
            session_file.write_text("invalid json{", encoding="utf-8")

            result = router._find_session_active_channel("testuser")

            assert result is None

    def test_returns_none_on_oserror_iterating(self, mock_config, tmp_path: Path):
        """Test handles OSError when iterating sessions."""
        mock_config.session_workspace = str(tmp_path)
        with patch("agent_py_agent.agent.notification.router.ChannelStatusChecker"), \
             patch("agent_py_agent.agent.notification.router.NotificationManager"):
            router = NotificationRouter(mock_config)

            with patch.object(Path, "iterdir", side_effect=OSError("Permission denied")):
                result = router._find_session_active_channel("testuser")

                assert result is None

    def test_skips_non_matching_user(self, mock_config, tmp_path: Path):
        """Test skips sessions not belonging to target user."""
        mock_config.session_workspace = str(tmp_path)
        with patch("agent_py_agent.agent.notification.router.ChannelStatusChecker"), \
             patch("agent_py_agent.agent.notification.router.NotificationManager"):
            router = NotificationRouter(mock_config)

            session_dir = tmp_path / "session-001"
            session_dir.mkdir()
            session_file = session_dir / "session.json"
            session_file.write_text(json.dumps({
                "user_id": "otheruser",
                "last_active_channel": "chat",
                "updated_at": 9999999999,
            }), encoding="utf-8")

            result = router._find_session_active_channel("testuser")

            assert result is None

    def test_skips_excluded_channel(self, mock_config, tmp_path: Path):
        """Test skips the excluded channel."""
        mock_config.session_workspace = str(tmp_path)
        with patch("agent_py_agent.agent.notification.router.ChannelStatusChecker"), \
             patch("agent_py_agent.agent.notification.router.NotificationManager"):
            router = NotificationRouter(mock_config)

            session_dir = tmp_path / "session-001"
            session_dir.mkdir()
            session_file = session_dir / "session.json"
            session_file.write_text(json.dumps({
                "user_id": "testuser",
                "last_active_channel": "feishu",
                "updated_at": 9999999999,
            }), encoding="utf-8")

            result = router._find_session_active_channel("testuser", exclude_channel="feishu")

            assert result is None


class TestDeliver:
    """Test deliver method."""

    def test_returns_false_for_nonexistent_notification(self, mock_config):
        """Test deliver returns error when notification doesn't exist."""
        with patch("agent_py_agent.agent.notification.router.ChannelStatusChecker") as MockChecker, \
             patch("agent_py_agent.agent.notification.router.NotificationManager") as MockManager:
            router = NotificationRouter(mock_config)
            router._manager.load_notification.return_value = None

            success, info = router.deliver("nonexistent-id")

            assert success is False
            assert "不存在" in info

    def test_returns_true_for_already_delivered(self, mock_config):
        """Test deliver returns True for already delivered notification."""
        with patch("agent_py_agent.agent.notification.router.ChannelStatusChecker") as MockChecker, \
             patch("agent_py_agent.agent.notification.router.NotificationManager") as MockManager:
            router = NotificationRouter(mock_config)

            notification = MagicMock(spec=Notification)
            notification.status = "delivered"
            notification.delivery_channel = "chat"

            router._manager.load_notification.return_value = notification

            success, info = router.deliver("test-id")

            assert success is True
            assert info == "chat"

    def test_marks_stored_when_no_channel_available(self, mock_config):
        """Test marks notification as stored when no channel is available."""
        with patch("agent_py_agent.agent.notification.router.ChannelStatusChecker") as MockChecker, \
             patch("agent_py_agent.agent.notification.router.NotificationManager") as MockManager:
            router = NotificationRouter(mock_config)

            notification = MagicMock(spec=Notification)
            notification.status = "pending"
            notification.channel = "chat"
            notification.user_id = "testuser"

            router._manager.load_notification.return_value = notification
            router.route = MagicMock(return_value=None)

            success, info = router.deliver("test-id")

            assert success is False
            assert "已存储" in info
            router._manager.mark_stored.assert_called_once_with("test-id")

    def test_marks_failed_on_delivery_error(self, mock_config):
        """Test marks notification as failed when delivery fails."""
        with patch("agent_py_agent.agent.notification.router.ChannelStatusChecker") as MockChecker, \
             patch("agent_py_agent.agent.notification.router.NotificationManager") as MockManager:
            router = NotificationRouter(mock_config)

            notification = MagicMock(spec=Notification)
            notification.status = "pending"
            notification.channel = "feishu"
            notification.user_id = "testuser"

            router._manager.load_notification.return_value = notification
            router.route = MagicMock(return_value="feishu")
            router._do_deliver = MagicMock(return_value=False)

            success, info = router.deliver("test-id")

            assert success is False
            assert "投递失败" in info
            router._manager.mark_failed.assert_called_once()


class TestFlushStored:
    """Test flush_stored method."""

    def test_flushes_stored_notifications(self, mock_config):
        """Test flushes all stored notifications for a user."""
        with patch("agent_py_agent.agent.notification.router.ChannelStatusChecker") as MockChecker, \
             patch("agent_py_agent.agent.notification.router.NotificationManager") as MockManager:
            router = NotificationRouter(mock_config)

            notification1 = MagicMock(spec=Notification)
            notification1.notification_id = "notif-1"
            notification1.status = "stored"

            notification2 = MagicMock(spec=Notification)
            notification2.notification_id = "notif-2"
            notification2.status = "stored"

            router._manager.get_pending.return_value = [notification1, notification2]
            router.deliver = MagicMock(side_effect=[(True, "chat"), (False, "no channel")])

            result = router.flush_stored("testuser")

            assert result == 1  # Only one succeeded

    def test_returns_zero_when_no_pending(self, mock_config):
        """Test returns 0 when no pending notifications."""
        with patch("agent_py_agent.agent.notification.router.ChannelStatusChecker") as MockChecker, \
             patch("agent_py_agent.agent.notification.router.NotificationManager") as MockManager:
            router = NotificationRouter(mock_config)
            router._manager.get_pending.return_value = []

            result = router.flush_stored("testuser")

            assert result == 0


class TestIsChannelOnline:
    """Test is_channel_online method."""

    def test_delegates_to_channel_checker(self, mock_config):
        """Test is_channel_online delegates to checker."""
        with patch("agent_py_agent.agent.notification.router.ChannelStatusChecker") as MockChecker, \
             patch("agent_py_agent.agent.notification.router.NotificationManager") as MockManager:
            router = NotificationRouter(mock_config)
            router._channel_checker.check.return_value = True

            result = router.is_channel_online("chat", "testuser")

            assert result is True
            router._channel_checker.check.assert_called_once_with("chat", "testuser")


class TestGetPendingCount:
    """Test get_pending_count method."""

    def test_delegates_to_manager(self, mock_config):
        """Test get_pending_count delegates to manager."""
        with patch("agent_py_agent.agent.notification.router.ChannelStatusChecker") as MockChecker, \
             patch("agent_py_agent.agent.notification.router.NotificationManager") as MockManager:
            router = NotificationRouter(mock_config)
            router._manager.get_pending_count.return_value = 5

            result = router.get_pending_count("testuser")

            assert result == 5
            router._manager.get_pending_count.assert_called_once_with("testuser")