"""LLM: 权限模型和多租户鉴权测试。

给人看的解释：
测试 Role/Permission 模型推断、AuthManager 认证鉴权、
auth_enabled=false 关闭模式、gateway HTTP 中间件。
"""

from __future__ import annotations

from agent_py_agent.agent.auth import Action, AuthManager, AuthMiddleware, Permission, Role
from agent_py_agent.agent.auth.models import build_permission, infer_role

# ---------------------------------------------------------------------------
# Role 推断测试
# ---------------------------------------------------------------------------


class TestRoleInference:
    """测试从 channel + user_id 推断角色。"""

    def test_terminal_channel_is_admin(self) -> None:
        for ch in ("chat", "cli", "terminal", "", "  "):
            role = infer_role(ch, "any_user")
            assert role == Role.ADMIN, f"channel={ch!r} should be ADMIN"

    def test_feishu_channel_is_user(self) -> None:
        role = infer_role("feishu", "ou_123")
        assert role == Role.USER

    def test_qq_channel_is_user(self) -> None:
        role = infer_role("qq", "777")
        assert role == Role.USER

    def test_admin_user_id_in_external_channel_is_admin(self) -> None:
        role = infer_role("feishu", "admin", admin_user_id="admin")
        assert role == Role.ADMIN

    def test_admin_user_id_custom(self) -> None:
        role = infer_role("qq", "superuser", admin_user_id="superuser")
        assert role == Role.ADMIN

    def test_different_admin_user_id_still_user(self) -> None:
        role = infer_role("feishu", "ou_123", admin_user_id="admin")
        assert role == Role.USER


class TestPermissionBuild:
    """测试 Permission 对象构建。"""

    def test_admin_permission_has_full_access(self) -> None:
        p = build_permission("chat", "admin")
        assert p.role == Role.ADMIN
        assert p.can_access_all_users is True
        assert p.can(Action.READ_TASK) is True
        assert p.can(Action.WRITE_TASK) is True
        assert p.can(Action.ADMIN_QUERY) is True

    def test_user_permission_restricted(self) -> None:
        p = build_permission("feishu", "ou_123")
        assert p.role == Role.USER
        assert p.can_access_all_users is False
        assert p.can(Action.READ_TASK) is True  # 读任务允许
        assert p.can(Action.WRITE_TASK) is False
        assert p.can(Action.ADMIN_QUERY) is False


# ---------------------------------------------------------------------------
# AuthManager 测试
# ---------------------------------------------------------------------------


class TestAuthManagerAuthenticate:
    """测试 AuthManager.authenticate。"""

    def test_chat_is_admin(self) -> None:
        mgr = AuthManager(admin_user_id="admin", auth_enabled=True)
        p = mgr.authenticate("chat", "anyone")
        assert p.role == Role.ADMIN

    def test_feishu_is_user(self) -> None:
        mgr = AuthManager(admin_user_id="admin", auth_enabled=True)
        p = mgr.authenticate("feishu", "ou_123")
        assert p.role == Role.USER

    def test_admin_user_id_in_feishu_is_admin(self) -> None:
        mgr = AuthManager(admin_user_id="admin", auth_enabled=True)
        p = mgr.authenticate("feishu", "admin")
        assert p.role == Role.ADMIN

    def test_auth_disabled_all_admin(self) -> None:
        mgr = AuthManager(admin_user_id="admin", auth_enabled=False)
        p1 = mgr.authenticate("feishu", "any_user")
        p2 = mgr.authenticate("qq", "another_user")
        assert p1.role == Role.ADMIN
        assert p2.role == Role.ADMIN
        assert p1.can_access_all_users is True

    def test_is_admin_shortcut(self) -> None:
        mgr = AuthManager(admin_user_id="admin", auth_enabled=True)
        assert mgr.is_admin("admin", "chat") is True
        assert mgr.is_admin("anyone", "chat") is True  # terminal is admin
        assert mgr.is_admin("ou_123", "feishu") is False

    def test_get_role_shortcut(self) -> None:
        mgr = AuthManager(admin_user_id="admin", auth_enabled=True)
        assert mgr.get_role("anyone", "chat") == Role.ADMIN
        assert mgr.get_role("ou_123", "feishu") == Role.USER


