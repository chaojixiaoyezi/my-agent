# LLM: Orchestration tool summaries keep parent agents refs-first after large tool outputs are archived.
# 模块用途: 将 dispatch/schedule/read_artifact 的大 JSON 输出压成可执行摘要，避免父级反复读取 artifact 正文。

from __future__ import annotations

import json
from typing import Any

from .tool_context_recovery_summary import strategy_preview
from .tool_context_repair_summary import repair_advice_action_lines

_ORCHESTRATION_TOOLS = {"create_subagents", "dispatch_subagents", "schedule_child_subagents", "subagent_board"}
_MAX_INLINE_JSON = 900
_MAX_INLINE_TEXT = 500


# LLM: orchestration_live_summary returns a compact live-prompt replacement for orchestration outputs.
# 函数用途: 调度类工具被外置后，保留 next_action、run refs 和建议工具调用；不展开 records 或产物正文。
def orchestration_live_summary(result, archive_record: dict[str, object]) -> str:
    tool = str(getattr(result, "tool", "") or "")
    payload = _json_object(str(getattr(result, "output", "") or ""))
    if tool in _ORCHESTRATION_TOOLS and payload is not None:
        return _render_orchestration_summary("orchestration_summary", tool, payload, archive_record)
    if tool == "read_artifact" and payload is not None:
        return _render_artifact_read_summary(payload, archive_record)
    return ""


# LLM: _render_artifact_read_summary keeps explicit artifact reads from causing second-order prompt growth.
# 函数用途: read_artifact 读到调度 artifact 时，只回传调度摘要；普通正文只给短 preview 和元数据。
def _render_artifact_read_summary(
    payload: dict[str, Any],
    archive_record: dict[str, object],
) -> str:
    inner_tool = str(payload.get("tool") or "")
    content = str(payload.get("content") or "")
    inner = _json_object(content)
    if inner_tool in _ORCHESTRATION_TOOLS and inner is not None:
        return _render_orchestration_summary("artifact_read_summary", inner_tool, inner, _source_artifact_record(payload))
    lines = [
        "[tool=read_artifact; status=ok]",
        "artifact_read_summary:",
        "- policy: artifact body was requested explicitly; live prompt keeps only a bounded preview.",
        f"- source_artifact_ref: {payload.get('artifact_ref', '')}",
        f"- source_artifact_tool: {inner_tool}",
        f"- source_call_id: {payload.get('call_id', '')}",
        f"- content_offset: {payload.get('content_offset', 0)}",
        f"- content_chars: {payload.get('content_chars', len(content))}",
        f"- truncated: {payload.get('truncated', False)}",
        f"- content_preview: {_clip(content)}",
        "- continue_read_policy: read source_artifact_ref with offset/mode if more evidence is needed; do not read a read_artifact wrapper path.",
    ]
    return "\n".join(lines)


# LLM: _render_orchestration_summary extracts high-signal fields and drops bulky record arrays.
# 函数用途: 给模型下一步足够的状态、建议和 refs，同时避免把完整调度报告或 child artifact 正文塞回 prompt。
def _render_orchestration_summary(
    heading: str,
    tool: str,
    payload: dict[str, Any],
    archive_record: dict[str, object],
) -> str:
    lines = [
        f"[tool={tool}; status={'ok' if archive_record.get('ok', True) else 'error'}]",
        f"{heading}:",
        "- policy: refs-first orchestration output; do not read artifact/file bodies unless a specific evidence ref requires it.",
    ]
    lines.extend(_direct_children_lines(payload.get("direct_children")))
    lines.extend(repair_advice_action_lines(payload.get("parent_acceptance_repair_advice")))
    lines.extend(_top_level_action_lines(payload))
    lines.extend(_result_refs_by_run_lines(payload.get("result_refs_by_run")))
    lines.extend(_ref_lines(payload))
    lines.extend(_summary_lines(payload))
    lines.extend(_archive_pointer_lines(archive_record, include_read_hint=False))
    return "\n".join(lines)


