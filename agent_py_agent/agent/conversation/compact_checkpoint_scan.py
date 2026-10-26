# LLM: 有界缓存只复用稳定五元组文件的已验证行地址/hash；每次lookup仍核对原字节，不创建提交或覆盖权威。
# 模块用途: 缓存检查点行索引，坏orphan仍阻断，旧正文按需读取且不常驻。
from __future__ import annotations

import hashlib
import json
import os
import threading
from collections import OrderedDict
from contextlib import contextmanager
from pathlib import Path

from ..common.cache_freshness import cache_entry_trustworthy, cache_stat_signature

_CHECKPOINT_OFFSETS = OrderedDict()
_CHECKPOINT_OFFSETS_LOCK = threading.Lock()


# LLM: 单份偏移最多131072行、最多64份文件，缓存只含地址和hash；全局锁不覆盖磁盘I/O。
# 函数用途: 仅保存仍稳定且已离开粗时间窗口的已校验完整文件索引。
def _cache_offsets(path, stamp, offsets) -> None:
    if (not cache_entry_trustworthy(stamp[3]) or len(offsets) > 131072
            or cache_stat_signature(path.stat()) != stamp):
        return
    with _CHECKPOINT_OFFSETS_LOCK:
        _CHECKPOINT_OFFSETS[(str(path), *stamp)] = offsets
        _CHECKPOINT_OFFSETS.move_to_end((str(path), *stamp))
        while (len(_CHECKPOINT_OFFSETS) > 64
               or sum(len(item) for item in _CHECKPOINT_OFFSETS.values()) > 131072):
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


# LLM: 全部行先校验再允许lookup；稳定五元组命中省解析，重复ID仍last-wins；无缓冲读取避免seek命中旧read-ahead掩盖改写。
# 函数用途: 复用或建立原检查点行地址，作用域退出关闭文件，不写账本。
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
        through = handle.seek(0, 2)
        handle.seek(0)
        with _CHECKPOINT_OFFSETS_LOCK:
            cached = _CHECKPOINT_OFFSETS.get((str(path), *stamp)) if cache_entry_trustworthy(stamp[3]) else None
            if cached is not None:
                _CHECKPOINT_OFFSETS.move_to_end((str(path), *stamp))
        offsets: dict[str, tuple[int, int, bytes]] = cached if cached is not None else {}
        while cached is None and handle.tell() < through:
            start = handle.tell()
            raw = handle.readline(through - start)
            if not raw:
                raise OSError('conversation compact checkpoint ledger was truncated')
            row = _parse_row(raw)
            if row is not None:
                checkpoint_id = str(row.get('checkpoint_id') or '').strip()
                if checkpoint_id:
                    offsets[checkpoint_id] = (start, len(raw), hashlib.sha256(raw).digest())
            del row, raw
        if cached is None and cache_stat_signature(os.fstat(handle.fileno())) == stamp:
            _cache_offsets(path, stamp, offsets)

        # LLM: 同一已扫描述符按原字节hash复查选中行，旧代内容变动不能生成覆盖；追加不改变固定范围。
        # 函数用途: 返回原行的新解析对象，正文只随调用方当前消费而存活；缺ID由链验证器处理。
        def lookup(checkpoint_id):
            address = offsets.get(checkpoint_id)
            if address is None:
                return None
            offset, length, digest = address
            handle.seek(offset)
            raw = handle.read(length)
            if len(raw) != length or hashlib.sha256(raw).digest() != digest:
                raise OSError('conversation compact checkpoint source changed during reading')
            return _parse_row(raw)

        yield lookup
