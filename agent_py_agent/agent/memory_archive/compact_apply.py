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
from .compact_apply_lineage import CompactApplyLineageRequest, build_compact_apply_lineage
from .compact_apply_payloads import (
    apply_bundle_payload as _apply_bundle_payload,
)
from .compact_apply_payloads import (
    compact_apply_refs as _refs,
)
from .compact_apply_payloads import (
    ledger_record as _ledger_record,
)
from .compact_apply_payloads import (
    restore_refs_payload as _restore_refs_payload,
)
from .compact_apply_rendering import render_compact_context_markdown
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
)
from .compact_context_bundle_match import (
    compact_context_bundle_match,
    context_bundle_match_allows_attach,
)
from .compact_context_bundle_refs import (
    compact_context_bundle_summary,
    load_main_context_bundle_ref,
)
from .compact_gate_bridge import evaluate_pre_compaction_state
from .schema import (
    RuntimeMemorySchemaOptions,
    runtime_memory_reserved_fields,
    runtime_memory_schema_payload,
)

COMPACT_APPLY_SCHEMA = RuntimeMemorySchemaOptions("compact_apply")


# LLM: memory archive compact apply 的入口 bundle；新增 apply 策略字段时放这里，不拉长函数签名。
# 类用途: 保存 compact apply 的计划过滤条件和执行来源；关键副作用: 本身不写文件，传给 apply_memory_compact 后才会落盘。
@dataclass(frozen=True)
class MemoryCompactApplyOptions:
    """Bundle inputs for non-destructive compact apply."""

    # LLM: plan_options preserves the exact dry-run scope used to create apply artifacts.
    plan_options: MemoryCompactPlanOptions
    actor: str = "memory-compact"
    main_context_bundle_ref: str = ""
    main_context_bundle_ref_explicit: bool = False


# LLM: _ApplyMetadataBuildRequest 属于 compact apply 内部 bundle；防止 metadata helper 参数继续增长。
# 类用途: 汇总 metadata 生成所需的 plan、options、event 和 path 信息，便于后续扩展 reserved 字段。
@dataclass(frozen=True)
class _ApplyMetadataBuildRequest:
    plan: dict[str, Any]
    options: MemoryCompactApplyOptions
    event_id: str
    now: str
    paths: dict[str, Path]
    lineage: dict[str, Any]


@dataclass(frozen=True)
class _SelfCheckRequest:
    payload: dict[str, Any]
    plan: dict[str, Any]
    paths: dict[str, Path]
    now: str
    work_state: dict[str, Any]


# LLM: apply_memory_compact 是 memory compact 的显式 apply 边界；保持非破坏性和可审计输出。
# 函数用途: 根据 dry-run plan 写 compact context、metadata、ledger 和 self-check；不会删除或覆盖归档事实源。
def apply_memory_compact(root: str | Path, options: MemoryCompactApplyOptions) -> dict[str, Any]:
    workspace = Path(root)
    plan = build_memory_compact_plan(workspace, options.plan_options)
    now = _utc_now()
    plan_id = compact_apply_plan_id(plan)
    apply_id = _unique_apply_id(workspace, plan_id, now)
    paths = _apply_paths(workspace, apply_id)
    lineage = build_compact_apply_lineage(
        CompactApplyLineageRequest(
            ledger_path=paths["ledger_jsonl"],
            plan_id=plan_id,
            apply_id=apply_id,
            metadata_ref=str(paths["metadata_json"]),
            apply_bundle_ref=str(paths["apply_bundle_json"]),
        )
    )
    main_context_bundle = load_main_context_bundle_ref(workspace, options.main_context_bundle_ref)
    payload = _metadata_payload(_ApplyMetadataBuildRequest(plan, options, apply_id, now, paths, lineage))
    _attach_main_context_bundle(payload, main_context_bundle, plan)
    context = render_compact_context_markdown(payload, plan)
    _write_text(paths["context_md"], context)
    restore_refs = _restore_refs_payload(payload, plan, paths, now)
    _write_json(paths["restore_refs_json"], restore_refs)
    work_state = build_work_state_snapshot(
        WorkStateSnapshotRequest(plan, restore_refs, paths, now, payload["apply_id"], payload["plan_id"])
    )
    _write_json(paths["work_state_snapshot_json"], work_state)
    apply_bundle = _apply_bundle_payload(payload, restore_refs, work_state, paths)
    _write_json(paths["apply_bundle_json"], apply_bundle)
    _attach_compaction_gate(payload, restore_refs, work_state)
    self_check = _attach_self_check(_SelfCheckRequest(payload, plan, paths, now, work_state))
    payload["post_compact_self_check"] = self_check
    payload["restore_refs"] = restore_refs
    payload["work_state_snapshot"] = work_state
    payload["apply_bundle"] = apply_bundle
    _write_json(paths["self_check_json"], self_check)
    _write_json(paths["metadata_json"], payload)
    _append_jsonl(paths["ledger_jsonl"], _ledger_record(payload))
    return payload


# LLM: _attach_compaction_gate stores the shared compaction gate decision on apply metadata.
# 函数用途: 把 compact 前状态快照写成机器字段，供 memory-resume 做 post_compact 对比。
def _attach_compaction_gate(
    payload: dict[str, Any], restore_refs: dict[str, Any], work_state: dict[str, Any]
) -> None:
    compaction_gate = evaluate_pre_compaction_state(payload, restore_refs, work_state)
    payload["compaction_gate"] = compaction_gate
    if not compaction_gate["pre"]["allowed"]:
        payload["ok"] = False
        payload["compact_status"] = "blocked_compaction_gate_failed"


# LLM: _attach_self_check finalizes compact apply validation without expanding the entrypoint.
# 函数用途: 执行原有 self-check，失败时写结构化 failure payload 并更新 compact_status。
def _attach_self_check(request: _SelfCheckRequest) -> dict[str, Any]:
    self_check = _self_check_payload(request.plan, request.paths, request.now, request.work_state)
    if not self_check["ok"]:
        request.payload["ok"] = False
        request.payload["compact_status"] = "blocked_self_check_failed"
        request.payload["self_check_failure"] = _self_check_failure_payload(
            request.payload, self_check, _refs(request.paths), request.now
        )
        _write_json(request.paths["failed_self_check_json"], request.payload["self_check_failure"])
    return self_check


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
        "lineage": dict(request.lineage),
        "actor": request.options.actor,
        "main_context_bundle_ref_explicit": bool(request.options.main_context_bundle_ref_explicit),
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


# LLM: _attach_main_context_bundle records the root run card without making it a hard dependency.
# 函数用途: 将主代理 context bundle 摘要挂到 apply metadata/refs；旧调用未传时保持空摘要。
def _attach_main_context_bundle(payload: dict[str, Any], main_context_bundle: dict[str, Any], plan: dict[str, Any]) -> None:
    match = compact_context_bundle_match(
        plan.get("scope", {}),
        main_context_bundle,
        explicit=bool(payload.get("main_context_bundle_ref_explicit")),
    )
    payload["main_context_bundle_match"] = match
    if not context_bundle_match_allows_attach(match):
        payload["main_context_bundle"] = compact_context_bundle_summary({**main_context_bundle, "ref": "", "loaded": False})
        return
    payload["main_context_bundle"] = compact_context_bundle_summary(main_context_bundle)
    ref = str(main_context_bundle.get("ref", "") or "")
    if ref:
        payload["refs"]["main_context_bundle"] = ref


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
