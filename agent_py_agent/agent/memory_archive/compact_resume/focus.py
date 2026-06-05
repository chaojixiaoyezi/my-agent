
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ...common.value_parsing import sequence_strings

CHAR_WINDOW_RE = re.compile(r"\[char-window offset=(\d+) chars=(\d+) total_chars=(\d+)\]")
LINE_NUMBER_RE = re.compile(r"^(\d+):\s", re.MULTILINE)
NEXT_START_LINE_RE = re.compile(r"next_start_line=(\d+)")
TOTAL_LINES_RE = re.compile(r"total_lines=(\d+)")
CAPTURED_ARTIFACT_REF_LIMIT = 8


def resume_focus_payload(work_state: dict[str, Any], next_actions: list[str]) -> dict[str, Any]:
    coverage_action = full_read_coverage_resume_action(work_state)
    cursor_action = "" if coverage_action else read_cursor_resume_action(work_state)
    actions = (
        ([coverage_action] if coverage_action else [])
        or ([cursor_action] if cursor_action else [])
        or action_first_actions(next_actions, work_state)
    )
    next_step = str(work_state.get("next_step") or "").strip()
    next_action = actions[0] if actions else next_step
    do_not_repeat = [
        "不要把 compact/恢复文件当成新任务从头阅读；优先按 next_action 推进。",
        "不要重复已经登记的读取、写入或派工；只有验证、修补或缺事实时才重读。",
    ]
    if coverage_action:
        do_not_repeat.append("完整阅读任务恢复后不要回到 offset=0；search_text/run_command 可用于定位章节或锚点，但最终覆盖证明必须来自已读取的源片段和 coverage ledger。")
    elif cursor_action:
        do_not_repeat.append("恢复后不要回到已登记的 offset/start_line；如果仍需读取同一来源，按机器游标继续。")
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
    full_read_coverage = _read_coverage_payload(work_state.get("read_coverage")) or _full_read_coverage_payload(
        artifact_refs
    )
    return {
        "changed_files": sequence_strings(work_state.get("changed_files")),
        "read_files": sequence_strings(work_state.get("read_files")),
        "artifact_refs": captured,
        "artifact_ref_count": len(artifact_refs),
        "omitted_artifact_ref_count": max(0, len(artifact_refs) - len(captured)),
        "full_read_coverage": full_read_coverage,
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
        "ok": item.get("ok"),
        "status": str(item.get("status") or ""),
        "error_code": str(item.get("error_code") or ""),
        "offset": _optional_int(params.get("offset")),
        "max_chars": _optional_int(params.get("max_chars")),
        "start_line": _optional_int(params.get("start_line")),
        "end_line": _optional_int(params.get("end_line")),
    }


def looks_like_reader_first_recovery_hint(value: str) -> bool:
    text = value.strip().lower()
    if not text:
        return False
    recovery_markers = ("memory-resume", "localstore", "compact_context", "work_state_snapshot", "restore_refs")
    return any(marker in text for marker in recovery_markers)


def full_read_coverage_resume_action(work_state: dict[str, Any]) -> str:
    if not _work_state_wants_full_read(work_state):
        return ""
    cursor = _best_read_cursor_from_read_coverage(work_state.get("read_coverage"))
    if not cursor:
        cursor = _best_read_cursor_from_tool_progress(work_state.get("tool_progress"))
    if not cursor:
        cursor = _best_read_cursor(work_state.get("artifact_refs"))
    if not cursor:
        return ""
    total = int(cursor.get("total_chars") or 0)
    if total > 0 and int(cursor.get("covered_until") or 0) >= total:
        return ""
    source = cursor["source_path"]
    progress_ref = _task_progress_ref(work_state)
    progress_clause = f" 长清单/逐项事实的完整进度账本在 {progress_ref}；最终汇总前要读取或核对它。" if progress_ref else ""
    if cursor.get("kind") == "line_window":
        line = cursor["covered_until"] + 1
        read_call = f'read_file(path="{source}", start_line={line}, max_chars=50000)'
        progress_text = f"已连续覆盖 {cursor['covered_until']}/{cursor['total_chars']} 行"
        repeat_text = "不要回到 start_line=1"
    else:
        offset = cursor["covered_until"]
        read_call = f'read_file(path="{source}", offset={offset}, max_chars=50000)'
        progress_text = f"已连续覆盖 {offset}/{cursor['total_chars']} 字符"
        repeat_text = "不要回到 offset=0"
    return (
        f"继续完整阅读 {source}：先沉淀上一段已读出的关键事实，再调用 {read_call}。"
        f" {progress_text}；{repeat_text}。可以用 search_text/run_command 先定位章节或锚点，"
        "但每个需要进入最终结论的对象仍要有 read_file/read_artifact 源片段或 coverage ledger 证据。"
        " 如果任务要求逐章、逐项、逐检查点汇总，继续维护 work/ 下的事实记录表或 task_progress，最终报告以事实记录表、task_progress 完整账本和源文件证据为准，不要只靠 compact 摘要回忆。"
        f"{progress_clause}"
    )


