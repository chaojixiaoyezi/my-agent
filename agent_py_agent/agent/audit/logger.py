
# LLM: Audit event writer; keep JSONL and LocalStore payload shapes stable for later querying.
# 模块用途: 把任务、调度、状态等关键操作写入审计日志，并可同步写入本地事件库。

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


# LLM: Bridges high-level audit actions to JSONL and optional LocalStore records; preserve public convenience methods.
# 类用途: 负责生成审计记录、落盘到 audit.jsonl，并在可用时同步到 LocalStore。
class AuditLogger:

    # LLM: Creates the audit directory and stores optional LocalStore integration; constructor intentionally prepares filesystem state.
    # 函数用途: 初始化审计日志路径，确保目录存在，并保存可选的本地事件库引用。
    def __init__(self, config: AgentConfig, local_store: LocalStore | None = None):
        self.config = config
        self._local_store = local_store
        self._audit_root = Path(getattr(config, "audit_log_path", "data/audit"))
        self._audit_root.mkdir(parents=True, exist_ok=True)
        self._audit_file = self._audit_root / "audit.jsonl"

    # LLM: ID format is timestamp plus short random suffix; keep it stable enough for log readers.
    # 函数用途: 生成审计记录 ID，用时间戳方便粗略排序，用随机段降低冲突概率。
    def _generate_entry_id(self) -> str:
        """生成条目 ID。"""
        import secrets

        timestamp = int(time.time())
        random_part = secrets.token_hex(2)
        return f"audit_{timestamp}_{random_part}"

    # LLM: Main audit entry point; accepts legacy loose fields through normalize_log_params and always returns the stored entry.
    # 函数用途: 记录一次审计事件，兼容旧参数写法，最终写入文件并返回 AuditEntry。
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

    # LLM: Converts normalized bundle data into the persisted AuditEntry shape.
    # 函数用途: 把 LogParams 转成完整审计记录，补上 ID、时间戳和枚举字符串。
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

    # LLM: Appends one JSON object per line; downstream tools rely on UTF-8 and ensure_ascii=False.
    # 函数用途: 把审计记录追加写入 audit.jsonl，每条记录占一行。
    def _write_to_file(self, entry: AuditEntry) -> None:
        line = json.dumps(entry.to_dict(), ensure_ascii=False)
        with open(self._audit_file, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    # LLM: Optional best-effort mirror into LocalStore; failures are intentionally swallowed to keep audit JSONL primary.
    # 函数用途: 尝试把审计事件同步到 LocalStore，失败时不影响主审计日志写入。
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

    # LLM: Convenience wrapper for CREATE_TASK audit events; keep target_type as task for query compatibility.
    # 函数用途: 记录创建任务事件，少让调用方重复填写 action 和 target_type。
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

    # LLM: Convenience wrapper for dispatch audit events with task as the target.
    # 函数用途: 记录某个任务被调度的审计事件。
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

# LLM: AuditTaskUpdateRequest is a 审计系统 boundary object; coordinate field or method changes with callers, docs, and focused tests.
# 类用途: 保存 AuditTaskUpdateRequest 的输入字段，调用方先构造这个对象再进入 审计系统，避免继续散传参数。
@dataclass(frozen=True)
class AuditTaskUpdateRequest:
    """bundle for audit task status transitions."""

    task_id: str
    user_id: str
    channel: str
    status_before: str
    status_after: str
    details: dict[str, Any] | None = None


# LLM: AuditAccessDeniedRequest is a 审计系统 boundary object; coordinate field or method changes with callers, docs, and focused tests.
# 类用途: 保存 AuditAccessDeniedRequest 的输入字段，调用方先构造这个对象再进入 审计系统，避免继续散传参数。
@dataclass(frozen=True)
class AuditAccessDeniedRequest:
    """bundle for audit denied events."""

    action: AuditAction | str
    user_id: str
    channel: str
    target_type: str
    target_id: str
    reason: str


# LLM: AuditErrorRequest is a 审计系统 boundary object; coordinate field or method changes with callers, docs, and focused tests.
# 类用途: 保存 AuditErrorRequest 的输入字段，调用方先构造这个对象再进入 审计系统，避免继续散传参数。
@dataclass(frozen=True)
class AuditErrorRequest:
    """bundle for audit error events."""

    action: AuditAction | str
    user_id: str
    channel: str
    target_type: str
    target_id: str
    error: str


# LLM: _log_update_task belongs to 审计系统; keep caller-visible returns, errors, and side effects aligned with focused tests.
# 函数用途: 把结果、日志或状态写回磁盘/索引，改动时要确认审计记录和失败处理。
def _log_update_task(
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
    request = request or AuditTaskUpdateRequest(task_id, user_id, channel, status_before, status_after, details)
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


# LLM: _log_access_denied belongs to 审计系统; keep caller-visible returns, errors, and side effects aligned with focused tests.
# 函数用途: 把结果、日志或状态写回磁盘/索引，改动时要确认审计记录和失败处理。
def _log_access_denied(
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


# LLM: _log_error belongs to 审计系统; keep caller-visible returns, errors, and side effects aligned with focused tests.
# 函数用途: 把结果、日志或状态写回磁盘/索引，改动时要确认审计记录和失败处理。
def _log_error(
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


AuditLogger.log_update_task = _log_update_task
AuditLogger.log_access_denied = _log_access_denied
AuditLogger.log_error = _log_error

__all__ = ["AuditLogger", "AuditEntry", "AuditAction", "AuditStatus", "LogParams"]
