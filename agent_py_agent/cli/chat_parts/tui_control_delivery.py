from __future__ import annotations

"""LLM: Persist TUI slash controls until their Gateway operation receipt is resolved.

模块用途: 在 TUI 发送 `/stop`、`/compact`、`/btw` 等控制命令前保存稳定消息 ID，
断线后用同一 ID 重试，拿到 operation_id 后只读查询，避免用户重按命令造成第二次副作用。
"""

import hashlib
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from ...agent.conversation.control_commands import ConversationControlResult
from ...agent.gateway_parts.io import (
    locked_file_transition,
    read_json_file_report,
    write_json_file_atomic,
)

_CONTROL_OUTBOX_SCHEMA = "tui_control_operation_outbox.v1"


# LLM: One row freezes the exact command and expected turn observed at Enter time. Retry timing
# and operation id are transport state; neither may alter the command digest.
# 类用途: 保存一条尚未完成对账的 TUI 控制命令及其稳定身份。
@dataclass(frozen=True)
class TuiControlOperationEntry:
    message_id: str
    command_text: str
    command_kind: str
    expected_turn_id: str = ""
    operation_id: str = ""
    attempts: int = 0
    next_attempt_at: float = 0.0

    # LLM: Serialization is complete enough to replay the same POST or switch to read-only GET.
    # 函数用途: 把控制 outbox 行转换成持久 JSON 字典。
    def to_dict(self) -> dict[str, object]:
        return {
            "message_id": self.message_id,
            "command_text": self.command_text,
            "command_kind": self.command_kind,
            "expected_turn_id": self.expected_turn_id,
            "operation_id": self.operation_id,
            "attempts": self.attempts,
            "next_attempt_at": self.next_attempt_at,
        }

    # LLM: Missing identity/content is corruption; a partial row is never retried with defaults.
    # 函数用途: 从 JSON 校验并恢复一条 TUI 控制 outbox 行。
    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> TuiControlOperationEntry:
        try:
            attempts = max(0, int(payload.get("attempts") or 0))
            next_attempt_at = max(0.0, float(payload.get("next_attempt_at") or 0.0))
        except (TypeError, ValueError) as exc:
            raise ValueError("TUI control outbox retry state is invalid") from exc
        entry = cls(
            message_id=str(payload.get("message_id") or "").strip(),
            command_text=str(payload.get("command_text") or "").strip(),
            command_kind=str(payload.get("command_kind") or "").strip(),
            expected_turn_id=str(payload.get("expected_turn_id") or "").strip(),
            operation_id=str(payload.get("operation_id") or "").strip(),
            attempts=attempts,
            next_attempt_at=next_attempt_at,
        )
        if not entry.message_id or not entry.command_text or not entry.command_kind:
            raise ValueError("TUI control outbox entry is invalid")
        return entry


