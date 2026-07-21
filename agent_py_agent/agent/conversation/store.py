
"""Conversation store for threads, messages, tasks, observations, guidance,
wake signals, progress policies, and background claims.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..gateway_parts.daemon_metadata import build_process_identity, process_identity_is_live
from ..gateway_parts.io import (
    locked_file_transition,
    read_json_file,
    update_json_file_atomic,
    write_json_file_atomic,
)
from ..io.jsonl import append_jsonl
from ..runtime_errors import DataCorruptionError, runtime_error_report
from ..settings.defaults import default_config_value

_STORE_LOGGER = logging.getLogger("agent.conversation.store")
from .models import (
    THREAD_GOAL_OBJECTIVE_MAX_CHARS,
    THREAD_GOAL_STATUSES,
    THREAD_TASK_LINK_INACTIVE_STATUSES,
    THREAD_TASK_LINK_NON_RESURRECTABLE_STATUSES,
    ChannelBinding,
    ConversationThread,
    GuidanceEntry,
    MessageLogEntry,
    ObservationEvent,
    ProgressPolicy,
    ThreadGoal,
    ThreadTaskLink,
    WakeSignal,
    new_id,
    normalize_guidance_target_type,
)

# ---------------------------------------------------------------------------
# common helpers
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class JsonlReadReport:
    rows: list[dict[str, Any]]
    load_errors: list[dict[str, Any]]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return read_jsonl_report(path, context="conversation.jsonl").rows


def read_jsonl_report(path: Path, *, context: str) -> JsonlReadReport:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return JsonlReadReport([], [_jsonl_error(exc, context, path=path)])
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        row, error = _json_row(line, context=context, path=path, line_number=line_number)
        if error is not None:
            errors.append(error)
            continue
        if row is not None:
            rows.append(row)
    return JsonlReadReport(rows, errors)


def _read_tail_bytes(path: Path, limit: int) -> tuple[bytes, int]:
    """按 64KB 块从文件尾部倒读，直到覆盖 limit+1 个换行或到达文件头。"""
    with path.open("rb") as handle:
        handle.seek(0, 2)
        pos = handle.tell()
        block = 64 * 1024
        data = b""
        while pos > 0 and data.count(b"\n") <= limit:
            step = min(block, pos)
            pos -= step
            handle.seek(pos)
            data = handle.read(step) + data
    return data, pos


def read_jsonl_tail_report(path: Path, *, context: str, limit: int) -> JsonlReadReport:
    """从文件尾部按块倒读最后 limit 条记录，避免大账本全量加载。

    跨块边界可能截断的首行会被丢弃（它属于更早的记录）；limit<=0 退回全量读取。"""
    if limit <= 0:
        return read_jsonl_report(path, context=context)
    try:
        data, pos = _read_tail_bytes(path, limit)
    except OSError as exc:
        return JsonlReadReport([], [_jsonl_error(exc, context, path=path)])
    lines = data.decode("utf-8", errors="replace").splitlines()
    if pos > 0 and lines:
        lines = lines[1:]
    selected = [line for line in lines if line.strip()][-limit:]
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for line in selected:
        row, error = _json_row(line, context=context, path=path, line_number=0)
        if error is not None:
            errors.append(error)
            continue
        if row is not None:
            rows.append(row)
    return JsonlReadReport(rows, errors)


def now(value: float | None = None) -> float:
    return float(time.time() if value is None else value)


def float_value(value: object) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def safe_file_stem(value: str) -> str:
    text = str(value or "").strip()
    return "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in text) or "unknown"


def wake_urgency(value: str) -> str:
    return "urgent" if str(value or "").strip().lower() == "urgent" else "normal"


def with_handled_at(event: ObservationEvent, handled: dict[str, float]) -> ObservationEvent:
    handled_at = float(handled.get(event.observation_id) or event.handled_at or 0.0)
    return event if handled_at == event.handled_at else replace(event, handled_at=handled_at)


def wake_evidence_refs(observation: ObservationEvent | None, explicit_refs: object) -> tuple[str, ...]:
    if explicit_refs is not None:
        refs = explicit_refs
    elif observation is not None:
        refs = observation.evidence_refs
    else:
        refs = ()
    return tuple(str(item) for item in refs if str(item or "").strip()) if isinstance(refs, (list, tuple)) else ()


def _json_row(
    line: str,
    *,
    context: str,
    path: Path,
    line_number: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        row = json.loads(line)
    except json.JSONDecodeError as exc:
        return None, _jsonl_error(exc, context, path=path, line_number=line_number)
    if not isinstance(row, dict):
        return None, _jsonl_error(
            ValueError(f"JSONL row is {type(row).__name__}, expected object"),
            context,
            path=path,
            line_number=line_number,
        )
    return row, None


def _jsonl_error(
    exc: BaseException,
    context: str,
    *,
    path: Path,
    line_number: int | None = None,
) -> dict[str, Any]:
    report = runtime_error_report(exc, context=context)
    report["path"] = str(path)
    if line_number is not None:
        report["line_number"] = line_number
    return report


# ---------------------------------------------------------------------------
# shared layout
# ---------------------------------------------------------------------------

class ConversationBaseStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.threads_dir = self.root / "threads"
        self.messages_dir = self.root / "messages"
        self.tasks_dir = self.root / "tasks"
        self.policies_dir = self.root / "progress_policies"
        self.observations_dir = self.root / "observations"
        self.guidance_dir = self.root / "guidance"
        self.goals_dir = self.root / "goals"
        self.wake_queue_dir = self.root / "wake_queue"
        self.wake_handled_dir = self.wake_queue_dir / "handled"
        self.observation_handled_path = self.root / "observation_handled.json"
        self.guidance_delivered_path = self.root / "guidance_delivered.json"
        self.bindings_path = self.root / "channel_bindings.json"
        self.user_latest_path = self.root / "user_latest_threads.json"
        self.background_claims_dir = self.root / "background_claims"
        self.wake_dedupe_dir = self.wake_queue_dir / "dedupe"
        # 会话运行时 keeps goal wall-clock accounting in the live thread runtime, not
        # in the persisted goal timestamp. A process restart therefore starts a
        # fresh baseline instead of charging service downtime to the user.
        self._goal_clock_lock = threading.Lock()
        self._goal_clock: dict[str, tuple[str, float]] = {}
        self._ensure_dirs()

    def _ensure_dirs(self) -> None:
        for path in self._managed_dirs():
            path.mkdir(parents=True, exist_ok=True)

    def _managed_dirs(self) -> tuple[Path, ...]:
        return (
            self.threads_dir,
            self.messages_dir,
            self.tasks_dir,
            self.policies_dir,
            self.observations_dir,
            self.guidance_dir,
            self.goals_dir,
            self.background_claims_dir,
            self.wake_dedupe_dir,
            self.wake_queue_dir / "urgent",
            self.wake_queue_dir / "normal",
            self.wake_handled_dir,
        )

    def _thread_path(self, thread_id: str) -> Path:
        return self.threads_dir / f"{thread_id}.json"

    def _message_path(self, thread_id: str) -> Path:
        return self.messages_dir / f"{thread_id}.jsonl"

    def _task_path(self, task_id: str) -> Path:
        return self.tasks_dir / f"{task_id}.json"

    def _policy_path(self, policy_id: str) -> Path:
        return self.policies_dir / f"{policy_id}.json"

    def _background_claim_path(self, thread_id: str) -> Path:
        return self.background_claims_dir / f"{safe_file_stem(thread_id)}.json"

    def _observation_path(self, thread_id: str) -> Path:
        return self.observations_dir / f"{thread_id}.jsonl"

    def _guidance_path(self, target_type: str, target_id: str) -> Path:
        return self.guidance_dir / f"{safe_file_stem(target_type)}.{safe_file_stem(target_id)}.jsonl"

    # LLM: One sanitized path per thread is the sole durable goal record authority.
    # 函数用途: 返回当前 conversation thread 唯一的持续目标文件路径。
    def _goal_path(self, thread_id: str) -> Path:
        return self.goals_dir / f"{safe_file_stem(thread_id)}.json"

    def _wake_signal_path(self, signal: WakeSignal) -> Path:
        return self.wake_queue_dir / wake_urgency(signal.urgency) / f"{signal.wake_signal_id}.json"

    def _wake_dedupe_path(self, thread_id: str, dedupe_key: str) -> Path:
        return self.wake_dedupe_dir / f"{safe_file_stem(thread_id)}.{safe_file_stem(dedupe_key)}.json"


# ---------------------------------------------------------------------------
# thread records
# ---------------------------------------------------------------------------

def _binding_key(channel: str, conversation_id: str, user_id: str) -> str:
    return "\x1f".join([str(channel), str(conversation_id), str(user_id)])


def _channel_binding(thread_id: str, current: float, kwargs: dict) -> ChannelBinding:
    return ChannelBinding(
        channel=kwargs.get("channel", ""),
        channel_conversation_id=kwargs.get("channel_conversation_id", ""),
        channel_user_id=kwargs.get("channel_user_id", ""),
        canonical_user_id=kwargs.get("canonical_user_id", ""),
        thread_id=thread_id,
        last_active_at=current,
    )


def _replace_binding(thread: ConversationThread, binding: ChannelBinding) -> tuple[ChannelBinding, ...]:
    key = _binding_key(binding.channel, binding.channel_conversation_id, binding.channel_user_id)
    old = (
        item
        for item in thread.channel_bindings
        if _binding_key(item.channel, item.channel_conversation_id, item.channel_user_id) != key
    )
    return (*old, binding)


def _read_json_object_report(path: Path, *, context: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if not path.exists():
        return {}, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise DataCorruptionError(f"{path.name} is {type(payload).__name__}, expected object")
        return payload, None
    except Exception as exc:
        report = runtime_error_report(exc, context=context)
        report["path"] = str(path)
        return {}, report


class ConversationThreadStore(ConversationBaseStore):
    def get_or_create_thread(self, request: dict) -> ConversationThread:
        existing, binding_error = self.resolve_thread_report(
            channel=request.get("channel", ""),
            channel_conversation_id=request.get("channel_conversation_id", ""),
            channel_user_id=request.get("channel_user_id", ""),
        )
        if binding_error is not None:
            raise DataCorruptionError(str(binding_error))
        if existing is not None:
            return self._bind_existing(existing.thread_id, request)
        latest = None
        if request.get("reuse_latest_for_user"):
            latest, latest_error = self.latest_thread_for_user_report(request.get("canonical_user_id", ""))
            if latest_error is not None:
                raise DataCorruptionError(str(latest_error))
        if latest is not None:
            return self._bind_existing(latest.thread_id, request)
        return self._create_thread(request)

    def resolve_thread(
        self, *, channel: str, channel_conversation_id: str, channel_user_id: str
    ) -> ConversationThread | None:
        thread, _load_error = self.resolve_thread_report(
            channel=channel,
            channel_conversation_id=channel_conversation_id,
            channel_user_id=channel_user_id,
        )
        return thread

    def resolve_thread_report(
        self,
        *,
        channel: str,
        channel_conversation_id: str,
        channel_user_id: str,
    ) -> tuple[ConversationThread | None, dict[str, Any] | None]:
        bindings, error = self._read_bindings_report()
        if error is not None:
            return None, error
        thread_id = str(bindings.get(_binding_key(channel, channel_conversation_id, channel_user_id)) or "")
        return self.load_thread_report(thread_id) if thread_id else (None, None)

    def latest_thread_for_user(self, canonical_user_id: str) -> ConversationThread | None:
        thread, _load_error = self.latest_thread_for_user_report(canonical_user_id)
        return thread

    def latest_thread_for_user_report(
        self, canonical_user_id: str
    ) -> tuple[ConversationThread | None, dict[str, Any] | None]:
        payload, error = _read_json_object_report(
            self.user_latest_path,
            context="conversation.user_latest.read",
        )
        if error is not None:
            return None, error
        thread_id = str(payload.get(canonical_user_id) or "")
        return self.load_thread_report(thread_id) if thread_id else (None, None)

    def bind_channel(self, request: dict) -> ConversationThread:
        thread_id = str(request.get("thread_id") or "")
        thread = self._require_thread(thread_id)
        current = now(request.get("now"))
        binding = _channel_binding(thread.thread_id, current, request)
        updated = replace(
            thread,
            canonical_user_id=request.get("canonical_user_id") or thread.canonical_user_id,
            owner_id=str(request.get("owner_id") or thread.owner_id or ""),
            owner_home=str(request.get("owner_home") or thread.owner_home or ""),
            channel_bindings=_replace_binding(thread, binding),
            updated_at=current,
        )
        self._write_thread(updated)
        self._write_binding_indexes(binding)
        return updated

    def list_threads(self, *, limit: int = 100) -> list[ConversationThread]:
        threads, _load_errors = self.list_threads_report(limit=limit)
        return threads

    def list_threads_report(self, *, limit: int = 100) -> tuple[list[ConversationThread], list[dict[str, Any]]]:
        threads: list[ConversationThread] = []
        load_errors: list[dict[str, Any]] = []
        for path in sorted(self.threads_dir.glob("*.json")):
            thread, error = self._load_thread_path_report(path)
            if thread is not None:
                threads.append(thread)
            if error is not None:
                load_errors.append(error)
        threads.sort(key=lambda item: item.updated_at)
        limited = threads if limit <= 0 else threads[-limit:]
        return limited, load_errors

    def update_summary(self, thread_id: str, summary: str, *, now: float | None = None) -> ConversationThread:
        thread = self._require_thread(thread_id)
        current = now if now is not None else time.time()
        updated = replace(thread, summary=summary, updated_at=current)
        self._write_thread(updated)
        return updated

    def update_compact_state(
        self,
        thread_id: str,
        *,
        summary: str,
        compacted_through_message_id: str,
        compacted_through_byte_offset: int,
        source_messages: int,
        expected_generation: int,
        now: float | None = None,
    ) -> ConversationThread:
        """Atomically advance one thread's summary cursor without touching raw messages."""
        thread = self._require_thread(thread_id)
        if thread.compact_generation != expected_generation:
            raise RuntimeError(
                "conversation compact generation changed while summary was being prepared"
            )
        current = now if now is not None else time.time()
        updated = replace(
            thread,
            summary=str(summary).strip(),
            compacted_through_message_id=str(compacted_through_message_id),
            compacted_through_byte_offset=max(0, int(compacted_through_byte_offset)),
            compact_generation=thread.compact_generation + 1,
            compact_updated_at=current,
            compact_source_messages=max(0, int(source_messages)),
            updated_at=current,
        )
        self._write_thread(updated)
        return updated

    def update_verbose_level(
        self,
        thread_id: str,
        level: str,
        *,
        now: float | None = None,
    ) -> ConversationThread:
        normalized = str(level or "off").strip().lower()
        if normalized not in {"off", "on", "full"}:
            raise ValueError("verbose level must be one of: off, on, full")
        thread = self._require_thread(thread_id)
        current = now if now is not None else time.time()
        updated = replace(thread, verbose_level=normalized, updated_at=current)
        self._write_thread(updated)
        return updated

    def load_thread(self, thread_id: str) -> ConversationThread | None:
        thread, _load_error = self.load_thread_report(thread_id)
        return thread

    def load_thread_report(self, thread_id: str) -> tuple[ConversationThread | None, dict[str, Any] | None]:
        if not thread_id:
            return None, None
        return self._load_thread_path_report(self._thread_path(thread_id), thread_id=thread_id)

    def _require_thread(self, thread_id: str) -> ConversationThread:
        thread = self.load_thread(thread_id)
        if thread is None:
            raise KeyError(f"unknown conversation thread: {thread_id}")
        return thread

    def _write_thread(self, thread: ConversationThread) -> None:
        write_json_file_atomic(self._thread_path(thread.thread_id), thread.to_dict())

    def _read_bindings(self) -> dict[str, str]:
        bindings, _load_error = self._read_bindings_report()
        return bindings

    def _read_bindings_report(self) -> tuple[dict[str, str], dict[str, Any] | None]:
        payload, error = _read_json_object_report(
            self.bindings_path,
            context="conversation.bindings.read",
        )
        if error is not None:
            return {}, error
        return {str(key): str(value) for key, value in payload.items()}, None

    def _bind_existing(self, thread_id: str, kwargs: dict) -> ConversationThread:
        return self.bind_channel({
            "thread_id": thread_id,
            "canonical_user_id": kwargs.get("canonical_user_id", ""),
            "owner_id": kwargs.get("owner_id", ""),
            "owner_home": kwargs.get("owner_home", ""),
            "channel": kwargs.get("channel", ""),
            "channel_conversation_id": kwargs.get("channel_conversation_id", ""),
            "channel_user_id": kwargs.get("channel_user_id", ""),
            "now": kwargs.get("now"),
        })

    def _create_thread(self, kwargs: dict) -> ConversationThread:
        current = now(kwargs.get("now"))
        thread = ConversationThread(
            thread_id=new_id("thread"),
            canonical_user_id=kwargs.get("canonical_user_id", ""),
            owner_id=str(kwargs.get("owner_id") or ""),
            owner_home=str(kwargs.get("owner_home") or ""),
            title=kwargs.get("title", ""),
            created_at=current,
            updated_at=current,
        )
        self._write_thread(thread)
        return self._bind_existing(thread.thread_id, {**kwargs, "now": current})

    def _write_binding_indexes(self, binding: ChannelBinding) -> None:
        key = _binding_key(binding.channel, binding.channel_conversation_id, binding.channel_user_id)
        update_json_file_atomic(self.bindings_path, lambda data: {**data, key: binding.thread_id})
        update_json_file_atomic(
            self.user_latest_path, lambda data: {**data, binding.canonical_user_id: binding.thread_id}
        )

    def _load_thread_path_report(
        self,
        path: Path,
        *,
        thread_id: str = "",
    ) -> tuple[ConversationThread | None, dict[str, Any] | None]:
        payload, error = _read_json_object_report(path, context="conversation.thread.read")
        actual_thread_id = str(thread_id or path.stem)
        if error is not None:
            error["thread_id"] = actual_thread_id
            return None, error
        if not payload:
            return None, None
        try:
            return ConversationThread.from_dict(payload), None
        except Exception as exc:
            report = runtime_error_report(exc, context="conversation.thread.read")
            report["thread_id"] = actual_thread_id
            report["path"] = str(path)
            return None, report


