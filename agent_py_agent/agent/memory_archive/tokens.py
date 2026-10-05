# LLM: 本模块拥有唯一估算口径及用量账；有界小对象直接编码，其余流式统计，不改变UTF8/字符上界、结构开销或异常回退，联测容量调用方。
#   2026-10-05 estcache：容器估算加"内容指纹 → 单条长度"进程内 LRU 缓存与序列/映射合成——只在
#   能证明"逐元素独立编码与整段编码逐位一致"时走合成；指纹不可用（非 str 键、未知对象）或元素
#   编码抛 TypeError/ValueError 时整体回退原口径；缓存不落盘、不跨进程，估算值不因缓存状态改变。
# 模块用途: 为记忆及模型输入提供保守估算和账本记录；估算不是供应商实耗，读取估算本身不写账。

from __future__ import annotations

"""conservative token estimation helpers for memory budgeting.

新手说明:
这里不是精确 tokenizer，也不假装精确。
它只给压缩前 hook、状态面板和预算提示一个偏保守的估算，避免中文、英文和工具大结果被严重低估。
"""

import json
import math
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..common.json_io import write_private_text_file_atomic


@dataclass(frozen=True)
class TurnTokenUsage:
    """Bundle for append_session_token_usage keyword parameters."""

    session_id: str
    turn_id: str
    input_tokens: int
    output_tokens: int
    tool_tokens: int
    created_at: str


# 小对象复用公开直接编码入口，避免高频窗口试算积累流式编码器闭包；大对象仍不复制整份JSON。
_SMALL_JSON_MAX_BYTES = 512 * 1024
# 小 JSON 判定允许的最大嵌套深度；超过视为大对象，按大对象路径处理（无物理单位）。
_SMALL_JSON_MAX_DEPTH = 64
# 单条长度缓存的条目上限：一个模型回合的活跃消息为千条量级，4096 条覆盖整批历史加少量抖动。
_ITEM_LENGTH_CACHE_MAX_ENTRY_COUNT = 4096
# 进程内单条长度缓存：指纹 → (字符数, UTF8字节数)；有界 LRU，满了淘汰最久未用条目。
_ITEM_LENGTH_CACHE: OrderedDict[object, tuple[int, int]] = OrderedDict()


# LLM: 同一sort/default JSON序列按结构上界选择有界直接编码或流式累计；估算数值及异常回退不变，最大单值/字典排序仍需内存。
# 函数用途: 有界估算输入，避免大历史副本和高频小请求的编码器积累，保持既有预算口径，不发请求或写账。
def estimate_tokens(payload: Any) -> int:
    composed = _composed_lengths(payload)
    if composed is None:
        composed = _payload_lengths(payload)
    chars, utf8_bytes = composed
    return _tokens_from_lengths(chars, utf8_bytes, payload)


# LLM: 已序列化片段必须来自原JSON格式；structure只保留原顶层形状供同一开销公式，不承担内容权威或错误修复。
# 函数用途: 对可重放大数组的JSON字符流使用原token估算，避免先物化整份数组；不发请求或写账。
def estimate_tokens_from_json_parts(parts, *, structure: Any) -> int:
    chars = utf8_bytes = 0
    iterator = iter(parts)
    try:
        for part in iterator:
            chars += len(part)
            utf8_bytes += sum(len(part[offset:offset + 8192].encode('utf-8')) for offset in range(0, len(part), 8192))
    finally:
        close = getattr(iterator, 'close', None)
        if close is not None:
            close()
    return _tokens_from_lengths(chars, utf8_bytes, structure)


# LLM: 普通值与显式JSON流共享唯一舍入和结构开销口径，不新增tokenizer或供应商usage推断。
# 函数用途: 根据完整字符/UTF8计数计算原保守token上界。
def _tokens_from_lengths(chars: int, utf8_bytes: int, structure: Any) -> int:
    if not chars:
        return 1
    return max(1, math.ceil(utf8_bytes / 3), math.ceil(chars / 3)) + _structured_overhead(structure)


def token_ledger_dir(root: str | Path) -> Path:

    return Path(root) / "memory_archive" / "tokens"


