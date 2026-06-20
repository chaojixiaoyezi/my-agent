
from __future__ import annotations

"""main-agent auto continuation helpers for compact/resume."""

from dataclasses import dataclass
from typing import Any

_DELIVERY_COMPLETE_MARKER = "[MAIN_AGENT_DELIVERY_COMPLETE]"


@dataclass(frozen=True)
class CompactAutoContinuationDecision:
    should_continue: bool
    reason: str
    injection: str = ""
    user_prompt: str = ""


def compact_auto_continuation_decision(result: Any, *, depth: int = 0) -> CompactAutoContinuationDecision:
    packet = _continue_packet(result)
    if _result_delivery_complete(result):
        return CompactAutoContinuationDecision(False, "delivery_complete")
    if not _result_ready(result, packet):
        return CompactAutoContinuationDecision(False, "not_ready")
    return CompactAutoContinuationDecision(
        True,
        "ready",
        build_compact_auto_continue_injection(packet),
        compact_auto_continue_user_prompt(),
    )


def build_compact_auto_continue_injection(packet: dict[str, Any]) -> str:
    work_state = packet.get("work_state_snapshot", {}) if isinstance(packet.get("work_state_snapshot"), dict) else {}
    guard = packet.get("guard", {}) if isinstance(packet.get("guard"), dict) else {}
    resume_focus = _resume_focus(packet, work_state)
    sections = [
        "# Compact Auto Continuation",
        "Status: allowed_to_continue",
        f"Apply ID: {packet.get('apply_id', '')}",
        f"Continue mode: {packet.get('continue_mode', '')}",
        f"Guard status: {guard.get('status', '')}",
        "",
        _resume_focus_section(resume_focus),
        _captured_refs_section(resume_focus.get("captured_refs")),
        _task_progress_section(work_state.get("task_progress")),
        _tool_output_index_section(work_state.get("tool_progress"), packet.get("artifact_read_hints")),
        _runtime_handoff_section(work_state.get("runtime_handoff")),
        "## Goal",
        str(work_state.get("goal") or ""),
        "",
        "## Current Phase",
        str(work_state.get("current_phase") or ""),
        "",
        "## Next Step",
        str(resume_focus.get("next_action") or work_state.get("next_step") or "继续当前任务目标，从未完成部分推进。"),
        "",
        _items_section("## Acceptance", work_state.get("acceptance")),
        _items_section("## Constraints", work_state.get("constraints")),
        _tests_section(work_state.get("latest_tests")),
        _list_section("## Changed Files", work_state.get("changed_files")),
        "## Continuation Rules",
        "- Continue only from the Next Step above.",
        "- Do not redo completed work.",
        "- 不要先重读 compact 文件；只有下一步缺事实、需要校验或引用损坏时才读取恢复引用。",
        "- 如果已读材料已经覆盖任务要求，下一步必须优先写入交付物并调用 submit_for_acceptance；不要只返回进度总结或建议接管。",
        "- 如果原任务要求沿 next_path/清单/分片继续读取，只有看到 END、覆盖清单完成、验收目标全部满足等明确完成证据，才可以写最终交付；仅凭“已经读了若干文件”不能当作读完。",
        "- 如果上下文、工具轮数或预算接近上限，先把已有证据落成可验收产物，再提交验收；不要继续重复核对已读文件。",
        "- 如果可选备注缺失，继续从目标、已捕获引用和工作区事实推进；只有关键源引用完全无法定位时才报告阻塞。",
    ]
    return "\n".join(section for section in sections if section is not None).strip()


def compact_auto_continue_user_prompt() -> str:
    return (
        "继续当前任务的未完成部分。"
        "不要重做已完成内容；优先推进未完成部分，必要时才读取恢复引用。"
        "如果任务要求沿 next_path、清单或分片读到结束，必须看到明确结束证据后再写最终交付。"
        "如果已经读完或只差交付，请直接写入目标产物并提交验收，不要只汇报进度。"
    )


