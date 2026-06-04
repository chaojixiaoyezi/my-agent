
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ...common.value_parsing import sequence_strings

CHAR_WINDOW_RE = re.compile(r"\[char-window offset=(\d+) chars=(\d+) total_chars=(\d+)\]")
CAPTURED_ARTIFACT_REF_LIMIT = 8


def resume_focus_payload(work_state: dict[str, Any], next_actions: list[str]) -> dict[str, Any]:
    coverage_action = full_read_coverage_resume_action(work_state)
    actions = [coverage_action] if coverage_action else action_first_actions(next_actions, work_state)
    next_step = str(work_state.get("next_step") or "").strip()
    next_action = actions[0] if actions else next_step
    do_not_repeat = [
        "不要把 compact/恢复文件当成新任务从头阅读；优先按 next_action 推进。",
        "不要重复已经登记的读取、写入或派工；只有验证、修补或缺事实时才重读。",
    ]
    if coverage_action:
        do_not_repeat.append("完整阅读任务恢复后不要回到 offset=0，也不要用 search_text、run_command、grep 或 awk 代替连续 read_file 覆盖。")
    return {
        "next_action": next_action,
        "next_actions": actions,
        "captured_refs": captured_refs_payload(work_state),
        "do_not_repeat": do_not_repeat,
    }


def action_first_actions(next_actions: list[str], work_state: dict[str, Any]) -> list[str]:
    candidates = sequence_strings(next_actions) or sequence_strings(work_state.get("next_actions"))
    filtered = [item for item in candidates if not looks_like_reader_first_recovery_hint(item)]
    if filtered:
        return filtered
    next_step = str(work_state.get("next_step") or "").strip()
    if next_step and not looks_like_reader_first_recovery_hint(next_step):
        return [next_step]
    return []


def captured_refs_payload(work_state: dict[str, Any]) -> dict[str, Any]:
    artifact_refs = work_state.get("artifact_refs") if isinstance(work_state.get("artifact_refs"), list) else []
    captured = [artifact_ref_payload(item) for item in artifact_refs[-CAPTURED_ARTIFACT_REF_LIMIT:] if isinstance(item, dict)]
    return {
        "changed_files": sequence_strings(work_state.get("changed_files")),
        "read_files": sequence_strings(work_state.get("read_files")),
        "artifact_refs": captured,
        "artifact_ref_count": len(artifact_refs),
        "omitted_artifact_ref_count": max(0, len(artifact_refs) - len(captured)),
        "full_read_coverage": _full_read_coverage_payload(artifact_refs),
    }


def artifact_ref_payload(item: dict[str, Any]) -> dict[str, Any]:
    params = item.get("parameters") if isinstance(item.get("parameters"), dict) else {}
    artifact_path = str(item.get("path") or "")
    source_path = str(item.get("source_path") or item.get("source_input") or params.get("path") or artifact_path)
    return {
        "artifact_ref": str(artifact_path or item.get("scoped_call_id") or item.get("call_id") or source_path),
        "artifact_path": artifact_path,
        "source_path": source_path,
        "tool": str(item.get("tool") or ""),
        "kind": str(item.get("kind") or ""),
        "offset": _optional_int(params.get("offset")),
        "max_chars": _optional_int(params.get("max_chars")),
    }


def looks_like_reader_first_recovery_hint(value: str) -> bool:
    text = value.strip().lower()
    if not text:
        return False
    recovery_markers = ("memory-resume", "localstore", "compact_context", "work_state_snapshot", "restore_refs")
    reader_markers = ("先查看", "先读取", "read ", "inspect ", "查看", "读取")
    return any(marker in text for marker in recovery_markers) and any(marker in text for marker in reader_markers)


