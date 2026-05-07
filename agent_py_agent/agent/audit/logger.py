
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..local_store import LocalStore
    from ..settings.config import AgentConfig


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


def _normalize_log_params(
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


def _enum_value(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


class AuditLogger:

    def __init__(self, config: AgentConfig, local_store: LocalStore | None = None):
        self.config = config
        self._local_store = local_store
        self._audit_root = Path(getattr(config, "audit_log_path", "data/audit"))
        self._audit_root.mkdir(parents=True, exist_ok=True)
        self._audit_file = self._audit_root / "audit.jsonl"

    def _generate_entry_id(self) -> str:
        """生成条目 ID。"""
        import secrets

        timestamp = int(time.time())
        random_part = secrets.token_hex(2)
        return f"audit_{timestamp}_{random_part}"

    def log(
        self,
        params: LogParams | AuditAction | str = None,
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
    ) -> AuditEntry:
        log_params = _normalize_log_params(
            params,
            action=action,
            user_id=user_id,
            channel=channel,
            target_type=target_type,
            target_id=target_id,
            status=status,
            details=details,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        entry = self._entry_from_params(log_params)
        self._write_to_file(entry)
        if self._local_store is not None:
            self._write_to_local_store(entry)
        return entry

    def _entry_from_params(self, log_params: LogParams) -> AuditEntry:
        return AuditEntry(
            entry_id=self._generate_entry_id(),
            timestamp=time.time(),
            action=_enum_value(log_params.action),
            user_id=log_params.user_id,
            channel=log_params.channel,
            target_type=log_params.target_type,
            target_id=log_params.target_id,
            status=_enum_value(log_params.status),
            details=log_params.details or {},
            ip_address=log_params.ip_address,
            user_agent=log_params.user_agent,
        )

    def _write_to_file(self, entry: AuditEntry) -> None:
        line = json.dumps(entry.to_dict(), ensure_ascii=False)
        with open(self._audit_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def _write_to_local_store(self, entry: AuditEntry) -> None:
        try:
            from ..local_storage import LocalStore

            if isinstance(self._local_store, LocalStore):
                self._local_store.record_event(
                    source="audit",
                    event_type=entry.action,
                    payload={
                        "entry_id": entry.entry_id,
                        "user_id": entry.user_id,
                        "channel": entry.channel,
                        "target_type": entry.target_type,
                        "target_id": entry.target_id,
                        "status": entry.status,
                        "details": entry.details,
                    },
                )
        except Exception:
            # LocalStore 不可用时静默失败
            pass

    # 便捷方法

    def log_create_task(
        self,
        task_id: str,
        user_id: str,
        channel: str,
        details: dict[str, Any] | None = None,
    ) -> AuditEntry:
        """记录创建任务。"""
        return self.log(
            LogParams(
                action=AuditAction.CREATE_TASK,
                user_id=user_id,
                channel=channel,
                target_type="task",
                target_id=task_id,
                details=details,
            )
        )

    def log_update_task(
        self,
        task_id: str,
        user_id: str,
        channel: str,
        status_before: str,
        status_after: str,
        details: dict[str, Any] | None = None,
    ) -> AuditEntry:
        """记录更新任务。"""
        return self.log(
            LogParams(
                action=AuditAction.UPDATE_TASK,
                user_id=user_id,
                channel=channel,
                target_type="task",
                target_id=task_id,
                details={
                    "status_before": status_before,
                    "status_after": status_after,
                    **(details or {}),
                },
            )
        )

    def log_dispatch(
        self,
        task_id: str,
        user_id: str,
        channel: str,
        details: dict[str, Any] | None = None,
    ) -> AuditEntry:
        """记录调度操作。"""
        return self.log(
            LogParams(
                action=AuditAction.DISPATCH,
                user_id=user_id,
                channel=channel,
                target_type="task",
                target_id=task_id,
                details=details,
            )
        )

    def log_access_denied(
        self,
        action: AuditAction | str,
        user_id: str,
        channel: str,
        target_type: str,
        target_id: str,
        reason: str,
    ) -> AuditEntry:
        """记录访问拒绝。"""
        return self.log(
            LogParams(
                action=action,
                user_id=user_id,
                channel=channel,
                target_type=target_type,
                target_id=target_id,
                status=AuditStatus.DENIED,
                details={"reason": reason},
            )
        )

    def log_error(
        self,
        action: AuditAction | str,
        user_id: str,
        channel: str,
        target_type: str,
        target_id: str,
        error: str,
    ) -> AuditEntry:
        """记录错误。"""
        return self.log(
            LogParams(
                action=action,
                user_id=user_id,
                channel=channel,
                target_type=target_type,
                target_id=target_id,
                status=AuditStatus.ERROR,
                details={"error": error},
            )
        )


__all__ = ["AuditLogger", "AuditEntry", "AuditAction", "AuditStatus"]
