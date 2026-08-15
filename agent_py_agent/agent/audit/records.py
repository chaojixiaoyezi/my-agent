
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class AuditAction(str, Enum):
    """审计动作枚举。"""

    CREATE_TASK = "CREATE_TASK"
    UPDATE_TASK = "UPDATE_TASK"
    DELETE_TASK = "DELETE_TASK"
    DISPATCH = "DISPATCH"
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    QUERY = "QUERY"
    ADMIN_ACCESS = "ADMIN_ACCESS"
    LOGIN = "LOGIN"
    LOGOUT = "LOGOUT"
    SESSION_START = "SESSION_START"
    SESSION_END = "SESSION_END"
    NOTIFICATION_SENT = "NOTIFICATION_SENT"
    NOTIFICATION_STORED = "NOTIFICATION_STORED"
    GATEWAY_REQUEST = "GATEWAY_REQUEST"
    GATEWAY_RESPONSE = "GATEWAY_RESPONSE"


class AuditStatus(str, Enum):
    """审计状态枚举。"""

    SUCCESS = "success"
    DENIED = "denied"
    ERROR = "error"


@dataclass
class AuditEntry:
    """审计日志条目。"""

    entry_id: str
    timestamp: float
    action: str
    user_id: str
    channel: str
    target_type: str  # task, session, user, gateway
    target_id: str
    status: str  # success, denied, error
    details: dict[str, Any] = field(default_factory=dict)
    ip_address: str = ""
    user_agent: str = ""

    def to_dict(self) -> dict[str, Any]:
        """转换为字典。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AuditEntry:
        """从字典创建。"""
        return cls(**data)


@dataclass(frozen=True)
class LogParams:
    """Bundle of AuditLogger.log parameters."""

    action: AuditAction | str
    user_id: str
    channel: str
    target_type: str
    target_id: str
    status: AuditStatus | str = AuditStatus.SUCCESS
    details: dict[str, Any] | None = None
    ip_address: str = ""
    user_agent: str = ""


def enum_value(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


__all__ = [
    "AuditAction",
    "AuditEntry",
    "AuditStatus",
    "LogParams",
    "enum_value",
]
