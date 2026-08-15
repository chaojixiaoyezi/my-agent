"""结果端文本的首记号切分:纯字面切分,零语义。

结果端结论常写成"结论词 + 高基数尾巴"的一句话(如 'diverted ref=<hex> t=<n>'):
整值高基数、折叠后失明,但开头那个结论词才是判据眼里的信号。按空白/常见结构分隔符
取开头一段,供引擎少数派车道与 spec 集合匹配在文本字段上按首记号比对(而非要求整串相等)。
"""

from __future__ import annotations

import re

# 分隔符:空白 + 常见结构符(冒号/分号/逗号/竖线/等号/括号/引号/反引号)。
_HEAD_SPLIT = re.compile(r"[\s:;,|=\[\](){}<>\"'`]+")
_HEAD_CAP = 48


def head_token(text: str) -> str:
    """取文本开头到第一个分隔符之前的一段(截断到 _HEAD_CAP);空/纯分隔返回空串。"""
    stripped = str(text).strip()
    if not stripped:
        return ""
    return _HEAD_SPLIT.split(stripped, maxsplit=1)[0][:_HEAD_CAP]


__all__ = ["head_token"]
