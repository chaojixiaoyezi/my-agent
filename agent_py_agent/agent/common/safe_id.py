# LLM: 安全标识符归一的唯一权威(体检实锤:此前 4 处各自实现,默认值
#   main/run/unknown 三种、字符集两种〔有无"."〕悄悄分叉)。契约:只保留
#   字母数字与 -_.;空结果回退 default;max_len>0 时截断。调用方语义差异
#   (默认值/长度)全部经参数表达,不再复制实现。改动时同步检查
#   tests/test_common_safe_id.py 与四个迁移调用点。
# 模块用途: 把任意文本变成能安全用作目录名/键名的短标识,全仓只此一家。
from __future__ import annotations

import re

_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_.-]+")


# 函数用途: 归一标识符;default 是空结果的回退,max_len=0 表示不截断。
def safe_id(value: object, *, max_len: int = 0, default: str = "main") -> str:
    text = _UNSAFE_RE.sub("-", str(value or "")).strip("-_")
    if max_len > 0:
        text = text[:max_len].strip("-_")
    return text or default


__all__ = ["safe_id"]
