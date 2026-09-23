# LLM: 仅为现有摘要循环提供顺序字符视图，不拥有canonical字节覆盖或检查点；两遍序列化一致才可返回最终摘要。
# 模块用途: 流式读JSON编码片段，保留当前尝试窗口，避免把整份历史再复制成一个大字符串。
from __future__ import annotations

import hashlib
from collections import deque
from collections.abc import Callable, Iterable

from .compact_guard import ConversationCompactError, raise_if_compact_interrupted

_CHUNK_CHARS = 8192


# LLM: factory必须重放同一来源；位置单位是Unicode字符，与消息文件字节cursor无关。内存仍至少容纳最大编码单值。
# 类用途: 首遍统计长度和摘要，第二遍按原分段器需求前进；成功消费后释放旧窗口，失败或取消时关闭迭代器。
class CompactTextSource:
    # LLM: 首遍不留正文，JSONEncoder本身可能暂存最大单字符串；停止回调每个有限片段检查，不启动线程或写文件。
    # 函数用途: 冻结本轮来源的字符总数与hash，并准备同一来源的第二次顺序读取。
    def __init__(self, factory: Callable[[], Iterable[str]], interrupt_check=None):
        self._interrupt_check = interrupt_check
        self._total = 0
        expected = hashlib.sha256()
        pieces = self._pieces(factory())
        try:
            for piece in pieces:
                self._total += len(piece)
                expected.update(piece.encode("utf-8"))
        finally:
            pieces.close()
        self._expected = expected.digest()
        self._iterator = iter(self._pieces(factory()))
        self._digest = hashlib.sha256()
        self._chunks: deque[str] = deque()
        self._start = 0
        self._end = 0

    # LLM: 只拆编码输出而不重编码，保留json.dumps的精确字符序列；finally关闭上游生成器以释放大单值引用。
    # 函数用途: 将编码器可能给出的大字符串分成小块，在扫描与取消之间留检查点。
    def _pieces(self, chunks: Iterable[str]):
        iterator = iter(chunks)
        try:
            for chunk in iterator:
                for offset in range(0, len(chunk), _CHUNK_CHARS):
                    raise_if_compact_interrupted(self._interrupt_check)
                    yield chunk[offset:offset + _CHUNK_CHARS]
        finally:
            close = getattr(iterator, "close", None)
            if close is not None:
                close()

    # LLM: 长度来自首遍全部来源；不得用当前缓存长度误报分段完成。
    # 函数用途: 返回本轮JSON或文本来源的总字符数。
    def __len__(self):
        return self._total

    # LLM: 仅允许未释放范围的连续正向切片；重试可重读当前窗口，不能跳过已消费来源再假装完整。
    # 函数用途: 按现有分段器的字符区间提供正文，最多缓存探测到的窗口及一个编码块。
    def __getitem__(self, span: slice) -> str:
        if not isinstance(span, slice) or span.step not in (None, 1):
            raise ValueError("compact text source requires a contiguous slice")
        start, end = span.start, span.stop
        if not isinstance(start, int) or not isinstance(end, int) or not self._start <= start <= end <= self._total:
            raise ValueError("compact text source range is unavailable")
        while self._end < end:
            if not self._advance():
                raise self._changed()
        parts, cursor = [], self._start
        for chunk in self._chunks:
            following = cursor + len(chunk)
            if following > start and cursor < end:
                parts.append(chunk[max(0, start - cursor):min(len(chunk), end - cursor)])
            cursor = following
            if cursor >= end:
                break
        return "".join(parts)

    # LLM: 每个编码块仅进入hash一次；超过首遍长度提前失败，截短和等长改写由finish核对，不给来源覆盖。
    # 函数用途: 从第二遍编码器取下一块并更新当前窗口，不产生网络或持久状态。
    def _advance(self) -> bool:
        try:
            chunk = next(self._iterator)
        except StopIteration:
            return False
        self._end += len(chunk)
        if self._end > self._total:
            raise self._changed()
        self._digest.update(chunk.encode("utf-8"))
        self._chunks.append(chunk)
        return True

    # LLM: 只有摘要循环成功消费的终点可释放，重试前不得释放；与进度展示及canonical覆盖账无关系。
    # 函数用途: 摘要一个区间成功后移除旧正文，只保留已预读但尚未消费的尾部。
    def discard_before(self, offset: int) -> None:
        if not self._start <= offset <= self._end:
            raise ValueError("compact source release offset is invalid")
        while self._chunks and self._start + len(self._chunks[0]) <= offset:
            self._start += len(self._chunks.popleft())
        if self._chunks and offset > self._start:
            self._chunks[0] = self._chunks[0][offset - self._start:]
            self._start = offset

    # LLM: 最后一个片段成功后必须检查EOF/hash及取消，之后才允许上层原writer提交；不以进度100%证明来源一致。
    # 函数用途: 验证第二遍完整读到同一来源；追加、截短和改写均拒绝返回最终摘要。
    def finish(self) -> None:
        raise_if_compact_interrupted(self._interrupt_check)
        if self._end != self._total or self._advance() or self._digest.digest() != self._expected:
            raise self._changed()
        raise_if_compact_interrupted(self._interrupt_check)

    # LLM: 本地读取失败只返回typed Compact错误，不附来源正文或敏感参数。
    # 函数用途: 为两遍来源不一致产生统一错误，原检查点保持不变。
    @staticmethod
    def _changed():
        return ConversationCompactError("摘要来源在读取期间改变，保留原始历史", code="COMPACT_SOURCE_CHANGED")

    # LLM: context manager只管理临时迭代器，没有文件/线程/持久清理副作用。
    # 函数用途: 进入一次摘要来源读取作用域。
    def __enter__(self):
        return self

    # LLM: 正常或异常退出都释放窗口和生成器，不能吞掉供应商错误、取消或来源不一致。
    # 函数用途: 离开分段链时解除临时正文引用。
    def __exit__(self, *_exc):
        try:
            self._iterator.close()
        finally:
            self._chunks.clear()
