# LLM: canonical只读扫描；去重索引仅是原append-only合同下的进程内加速，不是持久覆盖证明，调用方持有幂等锁。
# 模块用途: 提供固定尾界、完整行分页及有界增量幂等查找，不修改历史或线程状态。
from __future__ import annotations

import hashlib
import json
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from ..io.cursor_cache import note_seen
from ..runtime_errors import DataCorruptionError, RecoverableRuntimeError
from .display_checkpoint import is_display_checkpoint
from .models import MessageLogEntry
from .store_io import complete_jsonl_end

# 消息去重索引最多缓存的线程文件数量。
_MAX_DEDUPE_INDEX_FILES = 64
# 单个消息文件去重索引最多保留的幂等键数量。
_MAX_DEDUPE_KEYS_PER_FILE = 16384


# LLM: recent保存首条非display键的精确地址，历史Bloom阳性需回查；64KiB尾锚及完整SHA256防中段改写后增长。
# 类用途: 用有界混合键表及原字节证据复用已验证JSON，不持有正文，旧字节仍分块校验。
@dataclass
class _TranscriptDedupeIndex:
    dev: int
    ino: int
    scanned_offset: int
    tail_anchor_offset: int
    tail_anchor_digest: bytes
    key_offsets: dict[str, tuple[int, int]]
    prefix_digest: bytes
    mtime_ns: int
    ctime_ns: int
    historical_seen: bytes


_DEDUPE_INDEX_CACHE: OrderedDict[str, _TranscriptDedupeIndex] = OrderedDict()
_DEDUPE_FILE_LOCKS: OrderedDict[str, threading.Lock] = OrderedDict()
_DEDUPE_INDEX_LOCK = threading.Lock()


# LLM: 全局锁仅保护有界LRU映射；单文件扫描持路径锁，不让慢盘或大文件阻塞其他owner的去重查询。
# 函数用途: 获取或创建有界的单路径扫描锁，防止跨路径去重查询互相串行阻塞。
def _dedupe_file_lock(cache_key: str) -> threading.Lock:
    with _DEDUPE_INDEX_LOCK:
        lock = _DEDUPE_FILE_LOCKS.get(cache_key)
        if lock is not None:
            _DEDUPE_FILE_LOCKS.move_to_end(cache_key)
            return lock
        lock = threading.Lock()
        _DEDUPE_FILE_LOCKS[cache_key] = lock
        while len(_DEDUPE_FILE_LOCKS) > _MAX_DEDUPE_INDEX_FILES * 2:
            _DEDUPE_FILE_LOCKS.popitem(last=False)
        return lock


# LLM: 超预算不代表canonical损坏；调用方按error_type处理，不能跳行、推进游标或当作空页结束。
# 类用途: 明确区分单行超过本次读取预算与JSON损坏，原文件保持可读。
class MessagePageBudgetExceeded(RecoverableRuntimeError):
    category = "resource_limit"


# LLM: 只检查物理LF与当前描述符长度，不能证明文件未被等长改写；调用方依赖canonical append-only合同。
# 函数用途: 校验字节位置是文件内完整记录边界，不读取或解释整行正文。
def _validate_boundary(handle: BinaryIO, offset: int, size: int) -> None:
    if offset < 0 or offset > size:
        raise DataCorruptionError("message cursor exceeds canonical transcript")
    if offset:
        handle.seek(offset - 1)
        if handle.read(1) != b"\n":
            raise DataCorruptionError("message cursor is not a complete row boundary")


# LLM: 在一次打开的描述符上冻结物理大小，以64KiB块倒扫完整LF；迟到追加留给下一快照，半行不取得覆盖。
# 函数用途: 找到此次扫描可用的最后完整记录位置，内存不随巨大未完成尾行增长。
def complete_message_offset(path: Path) -> int:
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        return 0
    with handle:
        return complete_jsonl_end(handle, handle.seek(0, 2))


# LLM: canonical分页和来源扫描共用严格UTF8解析；数值溢出及错误身份归数据损坏，仅来源扫描显式跳Unicode空白。
# 函数用途: 将一条完整原字节消息解析为本线程记录，不修改正文、元数据或持久状态。
def _message_from_line(line: bytes, thread_id: str, *, skip_blank: bool = False) -> MessageLogEntry | None:
    try:
        text = line.decode("utf-8")
        if skip_blank and not text.strip():
            return None
        raw = json.loads(text)
        if not isinstance(raw, dict):
            raise ValueError("message row must be a JSON object")
        entry = MessageLogEntry.from_dict(raw)
    except (ValueError, TypeError, UnicodeError, OverflowError) as exc:
        raise DataCorruptionError("invalid canonical message row") from exc
    if entry.thread_id != thread_id:
        raise DataCorruptionError("invalid canonical message row")
    return entry