# ---------------------------------------------------------------------------
# message records
# ---------------------------------------------------------------------------

def _message_entries(rows: list[dict[str, Any]]) -> tuple[list[MessageLogEntry], list[dict[str, Any]]]:
    entries: list[MessageLogEntry] = []
    errors: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        try:
            entries.append(MessageLogEntry.from_dict(row))
        except Exception as exc:
            report = runtime_error_report(exc, context="conversation.messages.parse")
            report["row_index"] = index
            errors.append(report)
    return entries, errors


class ConversationMessageStore(ConversationThreadStore):
    def append_message(self, request: dict) -> MessageLogEntry:
        thread_id = str(request.get("thread_id") or "")
        thread = self._require_thread(thread_id)
        entry = MessageLogEntry(
            message_id=new_id("msg"),
            thread_id=thread.thread_id,
            role=str(request.get("role") or ""),
            content=str(request.get("content") or ""),
            channel=str(request.get("channel") or "internal"),
            channel_message_id=str(request.get("channel_message_id") or ""),
            created_at=now(request.get("now")),
            metadata=request.get("metadata") or {},
        )
        append_jsonl(self._message_path(thread_id), entry.to_dict(), sort_keys=True)
        self._write_thread(replace(thread, updated_at=max(thread.updated_at, entry.created_at)))
        return entry

    def append_message_once(self, request: dict, *, dedupe_key: str) -> MessageLogEntry:
        """Append one transcript event exactly once across retries and process restarts."""
        thread_id = str(request.get("thread_id") or "").strip()
        key = str(dedupe_key or "").strip()
        if not thread_id or not key:
            raise ValueError("thread_id and dedupe_key are required")
        path = self._message_path(thread_id)
        transition = path.with_name(f".{path.name}.append-once")
        with locked_file_transition(transition):
            rows, load_errors = self.recent_messages_report(thread_id, limit=0)
            if load_errors:
                raise DataCorruptionError(f"conversation transcript is unreadable: {thread_id}")
            existing = next(
                (
                    item
                    for item in rows
                    if str(item.metadata.get("dedupe_key") or "").strip() == key
                ),
                None,
            )
            if existing is not None:
                if (
                    existing.role != str(request.get("role") or "")
                    or existing.content != str(request.get("content") or "")
                ):
                    raise DataCorruptionError(
                        f"conversation message dedupe key reused with different input: {key}"
                    )
                return existing
            metadata = request.get("metadata")
            request = {
                **request,
                "metadata": {
                    **(metadata if isinstance(metadata, dict) else {}),
                    "dedupe_key": key,
                },
            }
            return self.append_message(request)

    def recent_messages(self, thread_id: str, *, limit: int = 20) -> list[MessageLogEntry]:
        entries, _errors = self.recent_messages_report(thread_id, limit=limit)
        return entries

    def recent_messages_report(
        self,
        thread_id: str,
        *,
        limit: int = 20,
    ) -> tuple[list[MessageLogEntry], list[dict[str, Any]]]:
        # 尾部倒读：limit>0 时只解析最后一段，长会话不再全量加载。
        # 多读一倍冗余行，留给 _message_entries 过滤非消息行后仍能凑满 limit。
        message_path = self._message_path(thread_id)
        if not message_path.exists():
            return [], []
        report = read_jsonl_tail_report(
            message_path,
            context="conversation.messages.read",
            limit=0 if limit <= 0 else max(limit * 2, limit + 8),
        )
        entries, parse_errors = _message_entries(report.rows)
        selected = entries if limit <= 0 else entries[-limit:]
        return selected, [*report.load_errors, *parse_errors]

    def messages_after_compact_report(
        self,
        thread: ConversationThread,
    ) -> tuple[list[MessageLogEntry], list[dict[str, Any]]]:
        """Read only the append-only tail after a validated compact byte cursor."""
        offset = max(0, int(thread.compacted_through_byte_offset or 0))
        if offset <= 0:
            return self.recent_messages_report(thread.thread_id, limit=0)
        path = self._message_path(thread.thread_id)
        try:
            size = path.stat().st_size
            if offset > size:
                raise DataCorruptionError(
                    f"conversation compact byte cursor {offset} exceeds transcript size {size}"
                )
            with path.open("rb") as handle:
                handle.seek(offset)
                data = handle.read()
        except Exception as exc:
            return [], [_jsonl_error(exc, "conversation.messages.after_compact", path=path)]
        rows: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for line in data.decode("utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            row, error = _json_row(
                line,
                context="conversation.messages.after_compact",
                path=path,
                line_number=0,
            )
            if error is not None:
                errors.append(error)
            elif row is not None:
                rows.append(row)
        entries, parse_errors = _message_entries(rows)
        return entries, [*errors, *parse_errors]

    def message_byte_offset_after(self, thread_id: str, message_id: str) -> int:
        """Return the byte position immediately after a message in the raw ledger."""
        path = self._message_path(thread_id)
        try:
            with path.open("rb") as handle:
                while line := handle.readline():
                    try:
                        row = json.loads(line.decode("utf-8"))
                    except (json.JSONDecodeError, UnicodeError) as exc:
                        raise DataCorruptionError(
                            f"conversation transcript contains an unreadable row: {path}"
                        ) from exc
                    if isinstance(row, dict) and str(row.get("message_id") or "") == message_id:
                        return handle.tell()
        except OSError as exc:
            raise DataCorruptionError(f"cannot read conversation transcript: {path}") from exc
        raise DataCorruptionError(
            f"conversation compact message cursor is missing from transcript: {message_id}"
        )


