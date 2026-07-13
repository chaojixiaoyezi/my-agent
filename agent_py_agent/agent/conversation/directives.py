from __future__ import annotations

"""Conversation-scoped user directives shared by every chat adapter."""

import re
from dataclasses import dataclass

_VERBOSE_COMMAND = re.compile(r"^/(?:verbose|v)(?:\s+(\S+))?\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class VerboseDirective:
    matched: bool
    requested_level: str = ""
    valid: bool = True


def parse_verbose_directive(prompt: str) -> VerboseDirective:
    match = _VERBOSE_COMMAND.fullmatch(str(prompt or "").strip())
    if match is None:
        return VerboseDirective(False)
    raw = str(match.group(1) or "").strip().lower()
    return VerboseDirective(
        True,
        requested_level=raw,
        valid=not raw or raw in {"off", "on", "full"},
    )


def verbose_user_message(current_level: str, directive: VerboseDirective) -> str:
    if not directive.valid:
        return "用法：/verbose off、/verbose on 或 /verbose full。"
    level = directive.requested_level or current_level
    if not directive.requested_level:
        labels = {"off": "关闭", "on": "开启", "full": "完整"}
        return f"当前详细过程模式：{labels.get(level, '关闭')}。"
    if level == "off":
        return "详细过程已关闭。"
    if level == "on":
        return "详细过程已开启：执行任务时会发送工具步骤摘要。"
    return "完整过程已开启：除工具步骤摘要外，还会发送经过脱敏和长度限制的工具结果。"


__all__ = ["VerboseDirective", "parse_verbose_directive", "verbose_user_message"]
