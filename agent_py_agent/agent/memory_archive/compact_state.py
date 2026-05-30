# LLM: Compaction state is the durable handoff boundary for multi-round compact/resume.
# 模块用途: 生成压缩交接包的机器状态和模型可读摘要；事实以 refs 为准，摘要只辅助续接。

from __future__ import annotations

"""Machine state and readable handoff summary for compact apply.

The compact state is intentionally refs-first.  It gives future runtime
auto-compact a stable machine packet while keeping the Markdown summary as a
model-facing handoff note, not as the source of truth.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .compact_apply_work_state import restore_refs_summary
from .schema import (
    RuntimeMemorySchemaOptions,
    runtime_memory_reserved_fields,
    runtime_memory_schema_payload,
)

COMPACT_STATE_SCHEMA = RuntimeMemorySchemaOptions("compact_state")


@dataclass(frozen=True)
class CompactionStateRequest:
    """Inputs needed to build one compact handoff boundary."""

    metadata: dict[str, Any]
    restore_refs: dict[str, Any]
    work_state: dict[str, Any]
    paths: dict[str, Path]


def build_compaction_state(request: CompactionStateRequest) -> dict[str, Any]:
    """Return the machine-readable state carried across repeated compactions."""

    metadata = request.metadata
    lineage = metadata.get("lineage", {}) if isinstance(metadata.get("lineage"), dict) else {}
    previous = _previous_compaction_state(lineage)
    source_refs = request.restore_refs.get("source_refs", {})
    if not isinstance(source_refs, dict):
        source_refs = {}
    state = {
        "version": COMPACT_STATE_SCHEMA.version,
        "schema": runtime_memory_schema_payload(COMPACT_STATE_SCHEMA),
        "event_type": "compact_state",
        "compact_id": str(metadata.get("apply_id", "") or ""),
        "plan_id": str(metadata.get("plan_id", "") or ""),
        "compact_index": _positive_int(lineage.get("cycle_index")),
        "previous_compact_id": str(lineage.get("previous_apply_id", "") or ""),
        "previous_metadata_ref": str(lineage.get("previous_metadata_ref", "") or ""),
        "previous_handoff_summary_ref": str(previous.get("handoff_summary_ref", "") or ""),
        "handoff_summary_ref": str(request.paths["handoff_summary_md"]),
        "workspace_root": str(metadata.get("workspace_root", "") or ""),
        "scope": dict(metadata.get("scope", {}) if isinstance(metadata.get("scope"), dict) else {}),
        "source_refs": _source_refs_payload(source_refs, request.restore_refs),
        "preserved_tail_refs": _preserved_tail_refs(metadata, request.paths),
        "work": _work_payload(request.work_state),
        "artifact_refs": _artifact_refs(request.work_state),
        "continuation": _continuation_payload(request.work_state),
        "summary_is_authoritative": False,
        "facts_authority": "source_refs_and_work_state",
        "reserved": runtime_memory_reserved_fields(COMPACT_STATE_SCHEMA),
    }
    return state


def render_compaction_handoff_summary(state: dict[str, Any]) -> str:
    """Render a compact Markdown handoff for the next model turn."""

    work = state.get("work", {}) if isinstance(state.get("work"), dict) else {}
    source_refs = state.get("source_refs", {}) if isinstance(state.get("source_refs"), dict) else {}
    continuation = state.get("continuation", {}) if isinstance(state.get("continuation"), dict) else {}
    previous_id = str(state.get("previous_compact_id") or "")
    previous_ref = str(state.get("previous_handoff_summary_ref") or "")
    lines = [
        "# Compact Handoff Summary",
        "",
        "这是一份压缩后的交接记录，只帮助模型续接；事实以 source refs、work_state、artifact registry 和 agent tree 为准。",
        "",
        "## 当前任务",
        "",
        f"- 目标: {work.get('goal') or 'unknown'}",
        f"- 当前阶段: {work.get('phase') or 'unknown'}",
        f"- 下一步: {work.get('next_step') or 'unknown'}",
        f"- 进度账本: {_progress_line(work.get('task_progress'))}",
        "",
        "## 用户要求和验收",
        "",
        f"- 约束: {_inline_items(work.get('constraints'))}",
        f"- 验收: {_inline_items(work.get('acceptance'))}",
        f"- 最近测试: {_inline_items(work.get('latest_tests'))}",
        "",
        "## 关键引用",
        "",
        f"- artifact_refs: {len(state.get('artifact_refs', []))}",
        f"- archive_files: {source_refs.get('archive_files_count', 0)}",
        f"- snapshot_files: {source_refs.get('snapshot_files_count', 0)}",
        f"- token_ledgers: {source_refs.get('token_ledgers_count', 0)}",
        f"- tool_outputs: {len(source_refs.get('tool_outputs', []))}",
        "",
        "## 下一步动作",
        "",
        *_bullet_items(continuation.get("next_actions")),
        "",
    ]
    if previous_id or previous_ref:
        lines.extend([
            "## 上一轮压缩",
            "",
            f"- previous_compact_id: {previous_id or 'unknown'}",
            f"- previous_handoff_summary_ref: {previous_ref or 'unknown'}",
            "",
        ])
    return "\n".join(lines).rstrip() + "\n"


def _previous_compaction_state(lineage: dict[str, Any]) -> dict[str, Any]:
    ref = str(lineage.get("previous_metadata_ref") or "")
    if not ref:
        return {}
    try:
        payload = json.loads(Path(ref).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    state = payload.get("compaction_state", {})
    return state if isinstance(state, dict) else {}


def _source_refs_payload(source_refs: dict[str, Any], restore_refs: dict[str, Any]) -> dict[str, Any]:
    counts = restore_refs_summary(restore_refs)
    tool_outputs = source_refs.get("tool_outputs", []) if isinstance(source_refs.get("tool_outputs"), list) else []
    return {
        "archive_files_count": counts["archive_files"],
        "snapshot_files_count": counts["snapshot_files"],
        "token_ledgers_count": counts["token_ledgers"],
        "tool_outputs": [_tool_output_ref(item) for item in tool_outputs if isinstance(item, dict)],
    }


def _tool_output_ref(item: dict[str, Any]) -> dict[str, Any]:
    path = str(item.get("artifact_ref") or item.get("path") or "")
    return {
        "kind": str(item.get("kind") or "tool_output"),
        "artifact_ref": path,
        "path": path,
        "tool": str(item.get("tool", "") or ""),
        "call_id": str(item.get("call_id", "") or ""),
        "scoped_call_id": str(item.get("scoped_call_id", "") or ""),
        "sha256": str(item.get("sha256", "") or ""),
        "size_bytes": int(item.get("size_bytes", 0) or 0),
    }


def _preserved_tail_refs(metadata: dict[str, Any], paths: dict[str, Path]) -> dict[str, str]:
    refs = metadata.get("refs", {}) if isinstance(metadata.get("refs"), dict) else {}
    main_context = metadata.get("main_context_bundle", {})
    main_ref = main_context.get("ref", "") if isinstance(main_context, dict) else ""
    return {
        "compact_context": str(paths["context_md"]),
        "work_state_snapshot": str(paths["work_state_snapshot_json"]),
        "restore_refs": str(paths["restore_refs_json"]),
        "main_context_bundle": str(refs.get("main_context_bundle") or main_ref or ""),
    }


def _work_payload(work_state: dict[str, Any]) -> dict[str, Any]:
    next_step = str(work_state.get("next_step") or "")
    if _looks_like_reader_first_hint(next_step):
        next_step = ""
    return {
        "goal": str(work_state.get("goal") or ""),
        "phase": str(work_state.get("phase") or ""),
        "next_step": next_step,
        "next_actions": _action_first_actions(work_state),
        "acceptance": _items(work_state.get("acceptance")),
        "constraints": _items(work_state.get("constraints")),
        "latest_tests": _items(work_state.get("latest_tests"), key="items"),
        "task_progress": _task_progress_payload(work_state.get("task_progress")),
        "read_files": _string_list(work_state.get("read_files")),
        "changed_files": _string_list(work_state.get("changed_files")),
        "missing_fields": _string_list(work_state.get("missing_fields")),
    }


# LLM: _continuation_payload stores action-first resume instructions in compaction_state.
# 函数用途: 生成 compact 后续接字段，明确恢复文件只是备用证据。
def _continuation_payload(work_state: dict[str, Any]) -> dict[str, Any]:
    return {
        "next_actions": _action_first_actions(work_state),
        "missing_fields": _string_list(work_state.get("missing_fields")),
        "continue_prompt": "继续执行当前任务；优先按下一步推进。compact 文件只是备用证据，缺事实或要核验时再读取。",
    }


# LLM: _artifact_refs preserves structured archive refs for later resume and verification.
# 函数用途: 从 work_state 中提取字典型 artifact refs，丢弃坏条目。
def _artifact_refs(work_state: dict[str, Any]) -> list[dict[str, Any]]:
    value = work_state.get("artifact_refs")
    return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _task_progress_payload(value: Any) -> dict[str, Any]:
    payload = value if isinstance(value, dict) else {}
    return {
        "summary": str(payload.get("summary") or ""),
        "next_action": str(payload.get("next_action") or ""),
        "counts": dict(payload.get("counts", {}) if isinstance(payload.get("counts"), dict) else {}),
        "active_items": [dict(item) for item in payload.get("active_items", []) if isinstance(item, dict)]
        if isinstance(payload.get("active_items"), list)
        else [],
        "ref": str(payload.get("ref") or ""),
    }


def _progress_line(value: Any) -> str:
    progress = _task_progress_payload(value)
    summary = progress.get("summary") or ""
    next_action = progress.get("next_action") or ""
    if summary and next_action:
        return f"{summary}；下一步：{next_action}"
    return summary or next_action or "未记录"


# LLM: _items reads compact payload list fields without trusting arbitrary shapes.
# 函数用途: 从带 items 字段的字典里取字符串列表，坏类型返回空列表。
def _items(value: Any, *, key: str = "items") -> list[str]:
    payload = value if isinstance(value, dict) else {}
    return _string_list(payload.get(key))


# LLM: _inline_items renders short compact state lists into one human-readable line.
# 函数用途: 把列表字段拼成中文分号分隔文本，空值显示未记录。
def _inline_items(value: Any) -> str:
    items = value if isinstance(value, list) else _items(value)
    normalized = _string_list(items)
    return "；".join(normalized) if normalized else "未记录"


# LLM: _bullet_items renders fallback continuation bullets for handoff summaries.
# 函数用途: 把下一步动作转成 bullet，缺失时给出通用继续推进提示。
def _bullet_items(value: Any) -> list[str]:
    items = _string_list(value)
    return [f"- {item}" for item in items] if items else ["- 按当前任务目标继续推进。"]


# LLM: _action_first_actions keeps compaction_state next actions focused on task progress.
# 函数用途: 过滤旧式恢复文件读取提示，保证机器状态里的下一步不是“先翻恢复包”。
def _action_first_actions(work_state: dict[str, Any]) -> list[str]:
    return [item for item in _string_list(work_state.get("next_actions")) if not _looks_like_reader_first_hint(item)]


# LLM: _looks_like_reader_first_hint recognizes recovery-reader prose from older compact flows.
# 函数用途: 判断 next_step/next_actions 是否只是读恢复文件的提示，命中后从续接动作中移除。
def _looks_like_reader_first_hint(value: str) -> bool:
    text = value.strip().lower()
    recovery_markers = ("memory-resume", "localstore", "compact_context", "work_state_snapshot", "restore_refs")
    reader_markers = ("先查看", "先读取", "read ", "inspect ", "查看", "读取")
    return any(marker in text for marker in recovery_markers) and any(marker in text for marker in reader_markers)


# LLM: _string_list is the compact_state local list normalizer.
# 函数用途: 只保留列表或元组中的非空字符串表示。
def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list | tuple):
        return []
    return [text for item in value if (text := str(item).strip())]


# LLM: _positive_int keeps optional numeric compact counters safe.
# 函数用途: 解析正整数，缺失、坏值或非正数都按 0 处理。
def _positive_int(value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


__all__ = [
    "CompactionStateRequest",
    "build_compaction_state",
    "render_compaction_handoff_summary",
]
