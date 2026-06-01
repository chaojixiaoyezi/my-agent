# LLM: Model-visible ref helpers keep live tool payloads on current-layout paths.
# 模块用途: 提供模型可见路径投影；当前工具输出遇到旧 data/subagents 路径时直接省略，不用占位符遮羞。

from __future__ import annotations

from pathlib import Path
from typing import Any


# LLM: is_legacy_subagent_path recognizes only the old internal subagent runtime layout.
# 函数用途: 判断字符串是否指向旧式 data/subagents 运行目录；普通用户正文不在这里改写。
def is_legacy_subagent_path(value: str) -> bool:
    text = str(value or "").replace("\\", "/")
    return "/data/subagents/" in text or text.startswith("data/subagents/")


# LLM: current_model_ref returns a live model-facing ref only when it is not a legacy runtime path.
# 函数用途: 当前状态/调度 payload 只暴露新布局路径或普通文件名；旧内部路径返回空字符串。
def current_model_ref(value: Any, *, basename_for_legacy: bool = False) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if not is_legacy_subagent_path(text):
        return text
    if basename_for_legacy:
        return Path(text.replace("\\", "/")).name
    return ""


# LLM: current_model_ref_list keeps refs unique while dropping legacy runtime paths.
# 函数用途: 过滤模型可见 ref 列表，避免旧路径和空字符串进入状态结果。
def current_model_ref_list(values: Any, *, basename_for_legacy: bool = False, limit: int = 0) -> list[str]:
    if not isinstance(values, list | tuple | set):
        return []
    refs: list[str] = []
    seen: set[str] = set()
    for value in values:
        ref = current_model_ref(value, basename_for_legacy=basename_for_legacy)
        if not ref or ref in seen:
            continue
        seen.add(ref)
        refs.append(ref)
        if limit and len(refs) >= limit:
            break
    return refs


__all__ = [
    "current_model_ref",
    "current_model_ref_list",
    "is_legacy_subagent_path",
]
