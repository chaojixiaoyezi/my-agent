
from __future__ import annotations

"""Helpers for subagent board item construction.

Risk flags use the current TaskStatus helpers; unknown raw status text remains
audit data and cannot become a board machine fact.
"""

import json
import re
import time
from pathlib import Path
from typing import Any

from ....model_visible_refs import current_model_ref, current_model_text
from ....runtime_errors import runtime_error_report
from ...model_capabilities import capability_request_counts_as_open
from ...models import (
    SubAgentBoardOptions,
    SubAgentTask,
    TaskStatus,
    task_has_failure_status,
    task_has_status,
    task_is_done_verified,
    task_status_reason_code,
)
from ...policies import (
    RISK_FLAG_CHANNEL_BROKEN,
    RISK_FLAG_CHANNEL_DEGRADED,
    RISK_FLAG_DONE_WITHOUT_EVIDENCE,
    RISK_FLAG_DONE_WITHOUT_VERIFICATION,
    RISK_FLAG_OPEN_CAPABILITY_GAP,
    RISK_FLAG_OPEN_CAPABILITY_REQUEST,
    RISK_FLAG_TAKEN_OVER,
)
from ...reports import SubAgentBoardItem
from ..agent_run_state import read_agent_state_payload


def build_risk_flags(
    task: SubAgentTask,
    open_request_count: int,
    open_gap_count: int,
) -> list[str]:
    flags: list[str] = []
    if task_has_failure_status(task):
        flags.append(task_status_reason_code(task.status))
    if task_has_status(task, TaskStatus.DONE) and not task.evidence:
        flags.append(RISK_FLAG_DONE_WITHOUT_EVIDENCE)
    if task_has_status(task, TaskStatus.DONE) and not task_is_done_verified(task):
        flags.append(RISK_FLAG_DONE_WITHOUT_VERIFICATION)
    if open_request_count:
        flags.append(RISK_FLAG_OPEN_CAPABILITY_REQUEST)
    if open_gap_count:
        flags.append(RISK_FLAG_OPEN_CAPABILITY_GAP)
    if task.takeover_by:
        flags.append(RISK_FLAG_TAKEN_OVER)
    if task.channel_status == "BROKEN":
        flags.append(RISK_FLAG_CHANNEL_BROKEN)
    if task.channel_status == "DEGRADED":
        flags.append(RISK_FLAG_CHANNEL_DEGRADED)
    return flags


def scoped_due_check_tasks(
    tasks: list[SubAgentTask],
    root_id: str,
    include_run_ids: list[str] | None = None,
    exclude_run_ids: list[str] | None = None,
) -> list[SubAgentTask]:
    normalized = str(root_id or "").strip()
    included = {str(item) for item in (include_run_ids or []) if str(item or "").strip()}
    excluded = {str(item) for item in (exclude_run_ids or []) if str(item or "").strip()}
    filtered = [task for task in tasks if task.id not in excluded]
    if included:
        filtered = [task for task in filtered if task.id in included]
    if not normalized:
        return filtered
    return [task for task in filtered if (task.root_id or task.id) == normalized]


def to_board_item(
    manager: Any,
    task: SubAgentTask,
    *,
    task_index: dict[str, SubAgentTask] | None = None,
    include_child_status_counts: bool = True,
) -> SubAgentBoardItem:
    counts = _board_open_counts(task)
    child_counts, child_load_errors = _board_child_counts(
        manager,
        task,
        task_index=task_index,
        include_child_status_counts=include_child_status_counts,
    )
    return SubAgentBoardItem(**_board_item_payload(task, counts, child_counts, child_load_errors))


def board_options(
    options: SubAgentBoardOptions | None,
    *,
    recent_limit: int,
) -> SubAgentBoardOptions:
    if options is not None:
        if not isinstance(options, SubAgentBoardOptions):
            raise TypeError("build_board requires options: SubAgentBoardOptions")
        return options
    return SubAgentBoardOptions(recent_limit=recent_limit)


def _board_open_counts(task: SubAgentTask) -> tuple[int, int]:
    open_request_count = sum(
        1 for item in task.capability_requests if capability_request_counts_as_open(getattr(item, "status", "OPEN"))
    )
    open_gap_count = sum(1 for item in task.capability_gaps if item.status == "OPEN")
    return open_request_count, open_gap_count


def _board_child_counts(
    manager: Any,
    task: SubAgentTask,
    *,
    task_index: dict[str, SubAgentTask] | None,
    include_child_status_counts: bool,
) -> tuple[dict[str, int], list[dict[str, object]]]:
    if not include_child_status_counts:
        return {}, []
    return _child_status_counts(manager, task, task_index=task_index)