def append_session_token_usage(
    root: str | Path,
    *,
    usage: TurnTokenUsage,
) -> dict[str, Any]:

    path = token_ledger_dir(root) / f"{usage.session_id}.json"
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {}
    else:
        payload = {}
    turns = payload.get("turns", [])
    if not isinstance(turns, list):
        turns = []
    turn_total = max(0, int(usage.input_tokens)) + max(0, int(usage.output_tokens)) + max(0, int(usage.tool_tokens))
    turns.append(
        {
            "turn_id": str(usage.turn_id),
            "created_at": str(usage.created_at),
            "input_tokens": max(0, int(usage.input_tokens)),
            "output_tokens": max(0, int(usage.output_tokens)),
            "tool_tokens": max(0, int(usage.tool_tokens)),
            "turn_total": turn_total,
        }
    )
    cumulative = sum(int(item.get("turn_total", 0) or 0) for item in turns)
    written = {
        "session_id": str(usage.session_id),
        "turn_count": len(turns),
        "cumulative_tokens": cumulative,
        "turns": turns,
    }
    # LLM: 会话 token 账本属会话数据，落盘走私有原子写（0600/0700）；输出字节与原实现一致（无尾换行）。
    write_private_text_file_atomic(path, json.dumps(written, ensure_ascii=False, indent=2, sort_keys=True))
    return {
        "path": str(path),
        "session_id": str(usage.session_id),
        "turn_id": str(usage.turn_id),
        "turn_total": turn_total,
        "cumulative_tokens": cumulative,
        "turn_count": len(turns),
    }


# LLM: 仅可证明有界的内置结构使用公开dumps；其它仍流式。先完成JSON再裁决UTF8错误，后续JSON失败必须保留旧str回退优先级。
# 函数用途: 统计字符及UTF8字节；小请求避免反复创建流式编码器，大来源不另复制全文，不调用额外对象转换或写账。
def _payload_lengths(payload: Any) -> tuple[int, int]:
    if isinstance(payload, str):
        return len(payload), sum(len(payload[offset:offset + 8192].encode("utf-8")) for offset in range(0, len(payload), 8192))
    if _bounded_json_size(payload, _SMALL_JSON_MAX_BYTES) is not None:
        try:
            text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            text = str(payload)
        return len(text), len(text.encode("utf-8"))
    chars = utf8_bytes = 0
    encoding_error = None
    parts = json.JSONEncoder(ensure_ascii=False, sort_keys=True, default=str).iterencode(payload)
    while True:
        try:
            part = next(parts)
        except StopIteration:
            if encoding_error is not None:
                raise encoding_error from None
            return chars, utf8_bytes
        except (TypeError, ValueError):
            text = str(payload)
            return len(text), len(text.encode("utf-8"))
        chars += len(part)
        if encoding_error is None:
            try:
                utf8_bytes += sum(len(part[offset:offset + 8192].encode("utf-8")) for offset in range(0, len(part), 8192))
            except UnicodeEncodeError as exc:
                encoding_error = exc.with_traceback(None)


# LLM: 只读精确内置类型，不调用自定义转换或比较；返回JSON UTF8保守上界，未知/环/深树/超限仅退回原流式口径，不拒绝输入。
# 函数用途: 证明一次直接编码的副本有界；按最坏转义、分隔符和数字长度计量，不序列化正文或修改容器。
def _bounded_json_size(
    payload: Any, remaining: int, active: set[int] | None = None, depth: int = 0,
) -> int | None:
    if remaining < 0 or depth >= _SMALL_JSON_MAX_DEPTH:
        return None
    kind = type(payload)
    if kind is str:
        size = 6 * len(payload) + 2
        return size if size <= remaining else None
    if payload is None or kind is bool:
        return 5 if remaining >= 5 else None
    if kind is int:
        size = payload.bit_length() + 2
        return size if size <= remaining else None
    if kind is float:
        return 32 if remaining >= 32 else None
    if kind is list or kind is tuple or kind is dict:
        return _bounded_json_container_size(payload, remaining, active, depth)
    return None


# LLM: 调用方已验证精确内置容器；只跟踪当前祖先防环，共享子对象按每次出现计量，所有返回路径都释放本层身份。
# 函数用途: 累计容器标点、键与值的上界，超限或不能证明时立即停止，不复制整份容器或修改来源。
def _bounded_json_container_size(
    payload: list | tuple | dict, remaining: int, active: set[int] | None, depth: int,
) -> int | None:
    active = set() if active is None else active
    identity = id(payload)
    is_dict = type(payload) is dict
    size = 2 + (6 if is_dict else 2) * len(payload)
    if identity in active or size > remaining:
        return None
    active.add(identity)
    try:
        parts = (item for pair in payload.items() for item in pair) if is_dict else payload
        for part in parts:
            item_size = _bounded_json_size(part, remaining - size, active, depth + 1)
            if item_size is None:
                return None
            size += item_size
        return size
    finally:
        active.remove(identity)