# ---------------------------------------------------------------------------
# task links
# ---------------------------------------------------------------------------

def _thread_with_task(thread: ConversationThread, task_id: str, updated_at: float) -> ConversationThread:
    task_ids = tuple(dict.fromkeys((*thread.task_ids, task_id)))
    active_task_ids = tuple(dict.fromkeys((*thread.active_task_ids, task_id)))
    return replace(
        thread,
        task_ids=task_ids,
        active_task_ids=active_task_ids,
        updated_at=updated_at,
    )


def _thread_without_task(thread: ConversationThread, task_id: str, updated_at: float) -> ConversationThread:
    task_ids = tuple(dict.fromkeys((*thread.task_ids, task_id)))
    active_task_ids = tuple(item for item in thread.active_task_ids if item != task_id)
    return replace(
        thread,
        task_ids=task_ids,
        active_task_ids=active_task_ids,
        updated_at=updated_at,
    )


def _read_task_link(
    path,
    task_id: str,
    *,
    context: str = "conversation.task_link.read",
) -> tuple[ThreadTaskLink | None, dict[str, Any] | None]:
    try:
        if not path.exists():
            raise FileNotFoundError(str(path))
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"conversation task link is {type(payload).__name__}, expected object")
        return ThreadTaskLink.from_dict(payload), None
    except Exception as exc:
        report = runtime_error_report(exc, context=context)
        report["task_id"] = task_id
        report["path"] = str(path)
        return None, report


_TASK_WORKSPACE_STATUS_BY_LINK = {
    "abandoned": "ABANDONED",
    "active": "RUNNING",
    "blocked": "BLOCKED",
    "cancelled": "CANCELLED",
    "channel_error": "CHANNEL_ERROR",
    "completed": "DONE",
    "done": "DONE",
    "failed": "FAILED",
    "interrupted": "PAUSED",
    "superseded": "ABANDONED",
    "taken_over": "TAKEN_OVER",
    "timeout": "TIMEOUT",
}


# LLM: conversation task link 是生命周期权威；work/state.json 只是同 task_path 的 owner-local 投影。
# 函数用途: 在任务链接变更后同步工作区状态，避免停止或完成后目录仍永久显示 RUNNING。
def _sync_task_workspace_status(
    link: ThreadTaskLink,
    current: float,
    *,
    owner_home: str,
) -> None:
    task_path = str(link.task_path or "").strip()
    owner_path = str(owner_home or "").strip()
    if not task_path or not owner_path:
        return
    try:
        task_root = Path(task_path).expanduser().resolve(strict=False)
        owner_tasks = Path(owner_path).expanduser().resolve(strict=False) / "tasks"
        task_root.relative_to(owner_tasks)
        unresolved_state_path = task_root / "work" / "state.json"
        if unresolved_state_path.is_symlink() or unresolved_state_path.parent.is_symlink():
            raise ValueError("task workspace state path cannot use symbolic links")
        state_path = unresolved_state_path.resolve(strict=False)
        state_path.relative_to(task_root)
    except ValueError as exc:
        _STORE_LOGGER.warning(
            "task workspace state path rejected(task=%s): %s",
            link.task_id,
            exc,
        )
        return
    except (OSError, RuntimeError) as exc:
        _STORE_LOGGER.warning("task workspace state path unavailable(task=%s): %s", link.task_id, exc)
        return
    if not state_path.is_file():
        return
    projected_status = _TASK_WORKSPACE_STATUS_BY_LINK.get(
        str(link.status or "").strip().lower(),
        str(link.status or "UNKNOWN").strip().upper(),
    )

    def updater(data: dict[str, Any]) -> dict[str, Any]:
        if not data:
            raise DataCorruptionError(f"task workspace state is unreadable: {link.task_id}")
        state_task_id = str(data.get("task_id") or "").strip()
        if state_task_id and state_task_id != link.task_id:
            raise DataCorruptionError(
                f"task workspace state identity does not match task link: {link.task_id}"
            )
        updated = dict(data)
        updated["task_id"] = link.task_id
        updated["status"] = projected_status
        updated["updated_at"] = datetime.fromtimestamp(current, timezone.utc).isoformat()
        return updated

    try:
        update_json_file_atomic(state_path, updater, require_existing=True)
    except (DataCorruptionError, OSError, TypeError, ValueError) as exc:
        _STORE_LOGGER.warning(
            "task workspace state sync failed(task=%s,status=%s): %s",
            link.task_id,
            projected_status,
            exc,
        )


def _merged_task_link(
    data: dict[str, Any],
    *,
    request: dict,
    thread_id: str,
    task_id: str,
    current: float,
    path_exists: bool,
) -> ThreadTaskLink:
    if not data:
        if path_exists:
            raise DataCorruptionError(f"conversation task link is unreadable: {task_id}")
        return ThreadTaskLink(
            thread_id=thread_id,
            task_id=task_id,
            goal=str(request.get("goal") or ""),
            status=str(request.get("status") or "active"),
            created_at=current,
            task_path=str(request.get("task_path") or ""),
        )
    existing = ThreadTaskLink.from_dict(data)
    if not existing.thread_id or existing.task_id != task_id:
        raise DataCorruptionError(f"conversation task link identity is invalid: {task_id}")
    if existing.thread_id != thread_id:
        raise ValueError(f"task {task_id} is already bound to another conversation thread")
    requested_status = str(request.get("status") or "").strip()
    # bind_task is an idempotent identity/path upsert, not a lifecycle reopen API.
    # A gateway retry, process restart, late runner callback, or repeated workspace
    # materialization may bind the same task again with its old "active" snapshot.
    # Once /stop or another terminal transition has landed, that stale bind must
    # never resurrect the task.  Deliberate reopen goes through update_task_status
    # (select_current_conversation_task), where the caller names the exact task.
    existing_status = str(existing.status or "").strip()
    merged_status = (
        existing_status
        if existing_status.lower() in THREAD_TASK_LINK_NON_RESURRECTABLE_STATUSES
        else requested_status or existing_status
    )
    return replace(
        existing,
        goal=existing.goal or str(request.get("goal") or ""),
        status=merged_status,
        created_at=existing.created_at or current,
        task_path=existing.task_path or str(request.get("task_path") or ""),
    )


