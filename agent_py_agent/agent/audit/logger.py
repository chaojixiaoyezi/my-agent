
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .records import (
    AuditAction,
    AuditEntry,
    AuditStatus,
    LogParams,
    enum_value,
    normalize_log_params,
)

if TYPE_CHECKING:
    from ..local_store import LocalStore
    from ..settings.config import AgentConfig


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
        log_params = normalize_log_params(
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
            action=enum_value(log_params.action),
            user_id=log_params.user_id,
            channel=log_params.channel,
            target_type=log_params.target_type,
            target_id=log_params.target_id,
            status=enum_value(log_params.status),
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
        request: AuditTaskUpdateRequest | None = None,
        *,
        task_id: str = "",
        user_id: str = "",
        channel: str = "",
        status_before: str = "",
        status_after: str = "",
        details: dict[str, Any] | None = None,
    ) -> AuditEntry:
        """记录更新任务。"""
        request = request or AuditTaskUpdateRequest(
            task_id=task_id,
            user_id=user_id,
            channel=channel,
            status_before=status_before,
            status_after=status_after,
            details=details,
        )
        return self.log(
            LogParams(
                action=AuditAction.UPDATE_TASK,
                user_id=request.user_id,
                channel=request.channel,
                target_type="task",
                target_id=request.task_id,
                details={
                    "status_before": request.status_before,
                    "status_after": request.status_after,
                    **(request.details or {}),
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
        request: AuditAccessDeniedRequest | None = None,
        *,
        action: AuditAction | str = "",
        user_id: str = "",
        channel: str = "",
        target_type: str = "",
        target_id: str = "",
        reason: str = "",
    ) -> AuditEntry:
        """记录访问拒绝。"""
        request = request or AuditAccessDeniedRequest(action, user_id, channel, target_type, target_id, reason)
        return self.log(
            LogParams(
                action=request.action,
                user_id=request.user_id,
                channel=request.channel,
                target_type=request.target_type,
                target_id=request.target_id,
                status=AuditStatus.DENIED,
                details={"reason": request.reason},
            )
        )

    def log_error(
        self,
        request: AuditErrorRequest | None = None,
        *,
        action: AuditAction | str = "",
        user_id: str = "",
        channel: str = "",
        target_type: str = "",
        target_id: str = "",
        error: str = "",
    ) -> AuditEntry:
        """记录错误。"""
        request = request or AuditErrorRequest(action, user_id, channel, target_type, target_id, error)
        return self.log(
            LogParams(
                action=request.action,
                user_id=request.user_id,
                channel=request.channel,
                target_type=request.target_type,
                target_id=request.target_id,
                status=AuditStatus.ERROR,
                details={"error": request.error},
            )
        )


__all__ = ["AuditLogger", "AuditEntry", "AuditAction", "AuditStatus", "LogParams"]


@dataclass(frozen=True)
class AuditTaskUpdateRequest:
    """LLM: bundle for audit task status transitions."""

    task_id: str
    user_id: str
    channel: str
    status_before: str
    status_after: str
    details: dict[str, Any] | None = None


@dataclass(frozen=True)
class AuditAccessDeniedRequest:
    """LLM: bundle for audit denied events."""

    action: AuditAction | str
    user_id: str
    channel: str
    target_type: str
    target_id: str
    reason: str


@dataclass(frozen=True)
class AuditErrorRequest:
    """LLM: bundle for audit error events."""

    action: AuditAction | str
    user_id: str
    channel: str
    target_type: str
    target_id: str
    error: str
