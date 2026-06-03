
from __future__ import annotations

"""compact/checkpoint recovery artifact builders for subagent tasks.

Human version:
这里把恢复所需的结构化事实从 persistence service 拆出来。checkpoint 只保留
恢复入口、证据引用、失败测试和下一步动作，不保存完整聊天历史。
"""

from ...common.value_parsing import sequence_strings
from ..models import SubAgentTask


def build_checkpoint_payload(task: SubAgentTask, output_payload: dict[str, object]) -> dict[str, object]:
    """Build the compact recovery checkpoint for one task."""

    return {
        "run_id": task.id,
        "status": task.status,
        "verification_status": task.verification_status,
        "progress": max(0.0, min(1.0, _float_value(task.progress))),
        "current_step": task.current_step or task.status,
        "latest_summary": task.latest_summary,
        "blockers": _unique_strings([*task.blockers, *sequence_strings(output_payload.get("blockers"), allow_scalar=True)]),
        "artifact_refs": _unique_strings(task.artifact_refs),
        "evidence_refs": _unique_strings(task.evidence_refs),
        "evidence_packet_ids": _unique_strings([item.id or item.claim for item in task.evidence_packets]),
        "finding_ids": _unique_strings([item.id or item.claim for item in task.findings]),
        "checkpoint_ref": task.checkpoint_json,
        "status_report_ref": task.status_report_json,
        "handoff_ref": task.handoff_file,
        "work_log_ref": task.work_log_file,
        "acceptance_ref": task.acceptance_file,
        "output_ref": task.output_json,
        "decision_ledger_ref": task.decision_ledger_json,
        "failing_tests_ref": task.failing_tests_json,
        "next_actions_ref": task.next_actions_json,
        "updated_at": task.updated_at or task.heartbeat_at or task.created_at,
    }


def build_decision_ledger_payload(task: SubAgentTask, output_payload: dict[str, object]) -> dict[str, object]:
    """Summarize durable decisions and open questions for resume."""

    summary_delta = task.latest_status_report.summary_delta if task.latest_status_report else {}
    decisions = _unique_strings(
        [
            *sequence_strings(summary_delta.get("decisions_changed"), allow_scalar=True),
            *[
                item.claim
                for item in task.findings
                if item.status.upper() not in {"OPEN", "BLOCKED"}
            ],
        ]
    )
    open_questions = _unique_strings(
        [
            *sequence_strings(summary_delta.get("open_questions"), allow_scalar=True),
            *task.blockers,
            *sequence_strings(output_payload.get("blockers"), allow_scalar=True),
            *[_capability_gap_summary(gap) for gap in task.capability_gaps],
        ]
    )
    return {
        "run_id": task.id,
        "decisions": decisions,
        "open_questions": open_questions,
        "capability_request_ids": _unique_strings([req.id for req in task.capability_requests]),
        "capability_gap_ids": _unique_strings([gap.id for gap in task.capability_gaps]),
        "finding_ids": _unique_strings([item.id or item.claim for item in task.findings]),
        "updated_at": task.updated_at or task.created_at,
    }


def build_failing_tests_payload(task: SubAgentTask, output_payload: dict[str, object]) -> dict[str, object]:
    """Extract failing test facts from output.json without interpreting prose."""

    failing: list[dict[str, object]] = []
    for item in output_payload.get("tests", []) or []:
        if not isinstance(item, dict):
            continue
        if _test_passed(item):
            continue
        failing.append(
            {
                "name": str(item.get("name") or item.get("command") or item.get("id") or "unnamed"),
                "status": str(item.get("status") or item.get("result") or "failed"),
                "evidence_ref": str(item.get("evidence_ref") or item.get("path") or ""),
                "message": str(item.get("message") or item.get("summary") or ""),
            }
        )
    return {"run_id": task.id, "failing_tests": failing, "updated_at": task.updated_at or task.created_at}


def build_next_actions_payload(task: SubAgentTask, output_payload: dict[str, object]) -> dict[str, object]:
    """Collect the next recoverable actions for a parent or resume flow."""

    actions = _unique_strings(
        [
            *sequence_strings(output_payload.get("next_actions"), allow_scalar=True),
            output_payload.get("next_action"),
            task.latest_status_report.next_recommended_action if task.latest_status_report else "",
            *[f"resolve blocker: {item}" for item in task.blockers],
        ]
    )
    return {
        "run_id": task.id,
        "next_actions": actions,
        "blockers": _unique_strings(task.blockers),
        "updated_at": task.updated_at or task.created_at,
    }


def render_progress_markdown(task: SubAgentTask) -> str:
    """Render a small human-readable progress file for compact/resume."""

    blockers = "\n".join(f"- {item}" for item in task.blockers) or "- 暂无"
    evidence_refs = "\n".join(f"- {item}" for item in task.evidence_refs) or "- 暂无"
    artifact_refs = "\n".join(f"- {item}" for item in task.artifact_refs) or "- 暂无"
    return (
        "# PROGRESS\n\n"
        f"- run_id: {task.id}\n"
        f"- status: {task.status}\n"
        f"- verification_status: {task.verification_status}\n"
        f"- progress: {max(0.0, min(1.0, _float_value(task.progress)))}\n"
        f"- current_step: {task.current_step or task.status}\n"
        f"- checkpoint: {task.checkpoint_json}\n\n"
        "## Latest Summary\n\n"
        f"{task.latest_summary or '暂无'}\n\n"
        "## Blockers\n\n"
        f"{blockers}\n\n"
        "## Evidence Refs\n\n"
        f"{evidence_refs}\n\n"
        "## Artifact Refs\n\n"
        f"{artifact_refs}\n"
    )


def build_checkpoint_artifact_payloads(
    task: SubAgentTask,
    output_payload: dict[str, object],
) -> dict[str, object]:
    """Return all checkpoint artifact payloads keyed by target field name."""

    return {
        "checkpoint_json": build_checkpoint_payload(task, output_payload),
        "decision_ledger_json": build_decision_ledger_payload(task, output_payload),
        "failing_tests_json": build_failing_tests_payload(task, output_payload),
        "next_actions_json": build_next_actions_payload(task, output_payload),
        "progress_md": render_progress_markdown(task),
    }


def _float_value(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _unique_strings(value: object) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in sequence_strings(value, allow_scalar=True):
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _test_passed(item: dict[str, object]) -> bool:
    if "ok" in item:
        return bool(item.get("ok"))
    status = str(item.get("status") or item.get("result") or "").lower()
    return status in {"ok", "pass", "passed", "success", "succeeded"}


def _capability_gap_summary(gap: object) -> str:
    missing = str(getattr(gap, "missing_capability", "") or "")
    why_failed = str(getattr(gap, "why_failed", "") or "")
    if missing and why_failed:
        return f"{missing}: {why_failed}"
    return missing or why_failed
