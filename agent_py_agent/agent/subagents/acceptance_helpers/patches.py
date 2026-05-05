from __future__ import annotations

"""Build findings for patches.

新手说明:
检查 patch 的状态（planned/blocked/applied）和审核情况。
"""

from ..models import SubAgentTask
from ..parsing import _dict_list
from ..reports import AcceptanceReviewFinding


def _build_patch_findings(
    task: SubAgentTask,
    output: dict[str, object],
    created_at: float,
) -> list[AcceptanceReviewFinding]:
    """Build findings for patches."""
    findings: list[AcceptanceReviewFinding] = []

    patches = _dict_list(output.get("patches", []))
    valid_patch_statuses = {"applied", "planned", "blocked"}
    unresolved_patches = [
        item for item in patches if str(item.get("status", "")).lower() in {"planned", "blocked"}
    ]
    invalid_patches = [
        item
        for item in patches
        if str(item.get("status", "")).lower() not in valid_patch_statuses
    ]
    unreviewed_applied_patches = [
        item
        for item in patches
        if str(item.get("status", "")).lower() == "applied"
        and str(item.get("review_status", "")).upper() != "APPROVED"
    ]
    findings.append(
        AcceptanceReviewFinding(
            name="no_unresolved_patches",
            ok=not unresolved_patches,
            severity="P1",
            message=(
                "没有未处理 patch。"
                if not unresolved_patches
                else f"仍有 {len(unresolved_patches)} 个 patch 处于 planned/blocked。"
            ),
            evidence_path=task.output_json,
            created_at=created_at,
        )
    )
    findings.append(
        AcceptanceReviewFinding(
            name="patch_status_valid",
            ok=not invalid_patches,
            severity="P1",
            message=(
                "patch 状态均符合协议。"
                if not invalid_patches
                else f"存在 {len(invalid_patches)} 个未知 patch 状态。"
            ),
            evidence_path=task.output_json,
            created_at=created_at,
        )
    )
    findings.append(
        AcceptanceReviewFinding(
            name="patches_reviewed",
            ok=not unreviewed_applied_patches,
            severity="P1",
            message=(
                "所有 applied patch 已审核。"
                if not unreviewed_applied_patches
                else f"仍有 {len(unreviewed_applied_patches)} 个 applied patch 未通过审核。"
            ),
            evidence_path=task.output_json,
            created_at=created_at,
        )
    )
    return findings
