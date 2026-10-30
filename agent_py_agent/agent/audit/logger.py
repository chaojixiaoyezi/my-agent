# LLM: 审计唯一文件账保原脱敏/字节/顺序，新增非阻塞只透传原锁，文件失败不得宣称已记录；LocalStore旁路语义保持。
# 模块用途: 保存原审计事件，C可拒绝显式锁等待，不新建业务事务账。


from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..common.json_io import append_private_jsonl_records, path_thread_lock
from ..common.nofollow_fs import ensure_private_dir
from ..runtime_errors import runtime_error_report
from .paths import resolve_audit_paths
from .records import (
    AuditAction,
    AuditEntry,
    AuditStatus,
    LogParams,
    enum_value,
)

if TYPE_CHECKING:
    from ..local_storage import LocalStore
    from ..settings.config import AgentConfig


def _audit_enabled(config: AgentConfig) -> bool:
    raw_value = getattr(config, "audit_enabled", True)
    if isinstance(raw_value, str):
        normalized = raw_value.strip().lower()
        if normalized in {"false", "0"}:
            return False
        if normalized in {"true", "1"}:
            return True
        return True
    return bool(raw_value)


# 审计 details 里键名命中这些词的值一律脱敏(防 secret/口令/凭据落审计文件造成二次泄漏,审计 #13)。
# 学 参考实现 AuditLedger 禁用键 + 通道运行时 redactSensitiveText 默认开。command/output/path 属审计价值,不脱敏。
_FORBIDDEN_AUDIT_KEY_PARTS = (
    "secret", "token", "password", "passwd", "api_key", "apikey", "access_key", "secret_key",
    "private_key", "credential", "authorization", "ciphertext", "session_key",
)


def _is_forbidden_audit_key(key: object) -> bool:
    low = str(key).lower()
    return any(part in low for part in _FORBIDDEN_AUDIT_KEY_PARTS)


