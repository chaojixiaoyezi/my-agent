from __future__ import annotations

from pathlib import Path
from typing import Any

from ...contracts.gates.models import GateDecision, GateFinding
from ...contracts.recovery_actions import RecoveryAction
from ...task_progress import progress_path, read_task_progress, task_progress_summary

_CLOSED_STATUSES = {"done", "skipped"}


def evaluate_task_progress_closeout_gate(closeout: object, report: dict[str, Any] | None = None) -> GateDecision:
    del report
    root = _progress_root(closeout)
    run_id = _run_id(closeout)
    if not root or not run_id:
        return _unchecked_progress_decision("progress_scope_missing")
    path = progress_path(root, run_id)
    if not path.exists():
        return _unchecked_progress_decision("progress_file_missing", run_id=run_id)
    progress = read_task_progress(root, run_id)
    summary = task_progress_summary({**progress, "ref": str(path)})
    open_items = _open_items(progress)
    if open_items:
        return _open_progress_decision(open_items, run_id, path, summary)
    return _closed_progress_decision(run_id, path, summary, progress)


def task_progress_repair_message(report: dict[str, Any]) -> str:
    payload = report.get("task_progress_closeout_gate")
    if not isinstance(payload, dict) or payload.get("allowed") is True:
        return ""
    message = str(payload.get("model_message") or "").strip()
    if message:
        return message
    findings = payload.get("findings")
    if isinstance(findings, list):
        for finding in findings:
            if isinstance(finding, dict):
                text = str(finding.get("message") or "").strip()
                if text:
                    return text
    return ""


def _unchecked_progress_decision(reason: str, run_id: str = "") -> GateDecision:
    evidence: dict[str, object] = {"checked": False, "reason": reason}
    if run_id:
        evidence["run_id"] = run_id
    return GateDecision.allow("task_progress_closeout", evidence=evidence)


def _closed_progress_decision(
    run_id: str,
    path: Path,
    summary: dict[str, Any],
    progress: dict[str, Any],
) -> GateDecision:
    advisory_findings = _advisory_findings(progress)
    return GateDecision(
        "task_progress_closeout",
        "ALLOW",
        True,
        tuple(advisory_findings),
        RecoveryAction.CONTINUE.value,
        {
            "checked": True,
            "run_id": run_id,
            "progress_ref": str(path),
            "counts": summary.get("counts", {}),
            "advisory_finding_codes": [finding.code for finding in advisory_findings],
        },
    )


def _open_progress_decision(
    open_items: list[dict[str, Any]],
    run_id: str,
    path: Path,
    summary: dict[str, Any],
) -> GateDecision:
    next_action = str(summary.get("next_action") or "").strip()
    finding = GateFinding(
        "TASK_PROGRESS_OPEN_ITEMS",
        "soft",
        message=_open_items_repair_message(open_items, next_action),
        evidence={
            "run_id": run_id,
            "progress_ref": str(path),
            "open_count": len(open_items),
            "open_items": [_compact_item(item) for item in open_items[:12]],
            "counts": summary.get("counts", {}),
            "summary": summary.get("summary", ""),
            "next_action": next_action,
        },
    )
    return GateDecision(
        "task_progress_closeout",
        "ALLOW",
        True,
        (finding,),
        recommended_action=RecoveryAction.REPAIR.value,
        evidence={
            "checked": True,
            "run_id": run_id,
            "progress_ref": str(path),
            "open_count": len(open_items),
            "counts": summary.get("counts", {}),
            "summary": summary.get("summary", ""),
            "next_action": next_action,
            "required_actions": [
                "continue_open_task_progress_items",
                "read_or_finish_remaining_sources",
                "submit_for_acceptance_after_open_items_are_done_or_skipped",
            ],
        },
    )


def _progress_root(closeout: object) -> Path | None:
    agent = getattr(closeout, "agent", None)
    home_paths = getattr(agent, "home_paths", None)
    owner_home = getattr(home_paths, "owner_home_dir", None)
    if owner_home:
        return Path(owner_home).expanduser().resolve(strict=False)
    root = getattr(agent, "root", None)
    return Path(root).expanduser().resolve(strict=False) if root else None