def mark_compact_auto_continued(result: Any, source_result: Any, *, depth: int) -> Any:
    result.memory_compact_auto_continued = True
    result.memory_compact_auto_continued_from_apply_id = getattr(source_result, "memory_compact_auto_apply_id", "")
    result.memory_compact_auto_continuation_depth = depth
    return result


def _result_ready(result: Any, packet: dict[str, Any]) -> bool:
    return bool(
        getattr(result, "memory_compact_auto_allowed_to_continue", False)
        and getattr(result, "memory_compact_auto_continue_ready", False)
        and getattr(result, "memory_compact_auto_apply_id", "")
        and packet.get("ready_to_continue")
    )


def _continue_packet(result: Any) -> dict[str, Any]:
    packet = getattr(result, "memory_compact_auto_continue_packet", None)
    return packet if isinstance(packet, dict) else {}


def _result_delivery_complete(result: Any) -> bool:
    text = str(getattr(result, "response", "") or getattr(result, "text", "") or "")
    return _DELIVERY_COMPLETE_MARKER in text


def _items_section(title: str, payload: Any) -> str:
    items = _items(payload.get("items")) if isinstance(payload, dict) else []
    return _list_section(title, items or ["<not recorded>"])


def _tests_section(payload: Any) -> str:
    tests = _items(payload.get("items")) if isinstance(payload, dict) else []
    status = str(payload.get("status") or "not_recorded") if isinstance(payload, dict) else "not_recorded"
    return "\n".join(["## Latest Tests", f"Status: {status}", *_bullet_lines(tests or ["<not recorded>"])])


def _resume_focus(packet: dict[str, Any], work_state: dict[str, Any]) -> dict[str, Any]:
    focus = packet.get("resume_focus")
    if isinstance(focus, dict):
        return dict(focus)
    next_actions = _action_first_items(_items(packet.get("next_actions")) or _items(work_state.get("next_actions")))
    next_step = str(work_state.get("next_step") or "").strip()
    return {
        "next_action": next_actions[0] if next_actions else next_step,
        "next_actions": next_actions,
        "captured_refs": {
            "changed_files": _items(work_state.get("changed_files")),
            "read_files": _items(work_state.get("read_files")),
            "artifact_refs": _artifact_ref_items(packet.get("artifact_read_hints")),
        },
        "do_not_repeat": [
            "不要把 compact/恢复文件当成新任务从头阅读；优先按 next_action 推进。",
            "不要重复已经登记的读取、写入或派工；只有验证、修补或缺事实时才重读。",
        ],
    }


def _resume_focus_section(focus: dict[str, Any]) -> str:
    next_action = str(focus.get("next_action") or "").strip() or "继续当前任务的未完成部分。"
    return "\n".join(
        [
            "## Resume Focus",
            f"- next_action: {next_action}",
            *_bullet_lines(_items(focus.get("next_actions"))),
            *_bullet_lines(_items(focus.get("do_not_repeat"))),
        ]
    )


def _captured_refs_section(payload: Any) -> str:
    refs = payload if isinstance(payload, dict) else {}
    lines = ["## Already Captured Refs"]
    coverage = refs.get("full_read_coverage") if isinstance(refs.get("full_read_coverage"), dict) else {}
    coverage_line = _full_read_coverage_line(coverage)
    if coverage_line:
        lines.append(coverage_line)
    lines.extend(_source_coverage_lines(refs, "incomplete_source_coverage"))
    lines.extend(_source_coverage_lines(refs, "source_coverage"))
    artifact_ref_count = refs.get("artifact_ref_count")
    omitted = refs.get("omitted_artifact_ref_count")
    if isinstance(artifact_ref_count, int) and artifact_ref_count > 0:
        lines.append(f"- artifact_refs_total: {artifact_ref_count}; omitted_from_prompt: {omitted if isinstance(omitted, int) else 0}")
    lines.extend(_prefixed_lines("changed_files", _items(refs.get("changed_files"))))
    lines.extend(_prefixed_lines("read_files", _items(refs.get("read_files"))))
    artifact_refs = refs.get("artifact_refs") if isinstance(refs.get("artifact_refs"), list) else []
    rendered_artifacts = [
        _render_artifact_ref(item) if isinstance(item, dict) else str(item).strip()
        for item in artifact_refs
        if (isinstance(item, dict) or str(item).strip())
    ]
    lines.extend(_prefixed_lines("artifact_refs", rendered_artifacts))
    if len(lines) == 1:
        lines.append("- <none>")
    return "\n".join(lines)