def full_read_coverage_resume_action(work_state: dict[str, Any]) -> str:
    if not _work_state_wants_full_read(work_state):
        return ""
    cursor = _best_char_cursor(work_state.get("artifact_refs"))
    if not cursor or cursor["covered_until"] >= cursor["total_chars"]:
        return ""
    source = cursor["source_path"]
    offset = cursor["covered_until"]
    total = cursor["total_chars"]
    progress_ref = _task_progress_ref(work_state)
    progress_clause = f" 长清单/逐项事实的完整进度账本在 {progress_ref}；最终汇总前要读取或核对它。" if progress_ref else ""
    return (
        f"继续完整阅读 {source}：先沉淀上一段已读出的关键事实，再调用 read_file(path=\"{source}\", offset={offset}, max_chars=50000)。"
        f" 已连续覆盖 {offset}/{total} 字符；不要回到 offset=0，不要用 search_text/run_command/grep/awk 替代完整阅读。"
        " 如果任务要求逐章、逐项、逐检查点汇总，继续维护 work/ 下的事实记录表或 task_progress，最终报告以事实记录表、task_progress 完整账本和源文件证据为准，不要只靠 compact 摘要回忆。"
        f"{progress_clause}"
    )


def _work_state_wants_full_read(work_state: dict[str, Any]) -> bool:
    texts = [
        str(work_state.get("goal") or ""),
        str(work_state.get("next_step") or ""),
        *sequence_strings(work_state.get("next_actions")),
    ]
    task_progress = work_state.get("task_progress")
    if isinstance(task_progress, dict):
        texts.extend([
            str(task_progress.get("summary") or ""),
            str(task_progress.get("next_action") or ""),
        ])
    joined = "\n".join(texts)
    return any(marker in joined for marker in ("完整读", "完整读取", "读完", "按顺序慢慢读", "连续 read_file"))


def _task_progress_ref(work_state: dict[str, Any]) -> str:
    progress = work_state.get("task_progress") if isinstance(work_state.get("task_progress"), dict) else {}
    return str(progress.get("ref") or "").strip()


def _best_char_cursor(value: object) -> dict[str, int | str]:
    refs = value if isinstance(value, list) else []
    ranges_by_source: dict[str, list[tuple[int, int, int]]] = {}
    for ref in refs:
        if not isinstance(ref, dict) or str(ref.get("tool") or "") != "read_file":
            continue
        source = _source_path(ref)
        window = _char_window_from_ref(ref)
        if not source or not window:
            continue
        ranges_by_source.setdefault(source, []).append(window)
    best: dict[str, int | str] = {}
    for source, ranges in ranges_by_source.items():
        total = max(total for _start, _end, total in ranges)
        covered = _covered_prefix_end([(start, end) for start, end, _total in ranges])
        if not best or int(best.get("covered_until") or 0) < covered:
            best = {"source_path": source, "covered_until": covered, "total_chars": total}
    return best


def _full_read_coverage_payload(value: object) -> dict[str, Any]:
    cursor = _best_char_cursor(value)
    if not cursor:
        return {}
    covered = int(cursor.get("covered_until") or 0)
    total = int(cursor.get("total_chars") or 0)
    return {
        "source_path": str(cursor.get("source_path") or ""),
        "covered_until": covered,
        "total_chars": total,
        "complete": bool(total and covered >= total),
    }


def _source_path(ref: dict[str, Any]) -> str:
    params = ref.get("parameters") if isinstance(ref.get("parameters"), dict) else {}
    return str(ref.get("source_path") or params.get("path") or "").strip()


def _optional_int(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _char_window_from_ref(ref: dict[str, Any]) -> tuple[int, int, int] | None:
    content = _artifact_content(ref.get("path") or ref.get("artifact_ref"))
    match = CHAR_WINDOW_RE.search(content)
    if not match:
        return None
    offset, chars, total = (int(item) for item in match.groups())
    return (offset, offset + chars, total)


def _artifact_content(value: object) -> str:
    text = str(value or "").strip()
    if not text or "://" in text:
        return ""
    try:
        payload = json.loads(Path(text).expanduser().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    for key in ("content", "output", "text", "result", "output_preview"):
        item = payload.get(key)
        if isinstance(item, str):
            return item
    return ""


def _covered_prefix_end(ranges: list[tuple[int, int]]) -> int:
    cursor = 0
    for start, end in sorted(ranges):
        if start > cursor:
            break
        cursor = max(cursor, end)
    return cursor


__all__ = [
    "action_first_actions",
    "captured_refs_payload",
    "CAPTURED_ARTIFACT_REF_LIMIT",
    "full_read_coverage_resume_action",
    "looks_like_reader_first_recovery_hint",
    "resume_focus_payload",
]
