"""Conversation store for threads, messages, tasks, observations, guidance,
wake signals, progress policies, and background claims.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import threading
import time
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..gateway_parts.daemon_metadata import build_process_identity, process_identity_is_live
from ..gateway_parts.io import (
    locked_file_transition,
    read_json_file,
    read_json_file_report,
    update_json_file_atomic,
    write_json_file_atomic,
)
from ..ingestion.source_binding import merge_audit_source_bindings
from ..io.jsonl import append_jsonl
from ..runtime_errors import DataCorruptionError, runtime_error_report
from ..settings.defaults import default_config_value

_STORE_LOGGER = logging.getLogger("agent.conversation.store")
from .audit_requirements import (
    append_audit_pending_requirement,
    append_audit_user_requirement,
    published_audit_requirement,
)
from .models import (
    THREAD_GOAL_OBJECTIVE_MAX_CHARS,
    THREAD_GOAL_STATUSES,
    THREAD_TASK_LINK_INACTIVE_STATUSES,
    THREAD_TASK_LINK_NON_RESURRECTABLE_STATUSES,
    ChannelBinding,
    ConversationCompactCommit,
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
from .workspace_paths import validated_durable_work_path

# ---------------------------------------------------------------------------
# common helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JsonlReadReport:
    rows: list[dict[str, Any]]
    load_errors: list[dict[str, Any]]


# LLM: This receipt is the durable idempotency and delivery authority for one guidance ingress.
# The JSONL row is only its FIFO projection; pending/reserved/submitted/consumed/rejected lives here.
# 类用途: 记录补充消息从等待、当前尝试预留、提交模型、确认消费到拒绝的唯一持久状态。
@dataclass(frozen=True)
class GuidanceOnceReceipt:
    dedupe_key: str
    input_digest: str
    status: str
    entry: GuidanceEntry
    updated_at: float
    attempt_id: str = ""
    submission_id: str = ""
    submitted_at: float = 0.0
    migration: dict[str, Any] = field(default_factory=dict)

    # LLM: Receipt serialization stays schema-neutral at the store boundary; callers consume
    # typed fields and must not infer delivery state from filenames or prose.
    # 函数用途: 把幂等回执转换成原子 JSON 文件可保存的字典。
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "conversation_guidance_once.v4",
            "dedupe_key": self.dedupe_key,
            "input_digest": self.input_digest,
            "status": self.status,
            "entry": self.entry.to_dict(),
            "updated_at": self.updated_at,
            "attempt_id": self.attempt_id,
            "submission_id": self.submission_id,
            "submitted_at": self.submitted_at,
            **({"migration": dict(self.migration)} if self.migration else {}),
        }

    # LLM: Invalid or partial receipt payloads fail closed so retry never invents acceptance.
    # 函数用途: 从持久化字典恢复幂等回执，并校验关键身份和状态字段。
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GuidanceOnceReceipt:
        entry = data.get("entry")
        schema_version = str(data.get("schema_version") or "").strip()
        status = str(data.get("status") or "").strip().lower()
        migration = data.get("migration")
        migration = dict(migration) if isinstance(migration, dict) else {}
        if schema_version == "conversation_guidance_once.v1":
            legacy_status = status
            status = {
                "pending": "pending",
                "claimed": "submitted",
                # v1 accepted meant only that Gateway had appended the row. It never proved
                # provider consumption, so the explicit migration keeps it unresolved.
                "accepted": "submitted",
                "consumed": "consumed",
                "rejected": "rejected",
            }.get(status, "")
            migration = {
                "from_schema": "conversation_guidance_once.v1",
                "from_status": legacy_status,
                "decision": "legacy_gateway_acceptance_is_not_provider_consumption",
                "migrated_at": time.time(),
            }
        elif schema_version == "conversation_guidance_once.v2":
            legacy_status = status
            status = {
                "pending": "pending",
                # v2 could not prove whether provider submission had begun, so
                # migration chooses the duplicate-safe unresolved side.
                "claimed": "submitted",
                "consumed": "consumed",
                "rejected": "rejected",
            }.get(status, "")
            migration = {
                "from_schema": "conversation_guidance_once.v2",
                "from_status": legacy_status,
                "decision": "legacy_claim_is_provider_submission_unknown",
                "migrated_at": time.time(),
            }
        elif schema_version == "conversation_guidance_once.v3":
            legacy_status = status
            migration = {
                "from_schema": "conversation_guidance_once.v3",
                "from_status": legacy_status,
                "decision": "legacy_submission_has_no_atomic_batch_identity",
                "migrated_at": time.time(),
            }
        elif schema_version != "conversation_guidance_once.v4":
            status = ""
        if not isinstance(entry, dict) or status not in {
            "pending",
            "reserved",
            "submitted",
            "consumed",
            "rejected",
        }:
            raise DataCorruptionError("conversation guidance receipt is invalid")
        receipt = cls(
            dedupe_key=str(data.get("dedupe_key") or "").strip(),
            input_digest=str(data.get("input_digest") or "").strip(),
            status=status,
            entry=GuidanceEntry.from_dict(entry),
            updated_at=float(data.get("updated_at") or 0.0),
            attempt_id=str(data.get("attempt_id") or "").strip(),
            submission_id=(
                str(data.get("submission_id") or "").strip()
                or ("legacy-unknown" if status == "submitted" else "")
            ),
            submitted_at=float(data.get("submitted_at") or 0.0),
            migration=migration,
        )
        if not receipt.dedupe_key or not receipt.input_digest or not receipt.entry.guidance_id:
            raise DataCorruptionError("conversation guidance receipt identity is invalid")
        _validate_guidance_once_receipt(receipt, legacy=bool(migration))
        return receipt


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


# LLM: Store commands may receive lists or tuples; normalize them before comparing exact
# thread scope so serialization shape cannot create a false lineage change.
# 函数用途: 将会话运行根目录整理成有序、去空、去重的不可变列表。
def normalized_runtime_workspace_roots(value: object) -> tuple[str, ...]:
    items = value if isinstance(value, (list, tuple)) else ()
    return tuple(
        dict.fromkeys(str(item).strip() for item in items if str(item or "").strip())
    )


def _named_work_name_matches(link: ThreadTaskLink, work_kind: str, work_name: str) -> bool:
    current = str(link.work_name or "")
    expected = str(work_name or "")
    return (
        current == expected if work_kind == "audit" else current.casefold() == expected.casefold()
    )


def wake_urgency(value: str) -> str:
    return "urgent" if str(value or "").strip().lower() == "urgent" else "normal"


def with_handled_at(event: ObservationEvent, handled: dict[str, float]) -> ObservationEvent:
    handled_at = float(handled.get(event.observation_id) or event.handled_at or 0.0)
    return event if handled_at == event.handled_at else replace(event, handled_at=handled_at)


def wake_evidence_refs(
    observation: ObservationEvent | None, explicit_refs: object
) -> tuple[str, ...]:
    if explicit_refs is not None:
        refs = explicit_refs
    elif observation is not None:
        refs = observation.evidence_refs
    else:
        refs = ()
    return (
        tuple(str(item) for item in refs if str(item or "").strip())
        if isinstance(refs, (list, tuple))
        else ()
    )


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
        self.guidance_dedupe_dir = self.root / "guidance_dedupe"
        self.guidance_turn_index_dir = self.root / "guidance_turn_index"
        self.guidance_input_index_dir = self.root / "guidance_input_index"
        self.guidance_submission_batches_dir = self.root / "guidance_submission_batches"
        self.guidance_ack_batches_dir = self.root / "guidance_ack_batches"
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
            self.guidance_dedupe_dir,
            self.guidance_turn_index_dir,
            self.guidance_input_index_dir,
            self.guidance_submission_batches_dir,
            self.guidance_ack_batches_dir,
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
        return (
            self.guidance_dir / f"{safe_file_stem(target_type)}.{safe_file_stem(target_id)}.jsonl"
        )

    # LLM: Dedupe keys may contain channel and thread identifiers, so filenames use a full
    # cryptographic digest while the original key remains inside the validated receipt.
    # 函数用途: 返回一条 guidance 幂等回执的唯一安全文件路径。
    def _guidance_dedupe_path(self, dedupe_key: str) -> Path:
        digest = hashlib.sha256(str(dedupe_key).encode("utf-8")).hexdigest()
        return self.guidance_dedupe_dir / f"{digest}.json"

    # LLM: The per-turn folder is a bounded lookup projection, never delivery authority. Receipt
    # state remains canonical and every ref stores the original dedupe key for validation.
    # 函数用途: 返回精确回合的补充消息索引目录，避免结束时扫描所有历史回执。
    def _guidance_turn_index_path(self, expected_turn_id: str, dedupe_key: str) -> Path:
        turn_digest = hashlib.sha256(str(expected_turn_id).encode("utf-8")).hexdigest()
        receipt_digest = hashlib.sha256(str(dedupe_key).encode("utf-8")).hexdigest()
        return self.guidance_turn_index_dir / turn_digest / f"{receipt_digest}.json"

    # LLM: Gateway ingress ids are opaque and owner-scoped; only their digest appears in a path.
    # 函数用途: 返回普通消息回执到 guidance 回执的反向索引路径，供崩溃恢复找回半完成接入。
    def _guidance_input_index_path(self, gateway_input_request_id: str) -> Path:
        digest = hashlib.sha256(gateway_input_request_id.encode("utf-8")).hexdigest()
        return self.guidance_input_index_dir / f"{digest}.json"

    # LLM: One provider-ack batch is immutable and addressed by exact turn plus its guidance ids.
    # 函数用途: 返回模型一次确认接收整批补充消息的原子提交文件位置。
    def _guidance_ack_batch_path(
        self,
        expected_turn_id: str,
        guidance_ids: tuple[str, ...],
    ) -> Path:
        turn_digest = hashlib.sha256(expected_turn_id.encode("utf-8")).hexdigest()
        batch_source = json.dumps(sorted(guidance_ids), ensure_ascii=False, separators=(",", ":"))
        batch_digest = hashlib.sha256(batch_source.encode("utf-8")).hexdigest()
        return self.guidance_ack_batches_dir / turn_digest / f"{batch_digest}.json"

    # LLM: A provider call id is host-generated and immutable. Hashing it under the exact turn
    # yields one atomic provider-boundary authority without exposing identifiers in filenames.
    # 函数用途: 返回一次模型物理调用所对应的补充消息提交批次文件。
    def _guidance_submission_batch_path(
        self,
        expected_turn_id: str,
        provider_call_id: str,
    ) -> Path:
        turn_digest = hashlib.sha256(expected_turn_id.encode("utf-8")).hexdigest()
        batch_digest = hashlib.sha256(provider_call_id.encode("utf-8")).hexdigest()
        return self.guidance_submission_batches_dir / turn_digest / f"{batch_digest}.json"

    # LLM: One sanitized path per thread is the sole durable goal record authority.
    # 函数用途: 返回当前 conversation thread 唯一的持续目标文件路径。
    def _goal_path(self, thread_id: str) -> Path:
        return self.goals_dir / f"{safe_file_stem(thread_id)}.json"

    def _wake_signal_path(self, signal: WakeSignal) -> Path:
        return self.wake_queue_dir / wake_urgency(signal.urgency) / f"{signal.wake_signal_id}.json"

    def _wake_dedupe_path(self, thread_id: str, dedupe_key: str) -> Path:
        return (
            self.wake_dedupe_dir / f"{safe_file_stem(thread_id)}.{safe_file_stem(dedupe_key)}.json"
        )


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


def _replace_binding(
    thread: ConversationThread, binding: ChannelBinding
) -> tuple[ChannelBinding, ...]:
    key = _binding_key(binding.channel, binding.channel_conversation_id, binding.channel_user_id)
    old = (
        item
        for item in thread.channel_bindings
        if _binding_key(item.channel, item.channel_conversation_id, item.channel_user_id) != key
    )
    return (*old, binding)


def _read_json_object_report(
    path: Path, *, context: str
) -> tuple[dict[str, Any], dict[str, Any] | None]:
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
            latest, latest_error = self.latest_thread_for_user_report(
                request.get("canonical_user_id", "")
            )
            if latest_error is not None:
                raise DataCorruptionError(str(latest_error))
        if latest is not None:
            return self._bind_existing(latest.thread_id, request)
        return self._create_thread(request)

    # LLM: Keep this adapter small; exact-id creation and collision checks live in
    # agent_thread_store so the channel-bound store does not absorb agent runtime policy.
    # 函数用途: 为一个确定的子代理运行创建或校验独立会话线程，不把它绑定成用户聊天会话。
    def ensure_agent_thread(self, request: dict) -> ConversationThread:
        from .agent_thread_store import ensure_agent_thread_record

        return ensure_agent_thread_record(self, request)

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
        thread_id = str(
            bindings.get(_binding_key(channel, channel_conversation_id, channel_user_id)) or ""
        )
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

    # LLM: Channel binding updates must merge into the latest durable thread atomically so a
    # delayed adapter request cannot revert compact, task indexes, or the sticky workspace.
    # 函数用途: 为同一会话增加或刷新通道绑定，同时保留其他并发更新的会话状态。
    def bind_channel(self, request: dict) -> ConversationThread:
        thread_id = str(request.get("thread_id") or "")
        self._require_thread(thread_id)
        current = now(request.get("now"))
        binding = _channel_binding(thread_id, current, request)
        requested_cwd = str(request.get("cwd") or "").strip()
        raw_runtime_roots = request.get("runtime_workspace_roots")
        requested_runtime_roots = tuple(
            str(item)
            for item in (raw_runtime_roots if isinstance(raw_runtime_roots, (list, tuple)) else [])
            if str(item or "").strip()
        )
        updated = self._update_thread_atomic(
            thread_id,
            lambda latest: replace(
                latest,
                canonical_user_id=(request.get("canonical_user_id") or latest.canonical_user_id),
                owner_id=str(request.get("owner_id") or latest.owner_id or ""),
                owner_home=str(request.get("owner_home") or latest.owner_home or ""),
                channel_bindings=_replace_binding(latest, binding),
                cwd=requested_cwd or latest.cwd,
                runtime_workspace_roots=(
                    requested_runtime_roots or latest.runtime_workspace_roots
                ),
                updated_at=max(latest.updated_at, current),
            ),
        )
        self._write_binding_indexes(binding)
        return updated

    def list_threads(self, *, limit: int = 100) -> list[ConversationThread]:
        threads, _load_errors = self.list_threads_report(limit=limit)
        return threads

    def list_threads_report(
        self, *, limit: int = 100
    ) -> tuple[list[ConversationThread], list[dict[str, Any]]]:
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

    # LLM: A summary owns only summary and activity time; it must never replace a stale full thread.
    # 函数用途: 原子更新会话摘要，不覆盖同时发生的任务目录、压缩游标或通道变化。
    def update_summary(
        self, thread_id: str, summary: str, *, now: float | None = None
    ) -> ConversationThread:
        current = now if now is not None else time.time()
        return self._update_thread_atomic(
            thread_id,
            lambda latest: replace(
                latest,
                summary=summary,
                updated_at=max(latest.updated_at, current),
            ),
        )

    def update_compact_state(
        self,
        thread_id: str,
        *,
        commit: ConversationCompactCommit,
        expected_generation: int,
        now: float | None = None,
    ) -> ConversationThread:
        """Atomically advance one validated summary/checkpoint without touching raw messages."""
        current = now if now is not None else time.time()

        def apply(thread: ConversationThread) -> ConversationThread:
            if thread.compact_generation != expected_generation:
                raise RuntimeError(
                    "conversation compact generation changed while summary was being prepared"
                )
            return replace(
                thread,
                summary=str(commit.summary).strip(),
                compact_operation_evidence=dict(commit.operation_evidence),
                compacted_through_message_id=str(commit.compacted_through_message_id),
                compacted_through_byte_offset=max(
                    0,
                    int(commit.compacted_through_byte_offset),
                ),
                compact_generation=thread.compact_generation + 1,
                compact_updated_at=current,
                compact_source_messages=max(0, int(commit.source_messages)),
                compact_checkpoint_id=str(commit.checkpoint_id),
                compact_consecutive_failures=0,
                compact_failure_updated_at=0.0,
                compact_failure_code="",
                updated_at=current,
            )

        return self._update_thread_atomic(thread_id, apply)

    # LLM: A failed summary candidate may update only the compact failure circuit; it must never
    # move the generation, cursor, summary, checkpoint pointer, or raw transcript.
    # 函数用途: 记录一次会话压缩失败，供跨请求熔断使用，但不把失败候选当成已经提交。
    def record_compact_failure(
        self,
        thread_id: str,
        *,
        failure_code: str,
        expected_generation: int,
        now: float | None = None,
    ) -> ConversationThread:
        current = now if now is not None else time.time()

        def apply(thread: ConversationThread) -> ConversationThread:
            if thread.compact_generation != expected_generation:
                return thread
            return replace(
                thread,
                compact_consecutive_failures=thread.compact_consecutive_failures + 1,
                compact_failure_updated_at=current,
                compact_failure_code=str(failure_code or "COMPACT_FAILED"),
                updated_at=current,
            )

        return self._update_thread_atomic(thread_id, apply)

    # LLM: Verbose is a thread-local system control and updates only its own field atomically.
    # 函数用途: 修改当前会话的详细输出级别，不让旧会话快照覆盖其他状态。
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
        current = now if now is not None else time.time()
        return self._update_thread_atomic(
            thread_id,
            lambda latest: replace(
                latest,
                verbose_level=normalized,
                updated_at=max(latest.updated_at, current),
            ),
        )

    def load_thread(self, thread_id: str) -> ConversationThread | None:
        thread, _load_error = self.load_thread_report(thread_id)
        return thread

    def load_thread_report(
        self, thread_id: str
    ) -> tuple[ConversationThread | None, dict[str, Any] | None]:
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

    # LLM: Compact CAS checks and writes must share one cross-process file lock; checking a
    # previously loaded dataclass and locking only the final replace permits two generations.
    # 函数用途: 在同一个文件锁里读取、校验和写回 thread，供 compact 成功/失败状态做真实原子迁移。
    def _update_thread_atomic(
        self,
        thread_id: str,
        updater: Callable[[ConversationThread], ConversationThread],
    ) -> ConversationThread:
        path = self._thread_path(thread_id)

        def apply(payload: dict) -> dict:
            thread = ConversationThread.from_dict(payload)
            if not thread.thread_id:
                raise DataCorruptionError(f"conversation thread is unreadable: {thread_id}")
            if thread.thread_id != thread_id:
                raise DataCorruptionError(f"conversation thread identity is invalid: {thread_id}")
            updated = updater(thread)
            if updated.thread_id != thread_id:
                raise DataCorruptionError(
                    f"conversation thread updater changed identity: {thread_id}"
                )
            return updated.to_dict()

        payload = update_json_file_atomic(path, apply, require_existing=True)
        return ConversationThread.from_dict(payload)

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
        return self.bind_channel(
            {
                "thread_id": thread_id,
                "canonical_user_id": kwargs.get("canonical_user_id", ""),
                "owner_id": kwargs.get("owner_id", ""),
                "owner_home": kwargs.get("owner_home", ""),
                "channel": kwargs.get("channel", ""),
                "channel_conversation_id": kwargs.get("channel_conversation_id", ""),
                "channel_user_id": kwargs.get("channel_user_id", ""),
                "cwd": kwargs.get("cwd", ""),
                "runtime_workspace_roots": kwargs.get("runtime_workspace_roots", ()),
                "now": kwargs.get("now"),
            }
        )

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
        key = _binding_key(
            binding.channel, binding.channel_conversation_id, binding.channel_user_id
        )
        update_json_file_atomic(self.bindings_path, lambda data: {**data, key: binding.thread_id})
        update_json_file_atomic(
            self.user_latest_path,
            lambda data: {**data, binding.canonical_user_id: binding.thread_id},
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


def _message_entries(
    rows: list[dict[str, Any]],
) -> tuple[list[MessageLogEntry], list[dict[str, Any]]]:
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


# LLM: Exact Memory evidence lookup must stop at the matching row while preserving JSONL corruption errors.
# 函数用途: 流式扫描单个 transcript 文件并返回目标消息，避免加载完整会话或在公开方法中堆叠嵌套分支。
def _message_entry_by_id_in_path(
    path: Path,
    target: str,
) -> tuple[MessageLogEntry | None, list[dict[str, Any]]]:
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            row, error = _json_row(
                line,
                context="conversation.messages.by_id",
                path=path,
                line_number=line_number,
            )
            if error is not None:
                return None, [error]
            if row is None or str(row.get("message_id") or "") != target:
                continue
            try:
                return MessageLogEntry.from_dict(row), []
            except Exception as exc:
                return None, [
                    _jsonl_error(
                        exc,
                        "conversation.messages.by_id",
                        path=path,
                        line_number=line_number,
                    )
                ]
    return None, []


class ConversationMessageStore(ConversationThreadStore):
    # LLM: Transcript append is append-only; its activity projection must merge into the latest
    # thread after the ledger write rather than writing the earlier loaded thread snapshot.
    # 函数用途: 追加一条对话记录，并只刷新会话活动时间，避免迟到消息把新任务目录改回旧目录。
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
        self._update_thread_atomic(
            thread.thread_id,
            lambda latest: replace(
                latest,
                updated_at=max(latest.updated_at, entry.created_at),
            ),
        )
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
                if existing.role != str(request.get("role") or "") or existing.content != str(
                    request.get("content") or ""
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

    # LLM: Memory Curator 只能从精确 message_id 后顺序读取有界批次；不得每次全量重放 transcript。
    # 函数用途: 从 append-only ConversationStore 游标后流式读取至多 limit 条消息和结构化错误。
    def messages_after_report(
        self,
        thread_id: str,
        *,
        after_message_id: str = "",
        limit: int = 100,
    ) -> tuple[list[MessageLogEntry], list[dict[str, Any]]]:
        bounded_limit = max(1, min(1_000, int(limit or 100)))
        path = self._message_path(thread_id)
        if not path.exists():
            return [], []
        try:
            offset = (
                self.message_byte_offset_after(thread_id, after_message_id)
                if str(after_message_id or "").strip()
                else 0
            )
            rows: list[dict[str, Any]] = []
            errors: list[dict[str, Any]] = []
            with path.open("rb") as handle:
                handle.seek(offset)
                while len(rows) < bounded_limit and (line := handle.readline()):
                    if not line.strip():
                        continue
                    try:
                        text = line.decode("utf-8")
                    except UnicodeDecodeError as exc:
                        errors.append(
                            _jsonl_error(exc, "conversation.messages.after", path=path)
                        )
                        break
                    row, error = _json_row(
                        text,
                        context="conversation.messages.after",
                        path=path,
                        line_number=0,
                    )
                    if error is not None:
                        errors.append(error)
                        break
                    if row is not None:
                        rows.append(row)
            entries, parse_errors = _message_entries(rows)
            return entries, [*errors, *parse_errors]
        except Exception as exc:
            return [], [_jsonl_error(exc, "conversation.messages.after", path=path)]

    # LLM: Persona/Memory evidence verification must resolve one exact message_id without loading a whole transcript.
    # 函数用途: 流式查找一条 ConversationStore 消息并返回结构化读取错误。
    def message_by_id_report(
        self,
        thread_id: str,
        message_id: str,
    ) -> tuple[MessageLogEntry | None, list[dict[str, Any]]]:
        path = self._message_path(thread_id)
        target = str(message_id or "").strip()
        if not path.exists() or not target:
            return None, []
        try:
            return _message_entry_by_id_in_path(path, target)
        except Exception as exc:
            return None, [_jsonl_error(exc, "conversation.messages.by_id", path=path)]

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


def _thread_with_task(
    thread: ConversationThread, task_id: str, updated_at: float
) -> ConversationThread:
    task_ids = tuple(dict.fromkeys((*thread.task_ids, task_id)))
    active_task_ids = tuple(dict.fromkeys((*thread.active_task_ids, task_id)))
    return replace(
        thread,
        task_ids=task_ids,
        active_task_ids=active_task_ids,
        updated_at=updated_at,
    )


def _thread_without_task(
    thread: ConversationThread, task_id: str, updated_at: float
) -> ConversationThread:
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


# LLM: conversation task link 是每次执行的生命周期权威；work/state.json 只是可复用
# task_path 当前执行的 owner-local 投影。旧 link 与当前投影身份不同是正常历史关系，
# 不能覆盖当前执行，也不应按数据损坏重复报警。
# 函数用途: 在当前任务链接变更后同步工作区状态，避免停止或完成后目录仍永久显示 RUNNING。
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
        task_root = validated_durable_work_path(
            owner_path,
            task_path,
            str(link.work_kind or ""),
        )
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
        _STORE_LOGGER.warning(
            "task workspace state path unavailable(task=%s): %s", link.task_id, exc
        )
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
            return data
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
    requested_duration = (
        max(1, int(request["duration_seconds"]))
        if request.get("duration_seconds") is not None
        else None
    )
    requested_expires_at = (
        float(request["expires_at"])
        if request.get("expires_at") is not None
        else current + requested_duration
        if requested_duration is not None
        else None
    )
    if not data:
        if path_exists:
            raise DataCorruptionError(f"conversation task link is unreadable: {task_id}")
        return _new_task_link(
            request,
            thread_id=thread_id,
            task_id=task_id,
            current=current,
            requested_duration=requested_duration,
            requested_expires_at=requested_expires_at,
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
    # (bind_current_conversation_workspace), where the caller names the exact task.
    existing_status = str(existing.status or "").strip()
    merged_status = (
        existing_status
        if existing_status.lower() in THREAD_TASK_LINK_NON_RESURRECTABLE_STATUSES
        else requested_status or existing_status
    )
    return _updated_task_link(
        existing,
        request,
        merged_status=merged_status,
        current=current,
        requested_duration=requested_duration,
        requested_expires_at=requested_expires_at,
    )


def _new_task_link(
    request: dict,
    *,
    thread_id: str,
    task_id: str,
    current: float,
    requested_duration: int | None,
    requested_expires_at: float | None,
) -> ThreadTaskLink:
    return ThreadTaskLink(
        thread_id=thread_id,
        task_id=task_id,
        goal=str(request.get("goal") or ""),
        status=str(request.get("status") or "active"),
        created_at=current,
        task_path=str(request.get("task_path") or ""),
        work_kind=str(request.get("work_kind") or ""),
        work_name=str(request.get("work_name") or ""),
        duration_seconds=requested_duration,
        expires_at=requested_expires_at,
        cancellation_scope=str(request.get("cancellation_scope") or "foreground"),
        context_anchor_message_id=str(request.get("context_anchor_message_id") or ""),
        pending_prompt=str(request.get("pending_prompt") or ""),
        pending_updated_at=float(request.get("pending_updated_at") or 0.0),
        pending_prepare_request_id=str(request.get("pending_prepare_request_id") or ""),
        effective_user_prompt=str(request.get("effective_user_prompt") or ""),
        effective_revision=max(0, int(request.get("effective_revision") or 0)),
        effective_updated_at=float(request.get("effective_updated_at") or 0.0),
        effective_prepare_request_id=str(request.get("effective_prepare_request_id") or ""),
        effective_evidence_refs=tuple(
            str(item) for item in (request.get("effective_evidence_refs") or []) if str(item)
        ),
        effective_source_bindings=tuple(
            dict(item)
            for item in (request.get("effective_source_bindings") or [])
            if isinstance(item, dict)
        ),
        run_epoch=max(0, int(request.get("run_epoch") or 0)),
        run_prompt=str(request.get("run_prompt") or ""),
    )


def _updated_task_link(
    existing: ThreadTaskLink,
    request: dict,
    *,
    merged_status: str,
    current: float,
    requested_duration: int | None,
    requested_expires_at: float | None,
) -> ThreadTaskLink:
    return replace(
        existing,
        goal=existing.goal or str(request.get("goal") or ""),
        status=merged_status,
        created_at=existing.created_at or current,
        task_path=existing.task_path or str(request.get("task_path") or ""),
        work_kind=existing.work_kind or str(request.get("work_kind") or ""),
        work_name=existing.work_name or str(request.get("work_name") or ""),
        duration_seconds=existing.duration_seconds
        if existing.duration_seconds is not None
        else requested_duration,
        expires_at=existing.expires_at if existing.expires_at is not None else requested_expires_at,
        cancellation_scope=(
            existing.cancellation_scope
            if existing.cancellation_scope != "foreground"
            else str(request.get("cancellation_scope") or existing.cancellation_scope)
        ),
        context_anchor_message_id=existing.context_anchor_message_id
        or str(request.get("context_anchor_message_id") or ""),
        pending_prompt=existing.pending_prompt or str(request.get("pending_prompt") or ""),
        pending_updated_at=existing.pending_updated_at
        or float(request.get("pending_updated_at") or 0.0),
        pending_prepare_request_id=existing.pending_prepare_request_id
        or str(request.get("pending_prepare_request_id") or ""),
        effective_user_prompt=existing.effective_user_prompt
        or str(request.get("effective_user_prompt") or ""),
        effective_revision=max(
            existing.effective_revision, int(request.get("effective_revision") or 0)
        ),
        effective_updated_at=existing.effective_updated_at
        or float(request.get("effective_updated_at") or 0.0),
        effective_prepare_request_id=existing.effective_prepare_request_id
        or str(request.get("effective_prepare_request_id") or ""),
        effective_evidence_refs=existing.effective_evidence_refs
        or tuple(str(item) for item in (request.get("effective_evidence_refs") or []) if str(item)),
        effective_source_bindings=existing.effective_source_bindings
        or tuple(
            dict(item)
            for item in (request.get("effective_source_bindings") or [])
            if isinstance(item, dict)
        ),
        run_epoch=max(existing.run_epoch, int(request.get("run_epoch") or 0)),
        run_prompt=existing.run_prompt or str(request.get("run_prompt") or ""),
    )


class ConversationTaskStore(ConversationMessageStore):
    def task_transition_guard(self, task_id: str):
        """Return the cross-process lock shared by steer, stop, and completion."""
        normalized = safe_file_stem(str(task_id or ""))
        if not normalized:
            raise ValueError("task_id is required")
        return locked_file_transition(self.tasks_dir / f".{normalized}.transition")

    def named_work_transition_guard(
        self,
        thread_id: str,
        work_kind: str,
        work_name: str,
    ):
        """Serialize one exact named-work identity across task ids.

        A pre-upgrade conversation may contain more than one terminal link with
        the same Audit name.  Reopening one of those identities must share the
        same lock as first creation, otherwise two concurrent starts could each
        resurrect a different historical task.
        """

        selected_thread = safe_file_stem(str(thread_id or ""))
        selected_kind = safe_file_stem(str(work_kind or "").strip().lower())
        selected_name = str(work_name or "").strip()
        if not selected_thread or not selected_kind or not selected_name:
            raise ValueError("thread_id, work_kind and work_name are required")
        return locked_file_transition(
            self.tasks_dir
            / (
                f".named.{selected_thread}.{selected_kind}."
                f"{hashlib.sha256(selected_name.encode('utf-8')).hexdigest()[:20]}"
            )
        )

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
        work_kind = str(request.get("work_kind") or "").strip().lower()
        work_name = str(request.get("work_name") or "").strip()
        cancellation_scope = str(request.get("cancellation_scope") or "foreground").strip().lower()
        request = dict(request)
        if (
            work_kind in {"audit", "goal"}
            and work_name
            and cancellation_scope == "detached"
            and "context_anchor_message_id" not in request
        ):
            messages, message_errors = self.recent_messages_report(
                thread.thread_id,
                limit=1,
            )
            if message_errors:
                raise DataCorruptionError(
                    "conversation transcript is unavailable for detached task binding"
                )
            request["context_anchor_message_id"] = messages[-1].message_id if messages else ""
        guard = (
            self.named_work_transition_guard(thread.thread_id, work_kind, work_name)
            if work_kind in {"audit", "goal"} and work_name
            else nullcontext()
        )
        with guard:
            if work_kind and work_name:
                links, errors = self.task_links_report(thread.thread_id)
                if errors:
                    raise DataCorruptionError("conversation task links are unavailable")
                duplicate = next(
                    (
                        link
                        for link in links
                        if link.task_id != task_id
                        and str(link.work_kind or "").strip().lower() == work_kind
                        and _named_work_name_matches(link, work_kind, work_name)
                        and str(link.status or "").strip().lower()
                        not in THREAD_TASK_LINK_INACTIVE_STATUSES
                    ),
                    None,
                )
                if duplicate is not None:
                    raise ValueError(f"an active {work_kind} named {work_name!r} already exists")
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
            indexed_thread = self._update_thread_task_index(
                thread.thread_id,
                task_id,
                link.status,
                current,
            )
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
                str(
                    (error or {}).get("message")
                    or f"conversation task link is unavailable: {task_id}"
                )
            )
        if link.thread_id != thread.thread_id:
            raise ValueError(f"task {task_id} is not bound to conversation thread {thread_id}")
        current = now(request.get("now"))

        def updater(data: dict[str, Any]) -> dict[str, Any]:
            if not data:
                raise DataCorruptionError(f"conversation thread is unreadable: {thread_id}")
            latest = ConversationThread.from_dict(data)
            if latest.thread_id != thread_id:
                raise DataCorruptionError(f"conversation thread identity is invalid: {thread_id}")
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

    def load_task_link(self, task_id: str) -> ThreadTaskLink | None:
        link, error = self.load_task_link_report(task_id)
        if error is not None:
            raise DataCorruptionError(
                str(error.get("message") or "conversation task link read failed")
            )
        return link

    def load_task_link_report(
        self,
        task_id: str,
    ) -> tuple[ThreadTaskLink | None, dict[str, Any] | None]:
        selected = str(task_id or "").strip()
        if not selected:
            return None, None
        path = self._task_path(selected)
        if not path.exists():
            return None, None
        return _read_task_link(path, selected)

    def task_links_report(
        self, thread_id: str
    ) -> tuple[list[ThreadTaskLink], list[dict[str, Any]]]:
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
            raise DataCorruptionError(
                str(load_error.get("message") or "conversation task link read failed")
            )
        return thread

    def thread_for_task_report(
        self, task_id: str
    ) -> tuple[ConversationThread | None, dict[str, Any] | None]:
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
            if (
                expected_status
                and str(link_current.status or "").strip().lower() != expected_status
            ):
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
        indexed_thread = self._update_thread_task_index(
            link.thread_id, task_id, link.status, current
        )
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

    def record_pending_audit_prompt(self, request: dict) -> ThreadTaskLink | None:
        """Append one exact pending turn for one exact nonterminal Audit."""
        return _record_pending_audit_prompt(self, request)

    def activate_audit(self, request: dict) -> ThreadTaskLink | None:
        """Atomically start one prepared Audit without replacing published config."""
        return _activate_audit(self, request)

    def reopen_audit_prepare(self, request: dict) -> ThreadTaskLink | None:
        """Re-enter preparation on one terminal named Audit without losing config."""
        return _reopen_audit_prepare(self, request)

    def reactivate_audit(self, request: dict) -> ThreadTaskLink | None:
        """Start a fresh run epoch on one terminal named Audit.

        This is the deliberate lifecycle counterpart to ``bind_task``'s
        non-resurrection rule. It preserves the exact published configuration
        and workspace while minting a new epoch for source/runtime isolation.
        """

        return _reactivate_audit(self, request)

    def _active_named_work_conflict(
        self,
        selected: ThreadTaskLink,
        task_id: str,
    ) -> bool:
        """Return whether another nonterminal link owns the same exact name."""

        return _active_named_work_conflict(self, selected, task_id)

    def publish_audit_effective_prompt(self, request: dict) -> ThreadTaskLink | None:
        """Publish one exact Audit revision while preserving the user's words."""
        return _publish_audit_effective_prompt(self, request)


