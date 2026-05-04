"""审计日志记录器。

记录所有操作到审计日志：
- 存储到 data/audit/audit.jsonl
- 同时写入 LocalStore 的 events 表
"""

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


class AuditLogger:
    """审计日志记录器。

    记录所有操作到审计日志：
    - 存储到 data/audit/audit.jsonl
    - 可选写入 LocalStore events 表
    """

    def __init__(self, config: AgentConfig, local_store: LocalStore | None = None):
        """初始化审计日志记录器。

        Args:
            config: 智能体配置对象
            local_store: LocalStore 实例（可选）
        """
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

    def log(self, params: LogParams | AuditAction | str = None, **kwargs) -> AuditEntry:
        """记录审计日志。

        Args:
            params: LogParams对象，或action（向后兼容）
            **kwargs: 向后兼容的其他参数

        Returns:
            创建的 AuditEntry
        """
        # Backward compatibility: if called with keyword arguments, construct LogParams
        if isinstance(params, LogParams):
            log_params = params
        elif params is not None:
            # Backward compatible mode: params is actually the action
            log_params = LogParams(
                action=params,
                user_id=kwargs.pop("user_id", ""),
                channel=kwargs.pop("channel", ""),
                target_type=kwargs.pop("target_type", ""),
                target_id=kwargs.pop("target_id", ""),
                status=kwargs.pop("status", AuditStatus.SUCCESS),
                details=kwargs.pop("details", None),
                ip_address=kwargs.pop("ip_address", ""),
                user_agent=kwargs.pop("user_agent", ""),
            )
        elif kwargs:
            # All kwargs provided
            log_params = LogParams(
                action=kwargs.pop("action", AuditAction.QUERY),
                user_id=kwargs.pop("user_id", ""),
                channel=kwargs.pop("channel", ""),
                target_type=kwargs.pop("target_type", ""),
                target_id=kwargs.pop("target_id", ""),
                status=kwargs.pop("status", AuditStatus.SUCCESS),
                details=kwargs.pop("details", None),
                ip_address=kwargs.pop("ip_address", ""),
                user_agent=kwargs.pop("user_agent", ""),
            )
        else:
            raise TypeError("log() requires either params: LogParams or keyword arguments")

        entry = AuditEntry(
            entry_id=self._generate_entry_id(),
            timestamp=time.time(),
            action=log_params.action.value
            if isinstance(log_params.action, AuditAction)
            else log_params.action,
            user_id=log_params.user_id,
            channel=log_params.channel,
            target_type=log_params.target_type,
            target_id=log_params.target_id,
            status=log_params.status.value
            if isinstance(log_params.status, AuditStatus)
            else log_params.status,
            details=log_params.details or {},
            ip_address=log_params.ip_address,
            user_agent=log_params.user_agent,
        )

        # 写入 JSONL 文件
        self._write_to_file(entry)

        # 写入 LocalStore events 表
        if self._local_store is not None:
            self._write_to_local_store(entry)

        return entry

    def _write_to_file(self, entry: AuditEntry) -> None:
        """写入审计日志文件。

        Args:
            entry: 审计条目
        """
        line = json.dumps(entry.to_dict(), ensure_ascii=False)
        with open(self._audit_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def _write_to_local_store(self, entry: AuditEntry) -> None:
        """写入 LocalStore events 表。

        Args:
            entry: 审计条目
        """
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
