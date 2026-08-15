# LLM: 路径规范化的唯一权威(体检实锤:全仓 7 种模式并存——多数缺 expandvars,
#   个别缺 resolve,环境变量在部分入口被静默忽略)。契约:expandvars →
#   expanduser → resolve(strict=False);空入参返回 None;OSError 按原样路径
#   兜底(不抛)。新代码一律用这里,存量在触碰时迁移。改动时同步检查
#   tests/test_common_path_normalize.py。
# 模块用途: 把用户/配置给的路径字符串变成规范绝对路径,环境变量和 ~ 都生效。
from __future__ import annotations

import os
from pathlib import Path


# 函数用途: 规范化一个路径;空值返回 None,解析失败回退到未 resolve 的形态。
def normalize_path(value: str | Path | None) -> Path | None:
    text = str(value or "").strip()
    if not text:
        return None
    expanded = Path(os.path.expandvars(text)).expanduser()
    try:
        return expanded.resolve(strict=False)
    except OSError:
        return expanded


__all__ = ["normalize_path"]
