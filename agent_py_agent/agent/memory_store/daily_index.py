from __future__ import annotations

"""把已提交的 Daily 经历投影进 owner 本地检索索引，读取时按 Daily 分片核对可见性。"""

# LLM: Daily 分片（memory/daily/YYYY-MM-DD.jsonl）是唯一权威；local.db 的 daily_event 行只是可删可重建的检索投影。
#   写入只在 Curator 四账本事务提交并放锁之后进行；读取时按日期分片核对事件仍在且正文一致，分片被删、改写或读不出时
#   立即隐藏投影行（不报错、不返回旧行）；维护节拍按游标有界清理孤儿行、按日期分片有界补建漏写或整库删除后的投影。
#   只读结构化字段，不解析正文语义。
# 模块用途: Daily 经历检索投影的写入、读取可见性核对与有界对账。

import hashlib
import heapq
import json
import logging
import os
import sqlite3
import stat
from collections.abc import Iterable
from datetime import date
from pathlib import Path

from ..local_storage.records import LocalRecordInput
from .daily import (
    DailyMemoryEvent,
    DailyMemoryStoreCorruptError,
    _load_daily_unlocked,
    daily_memory_path,
)

DAILY_EVENT_SOURCE_TYPE = "daily_event"
# 一次读取核对最多打开的日期分片数：检索一页最多 20 条、翻看窗口最多 41 条，日期数不会更多。
DAILY_VISIBILITY_MAX_SHARD_FILES = 64
# 一次读取核对累计最多读取的 Daily 分片字节数；超出预算的命中按“无法核对”隐藏，不猜可见。
DAILY_VISIBILITY_MAX_BYTES = 16 * 1024 * 1024
# 维护节拍每轮最多核对的投影行数，超出部分下一轮从游标续查。
DAILY_INDEX_PRUNE_SLICE_ROWS = 64
# 维护节拍每轮最多补建的 Daily 日期分片数，超出部分下一轮从日期游标续补。
DAILY_INDEX_BACKFILL_SLICE_FILES = 4
# 引用只保留这些身份字段，不把引用里可能带的原话片段复制进索引元数据。
_REF_IDENTITY_KEYS = ("message_id", "thread_id", "role", "operation_id", "run_id", "event_id", "tool", "artifact_ref")
_SHARD_SUFFIX = ".jsonl"

_LOGGER = logging.getLogger(__name__)


# 函数用途: 拼出一条 Daily 经历进入检索索引的正文（摘要、决定、下一步）。
def daily_event_text(event: DailyMemoryEvent) -> str:
    return "\n".join(item for item in (event.summary, *event.decisions, *event.next_actions) if item)


# 函数用途: 只保留引用的结构化身份字段。
def _ref_identities(refs: Iterable[object]) -> list[dict[str, str]]:
    result = []
    for ref in refs:
        if isinstance(ref, dict):
            item = {key: ref[key] for key in _REF_IDENTITY_KEYS if isinstance(ref.get(key), str) and ref[key]}
            if item:
                result.append(item)
    return result


# LLM: Daily 的 event_id 只在同一日期分片内唯一（来源身份相同的事件可以出现在不同日期），所以投影行以
#   “日期:event_id”为来源身份；日期取事件实际所在分片（created_at 前 10 位，与 DailyMemoryStore 落盘同一规则）。
#   thread_id 让 around 翻看同会话记录。
# 函数用途: 把一条已提交的 Daily 经历转换成本地检索记录。
def daily_event_record(store: object, event: DailyMemoryEvent) -> LocalRecordInput:
    day = event.created_at[:10]
    source_id = f"{day}:{event.event_id}"
    metadata: dict[str, object] = {
        "event_id": event.event_id,
        "date": day,
        "refs": {
            "message_refs": _ref_identities(event.message_refs),
            "tool_refs": _ref_identities(event.tool_refs),
            "artifact_refs": _ref_identities(event.artifact_refs),
        },
    }
    if event.thread_id:
        metadata["thread_id"] = event.thread_id
    return LocalRecordInput(
        source_type=DAILY_EVENT_SOURCE_TYPE, source_id=source_id, title=event.event_type,
        content=daily_event_text(event), metadata=metadata, visibility="private",
        record_id=store.make_record_id(DAILY_EVENT_SOURCE_TYPE, source_id),
    )


# 函数用途: 从投影行元数据取出它声称对应的分片日期与事件编号，任一缺失返回空串。
def _projection_identity(metadata: object) -> tuple[str, str]:
    metadata = metadata if isinstance(metadata, dict) else {}
    event_id = metadata.get("event_id")
    return _shard_day(metadata.get("date")), event_id if isinstance(event_id, str) else ""