def _board_item_payload(
    task: SubAgentTask,
    counts: tuple[int, int],
    child_status_counts: dict[str, int],
    child_status_load_errors: list[dict[str, object]],
) -> dict[str, object]:
    open_request_count, open_gap_count = counts
    timing = _task_timing(task)
    return {
        **_board_identity_payload(task),
        **_board_count_payload(
            task,
            counts,
            (child_status_counts, child_status_load_errors),
        ),
        **_board_progress_payload(task, timing, open_request_count, open_gap_count),
        **_board_ref_payload(task),
    }


def _board_identity_payload(task: SubAgentTask) -> dict[str, object]:
    return {
        "id": task.id,
        "root_id": task.root_id,
        "parent_id": task.parent_id,
        "depth": task.depth,
        "agent_name": task.agent_name,
        "role": task.role,
        "status": task.status,
        "verification_status": task.verification_status,
        "channel_status": task.channel_status,
        "owner": task.owner,
        "supervisor": task.supervisor,
        "final_owner": task.final_owner,
        "goal": current_model_text(task.goal),
        "updated_at": task.updated_at,
        "heartbeat_at": task.heartbeat_at,
        "created_at": task.created_at,
    }


def _board_count_payload(
    task: SubAgentTask,
    counts: tuple[int, int],
    child_status: tuple[dict[str, int], list[dict[str, object]]],
) -> dict[str, object]:
    open_request_count, open_gap_count = counts
    child_status_counts, child_status_load_errors = child_status
    return {
        "evidence_count": len(task.evidence),
        "evidence_packet_count": len(task.evidence_packets),
        "finding_count": len(task.findings),
        "open_request_count": open_request_count,
        "open_gap_count": open_gap_count,
        "child_count": len(task.child_ids),
        "child_status_counts": child_status_counts,
        "child_status_load_errors": child_status_load_errors,
    }


def _board_progress_payload(
    task: SubAgentTask,
    timing: dict[str, float],
    open_request_count: int,
    open_gap_count: int,
) -> dict[str, object]:
    return {
        "progress": max(0.0, min(1.0, float(task.progress or 0.0))),
        "current_step": current_model_text(task.current_step),
        "latest_summary": current_model_text(task.latest_summary),
        "blocker_count": len(task.blockers),
        "running_seconds": timing["running_seconds"],
        "seconds_since_progress": timing["seconds_since_progress"],
        "takeover_by": task.takeover_by,
        "locked_file_count": len(task.locked_files),
        "risk_flags": build_risk_flags(task, open_request_count, open_gap_count),
    }


def _board_ref_payload(task: SubAgentTask) -> dict[str, object]:
    return {
        "task_root": current_model_ref(task.task_workspace_dir),
        "final_report_ref": current_model_ref(task.agent_run_final_report_md or task.output_json),
        "task_work_dir": str(Path(task.task_workspace_dir) / "work") if current_model_ref(task.task_workspace_dir) else "",
        "task_output_dir": str(Path(task.task_workspace_dir) / "output") if current_model_ref(task.task_workspace_dir) else "",
        "agent_work_dir": current_model_ref(task.agent_run_workspace_dir),
        "checkpoint_ref": current_model_ref(task.agent_run_checkpoint_json or task.checkpoint_json or task.checkpoint_ref),
        "summary_ref": current_model_ref(task.agent_run_summary_md),
        "latest_tool_progress_ref": _latest_tool_progress_ref(task),
        "target_tokens": sorted(task_actual_target_tokens(task))[:20],
        "artifact_refs": _bounded_unique_strings(task.artifact_refs, limit=12),
        "artifact_registry_refs": _registry_records(task.attributes.get("artifact_registry_refs"), limit=12),
        "evidence_refs": _bounded_unique_strings(task.evidence_refs, limit=12),
    }


def _task_timing(task: SubAgentTask) -> dict[str, float]:
    now = time.time()
    started = float(task.heartbeat_at or task.created_at or task.updated_at or 0.0)
    progress_at = float(task.last_progress_at or task.heartbeat_at or task.updated_at or task.created_at or 0.0)
    return {
        "running_seconds": max(0.0, now - started) if started > 0 else 0.0,
        "seconds_since_progress": max(0.0, now - progress_at) if progress_at > 0 else 0.0,
    }


def _child_status_counts(
    manager: Any,
    task: SubAgentTask,
    *,
    task_index: dict[str, SubAgentTask] | None = None,
) -> tuple[dict[str, int], list[dict[str, object]]]:
    counts: dict[str, int] = {}
    errors: list[dict[str, object]] = []
    for child_id in task.child_ids:
        try:
            child = task_index[child_id] if task_index is not None else manager.load(child_id)
        except (FileNotFoundError, TypeError, KeyError):
            counts["missing"] = counts.get("missing", 0) + 1
            continue
        except (OSError, ValueError, UnicodeError) as exc:
            counts["load_error"] = counts.get("load_error", 0) + 1
            errors.append({
                "child_id": str(child_id),
                **runtime_error_report(exc, context="subagent_board.child_status.load"),
            })
            continue
        if child is None:
            counts["missing"] = counts.get("missing", 0) + 1
            continue
        counts[child.status] = counts.get(child.status, 0) + 1
    return counts, errors