# LLM: One session-level worker owns retry timing. It re-POSTs only before an operation id is
# known, always with the original message id; afterwards it can only poll the server receipt.
# 类用途: 统一处理本会话所有待确认控制命令并在终态后通知 TUI。
class TuiControlOperationReconciler:
    # LLM: Construction restores rows before starting the sole daemon; disk errors are surfaced
    # through on_error and never replaced by an empty outbox.
    # 函数用途: 创建控制命令对账器，恢复未完成行并按需启动后台线程。
    def __init__(
        self,
        *,
        path: Path,
        submit: Callable[[TuiControlOperationEntry], ConversationControlResult],
        status: Callable[[str], ConversationControlResult],
        on_restore: Callable[[TuiControlOperationEntry], None],
        on_complete: Callable[[TuiControlOperationEntry, ConversationControlResult], None],
        on_terminal_unknown: Callable[
            [TuiControlOperationEntry, ConversationControlResult], None
        ],
        on_conflict: Callable[[TuiControlOperationEntry], None],
        on_error: Callable[[BaseException], None],
        stop_event: threading.Event,
        initial_delay: float,
        maximum_delay: float,
    ) -> None:
        self.path = Path(path)
        self.submit = submit
        self.status = status
        self.on_complete = on_complete
        self.on_terminal_unknown = on_terminal_unknown
        self.on_conflict = on_conflict
        self.on_error = on_error
        self.stop_event = stop_event
        self.initial_delay = max(0.05, float(initial_delay))
        self.maximum_delay = max(self.initial_delay, float(maximum_delay))
        self._wake = threading.Event()
        self._lock = threading.RLock()
        self._worker: threading.Thread | None = None
        try:
            restored = self._read_entries()
        except Exception as exc:
            restored = {}
            self.on_error(exc)
        for entry in restored.values():
            on_restore(entry)
        if restored or self.path.exists():
            self._ensure_worker_started()

    # LLM: Persist-before-send and same-id comparison prevent a second command from borrowing a
    # durable operation identity after response loss.
    # 函数用途: 保存一条新控制命令并唤醒统一对账线程。
    def enqueue(self, entry: TuiControlOperationEntry) -> None:
        with self._lock, locked_file_transition(self._transition_path()):
            entries = self._read_entries_locked()
            existing = entries.get(entry.message_id)
            if existing is not None and _control_entry_identity(existing) != _control_entry_identity(
                entry
            ):
                raise ValueError("TUI control message id was reused with different input")
            entries[entry.message_id] = existing or entry
            self._write_entries_locked(entries)
        self._ensure_worker_started()
        self._wake.set()

    # LLM: Lazy creation keeps empty TUI sessions thread-free and guarantees one worker per
    # reconciler instance under concurrent first enqueues.
    # 函数用途: 在首次有未完成控制命令时启动后台对账线程。
    def _ensure_worker_started(self) -> None:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            if self.stop_event.is_set():
                return
            self._worker = threading.Thread(
                target=self._run,
                name=f"tui-control-reconciler-{self.path.stem[-8:]}",
                daemon=True,
            )
            self._worker.start()

    # LLM: Transient disk/transport failures back off without killing the sole session worker.
    # 函数用途: 循环处理到期控制命令并等待下一次唤醒或退避时间。
    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                entries = self._read_entries()
                now = time.time()
                due = [entry for entry in entries.values() if entry.next_attempt_at <= now]
                for entry in due[:32]:
                    if self.stop_event.is_set():
                        return
                    self._reconcile_one(entry)
                remaining = self._read_entries()
                next_due = min(
                    (entry.next_attempt_at for entry in remaining.values()),
                    default=now + self.maximum_delay,
                )
                timeout = max(0.05, min(self.maximum_delay, next_due - time.time()))
            except Exception as exc:
                self.on_error(exc)
                timeout = min(self.maximum_delay, max(self.initial_delay, 0.25))
            self._wake.wait(timeout)
            self._wake.clear()

    # LLM: A known operation id irrevocably switches the row to GET-only reconciliation. Completed,
    # terminal_unknown, conflict, and rejected are structural states, not inferred from message text.
    # 函数用途: 对账一条控制命令并按服务端结构化状态完成或继续退避。
    def _reconcile_one(self, entry: TuiControlOperationEntry) -> None:
        try:
            result = self.status(entry.operation_id) if entry.operation_id else self.submit(entry)
        except Exception:
            result = ConversationControlResult(
                entry.command_kind,
                False,
                "控制操作状态暂时无法确认。",
                control_state="transport_unknown",
            )
        operation_id = str(result.operation_id or entry.operation_id).strip()
        if result.control_state == "completed" and not _control_result_needs_poll(result):
            if self._finish(
                entry.message_id,
                lambda: self.on_complete(entry, result),
            ):
                return
        elif (
            result.control_state == "terminal_unknown"
            and entry.command_kind == "steer"
            and operation_id
        ):
            # wrapper 状态未知时，guidance 回执仍可能稍后收成 accepted/rejected；保留 GET-only 行。
            pass
        elif result.control_state in {"terminal_unknown", "rejected"}:
            if self._finish(
                entry.message_id,
                lambda: self.on_terminal_unknown(entry, result),
            ):
                return
        elif result.control_state == "conflict":
            if self._finish(entry.message_id, lambda: self.on_conflict(entry)):
                return
        attempts = entry.attempts + 1
        base = min(self.maximum_delay, self.initial_delay * (2 ** min(attempts, 12)))
        delay = min(self.maximum_delay, base * random.uniform(0.8, 1.2))
        self._replace(
            replace(
                entry,
                operation_id=operation_id,
                attempts=attempts,
                next_attempt_at=time.time() + delay,
            )
        )

    # LLM: Callback may repeat after a process crash but the server operation can never repeat;
    # callbacks are limited to idempotent local notice/console projections.
    # 函数用途: 执行终态界面动作，成功后删除对应 outbox 行。
    def _finish(self, message_id: str, callback: Callable[[], None]) -> bool:
        try:
            callback()
        except Exception:
            return False
        with self._lock, locked_file_transition(self._transition_path()):
            entries = self._read_entries_locked()
            entries.pop(message_id, None)
            self._write_entries_locked(entries)
        return True

    # LLM: Backoff updates replace only a still-present row with the same immutable client id.
    # 函数用途: 保存一条控制命令的新 operation ID 和下次重试时间。
    def _replace(self, entry: TuiControlOperationEntry) -> None:
        with self._lock, locked_file_transition(self._transition_path()):
            entries = self._read_entries_locked()
            current = entries.get(entry.message_id)
            if current is None:
                return
            if _control_entry_identity(current) != _control_entry_identity(entry):
                raise ValueError("TUI control outbox identity changed during retry")
            entries[entry.message_id] = entry
            self._write_entries_locked(entries)

    # LLM: All readers share the same cross-process transition as writers.
    # 函数用途: 读取当前会话全部有效控制 outbox 行。
    def _read_entries(self) -> dict[str, TuiControlOperationEntry]:
        with self._lock, locked_file_transition(self._transition_path()):
            return self._read_entries_locked()

    # LLM: Missing file means no work; an existing empty, malformed, or wrong-schema file remains
    # an explicit error and is never overwritten as a fresh outbox.
    # 函数用途: 在锁内读取并校验控制 outbox JSON。
    def _read_entries_locked(self) -> dict[str, TuiControlOperationEntry]:
        report = read_json_file_report(self.path, context="tui.control_outbox.read")
        if report.load_error is not None:
            raise RuntimeError("TUI control outbox is unreadable")
        if not report.payload:
            if self.path.exists():
                raise RuntimeError("TUI control outbox is empty or invalid")
            return {}
        if report.payload.get("schema_version") != _CONTROL_OUTBOX_SCHEMA:
            raise RuntimeError("TUI control outbox schema is invalid")
        rows = report.payload.get("entries")
        if not isinstance(rows, dict):
            raise RuntimeError("TUI control outbox entries are invalid")
        entries: dict[str, TuiControlOperationEntry] = {}
        for message_id, payload in rows.items():
            if not isinstance(payload, dict):
                raise RuntimeError("TUI control outbox row is invalid")
            entry = TuiControlOperationEntry.from_dict(payload)
            if entry.message_id != str(message_id):
                raise RuntimeError("TUI control outbox key conflicts")
            entries[entry.message_id] = entry
        return entries

    # LLM: One atomic file is the canonical pending set for this session.
    # 函数用途: 在锁内保存全部控制 outbox 行。
    def _write_entries_locked(self, entries: dict[str, TuiControlOperationEntry]) -> None:
        write_json_file_atomic(
            self.path,
            {
                "schema_version": _CONTROL_OUTBOX_SCHEMA,
                "entries": {key: value.to_dict() for key, value in sorted(entries.items())},
                "updated_at": time.time(),
            },
        )

    # LLM: Each session uses a colocated transition file; independent sessions never share locks.
    # 函数用途: 返回控制 outbox 的跨进程状态转换锁路径。
    def _transition_path(self) -> Path:
        return self.path.with_name(f".{self.path.name}.transition")