def read_cursor_resume_action(work_state: dict[str, Any]) -> str:
    cursor = _best_read_cursor_from_read_coverage(work_state.get("read_coverage"))
    if not cursor:
        cursor = _best_read_cursor_from_tool_progress(work_state.get("tool_progress"))
    if not cursor:
        cursor = _best_read_cursor(work_state.get("artifact_refs"))
    if not cursor:
        return ""
    total = int(cursor.get("total_chars") or 0)
    covered = int(cursor.get("covered_until") or 0)
    if total and covered >= total:
        return ""
    source = str(cursor.get("source_path") or "")
    if not source:
        return ""
    if cursor.get("kind") == "line_window":
        line = covered + 1
        read_call = f'read_file(path="{source}", start_line={line}, max_chars=50000)'
        progress_text = f"已连续覆盖到第 {covered} 行" + (f"/{total}" if total else "")
    else:
        read_call = f'read_file(path="{source}", offset={covered}, max_chars=50000)'
        progress_text = f"已连续覆盖到 offset={covered}" + (f"/{total}" if total else "")
    return (
        f"根据本轮工具读取账本继续 {source}：{progress_text}；"
        f"如果任务还需要读取这个来源，下一次从 {read_call} 开始。"
        "不要按旧 next_action 回到已登记范围；先沉淀已读事实，再继续未覆盖部分。"
    )


def _work_state_wants_full_read(work_state: dict[str, Any]) -> bool:
    task_progress = work_state.get("task_progress")
    if isinstance(task_progress, dict):
        coverage = task_progress.get("coverage")
        if _coverage_requires_full_source_read(coverage):
            return True
    coverage = work_state.get("target_coverage_contract")
    return _coverage_requires_full_source_read(coverage)


def _coverage_requires_full_source_read(value: object) -> bool:
    coverage = value if isinstance(value, dict) else {}
    if str(coverage.get("coverage_requirement") or "").strip() == "full_source_read":
        return True
    targets = coverage.get("targets")
    if not isinstance(targets, list):
        targets = coverage.get("active_targets")
    if not isinstance(targets, list):
        targets = coverage.get("target_items")
    if not isinstance(targets, list):
        return False
    return any(
        isinstance(item, dict) and str(item.get("coverage_kind") or "").strip() == "full_source_read"
        for item in targets
    )


def _task_progress_ref(work_state: dict[str, Any]) -> str:
    progress = work_state.get("task_progress") if isinstance(work_state.get("task_progress"), dict) else {}
    return str(progress.get("ref") or "").strip()


def _best_char_cursor(value: object) -> dict[str, int | str]:
    refs = value if isinstance(value, list) else []
    ranges_by_source: dict[str, list[tuple[int, int, int]]] = {}
    for ref in refs:
        if not isinstance(ref, dict) or str(ref.get("tool") or "") != "read_file":
            continue
        if not _successful_read_ref(ref):
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


