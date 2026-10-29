# LLM: 固定through的64KiB分块扫描只缓存已验证行地址/hash；每次lookup仍核对同FD原字节，坏orphan与超预算行均拒绝。
# 模块用途: 有界读取检查点账本，不缓存历史正文，不以缓存创建提交或覆盖权威。
from __future__ import annotations

import hashlib
import json
import os
import threading
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from ..common.cache_freshness import cache_entry_trustworthy, cache_stat_signature

_CHECKPOINT_OFFSETS = OrderedDict()
_CHECKPOINT_OFFSETS_LOCK = threading.Lock()
_CHECKPOINT_CHUNK_BYTES = 65536
_CHECKPOINT_ROW_MAX_BYTES = 8 * 1024 * 1024
_CHECKPOINT_INDEX_MAX_ROWS = 131072
_CHECKPOINT_INDEX_MAX_BYTES = 16 * 1024 * 1024


# LLM: 只含地址、hash及覆盖边界，不留正文；incomplete索引的缺ID不能解释为事实不存在。
# 类用途: 描述一次固定through扫描的有界只读投影。
@dataclass(frozen=True)
class _CheckpointIndex:
    offsets: object
    digest: bytes
    through: int
    terminated: bool
    complete: bool
    size: int


# LLM: 单份及进程驻留总预算均有界；只保存稳定文件，锁内不做磁盘I/O或复制索引。
# 函数用途: 缓存已校验索引并淘汰旧代。
def _cache_offsets(path, stamp, index) -> None:
    if (not cache_entry_trustworthy(stamp[3]) or index.size > _CHECKPOINT_INDEX_MAX_BYTES
            or cache_stat_signature(path.stat()) != stamp):
        return
    with _CHECKPOINT_OFFSETS_LOCK:
        _CHECKPOINT_OFFSETS[(str(path), *stamp)] = index
        _CHECKPOINT_OFFSETS.move_to_end((str(path), *stamp))
        while (len(_CHECKPOINT_OFFSETS) > 64
               or sum(item.size for item in _CHECKPOINT_OFFSETS.values()) > _CHECKPOINT_INDEX_MAX_BYTES):
            _CHECKPOINT_OFFSETS.popitem(last=False)


# LLM: 相同inode、严格变长且旧末尾有LF才可尝试增量；锁内只取引用，完整前缀验证在锁外。
# 函数用途: 查找精确版本或最近的可验证追加基础。
def _cached_index(path, stamp):
    if not cache_entry_trustworthy(stamp[3]):
        return None, None
    key = (str(path), *stamp)
    with _CHECKPOINT_OFFSETS_LOCK:
        exact = _CHECKPOINT_OFFSETS.get(key)
        if exact is not None:
            _CHECKPOINT_OFFSETS.move_to_end(key)
            return exact, None
        for previous_key in reversed(_CHECKPOINT_OFFSETS):
            index = _CHECKPOINT_OFFSETS[previous_key]
            if (previous_key[:3] == key[:3] and index.through < stamp[2]
                    and index.terminated and index.complete):
                return None, index
    return None, None


# LLM: 没有可信append凭证时必须核对全部旧前缀；64KiB块只减少解码，不声称O(delta)读取。
# 函数用途: 哈希指定范围并将描述符留在其末尾。
def _prefix_digest(handle, through):
    handle.seek(0)
    digest = hashlib.sha256()
    block = bytearray(_CHECKPOINT_CHUNK_BYTES)
    remaining = through
    while remaining:
        count = handle.readinto(memoryview(block)[:min(len(block), remaining)])
        if not count:
            raise OSError('conversation compact checkpoint ledger was truncated')
        digest.update(memoryview(block)[:count])
        remaining -= count
    return digest


# LLM: 构建时就限制行数与保守对象预算；超界仍验证所有行，未记录ID随后常量空间回源。
# 函数用途: 从固定起点扫描新记录并保留有界last-wins地址。
def _build_index(handle, through, previous=None):
    digest = _prefix_digest(handle, previous.through) if previous is not None else hashlib.sha256()
    if previous is not None and digest.digest() != previous.digest:
        previous = None
        digest = hashlib.sha256()
    if previous is None:
        handle.seek(0)
    offsets = dict(previous.offsets) if previous is not None else {}
    size = previous.size if previous is not None else 256
    complete = previous.complete if previous is not None else True
    terminated = previous.terminated if previous is not None else True
    for start, raw in _checkpoint_byte_rows(handle, through):
        digest.update(raw)
        terminated = raw.endswith(b'\n')
        row = _parse_row(raw)
        if row is not None:
            checkpoint_id = str(row.get('checkpoint_id') or '').strip()
            if checkpoint_id:
                cost = 512 + len(checkpoint_id) * 4
                existing = checkpoint_id in offsets
                if existing or (len(offsets) < _CHECKPOINT_INDEX_MAX_ROWS
                                and size + cost <= _CHECKPOINT_INDEX_MAX_BYTES):
                    offsets[checkpoint_id] = start, len(raw), hashlib.sha256(raw).digest()
                    if not existing:
                        size += cost
                else:
                    complete = False
        del row, raw
    return _CheckpointIndex(MappingProxyType(offsets), digest.digest(), through, terminated, complete, size)


