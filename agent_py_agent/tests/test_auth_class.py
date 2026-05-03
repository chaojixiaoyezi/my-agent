"""认证和权限测试 - auth.py 认证、权限校验。"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


class TestAuthManager:
    """测试 AuthManager 类。"""

    def test_authenticate_with_disabled_auth(self, tmp_path: Path):
        """禁用鉴权时返回管理员权限。"""
        from agent_py_agent.agent.auth.manager import AuthManager
        from agent_py_agent.agent.auth.models import Role

        manager = AuthManager(admin_user_id="admin", auth_enabled=False)

        permission = manager.authenticate("chat", "any_user")

        assert permission.role == Role.ADMIN
        assert permission.user_id == "any_user"

    def test_authenticate_with_enabled_auth_terminal_channel(self, tmp_path: Path):
        """终端通道鉴权时返回管理员。"""
        from agent_py_agent.agent.auth.manager import AuthManager
        from agent_py_agent.agent.auth.models import Role

        manager = AuthManager(admin_user_id="admin", auth_enabled=True)

        permission = manager.authenticate("chat", "any_user")

        assert permission.role == Role.ADMIN

    def test_authenticate_with_admin_user_id(self, tmp_path: Path):
        """admin 用户 ID 返回管理员权限。"""
        from agent_py_agent.agent.auth.manager import AuthManager
        from agent_py_agent.agent.auth.models import Role

        manager = AuthManager(admin_user_id="admin", auth_enabled=True)

        permission = manager.authenticate("feishu", "admin")

        assert permission.role == Role.ADMIN

    def test_authenticate_regular_user(self, tmp_path: Path):
        """普通用户返回普通权限。"""
        from agent_py_agent.agent.auth.manager import AuthManager
        from agent_py_agent.agent.auth.models import Role

        manager = AuthManager(admin_user_id="admin", auth_enabled=True)

        permission = manager.authenticate("feishu", "user123")

        assert permission.role == Role.USER


class TestAuthorize:
    """测试 authorize 方法。"""

    def test_authorize_with_disabled_auth(self, tmp_path: Path):
        """禁用鉴权时总是允许。"""
        from agent_py_agent.agent.auth.manager import AuthManager
        from agent_py_agent.agent.auth.models import Action

        manager = AuthManager(auth_enabled=False)

        permission = MagicMock()
        permission.can.return_value = False

        result = manager.authorize(permission, Action.READ_TASK, None)

        assert result is True

    def test_authorize_admin_can_do_anything(self, tmp_path: Path):
        """管理员可以做任何操作。"""
        from agent_py_agent.agent.auth.manager import AuthManager
        from agent_py_agent.agent.auth.models import Action, Permission, Role

        manager = AuthManager(auth_enabled=True)

        admin_permission = Permission(role=Role.ADMIN, user_id="admin")

        result = manager.authorize(admin_permission, Action.WRITE_TASK, None)

        assert result is True

    def test_authorize_user_read_task(self, tmp_path: Path):
        """普通用户可以读取任务。"""
        from agent_py_agent.agent.auth.manager import AuthManager
        from agent_py_agent.agent.auth.models import Action, Permission, Role

        manager = AuthManager(auth_enabled=True)

        user_permission = Permission(role=Role.USER, user_id="user123")

        result = manager.authorize(user_permission, Action.READ_TASK, None)

        assert result is True

    def test_authorize_cross_user_data_access_denied(self, tmp_path: Path):
        """普通用户不能访问其他用户数据。"""
        from agent_py_agent.agent.auth.manager import AuthManager
        from agent_py_agent.agent.auth.models import Action, Permission, Role

        manager = AuthManager(auth_enabled=True)

        user_permission = Permission(role=Role.USER, user_id="user123", can_access_all_users=False)

        result = manager.authorize(user_permission, Action.WRITE_TASK, "other_user")

        assert result is False

    def test_authorize_own_data_access_allowed(self, tmp_path: Path):
        """普通用户可以访问自己的数据。"""
        from agent_py_agent.agent.auth.manager import AuthManager
        from agent_py_agent.agent.auth.models import Action, Permission, Role

        manager = AuthManager(auth_enabled=True)

        # USER 角色的 can 方法只对 READ_TASK 返回 True
        user_permission = Permission(role=Role.USER, user_id="user123", can_access_all_users=False)

        # WRITE_TASK 对 USER 返回 False
        result = manager.authorize(user_permission, Action.WRITE_TASK, "user123")

        # USER 不能写任务
        assert result is False


class TestIsAdmin:
    """测试 is_admin 方法。"""

    def test_is_admin_disabled_auth(self, tmp_path: Path):
        """禁用鉴权时都是管理员。"""
        from agent_py_agent.agent.auth.manager import AuthManager

        manager = AuthManager(auth_enabled=False)

        assert manager.is_admin("anyone") is True

    def test_is_admin_terminal_channel(self, tmp_path: Path):
        """终端通道总是管理员。"""
        from agent_py_agent.agent.auth.manager import AuthManager

        manager = AuthManager(auth_enabled=True)

        assert manager.is_admin("anyone", channel="chat") is True

    def test_is_admin_admin_user(self, tmp_path: Path):
        """admin 用户总是管理员。"""
        from agent_py_agent.agent.auth.manager import AuthManager

        manager = AuthManager(admin_user_id="admin", auth_enabled=True)

        assert manager.is_admin("admin", channel="feishu") is True

    def test_is_admin_regular_user(self, tmp_path: Path):
        """普通用户不是管理员。"""
        from agent_py_agent.agent.auth.manager import AuthManager

        manager = AuthManager(admin_user_id="admin", auth_enabled=True)

        assert manager.is_admin("user123", channel="feishu") is False


class TestGetRole:
    """测试 get_role 方法。"""

    def test_get_role_disabled_auth(self, tmp_path: Path):
        """禁用鉴权时返回管理员。"""
        from agent_py_agent.agent.auth.manager import AuthManager
        from agent_py_agent.agent.auth.models import Role

        manager = AuthManager(auth_enabled=False)

        role = manager.get_role("anyone")

        assert role == Role.ADMIN

    def test_get_role_admin_user(self, tmp_path: Path):
        """admin 用户返回管理员角色。"""
        from agent_py_agent.agent.auth.manager import AuthManager
        from agent_py_agent.agent.auth.models import Role

        manager = AuthManager(admin_user_id="admin", auth_enabled=True)

        role = manager.get_role("admin", channel="feishu")

        assert role == Role.ADMIN

    def test_get_role_regular_user(self, tmp_path: Path):
        """普通用户返回普通角色。"""
        from agent_py_agent.agent.auth.manager import AuthManager
        from agent_py_agent.agent.auth.models import Role

        manager = AuthManager(admin_user_id="admin", auth_enabled=True)

        role = manager.get_role("user123", channel="feishu")

        assert role == Role.USER


class TestCheck:
    """测试 check 方法（认证+鉴权一站式）。"""

    def test_check_authenticate_and_authorize(self, tmp_path: Path):
        """一站式认证鉴权。"""
        from agent_py_agent.agent.auth.manager import AuthManager
        from agent_py_agent.agent.auth.models import Action

        manager = AuthManager(auth_enabled=True)

        ok, permission = manager.check("chat", "admin", Action.READ_TASK)

        assert ok is True
        assert permission is not None