def _best_line_cursor(value: object) -> dict[str, int | str]:
    refs = value if isinstance(value, list) else []
    ranges_by_source: dict[str, list[tuple[int, int, int]]] = {}
    for ref in refs:
        if not isinstance(ref, dict) or str(ref.get("tool") or "") != "read_file":
            continue
        if not _successful_read_ref(ref):
            continue
        source = _source_path(ref)
        window = _line_window_from_ref(ref)
        if not source or not window:
            continue
        ranges_by_source.setdefault(source, []).append(window)
    best: dict[str, int | str] = {}
    for source, ranges in ranges_by_source.items():
        total = max(total for _start, _end, total in ranges)
        covered = _covered_line_prefix_end([(start, end) for start, end, _total in ranges])
        if not best or int(best.get("covered_until") or 0) < covered:
            best = {"kind": "line_window", "source_path": source, "covered_until": covered, "total_chars": total}
    return best


def _best_read_cursor(value: object) -> dict[str, int | str]:
    char_cursor = _best_char_cursor(value)
    if char_cursor and int(char_cursor.get("covered_until") or 0) < int(char_cursor.get("total_chars") or 0):
        char_cursor["kind"] = "char_window"
        return char_cursor
    line_cursor = _best_line_cursor(value)
    if line_cursor:
        return line_cursor
    if char_cursor:
        char_cursor["kind"] = "char_window"
    return char_cursor


def _best_read_cursor_from_tool_progress(value: object) -> dict[str, int | str]:
    items = value if isinstance(value, list) else []
    char_ranges: dict[str, list[tuple[int, int, int]]] = {}
    line_ranges: dict[str, list[tuple[int, int, int]]] = {}
    for item in items:
        if not isinstance(item, dict) or str(item.get("tool") or "") not in {"read_file", "read_artifact"}:
            continue
        if not _successful_read_ref(item):
            continue
        source = str(item.get("source_path") or "").strip()
        if not source:
            continue
        offset = _optional_int(item.get("offset"))
        next_offset = _optional_int(item.get("next_offset"))
        if offset is not None and next_offset is not None:
            char_ranges.setdefault(source, []).append((offset, next_offset, _optional_int(item.get("total_chars")) or 0))
        start_line = _optional_int(item.get("start_line"))
        end_line = _optional_int(item.get("end_line"))
        if start_line is not None and end_line is not None:
            line_ranges.setdefault(source, []).append((start_line, end_line, _optional_int(item.get("total_lines")) or 0))
    best: dict[str, int | str] = {}
    for source, ranges in char_ranges.items():
        covered = _covered_prefix_end([(start, end) for start, end, _total in ranges])
        total = max((total for _start, _end, total in ranges), default=0)
        if covered and (not best or int(best.get("covered_until") or 0) < covered):
            best = {"kind": "char_window", "source_path": source, "covered_until": covered, "total_chars": total}
    if best:
        return best
    for source, ranges in line_ranges.items():
        covered = _covered_line_prefix_end([(start, end) for start, end, _total in ranges])
        total = max((total for _start, _end, total in ranges), default=0)
        if covered and (not best or int(best.get("covered_until") or 0) < covered):
            best = {"kind": "line_window", "source_path": source, "covered_until": covered, "total_chars": total}
    return best


def _best_read_cursor_from_read_coverage(value: object) -> dict[str, int | str]:
    coverage = value if isinstance(value, dict) else {}
    primary = coverage.get("primary") if isinstance(coverage.get("primary"), dict) else {}
    if not primary:
        return {}
    source = str(primary.get("source_path") or "").strip()
    covered = _optional_int(primary.get("covered_until") or primary.get("covered_until_offset") or primary.get("covered_until_line"))
    total = _optional_int(primary.get("total") or primary.get("total_chars") or primary.get("total_lines")) or 0
    if not source or covered is None:
        return {}
    kind = str(primary.get("kind") or "char_window")
    return {
        "kind": "line_window" if kind == "line_window" else "char_window",
        "source_path": source,
        "covered_until": covered,
        "total_chars": total,
    }


def _read_coverage_payload(value: object) -> dict[str, Any]:
    coverage = value if isinstance(value, dict) else {}
    primary = coverage.get("primary") if isinstance(coverage.get("primary"), dict) else {}
    if not primary:
        return {}
    source = str(primary.get("source_path") or "").strip()
    covered = _optional_int(primary.get("covered_until") or primary.get("covered_until_offset") or primary.get("covered_until_line"))
    total = _optional_int(primary.get("total") or primary.get("total_chars") or primary.get("total_lines")) or 0
    if not source or covered is None:
        return {}
    payload = {
        "kind": "line_window" if primary.get("kind") == "line_window" else "char_window",
        "source_path": source,
        "covered_until": covered,
        "total_chars": total,
        "complete": bool(total and covered >= total),
    }
    if payload["kind"] == "line_window":
        payload["covered_until_line"] = covered
        payload["total_lines"] = total
    else:
        payload["covered_until_offset"] = covered
    return payload


