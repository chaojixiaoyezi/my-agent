
"""权限模型 — Role 枚举、Permission 数据类、策略规则。

定义角色（ADMIN / USER）和权限对象，以及从 channel/user_id 推断角色的策略。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Role(Enum):
    """用户角色枚举。"""

    ADMIN = "admin"
    USER = "user"


class Action(Enum):
    """可授权的操作类型。"""

    READ_TASK = "read_task"
    WRITE_TASK = "write_task"
    READ_USER_DATA = "read_user_data"
    ADMIN_QUERY = "admin_query"


@dataclass
class Permission:
    """权限对象：描述一个用户在某个通道下的权限范围。"""

    role: Role
    user_id: str
    channels: list[str] = field(default_factory=list)
    can_access_all_users: bool = False

    def can(self, action: Action) -> bool:
        """检查是否拥有指定操作的权限。"""
        if self.role == Role.ADMIN:
            return True
        # USER 角色只能读自己的任务
        if action == Action.READ_TASK:
            return True
        return False


# ---------------------------------------------------------------------------
# 角色策略：从 channel 和 user_id 推断角色
# ---------------------------------------------------------------------------

# 终端通道天然是管理员
_TERMINAL_CHANNELS = {"chat", "cli", "terminal", ""}


def infer_role(channel: str, user_id: str, admin_user_id: str = "admin") -> Role:
    """根据通道和用户 ID 推断角色。

    规则：
    - 终端通道（chat/cli/terminal）→ ADMIN，天然全权
    - 外部通道 + user_id 是 admin_user_id → ADMIN
    - 其他外部通道 → USER

    Args:
        channel: 通道名（如 feishu / qq / chat）
        user_id: 用户 ID
        admin_user_id: 管理员用户 ID（默认 admin）

    Returns:
        Role: 推断出的角色"""
    if channel.strip() in _TERMINAL_CHANNELS or not channel.strip():
        return Role.ADMIN
    if user_id == admin_user_id:
        return Role.ADMIN
    return Role.USER


def build_permission(channel: str, user_id: str, admin_user_id: str = "admin") -> Permission:
    """根据通道和用户 ID 构建权限对象。

    Args:
        channel: 通道名
        user_id: 用户 ID
        admin_user_id: 管理员用户 ID

    Returns:
        Permission: 完整的权限对象"""
    role = infer_role(channel, user_id, admin_user_id)
    can_all = role == Role.ADMIN
    return Permission(
        role=role,
        user_id=user_id,
        channels=[channel] if channel else [],
        can_access_all_users=can_all,
    )
