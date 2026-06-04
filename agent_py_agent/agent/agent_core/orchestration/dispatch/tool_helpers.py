
from __future__ import annotations

from pathlib import Path

from agent_py_agent.agent.capability.config import CapabilityConfig

from ....capability.runtime_config import (
    default_capability_config_path,
    load_capability_config_snapshot,
)
from ....runtime_errors import runtime_error_report
from ..run_scope import remembered_orchestration_run_ids
from .refs import related_task_refs
from .scope import dispatch_include_run_ids_param


def _dispatch_capability_config(agent: SimpleAgent) -> CapabilityConfig:
    cfg, load_error = _runtime_capability_config(agent)
    agent._capability_config_load_error = load_error
    if _runner_timeouts_disabled(getattr(agent, "config", None)):
        cfg.subagent_run_timeout = 0
    return cfg


def _runtime_capability_config(agent: SimpleAgent) -> tuple[CapabilityConfig, dict[str, object] | None]:
    path = Path(getattr(agent, "capability_config_path", "") or default_capability_config_path(getattr(agent, "root", ".")))
    try:
        snapshot = load_capability_config_snapshot(path)
    except FileNotFoundError:
        return CapabilityConfig(), None
    except (OSError, TypeError, ValueError) as exc:
        return CapabilityConfig(), _capability_config_load_error(path, exc)
    agent.capability_config_path = path
    agent._capability_config_runtime_snapshot = snapshot
    return snapshot.config, None


def _capability_config_load_error(path: Path, exc: BaseException) -> dict[str, object]:
    report = runtime_error_report(exc, context="dispatch.capability_config.load")
    return {"path": str(path), **report}


def _runner_timeouts_disabled(config: object) -> bool:
    raw = getattr(config, "runner_timeout_seconds", "off")
    if isinstance(raw, str):
        return raw.strip().lower() in {"off", "none", "disabled", "false", "no", "0"}
    try:
        return float(raw) == 0.0
    except (TypeError, ValueError):
        return False


def _run_ids_for_scope(params: dict[str, object], report, agent: object | None = None) -> list[str]:
    ids = dispatch_include_run_ids_param(params, agent=agent)
    for record in getattr(report, "records", []) or []:
        if not _record_touches_run_scope(record):
            continue
        run_id = str(getattr(record, "run_id", "") or "").strip()
        if run_id and run_id not in ids:
            ids.append(run_id)
    return ids


def _run_ids_actually_dispatched(report) -> list[str]:
    ids: list[str] = []
    for record in getattr(report, "records", []) or []:
        if not _record_is_actual_runner_attempt(record):
            continue
        run_id = str(getattr(record, "run_id", "") or "").strip()
        if run_id and run_id not in ids:
            ids.append(run_id)
    return ids


def _record_touches_run_scope(record: object) -> bool:
    run_id = str(getattr(record, "run_id", "") or "").strip()
    if not run_id:
        return False
    step = str(getattr(record, "step", "") or "").strip().lower()
    return step in {"runner", "action_apply", "capability_route", "patch_review"}


def _record_is_actual_runner_attempt(record: object) -> bool:
    step = str(getattr(record, "step", "") or "").strip().lower()
    if step != "runner":
        return False
    action = str(getattr(record, "action", "") or "").strip().lower()
    if action not in {"execute_runner", "retry_runner"}:
        return False
    if bool(getattr(record, "dry_run", False)):
        return False
    return bool(getattr(record, "applied", False))