# LLM: through只限定同canonical文件的读取范围，不授予摘要覆盖；max_bytes限制物理页与单行，错误由Store包装。
# 函数用途: 读取一页完整消息；页内放不下的下一行留待下页，第一行超限则显式拒绝而不无限空转。
def read_message_page(
    path: Path, thread_id: str, *, after: int, limit: int,
    through: int | None, max_bytes: int | None,
) -> tuple[list[MessageLogEntry], int]:
    if through is not None and (type(through) is not int or through < after):
        raise ValueError("through must be an integer at or after the cursor")
    if max_bytes is not None and (type(max_bytes) is not int or max_bytes <= 0):
        raise ValueError("max_bytes must be a positive integer")
    entries: list[MessageLogEntry] = []
    cursor = after
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        if after == 0 and through in (None, 0):
            return [], after
        raise
    with handle:
        size = handle.seek(0, 2)
        _validate_boundary(handle, after, size)
        end = through
        if end is None:
            end = complete_jsonl_end(handle, size) if max_bytes is not None else size
        if end < after:
            raise DataCorruptionError("canonical transcript ends before the message cursor")
        if through is not None:
            _validate_boundary(handle, end, size)
        handle.seek(after)
        while cursor < end and len(entries) < max(1, min(1000, int(limit))):
            remaining = end - cursor if max_bytes is None else min(end - cursor, max_bytes - (cursor - after))
            if remaining == 0:
                break
            line = handle.readline(min(end - cursor, remaining + 1))
            if len(line) > remaining:
                if not entries:
                    raise MessagePageBudgetExceeded("canonical message row exceeds page byte budget")
                break
            if not line.endswith(b"\n"):
                if through is not None or max_bytes is not None or handle.tell() < end:
                    raise DataCorruptionError("canonical transcript truncated during page read")
                break
            entry = _message_from_line(line, thread_id)
            entries.append(entry)
            cursor = handle.tell()
    return entries, cursor


# LLM: 不包装UTF8、JSON及model异常，Unicode空白沿原语义跳过；未终止LF在解码之前拒绝。
# 函数用途: 按去重扫描原合同解析一条完整原始消息行。
def _dedupe_message_from_line(line: bytes) -> MessageLogEntry | None:
    if not line:
        raise DataCorruptionError("canonical transcript truncated during dedupe scan")
    if not line.endswith(b"\n"):
        raise DataCorruptionError("canonical transcript has an incomplete final row")
    text = line.decode("utf-8")
    return MessageLogEntry.from_dict(json.loads(text)) if text.strip() else None


# LLM: 只读取水位前最多64KiB；该锚是快速否定条件，完整前缀摘要仍是改写检测权威。
# 函数用途: 计算已验证前缀的64KiB尾块锚，供后续增量扫描核对。
def _dedupe_tail_anchor(handle: BinaryIO, offset: int) -> tuple[int, bytes]:
    start = max(0, offset - 65536)
    handle.seek(start)
    return start, hashlib.blake2b(handle.read(offset - start), digest_size=16).digest()


# LLM: 局部尾锚不能证明整个前缀；元数据或锚异常重扫，其他命中按64KiB块校验完整旧字节但不解析旧JSON。
#   传入空hasher，成功后其状态包含旧前缀；失败时调用方必须丢弃，不能拿部分摘要发布新水位。
# 函数用途: 核对缓存对应文件身份和完整旧前缀，防止等长中段改写加新尾绕过失效。
def _dedupe_index_matches(handle: BinaryIO, index: _TranscriptDedupeIndex, status: os.stat_result, end: int,
                          hasher) -> bool:
    if (index.dev, index.ino) != (status.st_dev, status.st_ino) or not 0 < index.scanned_offset <= end:
        return False
    if end == index.scanned_offset and (index.mtime_ns, index.ctime_ns) != (status.st_mtime_ns, status.st_ctime_ns):
        return False
    if _dedupe_tail_anchor(handle, index.scanned_offset) != (index.tail_anchor_offset, index.tail_anchor_digest):
        return False
    handle.seek(0)
    remaining = index.scanned_offset
    while remaining:
        block = handle.read(min(65536, remaining))
        if not block:
            return False
        hasher.update(block)
        remaining -= len(block)
    return hasher.digest() == index.prefix_digest


