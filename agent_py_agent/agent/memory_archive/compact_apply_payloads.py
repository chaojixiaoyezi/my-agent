# LLM: Compact apply payload helpers keep metadata assembly separate from apply orchestration.
# 模块用途: 生成 restore refs、apply bundle 和 ledger 行，让 compact_apply.py 保持薄编排。

from __future__ import annotations

from pathlib import Path
from typing import Any

from .compact_apply_work_state import restore_refs_summary, work_state_summary
from .compact_context_bundle_refs import (
    compact_context_bundle_summary,
    main_context_bundle_source_refs,
)
from .compact_tool_output_refs import tool_output_source_refs
from .schema import (
    RuntimeMemorySchemaOptions,
    runtime_memory_reserved_fields,
    runtime_memory_schema_payload,
)

COMPACT_APPLY_BUNDLE_SCHEMA = RuntimeMemorySchemaOptions("compact_apply_bundle")
COMPACT_APPLY_LEDGER_SCHEMA = RuntimeMemorySchemaOptions("compact_apply_ledger")
COMPACT_RESTORE_REFS_SCHEMA = RuntimeMemorySchemaOptions("compact_apply_restore_refs")


# LLM: compact_apply_refs turns apply paths into the stable refs table used by every payload.
# 函数用途: 把 Path bundle 转成 JSON 可写的引用字段，供 metadata、bundle、ledger 复用。
def compact_apply_refs(paths: dict[str, Path]) -> dict[str, str]:
    return {
        "compact_context": str(paths["context_md"]),
        "compaction_state": str(paths["compaction_state_json"]),
        "handoff_summary": str(paths["handoff_summary_md"]),
        "metadata": str(paths["metadata_json"]),
        "apply_bundle": str(paths["apply_bundle_json"]),
        "restore_refs": str(paths["restore_refs_json"]),
        "work_state_snapshot": str(paths["work_state_snapshot_json"]),
        "post_compact_self_check": str(paths["self_check_json"]),
        "self_check_failure": str(paths["failed_self_check_json"]),
        "apply_ledger": str(paths["ledger_jsonl"]),
        "global_apply_ledger": str(paths.get("global_ledger_jsonl", paths["ledger_jsonl"])),
    }


# LLM: restore_refs_payload records every original source path needed to verify or rebuild a compact context.
# 函数用途: 生成 compact apply 的恢复引用包，只保存路径和摘要，不复制或修改原始事实源。
def restore_refs_payload(payload: dict[str, Any], plan: dict[str, Any], paths: dict[str, Path], now: str) -> dict[str, Any]:
    result = {
        "version": COMPACT_RESTORE_REFS_SCHEMA.version,
        "schema": runtime_memory_schema_payload(COMPACT_RESTORE_REFS_SCHEMA),
        "event_type": "compact_apply_restore_refs",
        "apply_id": payload["apply_id"],
        "plan_id": payload["plan_id"],
        "workspace_root": plan["workspace_root"],
        "scope": plan["scope"],
        "created_at": now,
        "lineage": dict(payload.get("lineage", {})),
        "source_refs": _source_refs(plan),
        "apply_refs": compact_apply_refs(paths),
        "content_preserved": True,
        "reserved": runtime_memory_reserved_fields(COMPACT_RESTORE_REFS_SCHEMA),
    }
    result["source_refs"]["context_bundles"] = main_context_bundle_source_refs(payload.get("main_context_bundle", {}))
    return result


# LLM: apply_bundle_payload is the resume entrypoint for non-destructive compact apply.
# 函数用途: 生成 apply bundle，串起 context、metadata、自检、restore refs 和人工恢复步骤。
def apply_bundle_payload(
    payload: dict[str, Any], restore_refs: dict[str, Any], work_state: dict[str, Any], paths: dict[str, Path]
) -> dict[str, Any]:
    return {
        "version": COMPACT_APPLY_BUNDLE_SCHEMA.version,
        "schema": runtime_memory_schema_payload(COMPACT_APPLY_BUNDLE_SCHEMA),
        "event_id": payload["event_id"],
        "apply_id": payload["apply_id"],
        "plan_id": payload["plan_id"],
        "event_type": "compact_apply_bundle",
        "compact_status": payload["compact_status"],
        "workspace_root": payload["workspace_root"],
        "scope": payload["scope"],
        "refs": dict(payload.get("refs", compact_apply_refs(paths))),
        "lineage": dict(payload.get("lineage", {})),
        "main_context_bundle_match": dict(payload.get("main_context_bundle_match", {})),
        "compaction_state": _compaction_state_summary(payload.get("compaction_state", {})),
        "restore_refs_summary": restore_refs_summary(restore_refs),
        "work_state_summary": work_state_summary(work_state),
        "resume_focus": _resume_focus_summary(work_state),
        "main_context_bundle": compact_context_bundle_summary(payload.get("main_context_bundle", {})),
        "restore_steps": _restore_steps(),
        "content_preserved": True,
        "reserved": runtime_memory_reserved_fields(COMPACT_APPLY_BUNDLE_SCHEMA),
    }