class ConversationTaskStore(ConversationMessageStore):
    def task_transition_guard(self, task_id: str):
        """Return the cross-process lock shared by steer, stop, and completion."""
        normalized = safe_file_stem(str(task_id or ""))
        if not normalized:
            raise ValueError("task_id is required")
        return locked_file_transition(self.tasks_dir / f".{normalized}.transition")

    def _update_thread_task_index(
        self,
        thread_id: str,
        task_id: str,
        status: str,
        current: float,
    ) -> ConversationThread:
        def updater(data: dict[str, Any]) -> dict[str, Any]:
            if not data:
                raise DataCorruptionError(f"conversation thread is unreadable: {thread_id}")
            latest = ConversationThread.from_dict(data)
            if latest.thread_id != thread_id:
                raise DataCorruptionError(f"conversation thread identity is invalid: {thread_id}")
            updated = (
                _thread_without_task(latest, task_id, current)
                if str(status or "").strip().lower() in THREAD_TASK_LINK_INACTIVE_STATUSES
                else _thread_with_task(latest, task_id, current)
            )
            return updated.to_dict()

        payload = update_json_file_atomic(
            self._thread_path(thread_id),
            updater,
            require_existing=True,
        )
        return ConversationThread.from_dict(payload)

    def bind_task(self, request: dict) -> ThreadTaskLink:
        thread_id = str(request.get("thread_id") or "")
        thread = self._require_thread(thread_id)
        task_id = str(request.get("task_id") or "")
        if not task_id:
            raise ValueError("task_id is required")
        current = now(request.get("now"))
        path = self._task_path(task_id)
        payload = update_json_file_atomic(
            path,
            lambda data: _merged_task_link(
                data,
                request=request,
                thread_id=thread.thread_id,
                task_id=task_id,
                current=current,
                path_exists=path.exists(),
            ).to_dict(),
        )
        link = ThreadTaskLink.from_dict(payload)
        indexed_thread = self._update_thread_task_index(thread.thread_id, task_id, link.status, current)
        _sync_task_workspace_status(link, current, owner_home=indexed_thread.owner_home)
        return link

    # LLM: This is the only durable writer for a thread's 会话运行时 sticky root workspace.
    # It validates the exact thread/task identity and does not alter task lifecycle status.
    # 函数用途: 记住该会话后续轮次默认进入哪个已有任务目录；不会启动、恢复或结束任务。
    def select_workspace_task(self, request: dict) -> ConversationThread:
        thread_id = str(request.get("thread_id") or "").strip()
        task_id = str(request.get("task_id") or "").strip()
        if not thread_id or not task_id:
            raise ValueError("thread_id and task_id are required")
        thread = self._require_thread(thread_id)
        link, error = _read_task_link(
            self._task_path(task_id),
            task_id,
            context="conversation.workspace_task.read",
        )
        if error is not None or link is None:
            raise DataCorruptionError(
                str((error or {}).get("message") or f"conversation task link is unavailable: {task_id}")
            )
        if link.thread_id != thread.thread_id:
            raise ValueError(f"task {task_id} is not bound to conversation thread {thread_id}")
        current = now(request.get("now"))

        def updater(data: dict[str, Any]) -> dict[str, Any]:
            if not data:
                raise DataCorruptionError(f"conversation thread is unreadable: {thread_id}")
            latest = ConversationThread.from_dict(data)
            if latest.thread_id != thread_id:
                raise DataCorruptionError(
                    f"conversation thread identity is invalid: {thread_id}"
                )
            if task_id not in latest.task_ids:
                raise ValueError(
                    f"task {task_id} is not indexed by conversation thread {thread_id}"
                )
            return replace(
                latest,
                workspace_task_id=task_id,
                updated_at=max(latest.updated_at, current),
            ).to_dict()

        payload = update_json_file_atomic(
            self._thread_path(thread_id),
            updater,
            require_existing=True,
        )
        return ConversationThread.from_dict(payload)

    def task_links(self, thread_id: str) -> list[ThreadTaskLink]:
        links, _load_errors = self.task_links_report(thread_id)
        return links

    def task_links_report(self, thread_id: str) -> tuple[list[ThreadTaskLink], list[dict[str, Any]]]:
        thread = self._require_thread(thread_id)
        return self._task_links_for_ids(thread.task_ids)

    def active_task_links_report(
        self, thread_id: str
    ) -> tuple[list[ThreadTaskLink], list[dict[str, Any]]]:
        thread = self._require_thread(thread_id)
        return self._task_links_for_ids(thread.active_task_ids)

    def _task_links_for_ids(
        self, task_ids: tuple[str, ...]
    ) -> tuple[list[ThreadTaskLink], list[dict[str, Any]]]:
        links: list[ThreadTaskLink] = []
        load_errors: list[dict[str, Any]] = []
        for task_id in task_ids:
            link, error = _read_task_link(self._task_path(task_id), str(task_id))
            if link is not None:
                links.append(link)
            if error is not None:
                load_errors.append(error)
        return links, load_errors

    def thread_for_task(self, task_id: str) -> ConversationThread | None:
        thread, load_error = self.thread_for_task_report(task_id)
        if load_error is not None:
            raise DataCorruptionError(str(load_error.get("message") or "conversation task link read failed"))
        return thread

    def thread_for_task_report(self, task_id: str) -> tuple[ConversationThread | None, dict[str, Any] | None]:
        path = self._task_path(task_id)
        if not path.exists():
            return None, None
        link, error = _read_task_link(path, task_id, context="conversation.thread_for_task")
        if error is not None:
            return None, error
        if link is None:
            return None, None
        return self.load_thread_report(link.thread_id)

    def update_task_status(self, request: dict) -> ThreadTaskLink | None:
        task_id = str(request.get("task_id") or "")
        path = self._task_path(task_id)
        if not path.exists():
            return None
        current = now(request.get("now"))
        requested_status = str(request.get("status") or "active")
        expected_status = str(request.get("expected_status") or "").strip().lower()
        updated = False

        def updater(data: dict[str, Any]) -> dict[str, Any]:
            nonlocal updated
            if not data:
                raise DataCorruptionError(f"conversation task link is unreadable: {task_id}")
            link_current = ThreadTaskLink.from_dict(data)
            if not link_current.thread_id or link_current.task_id != task_id:
                raise DataCorruptionError(f"conversation task link identity is invalid: {task_id}")
            if expected_status and str(link_current.status or "").strip().lower() != expected_status:
                return data
            updated = True
            return replace(link_current, status=requested_status).to_dict()

        payload = update_json_file_atomic(path, updater, require_existing=True)
        if not updated:
            return None
        link = ThreadTaskLink.from_dict(payload)
        # active_task_ids 是候选任务索引，不是历史归档。终态链接保留在
        # tasks/<id>.json 供精确反查，但必须从热索引移除，避免普通聊天
        # 每轮扫描并注入越来越多已完成工作。索引更新也在文件锁内合并，
        # 避免多个子任务同时绑定/结束时彼此覆盖 thread.task_ids。
        indexed_thread = self._update_thread_task_index(link.thread_id, task_id, link.status, current)
        _sync_task_workspace_status(link, current, owner_home=indexed_thread.owner_home)
        return link

    # LLM: Goal edits update display intent only and preserve task/thread/workspace identity.
    # 函数用途: 精确修改一个持久任务的用户可见目标文本。
    def update_task_goal(self, request: dict) -> ThreadTaskLink | None:
        """Update only the user-visible goal of one exact durable task link."""
        task_id = str(request.get("task_id") or "").strip()
        goal = str(request.get("goal") or "").strip()
        path = self._task_path(task_id)
        if not task_id or not goal or not path.exists():
            return None
        updated = False

        def updater(data: dict[str, Any]) -> dict[str, Any]:
            nonlocal updated
            if not data:
                raise DataCorruptionError(f"conversation task link is unreadable: {task_id}")
            current = ThreadTaskLink.from_dict(data)
            if not current.thread_id or current.task_id != task_id:
                raise DataCorruptionError(f"conversation task link identity is invalid: {task_id}")
            updated = True
            return replace(current, goal=goal).to_dict()

        payload = update_json_file_atomic(path, updater, require_existing=True)
        return ThreadTaskLink.from_dict(payload) if updated else None


# ---------------------------------------------------------------------------
# observation records
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _ObservationEventInput:
    thread_id: str
    event_type: str
    summary: str
    current: float
    kwargs: dict[str, Any]


def _observation_event(request: _ObservationEventInput) -> ObservationEvent:
    kwargs = request.kwargs
    refs = kwargs.get("evidence_refs") or []
    return ObservationEvent(
        observation_id=new_id("obs"),
        thread_id=request.thread_id,
        event_type=str(request.event_type or "observation"),
        summary=str(request.summary or ""),
        urgency=str(kwargs.get("urgency") or "normal"),
        severity=str(kwargs.get("severity") or ""),
        source_agent_id=str(kwargs.get("source_agent_id") or ""),
        parent_agent_id=str(kwargs.get("parent_agent_id") or ""),
        root_task_id=str(kwargs.get("root_task_id") or ""),
        evidence_refs=tuple(str(item) for item in refs),
        requires_main_agent=bool(kwargs.get("requires_main_agent")),
        requires_llm_report=bool(kwargs.get("requires_llm_report")),
        observed_at=request.current,
        metadata=kwargs.get("metadata") or {},
    )


def _observation_from_request(thread_id: str, request: dict) -> tuple[ObservationEvent, float]:
    current = now(request.get("now"))
    kwargs = {
        "urgency": request.get("urgency", "normal"),
        "severity": request.get("severity", ""),
        "source_agent_id": request.get("source_agent_id", ""),
        "parent_agent_id": request.get("parent_agent_id", ""),
        "root_task_id": request.get("root_task_id", ""),
        "evidence_refs": request.get("evidence_refs") or [],
        "requires_main_agent": request.get("requires_main_agent", False),
        "requires_llm_report": request.get("requires_llm_report", False),
        "metadata": request.get("metadata") or {},
    }
    return (
        _observation_event(
            _ObservationEventInput(
                thread_id,
                str(request.get("event_type") or ""),
                str(request.get("summary") or ""),
                current,
                kwargs,
            )
        ),
        current,
    )


def _observation_events(
    rows: list[dict[str, Any]],
    handled: dict[str, float],
) -> tuple[list[ObservationEvent], list[dict[str, Any]]]:
    events: list[ObservationEvent] = []
    errors: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        try:
            events.append(with_handled_at(ObservationEvent.from_dict(row), handled))
        except Exception as exc:
            report = runtime_error_report(exc, context="conversation.observations.parse")
            report["row_index"] = index
            errors.append(report)
    return events, errors


class ConversationObservationStore(ConversationTaskStore):
    def append_observation(self, request: dict) -> ObservationEvent:
        thread_id = str(request.get("thread_id") or "")
        thread = self._require_thread(thread_id)
        event, current = _observation_from_request(thread.thread_id, request)
        append_jsonl(self._observation_path(thread_id), event.to_dict(), sort_keys=True)
        self._write_thread(replace(thread, updated_at=current))
        return event

    def recent_observations(
        self, thread_id: str, *, limit: int = 20, include_handled: bool = True
    ) -> list[ObservationEvent]:
        events, _errors = self.recent_observations_report(
            thread_id, limit=limit, include_handled=include_handled,
        )
        return events

    def recent_observations_report(
        self,
        thread_id: str,
        *,
        limit: int = 20,
        include_handled: bool = True,
    ) -> tuple[list[ObservationEvent], list[dict[str, Any]]]:
        handled = self._read_observation_handled()
        report = read_jsonl_report(
            self._observation_path(thread_id),
            context="conversation.observations.read",
        )
        events, parse_errors = _observation_events(report.rows, handled)
        if not include_handled:
            events = [event for event in events if event.handled_at <= 0]
        selected = events if limit <= 0 else events[-limit:]
        return selected, [*report.load_errors, *parse_errors]

    def unhandled_observations_requiring_main(self, *, limit: int = 20) -> list[ObservationEvent]:
        events = [
            event
            for path in sorted(self.observations_dir.glob("*.jsonl"))
            for event in self.recent_observations(path.stem, limit=0, include_handled=False)
            if event.requires_main_agent or event.requires_llm_report
        ]
        events.sort(key=lambda item: item.observed_at)
        return events if limit <= 0 else events[:limit]

    def mark_observations_handled(
        self, observation_ids: list[str] | tuple[str, ...], *, now: float | None = None
    ) -> None:
        ids = [str(item) for item in observation_ids if str(item or "").strip()]
        if ids:
            current = now if now is not None else time.time()
            update_json_file_atomic(
                self.observation_handled_path,
                lambda data: {**data, **dict.fromkeys(ids, current)},
            )

    def _read_observation_handled(self) -> dict[str, float]:
        handled: dict[str, float] = {}
        for key, value in read_json_file(self.observation_handled_path).items():
            try:
                handled[str(key)] = float(value or 0.0)
            except (TypeError, ValueError):
                continue
        return handled


# ---------------------------------------------------------------------------
# guidance records
# ---------------------------------------------------------------------------

def _with_guidance_delivered_at(entry: GuidanceEntry, delivered: dict[str, float]) -> GuidanceEntry:
    return replace(entry, delivered_at=float(delivered.get(entry.guidance_id) or 0.0))