def _redact_audit_value(key: object, value: object) -> object:
    if _is_forbidden_audit_key(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return _redact_audit_details(value)  # 嵌套 dict 递归脱敏
    return value


def _redact_audit_details(details: dict) -> dict:
    """脱敏审计 details:键名命中敏感词的值替换为 [REDACTED](嵌套 dict 递归)。"""
    return {key: _redact_audit_value(key, value) for key, value in (details or {}).items()}


# LLM: 原文件先于可选LocalStore；文件失败上抛、旁路失败尽力诊断，不把审计条目当业务许可或完成事实。
# 类用途: 以原格式记录审计，按显式参数选择是否等待同一文件锁。
class AuditLogger:

    def __init__(
        self,
        config: AgentConfig,
        local_store: LocalStore | None = None,
        *,
        root: Path | str | None = None,
    ):
        self.config = config
        self._local_store = local_store
        self.enabled = _audit_enabled(config)
        paths = resolve_audit_paths(config, root=root)
        self._audit_root = paths.root
        self._audit_file = paths.log_file
        self._trusted_root = (
            Path(os.path.abspath(os.path.expanduser(os.fspath(root))))
            if root is not None
            else None
        )
        if self.enabled:
            if self._trusted_root is not None:
                ensure_private_dir(self._audit_root, canonical_root=self._trusted_root)
            else:
                ensure_private_dir(self._audit_root)

    def _generate_entry_id(self) -> str:
        """生成条目 ID:秒级戳(可读/可排序)+ UUID4(122 位,无碰撞)。

        原 2 字节随机(16 位)+ 秒级戳在同秒高频审计下生日碰撞概率高(~256 条/秒即约 50%),
        导致审计条目 ID 重复、溯源串号(审计 #16)。改 UUID4 彻底消除碰撞。
        """
        import uuid

        return f"audit_{int(time.time())}_{uuid.uuid4().hex}"

    # LLM: 默认保原file→localstore写序；C仅给文件锁传False，失败仍原样抛，不把点击审计当业务完成。
    # 函数用途: 记录同一原审计条目，可选拒绝文件锁等待。
    def log(
        self,
        params: LogParams,
        *, blocking: bool = True,
    ) -> AuditEntry:
        if not isinstance(params, LogParams):
            raise TypeError("AuditLogger.log() requires LogParams")
        entry = self._entry_from_params(params)
        if not self.enabled:
            return entry
        if blocking:
            self._write_to_file(entry)
        else:
            self._write_to_file(entry,blocking=False)
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
            details=_redact_audit_details(log_params.details or {}),  # 脱敏:密钥/口令不落审计文件(审计 #13)
            ip_address=log_params.ip_address,
            user_agent=log_params.user_agent,
        )

    # LLM: 审计账唯一落盘口：append_private_jsonl_records 在同一把线程锁 + fcntl 锁内追加，文件 0600、
    #   目录缺失才按 0700 新建（pdp 2026-10-03）；格式与旧裸写逐字节一致（ensure_ascii=False、不排序键、一行一条）。
    # 函数用途: 把一条审计条目以仅本人可读写权限追加进审计账文件。
    # LLM: 字节/脱敏/路径不变，只透传原append锁参数；锁忙不能假称已留审计。
    # 函数用途: 以原格式私有追加审计事实。
    def _write_to_file(self, entry: AuditEntry, *, blocking: bool = True) -> None:
        if self._trusted_root is not None:
            from .query import (
                UnsafeAuditPathError,
                _audit_append_lock,
                _audit_lock_key_path,
                _audit_parent_current,
                _audit_regular,
            )

            ensure_private_dir(self._audit_root, canonical_root=self._trusted_root)
            handle = path_thread_lock(_audit_lock_key_path(self._audit_file, self._trusted_root))
            if not handle.lock.acquire(blocking=blocking):
                raise BlockingIOError("共享文件线程锁繁忙")
            try:
                with _audit_append_lock(
                    None,
                    self._audit_file.name,
                    path=self._audit_file,
                    trusted_root=self._trusted_root,
                    blocking=blocking,
                ) as (parent, root):
                    _audit_parent_current(self._audit_file, root, parent)
                    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW
                    try:
                        fd = os.open(self._audit_file.name, flags, 0o600, dir_fd=parent)
                    except OSError as exc:
                        import errno

                        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                            raise UnsafeAuditPathError("unsafe audit path") from exc
                        raise
                    try:
                        _audit_regular(os.fstat(fd))
                        os.fchmod(fd, 0o600)
                        blob = (json.dumps(entry.to_dict(), ensure_ascii=False, sort_keys=False) + "\n").encode("utf-8")
                        os.write(fd, blob)
                    finally:
                        os.close(fd)
            finally:
                handle.lock.release()
            return
        append_private_jsonl_records(self._audit_file, [entry.to_dict()], sort_keys=False, blocking=blocking)

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
        except Exception as exc:
            self._write_audit_side_effect_error(entry, exc)

    # LLM: 审计写本地库失败不能反过来打断主流程：旁路错误同样按私有追加落进审计根，OSError 直接放弃。
    # 函数用途: 把审计旁路失败的运行时报告写进 audit_side_effect_errors.jsonl。
    def _write_audit_side_effect_error(self, entry: AuditEntry, exc: BaseException) -> None:
        report = runtime_error_report(exc, context="audit.local_store.record_event")
        report["entry_id"] = entry.entry_id
        report["action"] = entry.action
        side_effect_path = self._audit_root / "audit_side_effect_errors.jsonl"
        try:
            # 旁路错误同样写宿主审计目录：私有追加（0600/0700），内容与原 json.dumps(...)+\n 逐字节一致。
            append_private_jsonl_records(side_effect_path, [report], sort_keys=False)
        except OSError:
            return

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

@dataclass(frozen=True)
class AuditTaskUpdateRequest:
    """bundle for audit task status transitions."""

    task_id: str
    user_id: str
    channel: str
    status_before: str
    status_after: str
    details: dict[str, Any] | None = None


@dataclass(frozen=True)
class AuditAccessDeniedRequest:
    """bundle for audit denied events."""

    action: AuditAction | str
    user_id: str
    channel: str
    target_type: str
    target_id: str
    reason: str


@dataclass(frozen=True)
class AuditErrorRequest:
    """bundle for audit error events."""

    action: AuditAction | str
    user_id: str
    channel: str
    target_type: str
    target_id: str
    error: str


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