class TestAuthManagerAuthorize:
    """测试 AuthManager.authorize。"""

    def test_admin_can_read_any_user_task(self) -> None:
        mgr = AuthManager(admin_user_id="admin", auth_enabled=True)
        p = mgr.authenticate("chat", "admin")
        assert mgr.authorize(p, Action.READ_TASK, target_user_id="other") is True

    def test_admin_can_write_any_user_task(self) -> None:
        mgr = AuthManager(admin_user_id="admin", auth_enabled=True)
        p = mgr.authenticate("chat", "admin")
        assert mgr.authorize(p, Action.WRITE_TASK, target_user_id="other") is True

    def test_user_cannot_write_others_task(self) -> None:
        mgr = AuthManager(admin_user_id="admin", auth_enabled=True)
        p = mgr.authenticate("feishu", "ou_123")
        assert mgr.authorize(p, Action.WRITE_TASK, target_user_id="ou_456") is False

    def test_user_can_read_own_task(self) -> None:
        mgr = AuthManager(admin_user_id="admin", auth_enabled=True)
        p = mgr.authenticate("feishu", "ou_123")
        assert mgr.authorize(p, Action.READ_TASK, target_user_id="ou_123") is True

    def test_user_cannot_admin_query(self) -> None:
        mgr = AuthManager(admin_user_id="admin", auth_enabled=True)
        p = mgr.authenticate("feishu", "ou_123")
        assert mgr.authorize(p, Action.ADMIN_QUERY) is False

    def test_admin_cannot_admin_query_if_disabled(self) -> None:
        # 即使 admin，auth_enabled=False 时 authorize 也应该返回 True
        mgr = AuthManager(admin_user_id="admin", auth_enabled=False)
        p = mgr.authenticate("feishu", "anyone")
        assert mgr.authorize(p, Action.ADMIN_QUERY) is True

    def test_check_combined(self) -> None:
        mgr = AuthManager(admin_user_id="admin", auth_enabled=True)
        ok, perm = mgr.check("feishu", "ou_123", Action.WRITE_TASK, target_user_id="ou_123")
        assert ok is False  # USER can't write
        ok, perm = mgr.check("feishu", "ou_123", Action.READ_TASK, target_user_id="ou_123")
        assert ok is True
        ok, perm = mgr.check("feishu", "admin", Action.WRITE_TASK, target_user_id="anyone")
        assert ok is True  # admin can write anyone


# ---------------------------------------------------------------------------
# AuthMiddleware 测试
# ---------------------------------------------------------------------------