def _full_read_coverage_line(coverage: dict[str, Any]) -> str:
    if not coverage:
        return ""
    source = str(coverage.get("source_path") or "").strip()
    if not source:
        return ""
    complete = coverage.get("complete")
    if coverage.get("kind") == "line_window":
        covered = coverage.get("covered_until_line")
        total = coverage.get("total_lines")
        if isinstance(covered, int) and isinstance(total, int):
            return (
                f"- full_read_coverage: source_path={source} covered_until_line={covered} "
                f"total_lines={total} complete={complete}"
            )
        return ""
    covered = coverage.get("covered_until_offset", coverage.get("covered_until"))
    total = coverage.get("total_chars")
    if isinstance(covered, int) and isinstance(total, int):
        return (
            f"- full_read_coverage: source_path={source} covered_until={covered} "
            f"total_chars={total} complete={complete}"
        )
    return ""


def _source_coverage_lines(refs: dict[str, Any], key: str) -> list[str]:
    rows = refs.get(key) if isinstance(refs.get(key), list) else []
    lines = [
        f"- {key}: {line}"
        for item in rows[:12]
        if isinstance(item, dict) and (line := _source_coverage_line(item))
    ]
    omitted = refs.get(f"omitted_{key}_count")
    if isinstance(omitted, int) and omitted > 0:
        lines.append(f"- {key}_omitted: {omitted}")
    return lines


def _source_coverage_line(item: dict[str, Any]) -> str:
    source = str(item.get("source_path") or "").strip()
    if not source:
        return ""
    complete = item.get("complete")
    if item.get("kind") == "line_window":
        covered = item.get("covered_until_line", item.get("covered_until"))
        total = item.get("total_lines", item.get("total"))
        next_value = item.get("next_start_line")
        return f"source_path={source} covered_until_line={covered} total_lines={total} next_start_line={next_value} complete={complete}"
    covered = item.get("covered_until_offset", item.get("covered_until"))
    total = item.get("total_chars", item.get("total"))
    next_value = item.get("next_offset")
    return f"source_path={source} covered_until={covered} total_chars={total} next_offset={next_value} complete={complete}"


def _task_progress_section(payload: Any) -> str | None:
    progress = payload if isinstance(payload, dict) else {}
    if not progress:
        return None
    lines = ["## Task Progress Ledger"]
    ref = str(progress.get("ref") or "").strip()
    summary = str(progress.get("summary") or "").strip()
    next_action = str(progress.get("next_action") or "").strip()
    counts = progress.get("counts") if isinstance(progress.get("counts"), dict) else {}
    if ref:
        lines.append(f"- full_ledger_ref: {ref}")
        lines.append("- compact prompt只显示账本摘要；逐项事实、长清单和最终汇总前的核对，以 full_ledger_ref 里的完整 JSON 为准。")
    if summary:
        lines.append(f"- summary: {summary}")
    if next_action:
        lines.append(f"- progress_next_action: {next_action}")
    if counts:
        rendered_counts = " ".join(f"{key}={value}" for key, value in sorted(counts.items()))
        lines.append(f"- counts: {rendered_counts}")
    quality = progress.get("quality_hints") if isinstance(progress.get("quality_hints"), dict) else {}
    messages = _items(quality.get("messages"))[:3] if quality else []
    suggestions = _items(quality.get("next_suggestions"))[:3] if quality else []
    if messages:
        lines.append("- quality_hints:")
        lines.extend(f"  - {item}" for item in messages)
    if suggestions:
        lines.append("- next_suggestions:")
        lines.extend(f"  - {item}" for item in suggestions)
    active = _progress_rows(progress.get("active_items"), limit=8)
    recent = _progress_rows(progress.get("recent_done_items"), limit=16)
    if active:
        lines.append("- active_items:")
        lines.extend(f"  - {item}" for item in active)
    if recent:
        lines.append("- recent_done_items:")
        lines.extend(f"  - {item}" for item in recent)
    coverage = progress.get("coverage") if isinstance(progress.get("coverage"), dict) else {}
    coverage_counts = coverage.get("counts") if isinstance(coverage.get("counts"), dict) else {}
    if coverage_counts:
        rendered_counts = " ".join(f"{key}={value}" for key, value in sorted(coverage_counts.items()))
        lines.append(f"- coverage_counts: {rendered_counts}")
    return "\n".join(lines)


