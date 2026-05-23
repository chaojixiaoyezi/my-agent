# LLM: Recovery-attempt budget logic is isolated from delivery repair classification.
# 模块用途: 读取结构化 recovery_attempt marker，决定自动恢复阶段是否还允许少量检查工具。

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


# LLM: strict_write_required upgrades the guard once unchanged failures have reached the no-progress threshold.
# 函数用途: 根据 unchanged_failure_count 和 no_progress_block_threshold 判断是否必须切到严格写入模式。
def strict_write_required(progress: dict[str, object], *, agent_root: Path) -> bool:
    marker_payload = _recovery_attempt_marker(agent_root)
    if _marker_requires_immediate_write(marker_payload):
        return True
    if _within_recovery_attempt_inspection_budget(progress, agent_root=agent_root):
        return False
    unchanged = _safe_int(progress.get("unchanged_failure_count"))
    threshold = _safe_int(progress.get("no_progress_block_threshold"))
    return threshold > 0 and unchanged >= threshold


# LLM: A recovery attempt marker gives one fresh attempt an inspection window before strict repair resumes.
# 函数用途: 根据恢复 marker 的基线和预算判断是否还允许少量读取现状，而不继承旧失败债务。
def _within_recovery_attempt_inspection_budget(progress: dict[str, object], *, agent_root: Path) -> bool:
    marker_payload = _recovery_attempt_marker(agent_root)
    if marker_payload:
        return _progress_delta_within_budget(progress, marker_payload)
    return _fresh_recovery_attempt_by_mtime(agent_root)


# LLM: _recovery_attempt_marker keeps this runtime helper grounded in structured fields.
# 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
def _recovery_attempt_marker(agent_root: Path) -> dict[str, object]:
    marker = agent_root / ".agent_delivery" / "recovery_attempt.json"
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict) or value.get("schema_version") != "delivery-recovery-attempt.v1":
        return {}
    return value


