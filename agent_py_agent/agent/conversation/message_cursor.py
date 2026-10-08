# LLM: 替换MessageStore只读游标扫描，不改变JSONL/追加/幂等/Compact推进；错误文本与原实现相同。
# 模块用途: 缓存精确message_id首位置，让稳态空线程零打开、追加线程只校验锚行和读取尾部。
from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path

from ..io.cursor_cache import CursorCache, FileStamp, cursor_key, file_stamp, note_seen
from ..runtime_errors import DataCorruptionError
from .display_checkpoint import is_display_checkpoint
from .store_io import json_row, jsonl_error

# 4096条覆盖800线程及近期推进位置；LRU淘汰只让定位重扫，不会从头重放消息。
_MESSAGE_CURSOR_CACHE_MAX_COUNT = 4096
# 每份首ID过滤器8KiB，4096份极端上界32MiB；实际同一次读取的多个位置共享不可变过滤器。
_MESSAGE_CURSOR_FILTER_BYTES = 8 * 1024
_MESSAGE_CURSORS = CursorCache(_MESSAGE_CURSOR_CACHE_MAX_COUNT)


# LLM: 偏移指向首条匹配记录，seen不含正文；未验证指纹与锚行时不得用于读尾。
# 类用途: 保存一个文件内精确消息游标及已扫描前缀的有界ID事实。
@dataclass(frozen=True)
class MessageCursor:
    stamp: FileStamp
    start: int
    end: int
    seen: bytes
    empty_tail: bool = False


# LLM: 同尺寸mtime/ctime变化必须完整扫描，不能因为游标行恰好未变而吞掉前缀新损坏。
# 函数用途: 判定旧偏移是否有资格进入锚行复核。
def _may_reuse(cursor: MessageCursor, stamp: FileStamp) -> bool:
    if not cursor.stamp.same_file(stamp) or stamp.size < cursor.stamp.size:
        return False
    return stamp.size > cursor.stamp.size or stamp == cursor.stamp


# LLM: 不信缓存键本身，必须在原始字节位置读回同message_id及同结束边界；失败正常退回完整扫描。
# 函数用途: 复核一个游标锚行，没有复核通过就不授予seek提示。
def _anchor_matches(handle, cursor: MessageCursor, target: str) -> bool:
    handle.seek(cursor.start)
    try:
        row = json.loads(handle.readline().decode("utf-8"))
    except (ValueError, UnicodeError):
        return False
    return isinstance(row, dict) and str(row.get("message_id") or "") == target and handle.tell() == cursor.end


