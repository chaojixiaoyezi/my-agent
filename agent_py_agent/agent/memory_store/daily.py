from __future__ import annotations

"""后台策展生成的 owner 每日经历账本。"""

# LLM: daily 是非权威经历摘要；不得写完整对话、工具大输出或长期记忆操作镜像。
# 模块用途: 给同一 owner 的每日事件分配稳定 ID、previous_event_id 和 sequence 并幂等落盘。

import hashlib
import json
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from ..common.json_io import (
    locked_json_path,
    read_jsonl_objects_report,
    write_text_file_atomic_unlocked,
)
from ..user_space.owner_quota import OwnerQuotaChange, OwnerQuotaEnforcer
from .candidate_models import normalize_iso_time, normalize_reference_list, normalize_string_list

DAILY_MEMORY_SCHEMA_VERSION = "my-agent.daily-memory.v2"
DAILY_EVENT_TYPES = frozenset(
    {
        "conversation",
        "decision",
        "task_progress",
        "tool_result",
        "lesson",
        "todo",
        "summary",
        "warning",
        "error",
    }
)
DAILY_ACTORS = frozenset({"user", "main_agent", "subagent", "tool", "system"})
DAILY_ORIGINS = frozenset(
    {"user_explicit", "tool_verified", "model_inferred", "subagent_finding", "reviewed"}
)
_MAX_SUMMARY_CHARS = 1_500
_MAX_LIST_ITEMS = 24
_MAX_LIST_ITEM_CHARS = 500


# LLM: event_id/previous_event_id/sequence 由 DailyMemoryStore 持有，模型输出不得决定账本顺序。
# 类用途: 表示一条有界、带证据引用但不含原始大正文的每日经历摘要。
@dataclass(frozen=True)
class DailyMemoryEvent:
    event_type: str
    summary: str
    actor: str
    origin: str = "model_inferred"
    session_id: str = ""
    thread_id: str = ""
    request_id: str = ""
    task_id: str = ""
    run_id: str = ""
    message_refs: tuple[dict[str, object], ...] | list[dict[str, object]] = ()
    tool_refs: tuple[dict[str, object], ...] | list[dict[str, object]] = ()
    artifact_refs: tuple[dict[str, object], ...] | list[dict[str, object]] = ()
    decisions: tuple[str, ...] | list[str] = ()
    lessons: tuple[str, ...] | list[str] = ()
    next_actions: tuple[str, ...] | list[str] = ()
    created_at: str = ""
    extracted_at: str = ""
    curator_run_id: str = ""
    event_id: str = ""
    previous_event_id: str = ""
    sequence: int = 0
    schema_version: str = DAILY_MEMORY_SCHEMA_VERSION

    # LLM: 序列化前必须拒绝未知枚举、长正文式字段和无界列表。
    # 函数用途: 输出 daily JSONL 的稳定记录。
    def to_record(self) -> dict[str, object]:
        normalized = normalize_daily_event(self)
        if not normalized.event_id or normalized.sequence <= 0:
            raise ValueError("daily event ordering must be assigned by DailyMemoryStore")
        return asdict(normalized)

    # LLM: 旧 daily mirror 不得被默认为 v2 经历；迁移器必须显式转换或归档。
    # 函数用途: 严格恢复一个 v2 daily 事件。
    @classmethod
    def from_record(cls, payload: dict[str, object]) -> DailyMemoryEvent:
        known = cls.__dataclass_fields__
        try:
            event = cls(**{key: value for key, value in payload.items() if key in known})
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid daily memory event") from exc
        return normalize_daily_event(event, require_order=True)


# LLM: 任一 daily 坏行都阻断追加，防止原子改写时静默丢掉损坏经历。
# 类用途: 表示 daily 账本需要管理员迁移或修复。
class DailyMemoryStoreCorruptError(RuntimeError):
    pass