def _record_pending_audit_prompt(store: object, request: dict) -> ThreadTaskLink | None:
    task_id = str(request.get("task_id") or "").strip()
    prompt = str(request.get("prompt") or "").strip()
    prepare_request_id = str(request.get("prepare_request_id") or "").strip()
    path = store._task_path(task_id)
    if not task_id or not prompt or not prepare_request_id or not path.exists():
        return None
    current_time = now(request.get("now"))
    updated = False

    def updater(data: dict[str, Any]) -> dict[str, Any]:
        nonlocal updated
        link = ThreadTaskLink.from_dict(data)
        if (
            link.task_id != task_id
            or str(link.work_kind or "").strip().lower() != "audit"
            or str(link.status or "").strip().lower() in THREAD_TASK_LINK_INACTIVE_STATUSES
        ):
            return data
        updated = True
        return replace(
            link,
            pending_prompt=append_audit_pending_requirement(link, prompt),
            pending_updated_at=current_time,
            pending_prepare_request_id=prepare_request_id,
        ).to_dict()

    payload = update_json_file_atomic(path, updater, require_existing=True)
    return ThreadTaskLink.from_dict(payload) if updated else None


def _activate_audit(store: object, request: dict) -> ThreadTaskLink | None:
    task_id = str(request.get("task_id") or "").strip()
    fallback_goal = str(request.get("goal") or "").strip()
    path = store._task_path(task_id)
    if not task_id or not fallback_goal or not path.exists():
        return None
    current_time = now(request.get("now"))
    duration = max(1, int(request.get("duration_seconds") or 0))
    updated = False

    def updater(data: dict[str, Any]) -> dict[str, Any]:
        nonlocal updated
        link = ThreadTaskLink.from_dict(data)
        if (
            link.task_id != task_id
            or str(link.work_kind or "").strip().lower() != "audit"
            or str(link.status or "").strip().lower() != "preparing"
        ):
            return data
        updated = True
        return replace(
            link,
            goal=link.goal or fallback_goal,
            run_prompt=fallback_goal,
            status="active",
            duration_seconds=duration,
            expires_at=current_time + duration,
            cancellation_scope="detached",
            effective_revision=max(1, link.effective_revision),
            effective_updated_at=link.effective_updated_at or current_time,
            run_epoch=max(1, link.run_epoch + 1),
        ).to_dict()

    payload = update_json_file_atomic(path, updater, require_existing=True)
    return _indexed_audit_link(store, task_id, current_time, payload, updated)