# LLM: _closeout_progress reads only persisted delivery progress facts.
# 函数用途: 从 closeout.json 提取 delivery_progress，坏文件或缺字段按空进度处理。
def _closeout_progress(task_workspace: Path) -> dict[str, object]:
    closeout = task_workspace / ".agent_delivery" / "closeout.json"
    try:
        payload = json.loads(closeout.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    progress = payload.get("delivery_progress") if isinstance(payload, dict) else {}
    return progress if isinstance(progress, dict) else {}


# LLM: _requires_direct_repair distinguishes write-first recovery from lookup recovery.
# 函数用途: 根据结构化 recommended_action 和目标状态判断是否应立即进入写入模式。
def _requires_direct_repair(action: dict[str, object], task_workspace: Path) -> bool:
    if _requires_source_evidence_lookup(action):
        return False
    recommended = str(action.get("recommended_action") or "").strip()
    if recommended == "repair_artifact_against_findings":
        if _artifact_missing_only(action):
            return False
        return _action_target_exists(action, task_workspace)
    if recommended in _DIRECT_REPAIR_ACTIONS:
        return _has_direct_repair_target(action)
    return False


# LLM: _requires_source_evidence_lookup preserves lookup budget for evidence-first checkpoints.
# 函数用途: 判断恢复动作是否需要先抓取来源证据，而不是立刻写最终产物。
def _requires_source_evidence_lookup(action: dict[str, object]) -> bool:
    if str(action.get("checkpoint_materialization_mode") or "").strip() == "source_evidence_first":
        return True
    if bool(action.get("requires_auditable_source_evidence")):
        return True
    fields = action.get("required_structured_fields")
    if isinstance(fields, list) and {"source_refs", "claims"}.issubset({str(item) for item in fields}):
        return True
    codes = (
        {str(code) for code in action.get("finding_codes", []) if str(code)}
        if isinstance(action.get("finding_codes"), list)
        else set()
    )
    code = str(action.get("code") or "").strip()
    category = str(action.get("category") or "").strip()
    return category == "evidence" or code == "COLLECTION_SOURCE_MISSING" or "COLLECTION_SOURCE_MISSING" in codes


# LLM: _has_direct_repair_target requires a concrete machine target before strict repair.
# 函数用途: 确认 builder/output 或 checkpoint/writer 字段齐全，避免空目标误触发写入模式。
def _has_direct_repair_target(action: dict[str, object]) -> bool:
    recommended = str(action.get("recommended_action") or "").strip()
    if recommended == "invoke_builder_tool":
        return bool(str(action.get("builder_tool") or "").strip() and str(action.get("output_ref") or "").strip())
    if recommended in {
        "materialize_checkpoint",
        "repair_collection_item_values",
        "repair_evidence_refs",
        "repair_structured_checkpoint_json",
        "write_non_empty_structured_rows",
    }:
        return bool(str(action.get("checkpoint_ref") or "").strip() and _declares_writer_or_update(action))
    return False


# LLM: _declares_writer_or_update checks whether a recovery action can mutate a target.
# 函数用途: 读取 writer_tool、write_tools 或 collection_item_updates 机器字段作为写入能力声明。
def _declares_writer_or_update(action: dict[str, object]) -> bool:
    if str(action.get("writer_tool") or "").strip():
        return True
    write_tools = action.get("write_tools")
    if isinstance(write_tools, list) and any(str(item).strip() for item in write_tools):
        return True
    updates = action.get("collection_item_updates")
    return isinstance(updates, list) and bool(updates)


# LLM: _artifact_missing_only keeps missing-file recovery in lookup mode.
# 函数用途: 判断 finding_codes 是否只有 ARTIFACT_MISSING，以便先允许少量定位检查。
def _artifact_missing_only(action: dict[str, object]) -> bool:
    codes = action.get("finding_codes")
    values = {str(code) for code in codes if str(code)} if isinstance(codes, list) else set()
    return bool(values) and values.issubset({"ARTIFACT_MISSING"})


# LLM: _action_target_exists checks declared repair refs inside the task workspace.
# 函数用途: 根据 artifact/checkpoint/output/source 结构化引用判断目标是否已经存在。
def _action_target_exists(action: dict[str, object], task_workspace: Path) -> bool:
    for key in ("artifact_path", "checkpoint_ref", "output_ref", "source_ref"):
        ref = str(action.get(key) or "").strip()
        if ref and _resolve_action_ref(ref, task_workspace).exists():
            return True
    return False


# LLM: _resolve_action_ref resolves recovery refs without reading prompt text.
# 函数用途: 绝对路径原样返回，相对路径绑定到 task_workspace 下。
def _resolve_action_ref(ref: str, task_workspace: Path) -> Path:
    path = Path(ref).expanduser()
    return path if path.is_absolute() else task_workspace / path


# LLM: marker budget zero means the recovery packet already points to a direct mutation.
# 函数用途: 让结构化 attempt marker 可以立即切到写入模式，不等 unchanged_failure_count 达阈值。
def _marker_requires_immediate_write(marker: dict[str, object]) -> bool:
    return bool(marker) and _safe_int(marker.get("inspection_round_budget")) <= 0


# LLM: _progress_delta_within_budget keeps this runtime helper grounded in structured fields.
# 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
def _progress_delta_within_budget(progress: dict[str, object], marker: dict[str, object]) -> bool:
    unchanged = _safe_int(progress.get("unchanged_failure_count"))
    baseline = _safe_int(marker.get("baseline_unchanged_failure_count"))
    budget = _safe_int(marker.get("inspection_round_budget"))
    return budget > 0 and unchanged <= baseline + budget


# LLM: _fresh_recovery_attempt_by_mtime keeps this runtime helper grounded in structured fields.
# 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
def _fresh_recovery_attempt_by_mtime(agent_root: Path) -> bool:
    closeout = agent_root / ".agent_delivery" / "closeout.json"
    marker = agent_root / ".agent_delivery" / "recovery_attempt.json"
    try:
        return marker.stat().st_mtime > closeout.stat().st_mtime
    except OSError:
        return False


# LLM: _safe_int keeps this runtime helper grounded in structured fields.
# 函数用途: 处理当前模块的结构化数据流，不把普通自然语言文本当作系统事实来源。
def _safe_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
