
from __future__ import annotations

from .due_models import DueInspectionContext, DueIssueSpec, _single_issue


def check_parent_timeout_child_issues(ctx: DueInspectionContext):
    """Check for unfinished children after a parent has timed out."""
    task = ctx.task
    if str(task.status or "").upper() != "TIMEOUT" or not getattr(task, "child_ids", None):
        return []
    unfinished = _unfinished_child_refs(ctx)
    if not unfinished:
        return []
    return [
        _single_issue(
            ctx,
            DueIssueSpec(
                "P1",
                "parent_timeout_with_unfinished_children",
                (
                    "父节点已经 TIMEOUT，但仍有未完成子任务需要重新指定领导者或验收入口："
                    f"{_child_ref_summary(unfinished)}。"
                ),
                "recover_child_after_parent_timeout",
                related_refs=[f"unfinished_child:{ref}" for ref in unfinished],
            ),
        )
    ]


def _unfinished_child_refs(ctx: DueInspectionContext) -> list[str]:
    task_index = ctx.task_index or {}
    refs: list[str] = []
    for child_id in getattr(ctx.task, "child_ids", []) or []:
        child = task_index.get(child_id)
        if child is None:
            refs.append(f"{child_id}:MISSING")
            continue
        if _child_needs_parent_recovery(child):
            refs.append(f"{child.id}:{str(child.status or '').upper()}")
    return refs


def _child_needs_parent_recovery(child) -> bool:
    status = str(getattr(child, "status", "") or "").upper()
    verification = str(getattr(child, "verification_status", "") or "").upper()
    if status == "DONE" and verification == "VERIFIED":
        return False
    return status not in {"TAKEN_OVER", "ABANDONED"}


def _child_ref_summary(refs: list[str]) -> str:
    head = refs[:5]
    suffix = "" if len(refs) <= 5 else f" 等 {len(refs)} 个"
    return ", ".join(head) + suffix