def _guidance_entries(
    rows: list[dict[str, Any]],
    delivered: dict[str, float],
) -> tuple[list[GuidanceEntry], list[dict[str, Any]]]:
    entries: list[GuidanceEntry] = []
    errors: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        try:
            entries.append(_with_guidance_delivered_at(GuidanceEntry.from_dict(row), delivered))
        except Exception as exc:
            report = runtime_error_report(exc, context="conversation.guidance.parse")
            report["row_index"] = index
            errors.append(report)
    return entries, errors


class ConversationGuidanceStore(ConversationObservationStore):
    def append_guidance(self, request: dict[str, Any]) -> GuidanceEntry:
        target_type = normalize_guidance_target_type(request.get("target_type"))
        target_id = str(request.get("target_id") or "").strip()
        message = str(request.get("message") or "").strip()
        if not target_type or not target_id:
            raise ValueError("target_type and target_id are required")
        if not message:
            raise ValueError("guidance message is required")
        entry = GuidanceEntry(
            guidance_id=new_id("guidance"),
            target_type=target_type,
            target_id=target_id,
            message=message,
            sender=str(request.get("sender") or "").strip(),
            priority=str(request.get("priority") or "normal").strip() or "normal",
            delivery=str(request.get("delivery") or "next_turn").strip() or "next_turn",
            created_at=now(request.get("now")),
            metadata=request.get("metadata") if isinstance(request.get("metadata"), dict) else {},
        )
        append_jsonl(self._guidance_path(target_type, target_id), entry.to_dict(), sort_keys=True)
        return entry

    def recent_guidance(
        self,
        target_type: str,
        target_id: str,
        *,
        limit: int = 20,
        include_delivered: bool = True,
    ) -> list[GuidanceEntry]:
        entries, _errors = self.recent_guidance_report(
            target_type, target_id, limit=limit, include_delivered=include_delivered,
        )
        return entries

    def recent_guidance_report(
        self,
        target_type: str,
        target_id: str,
        *,
        limit: int = 20,
        include_delivered: bool = True,
    ) -> tuple[list[GuidanceEntry], list[dict[str, Any]]]:
        normalized_type = normalize_guidance_target_type(target_type)
        if not normalized_type:
            return [], [{"code": "GUIDANCE_TARGET_TYPE_INVALID", "target_type": str(target_type or "")}]
        delivered = self._read_guidance_delivered()
        report = read_jsonl_report(
            self._guidance_path(normalized_type, str(target_id)),
            context="conversation.guidance.read",
        )
        entries, parse_errors = _guidance_entries(report.rows, delivered)
        if not include_delivered:
            entries = [item for item in entries if item.delivered_at <= 0]
        selected = entries if limit <= 0 else entries[-limit:]
        return selected, [*report.load_errors, *parse_errors]

    def pending_guidance(self, target_type: str, target_id: str, *, limit: int = 20) -> list[GuidanceEntry]:
        return self.recent_guidance(target_type, target_id, limit=limit, include_delivered=False)

    def pending_guidance_report(
        self, target_type: str, target_id: str, *, limit: int = 20
    ) -> tuple[list[GuidanceEntry], list[dict[str, Any]]]:
        return self.recent_guidance_report(target_type, target_id, limit=limit, include_delivered=False)

    def mark_guidance_delivered(self, guidance_ids: list[str] | tuple[str, ...], *, now: float | None = None) -> None:
        ids = [str(item).strip() for item in guidance_ids if str(item or "").strip()]
        if not ids:
            return
        delivered_at = now if now is not None else time.time()
        update_json_file_atomic(
            self.guidance_delivered_path,
            lambda data: {**data, **dict.fromkeys(ids, delivered_at)},
        )

    def _read_guidance_delivered(self) -> dict[str, float]:
        delivered: dict[str, float] = {}
        for key, value in read_json_file(self.guidance_delivered_path).items():
            try:
                delivered[str(key)] = float(value or 0.0)
            except (TypeError, ValueError):
                continue
        return delivered


# ---------------------------------------------------------------------------
# wake signals
# ---------------------------------------------------------------------------

def _wake_signal(thread_id: str, current: float, kwargs: dict[str, Any]) -> WakeSignal:
    observation = kwargs.get("observation")
    observation = observation if isinstance(observation, ObservationEvent) else None
    return WakeSignal(
        wake_signal_id=new_id("wake"),
        thread_id=thread_id,
        observation_id=observation.observation_id if observation is not None else "",
        urgency=wake_urgency(kwargs.get("urgency", "urgent")),
        severity=kwargs.get("severity") or (observation.severity if observation is not None else ""),
        reason=str(kwargs.get("reason") or "agent_event"),
        source_agent_id=kwargs.get("source_agent_id")
        or (observation.source_agent_id if observation is not None else ""),
        parent_agent_id=kwargs.get("parent_agent_id")
        or (observation.parent_agent_id if observation is not None else ""),
        root_task_id=kwargs.get("root_task_id") or (observation.root_task_id if observation is not None else ""),
        summary=kwargs.get("summary") or (observation.summary if observation is not None else ""),
        evidence_refs=wake_evidence_refs(observation, kwargs.get("evidence_refs")),
        created_at=current,
        dedupe_key=str(kwargs.get("dedupe_key") or ""),
        metadata=kwargs.get("metadata") or {},
    )


def _wake_kinds(include_normal: bool) -> tuple[str, ...]:
    return ("urgent", "normal") if include_normal else ("urgent",)


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _read_wake_signal(path: Path) -> tuple[WakeSignal | None, dict[str, Any] | None]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"wake signal file is {type(payload).__name__}, expected object")
        return WakeSignal.from_dict(payload), None
    except Exception as exc:
        report = runtime_error_report(exc, context="conversation.wake_signal.read")
        report["path"] = str(path)
        return None, report


_UNFINISHED_GOAL_STATUSES = THREAD_GOAL_STATUSES - {"complete"}
_LEGACY_CLEARED_GOAL_STATUS = "cleared"


# LLM: Parse goal files fail-closed and return corruption as a structured load report.
# 函数用途: 读取并校验一份持续目标记录，损坏时保留明确错误。
def _read_thread_goal_report(path: Path) -> tuple[ThreadGoal | None, dict[str, Any] | None]:
    payload, error = _read_json_object_report(path, context="conversation.goal.read")
    if error is not None or not payload:
        return None, error
    try:
        goal = ThreadGoal.from_dict(payload)
        if not goal.goal_id or not goal.thread_id or not goal.task_id:
            raise DataCorruptionError(f"thread goal identity is invalid: {path}")
        # Releases before 会话运行时 goal clear persisted a terminal
        # ``cleared`` tombstone. Current clear deletes the goal record, so a
        # well-formed legacy tombstone is the on-disk representation of no goal.
        # Keep every other unknown status fail-closed.
        if goal.status == _LEGACY_CLEARED_GOAL_STATUS:
            return None, None
        if goal.status not in THREAD_GOAL_STATUSES:
            raise DataCorruptionError(f"thread goal status is invalid: {goal.status}")
        return goal, None
    except Exception as exc:
        report = runtime_error_report(exc, context="conversation.goal.read")
        report["path"] = str(path)
        return None, report