def _structured_overhead(payload: Any) -> int:

    if isinstance(payload, dict):
        return max(1, len(payload) // 2)
    if isinstance(payload, list | tuple | set):
        return max(1, len(payload) // 4)
    return 0


# LLM: 缓存正确性边界：指纹相同 ⟹ JSON 编码相同才允许命中。str 用 hash 摘要，标量与 float 用值/repr
#   本身（避开 int/float/bool 的相等陷阱与 -0.0），容器递归组合；非 str 键与未知对象一律返回 None——
#   该值不缓存、dict 值遇到它放弃合成，回退原口径。缓存不落盘、不跨进程。
# 函数用途: 计算一个 JSON 值的内容指纹；不能证明与编码一一对应时返回 None（宁慢勿错）。
def _item_length_fingerprint(value: Any) -> object | None:
    kind = type(value)
    if kind is str:
        return ("s", hash(value))
    if kind is bool or value is None:
        return ("b", value)
    if kind is int:
        return ("i", value)
    if kind is float:
        return ("f", repr(value))
    if kind is dict:
        return _mapping_fingerprint(value)
    if kind is list or kind is tuple:
        return _sequence_fingerprint(value)
    return None


# 函数用途: 字典指纹：键必须是 str（否则 None），值递归取指纹；frozenset 让键顺序不影响摘要。
def _mapping_fingerprint(value: dict) -> object | None:
    entries: list[tuple[str, object]] = []
    for key, item in value.items():
        if type(key) is not str:
            return None
        item_fingerprint = _item_length_fingerprint(item)
        if item_fingerprint is None:
            return None
        entries.append((key, item_fingerprint))
    return ("d", frozenset(entries))


# 函数用途: 序列指纹：逐元素递归；任一元素不能取指纹就整体返回 None。
def _sequence_fingerprint(value: list | tuple) -> object | None:
    items: list[object] = []
    for item in value:
        item_fingerprint = _item_length_fingerprint(item)
        if item_fingerprint is None:
            return None
        items.append(item_fingerprint)
    return ("l", tuple(items))


# LLM: 命中判断只做"取表 + 刷新 LRU 位置"，绝不改动统计值；未命中先纯计算再记账。记账范围是顶层容器的
#   直接子项，以及顶层字典里列表值的元素（如 payload["messages"] 里的每条消息、schema 列表）；更深的子结构
#   纯算不记账——避免每层节点挤爆上限引发反复淘汰。缓存无显式锁：依赖 CPython GIL 保证单次 dict 操作原子，
#   并发时最坏是重复计算和淘汰顺序抖动，同指纹必得同一长度，数值不会错；改用无 GIL 解释器时这里要加锁。
# 函数用途: 取一个列表元素/字典值的（字符数、UTF8字节数）；指纹不可用或不能合成时返回 None。
def _cached_element_lengths(item: Any) -> tuple[int, int] | None:
    fingerprint = _item_length_fingerprint(item)
    if fingerprint is None:
        return None
    cached = _ITEM_LENGTH_CACHE.pop(fingerprint, None)
    if cached is not None:
        _ITEM_LENGTH_CACHE[fingerprint] = cached  # pop+重插即 LRU 刷新，且无并发 KeyError
        return cached
    lengths = _encode_element_lengths(item)
    if lengths is None:
        return None
    _ITEM_LENGTH_CACHE[fingerprint] = lengths
    while len(_ITEM_LENGTH_CACHE) > _ITEM_LENGTH_CACHE_MAX_ENTRY_COUNT:
        _ITEM_LENGTH_CACHE.popitem(last=False)
    return lengths


# 函数用途: 纯计算（不记账）的单值编码：str 按 JSON 值编码（含引号转义），容器递归，标量直接编码。
def _encode_element_lengths(item: Any) -> tuple[int, int] | None:
    if _bounded_json_size(item, _SMALL_JSON_MAX_BYTES) is None:
        # 超大或不能证明有界的元素不逐元素物化编码，放弃合成交原逻辑流式处理。
        return None
    if isinstance(item, str):
        return _json_string_lengths(item)
    kind = type(item)
    if kind is list or kind is tuple:
        return _composed_sequence_lengths(item, False)
    if kind is dict:
        return _composed_mapping_lengths(item, False)
    if kind is bool or kind is int or kind is float or item is None:
        return _payload_lengths(item)
    return None


# 函数用途: 一个"作为 JSON 值"的字符串的编码长度（含引号与转义）；顶层裸文本不走这里。
def _json_string_lengths(value: str) -> tuple[int, int]:
    text = json.dumps(value, ensure_ascii=False)
    return len(text), len(text.encode("utf-8"))


# LLM: 合成只覆盖可证明与整段编码逐位一致的容器：JSON 数组元素独立编码、", " 连接；元素/值不能
#   证明或出现异常（环、编码失败、自定义转换）时放弃合成，最终回退/抛出都由原逻辑决定。
#   use_cache=True 表示"本层直接子项记账"；容器值直接下钻一层，由下一层子项记账。
# 函数用途: 顶层合成分派；返回 (字符数, UTF8字节数)，不能安全合成时返回 None。
def _composed_lengths(payload: Any) -> tuple[int, int] | None:
    kind = type(payload)
    if kind is list or kind is tuple:
        return _composed_sequence_lengths(payload, True)
    if kind is dict:
        return _composed_mapping_lengths(payload, True)
    return None


# LLM: 合成内部信号：某元素/值无法证明与整段编码一致（不可缓存形状、超界、非 str 键）时抛出，
#   由合成顶层统一吞成"回退原口径"，绝不让它离开本模块。
# 类用途: 标记"这一层容器不能安全合成"，不是用户可见错误。
class _CompositionUnsupported(Exception):
    pass


# 函数用途: 合成序列的（字符数、UTF8字节数）="[" + 各元素编码 + ", " 连接 + "]"；元素按 use_cache 取。
def _composed_sequence_lengths(items: list | tuple, use_cache: bool) -> tuple[int, int] | None:
    chars = utf8_bytes = 0
    try:
        for item in items:
            item_chars, item_bytes = _required_element_lengths(item, use_cache)
            chars += item_chars + 2
            utf8_bytes += item_bytes + 2
    except Exception:  # noqa: BLE001 - 环/编码失败/自定义转换异常都放弃合成，最终行为由原逻辑决定
        return None
    return max(chars, 2), max(utf8_bytes, 2)


# 函数用途: 取一个序列元素的长度；不能安全合成时抛内部信号让容器层整体回退。
def _required_element_lengths(item: Any, use_cache: bool) -> tuple[int, int]:
    if use_cache:
        lengths = _cached_element_lengths(item)
    else:
        lengths = _encode_element_lengths(item)
    if lengths is None:
        raise _CompositionUnsupported()
    return lengths


# 函数用途: 合成 sort_keys 字典的（字符数、UTF8字节数）；只处理全 str 键，值下钻容器或独立编码。
def _composed_mapping_lengths(mapping: dict, use_cache: bool) -> tuple[int, int] | None:
    chars = utf8_bytes = 0
    try:
        for key in sorted(mapping):
            entry_chars, entry_bytes = _required_mapping_entry_lengths(mapping, key, use_cache)
            chars += entry_chars + 2
            utf8_bytes += entry_bytes + 2
    except Exception:  # noqa: BLE001 - 环/编码失败/自定义转换异常都放弃合成，最终行为由原逻辑决定
        return None
    return max(chars, 2), max(utf8_bytes, 2)


# 函数用途: 取一个字典条目 "键: 值" 的编码长度；不能安全合成时抛内部信号。
def _required_mapping_entry_lengths(mapping: dict, key: object, use_cache: bool) -> tuple[int, int]:
    if type(key) is not str:
        raise _CompositionUnsupported()
    value_lengths = _composed_value_lengths(mapping[key], use_cache)
    if value_lengths is None:
        raise _CompositionUnsupported()
    key_text = json.dumps(key, ensure_ascii=False)
    return len(key_text) + 2 + value_lengths[0], len(key_text.encode("utf-8")) + 2 + value_lengths[1]


# 函数用途: 一个字典值的编码长度：序列/字典继续合成，str 与标量独立编码，超大/未知对象放弃合成。
def _composed_value_lengths(value: Any, use_cache: bool) -> tuple[int, int] | None:
    kind = type(value)
    if kind is list or kind is tuple:
        return _composed_sequence_lengths(value, use_cache)
    if kind is dict:
        return _composed_mapping_lengths(value, use_cache)
    if kind is str:
        if _bounded_json_size(value, _SMALL_JSON_MAX_BYTES) is None:
            return None
        return _json_string_lengths(value)
    if kind is bool or kind is int or kind is float or value is None:
        return _payload_lengths(value)
    return None