# LLM: _direct_children_lines exposes parent progress without dumping dispatch records.
# 函数用途: 从 direct_children 中提取父级最该看的状态和建议工具调用。
def _direct_children_lines(value: object) -> list[str]:
    if not isinstance(value, dict):
        return []
    lines = [
        f"- parent_run_id: {value.get('parent_run_id', '')}",
        f"- direct_children_total: {value.get('total', 0)}",
        f"- direct_children_status: {_json_inline(value.get('by_status', {}))}",
        f"- next_action: {value.get('next_action', '')}",
    ]
    for key in (
        "unfinished_run_ids",
        "recovery_run_ids",
        "rejected_acceptance_run_ids",
        "running_run_ids",
        "planning_run_ids",
    ):
        if value.get(key):
            lines.append(f"- {key}: {_json_inline(value.get(key))}")
    if value.get("quality_advice"):
        lines.append(f"- quality_advice: {_json_inline(value.get('quality_advice'))}")
    lines.extend(_direct_children_recovery_lines(value))
    lines.extend(_direct_children_repair_lines(value))
    lines.extend(_direct_children_suggested_tool_lines(value))
    return lines


# LLM: _direct_children_recovery_lines keeps recovery summaries visible while preserving function size.
# 函数用途: 渲染 recovery counts/batches/strategy preview，不展开完整 records 或产物正文。
def _direct_children_recovery_lines(value: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    if value.get("recovery_action_counts"):
        lines.append(f"- recovery_action_counts: {_json_inline(value.get('recovery_action_counts'))}")
    if value.get("recovery_batches"):
        lines.append(f"- recovery_batches: {_json_inline(value.get('recovery_batches'))}")
    if value.get("recovery_strategies"):
        lines.append(f"- recovery_strategy_preview: {_json_inline(strategy_preview(value.get('recovery_strategies')))}")
    return lines


# LLM: _direct_children_repair_lines renders deferred repair advice separately from recovery actions.
# 函数用途: 渲染 QA/artifact/parent-acceptance repair 建议和恢复优先级延期标记。
def _direct_children_repair_lines(value: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    if value.get("qa_repair_advice"):
        lines.append(f"- qa_repair_advice: {_json_inline(value.get('qa_repair_advice'))}")
    if value.get("artifact_integrity_repair_advice"):
        lines.append(
            f"- artifact_integrity_repair_advice: {_json_inline(value.get('artifact_integrity_repair_advice'))}"
        )
    if value.get("parent_acceptance_repair_advice"):
        lines.append(
            f"- parent_acceptance_repair_advice: {_json_inline(value.get('parent_acceptance_repair_advice'))}"
        )
    if value.get("repair_wave_deferred_by_recovery"):
        lines.append("- repair_wave_deferred_by_recovery: true")
    if value.get("artifact_integrity_repair_deferred_by_recovery"):
        lines.append("- artifact_integrity_repair_deferred_by_recovery: true")
    if value.get("parent_acceptance_repair_deferred_by_recovery"):
        lines.append("- parent_acceptance_repair_deferred_by_recovery: true")
    return lines


# LLM: _direct_children_suggested_tool_lines keeps copyable tool calls in one summary section.
# 函数用途: 渲染调度/恢复建议工具调用，避免父级翻完整 artifact 找下一步。
def _direct_children_suggested_tool_lines(value: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    if value.get("suggested_tool_call"):
        lines.append(f"- suggested_tool_call: {_json_inline(value.get('suggested_tool_call'))}")
    if value.get("suggested_recovery_child_tool_call"):
        lines.append(
            f"- suggested_recovery_child_tool_call: {_json_inline(value.get('suggested_recovery_child_tool_call'))}"
        )
    return lines


# LLM: _top_level_action_lines keeps schedule outputs useful even when they have no direct_children block.
# 函数用途: 提取 schedule_child_subagents 等顶层字段中的新建 run、警告和 QA 建议。
def _top_level_action_lines(payload: dict[str, Any]) -> list[str]:
    keys = (
        "blocked",
        "reason",
        "created",
        "ids",
        "allowed_tools",
        "subagent_workspace",
        "completion_status",
        "must_not_report_done",
        "blocking_run_ids",
        "parent_acceptance_repair_advice",
        "created_run_ids",
        "planned_count",
        "scheduling_warnings",
        "coordination_advice",
        "runner_selection_recovery",
        "quality_advice",
        "artifact_integrity_repair_advice",
        # LLM: keep root's status table after create/schedule/dispatch outputs are externalized.
        # 函数用途: 大调度 JSON 外置后仍保留 current_turn_run_state，避免下一轮重复 create 或空 dispatch。
        "current_turn_run_state",
        "next_action",
    )
    return [f"- {key}: {_json_inline(payload.get(key))}" for key in keys if payload.get(key)]


# LLM: _result_refs_by_run_lines makes final handoff refs visible even when the flat ref list is clipped.
# 函数用途: 每个直接子代理单独一行展示主产物路径和状态，父级汇总时不用猜文件名或翻诊断文件。
def _result_refs_by_run_lines(value: object) -> list[str]:
    if not isinstance(value, list) or not value:
        return []
    lines = ["- result_refs_by_run:"]
    for item in value[:8]:
        if not isinstance(item, dict):
            continue
        run_id = item.get("run_id", "")
        status = f"{item.get('status', '')}/{item.get('verification_status', '')}"
        artifacts = _json_inline(item.get("primary_artifact_refs") or [])
        artifact_summaries = item.get("primary_artifact_summaries") or []
        output_json = item.get("output_json", "")
        summary = _clip(item.get("summary", ""), limit=220)
        lines.append(f"  - run_id={run_id} status={status} primary_artifact_refs={artifacts}")
        if artifact_summaries:
            lines.append(f"    artifact_summaries={_json_inline(artifact_summaries)}")
        if output_json:
            lines.append(f"    output_json={output_json}")
        if summary:
            lines.append(f"    summary={summary}")
    lines.append("- result_ref_policy: read primary_artifact_refs or output_json from result_refs_by_run; do not guess child filenames.")
    return lines


# LLM: _summary_lines includes count fields and deliberately reports only record_count.
# 函数用途: 保留调度统计，避免完整 records 列表撑大上下文。
def _summary_lines(payload: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    if payload.get("summary"):
        lines.append(f"- summary: {_json_inline(payload.get('summary'))}")
    bulky = payload.get("records")
    if isinstance(bulky, list):
        lines.append(f"- record_count: {len(bulky)}")
    if payload.get("dispatch_json"):
        lines.append(f"- dispatch_json: {payload.get('dispatch_json')}")
    if payload.get("dispatch_md"):
        lines.append(f"- dispatch_md: {payload.get('dispatch_md')}")
    return lines


# LLM: _ref_lines keeps completed child outputs visible after bulky board/dispatch payloads are archived.
# 函数用途: 从调度 payload 顶层和 items[] 中提取 artifact/evidence refs，告诉模型直接读 refs，不要猜路径。
def _ref_lines(payload: dict[str, Any]) -> list[str]:
    artifact_refs = _refs_from_payload(payload, "deliverable_artifact_refs", item_key="artifact_refs")
    evidence_refs = _refs_from_payload(payload, "deliverable_evidence_refs", item_key="evidence_refs")
    lines: list[str] = []
    if artifact_refs:
        lines.append(f"- deliverable_artifact_refs: {_json_inline(artifact_refs)}")
        lines.append("- refs_policy: use deliverable_artifact_refs/read_artifact first; do not guess task_dir child paths.")
    if evidence_refs:
        lines.append(f"- deliverable_evidence_refs: {_json_inline(evidence_refs)}")
    return lines


# LLM: _refs_from_payload handles both explicit top-level refs and older item-level board rows.
# 函数用途: 向后兼容旧 payload；如果没有顶层 refs，就从 items[] 里收集对应字段。
def _refs_from_payload(payload: dict[str, Any], top_key: str, *, item_key: str, limit: int = 12) -> list[str]:
    refs = _string_refs(payload.get(top_key), limit=limit)
    if refs:
        return refs
    items = payload.get("items")
    if not isinstance(items, list):
        return []
    collected: list[str] = []
    seen: set[str] = set()
    for item in items:
        if _collect_item_refs((collected, seen), item, item_key, limit):
            return collected
    return collected


# LLM: _collect_item_refs is the item-level fallback for older board payloads.
# 函数用途: 从单条 items[] 里追加唯一 ref；不是 dict 时直接跳过。
def _collect_item_refs(collection: tuple[list[str], set[str]], item: object, item_key: str, limit: int) -> bool:
    if not isinstance(item, dict):
        return False
    target, seen = collection
    for ref in _string_refs(item.get(item_key), limit=limit):
        if ref in seen:
            continue
        seen.add(ref)
        target.append(ref)
        if len(target) >= limit:
            return True
    return False


# LLM: _string_refs bounds refs before they enter the live prompt.
# 函数用途: 清洗 refs 列表，只保留非空字符串并做去重和数量限制。
def _string_refs(value: object, *, limit: int) -> list[str]:
    if not isinstance(value, list | tuple | set):
        return []
    refs: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        refs.append(text)
        if len(refs) >= limit:
            break
    return refs


# LLM: _archive_pointer_lines provides recovery pointers without encouraging immediate body rereads.
# 函数用途: 输出 hash/path/size；调度摘要足够时不放 read_artifact_hint。
def _archive_pointer_lines(archive_record: dict[str, object], *, include_read_hint: bool) -> list[str]:
    lines = [
        f"- output_path: {archive_record.get('output_path', '')}",
        f"- output_artifact_ref: {archive_record.get('artifact_ref', '')}",
        f"- output_call_id: {archive_record.get('call_id') or archive_record.get('id', '')}",
        f"- output_scoped_call_id: {archive_record.get('scoped_call_id', '')}",
        "- artifact_ref_policy: prefer output_scoped_call_id for read_artifact; avoid copying long paths or hashes.",
        f"- output_hash: {archive_record.get('output_hash', '')}",
        f"- output_size_bytes: {archive_record.get('output_size_bytes', 0)}",
    ]
    if include_read_hint:
        lines.append("- read_artifact_hint: read only a narrow evidence slice when the compact summary is insufficient.")
    return lines


# LLM: _source_artifact_record maps read_artifact metadata back to the original artifact, not the wrapper output.
# 函数用途: 生成摘要指针时隐藏 read_artifact 自己的二次外置路径，只保留原始 artifact 引用。
def _source_artifact_record(payload: dict[str, Any]) -> dict[str, object]:
    return {
        "output_path": "",
        "artifact_ref": str(payload.get("artifact_ref") or ""),
        "call_id": str(payload.get("call_id") or ""),
        "scoped_call_id": str(payload.get("scoped_call_id") or ""),
        "output_hash": str(payload.get("sha256") or ""),
        "output_size_bytes": int(payload.get("size_bytes") or payload.get("content_chars") or 0),
        "ok": bool(payload.get("ok", True)),
    }


# LLM: _json_object parses only dict JSON payloads for defensive summarization.
# 函数用途: 工具输出不是 JSON 对象时返回 None，让调用方走普通摘要路径。
def _json_object(text: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


# LLM: _json_inline bounds nested advisory JSON so suggested tool calls stay copyable but small.
# 函数用途: 将列表/字典压成单行 JSON，并在超长时带 hash 预览。
def _json_inline(value: object, *, limit: int = _MAX_INLINE_JSON) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except TypeError:
        text = repr(value)
    return _clip(text, limit=limit)


# LLM: _clip is a tiny prompt-safety primitive for all summary fields.
# 函数用途: 防止异常长字段通过摘要重新进入 live prompt。
def _clip(value: object, *, limit: int = _MAX_INLINE_TEXT) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[:limit].rstrip() + f"...[truncated {len(text) - limit} chars]"
