"""同一消息读取器的有界后台续办；缓存只保存原字节定位，不推进消费游标。"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from ..common.cache_freshness import cache_entry_trustworthy
from ..common.nofollow_fs import file_descriptor_change_ns, inspect_file_snapshot_beneath
from ..io.cursor_cache import FileStamp, cursor_key, file_stamp
from ..runtime_errors import DataCorruptionError
from . import message_cursor as cursor_io
from .display_checkpoint import is_display_checkpoint
from .message_scan import (
    TRANSCRIPT_MAX_SEGMENT_BYTES,
    ColdSegmentObservation,
    MessageReadDeferred,
    TranscriptReader,
    cold_message_lookup,
    transcript_manifest,
)


class MessageScanBudget(Protocol):
    max_bytes: int
    max_entries: int
    entries: int
    size: int

    def exhausted(self) -> bool: ...
    def charge(self, *, entries: int = 0, size: int = 0) -> None: ...


class MessageReadAllowance:
    """整次Curator共享一个原子行例外；子线程不能各自领取64MiB。"""
    def __init__(self, budget, shared=None):
        self.budget = budget
        self.shared = shared.shared if shared is not None else self
        self.message_entries = 0
        self.atomic_used = False
        self.atomic_granted = False

    @property
    def max_entries(self):
        return self.budget.max_entries

    @property
    def max_bytes(self):
        return self.budget.max_bytes

    @property
    def entries(self):
        return self.budget.entries

    @property
    def size(self):
        return self.budget.size

    def charge(self, *, entries=0, size=0):
        self.budget.charge(entries=entries, size=size)
        self.shared.message_entries += entries

    def exhausted(self):
        return self.budget.exhausted()

    def claim_atomic_record(self, *, current_probe_entries=0):
        if self.atomic_granted:
            return self.shared.message_entries <= current_probe_entries
        if self.shared.atomic_used or self.shared.message_entries > current_probe_entries:
            return False
        self.shared.atomic_used = self.atomic_granted = True
        return True


class _RecordOutsideBound(Exception):
    pass


@dataclass(frozen=True)
class MessageReadContinuation:
    stamp: FileStamp
    layout: str
    phase: str
    offset: int
    seen: bytes
    anchor: tuple[int, int, bytes] | None = None
    blocked_reason: str = ""
    changed_ns: int | None = None
    cold_target: tuple | None = None
    cold_sources: tuple[ColdSegmentObservation, ...] = ()
    cold_directory: FileStamp | None = None


@dataclass(frozen=True)
class MessageReadOutcome:
    rows: tuple[Any, ...] = ()
    load_errors: tuple[dict, ...] = ()
    partial: bool = False
    has_more: bool = False
    continuation: MessageReadContinuation | None = None
    reason: str = ""


def _layout(reader: TranscriptReader) -> str:
    return hashlib.sha256(json.dumps(reader.manifest, sort_keys=True).encode()).hexdigest()


def _line(handle, budget: MessageScanBudget) -> bytes | None:
    remaining = budget.max_bytes - budget.size
    start = handle.tell()
    previous_entries = budget.entries
    raw = handle.readline(max(1, remaining + 1))
    budget.charge(entries=int(bool(raw)), size=len(raw))
    if len(raw) > remaining:
        if previous_entries:
            handle.seek(start)
            return None
        claim = getattr(budget, "claim_atomic_record", None)
        if not callable(claim) or not claim(current_probe_entries=1):
            handle.seek(start)
            return None
        # 只扩本slice首条完整行，沿原冷上限；超界不解析、不缓存正文，也不授予EOF。
        if not raw.endswith(b"\n"):
            extra = handle.readline(TRANSCRIPT_MAX_SEGMENT_BYTES + 1 - len(raw))
            budget.charge(size=len(extra))
            raw += extra
        if len(raw) > TRANSCRIPT_MAX_SEGMENT_BYTES:
            handle.seek(start)
            raise _RecordOutsideBound

    return raw


def source_stamp_is_trustworthy(stamp, changed_ns):
    return (changed_ns is not None and cache_entry_trustworthy(stamp.mtime_ns)
            and cache_entry_trustworthy(stamp.ctime_ns) and cache_entry_trustworthy(changed_ns))


def _cold_directory(reader):
    if reader.manifest is None:
        return None
    return file_stamp(Path(reader._base).joinpath(*reader._parts, 'archive', reader.manifest['thread_id']).lstat())


def _address_tuple(address):
    return (address['offset'], address['length'], address['sha256']) if address is not None else None


def _usable(reader, handle, progress, stamp, changed_ns, layout, target, budget):
    if (not isinstance(progress, MessageReadContinuation) or progress.stamp != stamp
            or progress.changed_ns != changed_ns or progress.layout != layout or not source_stamp_is_trustworthy(stamp, changed_ns)):
        return False
    if reader.manifest is not None:
        if _cold_directory(reader) != progress.cold_directory:
            return False
        address = reader.index.get(reader.manifest['index'], f'id:{target}') if target else None
        if _address_tuple(address) != progress.cold_target:
            return False
        for source in progress.cold_sources:
            if budget.exhausted():
                raise MessageReadDeferred
            observed = inspect_file_snapshot_beneath(reader._base, source.parts, require_dir_fd=reader.root is not None)
            if observed is None:
                raise DataCorruptionError('canonical cold message source is missing')
            source_stamp = file_stamp(source.stat)
            if (file_stamp(observed.stat) != source_stamp or observed.changed_ns != source.changed_ns
                    or observed.parents != source.parents or not source_stamp_is_trustworthy(source_stamp, source.changed_ns)):
                return False
            reader.observed_segments[source.parts] = source
    if progress.anchor is not None:
        start, end, digest = progress.anchor
        covered = any(source.start <= start and end <= source.end for source in progress.cold_sources)
        if start < reader.base:
            # 同一已核FD代次和不可变清单证明原冷段；不为64MiB旧目标反复读正文。
            return covered
        if end - start > budget.max_bytes - budget.size:
            return False
        handle.seek(start)
        raw = handle.read(end - start)
        budget.charge(size=len(raw))
        if len(raw) != end - start or hashlib.sha256(raw).digest() != digest:
            return False
    return True


def _selected_sources(reader, target_address, anchor):
    target = target_address[0] if target_address else None
    at = anchor[0] if anchor is not None and anchor[0] < reader.base else None
    return tuple(source for source in reader.observed_segments.values()
                 if ((target is not None and source.start <= target < source.end)
                     or (at is not None and source.start <= at < source.end)))


def _anchor(start, raw):
    # 延续同完整FileStamp与粗时间门；尾锚只快速否定，不使巨大原行成为驻留缓存。
    suffix = raw[-65536:]
    return start + len(raw) - len(suffix), start + len(raw), hashlib.sha256(suffix).digest()


def read_after_slice(path: Path, target: str, limit: int, budget: MessageScanBudget) -> MessageReadOutcome:
    """定位未完成明确partial；源变化丢弃提示，原ID仍是唯一消费游标。"""
    if budget.exhausted():
        return MessageReadOutcome(partial=True, reason="slice_budget")
    key = (*cursor_key(path, target), 'bounded-read')
    saved = cursor_io._MESSAGE_CURSORS.get(key)
    with TranscriptReader(path, read_budget=budget) as reader:
        info = os.fstat(reader.hot.fileno())
        stamp = file_stamp(info)
        changed_ns = file_descriptor_change_ns(reader.hot.fileno(), info)
        if changed_ns is None:
            return MessageReadOutcome(partial=True, reason="source_identity_unavailable")
        directory = _cold_directory(reader)
        layout = _layout(reader)
        handle = reader.hot if isinstance(saved, MessageReadContinuation) and saved.phase == 'locate' else reader
        try:
            if _usable(reader, handle, saved, stamp, changed_ns, layout, target, budget):
                progress = saved
            else:
                progress = _start(reader, path, target, stamp, changed_ns, layout, budget)
        except MessageReadDeferred:
            return MessageReadOutcome(partial=True, reason="slice_budget")
        if progress.blocked_reason:
            return MessageReadOutcome(partial=True, continuation=progress, reason=progress.blocked_reason)
        rows, errors, positions = [], [], []
        seen = bytearray(progress.seen)
        handle = reader.hot if progress.phase == 'locate' else reader
        handle.seek(progress.offset)
        anchor = progress.anchor
        phase = progress.phase
        eof = False
        reason = ""
        while not budget.exhausted() and len(rows) < limit:
            start = handle.tell()
            try:
                raw = _line(handle, budget)
            except MessageReadDeferred:
                break
            except _RecordOutsideBound:
                reason = "record_exceeds_supported_limit"
                break
            if raw is None:
                break
            if not raw:
                if phase == 'locate':
                    raise DataCorruptionError(f'conversation compact message cursor is missing from transcript: {target}')
                eof = True
                break
            anchor = _anchor(start, raw)
            if phase == 'locate':
                try:
                    row = json.loads(raw.decode('utf-8'))
                except (ValueError, UnicodeError) as exc:
                    raise DataCorruptionError(f'conversation transcript contains an unreadable row: {path}') from exc
                if cursor_io._note_row(seen, row) == target:
                    physical_end = handle.tell()
                    exact = cursor_io.MessageCursor(stamp, start, physical_end, cursor_io._freeze_seen(seen), changed_ns=changed_ns)
                    if source_stamp_is_trustworthy(stamp, changed_ns):
                        cursor_io._remember(path, target, exact)
                    handle = reader
                    handle.seek(physical_end + reader.base)
                    anchor = (anchor[0] + reader.base, anchor[1] + reader.base, anchor[2])
                    phase = 'tail'
                continue
            row, error = cursor_io._tail_row(raw, path)
            if error is not None:
                errors.append(error)
                break
            if row is None:
                seen[:] = b"\xff" * len(seen)
            else:
                cursor_io._note_tail_position(row, seen, positions, (start, handle.tell()))
                if not is_display_checkpoint(row):
                    rows.append(row)
        if (file_stamp(os.fstat(reader.hot.fileno())) != stamp or file_stamp(path.stat()) != stamp
                or reader.manifest != transcript_manifest(path) or directory != _cold_directory(reader)
                or file_descriptor_change_ns(reader.hot.fileno(), os.fstat(reader.hot.fileno())) != changed_ns):
            # 不把变动中一片的局部读取授予覆盖；下一拍从原消费ID重新核验。
            return MessageReadOutcome(partial=True, reason="source_changed")
        if not errors and not reader.manifest and source_stamp_is_trustworthy(stamp, changed_ns):
            cursor_io._save_positions(path, stamp, seen, positions, changed_ns=changed_ns)
        unfinished = not eof and not errors and len(rows) < limit
        # 有完整真实消息时是一页可提交数据；partial只表示尚无可提交页，不能由空页推导EOF。
        partial = unfinished and not rows
        has_more = unfinished and bool(rows)
        target_address = progress.cold_target
        sources = _selected_sources(reader, target_address, anchor)
        continuation = MessageReadContinuation(stamp, layout, phase, handle.tell(), cursor_io._freeze_seen(seen),
            anchor, reason, changed_ns, target_address, sources, directory) if unfinished else None
        # 仅可信mtime/ctime/实际变更代次的来源进入跨拍缓存；没有正文或活FD。
        trusted = source_stamp_is_trustworthy(stamp, changed_ns) and all(source_stamp_is_trustworthy(file_stamp(source.stat), source.changed_ns) for source in sources)
        cursor_io._MESSAGE_CURSORS.put(key, continuation if partial and not rows and trusted else None)
        if eof and not rows and not errors and not reader.manifest and target:
            exact = cursor_io._MESSAGE_CURSORS.get(cursor_key(path, target))
            if isinstance(exact, cursor_io.MessageCursor) and source_stamp_is_trustworthy(stamp, changed_ns):
                cursor_io._remember(path, target, replace(exact, stamp=stamp, empty_tail=True))
        return MessageReadOutcome(tuple(rows), tuple(errors), partial, has_more, continuation, reason)


def _start(reader, path, target, stamp, changed_ns, layout, budget):
    empty = cursor_io._EMPTY_SEEN_FILTER
    if not str(target or '').strip():
        return MessageReadContinuation(stamp, layout, 'tail', 0, empty)
    cold = cold_message_lookup(path, f'id:{target}', reader=reader) if reader.manifest else None
    if cold is not None:
        address = cold[1]
        return MessageReadContinuation(stamp, layout, 'tail', address['offset'] + address['length'], empty,
            changed_ns=changed_ns, cold_target=_address_tuple(address))
    cached = cursor_io._MESSAGE_CURSORS.get(cursor_key(path, target))
    if (isinstance(cached, cursor_io.MessageCursor) and cached.stamp == stamp
            and source_stamp_is_trustworthy(stamp, changed_ns) and (os.name != "nt" or cached.changed_ns == changed_ns)
            and cached.end - cached.start <= budget.max_bytes - budget.size):
        reader.hot.seek(cached.start)
        raw = reader.hot.readline(cached.end - cached.start + 1)
        budget.charge(size=len(raw))
        try:
            row = json.loads(raw.decode('utf-8'))
            valid = (len(raw) == cached.end - cached.start and isinstance(row, dict)
                     and str(row.get('message_id') or '') == target)
        except (ValueError, UnicodeError):
            valid = False
        if valid:
            return MessageReadContinuation(stamp, layout, 'tail', cached.end + reader.base, cached.seen)
    return MessageReadContinuation(stamp, layout, 'locate', 0, empty)