# LLM: 每日文件是 append 语义的当前账本，但实现必须在同一锁内完整校验、去重、原子替换。
# 类用途: 管理 owner 的 memory/daily/YYYY-MM-DD.jsonl。
class DailyMemoryStore:
    # LLM: 构造器只绑定 owner daily 根和 quota，不扫描或自动转换 legacy mirror。
    # 函数用途: 初始化当前 owner 的 Daily v2 仓库。
    def __init__(
        self,
        daily_dir: str | Path,
        *,
        quota_enforcer: OwnerQuotaEnforcer | None = None,
    ) -> None:
        self.daily_dir = Path(daily_dir)
        self.daily_dir.mkdir(parents=True, exist_ok=True)
        self.quota_enforcer = quota_enforcer

    # LLM: 相同 event_id 重放必须返回既有记录；相同 ID 不同内容必须 fail closed。
    # 函数用途: 为一条策展结果分配当天稳定顺序并幂等提交。
    def append(self, event: DailyMemoryEvent) -> DailyMemoryEvent:
        normalized = normalize_daily_event(event)
        path = daily_memory_path(self.daily_dir, day=normalized.created_at[:10])
        with _quota_admission(self.quota_enforcer) as admission:
            with locked_json_path(path):
                existing = _load_daily_unlocked(path)
                rows, committed = merge_daily_events(existing, [normalized])
                _write_daily_unlocked(path, rows, admission=admission)
                return committed[0]

    # LLM: 读取时验证整条 previous/sequence 链，不能按时间字符串猜并发顺序。
    # 函数用途: 返回某天按权威 sequence 排序的经历事件。
    def list(self, *, day: date | str) -> list[DailyMemoryEvent]:
        path = daily_memory_path(self.daily_dir, day=day)
        with locked_json_path(path):
            return _load_daily_unlocked(path)


# LLM: 日期只决定分片文件名，不承担事件先后关系。
# 函数用途: 解析 daily 目录中的标准日期文件。
def daily_memory_path(daily_dir: str | Path, *, day: date | str | None = None) -> Path:
    day_key = day.isoformat() if isinstance(day, date) else str(day or date.today().isoformat())
    try:
        date.fromisoformat(day_key)
    except ValueError as exc:
        raise ValueError("daily memory day must use YYYY-MM-DD") from exc
    return Path(daily_dir) / f"{day_key}.jsonl"


# LLM: 兼容的函数入口仍委托唯一 Store；不得恢复裸 append 或旧 refs schema。
# 函数用途: 提交 daily 事件并返回实际分片路径。
def append_daily_memory_event(daily_dir: str | Path, event: DailyMemoryEvent) -> Path:
    committed = DailyMemoryStore(daily_dir).append(event)
    return daily_memory_path(daily_dir, day=committed.created_at[:10])


