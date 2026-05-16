# LLM: Patch acceptance findings keep patch protocol checks separate from artifact existence checks.
# 模块用途: 生成 patch 状态、未处理 patch 和 applied patch 审核情况的验收 finding。

from __future__ import annotations

from ..reports import AcceptanceReviewFinding


# LLM: patch_findings validates runner patch records without touching files.
# 函数用途: 根据 output.json 的 patches 字段生成验收 finding，确保 planned/blocked 未被误当成完成、applied patch 已审核。
def patch_findings(
    patches: list[dict[str, object]],
    *,
    evidence_path: str,
    created_at: float,
) -> list[AcceptanceReviewFinding]:
    unresolved = _patches_with_status(patches, {"planned", "blocked"})
    invalid = _invalid_patch_statuses(patches)
    unreviewed = _unreviewed_applied_patches(patches)
    return [
        AcceptanceReviewFinding(
            name="no_unresolved_patches",
            ok=not unresolved,
            severity="P1",
            message="没有未处理 patch。" if not unresolved else f"仍有 {len(unresolved)} 个 patch 处于 planned/blocked。",
            evidence_path=evidence_path,
            created_at=created_at,
        ),
        AcceptanceReviewFinding(
            name="patch_status_valid",
            ok=not invalid,
            severity="P1",
            message="patch 状态均符合协议。" if not invalid else f"存在 {len(invalid)} 个未知 patch 状态。",
            evidence_path=evidence_path,
            created_at=created_at,
        ),
        AcceptanceReviewFinding(
            name="patches_reviewed",
            ok=not unreviewed,
            severity="P1",
            message="所有 applied patch 已审核。" if not unreviewed else f"仍有 {len(unreviewed)} 个 applied patch 未通过审核。",
            evidence_path=evidence_path,
            created_at=created_at,
        ),
    ]


# LLM: _patches_with_status selects compact patch records by lower-cased status.
# 函数用途: 查找处于 planned/blocked 等状态的 patch 记录，供验收 finding 计数使用。
def _patches_with_status(patches: list[dict[str, object]], statuses: set[str]) -> list[dict[str, object]]:
    return [item for item in patches if str(item.get("status", "")).lower() in statuses]


# LLM: _invalid_patch_statuses protects the patch protocol from arbitrary status text.
# 函数用途: 找出不属于 applied/planned/blocked 的 patch 状态，避免模型自然语言状态绕过验收。
def _invalid_patch_statuses(patches: list[dict[str, object]]) -> list[dict[str, object]]:
    valid = {"applied", "planned", "blocked"}
    return [item for item in patches if str(item.get("status", "")).lower() not in valid]


# LLM: _unreviewed_applied_patches keeps applied code changes behind an approval signal.
# 函数用途: 找出已标记 applied 但 review_status 不是 APPROVED 的 patch，提醒父级继续审核。
def _unreviewed_applied_patches(patches: list[dict[str, object]]) -> list[dict[str, object]]:
    return [
        item
        for item in patches
        if str(item.get("status", "")).lower() == "applied"
        and str(item.get("review_status", "")).upper() != "APPROVED"
    ]