# LLM: This mixin is the single persistence authority for the `/goal` overlay of a conversation thread.
# 类用途: 持久化当前会话的持续目标，并提供原子生命周期与用量计量。
class ConversationGoalStore(ConversationGuidanceStore):
    """Persistent `/goal` overlay for an existing conversation thread."""

    # LLM: The live wall clock is process-local like 会话运行时 GoalWallClockAccounting;
    # persisted updated_at is presentation metadata and must never be used as a timer.
    # 函数用途: 启动或恢复同一目标的运行时计时基线，服务停机时不累计耗时。
    def begin_goal_accounting(
        self,
        goal: ThreadGoal,
        *,
        reset: bool = False,
        monotonic_now: float | None = None,
    ) -> None:
        current = time.monotonic() if monotonic_now is None else float(monotonic_now)
        with self._goal_clock_lock:
            prior = self._goal_clock.get(goal.thread_id)
            if reset or prior is None or prior[0] != goal.goal_id:
                self._goal_clock[goal.thread_id] = (goal.goal_id, current)

    # LLM: Whole-second accounting preserves the fractional remainder, matching
    # 会话运行时's Instant::elapsed().as_secs() plus mark_accounted advance behavior.
    # 函数用途: 取出本目标自上次结算后的完整秒数，并推进内存计时基线。
    def take_goal_elapsed_seconds(
        self,
        goal: ThreadGoal,
        *,
        monotonic_now: float | None = None,
    ) -> int:
        current = time.monotonic() if monotonic_now is None else float(monotonic_now)
        with self._goal_clock_lock:
            prior = self._goal_clock.get(goal.thread_id)
            if prior is None or prior[0] != goal.goal_id:
                self._goal_clock[goal.thread_id] = (goal.goal_id, current)
                return 0
            elapsed = max(0, int(current - prior[1]))
            if elapsed > 0:
                self._goal_clock[goal.thread_id] = (goal.goal_id, prior[1] + elapsed)
            return elapsed

    # LLM: Paused, blocked, limited, completed, replaced, or cleared goals stop
    # the active runtime clock without changing their durable usage snapshot.
    # 函数用途: 清除指定目标的运行时计时状态，避免后续目标继承旧基线。
    def clear_goal_accounting(self, thread_id: str, *, goal_id: str = "") -> None:
        with self._goal_clock_lock:
            prior = self._goal_clock.get(thread_id)
            if prior is None or (goal_id and prior[0] != goal_id):
                return
            self._goal_clock.pop(thread_id, None)

    # LLM: All load-plus-mutate goal operations for one thread share this filesystem transition lock.
    # 函数用途: 为单个 thread 的目标查看和迁移提供跨线程/跨进程临界区。
    def goal_transition_guard(self, thread_id: str):
        normalized = safe_file_stem(str(thread_id or "").strip())
        if not normalized:
            raise ValueError("thread_id is required")
        return locked_file_transition(self.goals_dir / f".{normalized}.transition")

    # LLM: Strict callers receive corruption as an exception rather than an apparent empty goal.
    # 函数用途: 严格读取当前 thread 目标，损坏时 fail-closed。
    def load_goal(self, thread_id: str) -> ThreadGoal | None:
        goal, error = self.load_goal_report(thread_id)
        if error is not None:
            raise DataCorruptionError(str(error.get("message") or "conversation goal read failed"))
        if goal is not None and goal.status == "active":
            self.begin_goal_accounting(goal)
        elif goal is not None and goal.status not in {"active", "budget_limited"}:
            self.clear_goal_accounting(goal.thread_id, goal_id=goal.goal_id)
        return goal

    # LLM: Request assembly uses the report form so it can expose a typed load failure without mutating state.
    # 函数用途: 读取目标与结构化错误，供 Gateway 上下文装配使用。
    def load_goal_report(
        self, thread_id: str
    ) -> tuple[ThreadGoal | None, dict[str, Any] | None]:
        normalized = str(thread_id or "").strip()
        self._require_thread(normalized)
        return _read_thread_goal_report(self._goal_path(normalized))

    # LLM: Atomically enforce one unfinished goal per thread and allocate one stable root task identity.
    # 函数用途: 在当前会话原子创建一个持续目标及其根任务身份。
    def create_goal(self, request: dict[str, Any]) -> ThreadGoal:
        thread_id = str(request.get("thread_id") or "").strip()
        objective = str(request.get("objective") or "").strip()
        self._require_thread(thread_id)
        if not objective:
            raise ValueError("goal objective is required")
        if len(objective) > THREAD_GOAL_OBJECTIVE_MAX_CHARS:
            raise ValueError(
                f"goal objective exceeds {THREAD_GOAL_OBJECTIVE_MAX_CHARS} characters"
            )
        token_budget_value = request.get("token_budget")
        token_budget = int(token_budget_value) if token_budget_value is not None else None
        if token_budget is not None and token_budget <= 0:
            raise ValueError("goal token_budget must be positive")
        current_time = now(request.get("now"))
        created: ThreadGoal | None = None

        def updater(data: dict[str, Any]) -> dict[str, Any]:
            nonlocal created
            if data:
                existing = ThreadGoal.from_dict(data)
                if existing.status in _UNFINISHED_GOAL_STATUSES:
                    raise ValueError("an unfinished goal already exists for this thread")
            goal_id = new_id("goal")
            created = ThreadGoal(
                goal_id=goal_id,
                thread_id=thread_id,
                objective=objective,
                task_id=str(request.get("task_id") or f"goal-task-{goal_id.removeprefix('goal-')}"),
                token_budget=token_budget,
                created_at=current_time,
                updated_at=current_time,
                metadata=request.get("metadata") if isinstance(request.get("metadata"), dict) else {},
            )
            return created.to_dict()

        payload = update_json_file_atomic(self._goal_path(thread_id), updater)
        goal = created or ThreadGoal.from_dict(payload)
        self.begin_goal_accounting(goal, reset=True)
        return goal

    # LLM: Compare expected goal/status before changing the objective or system-owned lifecycle.
    # 函数用途: 以 CAS 语义修改目标内容或状态，竞态失败返回空。
    def update_goal(self, request: dict[str, Any]) -> ThreadGoal | None:
        thread_id = str(request.get("thread_id") or "").strip()
        requested_status = str(request.get("status") or "").strip().lower()
        expected_status = str(request.get("expected_status") or "").strip().lower()
        expected_goal_id = str(request.get("goal_id") or request.get("expected_goal_id") or "").strip()
        objective = str(request.get("objective") or "").strip()
        if requested_status and requested_status not in THREAD_GOAL_STATUSES:
            raise ValueError(f"unsupported goal status: {requested_status}")
        if objective and len(objective) > THREAD_GOAL_OBJECTIVE_MAX_CHARS:
            raise ValueError(
                f"goal objective exceeds {THREAD_GOAL_OBJECTIVE_MAX_CHARS} characters"
            )
        if not requested_status and not objective and "token_budget" not in request:
            raise ValueError("goal update is empty")
        token_budget = request.get("token_budget")
        if "token_budget" in request and token_budget is not None:
            token_budget = int(token_budget)
            if token_budget <= 0:
                raise ValueError("goal token_budget must be positive")
        self._require_thread(thread_id)
        current_time = now(request.get("now"))
        changed = False
        previous_status = ""

        def updater(data: dict[str, Any]) -> dict[str, Any]:
            nonlocal changed, previous_status
            if not data:
                return data
            current = ThreadGoal.from_dict(data)
            if current.thread_id != thread_id:
                raise DataCorruptionError(f"thread goal identity is invalid: {thread_id}")
            if expected_goal_id and current.goal_id != expected_goal_id:
                return data
            if expected_status and current.status != expected_status:
                return data
            changed = True
            previous_status = current.status
            status = requested_status or current.status
            next_budget = token_budget if "token_budget" in request else current.token_budget
            if (
                status == "active"
                and next_budget is not None
                and current.tokens_used >= next_budget
            ):
                status = "budget_limited"
            elapsed = (
                self.take_goal_elapsed_seconds(current)
                if current.status in {"active", "budget_limited"}
                else 0
            )
            return replace(
                current,
                objective=objective or current.objective,
                status=status,
                token_budget=next_budget,
                time_used_seconds=current.time_used_seconds + elapsed,
                updated_at=current_time,
            ).to_dict()

        payload = update_json_file_atomic(
            self._goal_path(thread_id), updater, require_existing=True
        )
        if not changed:
            return None
        updated = ThreadGoal.from_dict(payload)
        if updated.status == "active":
            self.begin_goal_accounting(updated, reset=previous_status != "active")
        else:
            self.clear_goal_accounting(updated.thread_id, goal_id=updated.goal_id)
        return updated

    # LLM: Clear deletes the persisted goal record, matching 会话运行时; task files and transcript remain intact.
    # 函数用途: 以目标编号 CAS 删除当前 thread 的目标记录。
    def delete_goal(self, thread_id: str, *, expected_goal_id: str = "") -> ThreadGoal | None:
        normalized = str(thread_id or "").strip()
        self._require_thread(normalized)
        path = self._goal_path(normalized)
        current = self.load_goal(normalized)
        if current is None:
            return None
        if expected_goal_id and current.goal_id != expected_goal_id:
            return None
        path.unlink(missing_ok=True)
        self.clear_goal_accounting(normalized, goal_id=current.goal_id)
        return current

    # LLM: Charge only the exact active goal; reaching the budget is a system-owned status transition.
    # 函数用途: 原子累计目标 token 和活跃耗时，并在达到预算时标记 budget_limited。
    def account_goal_usage(self, request: dict[str, Any]) -> ThreadGoal | None:
        thread_id = str(request.get("thread_id") or "").strip()
        expected_goal_id = str(request.get("goal_id") or "").strip()
        token_delta = max(0, int(request.get("token_delta") or 0))
        time_delta = max(0, int(request.get("time_delta_seconds") or 0))
        mode = str(request.get("mode") or "active_status_only").strip()
        allowed_statuses = {
            "active_status_only": {"active"},
            "active_only": {"active", "budget_limited"},
            "active_or_complete": {"active", "budget_limited", "complete"},
            "active_or_stopped": {
                "active",
                "paused",
                "blocked",
                "usage_limited",
                "budget_limited",
            },
        }.get(mode)
        if allowed_statuses is None:
            raise ValueError(f"unsupported goal accounting mode: {mode}")
        self._require_thread(thread_id)
        if token_delta == 0 and time_delta == 0:
            goal = self.load_goal(thread_id)
            return goal if goal is not None and goal.goal_id == expected_goal_id else None
        current_time = now(request.get("now"))
        changed = False

        def updater(data: dict[str, Any]) -> dict[str, Any]:
            nonlocal changed
            if not data:
                return data
            current = ThreadGoal.from_dict(data)
            if current.goal_id != expected_goal_id or current.status not in allowed_statuses:
                return data
            changed = True
            tokens_used = current.tokens_used + token_delta
            status = current.status
            if (
                current.status == "active"
                and current.token_budget is not None
                and tokens_used >= current.token_budget
            ):
                status = "budget_limited"
            return replace(
                current,
                tokens_used=tokens_used,
                time_used_seconds=current.time_used_seconds + time_delta,
                status=status,
                updated_at=current_time,
            ).to_dict()

        payload = update_json_file_atomic(self._goal_path(thread_id), updater, require_existing=True)
        return ThreadGoal.from_dict(payload) if changed else None


