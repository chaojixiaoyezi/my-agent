"""热路径 JSON 编码：与同参 json.dumps 逐字节一致，但不在每次调用时做类型查找。

默认参数的 json.dumps 复用模块级编码器，其 default 与两个分隔符留在类上（实例的内联槽位为空），
每次编码都要回到类型上查找；非默认参数的调用还会每次新建编码器实例。在 python3.14t 关闭 GIL、
128 线程时这些查找会落到全局类型锁上（R3 §4.7）。这里为 ensure_ascii/sort_keys/compact 的
8 种组合各建一个编码器，所有属性都在实例上显式赋值，编码时不再触发类型查找。选编码器用分支读模块全局，
不查以元组为键的共享字典：G1 实测 128 线程同查这样的字典时，单次查找吞吐只剩 16 线程时的约 5%，整次 dumps 约 0.2–0.4 倍。
需要 indent、cls、自定义 default、skipkeys 或 allow_nan=False 的调用继续使用 json.dumps。
"""

from __future__ import annotations

import json


# LLM: 与 json.JSONEncoder.default 的报错文本逐字一致（差分测试钉住），作为实例属性传给编码器，
#   使 C 编码器调用它时不必经由类型查找绑定方法。
# 函数用途: 遇到不可序列化对象时抛出与标准库相同的 TypeError。
def _not_serializable(o: object) -> object:
    raise TypeError(f"Object of type {o.__class__.__name__} is not JSON serializable")


# LLM: 键为 (ensure_ascii, sort_keys, compact)；非紧凑时显式给出标准库默认分隔符 (", ", ": ")，
#   紧凑时为 (",", ":")。其余参数保持 json.dumps 默认（check_circular、allow_nan 为真，indent 为空）。
_ENCODERS: dict[tuple[bool, bool, bool], json.JSONEncoder] = {
    (ensure_ascii, sort_keys, compact): json.JSONEncoder(
        ensure_ascii=ensure_ascii,
        sort_keys=sort_keys,
        separators=(",", ":") if compact else (", ", ": "),
        default=_not_serializable,
    )
    for ensure_ascii in (False, True)
    for sort_keys in (False, True)
    for compact in (False, True)
}


# 按 (ensure_ascii, sort_keys, compact) 绑定的 8 个编码器，供 dumps 分支直接读取。
_UNICODE = _ENCODERS[(False, False, False)]
_UNICODE_COMPACT = _ENCODERS[(False, False, True)]
_UNICODE_SORTED = _ENCODERS[(False, True, False)]
_UNICODE_SORTED_COMPACT = _ENCODERS[(False, True, True)]
_ASCII = _ENCODERS[(True, False, False)]
_ASCII_COMPACT = _ENCODERS[(True, False, True)]
_ASCII_SORTED = _ENCODERS[(True, True, False)]
_ASCII_SORTED_COMPACT = _ENCODERS[(True, True, True)]


# LLM: 参数按真值（与 json.dumps 相同）选编码器；输出等同于
#   json.dumps(obj, ensure_ascii=..., sort_keys=..., separators=(",", ":") if compact else None)。
# 函数用途: 热路径上的默认形 JSON 编码入口。
def dumps(obj: object, *, ensure_ascii: bool = True, sort_keys: bool = False, compact: bool = False) -> str:
    if ensure_ascii:
        if sort_keys:
            encoder = _ASCII_SORTED_COMPACT if compact else _ASCII_SORTED
        else:
            encoder = _ASCII_COMPACT if compact else _ASCII
    elif sort_keys:
        encoder = _UNICODE_SORTED_COMPACT if compact else _UNICODE_SORTED
    else:
        encoder = _UNICODE_COMPACT if compact else _UNICODE
    return encoder.encode(obj)


__all__ = ["dumps"]
