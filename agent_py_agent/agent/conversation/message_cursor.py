# LLM: MessageStore只读游标缓存含初始空尾证明；不改变JSONL/追加/幂等/Compact推进，粗时间窗口内不信任或保存hint，错误合同不变。
# 模块用途: 缓存已稳定文件的精确message_id首位置，让稳态空线程零打开、追加线程校验锚行后读尾，避免同tick等长改写漏报。
from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path

from ..common.cache_freshness import cache_entry_trustworthy
from ..io.cursor_cache import CursorCache, FileStamp, cursor_key, file_stamp, note_seen
from ..runtime_errors import DataCorruptionError
from .display_checkpoint import is_display_checkpoint
from .store_io import json_row, jsonl_error

# 32768条容纳数万活跃游标，避免6212线程轮询时逐轮淘汰；淘汰只让定位重扫，不会从头重放消息。
_MESSAGE_CURSOR_CACHE_MAX_COUNT = 32768
# 每份首ID过滤器8KiB，32768份极端上界256MiB；同一次读取的多个位置共享不可变过滤器。
_MESSAGE_CURSOR_FILTER_BYTES = 8 * 1024
_EMPTY_SEEN_FILTER = bytes(_MESSAGE_CURSOR_FILTER_BYTES)
_MESSAGE_CURSORS = CursorCache(_MESSAGE_CURSOR_CACHE_MAX_COUNT)


# LLM: 空位图共享唯一不可变bytes；非空位图冻结一次后可在同次位置/空尾投影间共享，不改变Bloom语义。
# 函数用途: 冻结seen过滤器，避免为空闲线程重复保留8KiB零字节。
def _freeze_seen(seen: bytearray) -> bytes:
    return _EMPTY_SEEN_FILTER if not any(seen) else bytes(seen)


# LLM: 偏移指向首条匹配记录，seen不含正文；未验证指纹与锚行时不得用于读尾。
# 类用途: 保存一个文件内精确消息游标及已扫描前缀的有界ID事实。
@dataclass(frozen=True)
class MessageCursor:
    stamp: FileStamp
    start: int
    end: int
    seen: bytes
    empty_tail: bool = False


# LLM: 完整stamp在粗时间片内也可能碰撞；共享信任窗口未过必须冷扫，同尺寸变更不能只核锚行而吞前缀损坏。
# 函数用途: 判定稳定文件的旧偏移是否有资格进入锚行复核，不改持久游标。
def _may_reuse(cursor: MessageCursor, stamp: FileStamp) -> bool:
    if (not cache_entry_trustworthy(stamp.mtime_ns)
            or not cursor.stamp.same_file(stamp) or stamp.size < cursor.stamp.size):
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


# LLM: 逐行异常和首ID语义不变；定位结果通过统一冻结入口保存不可变seen，不保留可变位图。
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
            return MessageCursor(stamp, start, handle.tell(), _freeze_seen(seen))
    raise DataCorruptionError(f"conversation compact message cursor is missing from transcript: {target}")


# LLM: 对象message_id的字符串转换与原扫描相同；过滤器不能改变匹配或坏行处理。
# 函数用途: 标记一条原始行的ID并返回原比较值。
def _note_row(seen: bytearray, row: object) -> str | None:
    if not isinstance(row, dict):
        return None
    target = str(row.get("message_id") or "")
    note_seen(seen, target)
    return target


# LLM: 调用方先核同fd指纹，本入口还须拒绝粗时间窗口内的hint；否则刚读后同tick改写会在窗口外留下陈旧命中。
# 函数用途: 仅为已稳定文件保存消息首位置；不写文件、不推进消费游标，过长ID不驻留。
def _remember(path: Path, target: str, cursor: MessageCursor) -> None:
    if len(target) <= 256 and cache_entry_trustworthy(cursor.stamp.mtime_ns):
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


# LLM: 共享窗口外完整stamp相同才可复用；空键还必须有empty_tail证明，显式空ID定位的EOF不能代替从头空扫。
# 函数用途: 识别稳定且已追平的命名游标或初始空尾，不把近期写入和空ID偏移当作无新增。
def _unchanged_end(path: Path, target: str) -> bool:
    blank_target = not str(target or "").strip()
    cached = _MESSAGE_CURSORS.get(cursor_key(path, "" if blank_target else target))
    if cached is None:
        return False
    stamp = file_stamp(path.stat())
    return (cache_entry_trustworthy(stamp.mtime_ns) and cached.stamp == stamp
            and (cached.empty_tail or (not blank_target and cached.end == stamp.size)))


# LLM: 初始空游标复用共享零位图作扫描起点；正limit/empty_tail和同fd首末stamp规则不变，不改变坏行处理。
# 函数用途: 有界读取原消息尾部；已证从头空扫的稳定线程零打开，零预算不冒充扫描完成。
def read_after_rows(path: Path, target: str, limit: int):
    if (str(target or "").strip() or limit > 0) and _unchanged_end(path, target):
        return [], []
    cursor = locate_message_cursor(path, target) if str(target or "").strip() else None
    with path.open("rb") as handle:
        stamp = file_stamp(os.fstat(handle.fileno()))
        handle.seek(cursor.end if cursor else 0)
        seen = bytearray(cursor.seen if cursor else _EMPTY_SEEN_FILTER)
        rows, errors, positions = _read_tail(handle, path, limit, seen)
        if (cursor is None or cursor.stamp.same_file(stamp)) and file_stamp(os.fstat(handle.fileno())) == stamp:
            _cache_read(path, stamp, (target, cursor), (rows, errors, positions, seen), limit=limit)
        return rows, errors


# LLM: 同次提交只冻结一次seen并共享给位置与空尾；原同fd首末stamp/freshness/正limit规则不变，不推进消费状态。
# 函数用途: 保存精确首位置和空尾证明，包含空文件及仅展示/空白行，不缓存错误或未执行的扫描。
def _cache_read(path: Path, stamp: FileStamp, anchor: tuple, result: tuple, *, limit: int) -> None:
    target, cursor = anchor
    rows, errors, positions, seen = result
    frozen = _freeze_seen(seen)
    _save_positions(path, stamp, frozen, positions)
    if not rows and not errors:
        if cursor is not None:
            _remember(path, target, replace(cursor, stamp=stamp, seen=frozen, empty_tail=True))
        elif not str(target or "").strip() and limit > 0:
            _remember(path, "", MessageCursor(stamp, 0, stamp.size, frozen, empty_tail=True))


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


# LLM: 接受同次已冻结位图直接共享，旧bytearray调用沿统一冻结入口；空ID不占初始空尾键，不缓存正文或异常。
# 函数用途: 为非空消息ID记录首位置，避免空ID行的中间偏移污染从头读取。
def _save_positions(path: Path, stamp: FileStamp, seen: bytearray | bytes, positions: list) -> None:
    frozen = _freeze_seen(seen) if isinstance(seen, bytearray) else seen
    for target, start, end in positions:
        if not target:
            continue
        _remember(path, target, MessageCursor(stamp, start, end, frozen))