class ConversationWakeStore(ConversationGoalStore):
    def append_observation_with_wake(
        self,
        observation_request: dict,
        wake_request: dict,
    ) -> tuple[ObservationEvent, WakeSignal]:
        """Publish the wake before its observation so the scheduler cannot race the pair."""
        thread_id = str(observation_request.get("thread_id") or "")
        thread = self._require_thread(thread_id)
        wake_thread_id = str(wake_request.get("thread_id") or thread_id)
        if wake_thread_id != thread.thread_id:
            raise ValueError("observation and wake signal must use the same thread")
        observation, observed_at = _observation_from_request(thread.thread_id, observation_request)
        kwargs = {
            "observation": observation,
            "urgency": wake_request.get("urgency", "urgent"),
            "severity": wake_request.get("severity", ""),
            "reason": wake_request.get("reason", "agent_event"),
            "source_agent_id": wake_request.get("source_agent_id", ""),
            "parent_agent_id": wake_request.get("parent_agent_id", ""),
            "root_task_id": wake_request.get("root_task_id", ""),
            "summary": wake_request.get("summary", ""),
            "evidence_refs": wake_request.get("evidence_refs"),
            "dedupe_key": wake_request.get("dedupe_key", ""),
            "metadata": wake_request.get("metadata") or {},
        }
        signal = _wake_signal(thread.thread_id, now(wake_request.get("now")), kwargs)
        try:
            selected = (
                self._raise_deduped_wake_signal(signal)
                if signal.dedupe_key
                else self._write_new_wake_signal(signal)
            )
        except Exception:
            # Keep the old observation-only fallback if the wake queue itself is unavailable.
            append_jsonl(self._observation_path(thread_id), observation.to_dict(), sort_keys=True)
            self._write_thread(replace(thread, updated_at=observed_at))
            raise
        linked = replace(observation, wake_signal_id=selected.wake_signal_id)
        if selected.wake_signal_id == signal.wake_signal_id:
            append_jsonl(self._observation_path(thread_id), linked.to_dict(), sort_keys=True)
            self._write_thread(replace(thread, updated_at=observed_at))
        return linked, selected

    def _write_new_wake_signal(self, signal: WakeSignal) -> WakeSignal:
        write_json_file_atomic(self._wake_signal_path(signal), signal.to_dict())
        return signal

    def raise_wake_signal(self, request: dict) -> WakeSignal:
        thread_id = str(request.get("thread_id") or "")
        thread = self._require_thread(thread_id)
        kwargs = {
            "observation": request.get("observation"),
            "urgency": request.get("urgency", "urgent"),
            "severity": request.get("severity", ""),
            "reason": request.get("reason", "agent_event"),
            "source_agent_id": request.get("source_agent_id", ""),
            "parent_agent_id": request.get("parent_agent_id", ""),
            "root_task_id": request.get("root_task_id", ""),
            "summary": request.get("summary", ""),
            "evidence_refs": request.get("evidence_refs"),
            "dedupe_key": request.get("dedupe_key", ""),
            "metadata": request.get("metadata") or {},
        }
        signal = _wake_signal(thread.thread_id, now(request.get("now")), kwargs)
        if signal.dedupe_key:
            return self._raise_deduped_wake_signal(signal)
        write_json_file_atomic(self._wake_signal_path(signal), signal.to_dict())
        return signal

    def pending_wake_signals(self, *, limit: int = 100, include_normal: bool = True) -> list[WakeSignal]:
        signals, _load_errors = self.pending_wake_signals_report(limit=limit, include_normal=include_normal)
        return signals

    def pending_wake_signals_report(
        self, *, limit: int = 100, include_normal: bool = True
    ) -> tuple[list[WakeSignal], list[dict[str, Any]]]:
        signals: list[WakeSignal] = []
        load_errors: list[dict[str, Any]] = []
        for kind in _wake_kinds(include_normal):
            kind_signals, kind_errors = self._pending_signals_report(kind)
            signals.extend(kind_signals)
            load_errors.extend(kind_errors)
        signals.sort(key=lambda item: (0 if item.urgency == "urgent" else 1, item.created_at))
        selected = signals if limit <= 0 else signals[:limit]
        return selected, load_errors

    def mark_wake_signal_handled(self, wake_signal_id: str, *, now: float | None = None) -> WakeSignal | None:
        path = self._find_wake_signal_path(wake_signal_id)
        if path is None or not (data := read_json_file(path)):
            return None
        current = now if now is not None else time.time()
        handled = replace(WakeSignal.from_dict(data), status="handled", handled_at=current)
        write_json_file_atomic(self.wake_handled_dir / f"{handled.wake_signal_id}.json", handled.to_dict())
        _unlink_quietly(path)
        if handled.observation_id:
            self.mark_observations_handled([handled.observation_id], now=handled.handled_at)
        return handled

    def _find_wake_signal_path(self, wake_signal_id: str) -> Path | None:
        name = f"{wake_signal_id}.json"
        return next(
            (path for kind in ("urgent", "normal") if (path := self.wake_queue_dir / kind / name).exists()), None
        )

    def _pending_signals(self, kind: str) -> list[WakeSignal]:
        signals, _load_errors = self._pending_signals_report(kind)
        return signals

    def _pending_signals_report(self, kind: str) -> tuple[list[WakeSignal], list[dict[str, Any]]]:
        signals: list[WakeSignal] = []
        load_errors: list[dict[str, Any]] = []
        for path in sorted((self.wake_queue_dir / kind).glob("*.json")):
            signal, error = _read_wake_signal(path)
            if error is not None:
                load_errors.append(error)
            if signal is not None and signal.status == "pending":
                signals.append(signal)
        return signals, load_errors

    def _raise_deduped_wake_signal(self, signal: WakeSignal) -> WakeSignal:
        selected: WakeSignal | None = None

        def updater(data: dict[str, Any]) -> dict[str, Any]:
            nonlocal selected
            existing = self._pending_wake_by_id(str(data.get("wake_signal_id") or ""))
            if existing is not None and existing.thread_id == signal.thread_id:
                selected = existing
                return data
            write_json_file_atomic(self._wake_signal_path(signal), signal.to_dict())
            selected = signal
            return {
                "schema_version": "wake_dedupe.v1",
                "thread_id": signal.thread_id,
                "dedupe_key": signal.dedupe_key,
                "wake_signal_id": signal.wake_signal_id,
                "updated_at": signal.created_at,
            }

        update_json_file_atomic(self._wake_dedupe_path(signal.thread_id, signal.dedupe_key), updater)
        return selected or signal

    def _pending_wake_by_id(self, wake_signal_id: str) -> WakeSignal | None:
        path = self._find_wake_signal_path(wake_signal_id)
        data = read_json_file(path) if path is not None else {}
        signal = WakeSignal.from_dict(data) if data else None
        return signal if signal is not None and signal.status == "pending" else None


# ---------------------------------------------------------------------------
# progress records
# ---------------------------------------------------------------------------

def _progress_policy_read_error(path: Path, exc: BaseException) -> dict[str, Any]:
    report = runtime_error_report(exc, context="conversation.progress_policy.read")
    report["path"] = str(path)
    report["policy_id"] = path.stem
    return report


def _read_progress_policy_report(path: Path) -> tuple[ProgressPolicy | None, dict[str, Any] | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"progress policy is {type(data).__name__}, expected object")
        return ProgressPolicy.from_dict(data), None
    except (OSError, UnicodeError, ValueError) as exc:
        return None, _progress_policy_read_error(path, exc)


# 无进展退避封顶倍数:间隔最多拉长到 8×(streak≥3 封顶),够让卡死任务把资源让给活任务,
# 又不至于把守望任务拖到没响应;有进展即归零复原,永不 disable。
_NO_PROGRESS_MAX_BACKOFF_MULTIPLIER = 8


class ConversationProgressStore(ConversationWakeStore):
    def set_progress_policy(self, request: dict) -> ProgressPolicy:
        thread_id = str(request.get("thread_id") or "")
        self._require_thread(thread_id)
        current = now(request.get("now"))
        interval_seconds = max(0, int(request.get("interval_seconds") or 0))
        policy = ProgressPolicy(
            policy_id=new_id("policy"),
            thread_id=thread_id,
            task_id=str(request.get("task_id") or ""),
            interval_seconds=interval_seconds,
            next_due_at=current + interval_seconds,
            route_channel=str(request.get("route_channel") or "internal"),
            route_target=str(request.get("route_target") or ""),
            metadata=request.get("metadata") or {},
        )
        write_json_file_atomic(self._policy_path(policy.policy_id), policy.to_dict())
        return policy

    def get_progress_policy(self, policy_id: str) -> ProgressPolicy | None:
        policy, _ = _read_progress_policy_report(self._policy_path(policy_id))
        return policy

    def list_progress_policies(self, *, enabled_only: bool = False) -> list[ProgressPolicy]:
        policies, _ = self.list_progress_policies_report(enabled_only=enabled_only)
        return policies

    def list_progress_policies_report(
        self, *, enabled_only: bool = False
    ) -> tuple[list[ProgressPolicy], list[dict[str, Any]]]:
        policies: list[ProgressPolicy] = []
        load_errors: list[dict[str, Any]] = []
        for path in sorted(self.policies_dir.glob("*.json")):
            policy, error = _read_progress_policy_report(path)
            if policy is not None:
                policies.append(policy)
            if error is not None:
                load_errors.append(error)
        policies = [policy for policy in policies if policy.enabled] if enabled_only else policies
        policies.sort(key=lambda item: item.next_due_at)
        return policies, load_errors

    def due_progress_policies(self, *, now: float | None = None) -> list[ProgressPolicy]:
        policies, _ = self.due_progress_policies_report(now=now)
        return policies

    def due_progress_policies_report(
        self, *, now: float | None = None
    ) -> tuple[list[ProgressPolicy], list[dict[str, Any]]]:
        current = now if now is not None else time.time()
        policies, load_errors = self.list_progress_policies_report(enabled_only=True)
        return [policy for policy in policies if policy.next_due_at <= current], load_errors

    def mark_progress_reported(
        self,
        policy_id: str,
        *,
        now: float | None = None,
        no_progress_streak: int | None = None,
        metadata_updates: dict[str, Any] | None = None,
    ) -> ProgressPolicy:
        # no_progress_streak(§6-B4 退避):调度器在唤醒轮结束后按【结构化信号】(本轮工具调用
        # 全失败或压根没调工具=无进展)传入连续无进展轮数;间隔按 2^streak 拉长、封顶 8 倍——
        # 卡死任务自动让出资源但【永不停机】(区别于 disable 退休),一有进展 streak 归零复原。
        # None = 旧语义原样(按原 interval 顺延,不碰 streak 账目),供续命/去重等非执行路径用。
        policy = self.get_progress_policy(policy_id)
        if policy is None:
            raise KeyError(f"unknown progress policy: {policy_id}")
        current = now if now is not None else time.time()
        interval = max(0, policy.interval_seconds)
        metadata = dict(policy.metadata or {})
        if metadata_updates:
            metadata.update(metadata_updates)
        if no_progress_streak is not None:
            streak = max(0, int(no_progress_streak))
            metadata["no_progress_streak"] = streak
            interval = interval * min(2**streak, _NO_PROGRESS_MAX_BACKOFF_MULTIPLIER)
        updated = replace(
            policy, last_report_at=current, next_due_at=current + interval, metadata=metadata
        )
        write_json_file_atomic(self._policy_path(policy_id), updated.to_dict())
        return updated

    def mark_progress_checked(
        self,
        policy_id: str,
        *,
        now: float | None = None,
        metadata_updates: dict[str, Any] | None = None,
    ) -> ProgressPolicy:
        """Snooze an unchanged automatic check without recording a model report."""
        policy = self.get_progress_policy(policy_id)
        if policy is None:
            raise KeyError(f"unknown progress policy: {policy_id}")
        current = now if now is not None else time.time()
        metadata = dict(policy.metadata or {})
        if metadata_updates:
            metadata.update(metadata_updates)
        metadata["last_material_check_at"] = current
        updated = replace(
            policy,
            next_due_at=current + max(0, policy.interval_seconds),
            metadata=metadata,
        )
        write_json_file_atomic(self._policy_path(policy_id), updated.to_dict())
        return updated

    def disable_progress_policy(self, policy_id: str, *, now: float | None = None) -> ProgressPolicy | None:
        # 退休一个进度策略：把 enabled 置 False，使它从 due 扫描里彻底消失。
        # 用于回收"被观察任务已终态/早已 stale"的后台 watch 策略，避免它被无限续命、
        # 每个间隔唤醒后台主代理发一次 LLM 进度汇报，把 gateway worker 占满（churn 根因）。
        policy = self.get_progress_policy(policy_id)
        if policy is None:
            return None
        if not policy.enabled:
            return policy
        current = now if now is not None else time.time()
        updated = replace(policy, enabled=False, last_report_at=current)
        write_json_file_atomic(self._policy_path(policy_id), updated.to_dict())
        return updated

    def expedite_progress_policy(
        self, policy_id: str, *, due_at: float, reason: str = "", now: float | None = None
    ) -> ProgressPolicy | None:
        # 单调提前一个 enabled 策略的下次触发时间(只往早、绝不往晚推)。调度器按结构信号
        # (如盯守 backlog 有活堆着)给排期封响应上限用:不改 interval_seconds——模型自选的
        # 节奏意图保留,信号消失后自动回到原节奏。目标时间不早于现值时原样返回(幂等不写盘)。
        policy = self.get_progress_policy(policy_id)
        if policy is None or not policy.enabled:
            return None
        if due_at >= policy.next_due_at:
            return policy
        current = now if now is not None else time.time()
        metadata = dict(policy.metadata or {})
        try:
            expedite_count = int(metadata.get("expedite_count") or 0)
        except (TypeError, ValueError):
            expedite_count = 0
        metadata["expedite_count"] = expedite_count + 1
        metadata["expedited_at"] = current
        metadata["expedite_reason"] = str(reason or "")
        updated = replace(policy, next_due_at=float(due_at), metadata=metadata)
        write_json_file_atomic(self._policy_path(policy_id), updated.to_dict())
        return updated


