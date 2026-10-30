# LLM: 固定through的64KiB分块扫描只缓存已验证行地址/hash；同inode增长按append-only合同复用，等长改写/轮转失效。
# 每次lookup仍核对同FD原字节；窗口外显式查询退回冷扫，不把淘汰当作记录不存在。
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

from ..common.cache_freshness import cache_stat_signature

_CHECKPOINT_OFFSETS = OrderedDict()
_CHECKPOINT_OFFSETS_LOCK = threading.Lock()
_CHECKPOINT_CHUNK_BYTES = 65536
_CHECKPOINT_ROW_MAX_BYTES = 8 * 1024 * 1024
_MAX_CACHED_OFFSETS = 131072


# LLM: 发布后映射不再改动；complete为假时，窗口外查询须沿固定快照冷扫，而非错误返回缺失。
# 类用途: 保存最近地址、文件签名以及无LF末行的安全续扫起点。
@dataclass(frozen=True)
class _CheckpointIndex:
    stamp: tuple
    offsets: OrderedDict
    resume_from: int
    complete: bool
    tail_digest: bytes
    head_digest: bytes
    validator: object = None


# LLM: 地址总量有界，不按mtime年龄拒绝缓存；stat与磁盘I/O在锁外，缓存不成为提交权威。
# 函数用途: 每个路径保存最新窗口，最多64份文件及131072个行地址。
def _cache_offsets(path, index) -> None:
    try:
        unchanged = cache_stat_signature(path.stat()) == index.stamp
    except OSError:
        return
    if not unchanged:
        return
    with _CHECKPOINT_OFFSETS_LOCK:
        _CHECKPOINT_OFFSETS[str(path)] = index
        _CHECKPOINT_OFFSETS.move_to_end(str(path))
        while (len(_CHECKPOINT_OFFSETS) > 64
               or sum(len(item.offsets) for item in _CHECKPOINT_OFFSETS.values()) > _MAX_CACHED_OFFSETS):
            _CHECKPOINT_OFFSETS.popitem(last=False)


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


# LLM: 新尾全部校验；同inode严格增长复用，近期mtime不触发全扫。旧无LF末行从行首重读，重复ID仍last-wins。
# 函数用途: 构造独立有界地址窗口，原缓存只读，扫描失败不发布部分索引。
def _scan_index(handle, stamp, cached, validator=None):
    offsets = OrderedDict(cached.offsets) if cached is not None else OrderedDict()
    start = cached.resume_from if cached is not None else 0
    complete = cached.complete if cached is not None else True
    if cached is not None and start < cached.stamp[2]:
        for key in tuple(offsets):
            if offsets[key][0] >= start:
                del offsets[key]
    handle.seek(start)
    resume = start
    for offset, raw in _checkpoint_byte_rows(handle, stamp[2]):
        row = _parse_row(raw)
        if row is not None and validator is not None:
            validator(row)
        checkpoint_id = str(row.get('checkpoint_id') or '').strip() if row is not None else ''
        if checkpoint_id:
            offsets[checkpoint_id] = (offset, len(raw), hashlib.sha256(raw).digest())
            offsets.move_to_end(checkpoint_id)
            if len(offsets) > _MAX_CACHED_OFFSETS:
                offsets.popitem(last=False)
                complete = False
        resume = stamp[2] if raw.endswith(b'\n') else offset
    return _CheckpointIndex(stamp, offsets, resume, complete, _tail_digest(handle, stamp[2]),
                            _head_digest(handle, stamp[2]), validator)


# LLM: 头锚和前次尾锚共同绑定已验证边界；固定窗口不宣称证明任意中段未改，选中行仍单独核SHA。
# 函数用途: 读取已验证前缀最前4KiB指纹，增长时发现旧头损坏即回退严格扫描。
def _head_digest(handle, through):
    handle.seek(0)
    return hashlib.sha256(handle.read(min(4096, through))).digest()


# LLM: 尾锚只作快速否定，不宣称证明全部旧前缀；所选旧行仍逐字节复核，读取量与历史长度无关。
# 函数用途: 排除truncate后重写增长及末行被改写的伪追加，最多读取64KiB。
def _tail_digest(handle, through):
    start = max(0, through - _CHECKPOINT_CHUNK_BYTES)
    handle.seek(start)
    return hashlib.sha256(handle.read(through - start)).digest()


# LLM: 窗口外未命中不是不存在；完整扫描仍验证坏orphan，保留最后一次相同ID的真实地址。
# 函数用途: 为旧历史的显式查询恢复地址，内存不随历史行数增长。
def _historical_address(handle, through, checkpoint_id):
    handle.seek(0)
    found = None
    for offset, raw in _checkpoint_byte_rows(handle, through):
        row = _parse_row(raw)
        if row is not None and str(row.get('checkpoint_id') or '').strip() == checkpoint_id:
            found = offset, len(raw), hashlib.sha256(raw).digest()
    return found


# LLM: 固定快照先验证全部新行，索引只在原inode追加合同下复用；选中行hash仍沿同FD核对，历史窗口不授予覆盖。
# 函数用途: 复用或建立有界检查点行地址，作用域退出关闭文件，不写账本或提前跳过坏orphan。
@contextmanager
def checkpoint_row_lookup(path: Path, *, validator=None):
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
        with _CHECKPOINT_OFFSETS_LOCK:
            cached = _CHECKPOINT_OFFSETS.get(str(path))
            if cached is not None:
                _CHECKPOINT_OFFSETS.move_to_end(str(path))
        if cached is not None and (
            cached.stamp[:2] != stamp[:2] or cached.stamp[2] > through
            or _tail_digest(handle, cached.stamp[2]) != cached.tail_digest
            or _head_digest(handle, cached.stamp[2]) != cached.head_digest
            or (validator is not None and cached.validator is not validator)
        ):
            cached = None
        if cached is not None and cached.stamp == stamp:
            index = cached
        else:
            if cached is not None and not (cached.stamp[:2] == stamp[:2] and cached.stamp[2] < through):
                cached = None
            index = _scan_index(handle, stamp, cached, validator)
            if cache_stat_signature(os.fstat(handle.fileno())) == stamp:
                _cache_offsets(path, index)

        # LLM: 同一已扫描述符按原字节hash复查选中行，旧代内容变动不能生成覆盖；追加不改变固定范围。
        # 函数用途: 返回原行的新解析对象，正文只随调用方当前消费而存活；缺ID由链验证器处理。
        def lookup(checkpoint_id):
            address = index.offsets.get(checkpoint_id)
            if address is None and not index.complete:
                address = _historical_address(handle, through, checkpoint_id)
            if address is None:
                return None
            offset, length, digest = address
            handle.seek(offset)
            raw = handle.read(length)
            if len(raw) != length or hashlib.sha256(raw).digest() != digest:
                raise OSError('conversation compact checkpoint source changed during reading')
            return _parse_row(raw)

        yield lookup
