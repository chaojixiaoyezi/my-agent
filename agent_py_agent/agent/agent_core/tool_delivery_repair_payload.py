# LLM: delivery repair payload extraction keeps closeout parsing outside the guard decision loop.
# 模块用途: 从 closeout.json 的结构化字段生成 staged-delivery 修复 payload。

from __future__ import annotations

import json
from pathlib import Path

from .tool_delivery_repair_attempt import strict_write_required
from .tool_delivery_repair_paths import repair_target_snapshots
from .tool_delivery_repair_required_calls import required_tool_calls
from .tool_delivery_repair_scope import report_matches_current_contract

_WRITE_FIRST_ACTIONS = {
    "invoke_builder_tool",
    "materialize_checkpoint",
    "repair_artifact_against_findings",
    "repair_collection_item_values",
    "repair_evidence_refs",
    "repair_structured_checkpoint_json",
    "write_non_empty_structured_rows",
}
_LIST_FIELDS = {
    "finding_codes",
    "finding_values",
    "collection_item_updates",
    "repair_targets",
    "required_columns",
    "required_fields",
    "write_tools",
}
_DICT_FIELDS = {
    "collection_contract",
}
_TEXT_FIELDS = (
    "artifact_id",
    "artifact_path",
    "builder_tool",
    "category",
    "checkpoint_materialization_mode",
    "checkpoint_ref",
    "checkpoint_shape_hint",
    "code",
    "evidence_shape_hint",
    "missing_columns",
    "groups_path",
    "items_path",
    "output_ref",
    "recommended_action",
    "source_ref",
    "writer_tool",
)


# LLM: delivery_repair_payload extracts only active write-first recovery actions from closeout.json.
# 函数用途: 读取 closeout 报告并筛选写入/构建优先级恢复动作；不读取自然语言日志。
def delivery_repair_payload(
    agent: object,
    current_contract: object | None = None,
    *,
    enforce_contract_scope: bool = False,
) -> dict[str, object]:
    report = _closeout_report(agent)
    if not report or report.get("ok") is True:
        return {}
    if enforce_contract_scope and not report_matches_current_contract(report, current_contract):
        return {}
    progress = report.get("delivery_progress")
    agent_root = Path(getattr(agent, "root", ".")).resolve()
    actions = _required_actions(progress)
    actions = _merge_required_actions(actions, _refreshed_required_actions(report, current_contract, agent_root))
    if not isinstance(progress, dict) or not actions:
        return {}
    pending_targets = progress.get("pending_materialization_targets")
    return {
        "pending_materialization_targets": pending_targets if isinstance(pending_targets, list) else [],
        "report_ref": str(report.get("report_ref") or ""),
        "required_actions": actions,
        "repair_target_snapshots": repair_target_snapshots(actions, agent_root),
        "required_tool_calls": required_tool_calls(actions),
        "strict_write_required": strict_write_required(progress, agent_root=agent_root),
    }


# LLM: _refreshed_required_actions derives current executable repairs from the active delivery contract.
# 函数用途: 旧 closeout 的 recovery_actions 可能缺少新门补出的 builder/action；用当前合同重新推导一次并合并。
def _refreshed_required_actions(
    report: dict[str, object],
    current_contract: object | None,
    agent_root: Path,
) -> list[dict[str, object]]:
    if not isinstance(current_contract, dict) or not current_contract:
        return []
    try:
        from .main_agent_delivery_closeout_recovery import _recovery_actions

        refreshed = _recovery_actions(report, contract=current_contract, workspace_root=agent_root)
    except Exception:
        return []
    return _required_actions({"recovery_actions": refreshed})


# LLM: _merge_required_actions preserves persisted repair actions while adding fresher contract-derived actions.
# 函数用途: 按结构化 action identity 去重合并，不用自然语言提示判断哪个动作重要。
def _merge_required_actions(
    existing: list[dict[str, object]],
    refreshed: list[dict[str, object]],
) -> list[dict[str, object]]:
    merged: list[dict[str, object]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for action in [*existing, *refreshed]:
        identity = _action_identity(action)
        if identity in seen:
            continue
        seen.add(identity)
        merged.append(action)
    return merged


# LLM: _action_identity uses stable machine fields to dedupe delivery repairs.
# 函数用途: 把同类 checkpoint/output/artifact 恢复动作折叠成一个，避免重复 required_tool_calls。
def _action_identity(action: dict[str, object]) -> tuple[str, str, str, str]:
    return (
        str(action.get("recommended_action") or ""),
        str(action.get("checkpoint_ref") or ""),
        str(action.get("output_ref") or ""),
        str(action.get("artifact_path") or ""),
    )


# LLM: _required_actions 是 agent_py_agent/agent/agent_core/tool_delivery_repair_payload.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 required actions 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _required_actions(progress: object) -> list[dict[str, object]]:
    if not isinstance(progress, dict):
        return []
    actions = progress.get("recovery_actions")
    if not isinstance(actions, list):
        return []
    return [
        normalized
        for item in actions
        for normalized in [_required_action(item)]
        if normalized
    ]


# LLM: _required_action 是 agent_py_agent/agent/agent_core/tool_delivery_repair_payload.py 的结构化 helper；修改时保持不读取普通自然语言作为机器事实。
# 函数用途: 处理 required action 相关的结构化数据、路径或 finding，供当前合同链路调用。
def _required_action(item: object) -> dict[str, object]:
    if not isinstance(item, dict):
        return {}
    if str(item.get("recommended_action") or "").strip() not in _WRITE_FIRST_ACTIONS:
        return {}
    payload = {field: str(item.get(field) or "") for field in _TEXT_FIELDS}
    payload.update({field: item.get(field) if isinstance(item.get(field), list) else [] for field in _LIST_FIELDS})
    payload.update({field: item.get(field) if isinstance(item.get(field), dict) else {} for field in _DICT_FIELDS})
    payload["required_sheets_min"] = item.get("required_sheets_min") or 0
    payload["retryable"] = bool(item.get("retryable", True))
    return payload


# LLM: _closeout_report keeps the repair guard grounded in the same machine report that closeout writes.
# 函数用途: 读取当前工作区 .agent_delivery/closeout.json；不存在或损坏时返回空对象。
def _closeout_report(agent: object) -> dict[str, object]:
    path = Path(getattr(agent, "root", ".")).resolve() / ".agent_delivery" / "closeout.json"
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}

__all__ = ["delivery_repair_payload"]