# LLM: 稳定 ID 只取来源身份和事件内容；curator run、提取时间与 previous/sequence
# 都不参与，因此失败重放或进程重启仍命中同一事件。
# 函数用途: 为未显式分配 ID 的宿主事件生成重放稳定编号。
def stable_daily_event_id(event: DailyMemoryEvent) -> str:
    material = json.dumps(
        _daily_event_source_identity(event),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return "daily-event-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


# LLM: Curator transactions and single append share one pure ordering/idempotency function;
# failed replay therefore cannot generate a new ID or skip sequence values.
# 函数用途: 将同一天的一批事件合并到已验证账本并分配连续 previous/sequence。
def merge_daily_events(
    existing: list[DailyMemoryEvent],
    events: list[DailyMemoryEvent] | tuple[DailyMemoryEvent, ...],
) -> tuple[list[DailyMemoryEvent], list[DailyMemoryEvent]]:
    rows = list(existing)
    committed: list[DailyMemoryEvent] = []
    for event in events:
        normalized = normalize_daily_event(event)
        explicit_event_id = bool(normalized.event_id)
        event_id = normalized.event_id or stable_daily_event_id(normalized)
        normalized = replace(normalized, event_id=event_id)
        prior = next((item for item in rows if item.event_id == event_id), None)
        if prior is not None:
            same_source = _daily_event_source_identity(normalized) == _daily_event_source_identity(
                prior
            )
            same_payload = _daily_event_identity(normalized) == _daily_event_identity(prior)
            if not same_source or (explicit_event_id and not same_payload):
                raise ValueError("daily event_id collision with different content")
            committed.append(prior)
            continue
        previous = rows[-1].event_id if rows else ""
        committed_event = replace(
            normalized,
            previous_event_id=previous,
            sequence=(rows[-1].sequence + 1 if rows else 1),
        )
        rows.append(committed_event)
        committed.append(committed_event)
    return rows, committed


# LLM: Daily replay identity is based on canonical source refs and typed runtime scope, not on
# model wording; paraphrased extraction after a crash therefore keeps the first committed event.
# 函数用途: 生成 Daily 稳定 ID 使用的来源身份。
def _daily_event_source_identity(event: DailyMemoryEvent) -> dict[str, object]:
    message_refs = normalize_reference_list(event.message_refs)
    tool_refs = normalize_reference_list(event.tool_refs)
    artifact_refs = normalize_reference_list(event.artifact_refs)
    return {
        "event_type": str(event.event_type or "").strip().lower(),
        "actor": str(event.actor or "").strip().lower(),
        "origin": str(event.origin or "").strip().lower(),
        "session_id": str(event.session_id or "").strip(),
        "thread_id": str(event.thread_id or "").strip(),
        "request_id": str(event.request_id or "").strip(),
        "task_id": str(event.task_id or "").strip(),
        "run_id": str(event.run_id or "").strip(),
        "message_ids": sorted(str(ref.get("message_id") or "") for ref in message_refs),
        "tool_ids": sorted(
            str(
                ref.get("event_id")
                or ref.get("operation_id")
                or ref.get("tool_call_id")
                or ref.get("call_id")
                or ""
            )
            for ref in tool_refs
        ),
        "artifact_refs": sorted(
            json.dumps(ref, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            for ref in artifact_refs
        ),
    }


# LLM: 提取/提交时间和链式顺序是宿主运行元数据，不改变事件的幂等语义身份。
# 函数用途: 生成 daily event_id 和碰撞检查共用的规范化内容。
def _daily_event_identity(event: DailyMemoryEvent) -> dict[str, object]:
    return {
        "event_type": str(event.event_type or "").strip().lower(),
        "summary": " ".join(str(event.summary or "").split()),
        "actor": str(event.actor or "").strip().lower(),
        "origin": str(event.origin or "").strip().lower(),
        "session_id": str(event.session_id or "").strip(),
        "thread_id": str(event.thread_id or "").strip(),
        "request_id": str(event.request_id or "").strip(),
        "task_id": str(event.task_id or "").strip(),
        "run_id": str(event.run_id or "").strip(),
        "message_refs": normalize_reference_list(event.message_refs),
        "tool_refs": normalize_reference_list(event.tool_refs),
        "artifact_refs": normalize_reference_list(event.artifact_refs),
        "decisions": _bounded_text_list(event.decisions, "decisions"),
        "lessons": _bounded_text_list(event.lessons, "lessons"),
        "next_actions": _bounded_text_list(event.next_actions, "next_actions"),
    }


# LLM: daily 验证只接受摘要和短列表；引用复用统一禁止 content/output/body 的合同。
# 函数用途: 规范化一个每日事件并验证 host-owned 排序字段。
def normalize_daily_event(
    event: DailyMemoryEvent,
    *,
    require_order: bool = False,
) -> DailyMemoryEvent:
    if not isinstance(event, DailyMemoryEvent):
        raise TypeError("daily event must use DailyMemoryEvent")
    if event.schema_version != DAILY_MEMORY_SCHEMA_VERSION:
        raise ValueError("unsupported daily memory schema_version")
    event_type = str(event.event_type or "").strip().lower()
    actor = str(event.actor or "").strip().lower()
    origin = str(event.origin or "").strip().lower()
    summary = " ".join(str(event.summary or "").split())
    if event_type not in DAILY_EVENT_TYPES:
        raise ValueError(f"unsupported daily event_type: {event_type or '-'}")
    if actor not in DAILY_ACTORS:
        raise ValueError(f"unsupported daily actor: {actor or '-'}")
    if origin not in DAILY_ORIGINS:
        raise ValueError(f"unsupported daily origin: {origin or '-'}")
    if not summary or len(summary) > _MAX_SUMMARY_CHARS:
        raise ValueError("daily summary must contain 1..1500 characters")
    created_at = normalize_iso_time(event.created_at, default=_utc_now(), allow_empty=False)
    extracted_at = normalize_iso_time(event.extracted_at, default=_utc_now(), allow_empty=False)
    event_id = str(event.event_id or "").strip()
    previous_event_id = str(event.previous_event_id or "").strip()
    sequence = int(event.sequence or 0)
    if require_order and (not event_id or sequence <= 0):
        raise ValueError("daily event is missing authoritative ordering")
    if sequence < 0 or (sequence == 1 and previous_event_id):
        raise ValueError("invalid daily event sequence")
    return replace(
        event,
        event_type=event_type,
        summary=summary,
        actor=actor,
        origin=origin,
        session_id=str(event.session_id or "").strip(),
        thread_id=str(event.thread_id or "").strip(),
        request_id=str(event.request_id or "").strip(),
        task_id=str(event.task_id or "").strip(),
        run_id=str(event.run_id or "").strip(),
        message_refs=tuple(normalize_reference_list(event.message_refs)),
        tool_refs=tuple(normalize_reference_list(event.tool_refs)),
        artifact_refs=tuple(normalize_reference_list(event.artifact_refs)),
        decisions=tuple(_bounded_text_list(event.decisions, "decisions")),
        lessons=tuple(_bounded_text_list(event.lessons, "lessons")),
        next_actions=tuple(_bounded_text_list(event.next_actions, "next_actions")),
        created_at=created_at,
        extracted_at=extracted_at,
        curator_run_id=str(event.curator_run_id or "").strip(),
        event_id=event_id,
        previous_event_id=previous_event_id,
        sequence=sequence,
    )


# LLM: 文本列表用于简短结构化结论，不能成为大内容旁路。
# 函数用途: 限制 decisions/lessons/next_actions 的数量和单项长度。
def _bounded_text_list(value: object, field_name: str) -> list[str]:
    items = normalize_string_list(value)
    if len(items) > _MAX_LIST_ITEMS:
        raise ValueError(f"daily {field_name} contains too many items")
    if any(len(item) > _MAX_LIST_ITEM_CHARS for item in items):
        raise ValueError(f"daily {field_name} item is too long")
    return items


# LLM: 读取后逐项验证 ID 唯一、sequence 连续和 previous 链完整。
# 函数用途: 严格加载一个 daily 分片。
def _load_daily_unlocked(path: Path) -> list[DailyMemoryEvent]:
    report = read_jsonl_objects_report(path, context="daily_memory.load")
    if report.load_errors:
        raise DailyMemoryStoreCorruptError(
            f"daily ledger contains {len(report.load_errors)} unreadable row(s)"
        )
    events: list[DailyMemoryEvent] = []
    for index, payload in enumerate(report.records, start=1):
        try:
            event = DailyMemoryEvent.from_record(payload)
        except ValueError as exc:
            raise DailyMemoryStoreCorruptError(
                f"daily ledger row {index} failed schema validation"
            ) from exc
        expected_previous = events[-1].event_id if events else ""
        if event.sequence != index or event.previous_event_id != expected_previous:
            raise DailyMemoryStoreCorruptError("daily ledger ordering chain is invalid")
        if any(current.event_id == event.event_id for current in events):
            raise DailyMemoryStoreCorruptError("daily ledger contains duplicate event_id")
        events.append(event)
    return events


# LLM: 写入前再次序列化验证，并通过 owner quota admission 后原子替换。
# 函数用途: 保存一个完整 daily 分片。
def _write_daily_unlocked(path: Path, events: list[DailyMemoryEvent], *, admission: Any) -> None:
    text = "".join(
        json.dumps(event.to_record(), ensure_ascii=False, sort_keys=True) + "\n"
        for event in events
    )
    admission.check([OwnerQuotaChange(path, len(text.encode("utf-8")))])
    write_text_file_atomic_unlocked(path, text)


# LLM: quota 锁优先于 daily 文件锁，保持 owner 存储统一锁序。
# 类用途: 无 quota 测试环境的空 admission。
class _NullAdmission:
    # LLM: 无 quota 模式保留统一容量检查协议，不引入隐藏的放宽或第二策略。
    # 函数用途: 接受 Daily 整文件容量检查并保持空操作。
    def check(self, _changes: list[OwnerQuotaChange]) -> None:
        return None


# LLM: 不吞配额异常；这里只统一有无 quota 时的上下文协议。
# 函数用途: 返回 daily 写入 admission。
def _quota_admission(enforcer: OwnerQuotaEnforcer | None):
    if enforcer is not None:
        return enforcer.admission()

    # LLM: 本地上下文只适配 admission 协议，不改变异常传播或写入顺序。
    # 类用途: 为未配置 owner quota 的 Daily 写入提供统一 with 接口。
    class _Context:
        # LLM: 进入时返回无状态 admission，不创建额外账本。
        # 函数用途: 开始无 quota Daily 写入临界区。
        def __enter__(self) -> _NullAdmission:
            return _NullAdmission()

        # LLM: 退出时不吞异常，使原子写失败可由 Curator 正确回滚。
        # 函数用途: 结束无 quota Daily 写入临界区。
        def __exit__(
            self,
            exc_type: object,
            exc: object,
            traceback: object,
        ) -> None:
            return None

    return _Context()


# LLM: 所有缺省时间统一为带 UTC 偏移的 ISO 字符串。
# 函数用途: 生成当前 UTC 时间。
def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "DAILY_ACTORS",
    "DAILY_EVENT_TYPES",
    "DAILY_MEMORY_SCHEMA_VERSION",
    "DAILY_ORIGINS",
    "DailyMemoryEvent",
    "DailyMemoryStore",
    "DailyMemoryStoreCorruptError",
    "append_daily_memory_event",
    "daily_memory_path",
    "merge_daily_events",
    "stable_daily_event_id",
]
