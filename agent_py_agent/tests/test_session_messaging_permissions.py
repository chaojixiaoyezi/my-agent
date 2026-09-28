"""会话间消息与派活的权限矩阵合同单测。

覆盖 dev 审阅要求的 8 格权限矩阵（管理员/普通用户 × message/task × 同 owner/跨 owner），
外加边界：目标不存在与跨 owner 返回同一错误码（不泄露存在性）、自派任务拒绝、自消息允许、
开关关闭逐项拒绝、kind 非法。判定全部是纯函数，无 IO。
"""

from __future__ import annotations

import pytest

from agent_py_agent.agent.conversation.session_messaging import (
    SESSION_MESSAGING_DISABLED,
    SESSION_TARGET_CHANNEL_UNSUPPORTED,
    SESSION_TARGET_OUT_OF_SCOPE,
    SESSION_TASK_NOT_ALLOWED,
    SESSION_TASK_TARGET_SELF,
    SessionMessagingRequest,
    decide_session_messaging,
    session_messaging_tool_visible,
    session_task_tool_visible,
)
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity

_ADMIN = OwnerIdentity.local_main()
_USER = OwnerIdentity.provider_user("feishu", "ou_alice")
_USER_B = OwnerIdentity.provider_user("feishu", "ou_bob")


def _req(**overrides) -> SessionMessagingRequest:
    base = dict(
        sender_identity=_ADMIN,
        sender_thread_id="thread-a",
        target_thread_id="thread-b",
        target_owner_identity=_ADMIN,
        kind="message",
        messaging_admin_enabled=True,
        messaging_user_enabled=False,
        task_admin_enabled=True,
    )
    base.update(overrides)
    return SessionMessagingRequest(**base)


# --- 8 格权限矩阵 ---

def test_admin_message_same_owner_allowed() -> None:
    decision = decide_session_messaging(_req(kind="message", target_owner_identity=_ADMIN))
    assert decision.allowed and decision.error_code == ""


def test_admin_message_cross_owner_rejected_out_of_scope() -> None:
    decision = decide_session_messaging(
        _req(kind="message", sender_identity=_ADMIN, target_owner_identity=_USER)
    )
    assert not decision.allowed
    assert decision.error_code == SESSION_TARGET_OUT_OF_SCOPE


def test_admin_task_same_owner_allowed() -> None:
    decision = decide_session_messaging(_req(kind="task", target_owner_identity=_ADMIN))
    assert decision.allowed and decision.error_code == ""


def test_admin_task_cross_owner_rejected_out_of_scope() -> None:
    decision = decide_session_messaging(
        _req(kind="task", sender_identity=_ADMIN, target_owner_identity=_USER)
    )
    assert not decision.allowed
    assert decision.error_code == SESSION_TARGET_OUT_OF_SCOPE


def test_user_message_same_owner_rejected_messaging_disabled() -> None:
    decision = decide_session_messaging(
        _req(kind="message", sender_identity=_USER, target_owner_identity=_USER)
    )
    assert not decision.allowed
    assert decision.error_code == SESSION_MESSAGING_DISABLED


def test_user_message_cross_owner_rejected_out_of_scope() -> None:
    # 跨 owner 先于开关判定：返回越界码，不因普通用户换成 disabled。
    decision = decide_session_messaging(
        _req(kind="message", sender_identity=_USER, target_owner_identity=_ADMIN)
    )
    assert not decision.allowed
    assert decision.error_code == SESSION_TARGET_OUT_OF_SCOPE


def test_user_task_same_owner_rejected_task_not_allowed() -> None:
    decision = decide_session_messaging(
        _req(kind="task", sender_identity=_USER, target_owner_identity=_USER)
    )
    assert not decision.allowed
    assert decision.error_code == SESSION_TASK_NOT_ALLOWED


def test_user_task_cross_owner_rejected_out_of_scope() -> None:
    decision = decide_session_messaging(
        _req(kind="task", sender_identity=_USER, target_owner_identity=_ADMIN)
    )
    assert not decision.allowed
    assert decision.error_code == SESSION_TARGET_OUT_OF_SCOPE


# --- 不泄露存在性 ---