def _progress_rows(value: Any, *, limit: int) -> list[str]:
    rows: list[str] = []
    for item in value if isinstance(value, list | tuple) else []:
        if row := _progress_row(item):
            rows.append(row)
        if len(rows) >= limit:
            break
    return rows


def _progress_row(item: Any) -> str:
    if not isinstance(item, dict):
        return str(item or "").strip()
    parts = _progress_row_parts(item)
    return " ".join(parts) if parts else ""


def _progress_row_parts(item: dict[str, Any]) -> list[str]:
    parts = [
        f"{key}={text}"
        for key in ("id", "title", "status", "notes", "next")
        if (text := str(item.get(key) or "").strip())
    ]
    evidence = _items(item.get("evidence"))[:3]
    if evidence:
        parts.append("evidence=" + "; ".join(evidence))
    return parts


def _tool_output_index_section(tool_progress: Any, artifact_hints: Any) -> str | None:
    rows = _tool_output_rows(tool_progress)
    if not rows:
        rows = _tool_output_rows_from_hints(artifact_hints)
    if not rows:
        return None
    lines = [
        "## Exact Tool Output Index",
        "- 精确字段、编号、SECRET、checksum、引用和清单不能凭 compact 摘要填写；列出 artifact_ref 的条目可用 read_artifact 读回，未列 artifact_ref 的条目直接按 source_path 重新读取（不要去猜 ref）。",
    ]
    for row in rows[:40]:
        parts = []
        tool = str(row.get("tool") or "").strip()
        source = str(row.get("source_path") or "").strip()
        artifact_ref = str(row.get("artifact_ref") or row.get("scoped_call_id") or "").strip()
        externalized = bool(row.get("externalized"))
        size = row.get("size_bytes")
        if tool:
            parts.append(f"tool={tool}")
        if source:
            parts.append(f"source_path={source}")
        # Fix B(阶段4):只有确认外置成可读 blob 的条目才把 artifact_ref 当 read_artifact 入口喂
        # 模型;未外置的(compaction deferred、index path 空,read_artifact 读它必报
        # not_externalized)不喂 ref、只留 source_path,从源头免去模型对读不到的 ref 瞎试。
        if artifact_ref and externalized:
            parts.append(f"artifact_ref={artifact_ref}")
        if isinstance(size, int) and size > 0:
            parts.append(f"size_bytes={size}")
        if parts:
            lines.append("- " + " ".join(parts))
    return "\n".join(lines)


def _tool_output_rows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list | tuple):
        return []
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source_path") or item.get("path") or "").strip()
        artifact_ref = str(item.get("artifact_ref") or item.get("scoped_call_id") or item.get("call_id") or "").strip()
        key = (source, artifact_ref)
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "tool": str(item.get("tool") or "").strip(),
                "source_path": source,
                "artifact_ref": artifact_ref,
                "size_bytes": _positive_int(item.get("size_bytes")),
                "externalized": bool(item.get("externalized")),
            }
        )
    return rows


def _tool_output_rows_from_hints(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list | tuple):
        return []
    rows: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "tool": str(item.get("source_tool") or item.get("tool") or "").strip(),
                "source_path": str(item.get("source_path") or "").strip(),
                "artifact_ref": str(item.get("artifact_ref") or "").strip(),
                "size_bytes": _positive_int(item.get("size_bytes")),
                # 显式 read_hints 是系统给的读回建议(已外置导向),保留喂 ref 旧行为;
                # 万一含未外置的,Fix A 的 not_externalized 明确错误会兜底纠偏。
                "externalized": True,
            }
        )
    return rows


