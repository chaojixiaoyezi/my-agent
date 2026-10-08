# LLM: 原Compact的临时provider数组投影；factory必须重放同一冻结来源，非持久历史/覆盖权威，Auxiliary运输仍只接完整列表。
# 模块用途: 按原JSON参数编码历史数组并复用唯一token公式，超大来源直接进入原字符分段器。
from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from ..memory_archive import estimate_tokens
from ..memory_archive.tokens import estimate_tokens_from_json_parts

# 摘要来源里 Responses 加密思考密文的固定占位：密文只有原模型在原协议里能解开，摘要模型读到的只是一段 base64，
# 每个助手轮约 3.6K 字符（09-30 生产实测 17 块、60712 字符，约 2 万估算 token）。固定字符串保证两遍来源逐字一致。
REASONING_CIPHERTEXT_PLACEHOLDER = "[encrypted reasoning omitted from summary source]"
# 单次压缩请求瘦身时替换较早工具结果正文的固定占位：只写原长度，不含正文；固定字符串保证两遍来源逐字一致。
TOOL_OUTPUT_PLACEHOLDER = "[compact-omitted-tool-output chars={chars}] 这条较早的工具输出已为压缩请求省略，完整内容仍在会话归档中。"
# 瘦身时永远原样保留的最近工具结果条数：摘要至少要看到最新的工具结果原文。
SHRINK_KEEP_RECENT_TOOL_RESULT_COUNT = 2


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

    # LLM: 逐条套用纯函数投影（不改原消息），早退/失败时显式关闭上游迭代器（与 with_tail 同一关闭合同）；
    #   投影必须确定：同一冻结来源两遍读出逐字相同，CompactTextSource 的两遍一致校验才仍然成立。
    # 函数用途: 得到一份按消息投影后的同序来源，供分段摘要编码。
    def projected(self, transform: Callable[[dict[str, Any]], dict[str, Any]]):
        # 函数用途: 顺序重放原来源并逐条投影，结束或提前关闭都释放原来源。
        def mapped():
            iterator = iter(self)
            try:
                for message in iterator:
                    yield transform(message)
            finally:
                iterator.close()

        return CompactMessageSource(mapped)

    # LLM: tail来自同次已准备的工具投影，追加顺序不变；不把其refs当作文字来源。
    # 函数用途: 将联合工具历史接到原会话来源末尾，不复制已有数组。
    def with_tail(self, tail):
        # LLM: yield from将提前close传给当前子迭代器；不能用不传播close的chain持有打开文件。
        # 函数用途: 顺序重放原来源及工具尾部，结束或失败都释放当前来源。
        def combined():
            yield from self
            yield from tail

        return CompactMessageSource(combined)


# LLM: 分段摘要来源的唯一消息投影：只把 responses_reasoning 块里 item.encrypted_content 换成固定占位，保留块位置、
#   model、id 与可读的 summary_text；其它块（含 thinking 正文、工具参数与结果）原样。不改原消息，不含它的消息原对象返回。
#   只用于把历史编码成摘要文字的分段路径；整请求原协议发送的路径照旧带密文（同后端能用它续推理）。
# 函数用途: 去掉摘要模型读不懂的加密思考密文，减少分段摘要的来源量而不丢可读内容。
def summary_source_message(message: dict[str, Any]) -> dict[str, Any]:
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list) or not any(_has_reasoning_ciphertext(block) for block in content):
        return message
    return {**message, "content": [
        {**block, "item": {**block["item"], "encrypted_content": REASONING_CIPHERTEXT_PLACEHOLDER}}
        if _has_reasoning_ciphertext(block) else block
        for block in content
    ]}


# 函数用途: 判断一个内容块是不是带加密密文的 Responses 思考块。
def _has_reasoning_ciphertext(block: object) -> bool:
    item = block.get("item") if isinstance(block, dict) and block.get("type") == "responses_reasoning" else None
    return isinstance(item, dict) and isinstance(item.get("encrypted_content"), str)


# LLM: 单次缓存面压缩请求超预算时的瘦身投影（不是分段）：按时间顺序把最早的 tool_result 文字正文换成固定占位
#   （保留块位置、tool_use_id 与其它字段，最近 SHRINK_KEEP_RECENT_TOOL_RESULT_COUNT 条不动），直到省出的估算 token
#   不少于 excess_tokens。只改交给摘要的临时副本，不改 rows、不改分段来源；省不够返回 None。带图或非文字的
#   tool_result 不动。结果来源重放同一冻结元组，两遍逐字一致。
# 类用途: 瘦身结果：新的可重放来源和被替换的条数。
@dataclass(frozen=True)
class ShrunkMessageSource:
    source: CompactMessageSource
    replaced: int


# 函数用途: 把最早的工具结果正文换成占位，让单次压缩请求装进窗口而不放弃主请求前缀；省不够返回 None。
def shrink_tool_outputs(source, excess_tokens: int, *, keep_recent: int = SHRINK_KEEP_RECENT_TOOL_RESULT_COUNT):
    messages = list(source)
    slots = [
        (index, position)
        for index, message in enumerate(messages)
        for position, block in enumerate(_content_blocks(message))
        if _tool_result_text(block) is not None
    ]
    saved = replaced = 0
    for index, position in slots[:max(0, len(slots) - max(0, int(keep_recent)))]:
        if saved >= excess_tokens:
            break
        block = messages[index]["content"][position]
        text = _tool_result_text(block)
        placeholder = TOOL_OUTPUT_PLACEHOLDER.format(chars=len(text))
        if len(text) <= len(placeholder):
            continue
        saved += max(0, estimate_tokens(text) - estimate_tokens(placeholder))
        content = list(messages[index]["content"])
        content[position] = {**block, "content": placeholder}
        messages[index] = {**messages[index], "content": content}
        replaced += 1
    if not replaced or saved < excess_tokens:
        return None
    frozen = tuple(messages)
    return ShrunkMessageSource(CompactMessageSource(lambda: iter(frozen)), replaced)


# 函数用途: 返回一条消息的内容块列表；content 不是列表时返回空元组。
def _content_blocks(message: object) -> tuple:
    content = message.get("content") if isinstance(message, dict) else None
    return tuple(content) if isinstance(content, list) else ()


# 函数用途: 取出 tool_result 块里可替换的纯文字正文；带图或非文字块返回 None（不动）。
def _tool_result_text(block: object) -> str | None:
    if not isinstance(block, dict) or block.get("type") != "tool_result":
        return None
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list) and content and all(
        isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str) for item in content
    ):
        return "\n".join(item["text"] for item in content)
    return None


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
