"""个人私聊会话锁 + 密码解锁(

个人 Agent 私聊闲置超过阈值(默认 3 小时)后锁定,需密码解锁;群聊不锁。
密码 scrypt 加盐慢散列、永不明文;解锁走飞书卡片输入框、不进聊天记录;
暴破失败指数退避锁定(monotonic 防墙钟前跳绕过)。
"""

from .passwords import (
    PasswordPolicyError,
    hash_password,
    validate_password_policy,
    verify_password,
)
from .service import DEFAULT_IDLE_SECONDS, UnlockService, UnlockStatus
from .store import SessionLockStore

__all__ = [
    "DEFAULT_IDLE_SECONDS",
    "PasswordPolicyError",
    "SessionLockStore",
    "UnlockService",
    "UnlockStatus",
    "hash_password",
    "validate_password_policy",
    "verify_password",
]
