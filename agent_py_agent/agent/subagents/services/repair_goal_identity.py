# LLM: Repair goal identity is a fallback when the model forgets the formal repair_contract fields.
# 模块用途: 从自然语言修复目标里提取稳定文件 ref，让 create/schedule 能复用同一个 repair owner。

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_ABS_PATH_RE = re.compile(r"(?<![\w.-])/(?:[^\s\"'<>`，。；：、)）\]}】]+/)*[^\s\"'<>`，。；：、)）\]}】]+")
_FILE_TOKEN_RE = re.compile(r"(?<![\w./-])[\w.-]+\.(?:html?|xlsx|csv|json|md|txt|py|ts|tsx|js|jsx|css)\b")
_REPAIR_TOKENS = (
    "repair",
    "fix",
    "修复",
    "补齐",
    "纠正",
    "整改",
    "收尾",
    "继续完成",
)


# LLM: repair_goal_targets returns stable target refs only for repair-like child creation.
# 函数用途: 当 LLM 漏传 repair_contract 时，从 goal/agent_name/role 中识别修复任务和目标文件。
def repair_goal_targets(value: Any) -> tuple[str, ...]:
    role = _text(getattr(value, "role", ""))
    name = _text(getattr(value, "agent_name", ""))
    goal = _text(getattr(value, "goal", ""))
    if not _repair_like(role, name, goal):
        return ()
    return _target_refs(goal)


# LLM: repair_goal_targets_overlap lets a broad repair owner cover a later narrower same-file repair.
# 函数用途: 判断两个修复目标集合是否有明确交集；用于复用已有修复负责人。
def repair_goal_targets_overlap(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    return bool(set(left) & set(right))


# LLM: _repair_like requires explicit repair wording or repair-ish role/name before target extraction matters.
# 函数用途: 防止普通创建任务因为提到了某个文件名就被误合并成修复任务。
def _repair_like(role: str, name: str, goal: str) -> bool:
    text = " ".join([role, name, goal]).casefold()
    return any(token.casefold() in text for token in _REPAIR_TOKENS)


# LLM: _target_refs extracts concrete files while avoiding generic directories.
# 函数用途: 优先提取绝对文件路径，再补相对文件名；去重排序后作为修复身份。
def _target_refs(goal: str) -> tuple[str, ...]:
    refs: list[str] = []
    for raw in _ABS_PATH_RE.findall(goal):
        path = _normalize_path(raw)
        if _looks_like_file(path):
            refs.append(path)
    for raw in _FILE_TOKEN_RE.findall(goal):
        refs.append(_normalize_path(raw))
    return tuple(sorted(dict.fromkeys(refs)))


# LLM: _looks_like_file limits identity refs to file-shaped paths, not workspace directories.
# 函数用途: 只把带扩展名的目标纳入修复身份，避免整个项目目录让不同修复任务互相误复用。
def _looks_like_file(value: str) -> bool:
    return bool(Path(value).suffix)


# LLM: _normalize_path is intentionally lexical so nonexistent repair targets are still comparable.
# 函数用途: 清理标点、尾斜杠和重复空白，不访问文件系统。
def _normalize_path(value: object) -> str:
    text = _text(value).rstrip(".,;:，。；：、")
    if text != "/":
        text = text.rstrip("/")
    return text


# LLM: _text keeps matcher helpers tolerant of missing mock/dataclass fields.
# 函数用途: 将字段转为去空格字符串，空值返回空串。
def _text(value: object) -> str:
    return str(value or "").strip()


__all__ = ["repair_goal_targets", "repair_goal_targets_overlap"]