def _run_id(closeout: object) -> str:
    params = getattr(closeout, "params", None)
    for value in (
        getattr(params, "run_id", ""),
        getattr(getattr(closeout, "agent", None), "_main_agent_run_id", ""),
    ):
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _open_items(progress: dict[str, Any]) -> list[dict[str, Any]]:
    items = progress.get("items") if isinstance(progress, dict) else []
    if not isinstance(items, list):
        return []
    return [
        item
        for item in items
        if isinstance(item, dict) and _status_text(item.get("status") or "pending") not in _CLOSED_STATUSES
    ]


def _advisory_findings(progress: dict[str, Any]) -> list[GateFinding]:
    return [
        *_empty_done_advisory_findings([item for item in progress.get("items", []) if isinstance(item, dict)]),
        *_coverage_incomplete_findings(progress),
    ]


def _empty_done_advisory_findings(items: list[dict[str, Any]]) -> list[GateFinding]:
    empty_done = [
        item
        for item in items
        if _status_text(item.get("status")) == "done"
        and not str(item.get("notes") or "").strip()
        and not str(item.get("result") or item.get("outcome") or item.get("conclusion") or "").strip()
        and not _list(item.get("evidence"))
    ]
    if not empty_done:
        return []
    return [
        GateFinding(
            "TASK_PROGRESS_DONE_WITHOUT_EVIDENCE",
            "soft",
            message=(
                f"进度账本里有 {len(empty_done)} 个 done 项没有事实或证据；"
                "建议补上读到的关键值、来源行；这是软提醒，不阻断验收。"
            ),
            evidence={
                "item_ids": [_item_id(item) for item in empty_done[:20]],
                "empty_done_count": len(empty_done),
            },
        )
    ]


def _coverage_incomplete_findings(progress: dict[str, Any]) -> list[GateFinding]:
    coverage = progress.get("coverage")
    counts = coverage.get("counts") if isinstance(coverage, dict) else {}
    if not isinstance(counts, dict):
        return []
    incomplete_targets = int(counts.get("targets_incomplete") or 0)
    incomplete_checks = int(counts.get("checks_incomplete") or 0)
    if incomplete_targets <= 0 and incomplete_checks <= 0:
        return []
    active = []
    for target in coverage.get("targets", []) if isinstance(coverage, dict) else []:
        if not isinstance(target, dict):
            continue
        target_counts = dict(target.get("checks") or {})
        checks_open = [name for name, status in target_counts.items() if _status_text(status) not in _CLOSED_STATUSES]
        if checks_open or _status_text(target.get("status") or "pending") not in _CLOSED_STATUSES:
            active.append(
                {
                    "id": str(target.get("id") or target.get("title") or ""),
                    "checks_open": checks_open[:8],
                    "next": str(target.get("next") or ""),
                }
            )
    return [
        GateFinding(
            "TASK_PROGRESS_COVERAGE_INCOMPLETE",
            "soft",
            message="覆盖账本还有未完成对象或字段；这是软提醒，建议继续补齐 checks/evidence。",
            evidence={
                "targets_incomplete": incomplete_targets,
                "checks_incomplete": incomplete_checks,
                "active_targets": active[:12],
            },
        )
    ]


def _status_text(value: object) -> str:
    return str(value or "").strip().lower()


def _compact_item(item: dict[str, Any]) -> dict[str, str]:
    return {
        "id": str(item.get("id") or ""),
        "title": str(item.get("title") or ""),
        "status": str(item.get("status") or ""),
        "next": str(item.get("next") or ""),
    }


def _item_id(item: dict[str, Any]) -> str:
    return str(item.get("id") or item.get("title") or "").strip()


def _list(value: object) -> list:
    return list(value) if isinstance(value, list | tuple) else []


def _open_items_repair_message(open_items: list[dict[str, Any]], next_action: str) -> str:
    first = str(open_items[0].get("id") or open_items[0].get("title") or "").strip() if open_items else ""
    suffix = f"；下一步：{next_action}" if next_action else f"；先继续处理 {first}" if first else ""
    return f"进度账本还有 {len(open_items)} 个未完成项，建议提交前处理或在最终说明里解释{suffix}。"


__all__ = ["evaluate_task_progress_closeout_gate", "task_progress_repair_message"]