def _bounded_unique_strings(value: object, *, limit: int) -> list[str]:
    if not isinstance(value, list | tuple | set):
        return []
    items: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = current_model_ref(item)
        if not text or text in seen:
            continue
        seen.add(text)
        items.append(text)
        if len(items) >= limit:
            break
    return items


def _registry_records(value: object, *, limit: int) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        row = _registry_record(item)
        key = _registry_record_key(row)
        if not key or key in seen:
            continue
        seen.add(key)
        rows.append(row)
        if len(rows) >= limit:
            break
    return rows


def _registry_record(item: dict[str, object]) -> dict[str, object]:
    row = dict(item)
    if "path" not in row:
        return row
    path = current_model_ref(row.get("path"))
    if path:
        return {**row, "path": path}
    return {key: value for key, value in row.items() if key != "path"}


def _registry_record_key(row: dict[str, object]) -> str:
    return str(row.get("artifact_id") or row.get("path") or "").strip()


def _latest_tool_progress_ref(task: SubAgentTask) -> str:
    workspace = str(getattr(task, "agent_run_workspace_dir", "") or "").strip()
    workspace_ref = current_model_ref(workspace)
    return str(Path(workspace_ref) / "progress" / "latest_tool_progress.json") if workspace_ref else ""


def task_actual_target_tokens(item: Any) -> set[str]:
    attribute_targets = _target_tokens_from_attributes(getattr(item, "attributes", {}) or {})
    if attribute_targets:
        return attribute_targets
    output_targets = _target_tokens_from_output_json(getattr(item, "output_json", "") or "")
    if output_targets:
        return output_targets
    result_targets = _target_tokens_from_result_json(_task_result_text(item))
    if result_targets:
        return result_targets
    return _target_tokens_from_output_values(getattr(item, "extra_write_roots", []) or [])


def _target_tokens_from_attributes(attributes: dict[str, object]) -> set[str]:
    if not isinstance(attributes, dict):
        return set()
    targets: set[str] = set()
    for field in ("output_refs", "output_files", "artifact_refs"):
        targets.update(_target_tokens_from_output_values(attributes.get(field)))
    return targets


def _target_tokens_from_output_json(output_json: str) -> set[str]:
    path = Path(str(output_json or ""))
    if not output_json or not path.is_file():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return set()
    targets: set[str] = set()
    targets.update(_target_tokens_from_output_values(payload.get("artifact_path")))
    targets.update(_target_tokens_from_output_values(payload.get("artifacts")))
    targets.update(_target_tokens_from_output_values(payload.get("patches")))
    return targets


def _target_tokens_from_result_json(result_text: str) -> set[str]:
    text = str(result_text or "")
    match = re.search(r"\[SUBAGENT_RESULT\]\s*(\{.*\})\s*\[/SUBAGENT_RESULT\]", text, re.DOTALL)
    if not match:
        return set()
    try:
        payload = json.loads(match.group(1))
    except (json.JSONDecodeError, TypeError):
        return set()
    targets: set[str] = set()
    targets.update(_target_tokens_from_output_values(payload.get("artifact_path")))
    targets.update(_target_tokens_from_output_values(payload.get("artifacts")))
    targets.update(_target_tokens_from_output_values(payload.get("patches")))
    return targets


def _target_tokens_from_output_values(value: Any) -> set[str]:
    if isinstance(value, str):
        return set(_target_tokens_from_text(value))
    if isinstance(value, dict):
        values = [value.get(key) for key in ("path", "artifact_path")]
        return {token for item in values for token in _target_tokens_from_output_values(item)}
    if isinstance(value, list):
        return {token for item in value for token in _target_tokens_from_output_values(item)}
    return set()


def _task_result_text(item: Any) -> str:
    direct = str(getattr(item, "result", "") or "")
    if direct:
        return direct
    task_dir = Path(str(getattr(item, "task_dir", "") or ""))
    if not str(task_dir):
        return ""
    try:
        payload = read_agent_state_payload(task_dir / "task.json")
    except (OSError, json.JSONDecodeError, TypeError, FileNotFoundError):
        return ""
    return str(payload.get("result") or "")


def _target_tokens_from_text(text: str) -> list[str]:
    tokens: list[str] = []
    for match in _target_file_match_candidates(text):
        token = Path(match.strip("`'\" ,;:，。；：、)]}）】")).name.lower()
        if token and token not in tokens:
            tokens.append(token)
    return tokens


def _target_file_match_candidates(text: str) -> list[str]:
    return [
        match
        for match in re.findall(r"[\w./~:-]+\.(?:html|css|js|ts|tsx|jsx|py|md|json|txt|csv|yaml|yml)", str(text or ""))
        if "://" not in match
    ]
