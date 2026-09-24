# LLM: 仅重放原canonical行地址及hash，不存正文、不写索引或授予覆盖；范围切片共享同次冻结身份，联测选择器及Compact。
# 模块用途: 让历史来源按需读取独立消息对象，解除全量正文驻留，迟到追加留给下一次来源。
from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path

from ..runtime_errors import DataCorruptionError
from .message_scan import _message_from_line, _validate_boundary
from .models import MessageLogEntry


# LLM: 地址只是本次读取投影；hash绑定完整原字节，不能用ID/长度相同替代内容一致性。
# 类用途: 保存一行的位置与完整性证据，不保存正文或包含native正文的metadata。
@dataclass(frozen=True)
class MessageRowAddress:
    offset: int
    length: int
    digest: bytes


# LLM: 每次迭代重开并验证原文件，退出关闭FD；短路组合读也显式关闭，切片仅复制轻量地址。
# 类用途: 提供len、索引、切片和多次读取一致的只读消息序列，文件变化时明确拒绝。
@dataclass(frozen=True, eq=False)
class MessageSnapshotRows(Sequence[MessageLogEntry]):
    path: Path
    thread_id: str
    through: int
    identity: tuple[int, int] | None
    addresses: tuple[MessageRowAddress, ...]
    interrupt_check: Callable[[], bool] | None = None

    # LLM: 不读文件或正文，条数只来自已验证选择结果。
    # 函数用途: 返回冻结范围的消息数量。
    def __len__(self):
        return len(self.addresses)

    # LLM: 切片保持原冻结绑定；单行读取也完整校验且关闭生成器，不允许绕过hash。
    # 函数用途: 按地址选择子范围或读取一个独立消息对象。
    def __getitem__(self, key):
        if isinstance(key, slice):
            return replace(self, addresses=self.addresses[key])
        selected = replace(self, addresses=(self.addresses[key],))
        iterator = iter(selected)
        try:
            return next(iterator)
        finally:
            iterator.close()

    # LLM: 正文每次重新解析，原inode与固定尾界必验；append合法，改写/截短不能成为新冻结来源。
    # 函数用途: 用一个短生命周期FD顺序重放选中行，取消或读取失败时关闭文件。
    def __iter__(self):
        from .compact_guard import raise_if_compact_interrupted

        raise_if_compact_interrupted(self.interrupt_check)
        if not self.addresses:
            return
        with self.path.open('rb') as handle:
            stat = os.fstat(handle.fileno())
            if self.identity != (stat.st_dev, stat.st_ino):
                raise DataCorruptionError('canonical message source changed during replay')
            _validate_boundary(handle, self.through, stat.st_size)
            for address in self.addresses:
                raise_if_compact_interrupted(self.interrupt_check)
                handle.seek(address.offset)
                raw = handle.read(address.length)
                if len(raw) != address.length or hashlib.sha256(raw).digest() != address.digest:
                    raise DataCorruptionError('canonical message source changed during replay')
                row = _message_from_line(raw, self.thread_id)
                del raw
                raise_if_compact_interrupted(self.interrupt_check)
                yield row
                del row
            raise_if_compact_interrupted(self.interrupt_check)

    # LLM: 值比较仅为原序列调用者兼容；不缓存比较读出的正文，线程/覆盖权不由相等运算决定。
    # 函数用途: 保持与既有tuple/list结果的逐值比较行为。
    def __eq__(self, other):
        if not isinstance(other, Sequence) or len(self) != len(other):
            return False
        with closing(iter(self)) as rows:
            other_rows = iter(other)
            try:
                return all(a == b for a, b in zip(rows, other_rows))
            finally:
                close = getattr(other_rows, 'close', None)
                if close is not None:
                    close()

    # LLM: predicate仅决定保留，地址、顺序和冻结身份不变；调用者不得从正文推断权限。
    # 函数用途: 在同次来源上选择前台或可摘要行，避免构造正文列表。
    def filtered(self, predicate):
        with closing(iter(self)) as rows:
            selected = tuple(address for address, row in zip(self.addresses, rows) if predicate(row))
        return replace(self, addresses=selected)

    # LLM: 提交前检查本次实际覆盖的完整字节，检查本身不推进head或游标。
    # 函数用途: 完整耗尽来源以发现摘要期间的改写，保留原失败语义。
    def validate(self):
        for _row in self:
            pass


# LLM: 磁盘来源只保留地址；已有内存序列沿原对象引用，不能把一遍迭代器当可重放来源。
# 函数用途: 统一筛选消息，保持顺序和完整正文。
def filtered_message_rows(rows, predicate):
    return rows.filtered(predicate) if isinstance(rows, MessageSnapshotRows) else tuple(row for row in rows if predicate(row))


# LLM: 同一冻结文件的多个子范围合并地址，不重新筛选或复制正文；内存输入继续保留原引用。
# 函数用途: 合并近期尾部和媒体保护后缀，不改变原消息顺序。
def concatenate_message_rows(*parts):
    nonempty = [part for part in parts if part]
    if not nonempty:
        return ()
    first = nonempty[0]
    if isinstance(first, MessageSnapshotRows) and all(
        isinstance(part, MessageSnapshotRows) and (part.path, part.thread_id, part.through, part.identity)
        == (first.path, first.thread_id, first.through, first.identity) for part in nonempty
    ):
        return replace(first, addresses=tuple(address for part in nonempty for address in part.addresses))
    return tuple(row for part in nonempty for row in part)


# LLM: 普通内存序列无需关闭，文件生成器在短路/异常时显式close；只管理读取资源，不拥有选择或覆盖语义。
# 函数用途: 为分区、原生投影及提交校验提供一个可安全提前退出的读取范围。
@contextmanager
def message_rows_iterator(rows):
    iterator = iter(rows)
    try:
        yield iterator
    finally:
        close = getattr(iterator, 'close', None)
        if close is not None:
            close()