# LLM: 旧地址命中必须读回完整行并核对真实key及display角色；不匹配或解析失败只触发从0重扫，不吞最终损坏异常。
# 函数用途: 从已索引字节地址恢复首个匹配消息，不能直接信任缓存中的旧行身份。
def _indexed_dedupe_message(handle: BinaryIO, key: str, address: tuple[int, int]) -> MessageLogEntry | None:
    start, length = address
    handle.seek(start)
    line = handle.read(length)
    if len(line) != length:
        return None
    try:
        entry = _dedupe_message_from_line(line)
    except (DataCorruptionError, ValueError, TypeError, AttributeError, OverflowError):
        return None
    if entry is not None and not is_display_checkpoint(entry) and str(entry.metadata.get("dedupe_key") or "").strip() == key:
        return entry
    return None


# LLM: 与cursor_cache.note_seen使用同一三探针算法；只读位图，不复制整份历史过滤器或修改覆盖事实。
# 函数用途: 查询一个历史键是否可能存在，阳性必须回磁盘核实。
def _dedupe_seen(seen: bytes | bytearray, key: str) -> bool:
    digest = hashlib.blake2b(key.encode("utf-8", "surrogatepass"), digest_size=12).digest()
    indexes = (int.from_bytes(digest[i:i + 4], "little") % (len(seen) * 8) for i in (0, 4, 8))
    return all(seen[index // 8] & (1 << (index % 8)) for index in indexes)


# LLM: 历史阳性只按原字节地址验证，不依据Bloom直接判重；完整消费给定前缀防首匹配掩盖后续坏行，退出恢复扫描位置。
# 函数用途: 找到历史键的首地址，供Bloom阳性验证及重新进入recent使用。
def _historical_dedupe_address(handle: BinaryIO, key: str, end: int) -> tuple[int, int] | None:
    resume, found = handle.tell(), None
    try:
        handle.seek(0)
        while handle.tell() < end:
            start = handle.tell()
            raw = handle.readline(end - start)
            row = _dedupe_message_from_line(raw)
            if (found is None and row is not None and not is_display_checkpoint(row)
                    and str(row.metadata.get("dedupe_key") or "").strip() == key):
                found = start, len(raw)
        return found
    finally:
        handle.seek(resume)


# LLM: 传入地址已被证明是首出现；旧键重新活跃时可回recent，淘汰进入Bloom，过滤器不删除位。
# 函数用途: 保持最近16384个精确键及首地址，超过阈值不销毁缓存。
def _promote_dedupe_key(offsets: OrderedDict, seen: bytearray, key: str, address: tuple[int, int]) -> None:
    offsets[key] = address
    offsets.move_to_end(key)
    if len(offsets) > _MAX_DEDUPE_KEYS_PER_FILE:
        old, _address = offsets.popitem(last=False)
        note_seen(seen, old)


# LLM: 历史Bloom阳性先核首地址再入recent；假阳性新键也正常进入，不能把当前重复行覆盖旧首地址。
# 函数用途: 扫描一条键后更新真实recent集合及历史位图。
def _remember_dedupe_key(handle, offsets: OrderedDict, seen: bytearray, key: str, address: tuple[int, int]) -> None:
    if not key:
        return
    if key in offsets:
        offsets.move_to_end(key)
        return
    if _dedupe_seen(seen, key):
        address = _historical_dedupe_address(handle, key, address[0]) or address
    _promote_dedupe_key(offsets, seen, key, address)


# LLM: 固定EOF前所有新行必须校验，命中也不提前返回；空白入摘要，Bloom只保存被淘汰键，不限制扫描覆盖。
# 函数用途: 单遍增量校验新尾、更新摘要与混合键表，保留首匹配。
def _scan_dedupe_tail(handle: BinaryIO, end: int, key: str, key_offsets: OrderedDict,
                      existing: MessageLogEntry | None, hasher, seen: bytearray) -> MessageLogEntry | None:
    while handle.tell() < end:
        start = handle.tell()
        line = handle.readline(end - start)
        entry = _dedupe_message_from_line(line)
        hasher.update(line)
        if entry is None or is_display_checkpoint(entry):
            continue
        row_key = str(entry.metadata.get("dedupe_key") or "").strip()
        _remember_dedupe_key(handle, key_offsets, seen, row_key, (start, len(line)))
        if existing is None and row_key == key:
            existing = entry
    return existing


# LLM: 原append-once锁仍是跨进程幂等权威；全局锁仅保护有界LRU字典，单路径锁保护本文件增量扫描，不阻塞其他文件。
#   缓存只省略已验证前缀；先恢复其首匹配再扫描新尾，避免迟到同键覆盖首条；空key保持原全扫行为。
# 函数用途: 校验水位、尾锚及前缀字节后增量解析新尾，Bloom命中历史时按原文件核实首匹配。
def find_message_dedupe(path: Path, key: str) -> MessageLogEntry | None:
    cache_key = str(path)
    with _dedupe_file_lock(cache_key):
        return _find_message_dedupe_locked(path, cache_key, key)


# LLM: 调用方已持路径锁；全局LRU锁不覆盖I/O，失败直接在当前锁内冷扫，不递归获取路径锁。
# 函数用途: 复核完整前缀后复用recent/Bloom索引，历史可能命中才额外扫描旧键。
def _find_message_dedupe_locked(path: Path, cache_key: str, key: str) -> MessageLogEntry | None:
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        with _DEDUPE_INDEX_LOCK:
            _DEDUPE_INDEX_CACHE.pop(cache_key, None)
        return None
    with handle:
        with _DEDUPE_INDEX_LOCK:
            index = _DEDUPE_INDEX_CACHE.pop(cache_key, None)
        status = os.fstat(handle.fileno())
        end = handle.seek(0, 2)
        cursor, key_offsets, existing = 0, OrderedDict(), None
        seen = bytearray(65536)
        hasher = hashlib.sha256()
        if key and index is not None and _dedupe_index_matches(handle, index, status, end, hasher):
            cursor, key_offsets = index.scanned_offset, OrderedDict(index.key_offsets)
            seen = bytearray(index.historical_seen)
            existing, valid = _cached_dedupe_match(handle, key, key_offsets, seen, cursor)
            if not valid:
                cursor, key_offsets, seen = 0, OrderedDict(), bytearray(65536)
                hasher = hashlib.sha256()
        else:
            hasher = hashlib.sha256()
        handle.seek(cursor)
        existing = _scan_dedupe_tail(handle, end, key, key_offsets, existing, hasher, seen)
        if end:
            anchor_offset, anchor_digest = _dedupe_tail_anchor(handle, end)
            new_index = _TranscriptDedupeIndex(
                dev=status.st_dev, ino=status.st_ino, scanned_offset=end,
                tail_anchor_offset=anchor_offset, tail_anchor_digest=anchor_digest, key_offsets=key_offsets,
                prefix_digest=hasher.digest(),
                mtime_ns=status.st_mtime_ns, ctime_ns=status.st_ctime_ns,
                historical_seen=bytes(seen),
            )
            with _DEDUPE_INDEX_LOCK:
                _DEDUPE_INDEX_CACHE[cache_key] = new_index
                while len(_DEDUPE_INDEX_CACHE) > _MAX_DEDUPE_INDEX_FILES:
                    _DEDUPE_INDEX_CACHE.popitem(last=False)
        return existing


# LLM: 完整旧前缀已复核才调用；recent或Bloom阳性均需原行读回，失配由调用方同锁冷扫，不递归获取锁。
# 函数用途: 核实首匹配并让已确认历史键回温，单独返回缓存是否仍有效。
def _cached_dedupe_match(handle, key, offsets, seen, cursor):
    address = offsets.get(key)
    if address is None and _dedupe_seen(seen, key):
        address = _historical_dedupe_address(handle, key, cursor)
    if address is None:
        return None, True
    existing = _indexed_dedupe_message(handle, key, address)
    if existing is None:
        return None, False
    _promote_dedupe_key(offsets, seen, key, address)
    return existing, True


# LLM: 固定LF尾界内严格解析，空白仍计hash；可选地址visitor只生成临时引用，原inode及取消在读取边界检查。
# 函数用途: 不保存全量正文地扫描canonical消息及原行地址，坏行、截断或换文件拒绝整个来源。
def scan_message_snapshot(path: Path, thread_id: str, through: int, visitor, *, address_visitor=None,
                          expected_identity=None, interrupt_check=None) -> str:
    import hashlib

    from .compact_guard import raise_if_compact_interrupted

    raise_if_compact_interrupted(interrupt_check)
    digest = hashlib.sha256()
    try:
        handle = path.open("rb")
    except FileNotFoundError:
        if through == 0:
            return digest.hexdigest()
        raise
    with handle:
        stat = os.fstat(handle.fileno())
        if expected_identity is not None and expected_identity != (stat.st_dev, stat.st_ino):
            raise DataCorruptionError("canonical message source changed during selection")
        _validate_boundary(handle, through, handle.seek(0, 2))
        handle.seek(0)
        while handle.tell() < through:
            raise_if_compact_interrupted(interrupt_check)
            offset = handle.tell()
            line = handle.readline(through - handle.tell())
            if not line.endswith(b"\n"):
                raise DataCorruptionError("canonical transcript truncated during source scan")
            digest.update(line)
            entry = _message_from_line(line, thread_id, skip_blank=True)
            if entry is not None:
                visitor(entry)
                if address_visitor is not None:
                    address_visitor(entry, offset, len(line), hashlib.sha256(line).digest())
            del entry, line
        raise_if_compact_interrupted(interrupt_check)
    return digest.hexdigest()
