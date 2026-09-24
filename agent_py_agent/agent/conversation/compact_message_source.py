# LLM: 原Compact的临时provider数组投影；factory必须重放同一冻结来源，非持久历史/覆盖权威，Auxiliary运输仍只接完整列表。
# 模块用途: 按原JSON参数编码历史数组并复用唯一token公式，超大来源直接进入原字符分段器。
from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from ..memory_archive import estimate_tokens
from ..memory_archive.tokens import estimate_tokens_from_json_parts


# LLM: factory是同一冻结来源的准备层投影，可读盘且须关闭上游；容器不缓存正文，不能进入纯请求投影合同。
# 类用途: 携带可重复读取的原生消息数组，按需物化单请求或流式编码完整摘要来源。
@dataclass(frozen=True)
class CompactMessageSource:
    factory: Callable[[], Iterable[dict[str, Any]]]

    # LLM: 不缓存已读消息，失败/提前退出关闭上游；调用者不能把一次迭代器当作第二次重放。
    # 函数用途: 顺序读取一遍独立provider消息。
    def __iter__(self):
        iterator = iter(self.factory())
        try:
            yield from iterator
        finally:
            close = getattr(iterator, 'close', None)
            if close is not None:
                close()

    # LLM: 标点/JSON参数与原数组一致；单消息用公开encode避免反复iterencode闭包积累，峰值至少容纳最大消息编码。
    # 函数用途: 一条消息一个编码块地输出同一JSON数组，不构造整份历史list或字符串。
    def json_parts(self, *, sort_keys=False, default=None):
        encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=sort_keys, default=default)
        yield '['
        iterator = iter(self)
        try:
            for index, message in enumerate(iterator):
                if index:
                    yield ', '
                yield encoder.encode(message)
        finally:
            iterator.close()
        yield ']'

    # LLM: tail来自同次已准备的工具投影，追加顺序不变；不把其refs当作文字来源。
    # 函数用途: 将联合工具历史接到原会话来源末尾，不复制已有数组。
    def with_tail(self, tail):
        # LLM: yield from将提前close传给当前子迭代器；不能用不传播close的chain持有打开文件。
        # 函数用途: 顺序重放原来源及工具尾部，结束或失败都释放当前来源。
        def combined():
            yield from self
            yield from tail

        return CompactMessageSource(combined)


# LLM: 只有显式CompactMessageSource替换数组编码；其余值仍使用原估算器，顶层字段数和原JSON参数保持。
# 函数用途: 完整计量包含可重放历史的原请求，避免source对象被default=str误算。
def estimate_compact_payload(payload):
    if not any(isinstance(value, CompactMessageSource) for value in payload.values()):
        return estimate_tokens(payload)
    return estimate_tokens_from_json_parts(_payload_json_parts(payload), structure=payload)


# LLM: 原数组顶层开销依赖完整消息数；只累计None槽位，JSON值仍从同一来源流式编码，公式归tokens。
# 函数用途: 按原数组口径估算近期尾部，不因一个大回合先构造全部provider正文。
def estimate_compact_messages(source):
    shape = []

    # LLM: 一个编码遍历同时记录数组长度；不保存消息对象，也不改变原来源顺序。
    # 函数用途: 给原结构开销计算提供轻量条数形状。
    def counted():
        iterator = iter(source)
        try:
            for message in iterator:
                shape.append(None)
                yield message
        finally:
            iterator.close()

    parts = CompactMessageSource(counted).json_parts(sort_keys=True, default=str)
    return estimate_tokens_from_json_parts(parts, structure=shape)


# LLM: 仅处理本模块调用方构造的顶层字符串键字典；排序、空格、default和ensure_ascii与原tokens一致。
# 函数用途: 把固定请求字段与完整可重放数组编码成同一个JSON字符流，不改变原结构开销。
def _payload_json_parts(payload):
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, default=str)
    yield '{'
    for index, key in enumerate(sorted(payload)):
        if index:
            yield ', '
        yield encoder.encode(key)
        yield ': '
        value = payload[key]
        if isinstance(value, CompactMessageSource):
            yield from value.json_parts(sort_keys=True, default=str)
        else:
            yield encoder.encode(value)
    yield '}'
