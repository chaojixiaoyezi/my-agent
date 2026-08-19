# LLM: This module owns one durable active-input outbox and one reconciler per TUI session.
# It reuses immutable client/turn ids across response loss and never starts one thread per message.
# 模块用途: 持久保存 TUI 运行中补充消息，并用单个退避线程统一查询接收或排队结果。

from __future__ import annotations

import hashlib
import random
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from ...agent.gateway_parts.io import (
    locked_file_transition,
    read_json_file_report,
    write_json_file_atomic,
)
from ...agent.gateway_parts.request_client import GatewayAskExecutionOptions
from ..chat_client_context import ActiveTurnInputDelivery, ActiveTurnInputResult

_OUTBOX_SCHEMA = "tui_active_input_outbox.v2"
_LEGACY_OUTBOX_SCHEMA = "tui_active_input_outbox.v1"
_LEGACY_TUI_EXECUTION_OPTIONS = GatewayAskExecutionOptions(
    tool_approval=True,
    rich_transcript=True,
)


# LLM: One row keeps all information required to retry the same ingress or attach its canonical
# queued request after a TUI restart; next_attempt_at is scheduling state, never delivery authority.
# 类用途: 保存一条待确认补充消息的稳定身份、正文、目标回合和 Gateway 请求 ID。
@dataclass(frozen=True)
class TuiActiveInputOutboxEntry:
    message_id: str
    expected_turn_id: str
    text: str
    display_text: str
    execution_options: GatewayAskExecutionOptions = _LEGACY_TUI_EXECUTION_OPTIONS
    request_id: str = ""
    attempts: int = 0
    next_attempt_at: float = 0.0
    options_migrated_from_v1: bool = False

    # LLM: Serialization is complete and versioned so restart never reconstructs identity from UI
    # prose or queue order.
    # 函数用途: 把待确认消息转换成持久 JSON 对象。
    def to_dict(self) -> dict[str, object]:
        return {
            "message_id": self.message_id,
            "expected_turn_id": self.expected_turn_id,
            "text": self.text,
            "display_text": self.display_text,
            "execution_options": self.execution_options.to_payload(),
            "request_id": self.request_id,
            "attempts": self.attempts,
            "next_attempt_at": self.next_attempt_at,
            "options_migration": (
                "v1_tui_defaults" if self.options_migrated_from_v1 else ""
            ),
        }

    # LLM: Corrupt or partial rows are rejected instead of being retried with invented ids.
    # 函数用途: 从持久 JSON 校验并恢复一条待确认消息。
    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> TuiActiveInputOutboxEntry:
        raw_options = payload.get("execution_options")
        migrated = not isinstance(raw_options, dict)
        entry = cls(
            message_id=str(payload.get("message_id") or "").strip(),
            expected_turn_id=str(payload.get("expected_turn_id") or "").strip(),
            text=str(payload.get("text") or ""),
            display_text=str(payload.get("display_text") or ""),
            execution_options=(
                GatewayAskExecutionOptions.from_payload(raw_options)
                if isinstance(raw_options, dict)
                else _LEGACY_TUI_EXECUTION_OPTIONS
            ),
            request_id=str(payload.get("request_id") or "").strip(),
            attempts=max(0, int(payload.get("attempts") or 0)),
            next_attempt_at=max(0.0, float(payload.get("next_attempt_at") or 0.0)),
            options_migrated_from_v1=(
                migrated
                or str(payload.get("options_migration") or "") == "v1_tui_defaults"
            ),
        )
        if not entry.message_id or not entry.expected_turn_id or not entry.text:
            raise ValueError("TUI active input outbox entry is invalid")
        return entry