def _runtime_handoff_section(payload: Any) -> str | None:
    handoff = payload if isinstance(payload, dict) else {}
    tree = handoff.get("agent_tree") if isinstance(handoff.get("agent_tree"), dict) else {}
    active = tree.get("active_agents") if isinstance(tree.get("active_agents"), list) else []
    active = [row for row in active if isinstance(row, dict)]
    recent = tree.get("recent_agents") if isinstance(tree.get("recent_agents"), list) else []
    recent = [row for row in recent if isinstance(row, dict)]
    rows = active or recent
    if not rows:
        return None
    counts = tree.get("counts") if isinstance(tree.get("counts"), dict) else {}
    lines = [
        "## Existing Child Agents",
        f"- counts: {counts}" if counts else "- counts: <not recorded>",
        _child_agent_instruction(active),
    ]
    lines.extend(_child_agent_lines(rows))
    return "\n".join(lines)


def _child_agent_instruction(active: list[dict[str, Any]]) -> str:
    if active:
        return "- 已有子代理在当前任务树里；不要重复 create_subagents。先查看/等待/收集这些子代理结果，只有确实新增工作时才再派新的。"
    return "- 当前任务树已有子代理记录且没有活跃子代理；优先汇总 recent_agents 的结果，别被旧 artifact 带回重复等待。"


def _child_agent_lines(rows: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for row in rows[:8]:
        run_id = str(row.get("run_id") or "").strip()
        status = str(row.get("status") or "").strip()
        parent = str(row.get("parent_run_id") or "").strip()
        tool = str(row.get("current_tool") or "").strip()
        note = str(row.get("last_progress_summary") or "").strip()
        state_ref = str(row.get("state_ref") or "").strip()
        parts = [f"run_id={run_id}"]
        if status:
            parts.append(f"status={status}")
        if parent:
            parts.append(f"parent={parent}")
        if tool:
            parts.append(f"tool={tool}")
        if note:
            parts.append(f"note={note}")
        if state_ref:
            parts.append(f"state_ref={state_ref}")
        lines.append("- active_child_agent: " + " ".join(parts))
    return lines


def _artifact_ref_items(value: Any) -> list[str]:
    if not isinstance(value, list | tuple):
        return []
    refs: list[str] = []
    for item in value:
        if isinstance(item, dict):
            text = str(item.get("artifact_ref") or item.get("source_path") or "").strip()
        else:
            text = str(item).strip()
        if text:
            refs.append(text)
    return refs


def _render_artifact_ref(item: dict[str, Any]) -> str:
    ref = str(item.get("artifact_ref") or item.get("source_path") or "").strip()
    source = str(item.get("source_path") or "").strip()
    parts = [ref or source]
    if source and ref and source != ref:
        parts.append(f"source={source}")
    if isinstance(item.get("offset"), int):
        parts.append(f"offset={item['offset']}")
    if isinstance(item.get("max_chars"), int):
        parts.append(f"max_chars={item['max_chars']}")
    return " ".join(part for part in parts if part)


def _prefixed_lines(label: str, values: list[str]) -> list[str]:
    return [f"- {label}: {item}" for item in values if item]


def _action_first_items(items: list[str]) -> list[str]:
    return list(items)


def _list_section(title: str, values: Any) -> str:
    return "\n".join([title, *_bullet_lines(_items(values) or ["<none>"])])


def _bullet_lines(values: list[str]) -> list[str]:
    return [f"- {item}" for item in values if item]


def _items(value: Any) -> list[str]:
    if isinstance(value, list | tuple):
        return [text for item in value if (text := str(item).strip())]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _positive_int(value: Any) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return 0
    return result if result > 0 else 0


__all__ = [
    "CompactAutoContinuationDecision",
    "build_compact_auto_continue_injection",
    "compact_auto_continuation_decision",
    "compact_auto_continue_user_prompt",
    "mark_compact_auto_continued",
]