def _dispatch_top_level_guidance(agent: object, report: object, records: list[dict[str, object]]) -> dict[str, object]:
    blockers = _blocking_run_ids(records)
    unfinished, load_errors = _unfinished_remembered_run_ids(agent)
    payload: dict[str, object] = {
        "completion_status": _dispatch_completion_status(
            blockers,
            unfinished,
            load_errors,
        ),
        "completion_risk": bool(blockers or unfinished or load_errors),
        "must_not_report_done": bool(blockers or unfinished or load_errors),
    }
    if blockers:
        payload["blocking_run_ids"] = blockers
    if unfinished:
        payload["unfinished_run_ids"] = unfinished
    if load_errors:
        payload["unfinished_load_errors"] = load_errors
    if blockers or unfinished:
        payload.setdefault("next_action", _dispatch_next_action(blockers))
    if load_errors:
        payload.setdefault("next_action", "refresh_agent_tree_or_rebuild_state_index")
    artifact_refs = related_task_refs(agent, report, "artifact_refs")
    evidence_refs = related_task_refs(agent, report, "evidence_refs")
    if blockers or unfinished:
        if artifact_refs:
            payload["pending_artifact_refs"] = artifact_refs
        if evidence_refs:
            payload["pending_evidence_refs"] = evidence_refs
        return payload
    if artifact_refs:
        payload["deliverable_artifact_refs"] = artifact_refs
    if evidence_refs:
        payload["deliverable_evidence_refs"] = evidence_refs
    return payload


def _dispatch_next_action(blockers: list[str]) -> str:
    return "repair_or_continue_blocking_run_ids" if blockers else "continue_dispatch_unfinished_run_ids"


def _child_result_index_hint(rows: list[dict[str, object]]) -> str:
    if not rows:
        return ""
    return (
        "先核对 child_result_index 中每个 child 的 summary、final_report_ref 和 artifact refs；"
        "如果协调汇总和 child 摘要冲突，先修复汇总或继续调度，不要只看输出目录或单个协调产物。"
    )


def _dispatch_completion_status(
    blocking_run_ids: list[str],
    unfinished_run_ids: list[str] | None = None,
    load_errors: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    unfinished_run_ids = list(unfinished_run_ids or [])
    load_errors = list(load_errors or [])
    if not blocking_run_ids and not unfinished_run_ids and not load_errors:
        return {
            "status": "complete_or_no_blockers",
            "blocking_run_ids": [],
            "unfinished_run_ids": [],
            "unfinished_load_errors": [],
            "completion_risk": False,
            "must_not_report_done": False,
        }
    payload: dict[str, object] = {
        "status": "not_complete",
        "blocking_run_ids": blocking_run_ids,
        "unfinished_run_ids": unfinished_run_ids,
        "unfinished_load_errors": load_errors,
        "completion_risk": True,
        "must_not_report_done": True,
        "recommended_next_action": "inspect_or_continue_unfinished_run_ids",
    }
    if load_errors and not (blocking_run_ids or unfinished_run_ids):
        payload["recommended_next_action"] = "refresh_agent_tree_or_rebuild_state_index"
    return payload


def _unfinished_remembered_run_ids(agent: object) -> tuple[list[str], list[dict[str, object]]]:
    load = getattr(getattr(agent, "subagents", None), "load", None)
    if not callable(load):
        return [], []
    unfinished: list[str] = []
    load_errors: list[dict[str, object]] = []
    for run_id in sorted(remembered_orchestration_run_ids(agent)):
        try:
            task = load(run_id)
        except Exception as exc:
            load_errors.append(_run_load_error(run_id, exc))
            continue
        status = str(getattr(task, "status", "") or "").strip().upper()
        verification = str(getattr(task, "verification_status", "") or "").strip().upper()
        if status != "DONE" or verification != "VERIFIED":
            unfinished.append(run_id)
    return unfinished[:20], load_errors[:20]


def _run_load_error(run_id: str, exc: BaseException) -> dict[str, object]:
    report = runtime_error_report(exc, context="dispatch.remembered_run.load")
    return {"run_id": run_id, **report}


def _blocking_run_ids(
    records: list[dict[str, object]],
) -> list[str]:
    ids: list[str] = []
    for record in records:
        if bool(record.get("ok", True)):
            continue
        run_id = str(record.get("run_id") or "").strip()
        if run_id and run_id not in ids:
            ids.append(run_id)
    return ids[:20]


def _unique_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result