# LLM: ledger_record controls ledger row width so JSONL does not duplicate full metadata.
# 函数用途: 提取 apply metadata 的关键字段，作为 append-only ledger 的单行记录。
def ledger_record(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": COMPACT_APPLY_LEDGER_SCHEMA.version,
        "schema": runtime_memory_schema_payload(COMPACT_APPLY_LEDGER_SCHEMA),
        "event_id": payload["event_id"],
        "apply_id": payload["apply_id"],
        "plan_id": payload["plan_id"],
        "event_type": payload["event_type"],
        "compact_status": payload["compact_status"],
        "workspace_root": payload["workspace_root"],
        "scope": payload["scope"],
        "refs": payload["refs"],
        "lineage": dict(payload.get("lineage", {})),
        "compaction_state": _compaction_state_summary(payload.get("compaction_state", {})),
        "main_context_bundle_match": dict(payload.get("main_context_bundle_match", {})),
        "created_at": payload["created_at"],
        "content_preserved": payload["content_preserved"],
        "restore_ready": bool(payload.get("restore_ready") and payload.get("ok", True)),
        "reserved": runtime_memory_reserved_fields(COMPACT_APPLY_LEDGER_SCHEMA),
    }


# LLM: _source_refs keeps restore groups explicit so resume/debug can jump back to each source family.
# 函数用途: 从 dry-run plan 提取 raw/hook archive、snapshot 和 token ledger 的原始文件引用。
def _source_refs(plan: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {
        "archive_files": _file_refs(plan["archive"].get("files", [])),
        "snapshot_files": _file_refs(plan["snapshots"].get("latest", [])),
        "token_ledgers": _file_refs(plan["tokens"].get("latest", [])),
        "tool_outputs": tool_output_source_refs(plan["workspace_root"], plan["scope"]),
    }


# LLM: _compaction_state_summary keeps bundle and ledger rows small but chain-aware.
# 函数用途: 提取 compact state 的核心 id、摘要路径和下一步，避免 JSONL 复制完整状态。
def _compaction_state_summary(value: Any) -> dict[str, Any]:
    state = value if isinstance(value, dict) else {}
    work = state.get("work", {}) if isinstance(state.get("work"), dict) else {}
    return {
        "compact_id": str(state.get("compact_id") or ""),
        "compact_index": int(state.get("compact_index", 0) or 0),
        "previous_compact_id": str(state.get("previous_compact_id") or ""),
        "handoff_summary_ref": str(state.get("handoff_summary_ref") or ""),
        "previous_handoff_summary_ref": str(state.get("previous_handoff_summary_ref") or ""),
        "goal": str(work.get("goal") or ""),
        "next_step": str(work.get("next_step") or ""),
    }


# LLM: _file_refs normalizes compact plan file summaries without reading file bodies.
# 函数用途: 将 plan 中的 file_path/size/created_at 摘要转成 restore refs 的稳定列表。
def _file_refs(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for item in items:
        refs.append({
            "path": str(item.get("path") or item.get("file_path") or ""),
            "size_bytes": int(item.get("size_bytes", 0) or 0),
            "created_at": str(item.get("created_at", "") or ""),
            "reserved": {},
        })
    return refs


# LLM: _restore_steps is a stable human/model checklist for safe non-destructive recovery.
# 函数用途: 返回使用 apply bundle 恢复上下文时必须遵守的核验顺序。
def _restore_steps() -> list[str]:
    return [
        "continue from resume_focus.next_action or work_state_summary first",
        "use compact_context, work_state_snapshot, restore_refs, or post_compact_self_check only when facts are missing, refs look broken, or verification is needed",
        "use restore_refs to verify raw/hook archives, snapshots, token ledgers, and task/run workspaces when rebuilding context",
        "trust restored answers only after source refs still exist and match the requested scope",
    ]


# LLM: _resume_focus_summary keeps apply bundles action-first for automatic continuation.
# 函数用途: 从 work_state 提取续接优先动作，避免 apply bundle 指导模型先重读 compact 文件。
def _resume_focus_summary(work_state: dict[str, Any]) -> dict[str, Any]:
    actions = _string_list(work_state.get("next_actions"))
    next_step = str(work_state.get("next_step") or "").strip()
    return {
        "next_action": actions[0] if actions else next_step,
        "next_actions": actions,
    }


# LLM: _string_list normalizes optional work-state arrays for compact payloads.
# 函数用途: 把 next_actions 等字段转成去空字符串列表，坏类型按空列表处理。
def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list | tuple):
        return []
    return [text for item in value if (text := str(item).strip())]


__all__ = ["apply_bundle_payload", "compact_apply_refs", "ledger_record", "restore_refs_payload"]
