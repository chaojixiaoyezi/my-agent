from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...contracts.gates.models import GateDecision, GateFinding
from ...contracts.recovery_actions import RecoveryAction

_SUCCESS_STATUSES = {"DONE", "COMPLETED", "COMPLETE", "SUCCESS", "SUCCEEDED", "ACCEPTED", "VERIFIED"}
_RESOLVED_STATUSES = {"CANCELLED", "ABANDONED", "TAKEN_OVER"}
_TERMINAL_FAILED_STATUSES = {"FAILED", "BLOCKED", "TIMEOUT", "CHANNEL_ERROR"}


def evaluate_subagent_aggregation_gate(closeout: object) -> GateDecision:
    task_root = _current_task_root(closeout)
    if task_root is None:
        return GateDecision.allow("subagent_aggregation", evidence={"checked": False, "reason": "task_root_missing"})
    children = _child_states(task_root)
    if not children:
        return _allowed_decision(task_root, children)
    unfinished = [item for item in children if _is_unfinished(item)]
    unresolved = [item for item in children if _is_unresolved_failure(item)]
    if unfinished or unresolved:
        return _warning_decision(task_root, children, unfinished, unresolved)
    return _allowed_decision(task_root, children)


def _allowed_decision(task_root: Path, children: list[dict[str, Any]]) -> GateDecision:
    return GateDecision.allow(
        "subagent_aggregation",
        evidence={
            "checked": True,
            "task_root": str(task_root),
            "child_count": len(children),
            **({"terminal_statuses": _status_counts(children)} if children else {}),
        },
    )


def _warning_decision(
    task_root: Path,
    children: list[dict[str, Any]],
    unfinished: list[dict[str, Any]],
    unresolved: list[dict[str, Any]],
) -> GateDecision:
    code = "SUBAGENTS_UNFINISHED" if unfinished else "SUBAGENTS_UNRESOLVED"
    return GateDecision(
        "subagent_aggregation",
        "ALLOW",
        True,
        (_warning_finding(code, unfinished, unresolved),),
        RecoveryAction.WAIT.value,
        evidence=_blocked_evidence(task_root, children, unfinished, unresolved),
    )


def _warning_finding(
    code: str,
    unfinished: list[dict[str, Any]],
    unresolved: list[dict[str, Any]],
) -> GateFinding:
    return GateFinding(
        code,
        "P1",
        message=(
            "当前任务还有子代理没有完成或没有被明确接管/取消；"
            "提交前建议先 inspect_agent_tree/read_child_result，等待完成，或用 cancel_subagents/takeover 明确处理后在最终报告中说明。"
        ),
        evidence={
            "unfinished_run_ids": _run_ids(unfinished),
            "unresolved_run_ids": _run_ids(unresolved),
        },
    )


def _blocked_evidence(
    task_root: Path,
    children: list[dict[str, Any]],
    unfinished: list[dict[str, Any]],
    unresolved: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "checked": True,
        "task_root": str(task_root),
        "child_count": len(children),
        "unfinished_children": _compact_children(unfinished),
        "unresolved_children": _compact_children(unresolved),
        "required_actions": [
            "inspect_agent_tree",
            "read_child_result_or_wait_for_done",
            "cancel_subagents_or_takeover_if_child_is_no_longer_needed",
            "merge_child_outputs_before_submit_for_acceptance",
        ],
    }


def append_subagent_rework_context(params: object, decision: GateDecision, report: dict[str, Any]) -> None:
    tool_context = getattr(params, "tool_context", None)
    if not isinstance(tool_context, list):
        return
    tool_context.append(
        "[subagent-aggregation-check]\n"
        + json.dumps(
            {
                "ok": False,
                "report_ref": report.get("report_ref", ""),
                "failed_gate": decision.to_dict(),
                "repair_guidance": {
                    "mode": "subagent_aggregation_rework",
                    "required_actions": list(decision.evidence.get("required_actions") or []),
                    "message_zh": (
                        "提交前建议查看当前任务子代理状态，读取已完成子代理结果；"
                        "仍在运行的继续等待或补充引导，确定不需要的用 cancel_subagents 明确取消，"
                        "然后把所有已完成/已处理的子代理结果合并进最终报告；如直接接管或跳过，应在最终报告里说明原因。"
                    ),
                    "submit_when_ready": "submit_for_acceptance",
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )


def _current_task_root(closeout: object) -> Path | None:
    params = getattr(closeout, "params", None)
    attrs = getattr(params, "task_attributes", None)
    if root := _task_root_from_attrs(attrs):
        return root
    workspace = getattr(getattr(closeout, "agent", None), "_current_run_task_workspace", None)
    root = getattr(workspace, "task_root", None) or getattr(workspace, "root", None)
    return Path(root).expanduser().resolve(strict=False) if root else None


def _task_root_from_attrs(attrs: object) -> Path | None:
    workspace = attrs.get("run_workspace") if isinstance(attrs, dict) else None
    if not isinstance(workspace, dict):
        return None
    text = str(workspace.get("task_root") or "").strip()
    return Path(text).expanduser().resolve(strict=False) if text else None


def _child_states(task_root: Path) -> list[dict[str, Any]]:
    agents_dir = task_root / "work" / "agents"
    if not agents_dir.exists():
        return []
    children: list[dict[str, Any]] = []
    for path in sorted(agents_dir.glob("*/canonical_state.json")):
        payload = _read_json(path)
        if not payload:
            children.append(
                {
                    "run_id": path.parent.name,
                    "status": "STATE_UNREADABLE",
                    "canonical_state_ref": str(path),
                }
            )
            continue
        payload.setdefault("run_id", payload.get("id") or path.parent.name)
        payload.setdefault("canonical_state_ref", str(path))
        children.append(payload)
    return children


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _is_unfinished(item: dict[str, Any]) -> bool:
    status = _status(item)
    return status not in _SUCCESS_STATUSES | _RESOLVED_STATUSES | _TERMINAL_FAILED_STATUSES


def _is_unresolved_failure(item: dict[str, Any]) -> bool:
    return _status(item) in _TERMINAL_FAILED_STATUSES


def _status(item: dict[str, Any]) -> str:
    return str(item.get("status") or "").strip().upper() or "UNKNOWN"


def _compact_children(children: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in children[:20]:
        rows.append(
            {
                "run_id": str(item.get("run_id") or item.get("id") or ""),
                "status": _status(item),
                "progress": item.get("progress"),
                "channel_status": str(item.get("channel_status") or ""),
                "summary": str(item.get("latest_summary") or "")[:240],
                "canonical_state_ref": str(item.get("canonical_state_ref") or ""),
            }
        )
    return rows


def _run_ids(children: list[dict[str, Any]]) -> list[str]:
    return [str(item.get("run_id") or "") for item in children]


def _status_counts(children: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in children:
        status = _status(item)
        counts[status] = counts.get(status, 0) + 1
    return counts


__all__ = ["append_subagent_rework_context", "evaluate_subagent_aggregation_gate"]