def _full_read_coverage_payload(value: object) -> dict[str, Any]:
    cursor = _best_read_cursor(value)
    if not cursor:
        return {}
    covered = int(cursor.get("covered_until") or 0)
    total = int(cursor.get("total_chars") or 0)
    payload = {
        "source_path": str(cursor.get("source_path") or ""),
        "covered_until": covered,
        "total_chars": total,
        "complete": bool(total and covered >= total),
    }
    if cursor.get("kind") == "line_window":
        payload["kind"] = "line_window"
        payload["covered_until_line"] = covered
        payload["total_lines"] = total
    else:
        payload["kind"] = "char_window"
        payload["covered_until_offset"] = covered
    return payload


def _source_path(ref: dict[str, Any]) -> str:
    params = ref.get("parameters") if isinstance(ref.get("parameters"), dict) else {}
    return str(ref.get("source_path") or params.get("path") or "").strip()


def _successful_read_ref(ref: dict[str, Any]) -> bool:
    if ref.get("ok") is False:
        return False
    if str(ref.get("error_code") or "").strip():
        return False
    status = str(ref.get("status") or "").strip().lower()
    return status not in {"error", "failed", "failure", "deferred", "blocked"}


def _optional_int(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _char_window_from_ref(ref: dict[str, Any]) -> tuple[int, int, int] | None:
    content = _artifact_content(ref.get("path") or ref.get("artifact_ref"))
    match = CHAR_WINDOW_RE.search(content)
    if not match:
        params = ref.get("parameters") if isinstance(ref.get("parameters"), dict) else {}
        max_chars = _optional_int(params.get("max_chars"))
        if max_chars is None:
            return None
        offset = _optional_int(params.get("offset"))
        if offset is None and _optional_int(params.get("start_line")) is None and _optional_int(params.get("end_line")) is None:
            offset = 0
        if offset is None:
            return None
        return (offset, offset + max_chars, 0)
    offset, chars, total = (int(item) for item in match.groups())
    return (offset, offset + chars, total)


def _line_window_from_ref(ref: dict[str, Any]) -> tuple[int, int, int] | None:
    content = _artifact_content(ref.get("path") or ref.get("artifact_ref"))
    if not content:
        return None
    params = ref.get("parameters") if isinstance(ref.get("parameters"), dict) else {}
    line_numbers = [int(match.group(1)) for match in LINE_NUMBER_RE.finditer(content)]
    explicit_start = _optional_int(params.get("start_line")) or 0
    explicit_end = _optional_int(params.get("end_line")) or 0
    next_start = _regex_int(NEXT_START_LINE_RE, content)
    total = _regex_int(TOTAL_LINES_RE, content)
    if total <= 0 or not (line_numbers or explicit_start or explicit_end or next_start):
        return None
    start = line_numbers[0] if line_numbers else (explicit_start or 1)
    end = line_numbers[-1] if line_numbers else explicit_end
    if next_start:
        end = min(end or next_start - 1, next_start - 1)
    if explicit_end and not next_start:
        end = min(end or explicit_end, explicit_end)
    if end < start:
        return None
    return (start, end, total)


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


def _regex_int(pattern: re.Pattern[str], text: str) -> int:
    match = pattern.search(text or "")
    if not match:
        return 0
    try:
        return int(match.group(1))
    except ValueError:
        return 0


def _covered_prefix_end(ranges: list[tuple[int, int]]) -> int:
    cursor = 0
    for start, end in sorted(ranges):
        if start > cursor:
            break
        cursor = max(cursor, end)
    return cursor


def _covered_line_prefix_end(ranges: list[tuple[int, int]]) -> int:
    cursor = 0
    for start, end in sorted(ranges):
        if start > cursor + 1:
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
