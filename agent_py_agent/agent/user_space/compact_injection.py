# LLM: Compact injection rendering keeps resume prompts consistent across task, run, and agent scopes.
# 模块用途: 把 compact_context.md 和 continue_packet.json 渲染成模型可读续接提示；只提示，不执行工具。

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def render_compact_injection(package_dir: str | Path) -> str:
    root = Path(package_dir)
    context = _read_text(root / "compact_context.md")
    packet = _read_json_object(root / "continue_packet.json")
    sections = ["【压缩前工作摘要 compact_context】", context.strip() or "无摘要。"]
    sections.extend(_continue_packet_sections(packet))
    return "\n".join(sections).strip() + "\n"


def _continue_packet_sections(packet: dict[str, Any]) -> list[str]:
    return [
        "",
        "【下一步续接包 continue_packet】",
        f"- 下一步先做：{_text(packet.get('next_action'))}",
        f"- 已完成：{_list_text(packet.get('completed_items') or packet.get('completed_headings'))}",
        f"- 还没完成：{_list_text(packet.get('pending_work'))}",
        f"- 不要重复：{_list_text(packet.get('avoid_repeating'))}",
        f"- 目标产物：{_list_text(packet.get('target_outputs'))}",
        f"- 当前阻塞：{_list_text(packet.get('known_blockers'))}",
        f"- 用户中途补充要求：{_list_text(packet.get('user_updates'))}",
    ]


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _list_text(value: object) -> str:
    if isinstance(value, list):
        return "；".join(str(item) for item in value if str(item).strip()) or "无"
    return _text(value)


def _text(value: object) -> str:
    text = str(value or "").strip()
    return text or "无"


__all__ = ["render_compact_injection"]