# LLM: 不完整索引的缺ID回读同一FD固定范围；完整hash匹配后才返回last-wins对象，不存历史正文。
# 函数用途: 在地址预算耗尽时保留原查找合同。
def _uncached_lookup(handle, index, checkpoint_id):
    handle.seek(0)
    digest = hashlib.sha256()
    selected = None
    for _start, raw in _checkpoint_byte_rows(handle, index.through):
        digest.update(raw)
        row = _parse_row(raw)
        if row is not None and str(row.get('checkpoint_id') or '').strip() == checkpoint_id:
            selected = row
        del row, raw
    if digest.digest() != index.digest:
        raise OSError('conversation compact checkpoint source changed during reading')
    return selected

# LLM: 保留原JSON对象/UTF8/Unicode空白语义；无LF的完整末行有效，坏orphan也拒绝，不用宽松解码修补。
# 函数用途: 将一条物理LF记录解析为原对象或空行标记，失败只报告账本不可读。
def _parse_row(raw: bytes) -> dict | None:
    try:
        text = raw.decode('utf-8')
        if not text.strip():
            return None
        row = json.loads(text)
        if not isinstance(row, dict):
            raise ValueError('checkpoint row must be an object')
        return row
    except (ValueError, UnicodeError) as exc:
        raise OSError('conversation compact checkpoint ledger is unreadable') from exc


# LLM: 只沿物理LF分行，UTF8/JSON由原严格解析器验证；读取量受固定through约束，缓冲及单记录分别有硬预算。
# 函数用途: 用readinto批量扫描真实字节地址，避免无缓冲readline逐字节系统调用及无界长行内存。
def _checkpoint_byte_rows(handle, through):
    block = bytearray(_CHECKPOINT_CHUNK_BYTES)
    pending = bytearray()
    offset = handle.tell()
    position = offset
    while position < through:
        wanted = min(len(block), through - position)
        count = handle.readinto(memoryview(block)[:wanted])
        if not count:
            raise OSError('conversation compact checkpoint ledger was truncated')
        position += count
        cursor = 0
        while cursor < count:
            newline = block.find(b'\n', cursor, count)
            end = count if newline < 0 else newline + 1
            if len(pending) + end - cursor > _CHECKPOINT_ROW_MAX_BYTES:
                raise OSError('conversation compact checkpoint record exceeds byte budget')
            pending.extend(memoryview(block)[cursor:end])
            cursor = end
            if newline >= 0:
                raw = bytes(pending)
                yield offset, raw
                offset += len(raw)
                pending.clear()
    if pending:
        yield offset, bytes(pending)


# LLM: 全部行先验证；追加只省去已核对前缀的重复解析，未终止末行及损坏前缀强制冷扫。
# 函数用途: 复用有界地址索引或固定范围回源，保留坏orphan与last-wins合同。
@contextmanager
def checkpoint_row_lookup(path: Path):
    try:
        handle = path.open('rb', buffering=0)
    except FileNotFoundError:
        # LLM: 原不存在账本视为空集合，非空head随后由原chain reader报告缺链。
        # 函数用途: 保留缺文件时原指针验证顺序，不把不存在的行当已提交。
        def missing(_checkpoint_id):
            return None
        yield missing
        return
    with handle:
        stamp = cache_stat_signature(os.fstat(handle.fileno()))
        through = stamp[2]
        cached, previous = _cached_index(path, stamp)
        index = cached if cached is not None else _build_index(handle, through, previous)
        if cached is None and cache_stat_signature(os.fstat(handle.fileno())) == stamp:
            _cache_offsets(path, stamp, index)

        # LLM: 同一已扫描述符按原字节hash复查选中行，旧代内容变动不能生成覆盖；追加不改变固定范围。
        # 函数用途: 返回原行的新解析对象，正文只随调用方当前消费而存活；缺ID由链验证器处理。
        def lookup(checkpoint_id):
            address = index.offsets.get(checkpoint_id)
            if address is None:
                return None if index.complete else _uncached_lookup(handle, index, checkpoint_id)
            offset, length, digest = address
            handle.seek(offset)
            raw = handle.read(length)
            if len(raw) != length or hashlib.sha256(raw).digest() != digest:
                raise OSError('conversation compact checkpoint source changed during reading')
            return _parse_row(raw)

        yield lookup
