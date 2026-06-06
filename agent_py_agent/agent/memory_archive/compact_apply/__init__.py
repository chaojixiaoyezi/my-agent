
from __future__ import annotations

"""non-destructive memory compact apply records.

Human version:
This module turns a dry-run compact plan into durable apply artifacts. It does
not delete or rewrite raw archive files, snapshots, token ledgers, task files,
or artifacts. The first apply step only materializes a compact context and a
self-check report so later resume/apply stages have a trustworthy boundary.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...common.value_parsing import sequence_strings
from ..compact import MemoryCompactPlanOptions, build_memory_compact_plan
from ..compact_context_bundle import (
    compact_context_bundle_match,
    compact_context_bundle_summary,
    context_bundle_match_allows_attach,
    load_main_context_bundle_ref,
    main_context_bundle_source_refs,
)
from ..compact_gate_bridge import evaluate_pre_compaction_state
from ..compact_state import (
    CompactionStateRequest,
    build_compaction_state,
    render_compaction_handoff_summary,
)
from ..compact_tool_output_refs import tool_call_source_refs, tool_output_source_refs
from ..schema import (
    RuntimeMemorySchemaOptions,
    runtime_memory_schema_payload,
)
from .work_state import (
    WorkStateSnapshotRequest,
    build_work_state_snapshot,
    restore_refs_summary,
    work_state_summary,
)

COMPACT_APPLY_SCHEMA = RuntimeMemorySchemaOptions("compact_apply")
COMPACT_APPLY_BUNDLE_SCHEMA = RuntimeMemorySchemaOptions("compact_apply_bundle")
COMPACT_APPLY_LEDGER_SCHEMA = RuntimeMemorySchemaOptions("compact_apply_ledger")
COMPACT_RESTORE_REFS_SCHEMA = RuntimeMemorySchemaOptions("compact_apply_restore_refs")
COMPACT_SELF_CHECK_SCHEMA = RuntimeMemorySchemaOptions("compact_apply_self_check")
COMPACT_SELF_CHECK_FAILURE_SCHEMA = RuntimeMemorySchemaOptions("compact_apply_self_check_failure")


@dataclass(frozen=True)
class MemoryCompactApplyOptions:
    """Bundle inputs for non-destructive compact apply."""

    plan_options: MemoryCompactPlanOptions
    actor: str = "memory-compact"
    main_context_bundle_ref: str = ""
    main_context_bundle_ref_explicit: bool = False


@dataclass(frozen=True)
class _ApplyMetadataBuildRequest:
    plan: dict[str, Any]
    options: MemoryCompactApplyOptions
    event_id: str
    now: str
    paths: dict[str, Path]
    lineage: dict[str, Any]
    run_scope_id: str


@dataclass(frozen=True)
class _PrepareApplyMetadataRequest:
    workspace: Path
    plan: dict[str, Any]
    options: MemoryCompactApplyOptions
    now: str


@dataclass(frozen=True)
class CompactApplyLineageRequest:
    ledger_path: Path
    plan_id: str
    apply_id: str
    metadata_ref: str
    apply_bundle_ref: str


@dataclass(frozen=True)
class CompactApplyFinalizeRequest:
    """Bundle final compact apply validation and ledger-write inputs."""

    payload: dict[str, Any]
    plan: dict[str, Any]
    paths: dict[str, Path]
    now: str
    restore_refs: dict[str, Any]
    work_state: dict[str, Any]
    apply_bundle: dict[str, Any]


def apply_memory_compact(root: str | Path, options: MemoryCompactApplyOptions) -> dict[str, Any]:
    workspace = Path(root)
    plan = build_memory_compact_plan(workspace, options.plan_options)
    now = _utc_now()
    paths, payload = _prepare_apply_metadata(_PrepareApplyMetadataRequest(workspace, plan, options, now))
    context = render_compact_context_markdown(payload, plan)
    _write_text(paths["context_md"], context)
    restore_refs, work_state, _, _ = _write_apply_state_documents(plan, payload, paths, now)
    apply_bundle = apply_bundle_payload(payload, restore_refs, work_state, paths)
    _write_json(paths["apply_bundle_json"], apply_bundle)
    finalize_apply_payload(
        CompactApplyFinalizeRequest(payload, plan, paths, now, restore_refs, work_state, apply_bundle)
    )
    return payload


def _prepare_apply_metadata(request: _PrepareApplyMetadataRequest) -> tuple[dict[str, Path], dict[str, Any]]:
    plan = request.plan
    workspace = request.workspace
    options = request.options
    plan_id = compact_apply_plan_id(plan)
    run_scope_id = _compact_run_scope_id(plan)
    apply_id = _unique_apply_id(workspace, plan, plan_id, request.now)
    paths = _apply_paths(workspace, apply_id, plan)
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
    payload = _metadata_payload(_ApplyMetadataBuildRequest(plan, options, apply_id, request.now, paths, lineage, run_scope_id))
    _attach_main_context_bundle(payload, main_context_bundle, plan)
    return paths, payload


def _write_apply_state_documents(
    plan: dict[str, Any],
    payload: dict[str, Any],
    paths: dict[str, Path],
    now: str,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str]:
    restore_refs = restore_refs_payload(payload, plan, paths, now)
    _write_json(paths["restore_refs_json"], restore_refs)
    work_state = build_work_state_snapshot(
        WorkStateSnapshotRequest(plan, restore_refs, paths, now, payload["apply_id"], payload["plan_id"])
    )
    _write_json(paths["work_state_snapshot_json"], work_state)
    compaction_state = build_compaction_state(CompactionStateRequest(payload, restore_refs, work_state, paths))
    handoff_summary = render_compaction_handoff_summary(compaction_state)
    _write_json(paths["compaction_state_json"], compaction_state)
    _write_text(paths["handoff_summary_md"], handoff_summary)
    payload["compaction_state"] = compaction_state
    payload["handoff_summary"] = handoff_summary
    return restore_refs, work_state, compaction_state, handoff_summary


def _apply_paths(workspace: Path, event_id: str, plan: dict[str, Any]) -> dict[str, Path]:
    directory = _compact_apply_directory(workspace, plan)
    global_directory = workspace / "memory_archive" / "compact_applies"
    return {
        "directory": directory,
        "context_md": directory / f"{event_id}.md",
        "compaction_state_json": directory / f"{event_id}.compaction_state.json",
        "handoff_summary_md": directory / f"{event_id}.handoff.md",
        "metadata_json": directory / f"{event_id}.json",
        "apply_bundle_json": directory / f"{event_id}.apply_bundle.json",
        "restore_refs_json": directory / f"{event_id}.restore_refs.json",
        "work_state_snapshot_json": directory / f"{event_id}.work_state_snapshot.json",
        "self_check_json": directory / f"{event_id}.self_check.json",
        "failed_self_check_json": directory / f"{event_id}.self_check_failed.json",
        "ledger_jsonl": directory / "ledger.jsonl",
        "global_ledger_jsonl": global_directory / "ledger.jsonl",
    }


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
        "run_scope_id": request.run_scope_id,
        "source_plan": _source_plan(plan),
        "refs": compact_apply_refs(request.paths),
        "lineage": dict(request.lineage),
        "actor": request.options.actor,
        "main_context_bundle_ref_explicit": bool(request.options.main_context_bundle_ref_explicit),
        "created_at": request.now,
        "content_preserved": True,
        "restore_ready": True,
    }


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


def _attach_main_context_bundle(payload: dict[str, Any], main_context_bundle: dict[str, Any], plan: dict[str, Any]) -> None:
    match = compact_context_bundle_match(
        plan.get("scope", {}),
        main_context_bundle,
        explicit=bool(payload.get("main_context_bundle_ref_explicit")),
    )
    payload["main_context_bundle_match"] = match
    if not context_bundle_match_allows_attach(match):
        load_error = main_context_bundle.get("load_error")
        if isinstance(load_error, dict) and load_error:
            payload["main_context_bundle"] = compact_context_bundle_summary(main_context_bundle)
            return
        payload["main_context_bundle"] = compact_context_bundle_summary({**main_context_bundle, "ref": "", "loaded": False})
        return
    payload["main_context_bundle"] = compact_context_bundle_summary(main_context_bundle)
    ref = str(main_context_bundle.get("ref", "") or "")
    if ref:
        payload["refs"]["main_context_bundle"] = ref


def compact_apply_plan_id(plan: dict[str, Any]) -> str:
    return "plan-" + _sha256_json(_identity_payload(plan))[:16]


def compact_apply_id(plan_id: str, now: str) -> str:
    return "apply-" + plan_id.removeprefix("plan-") + "-" + _safe_time_segment(now)


def compact_scope_hash(plan: dict[str, Any]) -> str:
    return _sha256_json({"workspace_root": plan["workspace_root"], "scope": plan["scope"]})[:16]


def compact_candidate_counts(plan: dict[str, Any]) -> dict[str, int]:
    return {
        "archive_records": int(plan["archive"]["record_count"]),
        "archive_files": int(plan["archive"]["file_count"]),
        "snapshot_files": int(plan["snapshots"]["file_count"]),
        "token_ledgers": int(plan["tokens"]["ledger_count"]),
    }


def compact_risk_level(plan: dict[str, Any]) -> str:
    if plan["risks"]:
        return "medium"
    if int(plan["snapshots"].get("invalid_count", 0) or 0) or int(plan["tokens"].get("invalid_count", 0) or 0):
        return "medium"
    return "low"


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
    }
    result["source_refs"]["context_bundles"] = main_context_bundle_source_refs(payload.get("main_context_bundle", {}))
    return result


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
    }


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
    }


def render_compact_context_markdown(payload: dict[str, Any], plan: dict[str, Any]) -> str:
    source = payload["source_plan"]
    return (
        "# Memory Compact Context\n\n"
        f"- event_id: {payload['event_id']}\n"
        f"- compact_status: {payload['compact_status']}\n"
        f"- workspace_root: {payload['workspace_root']}\n"
        f"- archive_records: {source['archive_record_count']}\n"
        f"- snapshots: {source['snapshot_file_count']}\n"
        f"- token_ledgers: {source['token_ledger_count']}\n"
        f"- main_context_bundle: {payload.get('refs', {}).get('main_context_bundle', '') or '-'}\n"
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


def build_compact_apply_lineage(request: CompactApplyLineageRequest) -> dict[str, Any]:
    previous = _latest_record(request.ledger_path)
    if not previous:
        previous = _latest_record_for_plan(request.ledger_path, request.plan_id)
    previous_lineage = previous.get("lineage", {}) if isinstance(previous.get("lineage"), dict) else {}
    previous_cycle = _positive_int(previous_lineage.get("cycle_index"))
    if previous and previous_cycle == 0:
        previous_cycle = _count_records(request.ledger_path)
    refs = previous.get("refs", {}) if isinstance(previous.get("refs"), dict) else {}
    previous_apply_id = str(previous.get("apply_id") or "")
    return {
        "schema_version": 1,
        "status": "continued" if previous_apply_id else "root",
        "plan_id": request.plan_id,
        "current_apply_id": request.apply_id,
        "current_metadata_ref": request.metadata_ref,
        "current_apply_bundle_ref": request.apply_bundle_ref,
        "cycle_index": previous_cycle + 1 if previous_apply_id else 1,
        "previous_apply_id": previous_apply_id,
        "previous_metadata_ref": str(refs.get("metadata") or ""),
        "previous_apply_bundle_ref": str(refs.get("apply_bundle") or ""),
        "content_preserved": True,
    }


def finalize_apply_payload(request: CompactApplyFinalizeRequest) -> None:
    """Attach validation outputs and write metadata plus ledgers."""

    _attach_compaction_gate(request.payload, request.restore_refs, request.work_state)
    self_check = _attach_self_check(request)
    request.payload["post_compact_self_check"] = self_check
    request.payload["restore_refs"] = request.restore_refs
    request.payload["work_state_snapshot"] = request.work_state
    request.payload["apply_bundle"] = request.apply_bundle
    _write_json(request.paths["self_check_json"], self_check)
    _write_json(request.paths["metadata_json"], request.payload)
    _append_jsonl(request.paths["ledger_jsonl"], ledger_record(request.payload))
    if request.paths["global_ledger_jsonl"] != request.paths["ledger_jsonl"]:
        _append_jsonl(request.paths["global_ledger_jsonl"], ledger_record(request.payload))


def build_self_check_payload(
    plan: dict[str, Any], paths: dict[str, Path], now: str, work_state: dict[str, Any]
) -> dict[str, Any]:
    checks = _self_checks(plan, paths, work_state)
    return {
        "version": COMPACT_SELF_CHECK_SCHEMA.version,
        "schema": runtime_memory_schema_payload(COMPACT_SELF_CHECK_SCHEMA),
        "ok": all(item["ok"] for item in checks if item["severity"] == "hard"),
        "apply_id": work_state["apply_id"],
        "plan_id": work_state["plan_id"],
        "event_type": "post_compact_self_check",
        "checks": checks,
        "created_at": now,
    }


def build_self_check_failure_payload(
    payload: dict[str, Any], self_check: dict[str, Any], refs: dict[str, str], now: str
) -> dict[str, Any]:
    return {
        "version": COMPACT_SELF_CHECK_FAILURE_SCHEMA.version,
        "schema": runtime_memory_schema_payload(COMPACT_SELF_CHECK_FAILURE_SCHEMA),
        "ok": False,
        "event_id": payload["event_id"],
        "apply_id": payload["apply_id"],
        "plan_id": payload["plan_id"],
        "event_type": "compact_apply_self_check_failure",
        "compact_status": "blocked_self_check_failed",
        "created_at": now,
        "failed_checks": [item for item in self_check["checks"] if not item["ok"]],
        "refs": refs,
        "content_preserved": True,
    }


def _source_refs(plan: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {
        "archive_files": _file_refs(plan["archive"].get("files", [])),
        "snapshot_files": _file_refs(plan["snapshots"].get("latest", [])),
        "token_ledgers": _file_refs(plan["tokens"].get("latest", [])),
        "tool_calls": tool_call_source_refs(plan["workspace_root"], plan["scope"]),
        "tool_outputs": tool_output_source_refs(plan["workspace_root"], plan["scope"]),
    }


def _identity_payload(plan: dict[str, Any]) -> dict[str, Any]:
    return {
        "workspace_root": plan["workspace_root"],
        "scope": plan["scope"],
        "candidate_counts": compact_candidate_counts(plan),
        "estimated_compactable_bytes": plan["estimated_compactable_bytes"],
        "risks": list(plan["risks"]),
    }


def _sha256_json(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _safe_time_segment(value: str) -> str:
    return value.replace(":", "").replace("-", "").replace("+", "Z")


def _file_refs(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for item in items:
        refs.append({
            "path": str(item.get("path") or item.get("file_path") or ""),
            "size_bytes": int(item.get("size_bytes", 0) or 0),
            "created_at": str(item.get("created_at", "") or ""),
        })
    return refs


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


def _restore_steps() -> list[str]:
    return [
        "continue from resume_focus.next_action or work_state_summary first",
        "use compact_context, work_state_snapshot, restore_refs, or post_compact_self_check only when facts are missing, refs look broken, or verification is needed",
        "use restore_refs to verify raw/hook archives, snapshots, token ledgers, and task/run workspaces when rebuilding context",
        "trust restored answers only after source refs still exist and match the requested scope",
    ]


def _resume_focus_summary(work_state: dict[str, Any]) -> dict[str, Any]:
    actions = sequence_strings(work_state.get("next_actions"))
    next_step = str(work_state.get("next_step") or "").strip()
    return {
        "next_action": actions[0] if actions else next_step,
        "next_actions": actions,
    }


def _attach_compaction_gate(
    payload: dict[str, Any], restore_refs: dict[str, Any], work_state: dict[str, Any]
) -> None:
    compaction_gate = evaluate_pre_compaction_state(payload, restore_refs, work_state)
    payload["compaction_gate"] = compaction_gate
    if not compaction_gate["pre"]["allowed"]:
        payload["ok"] = False
        payload["compact_status"] = "blocked_compaction_gate_failed"


def _attach_self_check(request: CompactApplyFinalizeRequest) -> dict[str, Any]:
    self_check = _self_check_payload(request.plan, request.paths, request.now, request.work_state)
    if not self_check["ok"]:
        request.payload["ok"] = False
        request.payload["compact_status"] = "blocked_self_check_failed"
        request.payload["self_check_failure"] = _self_check_failure_payload(
            request.payload, self_check, compact_apply_refs(request.paths), request.now
        )
        _write_json(request.paths["failed_self_check_json"], request.payload["self_check_failure"])
    return self_check


def _self_checks(plan: dict[str, Any], paths: dict[str, Path], work_state: dict[str, Any]) -> list[dict[str, Any]]:
    completeness = work_state["completeness"]
    return [
        {"name": "source_content_preserved", "ok": True, "severity": "hard"},
        {"name": "compact_context_written", "ok": paths["context_md"].exists(), "severity": "hard"},
        {"name": "restore_refs_written", "ok": paths["restore_refs_json"].exists(), "severity": "hard"},
        {"name": "apply_bundle_written", "ok": paths["apply_bundle_json"].exists(), "severity": "hard"},
        {"name": "work_state_snapshot_written", "ok": paths["work_state_snapshot_json"].exists(), "severity": "hard"},
        {"name": "restore_refs_exist", "ok": completeness["restore_refs_exist"], "severity": "hard"},
        {"name": "apply_ids_consistent", "ok": _apply_ids_consistent(paths, work_state), "severity": "hard"},
        {"name": "restore_refs_have_sources", "ok": completeness["source_refs_present"], "severity": "soft"},
        {"name": "goal_present", "ok": completeness["goal_present"], "severity": "soft"},
        {"name": "next_actions_present", "ok": completeness["next_actions_present"], "severity": "soft"},
        {"name": "acceptance_present", "ok": completeness["acceptance_present"], "severity": "soft"},
        {"name": "constraints_present", "ok": completeness["constraints_present"], "severity": "soft"},
        {"name": "latest_tests_recorded", "ok": completeness["test_state_present"], "severity": "soft"},
        {"name": "artifact_refs_valid", "ok": _artifact_refs_valid(work_state), "severity": "soft"},
        {"name": "risks_carried_forward", "ok": isinstance(plan["risks"], list), "severity": "soft"},
    ]


def _apply_ids_consistent(paths: dict[str, Path], work_state: dict[str, Any]) -> bool:
    apply_id = str(work_state["apply_id"])
    return all(apply_id in paths[key].name for key in ("metadata_json", "apply_bundle_json", "work_state_snapshot_json"))


def _artifact_refs_valid(work_state: dict[str, Any]) -> bool:
    refs = work_state.get("artifact_refs", [])
    if not isinstance(refs, list):
        return False
    paths = [Path(str(ref["path"])) for ref in refs if isinstance(ref, dict) and ref.get("path")]
    return all(path.exists() for path in paths)


def _latest_record(path: Path) -> dict[str, Any]:
    latest: dict[str, Any] = {}
    for record in _iter_jsonl_records(path):
        latest = record
    return latest


def _count_records(path: Path) -> int:
    return len(_iter_jsonl_records(path))


def _latest_record_for_plan(path: Path, plan_id: str) -> dict[str, Any]:
    latest: dict[str, Any] = {}
    for record in _iter_jsonl_records(path):
        if str(record.get("plan_id") or "") == plan_id:
            latest = record
    return latest


def _iter_jsonl_records(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            records.append(value)
    return records


def _positive_int(value: Any) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return 0
    return result if result > 0 else 0


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _bullet_lines(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items) if items else "- none"


_self_check_payload = build_self_check_payload
_self_check_failure_payload = build_self_check_failure_payload


def _unique_apply_id(workspace: Path, plan: dict[str, Any], plan_id: str, now: str) -> str:
    base = compact_apply_id(plan_id, now)
    candidate = base
    suffix = 2
    while _apply_paths(workspace, candidate, plan)["metadata_json"].exists():
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def _compact_apply_directory(workspace: Path, plan: dict[str, Any]) -> Path:
    scope_id = _compact_run_scope_id(plan)
    if not scope_id:
        return workspace / "memory_archive" / "compact_applies" / "unscoped"
    return workspace / "memory_archive" / "runs" / _safe_scope_id(scope_id) / "compact_applies"


def _compact_run_scope_id(plan: dict[str, Any]) -> str:
    scope = plan.get("scope", {}) if isinstance(plan.get("scope"), dict) else {}
    for key in ("run_id", "request_id", "task_id", "session_id"):
        value = str(scope.get(key) or "").strip()
        if value:
            return value
    return ""


def _safe_scope_id(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in str(value or "")).strip("-") or "unscoped"


def _utc_now() -> str:
    return datetime.now(tz=timezone.utc).replace(microsecond=0).isoformat()


__all__ = [
    "COMPACT_SELF_CHECK_FAILURE_SCHEMA",
    "COMPACT_SELF_CHECK_SCHEMA",
    "CompactApplyFinalizeRequest",
    "CompactApplyLineageRequest",
    "MemoryCompactApplyOptions",
    "apply_bundle_payload",
    "apply_memory_compact",
    "build_compact_apply_lineage",
    "build_self_check_failure_payload",
    "build_self_check_payload",
    "compact_apply_id",
    "compact_apply_plan_id",
    "compact_apply_refs",
    "compact_candidate_counts",
    "compact_risk_level",
    "compact_scope_hash",
    "finalize_apply_payload",
    "ledger_record",
    "render_compact_context_markdown",
    "restore_refs_payload",
]