# 函数用途: 只接受标准 YYYY-MM-DD 日期，拒绝 fromisoformat 也认的紧凑写法，避免把坏元数据当成“分片已删”。
def _shard_day(value: object) -> str:
    day = value if isinstance(value, str) else ""
    try:
        return day if date.fromisoformat(day).isoformat() == day else ""
    except ValueError:
        return ""


# LLM: 不加锁直读（写入端整文件原子替换），不建目录不建锁文件；目录或分片是符号链接、不是普通文件、超出字节预算、
#   坏行或链断一律当“读不出”返回 None；分片不存在返回空表（事件确已不在）。第二个返回值是计入预算的字节数。
# 函数用途: 读一个日期分片里当前的全部经历，按 event_id 建表。
def _shard_events(daily_dir: Path, day: str, *, max_bytes: int) -> tuple[dict[str, DailyMemoryEvent] | None, int]:
    if not _shard_day(day):
        return None, 0
    try:
        if daily_dir.is_symlink():
            return None, 0
        path = daily_memory_path(daily_dir, day=day)
        try:
            info = path.lstat()
        except FileNotFoundError:
            return {}, 0
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            return None, 0
        return {event.event_id: event for event in _load_daily_unlocked(path)}, int(info.st_size)
    except (OSError, ValueError, DailyMemoryStoreCorruptError):
        return None, 0


# 函数用途: 判断一条检索行是否仍与 Daily 原件里的同一事件一致（类型与正文都要对上）。
def _projection_matches(event: DailyMemoryEvent | None, title: str, content: str) -> bool:
    return event is not None and event.event_type == title and daily_event_text(event) == content


# LLM: 非 daily_event 行原样保留；daily_event 行按日期分组、每个分片只读一次（分片数与总字节都有上限），
#   事件仍在且类型、正文一致才保留。分片读不出、超出预算或拿不到 Daily 目录时隐藏而不是猜可见。
# 函数用途: 过滤掉 Daily 权威已不存在或已改变的检索行，返回仍可展示的命中。
def visible_daily_hits(daily_dir: str | Path | None, hits: list) -> list:
    root = Path(daily_dir) if daily_dir else None
    shards: dict[str, dict[str, DailyMemoryEvent] | None] = {}
    remaining = DAILY_VISIBILITY_MAX_BYTES
    kept = []
    for hit in hits:
        if getattr(hit, "source_type", "") != DAILY_EVENT_SOURCE_TYPE:
            kept.append(hit)
            continue
        day, event_id = _projection_identity(hit.metadata)
        if day not in shards:
            if root is None or not day or len(shards) >= DAILY_VISIBILITY_MAX_SHARD_FILES or remaining <= 0:
                shards[day] = None
            else:
                shards[day], used = _shard_events(root, day, max_bytes=remaining)
                remaining -= used
        events = shards[day]
        event = events.get(event_id) if events is not None and hit.source_id == f"{day}:{event_id}" else None
        if _projection_matches(event, hit.title, hit.content):
            kept.append(hit)
    return kept


# 函数用途: 按名字列出 Daily 目录里日期大于游标的前 limit 个标准分片日期，只保留 limit 个名字在内存里。
def _next_shard_days(daily_dir: Path, after: str, limit: int) -> list[str]:
    if daily_dir.is_symlink() or not daily_dir.is_dir():
        return []
    days = []
    with os.scandir(daily_dir) as entries:
        for entry in entries:
            name = entry.name
            if name.endswith(_SHARD_SUFFIX) and (day := _shard_day(name[:-len(_SHARD_SUFFIX)])) and day > after:
                days.append(day)
                if len(days) > limit * 8:
                    days = heapq.nsmallest(limit, days)
    return heapq.nsmallest(limit, days)


