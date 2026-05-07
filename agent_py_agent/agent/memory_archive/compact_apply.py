# LLM: Memory archive module; keep compact apply non-destructive until restore checks mature.
# 模块用途: 生成 memory compact 的非破坏性 apply 记录、自检报告和恢复上下文。

from __future__ import annotations

"""non-destructive memory compact apply records.

Human version:
This module turns a dry-run compact plan into durable apply artifacts. It does
not delete or rewrite raw archive files, snapshots, token ledgers, task files,
or artifacts. The first apply step only materializes a compact context and a
self-check report so later resume/apply stages have a trustworthy boundary.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .compact import MemoryCompactPlanOptions, build_memory_compact_plan
from .compact_apply_ids import (
    compact_apply_id,
    compact_apply_plan_id,
    compact_candidate_counts,
    compact_risk_level,
    compact_scope_hash,
)
from .compact_apply_io import append_jsonl as _append_jsonl
from .compact_apply_io import write_json as _write_json
from .compact_apply_io import write_text as _write_text
from .compact_apply_self_check import (
    COMPACT_SELF_CHECK_FAILURE_SCHEMA,
    COMPACT_SELF_CHECK_SCHEMA,
)
from .compact_apply_self_check import (
    build_self_check_failure_payload as _self_check_failure_payload,
)
from .compact_apply_self_check import (
    build_self_check_payload as _self_check_payload,
)
from .compact_apply_work_state import (
    WorkStateSnapshotRequest,
    build_work_state_snapshot,
    restore_refs_summary,
    work_state_summary,
)
from .schema import (
    RuntimeMemorySchemaOptions,
    runtime_memory_reserved_fields,
    runtime_memory_schema_payload,
)

COMPACT_APPLY_SCHEMA = RuntimeMemorySchemaOptions("compact_apply")
COMPACT_APPLY_BUNDLE_SCHEMA = RuntimeMemorySchemaOptions("compact_apply_bundle")
COMPACT_APPLY_LEDGER_SCHEMA = RuntimeMemorySchemaOptions("compact_apply_ledger")
COMPACT_RESTORE_REFS_SCHEMA = RuntimeMemorySchemaOptions("compact_apply_restore_refs")


# LLM: memory archive compact apply 的入口 bundle；新增 apply 策略字段时放这里，不拉长函数签名。
# 类用途: 保存 compact apply 的计划过滤条件和执行来源；关键副作用: 本身不写文件，传给 apply_memory_compact 后才会落盘。
@dataclass(frozen=True)
class MemoryCompactApplyOptions:
    """Bundle inputs for non-destructive compact apply."""

    # LLM: plan_options preserves the exact dry-run scope used to create apply artifacts.
    plan_options: MemoryCompactPlanOptions
    actor: str = "memory-compact"


# LLM: _ApplyMetadataBuildRequest 属于 compact apply 内部 bundle；防止 metadata helper 参数继续增长。
# 类用途: 汇总 metadata 生成所需的 plan、options、event 和 path 信息，便于后续扩展 reserved 字段。
@dataclass(frozen=True)
class _ApplyMetadataBuildRequest:
    plan: dict[str, Any]
    options: MemoryCompactApplyOptions
    event_id: str
    now: str
    paths: dict[str, Path]


# LLM: apply_memory_compact 是 memory compact 的显式 apply 边界；保持非破坏性和可审计输出。
# 函数用途: 根据 dry-run plan 写 compact context、metadata、ledger 和 self-check；不会删除或覆盖归档事实源。
def apply_memory_compact(root: str | Path, options: MemoryCompactApplyOptions) -> dict[str, Any]:
    workspace = Path(root)
    plan = build_memory_compact_plan(workspace, options.plan_options)
    now = _utc_now()
    plan_id = compact_apply_plan_id(plan)
    apply_id = _unique_apply_id(workspace, plan_id, now)
    paths = _apply_paths(workspace, apply_id)
    payload = _metadata_payload(_ApplyMetadataBuildRequest(plan, options, apply_id, now, paths))
    context = _context_markdown(payload, plan)
    _write_text(paths["context_md"], context)
    restore_refs = _restore_refs_payload(payload, plan, paths, now)
    _write_json(paths["restore_refs_json"], restore_refs)
    work_state = build_work_state_snapshot(
        WorkStateSnapshotRequest(plan, restore_refs, paths, now, payload["apply_id"], payload["plan_id"])
    )
    _write_json(paths["work_state_snapshot_json"], work_state)
    apply_bundle = _apply_bundle_payload(payload, restore_refs, work_state, paths)
    _write_json(paths["apply_bundle_json"], apply_bundle)
    self_check = _self_check_payload(plan, paths, now, work_state)
    payload["post_compact_self_check"] = self_check
    payload["restore_refs"] = restore_refs
    payload["work_state_snapshot"] = work_state
    payload["apply_bundle"] = apply_bundle
    if not self_check["ok"]:
        payload["ok"] = False
        payload["compact_status"] = "blocked_self_check_failed"
        payload["self_check_failure"] = _self_check_failure_payload(payload, self_check, _refs(paths), now)
        _write_json(paths["failed_self_check_json"], payload["self_check_failure"])
    _write_json(paths["self_check_json"], self_check)
    _write_json(paths["metadata_json"], payload)
    _append_jsonl(paths["ledger_jsonl"], _ledger_record(payload))
    return payload


# LLM: _apply_paths 统一 compact apply 产物路径；避免 CLI、测试和后续 resume 各自拼路径。
# 函数用途: 根据 workspace 和 event_id 计算 apply metadata、context、自检和 ledger 文件位置。
def _apply_paths(workspace: Path, event_id: str) -> dict[str, Path]:
    directory = workspace / "memory_archive" / "compact_applies"
    return {
        "directory": directory,
        "context_md": directory / f"{event_id}.md",
        "metadata_json": directory / f"{event_id}.json",
        "apply_bundle_json": directory / f"{event_id}.apply_bundle.json",
        "restore_refs_json": directory / f"{event_id}.restore_refs.json",
        "work_state_snapshot_json": directory / f"{event_id}.work_state_snapshot.json",
        "self_check_json": directory / f"{event_id}.self_check.json",
        "failed_self_check_json": directory / f"{event_id}.self_check_failed.json",
        "ledger_jsonl": directory / "ledger.jsonl",
    }


# LLM: _metadata_payload 定义 compact apply 的权威 JSON 形状；新增保留字段必须兼容旧 reader。
# 函数用途: 组装非破坏性 apply metadata，记录 plan 摘要、输出路径、状态和保留扩展字段。
def _metadata_payload(request: _ApplyMetadataBuildRequest) -> dict[str, Any]:
    plan = request.plan
    return {
        "version": COMPACT_APPLY_SCHEMA.version,
        "schema": runtime_memory_schema_payload(COMPACT_APPLY_SCHEMA),
        "ok": True,
        "mode": "apply",
        "dry_run": False,
        "event_id": request.event_id,
        "apply_id": request.event_id,
        "plan_id": compact_apply_plan_id(plan),
        "event_type": "memory_compact_apply",
        "compact_status": "applied_non_destructive",
        "workspace_root": plan["workspace_root"],
        "scope": plan["scope"],
        "source_plan": _source_plan(plan),
        "refs": _refs(request.paths),
        "actor": request.options.actor,
        "created_at": request.now,
        "content_preserved": True,
        "restore_ready": True,
        "reserved": runtime_memory_reserved_fields(COMPACT_APPLY_SCHEMA),
    }


# LLM: _source_plan 只保存 plan 摘要，避免 apply metadata 复制大列表和未来工具输出。
# 函数用途: 从 dry-run plan 提取 compact apply 需要审计的计数、风险和体积摘要。
def _source_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "archive_record_count": plan["archive"]["record_count"],
        "archive_file_count": plan["archive"]["file_count"],
        "snapshot_file_count": plan["snapshots"]["file_count"],
        "token_ledger_count": plan["tokens"]["ledger_count"],
        "estimated_compactable_bytes": plan["estimated_compactable_bytes"],
        "risks": list(plan["risks"]),
        "recommended_actions": list(plan["recommended_actions"]),
        "plan_id": compact_apply_plan_id(plan),
        "scope_hash": compact_scope_hash(plan),
        "candidate_counts": compact_candidate_counts(plan),
        "risk_level": compact_risk_level(plan),
    }


# LLM: _refs 将本轮 apply 的关键文件全部显式登记，方便 resume/doctor 后续接入。
# 函数用途: 把 Path bundle 转成 JSON 可写的引用字段。
def _refs(paths: dict[str, Path]) -> dict[str, str]:
    return {
        "compact_context": str(paths["context_md"]),
        "metadata": str(paths["metadata_json"]),
        "apply_bundle": str(paths["apply_bundle_json"]),
        "restore_refs": str(paths["restore_refs_json"]),
        "work_state_snapshot": str(paths["work_state_snapshot_json"]),
        "post_compact_self_check": str(paths["self_check_json"]),
        "self_check_failure": str(paths["failed_self_check_json"]),
        "apply_ledger": str(paths["ledger_jsonl"]),
    }


# LLM: _restore_refs_payload records every original source path needed to verify or rebuild a compact context.
# 函数用途: 生成 compact apply 的恢复引用包，只保存路径和摘要，不复制或修改原始事实源。
def _restore_refs_payload(
    payload: dict[str, Any], plan: dict[str, Any], paths: dict[str, Path], now: str
) -> dict[str, Any]:
    return {
        "version": COMPACT_RESTORE_REFS_SCHEMA.version,
        "schema": runtime_memory_schema_payload(COMPACT_RESTORE_REFS_SCHEMA),
        "event_type": "compact_apply_restore_refs",
        "apply_id": payload["apply_id"],
        "plan_id": payload["plan_id"],
        "workspace_root": plan["workspace_root"],
        "scope": plan["scope"],
        "created_at": now,
        "source_refs": _source_refs(plan),
        "apply_refs": _refs(paths),
        "content_preserved": True,
        "reserved": runtime_memory_reserved_fields(COMPACT_RESTORE_REFS_SCHEMA),
    }


# LLM: _apply_bundle_payload is the resume entrypoint for non-destructive compact apply.
# 函数用途: 生成 apply bundle，串起 context、metadata、自检、restore refs 和人工恢复步骤。
def _apply_bundle_payload(
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
        "refs": _refs(paths),
        "restore_refs_summary": restore_refs_summary(restore_refs),
        "work_state_summary": work_state_summary(work_state),
        "restore_steps": _restore_steps(),
        "content_preserved": True,
        "reserved": runtime_memory_reserved_fields(COMPACT_APPLY_BUNDLE_SCHEMA),
    }


# LLM: _source_refs keeps restore groups explicit so resume/debug can jump back to each source family.
# 函数用途: 从 dry-run plan 提取 raw/hook archive、snapshot 和 token ledger 的原始文件引用。
def _source_refs(plan: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {
        "archive_files": _file_refs(plan["archive"].get("files", [])),
        "snapshot_files": _file_refs(plan["snapshots"].get("latest", [])),
        "token_ledgers": _file_refs(plan["tokens"].get("latest", [])),
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
        "read post_compact_self_check and stop if ok is false",
        "read work_state_snapshot and compare goal, next action, refs, and tests before continuing",
        "read compact_context as the compact entrypoint",
        "use restore_refs to verify raw/hook archives, snapshots, token ledgers, and task/run workspaces",
        "trust restored answers only after source refs still exist and match the requested scope",
    ]


# LLM: _context_markdown 生成给人和模型读的 compact context；它是摘要入口，不是原始事实源替代品。
# 函数用途: 渲染 compact apply 后的恢复上下文，提示调用方先读自检再回到原始 archive/snapshot 核实。
def _context_markdown(payload: dict[str, Any], plan: dict[str, Any]) -> str:
    source = payload["source_plan"]
    return (
        "# Memory Compact Context\n\n"
        f"- event_id: {payload['event_id']}\n"
        f"- compact_status: {payload['compact_status']}\n"
        f"- workspace_root: {payload['workspace_root']}\n"
        f"- archive_records: {source['archive_record_count']}\n"
        f"- snapshots: {source['snapshot_file_count']}\n"
        f"- token_ledgers: {source['token_ledger_count']}\n"
        f"- estimated_compactable_bytes: {source['estimated_compactable_bytes']}\n"
        "- content_preserved: true\n\n"
        "## Scope\n\n"
        f"{json.dumps(plan['scope'], ensure_ascii=False, sort_keys=True)}\n\n"
        "## Risks\n\n"
        f"{_bullet_lines(source['risks'])}\n\n"
        "## Recovery Rule\n\n"
        "Use this context as the compact entrypoint, then verify against raw/hook archives, "
        "compression snapshots, token ledgers, and task/run workspaces before trusting a resumed answer.\n"
    )


# LLM: _ledger_record 控制 ledger 行宽，避免把整份 apply metadata 重复塞进 JSONL。
# 函数用途: 提取 apply metadata 的关键字段，作为 append-only ledger 的单行记录。
def _ledger_record(payload: dict[str, Any]) -> dict[str, Any]:
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
        "created_at": payload["created_at"],
        "content_preserved": payload["content_preserved"],
        "restore_ready": bool(payload.get("restore_ready") and payload.get("ok", True)),
        "reserved": runtime_memory_reserved_fields(COMPACT_APPLY_LEDGER_SCHEMA),
    }


# LLM: _bullet_lines 是 Markdown 小渲染 helper；保持空列表输出稳定，方便测试和人工阅读。
# 函数用途: 把字符串列表渲染为 Markdown bullet，没有内容时返回 none。
def _bullet_lines(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items) if items else "- none"


# LLM: _unique_apply_id prevents same-second manual apply attempts from overwriting artifacts.
# 函数用途: 如果同一 plan 同一秒重复 apply，追加数字后缀并保持所有产物使用同一个 apply_id。
def _unique_apply_id(workspace: Path, plan_id: str, now: str) -> str:
    base = compact_apply_id(plan_id, now)
    candidate = base
    suffix = 2
    while _apply_paths(workspace, candidate)["metadata_json"].exists():
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


# LLM: _utc_now 集中时间来源，测试需要稳定事件时可 monkeypatch 这一层。
# 函数用途: 返回 UTC ISO 时间字符串，供 event id、metadata 和 self-check 共用。
def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat()


__all__ = ["MemoryCompactApplyOptions", "apply_memory_compact"]