# LLM: 逐行JSON/UTF-8异常保持原DataCorruptionError；非对象行仍可越过，首条匹配后不扫描更晚坏行。
# 函数用途: 校验失效或冷启动时完整定位原精确消息游标，并累积有界首ID过滤器。
def _scan_cursor(handle, path: Path, target: str, stamp: FileStamp) -> MessageCursor:
    handle.seek(0)
    seen = bytearray(_MESSAGE_CURSOR_FILTER_BYTES)
    while True:
        start = handle.tell()
        line = handle.readline()
        if not line:
            break
        try:
            row = json.loads(line.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise DataCorruptionError(f"conversation transcript contains an unreadable row: {path}") from exc
        if _note_row(seen, row) == target:
            return MessageCursor(stamp, start, handle.tell(), bytes(seen))
    raise DataCorruptionError(f"conversation compact message cursor is missing from transcript: {target}")


# LLM: 对象message_id的字符串转换与原扫描相同；过滤器不能改变匹配或坏行处理。
# 函数用途: 标记一条原始行的ID并返回原比较值。
def _note_row(seen: bytearray, row: object) -> str | None:
    if not isinstance(row, dict):
        return None
    target = str(row.get("message_id") or "")
    note_seen(seen, target)
    return target


# LLM: 只在实际描述符与路径指纹一致时存提示；过长ID不驻留，仍由原完整扫描得出结果。
# 函数用途: 保存已复核的消息首位置，不写文件或推进已消费游标。
def _remember(path: Path, target: str, cursor: MessageCursor) -> None:
    if len(target) <= 256:
        _MESSAGE_CURSORS.put(cursor_key(path, target), cursor)


# LLM: byte_offset_after及after_report共用本入口；缓存缺失/替换/截断/改写时沿唯一完整扫描，不另留旧分支。
# 函数用途: 返回消息精确首位置，IO失败和缺游标保持原损坏异常。
def locate_message_cursor(path: Path, target: str) -> MessageCursor:
    try:
        with path.open("rb") as handle:
            return _locate_open(handle, path, target)
    except OSError as exc:
        raise DataCorruptionError(f"cannot read conversation transcript: {path}") from exc


# LLM: 在同一真实描述符上校验/扫描后核对快照未变，避免把中途修改的偏移标作稳定缓存。
# 函数用途: 复核或完整定位已打开文件的消息首位置。
def _locate_open(handle, path: Path, target: str) -> MessageCursor:
    stamp = file_stamp(os.fstat(handle.fileno()))
    cached = _MESSAGE_CURSORS.get(cursor_key(path, target))
    if cached is not None and _may_reuse(cached, stamp) and _anchor_matches(handle, cached, target):
        cursor = replace(cached, stamp=stamp, empty_tail=False)
    else:
        cursor = _scan_cursor(handle, path, target, stamp)
    if file_stamp(os.fstat(handle.fileno())) == stamp:
        _remember(path, target, cursor)
    return cursor


# LLM: 只有同完整指纹且精确游标就在EOF才能零打开；mtime/ctime参与，大小相同不等于历史未改写。
# 函数用途: 识别已追平且没有任何文件变化的线程。
def _unchanged_end(path: Path, target: str) -> bool:
    cached = _MESSAGE_CURSORS.get(cursor_key(path, target))
    if cached is None:
        return False
    stamp = file_stamp(path.stat())
    return cached.stamp == stamp and (cached.empty_tail or cached.end == stamp.size)


# LLM: 真实读尾仍保留display/坏行/limit原规则；位置只对原始首ID提示，解析消息及错误由MessageStore负责。
# 函数用途: 获取精确游标后的原JSON对象及读取错误，空稳态不打开文件。
def read_after_rows(path: Path, target: str, limit: int):
    if str(target or "").strip() and _unchanged_end(path, target):
        return [], []
    cursor = locate_message_cursor(path, target) if str(target or "").strip() else None
    with path.open("rb") as handle:
        stamp = file_stamp(os.fstat(handle.fileno()))
        handle.seek(cursor.end if cursor else 0)
        seen = bytearray(cursor.seen if cursor else bytes(_MESSAGE_CURSOR_FILTER_BYTES))
        rows, errors, positions = _read_tail(handle, path, limit, seen)
        if (cursor is None or cursor.stamp.same_file(stamp)) and file_stamp(os.fstat(handle.fileno())) == stamp:
            _cache_read(path, stamp, (target, cursor), (rows, errors, positions, seen))
        return rows, errors


# LLM: 单批稳定读取才更新提示，空尾包括display/空白行；不把错误当无新增，也不改原消费状态。
# 函数用途: 保存尾读可证明的新位置与空尾指纹。
def _cache_read(path: Path, stamp: FileStamp, anchor: tuple, result: tuple) -> None:
    target, cursor = anchor
    rows, errors, positions, seen = result
    _save_positions(path, stamp, seen, positions)
    if cursor and not rows and not errors:
        _remember(path, target, replace(cursor, stamp=stamp, seen=bytes(seen), empty_tail=True))


# LLM: UTF-8/JSON坏行即停，display不计条数；Bloom误报只不记位置，读取对象和错误保持原样。
# 函数用途: 逐行读取消息尾部，附带可证明首出现的ID位置。
def _read_tail(handle, path: Path, limit: int, seen: bytearray):
    rows, errors, positions = [], [], []
    safe_prefix = True
    while len(rows) < limit:
        start = handle.tell()
        line = handle.readline()
        if not line:
            break
        row, error = _tail_row(line, path)
        if error is not None:
            errors.append(error)
            break
        if row is None:
            safe_prefix = False
            continue
        if safe_prefix:
            _note_tail_position(row, seen, positions, (start, handle.tell()))
        if not is_display_checkpoint(row):
            rows.append(row)
    return rows, errors, positions


# LLM: 已见ID（含Bloom误报）不提升，避免重复消息ID改变原首条匹配；空白前缀不调用本入口。
# 函数用途: 标记尾部ID，仅记录可证明首出现的位置。
def _note_tail_position(row: dict, seen: bytearray, positions: list, bounds: tuple) -> None:
    target = str(row.get("message_id") or "")
    if not note_seen(seen, target):
        positions.append((target, *bounds))


# LLM: 空白行跳过只发生在尾读，前缀定位仍按原实现把空白JSON判损坏；错误context及line_number保持。
# 函数用途: 解码一条消息尾部行并按原合同报告损坏。
def _tail_row(line: bytes, path: Path):
    if not line.strip():
        return None, None
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError as exc:
        return None, jsonl_error(exc, "conversation.messages.after", path=path)
    return json_row(text, context="conversation.messages.after", path=path, line_number=0)


# LLM: 一次尾读的位置共享同一不可变过滤器；过长键不缓存，淘汰仅变慢，不缓存正文或异常。
# 函数用途: 为下一批可能推进到的消息ID记录首位置。
def _save_positions(path: Path, stamp: FileStamp, seen: bytearray, positions: list) -> None:
    frozen = bytes(seen)
    for target, start, end in positions:
        _remember(path, target, MessageCursor(stamp, start, end, frozen))
