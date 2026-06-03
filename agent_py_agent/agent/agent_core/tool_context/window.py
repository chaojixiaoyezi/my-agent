
from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

_DEFAULT_MAX_CHARS = 48_000
_RECENT_REF_LIMIT = 8


@dataclass(frozen=True)
class ToolContextWindowRequest:
    __test__: ClassVar[bool] = False

    tool_context: list
    archive_tool_calls: list
    max_chars: int = _DEFAULT_MAX_CHARS


@dataclass(frozen=True)
class ToolContextWindowResult:
    windowed: bool = False
    omitted_count: int = 0
    original_chars: int = 0
    preserved_count: int = 0


def window_tool_context_for_live_prompt(request: ToolContextWindowRequest) -> ToolContextWindowResult:
    entries = request.tool_context
    if not isinstance(entries, list) or not entries:
        return ToolContextWindowResult()
    max_chars = _positive_max_chars(request.max_chars)
    original_chars = _context_chars(entries)
    if original_chars <= max_chars:
        return ToolContextWindowResult(original_chars=original_chars)
    active_entries = _non_window_entries(entries)
    recent_entries = _recent_entries_within_budget(active_entries, max_chars)
    omitted_count = max(0, len(active_entries) - len(recent_entries))
    if omitted_count <= 0:
        return ToolContextWindowResult(original_chars=original_chars, preserved_count=len(recent_entries))
    entries[:] = [
        _window_summary(omitted_count, recent_entries, request.archive_tool_calls),
        *recent_entries,
    ]
    return ToolContextWindowResult(
        windowed=True,
        omitted_count=omitted_count,
        original_chars=original_chars,
        preserved_count=len(recent_entries),
    )


def window_tool_context_params(agent: object, params: object) -> None:
    result = window_tool_context_for_live_prompt(
        ToolContextWindowRequest(
            tool_context=getattr(params, "tool_context", []),
            archive_tool_calls=getattr(params, "archive_tool_calls", []),
        )
    )
    if result.windowed and _persistent_compact_enabled(agent, params):
        _record_tool_context_window_overflow(params, result)


def _persistent_compact_enabled(agent: object, params: object) -> bool:
    save = getattr(params, "save", None)
    if save is not None:
        return bool(save)
    return bool(getattr(getattr(agent, "config", None), "auto_save_memory", True))


def _record_tool_context_window_overflow(params: object, result: ToolContextWindowResult) -> None:
    state = getattr(params, "live_archive_state", None)
    if not isinstance(state, dict):
        return
    state["tool_context_window_overflow"] = {
        "omitted_count": result.omitted_count,
        "original_chars": result.original_chars,
        "preserved_count": result.preserved_count,
    }


def _positive_max_chars(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_CHARS
    return parsed if parsed > 0 else _DEFAULT_MAX_CHARS


def _context_chars(entries: list) -> int:
    return sum(len(str(item or "")) for item in entries)


def _non_window_entries(entries: list) -> list[str]:
    return [
        str(item or "")
        for item in entries
        if not str(item or "").startswith("[tool-context-window]")
    ]


def _recent_entries_within_budget(entries: list[str], max_chars: int) -> list[str]:
    budget = max(1_000, max_chars - 1_200)
    selected: list[str] = []
    used = 0
    for entry in reversed(entries):
        entry_size = len(entry)
        if selected and used + entry_size > budget:
            break
        selected.append(entry)
        used += entry_size
        if used >= budget:
            break
    return list(reversed(selected))


def _window_summary(omitted_count: int, recent_entries: list[str], archive_tool_calls: list) -> str:
    lines = [
        "[tool-context-window]",
        f"- omitted_old_tool_context_entries: {omitted_count}",
        f"- preserved_recent_tool_context_entries: {len(recent_entries)}",
        "- policy: older tool calls are archived; do not replay old writes or reads. "
        "Use current files/artifact refs only when more detail is needed.",
    ]
    refs = _recent_archive_refs(archive_tool_calls)
    if refs:
        lines.append(f"- recent_archive_refs: {refs}")
    return "\n".join(lines)


def _recent_archive_refs(records: list) -> list[str]:
    refs: list[str] = []
    for record in reversed(records or []):
        ref = _archive_ref(record)
        if ref and ref not in refs:
            refs.append(ref)
        if len(refs) >= _RECENT_REF_LIMIT:
            break
    return list(reversed(refs))


def _archive_ref(record: object) -> str:
    if not isinstance(record, dict):
        return ""
    for key in ("scoped_call_id", "artifact_ref", "id", "output_path"):
        value = str(record.get(key) or "").strip()
        if value:
            return value
    return ""
