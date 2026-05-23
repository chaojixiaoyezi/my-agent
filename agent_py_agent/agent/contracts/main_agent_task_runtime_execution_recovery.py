# LLM: Execution recovery helpers derive retry budgets from structured closeout progress.
# 模块用途: 给 task/real_task 执行入口复用恢复 attempt 的结构化基线和检查窗口。

from __future__ import annotations

import json
from pathlib import Path

_DIRECT_REPAIR_ACTIONS = {
    "invoke_builder_tool",
    "repair_evidence_refs",
    "repair_structured_checkpoint_json",
    "write_non_empty_structured_rows",
}


# LLM: recovery_attempt_inspection_budget is derived from structured closeout actions.
# 函数用途: 直接写入/构建类恢复不给额外读取窗口；定位缺失产物时才保留小检查预算。
def recovery_attempt_inspection_budget(task_workspace: Path) -> int:
    progress = _closeout_progress(task_workspace)
    actions = progress.get("recovery_actions") if isinstance(progress, dict) else None
    action_rows = [item for item in actions if isinstance(item, dict)] if isinstance(actions, list) else []
    if any(_requires_direct_repair(item, task_workspace) for item in action_rows):
        return 0
    return 2 if action_rows else 4


# LLM: recovery_attempt_baseline keeps resume guards grounded in structured fields.
# 函数用途: 提取 closeout failure fingerprint，供下一轮检测是否仍无进展。
def recovery_attempt_baseline(task_workspace: Path) -> dict[str, object]:
    progress_payload = _closeout_progress(task_workspace)
    return {
        "baseline_failure_fingerprint": str(progress_payload.get("failure_fingerprint") or ""),
        "baseline_unchanged_failure_count": _safe_int(progress_payload.get("unchanged_failure_count")),
    }


def _closeout_progress(task_workspace: Path) -> dict[str, object]:
    closeout = task_workspace / ".agent_delivery" / "closeout.json"
    try:
        payload = json.loads(closeout.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    progress = payload.get("delivery_progress") if isinstance(payload, dict) else {}
    return progress if isinstance(progress, dict) else {}


def _requires_direct_repair(action: dict[str, object], task_workspace: Path) -> bool:
    recommended = str(action.get("recommended_action") or "").strip()
    if recommended in _DIRECT_REPAIR_ACTIONS:
        return True
    if recommended != "repair_artifact_against_findings":
        return False
    if _artifact_missing_only(action):
        return False
    return _action_target_exists(action, task_workspace)


def _artifact_missing_only(action: dict[str, object]) -> bool:
    codes = action.get("finding_codes")
    values = {str(code) for code in codes if str(code)} if isinstance(codes, list) else set()
    return bool(values) and values.issubset({"ARTIFACT_MISSING"})


def _action_target_exists(action: dict[str, object], task_workspace: Path) -> bool:
    for key in ("artifact_path", "checkpoint_ref", "output_ref", "source_ref"):
        ref = str(action.get(key) or "").strip()
        if ref and _resolve_action_ref(ref, task_workspace).exists():
            return True
    return False


def _resolve_action_ref(ref: str, task_workspace: Path) -> Path:
    path = Path(ref).expanduser()
    return path if path.is_absolute() else task_workspace / path


def _safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