# LLM: 维护游标只在进程内（重启后从头再扫一遍也有界）；投影写删都沿 LocalStore 原入口（delete_record 一并清 FTS、
#   记录和外置正文），不另建账本。
# 类用途: 绑定 owner 的本地检索库与 Daily 目录，负责 daily_event 投影的写入与有界对账。
class DailyEventIndex:
    # 函数用途: 绑定唯一本地检索库和 Daily 权威目录；不读文件、不建目录。
    def __init__(self, local_store: object, daily_dir: str | Path) -> None:
        self.local_store = local_store
        self.daily_dir = Path(daily_dir)
        self._prune_after = ""
        self._backfill_after = ""

    # LLM: 调用方保证 Curator 事务已提交且锁已释放；正文与元数据都没变时跳过写入（不让事件日志空转）。
    #   单条写失败只计数、不中断其余条目，也不回滚已提交的 Daily 事实；执行未结等其它异常照常上抛。
    # 函数用途: 把本轮提交的 Daily 经历写进检索索引，返回写入/跳过/失败计数。
    def index(self, events: Iterable[DailyMemoryEvent]) -> dict[str, int]:
        report = {"indexed": 0, "unchanged": 0, "failed": 0}
        for event in events:
            if not event.event_id:
                continue
            try:
                record = daily_event_record(self.local_store, event)
                if self.local_store.record_matches(record):
                    report["unchanged"] += 1
                    continue
                self.local_store.upsert_record(record)
                report["indexed"] += 1
            except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
                report["failed"] += 1
                _LOGGER.warning("DAILY_EVENT_INDEX_WRITE_FAILED type=%s", type(exc).__name__)
        return report

    # LLM: 两段各自有界：先按 source_id 续页核对一页投影行（分片里事件已不存在的删、正文或元数据变了的重写、
    #   分片读不出的这轮跳过不删），再按日期游标补建几个分片里缺失或不一致的投影（漏写、删库重建都靠这一步自愈）。
    # 函数用途: 有界对账 Daily 原件与检索投影，返回核对、清理、补建计数及两段游标是否各自走完一圈。
    def maintain(
        self,
        *,
        max_rows: int = DAILY_INDEX_PRUNE_SLICE_ROWS,
        max_shards: int = DAILY_INDEX_BACKFILL_SLICE_FILES,
    ) -> dict[str, object]:
        report: dict[str, object] = {"checked": 0, "deleted": 0, "refreshed": 0, "unreadable": 0,
                                     "backfilled": 0, "shards": 0}
        self._prune_page(max(1, int(max_rows)), report)
        self._backfill(max(1, int(max_shards)), report)
        # 两段游标各自转圈，分别报告本轮是否走完了自己那一圈。
        report["prune_complete"] = not self._prune_after
        report["backfill_complete"] = not self._backfill_after
        return report

    # 函数用途: 核对一页 daily_event 投影行，删孤儿、刷新已变化的行。
    def _prune_page(self, limit: int, report: dict[str, object]) -> None:
        rows = self.local_store.record_page(DAILY_EVENT_SOURCE_TYPE, after_source_id=self._prune_after, limit=limit)
        report["checked"] = len(rows)
        shards: dict[str, dict[str, DailyMemoryEvent] | None] = {}
        for row in rows:
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            day, event_id = _projection_identity(metadata)
            if not day or not event_id:
                # 元数据坏到无法对应任何 Daily 事件的投影行只是坏派生数据，直接删除。
                self.local_store.delete_record(row["id"])
                report["deleted"] += 1
                continue
            if day not in shards:
                shards[day] = _shard_events(self.daily_dir, day, max_bytes=DAILY_VISIBILITY_MAX_BYTES)[0]
            events = shards[day]
            if events is None:
                report["unreadable"] += 1
                continue
            event = events.get(event_id) if row["source_id"] == f"{day}:{event_id}" else None
            if event is None:
                self.local_store.delete_record(row["id"])
                report["deleted"] += 1
                continue
            record = daily_event_record(self.local_store, event)
            digest = hashlib.sha256(record.content.encode("utf-8")).hexdigest()
            if (row.get("title") != record.title or row.get("content_hash") != digest
                    or json.dumps(metadata, sort_keys=True) != json.dumps(record.metadata, sort_keys=True)):
                self.local_store.upsert_record(record)
                report["refreshed"] += 1
        self._prune_after = rows[-1]["source_id"] if len(rows) >= limit else ""

    # 函数用途: 按日期游标把几个 Daily 分片里缺失或不一致的投影补齐。
    def _backfill(self, limit: int, report: dict[str, object]) -> None:
        days = _next_shard_days(self.daily_dir, self._backfill_after, limit)
        for day in days:
            events = _shard_events(self.daily_dir, day, max_bytes=DAILY_VISIBILITY_MAX_BYTES)[0]
            report["shards"] += 1
            if events is None:
                report["unreadable"] += 1
                continue
            for event in events.values():
                record = daily_event_record(self.local_store, event)
                if not self.local_store.record_matches(record):
                    self.local_store.upsert_record(record)
                    report["backfilled"] += 1
        self._backfill_after = days[-1] if len(days) >= limit else ""


__all__ = [
    "DAILY_EVENT_SOURCE_TYPE",
    "DailyEventIndex",
    "daily_event_record",
    "daily_event_text",
    "visible_daily_hits",
]