def test_missing_target_and_foreign_target_share_one_code() -> None:
    missing = decide_session_messaging(_req(target_owner_identity=None))
    foreign = decide_session_messaging(_req(target_owner_identity=_USER))
    assert not missing.allowed and not foreign.allowed
    assert missing.error_code == foreign.error_code == SESSION_TARGET_OUT_OF_SCOPE
    assert missing.scope_warnings == foreign.scope_warnings


# --- 自派任务 / 自消息 ---

def test_admin_task_to_self_rejected() -> None:
    decision = decide_session_messaging(
        _req(kind="task", sender_thread_id="t1", target_thread_id="t1")
    )
    assert not decision.allowed
    assert decision.error_code == SESSION_TASK_TARGET_SELF


def test_admin_message_to_self_allowed() -> None:
    decision = decide_session_messaging(
        _req(kind="message", sender_thread_id="t1", target_thread_id="t1")
    )
    assert decision.allowed


# --- 开关 ---

def test_admin_message_switch_off_rejected() -> None:
    decision = decide_session_messaging(_req(messaging_admin_enabled=False))
    assert not decision.allowed
    assert decision.error_code == SESSION_MESSAGING_DISABLED


def test_admin_task_switch_off_rejected() -> None:
    decision = decide_session_messaging(_req(kind="task", task_admin_enabled=False))
    assert not decision.allowed
    assert decision.error_code == SESSION_TASK_NOT_ALLOWED


def test_user_message_open_allows_for_user() -> None:
    # 开关打开后普通用户同 owner 可以发消息（用户隔离期才开，但判定逻辑要正确）。
    decision = decide_session_messaging(
        _req(kind="message", sender_identity=_USER, target_owner_identity=_USER, messaging_user_enabled=True)
    )
    assert decision.allowed


# --- IM 目标拒绝（dev 审阅点 3）---

def test_im_target_channel_rejected() -> None:
    decision = decide_session_messaging(_req(kind="message", target_channel="feishu"))
    assert not decision.allowed
    assert decision.error_code == SESSION_TARGET_CHANNEL_UNSUPPORTED


def test_user_im_target_rejected_channel_code_first() -> None:
    # 普通用户 + IM 目标：渠道路径先于开关判定，返回渠道码。
    decision = decide_session_messaging(
        _req(kind="message", sender_identity=_USER, target_owner_identity=_USER, target_channel="feishu")
    )
    assert not decision.allowed
    assert decision.error_code == SESSION_TARGET_CHANNEL_UNSUPPORTED


def test_local_target_channel_allowed() -> None:
    decision = decide_session_messaging(_req(kind="message", target_channel="chat"))
    assert decision.allowed


# --- 关闭时工具不可见（dev 审阅点 1）---

def test_admin_tool_visible_when_admin_switch_on() -> None:
    assert session_messaging_tool_visible(_Home("main"), _Cfg(admin=True, user=False)) is True


def test_admin_tool_hidden_when_admin_switch_off() -> None:
    assert session_messaging_tool_visible(_Home("main"), _Cfg(admin=False, user=True)) is False


def test_user_tool_hidden_by_default() -> None:
    assert session_messaging_tool_visible(_Home("user"), _Cfg(admin=True, user=False)) is False


def test_user_tool_visible_when_user_switch_on() -> None:
    assert session_messaging_tool_visible(_Home("user"), _Cfg(admin=True, user=True)) is True


def test_task_tool_visible_only_for_admin() -> None:
    assert session_task_tool_visible(_Home("main"), _Cfg(admin=True, user=False)) is True
    assert session_task_tool_visible(_Home("user"), _Cfg(admin=True, user=True)) is False


class _Home:
    def __init__(self, owner_kind: str) -> None:
        self.owner_kind = owner_kind


class _Cfg:
    def __init__(self, *, admin: bool, user: bool) -> None:
        self.session_messaging_admin_enabled = admin
        self.session_messaging_user_enabled = user
        self.session_task_admin_enabled = admin


# --- kind 非法 ---

@pytest.mark.parametrize("kind", ["", "broadcast", "TASK ", "Message"])
def test_invalid_kind_rejected(kind: str) -> None:
    decision = decide_session_messaging(_req(kind=kind))
    # "task " 会 trim 成 task，"Message" 会 lower 成 message；其余非法。
    if kind.strip().lower() not in {"message", "task"}:
        assert not decision.allowed
        assert decision.error_code == "SESSION_KIND_INVALID"
