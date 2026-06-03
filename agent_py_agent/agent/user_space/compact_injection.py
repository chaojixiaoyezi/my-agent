
from __future__ import annotations

from pathlib import Path
from typing import Any

from ..common.json_io import read_json_object_report


def render_compact_injection(package_dir: str | Path) -> str:
    root = Path(package_dir)
    context = _read_text(root / "compact_context.md")
    packet_report = read_json_object_report(root / "continue_packet.json", context="compact_injection.continue_packet")
    packet = packet_report.payload
    sections = ["【压缩前工作摘要 compact_context】", context.strip() or "无摘要。"]
    if packet_report.load_error:
        sections.extend(_load_error_sections(packet_report.load_error))
    sections.extend(_continue_packet_sections(packet))
    return "\n".join(sections).strip() + "\n"


def _load_error_sections(load_error: dict[str, object]) -> list[str]:
    return [
        "",
        "【续接包读取错误】",
        "- continue_packet 读取失败；不要把下面的空续接项理解成任务没有下一步。",
        f"- context: {_text(load_error.get('context'))}",
        f"- path: {_text(load_error.get('path'))}",
        f"- category: {_text(load_error.get('category'))}",
        f"- message: {_text(load_error.get('message'))}",
    ]


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


def _list_text(value: object) -> str:
    if isinstance(value, list):
        return "；".join(str(item) for item in value if str(item).strip()) or "无"
    return _text(value)


def _text(value: object) -> str:
    text = str(value or "").strip()
    return text or "无"


__all__ = ["render_compact_injection"]
