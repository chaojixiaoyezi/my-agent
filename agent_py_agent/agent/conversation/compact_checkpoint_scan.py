# LLM: 只在单次读取中索引原账本行地址/hash，不落盘、不创建提交或覆盖权威；调用方仍验证原head和完整链。
# 模块用途: 固定同一文件描述符的EOF，逐行检查全部JSON后按需重读，避免保留orphan和旧摘要正文。
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from pathlib import Path


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


# LLM: 全部行先校验再允许lookup；重复ID按原规则last-wins，只有索引及最大单行驻留，追加留给下次读取。
# 函数用途: 建立本次描述符内的临时查找器，作用域退出即关闭文件；不跨读取缓存，不写文件或锁。
@contextmanager
def checkpoint_row_lookup(path: Path):
    try:
        handle = path.open('rb')
    except FileNotFoundError:
        # LLM: 原不存在账本视为空集合，非空head随后由原chain reader报告缺链。
        # 函数用途: 保留缺文件时原指针验证顺序，不把不存在的行当已提交。
        def missing(_checkpoint_id):
            return None
        yield missing
        return
    with handle:
        through = handle.seek(0, 2)
        handle.seek(0)
        offsets: dict[str, tuple[int, int, bytes]] = {}
        while handle.tell() < through:
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