# ---------------------------------------------------------------------------
# background claims
# ---------------------------------------------------------------------------

_FINISH_STATUSES = {"finished", "failed", "cancelled"}
_INVALID_FINISH_STATUS = "invalid_status"


@dataclass(frozen=True)
class BackgroundClaimPayload:
    thread_id: str
    reason: str
    current: float
    lease: int
    task_id: str = ""
    owner_process: dict[str, object] = field(default_factory=dict)


def _new_claim(payload: BackgroundClaimPayload) -> dict[str, Any]:
    return {
        "schema_version": "background_run_claim.v1",
        "claim_id": new_id("bgclaim"),
        "thread_id": payload.thread_id,
        "task_id": payload.task_id,
        "reason": str(payload.reason or ""),
        "status": "running",
        "phase": "claimed",
        "started_at": payload.current,
        "heartbeat_at": payload.current,
        "expires_at": payload.current + payload.lease,
        "owner_process": dict(payload.owner_process),
        "acquisition": {"reason": "new_claim"},
        "takeover": {"allowed": False, "reason": "claim_running"},
    }


def _claim_lease_seconds(value: object) -> int:
    if value is None or value == "":
        value = default_config_value("background_claim_ttl_seconds")
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return max(1, int(default_config_value("background_claim_ttl_seconds")))


def _finish_status(value: object) -> str:
    status = str(value or "").strip()
    return status if status in _FINISH_STATUSES else _INVALID_FINISH_STATUS


def _error_payload(value: object) -> dict[str, Any]:
    if isinstance(value, BaseException):
        return {"type": type(value).__name__, "message": str(value)}
    if isinstance(value, dict):
        return {
            "type": str(value.get("type") or value.get("error_type") or ""),
            "message": str(value.get("message") or value.get("error") or ""),
        }
    text = str(value or "").strip()
    return {"type": "", "message": text} if text else {}


def _takeover_payload(status: str) -> dict[str, Any]:
    if status == "failed":  # CLAIM_STATUS_FAILED
        return {"allowed": True, "reason": "runtime_failed"}
    if status == "cancelled":
        return {"allowed": False, "reason": "user_interrupted"}
    if status == _INVALID_FINISH_STATUS:
        return {"allowed": True, "reason": "runtime_invalid_status"}
    return {"allowed": False, "reason": "run_finished"}


def _previous_claim_summary(data: dict[str, Any], current: float) -> dict[str, Any]:
    if not data:
        return {}
    status = str(data.get("status") or "")
    expires_at = float_value(data.get("expires_at"))
    return {
        "claim_id": str(data.get("claim_id") or ""),
        "status": status,
        "reason": str(data.get("reason") or ""),
        "task_id": str(data.get("task_id") or ""),
        "heartbeat_at": float_value(data.get("heartbeat_at")),
        "expires_at": expires_at,
        "expired": bool(expires_at and expires_at <= current),
        "owner_process": dict(data.get("owner_process")) if isinstance(data.get("owner_process"), dict) else {},
        "last_error": _error_payload(data.get("last_error")),
        "takeover": data.get("takeover") if isinstance(data.get("takeover"), dict) else _takeover_payload(status),
    }


# LLM: 接管原因只来自旧 claim 的结构化状态与租约时钟；同进程域死进程的提前接管由调用方
# 单独标为 owner_process_stale，跨进程域/旧格式记录仍必须等 TTL。
# 函数用途: 给每次成功获取租约写清“新建、到期或终态接手”的机器原因。
def _claim_acquisition_reason(data: dict[str, Any], current: float) -> str:
    if not data:
        return "new_claim"
    status = str(data.get("status") or "")
    if status == "running" and float_value(data.get("expires_at")) <= current:
        return "lease_expired"
    return f"previous_{status or 'unknown'}"


def _read_claim_report(path: Path) -> tuple[dict[str, Any], dict[str, Any] | None]:
    if not path.exists():
        return {}, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {}, _claim_load_error(exc, path)
    if not isinstance(payload, dict):
        return {}, _claim_load_error(
            DataCorruptionError(f"background claim must be a JSON object: {path}"), path
        )
    return payload, None


def _claim_load_error(exc: BaseException, path: Path) -> dict[str, Any]:
    report = runtime_error_report(exc, context="conversation.background_claim.read")
    report["path"] = str(path)
    return report


class ConversationClaimStore(ConversationProgressStore):
    def claim_background_run(self, request: dict) -> dict[str, Any] | None:
        thread_id = str(request.get("thread_id") or "")
        thread = self._require_thread(thread_id)
        current = now(request.get("now"))
        lease = _claim_lease_seconds(request.get("lease_seconds"))
        claim = _new_claim(BackgroundClaimPayload(
            thread_id=thread.thread_id,
            reason=str(request.get("reason") or ""),
            current=current,
            lease=lease,
            task_id=str(request.get("task_id") or ""),
            owner_process=build_process_identity(),
        ))
        claimed = False

        def updater(data: dict[str, Any]) -> dict[str, Any]:
            nonlocal claimed
            active = str(data.get("status") or "") == "running" and float_value(data.get("expires_at")) > current
            owner_stale = active and process_identity_is_live(data.get("owner_process")) is False
            if active and not owner_stale:
                claimed = False
                return data
            claimed = True
            acquisition_reason = "owner_process_stale" if owner_stale else _claim_acquisition_reason(data, current)
            return {
                **claim,
                "acquisition": {"reason": acquisition_reason},
                "previous_claim": _previous_claim_summary(data, current),
            }

        updated = update_json_file_atomic(self._background_claim_path(thread.thread_id), updater)
        return updated if claimed else None

    def load_background_run_claim(self, thread_id: str) -> dict[str, Any]:
        claim, load_error = self.load_background_run_claim_report(thread_id)
        return claim if not load_error else {"load_error": load_error}

    def load_background_run_claim_report(self, thread_id: str) -> tuple[dict[str, Any], dict[str, Any] | None]:
        self._require_thread(str(thread_id or ""))
        return _read_claim_report(self._background_claim_path(str(thread_id or "")))

    def renew_background_run_claim(self, request: dict) -> dict[str, Any] | None:
        thread_id = str(request.get("thread_id") or "")
        claim_id = str(request.get("claim_id") or "")
        # 续租只作用于 claim 租约文件（按 thread_id 定位），不需要线程对象。若线程此刻不可读
        # （边缘/竞态：外部清理、长跑中线程消失、极端下 store 根不一致），续租已无意义——返回 None
        # 让后台心跳线程按既有 `renewed is None → 停机` 契约优雅收尾，绝不抛 KeyError 裸崩 daemon 线程。
        if self.load_thread(thread_id) is None:
            # 真机诊断锚点：把此刻解析出的线程文件绝对路径打出来（=该 store 的会话库根），
            # 一旦真出现「续租时线程缺失」，日志即可证实/排除 supervisor 与请求路的 store 根是否不一致。
            _STORE_LOGGER.warning(
                "background claim renew skipped: conversation thread not readable thread=%s path=%s",
                thread_id,
                self._thread_path(thread_id),
            )
            return None
        current = now(request.get("now"))
        lease = _claim_lease_seconds(request.get("lease_seconds"))
        renewed = False

        def updater(data: dict[str, Any]) -> dict[str, Any]:
            nonlocal renewed
            if (
                str(data.get("claim_id") or "") != str(claim_id or "")
                or str(data.get("status") or "") != "running"
            ):
                renewed = False
                return data
            renewed = True
            return {**data, "heartbeat_at": current, "expires_at": current + lease}

        updated = update_json_file_atomic(self._background_claim_path(thread_id), updater)
        return updated if renewed else None

    def finish_background_run(self, request: dict) -> dict[str, Any] | None:
        thread_id = str(request.get("thread_id") or "")
        claim_id = str(request.get("claim_id") or "")
        # 收尾（释放租约/记失败事实）只作用于 claim 文件，不依赖线程仍可读。这条在 `_run_with_heartbeat`
        # 的 finally 里跑：若线程在长跑中变不可读还硬 `_require_thread`，会二次抛 KeyError 盖掉真正的 run
        # 错误、并再次崩后台清理。改为对已存在的 claim 文件收尾；无 claim 文件则无可收尾直接返回 None。
        claim_path = self._background_claim_path(thread_id)
        if not claim_path.exists():
            return None
        current = now(request.get("now"))
        raw_status = request.get("status")
        status = _finish_status(raw_status)
        error = _error_payload(request.get("error"))
        if status == _INVALID_FINISH_STATUS and not error:
            error = {
                "type": "InvalidBackgroundClaimStatus",
                "message": f"unsupported background claim finish status: {raw_status!r}",
            }
        task_id = str(request.get("task_id") or "")
        runtime_facts = request.get("runtime_facts") if isinstance(request.get("runtime_facts"), dict) else {}
        finished = False

        def updater(data: dict[str, Any]) -> dict[str, Any]:
            nonlocal finished
            if str(data.get("claim_id") or "") != str(claim_id or ""):
                finished = False
                return data
            finished = True
            payload = {
                **data,
                "status": status,
                "phase": status,
                "finished_at": current,
                "heartbeat_at": current,
                "takeover": _takeover_payload(status),
            }
            if runtime_facts:
                payload["last_runtime_facts"] = runtime_facts
            if task_id:
                payload["task_id"] = task_id
            if error:
                payload["last_error"] = error
            return payload

        updated = update_json_file_atomic(claim_path, updater)
        return updated if finished else None


# ---------------------------------------------------------------------------
# public conversation store
# ---------------------------------------------------------------------------

class ConversationStore(ConversationClaimStore):
    """Public conversation store — the single entry point for all persistence."""

    def context_bundle(self, thread_id: str, *, recent_limit: int = 20) -> dict[str, Any]:
        bundle, _load_errors = self.context_bundle_report(thread_id, recent_limit=recent_limit)
        return bundle

    def context_bundle_report(
        self, thread_id: str, *, recent_limit: int = 20,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        thread = self._require_thread(thread_id)
        messages, message_errors = self.recent_messages_report(thread_id, limit=recent_limit)
        tasks, task_errors = self.task_links_report(thread_id)
        observations, observation_errors = self.recent_observations_report(thread_id, limit=recent_limit)
        guidance, guidance_errors = self.pending_guidance_report("thread", thread_id, limit=recent_limit)
        goal, goal_error = self.load_goal_report(thread_id)
        return {
            "thread": thread.to_dict(),
            "messages": [item.to_dict() for item in messages],
            "tasks": [item.to_dict() for item in tasks],
            "channel_bindings": [item.to_dict() for item in thread.channel_bindings],
            "observations": [item.to_dict() for item in observations],
            "guidance": [item.to_dict() for item in guidance],
            "goal": goal.to_dict() if goal is not None else None,
        }, [
            *message_errors,
            *task_errors,
            *observation_errors,
            *guidance_errors,
            *([goal_error] if goal_error is not None else []),
        ]
