# LLM: Audit module; keep JSONL entry shape and query filters stable.
# 模块用途: 记录和查询关键操作审计事件，支持后续排查和治理。

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


# LLM: AuditAction is a 审计系统 boundary object; coordinate field or method changes with callers, docs, and focused tests.
# 类用途: 审计动作枚举。
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


# LLM: AuditStatus is a 审计系统 boundary object; coordinate field or method changes with callers, docs, and focused tests.
# 类用途: 审计状态枚举。
class AuditStatus(str, Enum):
    """审计状态枚举。"""

    SUCCESS = "success"
    DENIED = "denied"
    ERROR = "error"


# LLM: AuditEntry is a 审计系统 boundary object; coordinate field or method changes with callers, docs, and focused tests.
# 类用途: 审计日志条目。
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

    # LLM: AuditEntry.to_dict belongs to 审计系统; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 转换为字典。。
    def to_dict(self) -> dict[str, Any]:
        """转换为字典。"""
        return asdict(self)

    # LLM: AuditEntry.from_dict belongs to 审计系统; keep caller-visible returns, errors, and side effects aligned with focused tests.
    # 函数用途: 从字典创建。。
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AuditEntry:
        """从字典创建。"""
        return cls(**data)


# LLM: LogParams is a 审计系统 boundary object; coordinate field or method changes with callers, docs, and focused tests.
# 类用途: Bundle of AuditLogger.log parameters.
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


# LLM: normalize_log_params belongs to 审计系统; keep caller-visible returns, errors, and side effects aligned with focused tests.
# 函数用途: 把新旧两种 log 调用形式统一成 LogParams。。
def normalize_log_params(
    params: LogParams | AuditAction | str | None,
    *,
    action: AuditAction | str | None = None,
    user_id: str = "",
    channel: str = "",
    target_type: str = "",
    target_id: str = "",
    status: AuditStatus | str = AuditStatus.SUCCESS,
    details: dict[str, Any] | None = None,
    ip_address: str = "",
    user_agent: str = "",
) -> LogParams:
    """把新旧两种 log 调用形式统一成 LogParams。"""

    if isinstance(params, LogParams):
        return params
    action_value = params if params is not None else action
    if action_value is not None:
        return LogParams(
            action=action_value,
            user_id=user_id,
            channel=channel,
            target_type=target_type,
            target_id=target_id,
            status=status,
            details=details,
            ip_address=ip_address,
            user_agent=user_agent,
        )
    raise TypeError("log() requires LogParams or action")


# LLM: enum_value belongs to 审计系统; keep caller-visible returns, errors, and side effects aligned with focused tests.
# 函数用途: 完成 审计系统 里的 enum_value 步骤，保持现有返回值、异常和副作用语义。
def enum_value(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


__all__ = [
    "AuditAction",
    "AuditEntry",
    "AuditStatus",
    "LogParams",
    "enum_value",
    "normalize_log_params",
]