# LLM: The reconciler is the only owner of retry timing for one session. Callbacks update the
# canonical TuiRuntime/job queue, while this class owns durable transport uncertainty only.
# 类用途: 用一个后台线程处理本会话全部待确认补充消息，并在终态后删除对应 outbox 行。
class TuiActiveInputReconciler:
    # LLM: Construction restores durable rows and starts the sole daemon only when work exists;
    # callback failures leave the row for the next retry or process restart.
    # 函数用途: 创建会话级对账器，并在确有待确认消息时恢复显示和启动后台线程。
    def __init__(
        self,
        *,
        path: Path,
        submit: Callable[[TuiActiveInputOutboxEntry], ActiveTurnInputResult],
        status: Callable[[str], ActiveTurnInputResult],
        on_restore: Callable[[TuiActiveInputOutboxEntry], None],
        on_accepted: Callable[[TuiActiveInputOutboxEntry], None],
        on_queued: Callable[[TuiActiveInputOutboxEntry, str], None],
        on_rejected: Callable[[TuiActiveInputOutboxEntry], None],
        on_conflict: Callable[[TuiActiveInputOutboxEntry], None],
        on_error: Callable[[BaseException], None],
        stop_event: threading.Event,
        initial_delay: float,
        maximum_delay: float,
    ) -> None:
        self.path = Path(path)
        self.submit = submit
        self.status = status
        self.on_accepted = on_accepted
        self.on_queued = on_queued
        self.on_rejected = on_rejected
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

    # LLM: Persist-before-send covers an HTTP response-loss crash. Reusing a message id with another
    # payload fails before the worker invokes Gateway.
    # 函数用途: 新增一条待确认消息并立刻唤醒统一对账线程。
    def enqueue(self, entry: TuiActiveInputOutboxEntry) -> None:
        with self._lock, locked_file_transition(self._transition_path()):
            entries = self._read_entries_locked()
            existing = entries.get(entry.message_id)
            if existing is not None and (
                existing.expected_turn_id != entry.expected_turn_id
                or existing.text != entry.text
                or existing.display_text != entry.display_text
                or existing.execution_options != entry.execution_options
            ):
                raise ValueError("TUI active input id was reused with different content")
            entries[entry.message_id] = existing or entry
            self._write_entries_locked(entries)
        self._ensure_worker_started()
        self._wake.set()

    # LLM: Thread creation is lazy and guarded so empty TUI/test sessions allocate no daemon and
    # concurrent first enqueues still create exactly one reconciler worker.
    # 函数用途: 在首次出现待确认消息时安全启动本会话唯一后台线程。
    def _ensure_worker_started(self) -> None:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            if self.stop_event.is_set():
                return
            self._worker = threading.Thread(
                target=self._run,
                name=f"tui-input-reconciler-{self.path.stem[-8:]}",
                daemon=True,
            )
            self._worker.start()

    # LLM: The worker processes a bounded snapshot, persists backoff/request identity after every
    # unknown result, and removes a row only after its UI/job callback succeeds.
    # 函数用途: 循环处理全部到期消息，空闲时等待唤醒或最短退避时间。
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
                entries = self._read_entries()
                next_due = min(
                    (entry.next_attempt_at for entry in entries.values()),
                    default=now + self.maximum_delay,
                )
                timeout = max(0.05, min(self.maximum_delay, next_due - time.time()))
            except Exception as exc:
                self.on_error(exc)
                timeout = min(self.maximum_delay, max(self.initial_delay, 0.25))
            self._wake.wait(timeout)
            self._wake.clear()

    # LLM: Once Gateway returns a stable ingress id, retries switch to read-only /input-status;
    # only a response-loss row without that id repeats POST with the same immutable message id.
    # 函数用途: 对账一条消息，并按 accepted、queued、rejected 或 unknown 更新持久状态。
    def _reconcile_one(self, entry: TuiActiveInputOutboxEntry) -> None:
        try:
            result = self.status(entry.request_id) if entry.request_id else self.submit(entry)
        except Exception:
            result = ActiveTurnInputResult(ActiveTurnInputDelivery.UNKNOWN)
        if result.delivery is ActiveTurnInputDelivery.ACCEPTED:
            if self._finish(entry.message_id, lambda: self.on_accepted(entry)):
                return
        elif result.delivery is ActiveTurnInputDelivery.QUEUED and result.request_id:
            if self._finish(
                entry.message_id,
                lambda: self.on_queued(entry, result.request_id),
            ):
                return
        elif result.delivery is ActiveTurnInputDelivery.REJECTED:
            if self._finish(entry.message_id, lambda: self.on_rejected(entry)):
                return
        elif result.delivery is ActiveTurnInputDelivery.CONFLICT:
            if self._finish(entry.message_id, lambda: self.on_conflict(entry)):
                return
        request_id = str(result.request_id or entry.request_id).strip()
        attempts = entry.attempts + 1
        base = min(self.maximum_delay, self.initial_delay * (2 ** min(attempts, 12)))
        delay = min(self.maximum_delay, base * random.uniform(0.8, 1.2))
        self._replace(
            replace(
                entry,
                request_id=request_id,
                attempts=attempts,
                next_attempt_at=time.time() + delay,
            )
        )

    # LLM: Callback completion and outbox removal are ordered so a crash may repeat a local attach
    # after restart but can never repeat the Gateway request; the callback must therefore be idempotent.
    # 函数用途: 执行终态 UI 动作，成功后删除本条持久待确认记录。
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

    # LLM: Unknown-state updates compare only the stable message id and replace one row atomically.
    # 函数用途: 保存一条消息新的 Gateway ID 和下次退避时间。
    def _replace(self, entry: TuiActiveInputOutboxEntry) -> None:
        with self._lock, locked_file_transition(self._transition_path()):
            entries = self._read_entries_locked()
            if entry.message_id not in entries:
                return
            entries[entry.message_id] = entry
            self._write_entries_locked(entries)

    # LLM: Public reads use the same transition lock as writers so a renderer/test never observes
    # the temporary atomic-replace edge as an empty outbox.
    # 函数用途: 读取当前会话全部有效待确认消息。
    def _read_entries(self) -> dict[str, TuiActiveInputOutboxEntry]:
        with self._lock, locked_file_transition(self._transition_path()):
            return self._read_entries_locked()

    # LLM: Caller holds the outbox transition lock; invalid schemas/rows fail closed and remain on
    # disk for diagnosis rather than being silently overwritten.
    # 函数用途: 在锁内读取并校验 outbox JSON。
    def _read_entries_locked(self) -> dict[str, TuiActiveInputOutboxEntry]:
        report = read_json_file_report(self.path, context="tui.active_input_outbox.read")
        if report.load_error is not None:
            raise RuntimeError("TUI active input outbox is unreadable")
        if not report.payload:
            if self.path.exists():
                raise RuntimeError("TUI active input outbox is empty or invalid")
            return {}
        schema = report.payload.get("schema_version")
        if schema not in {_OUTBOX_SCHEMA, _LEGACY_OUTBOX_SCHEMA}:
            raise RuntimeError("TUI active input outbox schema is invalid")
        rows = report.payload.get("entries")
        if not isinstance(rows, dict):
            raise RuntimeError("TUI active input outbox entries are invalid")
        entries: dict[str, TuiActiveInputOutboxEntry] = {}
        for message_id, payload in rows.items():
            if not isinstance(payload, dict):
                raise RuntimeError("TUI active input outbox row is invalid")
            entry = TuiActiveInputOutboxEntry.from_dict(payload)
            if entry.message_id != str(message_id):
                raise RuntimeError("TUI active input outbox key conflicts")
            entries[entry.message_id] = entry
        if schema == _LEGACY_OUTBOX_SCHEMA:
            self._write_entries_locked(entries)
        return entries

    # LLM: Caller holds the transition lock; one atomic file is the authority for this session's
    # small pending set.
    # 函数用途: 在锁内原子保存全部待确认消息。
    def _write_entries_locked(self, entries: dict[str, TuiActiveInputOutboxEntry]) -> None:
        write_json_file_atomic(
            self.path,
            {
                "schema_version": _OUTBOX_SCHEMA,
                "entries": {key: value.to_dict() for key, value in sorted(entries.items())},
                "updated_at": time.time(),
            },
        )

    # LLM: The lock filename is deterministic and colocated with its outbox; no global TUI lock
    # serializes independent sessions.
    # 函数用途: 返回当前会话 outbox 的跨进程转换锁路径。
    def _transition_path(self) -> Path:
        return self.path.with_name(f".{self.path.name}.transition")


# LLM: Session ids are hashed before becoming filenames so channel/user text never escapes into a
# path, while the same session always resumes the same durable outbox.
# 函数用途: 根据 Gateway 根目录和会话 ID 返回 TUI 待确认消息文件。
def tui_active_input_outbox_path(gateway_root: Path, session_id: str) -> Path:
    digest = hashlib.sha256(str(session_id or "default").encode("utf-8")).hexdigest()
    return Path(gateway_root) / "client_outbox" / f"tui-active-{digest}.json"


__all__ = [
    "TuiActiveInputOutboxEntry",
    "TuiActiveInputReconciler",
    "tui_active_input_outbox_path",
]