class TestAuthMiddleware:
    """测试 HTTP 鉴权中间件。"""

    def test_no_headers_defaults_to_admin(self) -> None:
        mgr = AuthManager(admin_user_id="admin", auth_enabled=True)
        mw = AuthMiddleware(mgr)
        uid, ch = mw.extract_identity({})
        assert uid == "admin"
        assert ch == "chat"

    def test_feishu_headers(self) -> None:
        mgr = AuthManager(admin_user_id="admin", auth_enabled=True)
        mw = AuthMiddleware(mgr)
        uid, ch = mw.extract_identity({"X-User-Id": "ou_123", "X-Channel": "feishu"})
        assert uid == "ou_123"
        assert ch == "feishu"

    def test_qq_headers(self) -> None:
        mgr = AuthManager(admin_user_id="admin", auth_enabled=True)
        mw = AuthMiddleware(mgr)
        uid, ch = mw.extract_identity({"X-User-Id": "777", "X-Channel": "qq"})
        assert uid == "777"
        assert ch == "qq"

    def test_admin_header_user_in_feishu(self) -> None:
        mgr = AuthManager(admin_user_id="admin", auth_enabled=True)
        mw = AuthMiddleware(mgr)
        p = mw.get_permission({"X-User-Id": "admin", "X-Channel": "feishu"})
        assert p.role == Role.ADMIN

    def test_permission_check_allowed(self) -> None:
        mgr = AuthManager(admin_user_id="admin", auth_enabled=True)
        mw = AuthMiddleware(mgr)
        ok, perm, status, _ = mw.check_permission(
            {"X-User-Id": "admin", "X-Channel": "feishu"},
            Action.READ_TASK,
            target_user_id="anyone",
        )
        assert ok is True
        assert status == 200

    def test_permission_check_forbidden(self) -> None:
        mgr = AuthManager(admin_user_id="admin", auth_enabled=True)
        mw = AuthMiddleware(mgr)
        ok, perm, status, body = mw.check_permission(
            {"X-User-Id": "ou_123", "X-Channel": "feishu"},
            Action.WRITE_TASK,
            target_user_id="ou_456",
        )
        assert ok is False
        assert status == 403
        assert "forbidden" in body["error"]

    def test_require_admin_allowed(self) -> None:
        mgr = AuthManager(admin_user_id="admin", auth_enabled=True)
        mw = AuthMiddleware(mgr)
        ok, _, status, _ = mw.require_admin({"X-User-Id": "admin", "X-Channel": "feishu"})
        assert ok is True
        assert status == 200

    def test_require_admin_denied(self) -> None:
        mgr = AuthManager(admin_user_id="admin", auth_enabled=True)
        mw = AuthMiddleware(mgr)
        ok, _, status, body = mw.require_admin({"X-User-Id": "ou_123", "X-Channel": "feishu"})
        assert ok is False
        assert status == 403
        assert "管理员权限" in body["message"]

    def test_auth_disabled_bypasses_all(self) -> None:
        mgr = AuthManager(admin_user_id="admin", auth_enabled=False)
        mw = AuthMiddleware(mgr)
        ok, _, status, _ = mw.check_permission(
            {"X-User-Id": "anyone", "X-Channel": "feishu"},
            Action.ADMIN_QUERY,
        )
        assert ok is True
        assert status == 200


class TestMiddlewareRequireHandlerHelpers:
    """测试 middleware helper 函数（在 handler 上下文中）。"""

    def test_require_permission_returns_false_when_no_middleware(self) -> None:
        from agent_py_agent.agent.auth.middleware import require_permission

        class FakeHandler:
            pass

        # 无 middleware，不拦截
        class NoMwHandler(FakeHandler):
            pass

        result = require_permission(NoMwHandler(), Action.READ_TASK)
        assert result is False  # 不拦截，继续处理

    def test_require_permission_blocks_forbidden(self) -> None:
        from agent_py_agent.agent.auth.middleware import require_permission

        class FakeHandler:
            def __init__(self) -> None:
                self._auth_middleware = None
                self._sent_status = 0
                self._sent_body = {}
                self.headers = {"X-User-Id": "ou_123", "X-Channel": "feishu"}

            def _send_json(self, status: int, body: dict) -> None:
                self._sent_status = status
                self._sent_body = body

        mgr = AuthManager(admin_user_id="admin", auth_enabled=True)
        from agent_py_agent.agent.auth.middleware import AuthMiddleware

        handler = FakeHandler()
        handler._auth_middleware = AuthMiddleware(mgr)

        result = require_permission(handler, Action.WRITE_TASK, target_user_id="other")
        assert result is True  # 已发送响应
        assert handler._sent_status == 403

    def test_require_admin_blocks_non_admin(self) -> None:
        from agent_py_agent.agent.auth.middleware import require_admin_handler

        class FakeHandler:
            def __init__(self) -> None:
                self._auth_middleware = None
                self._sent_status = 0
                self.headers = {"X-User-Id": "ou_123", "X-Channel": "feishu"}

            def _send_json(self, status: int, body: dict) -> None:
                self._sent_status = status

        mgr = AuthManager(admin_user_id="admin", auth_enabled=True)
        from agent_py_agent.agent.auth.middleware import AuthMiddleware

        handler = FakeHandler()
        handler._auth_middleware = AuthMiddleware(mgr)

        result = require_admin_handler(handler)
        assert result is True
        assert handler._sent_status == 403
