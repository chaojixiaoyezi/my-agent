# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""Build findings for patches.

新手说明:
检查 patch 的状态（planned/blocked/applied）和审核情况。
"""

from ..models import SubAgentTask
from ..parsing import _dict_list
from ..reports import AcceptanceReviewFinding
from .evidence import _make_finding


# LLM: _classify_patches 属于子代理验收证据的函数边界；调整时先确认验收证据、补丁摘要和就绪判断仍按原契约工作。
# 函数用途: 处理classifypatches相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持验收证据、补丁摘要和就绪判断上的返回值和副作用边界稳定。
def _classify_patches(
    patches: list[dict],
) -> tuple[list[dict], list[dict], list[dict]]:
    """Classify patches into unresolved, invalid, and unreviewed lists."""
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
    return unresolved_patches, invalid_patches, unreviewed_applied_patches


# LLM: _build_patch_findings 属于子代理验收证据的函数边界；调整时先确认验收证据、补丁摘要和就绪判断仍按原契约工作。
# 函数用途: 构建补丁findings所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _build_patch_findings(
    task: SubAgentTask,
    output: dict[str, object],
    created_at: float,
) -> list[AcceptanceReviewFinding]:
    """Build findings for patches."""
    patches = _dict_list(output.get("patches", []))
    unresolved_patches, invalid_patches, unreviewed_applied_patches = _classify_patches(patches)
    return [
        _make_finding(
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
        ),
        _make_finding(
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
        ),
        _make_finding(
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
        ),
    ]