# LLM: Stable identity excludes operation/backoff fields so transport progress may update without
# permitting command or exact-turn drift.
# 函数用途: 返回控制 outbox 行不可变身份。
def _control_entry_identity(entry: TuiControlOperationEntry) -> tuple[str, str, str, str]:
    return (
        entry.message_id,
        entry.command_text,
        entry.command_kind,
        entry.expected_turn_id,
    )


# LLM: `/btw` remains pending until delivery is accepted or rejected even though execution of the
# wrapper operation itself is completed.
# 函数用途: 判断控制结果是否仍需按 operation_id 只读轮询。
def _control_result_needs_poll(result: ConversationControlResult) -> bool:
    return result.kind == "steer" and result.delivery_status in {"", "unknown"}


# LLM: Session ids are hashed before filename construction so user/channel text cannot escape the
# Gateway client_outbox directory.
# 函数用途: 返回一个 TUI 会话的控制操作 outbox 文件。
def tui_control_operation_outbox_path(gateway_root: Path, session_id: str) -> Path:
    digest = hashlib.sha256(str(session_id or "default").encode("utf-8")).hexdigest()
    return Path(gateway_root) / "client_outbox" / f"tui-control-{digest}.json"


__all__ = [
    "TuiControlOperationEntry",
    "TuiControlOperationReconciler",
    "tui_control_operation_outbox_path",
]