def _reopen_audit_prepare(store: object, request: dict) -> ThreadTaskLink | None:
    task_id = str(request.get("task_id") or "").strip()
    prompt = str(request.get("prompt") or "").strip()
    prepare_request_id = str(request.get("prepare_request_id") or "").strip()
    path = store._task_path(task_id)
    if not task_id or not prompt or not prepare_request_id or not path.exists():
        return None
    current_time = now(request.get("now"))
    initial = store.load_task_link(task_id)
    if not _reopenable_named_audit(initial):
        return None
    updated = False

    def updater(data: dict[str, Any]) -> dict[str, Any]:
        nonlocal updated
        link = ThreadTaskLink.from_dict(data)
        if not _terminal_named_audit(link, task_id):
            return data
        updated = True
        return replace(
            link,
            status="preparing",
            run_prompt="",
            duration_seconds=None,
            expires_at=None,
            cancellation_scope="foreground",
            pending_prompt=append_audit_pending_requirement(link, prompt),
            pending_updated_at=current_time,
            pending_prepare_request_id=prepare_request_id,
        ).to_dict()

    with store.named_work_transition_guard(initial.thread_id, "audit", initial.work_name):
        if store._active_named_work_conflict(initial, task_id):
            return None
        with store.task_transition_guard(task_id):
            payload = update_json_file_atomic(path, updater, require_existing=True)
    return _indexed_audit_link(store, task_id, current_time, payload, updated)


def _reactivate_audit(store: object, request: dict) -> ThreadTaskLink | None:
    task_id = str(request.get("task_id") or "").strip()
    fallback_goal = str(request.get("goal") or "").strip()
    path = store._task_path(task_id)
    if not task_id or not fallback_goal or not path.exists():
        return None
    current_time = now(request.get("now"))
    initial = store.load_task_link(task_id)
    if not _reopenable_named_audit(initial):
        return None
    duration = max(1, int(request.get("duration_seconds") or 0))
    updated = False

    def updater(data: dict[str, Any]) -> dict[str, Any]:
        nonlocal updated
        link = ThreadTaskLink.from_dict(data)
        if not _terminal_named_audit(link, task_id):
            return data
        updated = True
        return replace(
            link,
            goal=link.goal or fallback_goal,
            run_prompt=fallback_goal,
            status="active",
            duration_seconds=duration,
            expires_at=current_time + duration,
            cancellation_scope="detached",
            pending_prompt="",
            pending_updated_at=0.0,
            pending_prepare_request_id="",
            effective_revision=max(1, link.effective_revision),
            effective_updated_at=link.effective_updated_at or current_time,
            run_epoch=max(1, link.run_epoch + 1),
        ).to_dict()

    with store.named_work_transition_guard(initial.thread_id, "audit", initial.work_name):
        if store._active_named_work_conflict(initial, task_id):
            return None
        with store.task_transition_guard(task_id):
            payload = update_json_file_atomic(path, updater, require_existing=True)
    return _indexed_audit_link(store, task_id, current_time, payload, updated)


def _active_named_work_conflict(
    store: object,
    selected: ThreadTaskLink,
    task_id: str,
) -> bool:
    links, errors = store.task_links_report(selected.thread_id)
    if errors:
        raise DataCorruptionError("conversation task links are unavailable")
    return any(
        link.task_id != task_id
        and str(link.work_kind or "").strip().lower() == "audit"
        and _named_work_name_matches(link, "audit", selected.work_name)
        and str(link.status or "").strip().lower() not in THREAD_TASK_LINK_INACTIVE_STATUSES
        for link in links
    )


def _publish_audit_effective_prompt(
    store: object,
    request: dict,
) -> ThreadTaskLink | None:
    task_id = str(request.get("task_id") or "").strip()
    prompt = str(request.get("prompt") or "").strip()
    prepare_request_id = str(request.get("prepare_request_id") or "").strip()
    refs = tuple(str(item) for item in (request.get("evidence_refs") or []) if str(item))
    source_bindings = tuple(
        dict(item) for item in (request.get("source_bindings") or []) if isinstance(item, dict)
    )
    source_update_mode = str(request.get("source_update_mode") or "upsert").strip().lower()
    path = store._task_path(task_id)
    if (
        not task_id
        or not prompt
        or not prepare_request_id
        or source_update_mode not in {"upsert", "replace"}
        or not path.exists()
    ):
        return None
    current_time = now(request.get("now"))
    updated = False

    def updater(data: dict[str, Any]) -> dict[str, Any]:
        nonlocal updated
        link = ThreadTaskLink.from_dict(data)
        first, amendment = _audit_publish_modes(link, prepare_request_id)
        if not _audit_revision_publishable(link, task_id, first, amendment):
            return data
        updated = True
        user_history = (
            append_audit_user_requirement(link, str(link.pending_prompt or "").strip())
            if first
            else link.effective_user_prompt
        )
        return _published_audit_link(
            link,
            prompt=prompt,
            prepare_request_id=prepare_request_id,
            current_time=current_time,
            refs=refs,
            source_bindings=source_bindings,
            source_update_mode=source_update_mode,
            user_history=user_history,
        ).to_dict()

    payload = update_json_file_atomic(path, updater, require_existing=True)
    return ThreadTaskLink.from_dict(payload) if updated else None


def _published_audit_link(
    link: ThreadTaskLink,
    *,
    prompt: str,
    prepare_request_id: str,
    current_time: float,
    refs: tuple[str, ...],
    source_bindings: tuple[dict[str, Any], ...],
    source_update_mode: str,
    user_history: str,
) -> ThreadTaskLink:
    return replace(
        link,
        goal=published_audit_requirement(
            validated_notes=prompt,
            user_prepare_history=user_history,
        ),
        effective_user_prompt=user_history,
        pending_prompt="",
        pending_updated_at=0.0,
        pending_prepare_request_id="",
        effective_revision=link.effective_revision + 1,
        effective_updated_at=current_time,
        effective_prepare_request_id=prepare_request_id,
        effective_evidence_refs=refs,
        effective_source_bindings=(
            merge_audit_source_bindings(
                () if source_update_mode == "replace" else link.effective_source_bindings,
                source_bindings,
            )
            if source_bindings or source_update_mode == "replace"
            else link.effective_source_bindings
        ),
    )


def _audit_publish_modes(link: ThreadTaskLink, prepare_request_id: str) -> tuple[bool, bool]:
    pending_prompt = str(link.pending_prompt or "").strip()
    return (
        bool(pending_prompt and link.pending_prepare_request_id == prepare_request_id),
        bool(
            not pending_prompt
            and link.effective_prepare_request_id == prepare_request_id
            and str(link.effective_user_prompt or "").strip()
        ),
    )


def _audit_revision_publishable(
    link: ThreadTaskLink,
    task_id: str,
    first_publish: bool,
    same_turn_amendment: bool,
) -> bool:
    return bool(
        link.task_id == task_id
        and str(link.work_kind or "").strip().lower() == "audit"
        and str(link.status or "").strip().lower() not in THREAD_TASK_LINK_INACTIVE_STATUSES
        and (first_publish or same_turn_amendment)
    )


def _reopenable_named_audit(link: object | None) -> bool:
    return bool(
        link is not None
        and str(getattr(link, "work_kind", "") or "").strip().lower() == "audit"
        and str(getattr(link, "work_name", "") or "").strip()
    )


def _terminal_named_audit(link: ThreadTaskLink, task_id: str) -> bool:
    return bool(
        link.task_id == task_id
        and str(link.work_kind or "").strip().lower() == "audit"
        and str(link.status or "").strip().lower() in THREAD_TASK_LINK_NON_RESURRECTABLE_STATUSES
    )


def _indexed_audit_link(
    store: object,
    task_id: str,
    current_time: float,
    payload: dict[str, Any],
    updated: bool,
) -> ThreadTaskLink | None:
    if not updated:
        return None
    link = ThreadTaskLink.from_dict(payload)
    thread = store._update_thread_task_index(
        link.thread_id,
        task_id,
        link.status,
        current_time,
    )
    _sync_task_workspace_status(link, current_time, owner_home=thread.owner_home)
    return link


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
        observation_id=str(kwargs.get("_observation_id") or "").strip()
        or new_id("obs"),
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
        "_observation_id": request.get("_observation_id", ""),
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
    # LLM: Observation append owns its ledger row and activity time only; other thread fields are
    # preserved by the atomic latest-record updater.
    # 函数用途: 追加后台观察事件，并安全刷新活动时间而不覆盖任务、压缩或通道状态。
    def append_observation(self, request: dict) -> ObservationEvent:
        thread_id = str(request.get("thread_id") or "")
        thread = self._require_thread(thread_id)
        event, current = _observation_from_request(thread.thread_id, request)
        append_jsonl(self._observation_path(thread_id), event.to_dict(), sort_keys=True)
        self._update_thread_atomic(
            thread.thread_id,
            lambda latest: replace(
                latest,
                updated_at=max(latest.updated_at, current),
            ),
        )
        return event

    def recent_observations(
        self, thread_id: str, *, limit: int = 20, include_handled: bool = True
    ) -> list[ObservationEvent]:
        events, _errors = self.recent_observations_report(
            thread_id,
            limit=limit,
            include_handled=include_handled,
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


# LLM: Guidance construction is shared by ordinary append and idempotent ingress. Retry-only
# fields such as ``now`` never alter an already prepared entry.
# 函数用途: 校验补充消息并构造一条尚未写盘的 guidance 记录。
def _guidance_entry_from_request(
    request: dict[str, Any],
    *,
    guidance_id: str = "",
) -> GuidanceEntry:
    target_type = normalize_guidance_target_type(request.get("target_type"))
    target_id = str(request.get("target_id") or "").strip()
    message = str(request.get("message") or "").strip()
    if not target_type or not target_id:
        raise ValueError("target_type and target_id are required")
    if not message:
        raise ValueError("guidance message is required")
    metadata = request.get("metadata")
    return GuidanceEntry(
        guidance_id=str(guidance_id or "").strip() or new_id("guidance"),
        target_type=target_type,
        target_id=target_id,
        message=message,
        sender=str(request.get("sender") or "").strip(),
        priority=str(request.get("priority") or "normal").strip() or "normal",
        delivery=str(request.get("delivery") or "next_turn").strip() or "next_turn",
        created_at=now(request.get("now")),
        metadata=dict(metadata) if isinstance(metadata, dict) else {},
    )


# LLM: The digest covers every semantic input that could make reuse unsafe, but excludes time and
# the generated guidance id so a transport retry remains byte-independent.
# 函数用途: 为幂等键计算正文、目标和结构化元数据的稳定指纹。
def _guidance_input_digest(request: dict[str, Any]) -> str:
    metadata = request.get("metadata")
    canonical_metadata = dict(metadata) if isinstance(metadata, dict) else {}
    canonical_metadata.pop("dedupe_key", None)
    canonical = {
        "target_type": normalize_guidance_target_type(request.get("target_type")),
        "target_id": str(request.get("target_id") or "").strip(),
        "message": str(request.get("message") or "").strip(),
        "sender": str(request.get("sender") or "").strip(),
        "priority": str(request.get("priority") or "normal").strip() or "normal",
        "delivery": str(request.get("delivery") or "next_turn").strip() or "next_turn",
        "metadata": canonical_metadata,
    }
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# LLM: The embedded entry, not the stored digest string, reconstructs receipt identity. Server-only
# dedupe metadata is removed exactly as append does, while target/message and state combinations are
# validated before any queue repair can trust the row.
# 函数用途: 重算幂等补充回执的正文指纹，并校验状态、尝试和提交字段是否自洽。
def _validate_guidance_once_receipt(
    receipt: GuidanceOnceReceipt,
    *,
    legacy: bool,
) -> None:
    entry = receipt.entry
    metadata = dict(entry.metadata) if isinstance(entry.metadata, dict) else {}
    if str(metadata.get("dedupe_key") or "").strip() != receipt.dedupe_key:
        raise DataCorruptionError("conversation guidance receipt metadata key mismatch")
    reconstructed = {
        "target_type": entry.target_type,
        "target_id": entry.target_id,
        "message": entry.message,
        "sender": entry.sender,
        "priority": entry.priority,
        "delivery": entry.delivery,
        "metadata": metadata,
    }
    if (
        normalize_guidance_target_type(entry.target_type) != entry.target_type
        or not str(entry.target_id or "").strip()
        or not str(entry.message or "").strip()
        or _guidance_input_digest(reconstructed) != receipt.input_digest
    ):
        raise DataCorruptionError("conversation guidance receipt input digest mismatch")
    if (
        not math.isfinite(receipt.updated_at)
        or not math.isfinite(receipt.submitted_at)
        or receipt.updated_at < 0
        or receipt.submitted_at < 0
    ):
        raise DataCorruptionError("conversation guidance receipt timestamp is invalid")
    if legacy:
        return
    state_fields_valid = {
        "pending": (
            not receipt.attempt_id
            and not receipt.submission_id
            and receipt.submitted_at == 0
        ),
        "reserved": (
            bool(receipt.attempt_id)
            and not receipt.submission_id
            and receipt.submitted_at == 0
        ),
        "submitted": (
            bool(receipt.attempt_id)
            and bool(receipt.submission_id)
            and receipt.submitted_at > 0
        ),
        "consumed": (
            bool(receipt.attempt_id)
            and bool(receipt.submission_id)
            and receipt.submitted_at > 0
        ),
        "rejected": not receipt.submission_id and receipt.submitted_at == 0,
    }
    if not state_fields_valid.get(receipt.status, False):
        raise DataCorruptionError("conversation guidance receipt state fields are invalid")


class ConversationGuidanceStore(ConversationObservationStore):
    # LLM: Runtime prompt mutation and terminal receipt settlement for one exact turn share this
    # cross-process lock, mirroring 会话运行时's active_turn mutex around queue admission and finish.
    # 函数用途: 返回一个精确活动回合的补充消息状态转换锁。
    def guidance_turn_transition_guard(self, expected_turn_id: str):
        turn_id = str(expected_turn_id or "").strip()
        if not turn_id:
            raise ValueError("guidance turn transition requires expected_turn_id")
        digest = hashlib.sha256(turn_id.encode("utf-8")).hexdigest()
        return locked_file_transition(
            self.guidance_turn_index_dir / f".{digest}.transition"
        )

    # LLM: Plain guidance append remains the non-idempotent internal primitive. External ingress
    # with a stable message id must call ``append_guidance_once`` instead.
    # 函数用途: 追加一条不带重试语义的内部补充消息。
    def append_guidance(self, request: dict[str, Any]) -> GuidanceEntry:
        entry = _guidance_entry_from_request(request)
        append_jsonl(
            self._guidance_path(entry.target_type, entry.target_id),
            entry.to_dict(),
            sort_keys=True,
        )
        return entry

    # LLM: The receipt is written before the queue row, then the exact prepared entry is repaired
    # under one cross-process transition lock. This closes both append-before-index and
    # index-before-append crash windows without scanning every guidance file.
    # 函数用途: 按稳定消息 ID 只追加一次补充消息，进程崩溃后重试也会返回同一条记录。
    def append_guidance_once(
        self,
        request: dict[str, Any],
        *,
        dedupe_key: str,
    ) -> GuidanceEntry:
        key = str(dedupe_key or "").strip()
        if not key:
            raise ValueError("guidance dedupe_key is required")
        digest = _guidance_input_digest(request)
        receipt_path = self._guidance_dedupe_path(key)
        transition = receipt_path.with_name(f".{receipt_path.name}.transition")
        with locked_file_transition(transition):
            receipt = self._read_guidance_once_receipt(receipt_path)
            if receipt is not None:
                if receipt.dedupe_key != key or receipt.input_digest != digest:
                    raise DataCorruptionError(
                        f"conversation guidance dedupe key reused with different input: {key}"
                    )
                if receipt.status in {"pending", "reserved", "submitted"}:
                    self._ensure_guidance_turn_receipt_index(receipt)
                    self._ensure_guidance_input_receipt_index(receipt)
                    self._ensure_guidance_once_entry(receipt.entry)
                return receipt.entry
            metadata = request.get("metadata")
            prepared_request = {
                **request,
                "metadata": {
                    **(metadata if isinstance(metadata, dict) else {}),
                    "dedupe_key": key,
                },
            }
            entry = _guidance_entry_from_request(prepared_request)
            receipt = GuidanceOnceReceipt(
                dedupe_key=key,
                input_digest=digest,
                status="pending",
                entry=entry,
                updated_at=time.time(),
            )
            write_json_file_atomic(receipt_path, receipt.to_dict())
            self._ensure_guidance_turn_receipt_index(receipt)
            self._ensure_guidance_input_receipt_index(receipt)
            self._ensure_guidance_once_entry(entry)
            return entry

    # LLM: Receipt lookup repairs a prepared-but-not-appended row before exposing state. Missing
    # receipts mean no idempotent delivery fact exists; unreadable receipts fail closed.
    # 函数用途: 查询一条补充消息的持久回执，供 Gateway 对账网络超时。
    def guidance_once_receipt(self, dedupe_key: str) -> GuidanceOnceReceipt | None:
        key = str(dedupe_key or "").strip()
        if not key:
            return None
        receipt_path = self._guidance_dedupe_path(key)
        transition = receipt_path.with_name(f".{receipt_path.name}.transition")
        with locked_file_transition(transition):
            receipt = self._read_guidance_once_receipt(receipt_path)
            if receipt is None:
                return None
            if receipt.dedupe_key != key:
                raise DataCorruptionError("conversation guidance receipt key mismatch")
            if receipt.status in {"pending", "reserved", "submitted"}:
                self._ensure_guidance_turn_receipt_index(receipt)
                self._ensure_guidance_input_receipt_index(receipt)
                self._ensure_guidance_once_entry(receipt.entry)
            delivered = self._read_guidance_delivered().get(receipt.entry.guidance_id, 0.0)
            return replace(receipt, entry=replace(receipt.entry, delivered_at=delivered))

    # LLM: Competing runtime-claim and terminal-reject edges use this same receipt lock. The
    # returned state is the winner; callers must never overwrite a conflicting terminal outcome.
    # 函数用途: 原子推进补充消息状态，并把并发竞争中真正获胜的状态返回给调用方。
    def mark_guidance_once_status(self, dedupe_key: str, status: str) -> GuidanceOnceReceipt:
        key = str(dedupe_key or "").strip()
        normalized = str(status or "").strip().lower()
        if not key or normalized not in {"submitted", "consumed", "rejected"}:
            raise ValueError("guidance receipt requires submitted, consumed, or rejected status")
        receipt_path = self._guidance_dedupe_path(key)
        transition = receipt_path.with_name(f".{receipt_path.name}.transition")
        with locked_file_transition(transition):
            receipt = self._read_guidance_once_receipt(receipt_path)
            if receipt is None:
                raise KeyError(f"conversation guidance receipt not found: {key}")
            if receipt.dedupe_key != key:
                raise DataCorruptionError("conversation guidance receipt key mismatch")
            if receipt.status == normalized:
                return receipt
            allowed = (
                receipt.status == "pending" and normalized == "rejected"
            ) or (
                receipt.status == "reserved" and normalized == "submitted"
            ) or (
                receipt.status == "submitted" and normalized == "consumed"
            )
            if not allowed:
                return receipt
            updated = replace(receipt, status=normalized, updated_at=time.time())
            write_json_file_atomic(receipt_path, updated.to_dict())
            return updated

    # LLM: Runtime admission validates the exact turn and reserves pending input for one durable
    # attempt. A reserved/submitted receipt is never inherited by another attempt implicitly.
    # 函数用途: 在模型安全点为当前执行尝试首次预留补充消息，其他尝试不能重复注入。
    def claim_guidance_once_for_turn(
        self,
        entry: GuidanceEntry,
        *,
        expected_turn_id: str,
        attempt_id: str,
    ) -> bool:
        metadata = entry.metadata if isinstance(entry.metadata, dict) else {}
        dedupe_key = str(metadata.get("dedupe_key") or "").strip()
        if not dedupe_key:
            return True
        receipt_path = self._guidance_dedupe_path(dedupe_key)
        transition = receipt_path.with_name(f".{receipt_path.name}.transition")
        with locked_file_transition(transition):
            receipt = self._read_guidance_once_receipt(receipt_path)
            if receipt is None or receipt.entry.guidance_id != entry.guidance_id:
                raise DataCorruptionError("conversation guidance receipt entry mismatch")
            receipt_metadata = (
                receipt.entry.metadata if isinstance(receipt.entry.metadata, dict) else {}
            )
            receipt_turn_id = str(receipt_metadata.get("expected_turn_id") or "").strip()
            if receipt_turn_id and receipt_turn_id != str(expected_turn_id or "").strip():
                return False
            if receipt.status != "pending":
                return False
            normalized_attempt_id = str(attempt_id or "").strip()
            if not normalized_attempt_id:
                raise ValueError("guidance reservation requires attempt_id")
            updated = replace(
                receipt,
                status="reserved",
                attempt_id=normalized_attempt_id,
                updated_at=time.time(),
            )
            write_json_file_atomic(receipt_path, updated.to_dict())
            return True

    # LLM: This transition runs immediately before the provider call while holding the exact-turn
    # guard. Only rows reserved by the same durable attempt can cross into submission-unknown state.
    # 函数用途: 在真正调用模型前把本批补充消息标记为已开始提交，并返回对应消息 ID。
    def mark_guidance_entries_submitted(
        self,
        expected_turn_id: str,
        entries: list[GuidanceEntry] | tuple[GuidanceEntry, ...],
        *,
        attempt_id: str,
        provider_call_id: str = "",
        now: float | None = None,
    ) -> tuple[str, ...]:
        turn_id = str(expected_turn_id or "").strip()
        normalized_attempt_id = str(attempt_id or "").strip()
        if not turn_id or not normalized_attempt_id:
            raise ValueError("guidance submission requires turn and attempt ids")
        prepared = tuple(
            entry for entry in entries if str(getattr(entry, "guidance_id", "") or "").strip()
        )
        if not prepared:
            return ()
        guidance_ids = tuple(str(entry.guidance_id) for entry in prepared)
        call_id = str(provider_call_id or "").strip()
        if not call_id:
            legacy_source = json.dumps(
                [normalized_attempt_id, *sorted(guidance_ids)],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            call_id = f"legacy-{hashlib.sha256(legacy_source.encode('utf-8')).hexdigest()}"
        submitted_at = now if now is not None else time.time()
        items = [
            {
                "guidance_id": str(entry.guidance_id),
                "dedupe_key": str(
                    (entry.metadata if isinstance(entry.metadata, dict) else {}).get("dedupe_key")
                    or ""
                ).strip(),
            }
            for entry in prepared
        ]
        batch = {
            "schema_version": "conversation_guidance_submission_batch.v1",
            "expected_turn_id": turn_id,
            "attempt_id": normalized_attempt_id,
            "provider_call_id": call_id,
            "items": items,
            "status": "submitted",
            "committed_at": submitted_at,
        }
        with self.guidance_turn_transition_guard(turn_id):
            batch_path = self._guidance_submission_batch_path(turn_id, call_id)
            report = read_json_file_report(
                batch_path,
                context="conversation.guidance_submission_batch.read",
            )
            if report.load_error is not None:
                raise DataCorruptionError("conversation guidance submission batch is unreadable")
            if report.payload:
                stable_keys = (
                    "schema_version",
                    "expected_turn_id",
                    "attempt_id",
                    "provider_call_id",
                    "items",
                )
                if {key: report.payload.get(key) for key in stable_keys} != {
                    key: batch.get(key) for key in stable_keys
                }:
                    raise DataCorruptionError("conversation guidance submission batch conflicts")
                if str(report.payload.get("status") or "") != "submitted":
                    raise DataCorruptionError("rejected guidance submission cannot be replayed")
                batch = dict(report.payload)
            else:
                self._validate_guidance_submission_batch_locked(batch, allow_projected=False)
                write_json_file_atomic(batch_path, batch)
            self._apply_guidance_submission_batch_locked(batch)
        return guidance_ids

    # LLM: Only a structured provider pre-execution rejection may move the same attempt from
    # submitted back to reserved. Transport ambiguity and another attempt can never use this edge.
    # 函数用途: 模型明确因上下文超限拒绝请求时，把同一批消息恢复为本尝试可重新提交状态。
    def restore_submitted_guidance_for_retry(
        self,
        expected_turn_id: str,
        entries: list[GuidanceEntry] | tuple[GuidanceEntry, ...],
        *,
        attempt_id: str,
        provider_call_id: str = "",
    ) -> tuple[str, ...]:
        turn_id = str(expected_turn_id or "").strip()
        normalized_attempt_id = str(attempt_id or "").strip()
        if not turn_id or not normalized_attempt_id:
            raise ValueError("guidance retry restore requires turn and attempt ids")
        call_id = str(provider_call_id or "").strip()
        restored: list[str] = []
        with self.guidance_turn_transition_guard(turn_id):
            if call_id:
                batch_path = self._guidance_submission_batch_path(turn_id, call_id)
                report = read_json_file_report(
                    batch_path,
                    context="conversation.guidance_submission_batch.retry",
                )
                if report.load_error is not None or not report.payload:
                    raise DataCorruptionError("guidance retry submission batch is unavailable")
                batch = dict(report.payload)
                if (
                    str(batch.get("attempt_id") or "") != normalized_attempt_id
                    or str(batch.get("provider_call_id") or "") != call_id
                ):
                    raise DataCorruptionError("guidance retry submission batch mismatch")
                if str(batch.get("status") or "") == "submitted":
                    batch["status"] = "rejected"
                    batch["rejected_at"] = time.time()
                    write_json_file_atomic(batch_path, batch)
                self._apply_guidance_submission_batch_locked(batch)
            for entry in entries:
                metadata = entry.metadata if isinstance(entry.metadata, dict) else {}
                dedupe_key = str(metadata.get("dedupe_key") or "").strip()
                if not dedupe_key:
                    continue
                receipt_path = self._guidance_dedupe_path(dedupe_key)
                transition = receipt_path.with_name(f".{receipt_path.name}.transition")
                with locked_file_transition(transition):
                    receipt = self._read_guidance_once_receipt(receipt_path)
                    receipt_metadata = (
                        receipt.entry.metadata
                        if receipt is not None and isinstance(receipt.entry.metadata, dict)
                        else {}
                    )
                    if (
                        receipt is None
                        or receipt.entry.guidance_id != entry.guidance_id
                        or str(receipt_metadata.get("expected_turn_id") or "").strip()
                        != turn_id
                        or receipt.attempt_id != normalized_attempt_id
                    ):
                        raise DataCorruptionError("guidance retry restore mismatch")
                    if receipt.status == "reserved" and not receipt.submission_id:
                        restored.append(entry.guidance_id)
                        continue
                    if receipt.status != "submitted" or (
                        call_id and receipt.submission_id != call_id
                    ):
                        raise DataCorruptionError("guidance retry restore was not submitted")
                    write_json_file_atomic(
                        receipt_path,
                        replace(
                            receipt,
                            status="reserved",
                            submission_id="",
                            submitted_at=0.0,
                            updated_at=time.time(),
                        ).to_dict(),
                    )
                    restored.append(entry.guidance_id)
        return tuple(restored)

    # LLM: A submission batch is the all-or-nothing provider-boundary authority. Validation checks
    # every immutable guidance/key pair before the single batch file can be committed.
    # 函数用途: 校验一次模型调用对应的补充消息整批身份和当前预留状态。
    def _validate_guidance_submission_batch_locked(
        self,
        batch: dict[str, Any],
        *,
        allow_projected: bool,
    ) -> None:
        turn_id = str(batch.get("expected_turn_id") or "").strip()
        attempt_id = str(batch.get("attempt_id") or "").strip()
        call_id = str(batch.get("provider_call_id") or "").strip()
        items = batch.get("items")
        if (
            batch.get("schema_version") != "conversation_guidance_submission_batch.v1"
            or not turn_id
            or not attempt_id
            or not call_id
            or not isinstance(items, list)
            or not items
            or str(batch.get("status") or "") not in {"submitted", "rejected"}
        ):
            raise DataCorruptionError("conversation guidance submission batch is invalid")
        seen_ids: set[str] = set()
        seen_keys: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                raise DataCorruptionError("conversation guidance submission item is invalid")
            guidance_id = str(item.get("guidance_id") or "").strip()
            dedupe_key = str(item.get("dedupe_key") or "").strip()
            if not guidance_id or guidance_id in seen_ids:
                raise DataCorruptionError("conversation guidance submission item id conflicts")
            seen_ids.add(guidance_id)
            if not dedupe_key:
                continue
            if dedupe_key in seen_keys:
                raise DataCorruptionError("conversation guidance submission receipt key conflicts")
            seen_keys.add(dedupe_key)
            receipt_path = self._guidance_dedupe_path(dedupe_key)
            transition = receipt_path.with_name(f".{receipt_path.name}.transition")
            with locked_file_transition(transition):
                receipt = self._read_guidance_once_receipt(receipt_path)
                metadata = (
                    receipt.entry.metadata
                    if receipt is not None and isinstance(receipt.entry.metadata, dict)
                    else {}
                )
                if (
                    receipt is None
                    or receipt.entry.guidance_id != guidance_id
                    or str(metadata.get("expected_turn_id") or "").strip() != turn_id
                    or receipt.attempt_id != attempt_id
                ):
                    raise DataCorruptionError("guidance submission reservation mismatch")
                if not allow_projected and receipt.status != "reserved":
                    raise DataCorruptionError("guidance submission was not reserved")

    # LLM: Individual receipts are projections of the atomic batch. Replaying old batches never
    # overwrites a newer submission id; rejected batches only release their own exact projection.
    # 函数用途: 根据一份已提交或已拒绝的批次幂等修复每条补充消息回执。
    def _apply_guidance_submission_batch_locked(self, batch: dict[str, Any]) -> None:
        self._validate_guidance_submission_batch_locked(batch, allow_projected=True)
        call_id = str(batch.get("provider_call_id") or "").strip()
        batch_status = str(batch.get("status") or "")
        submitted_at = float(batch.get("committed_at") or time.time())
        for item in batch.get("items", []):
            key = str(item.get("dedupe_key") or "").strip()
            if not key:
                continue
            receipt_path = self._guidance_dedupe_path(key)
            transition = receipt_path.with_name(f".{receipt_path.name}.transition")
            with locked_file_transition(transition):
                receipt = self._read_guidance_once_receipt(receipt_path)
                if receipt.status in {"consumed", "rejected"}:
                    continue
                if batch_status == "submitted":
                    if receipt.status == "submitted" and receipt.submission_id != call_id:
                        continue
                    if receipt.status not in {"reserved", "submitted"}:
                        raise DataCorruptionError("guidance submission projection state is invalid")
                    updated = replace(
                        receipt,
                        status="submitted",
                        submission_id=call_id,
                        submitted_at=submitted_at,
                        updated_at=submitted_at,
                    )
                else:
                    if receipt.status == "submitted" and receipt.submission_id != call_id:
                        continue
                    if receipt.status == "reserved" and receipt.submission_id not in {"", call_id}:
                        continue
                    updated = replace(
                        receipt,
                        status="reserved",
                        submission_id="",
                        submitted_at=0.0,
                        updated_at=float(batch.get("rejected_at") or time.time()),
                    )
                if updated != receipt:
                    write_json_file_atomic(receipt_path, updated.to_dict())

    # LLM: Recovery and terminal settlement replay atomic submission batches before interpreting
    # receipt states, so a crash during the Nth receipt projection never splits one provider call.
    # 函数用途: 修复精确回合的模型提交批次投影，并返回发现的损坏批次数。
    def _repair_committed_guidance_submission_batches_locked(self, turn_id: str) -> int:
        turn_digest = hashlib.sha256(turn_id.encode("utf-8")).hexdigest()
        errors = 0
        batches: list[dict[str, Any]] = []
        for path in sorted((self.guidance_submission_batches_dir / turn_digest).glob("*.json")):
            report = read_json_file_report(
                path,
                context="conversation.guidance_submission_batch.repair",
            )
            payload = report.payload
            if (
                report.load_error is not None
                or payload.get("schema_version")
                != "conversation_guidance_submission_batch.v1"
                or str(payload.get("expected_turn_id") or "") != turn_id
            ):
                errors += 1
                continue
            batches.append(dict(payload))
        batches.sort(
            key=lambda item: (
                float(item.get("committed_at") or 0.0),
                float(item.get("rejected_at") or 0.0),
                str(item.get("provider_call_id") or ""),
            )
        )
        for batch in batches:
            try:
                self._apply_guidance_submission_batch_locked(batch)
            except Exception:
                errors += 1
        return errors

    # LLM: Read-only pending checks use the same exact-turn and receipt-state rules as claim, but
    # never mutate delivery fate; the later claim remains the only admission decision point.
    # 函数用途: 判断一条补充消息能否由指定回合认领，供运行循环做无副作用的待处理检查。
    def guidance_available_for_turn(
        self,
        entry: GuidanceEntry,
        *,
        expected_turn_id: str,
    ) -> bool:
        metadata = entry.metadata if isinstance(entry.metadata, dict) else {}
        dedupe_key = str(metadata.get("dedupe_key") or "").strip()
        if not dedupe_key:
            return True
        receipt = self.guidance_once_receipt(dedupe_key)
        if receipt is None or receipt.entry.guidance_id != entry.guidance_id:
            raise DataCorruptionError("conversation guidance receipt entry mismatch")
        receipt_metadata = (
            receipt.entry.metadata if isinstance(receipt.entry.metadata, dict) else {}
        )
        receipt_turn_id = str(receipt_metadata.get("expected_turn_id") or "").strip()
        if receipt_turn_id and receipt_turn_id != str(expected_turn_id or "").strip():
            return False
        return receipt.status == "pending"

    # LLM: Turn terminalization owns the same turn lock as runtime prompt mutation, then settles
    # only this turn's indexed receipts. Corruption is counted locally and cannot block other turns.
    # 函数用途: 回合结束时原子拒绝尚未消费的补充消息，并返回本回合状态计数。
    def reject_pending_guidance_for_turn(
        self,
        expected_turn_id: str,
        *,
        reject_reserved: bool = False,
    ) -> dict[str, int]:
        turn_id = str(expected_turn_id or "").strip()
        if not turn_id:
            return {
                "rejected": 0,
                "reserved": 0,
                "submitted": 0,
                "consumed": 0,
                "retired_legacy": 0,
                "errors": 0,
            }
        with self.guidance_turn_transition_guard(turn_id):
            return self._reject_pending_guidance_for_turn_locked(
                turn_id,
                reject_reserved=reject_reserved,
            )

    # LLM: Only a caller that has proved the owning execution attempt dead may release reserved
    # rows. Submitted rows crossed the provider boundary and always remain delivery-unknown.
    # 函数用途: 在请求租约已确认失效后，把尚未开始模型提交的预留消息恢复为可再次认领。
    def release_reserved_guidance_for_turn(
        self,
        expected_turn_id: str,
        *,
        dead_attempt_id: str = "",
    ) -> dict[str, int]:
        turn_id = str(expected_turn_id or "").strip()
        summary: dict[str, Any] = {
            "released": 0,
            "released_guidance_ids": [],
            "submitted": 0,
            "errors": 0,
        }
        if not turn_id:
            return summary
        expected_attempt = str(dead_attempt_id or "").strip()
        with self.guidance_turn_transition_guard(turn_id):
            summary["errors"] += self._repair_committed_guidance_submission_batches_locked(
                turn_id
            )
            turn_digest = hashlib.sha256(turn_id.encode("utf-8")).hexdigest()
            index_dir = self.guidance_turn_index_dir / turn_digest
            for index_path in sorted(index_dir.glob("*.json")):
                index_report = read_json_file_report(
                    index_path,
                    context="conversation.guidance_turn_index.release",
                )
                dedupe_key = str(index_report.payload.get("dedupe_key") or "").strip()
                if index_report.load_error is not None or not dedupe_key:
                    summary["errors"] += 1
                    continue
                receipt_path = self._guidance_dedupe_path(dedupe_key)
                transition = receipt_path.with_name(f".{receipt_path.name}.transition")
                try:
                    with locked_file_transition(transition):
                        receipt = self._read_guidance_once_receipt(receipt_path)
                        metadata = (
                            receipt.entry.metadata
                            if receipt is not None and isinstance(receipt.entry.metadata, dict)
                            else {}
                        )
                        if (
                            receipt is None
                            or str(metadata.get("expected_turn_id") or "").strip() != turn_id
                        ):
                            summary["errors"] += 1
                            continue
                        if receipt.status == "submitted":
                            summary["submitted"] += 1
                            continue
                        if receipt.status != "reserved" or (
                            expected_attempt and receipt.attempt_id != expected_attempt
                        ):
                            continue
                        write_json_file_atomic(
                            receipt_path,
                            replace(
                                receipt,
                                status="pending",
                                attempt_id="",
                                submitted_at=0.0,
                                updated_at=time.time(),
                            ).to_dict(),
                        )
                        summary["released"] += 1
                        summary["released_guidance_ids"].append(receipt.entry.guidance_id)
                except Exception:
                    summary["errors"] += 1
        return summary

    # LLM: Caller holds the exact turn guard; per-receipt locks retain retry idempotency while the
    # bounded turn index prevents one corrupt historical receipt from poisoning every close.
    # 函数用途: 在已持有回合锁时逐条结算当前回合索引。
    def _reject_pending_guidance_for_turn_locked(
        self,
        turn_id: str,
        *,
        reject_reserved: bool = False,
    ) -> dict[str, int]:
        summary = {
            "rejected": 0,
            "reserved": 0,
            "submitted": 0,
            "consumed": 0,
            "retired_legacy": 0,
            "errors": 0,
        }
        summary["errors"] += self._repair_committed_guidance_submission_batches_locked(
            turn_id
        )
        summary["errors"] += self._repair_committed_guidance_ack_batches_locked(turn_id)
        turn_digest = hashlib.sha256(turn_id.encode("utf-8")).hexdigest()
        index_dir = self.guidance_turn_index_dir / turn_digest
        for index_path in sorted(index_dir.glob("*.json")):
            index_report = read_json_file_report(
                index_path,
                context="conversation.guidance_turn_index.read",
            )
            dedupe_key = str(index_report.payload.get("dedupe_key") or "").strip()
            if index_report.load_error is not None or not dedupe_key:
                summary["errors"] += 1
                continue
            receipt_path = self._guidance_dedupe_path(dedupe_key)
            transition = receipt_path.with_name(f".{receipt_path.name}.transition")
            try:
                with locked_file_transition(transition):
                    receipt = self._read_guidance_once_receipt(receipt_path)
                    if receipt is None:
                        summary["errors"] += 1
                        continue
                    metadata = (
                        receipt.entry.metadata if isinstance(receipt.entry.metadata, dict) else {}
                    )
                    if str(metadata.get("expected_turn_id") or "").strip() != turn_id:
                        summary["errors"] += 1
                        continue
                    # Gateway terminalization holds its outer exact-turn lock. A reserved row is
                    # then proven not to have an atomic submission batch and is safe to reject.
                    if receipt.status == "pending" or (
                        reject_reserved and receipt.status == "reserved"
                    ):
                        receipt = replace(
                            receipt,
                            status="rejected",
                            submission_id="",
                            updated_at=time.time(),
                        )
                        write_json_file_atomic(receipt_path, receipt.to_dict())
                    if receipt.status in summary:
                        summary[receipt.status] += 1
            except Exception:
                summary["errors"] += 1
        # Internal non-idempotent request guidance has no receipt state. The
        # delivered projection is its only durable retirement fact, so close it
        # under the same exact-turn guard instead of letting stop replay it.
        entries, load_errors = self.recent_guidance_report(
            "request",
            turn_id,
            limit=0,
            include_delivered=False,
        )
        legacy_ids = tuple(
            entry.guidance_id
            for entry in entries
            if not str(
                (entry.metadata if isinstance(entry.metadata, dict) else {}).get("dedupe_key")
                or ""
            ).strip()
        )
        if legacy_ids:
            self.mark_guidance_delivered(legacy_ids)
            summary["retired_legacy"] += len(legacy_ids)
        summary["errors"] += len(load_errors)
        return summary

    # LLM: Provider success commits one immutable turn-level batch before repairing individual
    # receipts. The batch is authority across a crash between receipt files; terminal settlement
    # repairs the whole batch before it may reject any pending row.
    # 函数用途: 模型确认收到整批补充消息后，原子提交批次并把每条回执补齐为已消费。
    def consume_submitted_guidance_for_turn(
        self,
        expected_turn_id: str,
        entries: list[GuidanceEntry] | tuple[GuidanceEntry, ...],
        *,
        provider_call_id: str = "",
        now: float | None = None,
    ) -> tuple[str, ...]:
        turn_id = str(expected_turn_id or "").strip()
        if not turn_id:
            raise ValueError("guidance provider ack requires expected_turn_id")
        prepared = tuple(
            entry for entry in entries if str(getattr(entry, "guidance_id", "") or "").strip()
        )
        if not prepared:
            return ()
        guidance_ids = tuple(str(entry.guidance_id) for entry in prepared)
        items: list[dict[str, str]] = []
        for entry in prepared:
            metadata = entry.metadata if isinstance(entry.metadata, dict) else {}
            receipt_turn = str(metadata.get("expected_turn_id") or "").strip()
            if receipt_turn and receipt_turn != turn_id:
                raise DataCorruptionError("guidance provider ack turn mismatch")
            key = str(metadata.get("dedupe_key") or "").strip()
            items.append({"guidance_id": str(entry.guidance_id), "dedupe_key": key})
        committed_at = now if now is not None else time.time()
        call_id = str(provider_call_id or "").strip()
        batch = {
            "schema_version": "conversation_guidance_ack_batch.v3",
            "expected_turn_id": turn_id,
            "items": items,
            "provider_call_id": call_id,
            "committed_at": committed_at,
        }
        with self.guidance_turn_transition_guard(turn_id):
            if call_id:
                submission_path = self._guidance_submission_batch_path(turn_id, call_id)
                submission_report = read_json_file_report(
                    submission_path,
                    context="conversation.guidance_submission_batch.ack",
                )
                submission = submission_report.payload
                if (
                    submission_report.load_error is not None
                    or not submission
                    or str(submission.get("status") or "") != "submitted"
                    or str(submission.get("provider_call_id") or "") != call_id
                ):
                    raise DataCorruptionError("guidance provider ack has no submitted batch")
                self._apply_guidance_submission_batch_locked(submission)
            batch_path = self._guidance_ack_batch_path(turn_id, guidance_ids)
            report = read_json_file_report(batch_path, context="conversation.guidance_ack_batch.read")
            if report.load_error is not None:
                raise DataCorruptionError("conversation guidance ack batch is unreadable")
            if report.payload:
                stable_existing = {
                    key: report.payload.get(key)
                    for key in (
                        "schema_version",
                        "expected_turn_id",
                        "items",
                        "provider_call_id",
                    )
                }
                stable_batch = {key: batch.get(key) for key in stable_existing}
                if stable_existing != stable_batch:
                    raise DataCorruptionError("conversation guidance ack batch conflicts")
                batch = dict(report.payload)
                committed_at = float(batch.get("committed_at") or committed_at)
            else:
                self._validate_guidance_ack_batch_locked(batch)
                write_json_file_atomic(batch_path, batch)
            self._apply_guidance_ack_batch_locked(batch)
        self.mark_guidance_delivered(guidance_ids, now=committed_at)
        return guidance_ids

    # LLM: Compatibility callers still receive the same result, but the exact turn is taken only
    # from structured receipt metadata and the batch commit remains the authority.
    # 函数用途: 兼容旧调用方式，从本批结构化元数据提取精确回合后提交模型消费。
    def mark_guidance_entries_consumed(
        self,
        entries: list[GuidanceEntry] | tuple[GuidanceEntry, ...],
        *,
        now: float | None = None,
    ) -> tuple[str, ...]:
        turn_ids = {
            str((entry.metadata if isinstance(entry.metadata, dict) else {}).get("expected_turn_id") or "").strip()
            for entry in entries
            if str((entry.metadata if isinstance(entry.metadata, dict) else {}).get("expected_turn_id") or "").strip()
        }
        if len(turn_ids) != 1:
            raise ValueError("guidance provider ack requires one exact turn")
        return self.consume_submitted_guidance_for_turn(next(iter(turn_ids)), entries, now=now)

    # LLM: Caller holds the exact turn lock. Validation checks each immutable guidance/key pair
    # before the journal commit so a malformed entry cannot create a poisoned authoritative batch.
    # 函数用途: 校验模型确认批次中的每条消息、回执键和精确回合完全对应。
    def _validate_guidance_ack_batch_locked(self, batch: dict[str, Any]) -> None:
        turn_id = str(batch.get("expected_turn_id") or "").strip()
        items = batch.get("items")
        if (
            batch.get("schema_version")
            not in {"conversation_guidance_ack_batch.v2", "conversation_guidance_ack_batch.v3"}
            or not turn_id
            or not isinstance(items, list)
            or not items
        ):
            raise DataCorruptionError("conversation guidance ack batch is invalid")
        seen_ids: set[str] = set()
        seen_keys: set[str] = set()
        provider_call_id = str(batch.get("provider_call_id") or "").strip()
        for item in items:
            if not isinstance(item, dict):
                raise DataCorruptionError("conversation guidance ack item is invalid")
            guidance_id = str(item.get("guidance_id") or "").strip()
            dedupe_key = str(item.get("dedupe_key") or "").strip()
            if not guidance_id or guidance_id in seen_ids:
                raise DataCorruptionError("conversation guidance ack item id conflicts")
            seen_ids.add(guidance_id)
            if not dedupe_key:
                continue
            if dedupe_key in seen_keys:
                raise DataCorruptionError("conversation guidance ack receipt key conflicts")
            seen_keys.add(dedupe_key)
            receipt_path = self._guidance_dedupe_path(dedupe_key)
            transition = receipt_path.with_name(f".{receipt_path.name}.transition")
            with locked_file_transition(transition):
                receipt = self._read_guidance_once_receipt(receipt_path)
                metadata = (
                    receipt.entry.metadata
                    if receipt is not None and isinstance(receipt.entry.metadata, dict)
                    else {}
                )
                if (
                    receipt is None
                    or receipt.entry.guidance_id != guidance_id
                    or str(metadata.get("expected_turn_id") or "").strip() != turn_id
                    or receipt.status not in {"submitted", "consumed"}
                    or (
                        provider_call_id
                        and receipt.status == "submitted"
                        and receipt.submission_id != provider_call_id
                    )
                ):
                    raise DataCorruptionError("committed guidance receipt does not match batch")

    # LLM: Caller holds the exact turn lock. A committed v2 batch makes every paired receipt
    # consumed; individual receipt files are repairable projections of that one journal fact.
    # 函数用途: 把一份已校验的模型确认批次补写到各条 guidance 回执。
    def _apply_guidance_ack_batch_locked(self, batch: dict[str, Any]) -> None:
        self._validate_guidance_ack_batch_locked(batch)
        for item in batch.get("items", []):
            key = str(item.get("dedupe_key") or "").strip()
            if not key:
                continue
            receipt_path = self._guidance_dedupe_path(key)
            transition = receipt_path.with_name(f".{receipt_path.name}.transition")
            with locked_file_transition(transition):
                receipt = self._read_guidance_once_receipt(receipt_path)
                if receipt.status != "consumed":
                    receipt = replace(receipt, status="consumed", updated_at=time.time())
                    write_json_file_atomic(receipt_path, receipt.to_dict())
                self._project_guidance_transcript(receipt.entry)

    # LLM: Transcript is an idempotent projection of consumed receipt authority. Ack-batch replay
    # calls this even for already-consumed rows, closing crashes after receipt commit but before log.
    # 函数用途: 把已消费补充消息按 guidance ID 最多写入一次对应会话记录。
    def _project_guidance_transcript(self, entry: GuidanceEntry) -> bool:
        metadata = entry.metadata if isinstance(entry.metadata, dict) else {}
        if metadata.get("record_in_transcript") is not True:
            return True
        guidance_id = str(entry.guidance_id or "").strip()
        thread_id = str(metadata.get("thread_id") or "").strip()
        message = str(entry.message or "").strip()
        if not guidance_id or not thread_id or not message:
            return False
        attribution: dict[str, str] = {}
        if entry.target_type == "task" and entry.target_id:
            attribution["task_id"] = entry.target_id
        elif entry.target_type == "request" and entry.target_id:
            attribution["gateway_request_id"] = entry.target_id
        try:
            self.append_message_once(
                {
                    "thread_id": thread_id,
                    "role": "user",
                    "content": message,
                    "channel": str(metadata.get("channel") or "internal"),
                    "channel_message_id": str(metadata.get("channel_message_id") or ""),
                    "metadata": {
                        "kind": "active_turn_user_input",
                        "guidance_id": guidance_id,
                        **attribution,
                    },
                },
                dedupe_key=f"active-turn-input:{guidance_id}",
            )
        except Exception:
            return False
        return True

    # LLM: Terminal paths call this while holding the exact turn lock, so crash-recovery of a
    # committed provider batch finishes before any pending receipt can be rejected.
    # 函数用途: 修复精确回合中已提交但尚未逐条落完的模型确认批次，返回损坏批次数。
    def _repair_committed_guidance_ack_batches_locked(self, turn_id: str) -> int:
        turn_digest = hashlib.sha256(turn_id.encode("utf-8")).hexdigest()
        errors = 0
        for path in sorted((self.guidance_ack_batches_dir / turn_digest).glob("*.json")):
            try:
                report = read_json_file_report(path, context="conversation.guidance_ack_batch.read")
                payload = report.payload
                if (
                    report.load_error is not None
                    or payload.get("schema_version")
                    not in {
                        "conversation_guidance_ack_batch.v2",
                        "conversation_guidance_ack_batch.v3",
                    }
                    or str(payload.get("expected_turn_id") or "") != turn_id
                ):
                    raise DataCorruptionError("conversation guidance ack batch is invalid")
                self._apply_guidance_ack_batch_locked(payload)
                self.mark_guidance_delivered(
                    tuple(
                        str(item.get("guidance_id") or "")
                        for item in payload.get("items", [])
                        if isinstance(item, dict)
                    ),
                    now=float(payload.get("committed_at") or time.time()),
                )
            except Exception:
                errors += 1
        return errors

    # LLM: Receipt reads distinguish missing files from corrupt payloads; corruption can never be
    # interpreted as a retry miss that creates another guidance row.
    # 函数用途: 在已持有该回执 transition lock 时读取并校验 JSON。
    def _read_guidance_once_receipt(self, path: Path) -> GuidanceOnceReceipt | None:
        report = read_json_file_report(path, context="conversation.guidance_once.read")
        if report.load_error is not None:
            raise DataCorruptionError(f"conversation guidance receipt is unreadable: {path.name}")
        if not report.payload:
            if path.exists():
                raise DataCorruptionError(
                    f"conversation guidance receipt is empty or invalid: {path.name}"
                )
            return None
        receipt = GuidanceOnceReceipt.from_dict(report.payload)
        if report.payload.get("schema_version") != "conversation_guidance_once.v4":
            write_json_file_atomic(path, receipt.to_dict())
        return receipt

    # LLM: This projection is repaired whenever an active receipt is opened. Terminalization can
    # therefore inspect only one exact turn; corrupt refs are isolated to that turn.
    # 函数用途: 为活动补充回执写入一个按回合分桶的小索引引用。
    def _ensure_guidance_turn_receipt_index(self, receipt: GuidanceOnceReceipt) -> None:
        metadata = receipt.entry.metadata if isinstance(receipt.entry.metadata, dict) else {}
        turn_id = str(metadata.get("expected_turn_id") or "").strip()
        if not turn_id:
            return
        index_path = self._guidance_turn_index_path(turn_id, receipt.dedupe_key)
        expected = {
            "schema_version": "conversation_guidance_turn_ref.v1",
            "expected_turn_id": turn_id,
            "dedupe_key": receipt.dedupe_key,
        }
        report = read_json_file_report(index_path, context="conversation.guidance_turn_index.read")
        if report.load_error is not None:
            raise DataCorruptionError("conversation guidance turn index is unreadable")
        if report.payload:
            if report.payload != expected:
                raise DataCorruptionError("conversation guidance turn index conflicts")
            return
        write_json_file_atomic(index_path, expected)

    # LLM: This projection links a prepared Gateway input to its authoritative guidance receipt.
    # It is repaired with the queue projections so a crash before HTTP disposition remains recoverable.
    # 函数用途: 为 Gateway 普通消息写反向索引，避免 guidance 已写但入口回执仍 pending 时失联。
    def _ensure_guidance_input_receipt_index(self, receipt: GuidanceOnceReceipt) -> None:
        metadata = receipt.entry.metadata if isinstance(receipt.entry.metadata, dict) else {}
        input_request_id = str(metadata.get("gateway_input_request_id") or "").strip()
        if not input_request_id:
            return
        index_path = self._guidance_input_index_path(input_request_id)
        expected = {
            "schema_version": "conversation_guidance_input_ref.v1",
            "gateway_input_request_id": input_request_id,
            "dedupe_key": receipt.dedupe_key,
            "expected_turn_id": str(metadata.get("expected_turn_id") or "").strip(),
        }
        report = read_json_file_report(index_path, context="conversation.guidance_input_index.read")
        if report.load_error is not None:
            raise DataCorruptionError("conversation guidance input index is unreadable")
        if report.payload:
            if report.payload != expected:
                raise DataCorruptionError("conversation guidance input index conflicts")
            return
        write_json_file_atomic(index_path, expected)

    # LLM: Recovery follows one exact owner-scoped reverse index and never scans prose or all
    # historical receipts to guess which steer belongs to a Gateway input.
    # 函数用途: 按 Gateway 普通消息请求 ID 查询已建立的 guidance 回执和精确回合。
    def guidance_receipt_for_gateway_input(
        self,
        gateway_input_request_id: str,
    ) -> tuple[GuidanceOnceReceipt | None, str]:
        input_request_id = str(gateway_input_request_id or "").strip()
        if not input_request_id:
            return None, ""
        index_path = self._guidance_input_index_path(input_request_id)
        report = read_json_file_report(index_path, context="conversation.guidance_input_index.read")
        if report.load_error is not None:
            raise DataCorruptionError("conversation guidance input index is unreadable")
        if not report.payload:
            return None, ""
        if (
            report.payload.get("schema_version") != "conversation_guidance_input_ref.v1"
            or str(report.payload.get("gateway_input_request_id") or "") != input_request_id
        ):
            raise DataCorruptionError("conversation guidance input index is invalid")
        dedupe_key = str(report.payload.get("dedupe_key") or "").strip()
        turn_id = str(report.payload.get("expected_turn_id") or "").strip()
        if not dedupe_key:
            raise DataCorruptionError("conversation guidance input index has no receipt key")
        return self.guidance_once_receipt(dedupe_key), turn_id

    # LLM: Recovery compares the exact prepared entry before appending. A duplicate id with
    # different content is corruption, while an identical row is a successful crash replay.
    # 函数用途: 确保回执对应的 guidance 队列行存在且只存在一次。
    def _ensure_guidance_once_entry(self, entry: GuidanceEntry) -> None:
        path = self._guidance_path(entry.target_type, entry.target_id)
        if not path.exists():
            append_jsonl(path, entry.to_dict(), sort_keys=True)
            return
        report = read_jsonl_report(path, context="conversation.guidance_once.queue")
        if report.load_errors:
            raise DataCorruptionError(
                f"conversation guidance queue is unreadable: {entry.target_type}:{entry.target_id}"
            )
        matches = [
            GuidanceEntry.from_dict(row)
            for row in report.rows
            if str(row.get("guidance_id") or "") == entry.guidance_id
        ]
        if matches:
            if any(item.to_dict() != entry.to_dict() for item in matches) or len(matches) != 1:
                raise DataCorruptionError(
                    f"conversation guidance id has conflicting rows: {entry.guidance_id}"
                )
            return
        append_jsonl(path, entry.to_dict(), sort_keys=True)

    def recent_guidance(
        self,
        target_type: str,
        target_id: str,
        *,
        limit: int = 20,
        include_delivered: bool = True,
    ) -> list[GuidanceEntry]:
        entries, _errors = self.recent_guidance_report(
            target_type,
            target_id,
            limit=limit,
            include_delivered=include_delivered,
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
            return [], [
                {"code": "GUIDANCE_TARGET_TYPE_INVALID", "target_type": str(target_type or "")}
            ]
        delivered = self._read_guidance_delivered()
        report = read_jsonl_report(
            self._guidance_path(normalized_type, str(target_id)),
            context="conversation.guidance.read",
        )
        entries, parse_errors = _guidance_entries(report.rows, delivered)
        if not include_delivered:
            pending_entries: list[GuidanceEntry] = []
            for item in entries:
                try:
                    if self._guidance_entry_is_pending(item):
                        pending_entries.append(item)
                except Exception as exc:
                    error = runtime_error_report(exc, context="conversation.guidance.receipt")
                    error["guidance_id"] = item.guidance_id
                    parse_errors.append(error)
            entries = pending_entries
        selected = entries if limit <= 0 else entries[-limit:]
        return selected, [*report.load_errors, *parse_errors]

    # LLM: For idempotent ingress the receipt, not the delivered side index, is authoritative.
    # Non-idempotent internal guidance keeps the established delivered-index behavior.
    # 函数用途: 判断一条 guidance 是否仍可被运行时读取，拒绝和已消费记录不会再次注入。
    def _guidance_entry_is_pending(self, entry: GuidanceEntry) -> bool:
        metadata = entry.metadata if isinstance(entry.metadata, dict) else {}
        dedupe_key = str(metadata.get("dedupe_key") or "").strip()
        if not dedupe_key:
            return entry.delivered_at <= 0
        receipt = self.guidance_once_receipt(dedupe_key)
        if receipt is None or receipt.entry.guidance_id != entry.guidance_id:
            raise DataCorruptionError("conversation guidance receipt entry mismatch")
        return receipt.status == "pending"

    def pending_guidance(
        self, target_type: str, target_id: str, *, limit: int = 20
    ) -> list[GuidanceEntry]:
        return self.recent_guidance(target_type, target_id, limit=limit, include_delivered=False)

    def pending_guidance_report(
        self, target_type: str, target_id: str, *, limit: int = 20
    ) -> tuple[list[GuidanceEntry], list[dict[str, Any]]]:
        return self.recent_guidance_report(
            target_type, target_id, limit=limit, include_delivered=False
        )

    def mark_guidance_delivered(
        self, guidance_ids: list[str] | tuple[str, ...], *, now: float | None = None
    ) -> None:
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
        severity=kwargs.get("severity")
        or (observation.severity if observation is not None else ""),
        reason=str(kwargs.get("reason") or "agent_event"),
        source_agent_id=kwargs.get("source_agent_id")
        or (observation.source_agent_id if observation is not None else ""),
        parent_agent_id=kwargs.get("parent_agent_id")
        or (observation.parent_agent_id if observation is not None else ""),
        root_task_id=kwargs.get("root_task_id")
        or (observation.root_task_id if observation is not None else ""),
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
_GOAL_COLLECTION_SCHEMA_VERSION = 2


# LLM: One per-thread collection is the single goal authority; legacy single-goal
# records are read only for deployed-state migration and are rewritten on mutation.
# 函数用途: 读取并校验一个会话的全部持续目标，兼容已部署的单目标旧记录。
def _read_thread_goals_report(path: Path) -> tuple[list[ThreadGoal], dict[str, Any] | None]:
    payload, error = _read_json_object_report(path, context="conversation.goal.read")
    if error is not None or not payload:
        return [], error
    return _goals_from_update_data(payload, path=path)


def _goal_collection_payload(goals: list[ThreadGoal]) -> dict[str, Any]:
    return {
        "schema_version": _GOAL_COLLECTION_SCHEMA_VERSION,
        "goals": [goal.to_dict() for goal in goals],
    }


def _goals_from_update_data(
    payload: dict[str, Any],
    *,
    path: Path,
) -> tuple[list[ThreadGoal], dict[str, Any] | None]:
    if not payload:
        return [], None
    try:
        if "goals" in payload:
            rows = payload.get("goals")
            if payload.get("schema_version") != _GOAL_COLLECTION_SCHEMA_VERSION or not isinstance(
                rows, list
            ):
                raise DataCorruptionError(f"thread goal collection is invalid: {path}")
        else:
            rows = [payload]
        goals: list[ThreadGoal] = []
        seen_ids: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                raise DataCorruptionError(f"thread goal row is invalid: {path}")
            goal = ThreadGoal.from_dict(row)
            if goal.status == _LEGACY_CLEARED_GOAL_STATUS:
                continue
            if not goal.goal_id or not goal.thread_id or not goal.task_id:
                raise DataCorruptionError(f"thread goal identity is invalid: {path}")
            if goal.status not in THREAD_GOAL_STATUSES:
                raise DataCorruptionError(f"thread goal status is invalid: {goal.status}")
            if goal.goal_id in seen_ids:
                raise DataCorruptionError(f"duplicate thread goal id: {goal.goal_id}")
            seen_ids.add(goal.goal_id)
            goals.append(goal)
        return goals, None
    except Exception as exc:
        report = runtime_error_report(exc, context="conversation.goal.read")
        report["path"] = str(path)
        return [], report


def _goals_from_mutation_payload(payload: dict[str, Any]) -> list[ThreadGoal]:
    rows = payload.get("goals") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    return [ThreadGoal.from_dict(row) for row in rows if isinstance(row, dict)]


def _select_thread_goal(
    goals: list[ThreadGoal],
    *,
    goal_id: str = "",
    task_id: str = "",
    name: str = "",
) -> ThreadGoal | None:
    selected = goals
    if goal_id:
        selected = [goal for goal in selected if goal.goal_id == goal_id]
    if task_id:
        selected = [goal for goal in selected if goal.task_id == task_id]
    if name:
        folded = name.casefold()
        selected = [goal for goal in selected if goal.name.casefold() == folded]
    if goal_id or task_id or name:
        return selected[0] if len(selected) == 1 else None
    unfinished = [goal for goal in selected if goal.status in _UNFINISHED_GOAL_STATUSES]
    if len(unfinished) > 1:
        raise ValueError("multiple unfinished goals exist; select one by name or id")
    if unfinished:
        return unfinished[0]
    return max(selected, key=lambda goal: goal.updated_at, default=None)


# LLM: Goal clocks are process-local accounting helpers; durable goal state
# remains in the goal collection owned by ``ConversationGoalStore`` below.
# 类用途: 管理多个目标各自独立的进程内计时基线，不创建第二份持久事实源。
class ConversationGoalClockStore(ConversationGuidanceStore):
    """Process-local accounting clocks for persisted conversation goals."""

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
            prior = self._goal_clock.get(goal.goal_id)
            if reset or prior is None or prior[0] != goal.thread_id:
                self._goal_clock[goal.goal_id] = (goal.thread_id, current)

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
            prior = self._goal_clock.get(goal.goal_id)
            if prior is None or prior[0] != goal.thread_id:
                self._goal_clock[goal.goal_id] = (goal.thread_id, current)
                return 0
            elapsed = max(0, int(current - prior[1]))
            if elapsed > 0:
                self._goal_clock[goal.goal_id] = (goal.thread_id, prior[1] + elapsed)
            return elapsed

    def current_goal_time_seconds(
        self,
        goal: ThreadGoal,
        *,
        monotonic_now: float | None = None,
    ) -> int:
        """Return persisted plus current live seconds without advancing accounting."""
        if goal.status != "active":
            return max(0, int(goal.time_used_seconds))
        current = time.monotonic() if monotonic_now is None else float(monotonic_now)
        with self._goal_clock_lock:
            prior = self._goal_clock.get(goal.goal_id)
            live = (
                max(0, int(current - prior[1]))
                if prior is not None and prior[0] == goal.thread_id
                else 0
            )
        return max(0, int(goal.time_used_seconds)) + live

    # LLM: Paused, blocked, limited, completed, replaced, or cleared goals stop
    # the active runtime clock without changing their durable usage snapshot.
    # 函数用途: 清除指定目标的运行时计时状态，避免后续目标继承旧基线。
    def clear_goal_accounting(self, thread_id: str, *, goal_id: str = "") -> None:
        with self._goal_clock_lock:
            if goal_id:
                prior = self._goal_clock.get(goal_id)
                if prior is not None and prior[0] == thread_id:
                    self._goal_clock.pop(goal_id, None)
                return
            stale = [
                current_goal_id
                for current_goal_id, (current_thread_id, _started_at) in self._goal_clock.items()
                if current_thread_id == thread_id
            ]
            for current_goal_id in stale:
                self._goal_clock.pop(current_goal_id, None)


# LLM: This mixin is the single persistence authority for the `/goal` overlay of a conversation thread.
# 类用途: 持久化当前会话的持续目标，并提供原子生命周期与用量计量。
class ConversationGoalStore(ConversationGoalClockStore):
    """Persistent `/goal` overlay for an existing conversation thread."""

    # LLM: All load-plus-mutate goal operations for one thread share this filesystem transition lock.
    # 函数用途: 为单个 thread 的目标查看和迁移提供跨线程/跨进程临界区。
    def goal_transition_guard(self, thread_id: str):
        normalized = safe_file_stem(str(thread_id or "").strip())
        if not normalized:
            raise ValueError("thread_id is required")
        return locked_file_transition(self.goals_dir / f".{normalized}.transition")

    # LLM: Strict callers receive corruption as an exception rather than an apparent empty goal.
    # 函数用途: 严格读取当前 thread 目标，损坏时 fail-closed。
    def load_goal(
        self,
        thread_id: str,
        *,
        goal_id: str = "",
        task_id: str = "",
        name: str = "",
    ) -> ThreadGoal | None:
        goals, error = self.load_goals_report(thread_id)
        if error is not None:
            raise DataCorruptionError(str(error.get("message") or "conversation goal read failed"))
        goal = _select_thread_goal(
            goals,
            goal_id=str(goal_id or "").strip(),
            task_id=str(task_id or "").strip(),
            name=str(name or "").strip(),
        )
        if goal is not None and goal.status == "active":
            self.begin_goal_accounting(goal)
        elif goal is not None and goal.status not in {"active", "budget_limited"}:
            self.clear_goal_accounting(goal.thread_id, goal_id=goal.goal_id)
        return goal

    def load_goals(self, thread_id: str) -> list[ThreadGoal]:
        goals, error = self.load_goals_report(thread_id)
        if error is not None:
            raise DataCorruptionError(str(error.get("message") or "conversation goal read failed"))
        for goal in goals:
            if goal.status == "active":
                self.begin_goal_accounting(goal)
        return goals

    # LLM: Request assembly uses the report form so it can expose a typed load failure without mutating state.
    # 函数用途: 读取全部目标与结构化错误，供 Gateway 状态和上下文装配使用。
    def load_goals_report(self, thread_id: str) -> tuple[list[ThreadGoal], dict[str, Any] | None]:
        normalized = str(thread_id or "").strip()
        self._require_thread(normalized)
        return _read_thread_goals_report(self._goal_path(normalized))

    def load_goal_report(self, thread_id: str) -> tuple[ThreadGoal | None, dict[str, Any] | None]:
        goals, error = self.load_goals_report(thread_id)
        if error is not None:
            return None, error
        try:
            return _select_thread_goal(goals), None
        except ValueError as exc:
            report = runtime_error_report(exc, context="conversation.goal.select")
            report["thread_id"] = thread_id
            return None, report

    # LLM: Named goals may coexist; unnamed model-created goals retain the old
    # single-active constraint so a model cannot silently fan out durable work.
    # 函数用途: 原子创建一个命名持续目标；同名活跃目标和含糊的无名并发都会拒绝。
    def create_goal(self, request: dict[str, Any]) -> ThreadGoal:
        thread_id = str(request.get("thread_id") or "").strip()
        objective = str(request.get("objective") or "").strip()
        name = str(request.get("name") or "").strip()
        self._require_thread(thread_id)
        if not objective:
            raise ValueError("goal objective is required")
        if len(name) > 64 or "\n" in name or "\r" in name:
            raise ValueError("goal name must be at most 64 characters on one line")
        if len(objective) > THREAD_GOAL_OBJECTIVE_MAX_CHARS:
            raise ValueError(f"goal objective exceeds {THREAD_GOAL_OBJECTIVE_MAX_CHARS} characters")
        token_budget_value = request.get("token_budget")
        token_budget = int(token_budget_value) if token_budget_value is not None else None
        if token_budget is not None and token_budget <= 0:
            raise ValueError("goal token_budget must be positive")
        duration_value = request.get("duration_seconds")
        duration_seconds = int(duration_value) if duration_value is not None else None
        if duration_seconds is not None and duration_seconds <= 0:
            raise ValueError("goal duration_seconds must be positive")
        current_time = now(request.get("now"))
        created: ThreadGoal | None = None

        def updater(data: dict[str, Any]) -> dict[str, Any]:
            nonlocal created
            existing, error = _goals_from_update_data(
                data,
                path=self._goal_path(thread_id),
            )
            if error is not None:
                raise DataCorruptionError(
                    str(error.get("message") or "conversation goal read failed")
                )
            unfinished = [goal for goal in existing if goal.status in _UNFINISHED_GOAL_STATUSES]
            if name:
                if any(goal.name.casefold() == name.casefold() for goal in unfinished):
                    raise ValueError(f"an unfinished goal named {name!r} already exists")
            elif unfinished:
                raise ValueError("an unfinished unnamed goal already exists for this thread")
            goal_id = new_id("goal")
            created = ThreadGoal(
                goal_id=goal_id,
                thread_id=thread_id,
                objective=objective,
                task_id=str(request.get("task_id") or f"goal-task-{goal_id.removeprefix('goal-')}"),
                name=name,
                token_budget=token_budget,
                duration_seconds=duration_seconds,
                created_at=current_time,
                updated_at=current_time,
                metadata=request.get("metadata")
                if isinstance(request.get("metadata"), dict)
                else {},
            )
            return _goal_collection_payload([*existing, created])

        payload = update_json_file_atomic(self._goal_path(thread_id), updater)
        goals = _goals_from_mutation_payload(payload)
        goal = created or _select_thread_goal(goals, task_id=str(request.get("task_id") or ""))
        if goal is None:
            raise DataCorruptionError(f"created goal is missing: {thread_id}")
        self.begin_goal_accounting(goal, reset=True)
        return goal

    # LLM: Compare expected goal/status before changing the objective or system-owned lifecycle.
    # 函数用途: 以 CAS 语义修改目标内容或状态，竞态失败返回空。
    def update_goal(self, request: dict[str, Any]) -> ThreadGoal | None:
        thread_id = str(request.get("thread_id") or "").strip()
        requested_status = str(request.get("status") or "").strip().lower()
        expected_status = str(request.get("expected_status") or "").strip().lower()
        expected_goal_id = str(
            request.get("goal_id") or request.get("expected_goal_id") or ""
        ).strip()
        objective = str(request.get("objective") or "").strip()
        if requested_status and requested_status not in THREAD_GOAL_STATUSES:
            raise ValueError(f"unsupported goal status: {requested_status}")
        if objective and len(objective) > THREAD_GOAL_OBJECTIVE_MAX_CHARS:
            raise ValueError(f"goal objective exceeds {THREAD_GOAL_OBJECTIVE_MAX_CHARS} characters")
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
        updated_goal: ThreadGoal | None = None

        def updater(data: dict[str, Any]) -> dict[str, Any]:
            nonlocal changed, previous_status, updated_goal
            if not data:
                return data
            goals, error = _goals_from_update_data(data, path=self._goal_path(thread_id))
            if error is not None:
                raise DataCorruptionError(
                    str(error.get("message") or "conversation goal read failed")
                )
            current = _select_thread_goal(goals, goal_id=expected_goal_id)
            if current is None:
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
            next_time_used = current.time_used_seconds + elapsed
            if (
                status == "active"
                and current.duration_seconds is not None
                and next_time_used >= current.duration_seconds
            ):
                status = "budget_limited"
            updated_goal = replace(
                current,
                objective=objective or current.objective,
                status=status,
                token_budget=next_budget,
                time_used_seconds=next_time_used,
                updated_at=current_time,
            )
            return _goal_collection_payload(
                [updated_goal if goal.goal_id == current.goal_id else goal for goal in goals]
            )

        update_json_file_atomic(self._goal_path(thread_id), updater, require_existing=True)
        if not changed:
            return None
        updated = updated_goal
        if updated is None:
            return None
        if updated.status == "active":
            self.begin_goal_accounting(updated, reset=previous_status != "active")
        else:
            self.clear_goal_accounting(updated.thread_id, goal_id=updated.goal_id)
        return updated

    # LLM: Clear removes one exact goal from the per-thread collection; sibling
    # goals, task evidence, and transcript remain intact.
    # 函数用途: 以目标编号 CAS 删除一个持续目标，不影响同会话其他目标。
    def delete_goal(self, thread_id: str, *, expected_goal_id: str = "") -> ThreadGoal | None:
        normalized = str(thread_id or "").strip()
        self._require_thread(normalized)
        path = self._goal_path(normalized)
        deleted: ThreadGoal | None = None

        def updater(data: dict[str, Any]) -> dict[str, Any]:
            nonlocal deleted
            goals, error = _goals_from_update_data(data, path=path)
            if error is not None:
                raise DataCorruptionError(
                    str(error.get("message") or "conversation goal read failed")
                )
            selected = _select_thread_goal(goals, goal_id=expected_goal_id)
            if selected is None:
                return data
            deleted = selected
            return _goal_collection_payload(
                [goal for goal in goals if goal.goal_id != selected.goal_id]
            )

        update_json_file_atomic(path, updater, require_existing=True)
        if deleted is not None:
            self.clear_goal_accounting(normalized, goal_id=deleted.goal_id)
        return deleted

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
            return self.load_goal(thread_id, goal_id=expected_goal_id)
        current_time = now(request.get("now"))
        changed = False
        updated_goal: ThreadGoal | None = None

        def updater(data: dict[str, Any]) -> dict[str, Any]:
            nonlocal changed, updated_goal
            if not data:
                return data
            goals, error = _goals_from_update_data(data, path=self._goal_path(thread_id))
            if error is not None:
                raise DataCorruptionError(
                    str(error.get("message") or "conversation goal read failed")
                )
            current = _select_thread_goal(goals, goal_id=expected_goal_id)
            if current is None or current.status not in allowed_statuses:
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
            time_used = current.time_used_seconds + time_delta
            if (
                current.status == "active"
                and current.duration_seconds is not None
                and time_used >= current.duration_seconds
            ):
                status = "budget_limited"
            updated_goal = replace(
                current,
                tokens_used=tokens_used,
                time_used_seconds=time_used,
                status=status,
                updated_at=current_time,
            )
            return _goal_collection_payload(
                [updated_goal if goal.goal_id == current.goal_id else goal for goal in goals]
            )

        update_json_file_atomic(self._goal_path(thread_id), updater, require_existing=True)
        return updated_goal if changed else None


class ConversationWakeStore(ConversationGoalStore):
    # LLM: Wake and observation ledgers retain their publish order, while the thread activity
    # projection must merge with the newest record on both success and fallback paths.
    # 函数用途: 原子发布观察与唤醒信号，并防止迟到的后台事件回滚当前会话工作区。
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
            self._update_thread_atomic(
                thread.thread_id,
                lambda latest: replace(
                    latest,
                    updated_at=max(latest.updated_at, observed_at),
                ),
            )
            raise
        linked = replace(observation, wake_signal_id=selected.wake_signal_id)
        if selected.wake_signal_id == signal.wake_signal_id:
            append_jsonl(self._observation_path(thread_id), linked.to_dict(), sort_keys=True)
            self._update_thread_atomic(
                thread.thread_id,
                lambda latest: replace(
                    latest,
                    updated_at=max(latest.updated_at, observed_at),
                ),
            )
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

    def pending_wake_signals(
        self, *, limit: int = 100, include_normal: bool = True
    ) -> list[WakeSignal]:
        signals, _load_errors = self.pending_wake_signals_report(
            limit=limit, include_normal=include_normal
        )
        return signals

    def pending_wake_signal(self, wake_signal_id: str) -> WakeSignal | None:
        """Return one exact pending wake without truncating a queue view."""

        return self._pending_wake_by_id(str(wake_signal_id or "").strip())

    def cache_pending_wake_delivery(
        self,
        wake_signal_id: str,
        delivery: dict[str, Any],
    ) -> WakeSignal | None:
        """Freeze one model-authored owner payload on its existing durable wake."""

        selected_id = str(wake_signal_id or "").strip()
        path = self._find_wake_signal_path(selected_id)
        if path is None:
            return None
        cached: WakeSignal | None = None

        def updater(data: dict[str, Any]) -> dict[str, Any]:
            nonlocal cached
            signal = WakeSignal.from_dict(data)
            if signal.wake_signal_id != selected_id or signal.status != "pending":
                return data
            metadata = dict(signal.metadata or {})
            existing = metadata.get("owner_delivery")
            if isinstance(existing, dict):
                cached = signal
                return data
            metadata["owner_delivery"] = dict(delivery)
            cached = replace(signal, metadata=metadata)
            return cached.to_dict()

        update_json_file_atomic(path, updater, require_existing=True)
        return cached

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

    def mark_wake_signal_handled(
        self, wake_signal_id: str, *, now: float | None = None
    ) -> WakeSignal | None:
        path = self._find_wake_signal_path(wake_signal_id)
        if path is None or not (data := read_json_file(path)):
            return None
        current = now if now is not None else time.time()
        handled = replace(WakeSignal.from_dict(data), status="handled", handled_at=current)
        write_json_file_atomic(
            self.wake_handled_dir / f"{handled.wake_signal_id}.json", handled.to_dict()
        )
        _unlink_quietly(path)
        if handled.observation_id:
            self.mark_observations_handled([handled.observation_id], now=handled.handled_at)
        return handled

    def _find_wake_signal_path(self, wake_signal_id: str) -> Path | None:
        name = f"{wake_signal_id}.json"
        return next(
            (
                path
                for kind in ("urgent", "normal")
                if (path := self.wake_queue_dir / kind / name).exists()
            ),
            None,
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

        update_json_file_atomic(
            self._wake_dedupe_path(signal.thread_id, signal.dedupe_key), updater
        )
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

# 失败续跑记账(问题6「失败自动续跑记账不正确」)机制层根因:失败 run 的异常在
# _consume_with_supply_guard 被吸收后 policy.next_due_at 不动 → 下个 tick 又 due →
# 失败无限重试。修复=失败事实落账:next_due_at 推到 now+backoff(指数退避,抖动由
# 调度层按 policy_id 确定性派生),连续失败达 retire_after 次 → enabled=False 退休
# (等用户,绝不停机式无限重试)。成功路径由调度层 mark_progress_reported(failure_count=0)
# 清零复原,退休 policy 被 disable 后不再出现在 due 扫描,账目保留在 metadata 供复盘。
_POLICY_FAILURE_RETIRE_AFTER = 3

# 陈旧账本保留期(问题8):disabled policy / finished claim 超 7 天归档。
# 够复盘诊断,又不让 ledger 无限累积(真机 2026-08-09 一个线程堆出 182 个
# disabled policy 文件)。归档是移动不是删除,随时可回滚。
_LEDGER_GC_RETENTION_SECONDS = 7 * 24 * 3600
_LEDGER_ARCHIVE_DIR = ".ledger_archive"


def _archive_ledger_file(src: Path, archive_dir: Path) -> bool:
    """把单个账本文件(及其 .lock)移入归档目录;文件已被并发清走则视为成功(幂等)。"""
    try:
        archive_dir.mkdir(parents=True, exist_ok=True)
        src.rename(archive_dir / src.name)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    lock = Path(f"{src}.lock")
    try:
        if lock.exists():
            lock.rename(archive_dir / lock.name)
    except OSError:
        pass
    return True


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

    # 陈旧账本归档(问题8):disabled policy 超保留期移到 .ledger_archive/ 子目录。
    # 用文件系统移动而非删除——零破坏、可回滚;归档目录与扫描目录同级,glob("*.json")
    # 不递归,归档项自动从 list/due 扫描消失。判龄用 max(last_report_at, next_due_at):
    # disable 时写 last_report_at=当前时间,旧数据可能没有,回落 next_due_at 从严不误删。
    def gc_stale_ledger_records(
        self,
        *,
        now: float | None = None,
        retention_seconds: float = _LEDGER_GC_RETENTION_SECONDS,
    ) -> dict[str, int]:
        current = now if now is not None else time.time()
        archived_policies = 0
        policies, _ = self.list_progress_policies_report()
        for policy in policies:
            if policy.enabled:
                continue
            age = current - max(policy.last_report_at, policy.next_due_at)
            if age <= retention_seconds:
                continue
            if _archive_ledger_file(
                self._policy_path(policy.policy_id),
                self.policies_dir.parent / _LEDGER_ARCHIVE_DIR / "policies",
            ):
                archived_policies += 1
        archived_claims = 0
        for path in sorted(self.background_claims_dir.glob("*.json")):
            claim, error = _read_claim_report(path)
            if error is not None or not claim:
                continue
            if str(claim.get("status") or "") not in _FINISH_STATUSES:
                continue
            finished_at = float(claim.get("finished_at") or 0.0)
            if finished_at <= 0 or current - finished_at <= retention_seconds:
                continue
            if _archive_ledger_file(path, self.background_claims_dir.parent / _LEDGER_ARCHIVE_DIR / "claims"):
                archived_claims += 1
        return {"archived_policies": archived_policies, "archived_claims": archived_claims}

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

    def mark_progress_failed(
        self,
        policy_id: str,
        *,
        now: float | None = None,
        backoff_seconds: float,
        failure_count: int,
        retire_after: int = _POLICY_FAILURE_RETIRE_AFTER,
    ) -> ProgressPolicy | None:
        """失败续跑记账:写 failure_count/last_failure_at 并退避顺延,超阈值退休。

        backoff_seconds 由调度层按 5min×2^(n-1) 上限 1h 计算(含按 policy_id 确定性
        派生的抖动);本方法只落账,不自己算退避——退避公式是调度策略,存层只持久化。
        连续失败达 retire_after → enabled=False 退休(不再进 due 扫描),失败账目保留
        在 metadata(供复盘)。成功路径 mark_progress_reported 清零复原。
        """
        policy = self.get_progress_policy(policy_id)
        if policy is None:
            return None
        current = now if now is not None else time.time()
        metadata = dict(policy.metadata or {})
        metadata["failure_count"] = max(0, int(failure_count))
        metadata["last_failure_at"] = current
        metadata["last_backoff_seconds"] = float(backoff_seconds)
        retired = int(failure_count) >= max(1, int(retire_after))
        if retired:
            metadata["retired_at"] = current
        updated = replace(
            policy,
            enabled=not retired,
            last_report_at=current,
            next_due_at=current + float(backoff_seconds),
            metadata=metadata,
        )
        write_json_file_atomic(self._policy_path(policy_id), updated.to_dict())
        return updated

    def update_progress_policy_atomic(
        self,
        policy_id: str,
        updater: Callable[[ProgressPolicy], ProgressPolicy | None],
    ) -> tuple[ProgressPolicy | None, bool, bool]:
        """锁内 CAS 更新一个进度策略（flock 跨进程互斥，读-改-写原子）。

        2026-08-14 双席复核硬门1（严格 CAS）：try_claim_cli_resume 此前是
        先读 policy、再 write_json_file_atomic——两个 CLI/gateway 消费者
        并发时读-改-写窗口内互相覆盖，无 expected-version 比对。此方法用
        gateway_parts.io 的 update_json_file_atomic（fcntl.flock 锁内
        读-改-写）保证同文件系统内任意消费者的原子互斥。
        返回值 (policy, changed, aborted)：
        - aborted=True = updater 返回 None（条件不满足，明确放弃，不落盘）
        - changed=True = 锁内比对后内容真正写盘
        - policy=None = 查无此 policy（require_existing 失败）
        """
        changed = False
        aborted = False

        def _wrap(current: dict[str, Any]) -> dict[str, Any]:
            nonlocal changed, aborted
            policy = ProgressPolicy.from_dict(current)
            updated = updater(policy)
            if updated is None:
                aborted = True
                return current  # 条件不满足: 原样返回, 内容无变更
            result = updated.to_dict()
            changed = result != current
            return result

        try:
            update_json_file_atomic(
                self._policy_path(policy_id),
                _wrap,
                require_existing=True,
            )
        except (FileNotFoundError, KeyError):
            return None, False, False
        return self.get_progress_policy(policy_id), changed, aborted

    def disable_progress_policy(
        self, policy_id: str, *, now: float | None = None
    ) -> ProgressPolicy | None:
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
    claim_scope_id: str = ""
    owner_process: dict[str, object] = field(default_factory=dict)


def _new_claim(payload: BackgroundClaimPayload) -> dict[str, Any]:
    return {
        "schema_version": "background_run_claim.v1",
        "claim_id": new_id("bgclaim"),
        "thread_id": payload.thread_id,
        "claim_scope_id": payload.claim_scope_id or payload.thread_id,
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
        "owner_process": dict(data.get("owner_process"))
        if isinstance(data.get("owner_process"), dict)
        else {},
        "last_error": _error_payload(data.get("last_error")),
        "takeover": data.get("takeover")
        if isinstance(data.get("takeover"), dict)
        else _takeover_payload(status),
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
        claim_scope_id = str(request.get("claim_scope_id") or thread.thread_id).strip()
        current = now(request.get("now"))
        lease = _claim_lease_seconds(request.get("lease_seconds"))
        claim = _new_claim(
            BackgroundClaimPayload(
                thread_id=thread.thread_id,
                reason=str(request.get("reason") or ""),
                current=current,
                lease=lease,
                task_id=str(request.get("task_id") or ""),
                claim_scope_id=claim_scope_id,
                owner_process=build_process_identity(),
            )
        )
        claimed = False

        def updater(data: dict[str, Any]) -> dict[str, Any]:
            nonlocal claimed
            active = (
                str(data.get("status") or "") == "running"
                and float_value(data.get("expires_at")) > current
            )
            owner_stale = active and process_identity_is_live(data.get("owner_process")) is False
            if active and not owner_stale:
                claimed = False
                return data
            claimed = True
            acquisition_reason = (
                "owner_process_stale" if owner_stale else _claim_acquisition_reason(data, current)
            )
            return {
                **claim,
                "acquisition": {"reason": acquisition_reason},
                "previous_claim": _previous_claim_summary(data, current),
            }

        updated = update_json_file_atomic(
            self._background_claim_path(claim_scope_id),
            updater,
        )
        return updated if claimed else None

    def load_background_run_claim(
        self,
        thread_id: str,
        *,
        claim_scope_id: str = "",
    ) -> dict[str, Any]:
        claim, load_error = self.load_background_run_claim_report(
            thread_id,
            claim_scope_id=claim_scope_id,
        )
        return claim if not load_error else {"load_error": load_error}

    def load_background_run_claim_report(
        self,
        thread_id: str,
        *,
        claim_scope_id: str = "",
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        self._require_thread(str(thread_id or ""))
        selected_scope = str(claim_scope_id or thread_id or "").strip()
        return _read_claim_report(self._background_claim_path(selected_scope))

    def renew_background_run_claim(self, request: dict) -> dict[str, Any] | None:
        thread_id = str(request.get("thread_id") or "")
        claim_scope_id = str(request.get("claim_scope_id") or thread_id).strip()
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

        updated = update_json_file_atomic(
            self._background_claim_path(claim_scope_id),
            updater,
        )
        return updated if renewed else None

    def finish_background_run(self, request: dict) -> dict[str, Any] | None:
        thread_id = str(request.get("thread_id") or "")
        claim_scope_id = str(request.get("claim_scope_id") or thread_id).strip()
        claim_id = str(request.get("claim_id") or "")
        # 收尾（释放租约/记失败事实）只作用于 claim 文件，不依赖线程仍可读。这条在 `_run_with_heartbeat`
        # 的 finally 里跑：若线程在长跑中变不可读还硬 `_require_thread`，会二次抛 KeyError 盖掉真正的 run
        # 错误、并再次崩后台清理。改为对已存在的 claim 文件收尾；无 claim 文件则无可收尾直接返回 None。
        claim_path = self._background_claim_path(claim_scope_id)
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
        runtime_facts = (
            request.get("runtime_facts") if isinstance(request.get("runtime_facts"), dict) else {}
        )
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
        self,
        thread_id: str,
        *,
        recent_limit: int = 20,
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        thread = self._require_thread(thread_id)
        messages, message_errors = self.recent_messages_report(thread_id, limit=recent_limit)
        tasks, task_errors = self.task_links_report(thread_id)
        observations, observation_errors = self.recent_observations_report(
            thread_id, limit=recent_limit
        )
        guidance, guidance_errors = self.pending_guidance_report(
            "thread", thread_id, limit=recent_limit
        )
        goals, goal_error = self.load_goals_report(thread_id)
        return {
            "thread": thread.to_dict(),
            "messages": [item.to_dict() for item in messages],
            "tasks": [item.to_dict() for item in tasks],
            "channel_bindings": [item.to_dict() for item in thread.channel_bindings],
            "observations": [item.to_dict() for item in observations],
            "guidance": [item.to_dict() for item in guidance],
            "goals": [goal.to_dict() for goal in goals],
        }, [
            *message_errors,
            *task_errors,
            *observation_errors,
            *guidance_errors,
            *([goal_error] if goal_error is not None else []),
        ]
