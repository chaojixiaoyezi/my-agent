# LLM: CLI acceptance-progress helpers render parent acceptance projections without executing actions.
# 模块用途:给 status/subagents CLI 展示父级验收 plan 和 next-action 摘要；只处理 refs，不读取 artifact 正文。
from __future__ import annotations

"""Parent acceptance progress helpers for CLI status surfaces."""

from typing import Any

_ACCEPTANCE_PLAN_STATUSES = {"AWAITING_ACCEPTANCE", "FAILED", "BLOCKED", "ERROR", "TIMEOUT"}


# LLM: format_acceptance_plan_lines renders parent dry-run decisions without executing tests.
# 函数用途:给 status/subagents 展示父级验收下一步摘要；只显示 refs 和摘要字段，不展开事实文件正文。
def format_acceptance_plan_lines(panels: list[dict[str, Any]]) -> list[str]:
    entries = _acceptance_plan_entries_from_panels(panels)
    if not entries:
        return ["- 暂无"]
    lines: list[str] = []
    for entry in entries:
        lines.extend(_acceptance_plan_entry_lines(entry))
    return lines


# LLM: format_acceptance_next_action_lines renders advisory next actions without executing them.
# 函数用途:给 status/subagents 展示父级验收建议动作；只显示命令和 refs，不读取 artifact 正文。
def format_acceptance_next_action_lines(panels: list[dict[str, Any]]) -> list[str]:
    entries = _acceptance_next_action_entries_from_panels(panels)
    if not entries:
        return ["- 暂无"]
    lines: list[str] = []
    for entry in entries:
        lines.extend(_acceptance_next_action_entry_lines(entry))
    return lines


# LLM: acceptance_plan_entries calls the read-only parent controller for visible risky/awaiting runs.
# 函数用途:从控制面可见 run 生成父级验收 dry-run 摘要；异常时跳过该 run，不执行 tests 或写回状态。
def acceptance_plan_entries(runs: list[Any], planner: Any = None) -> list[dict[str, Any]]:
    if not callable(planner):
        return []
    entries: list[dict[str, Any]] = []
    for run in runs:
        if str(getattr(run, "status", "") or "").upper() not in _ACCEPTANCE_PLAN_STATUSES:
            continue
        try:
            decision = planner(str(getattr(run, "run_id", "") or ""))
        except Exception:
            continue
        entries.append(_decision_payload(decision))
    return entries


# LLM: acceptance_next_action_entries calls the read-only advisory hook for visible risky/awaiting runs.
# 函数用途:从控制面可见 run 生成父级下一动作建议；异常时跳过该 run，不执行命令或写回状态。
def acceptance_next_action_entries(runs: list[Any], planner: Any = None) -> list[dict[str, Any]]:
    if not callable(planner):
        return []
    entries: list[dict[str, Any]] = []
    for run in runs:
        if str(getattr(run, "status", "") or "").upper() not in _ACCEPTANCE_PLAN_STATUSES:
            continue
        try:
            action = planner(str(getattr(run, "run_id", "") or ""))
        except Exception:
            continue
        entries.append(_next_action_payload(action))
    return entries


# LLM: acceptance_planner locates the manager dry-run hook without making it mandatory for test doubles.
# 函数用途:安全取得 `plan_parent_acceptance`；没有该能力时 status/board 仍能展示其它面板内容。
def acceptance_planner(agent: Any) -> Any:
    return getattr(getattr(agent, "subagents", None), "plan_parent_acceptance", None)


# LLM: acceptance_next_action_planner locates the advisory hook without making it mandatory for test doubles.
# 函数用途:安全取得 `plan_parent_acceptance_next_action`；没有该能力时其它 status 面板不受影响。
def acceptance_next_action_planner(agent: Any) -> Any:
    return getattr(getattr(agent, "subagents", None), "plan_parent_acceptance_next_action", None)


# LLM: _acceptance_plan_entry_lines keeps one decision's human output compact and refs-only.
# 函数用途:渲染单条父级验收计划摘要，避免主格式函数继续加深嵌套。
def _acceptance_plan_entry_lines(entry: dict[str, Any]) -> list[str]:
    lines = [
        f"- {entry.get('run_id', '')} decision={entry.get('decision', '')} "
        f"risk={entry.get('risk_level', '')} "
        f"human={bool(entry.get('requires_human_confirmation', False))}"
    ]
    reason = entry.get("reason")
    if reason:
        lines.append(f"  - reason={reason}")
    lines.extend(_acceptance_plan_ref_lines(entry))
    return lines


# LLM: _acceptance_plan_ref_lines emits only paths already present in the dry-run decision.
# 函数用途:输出验收计划引用路径，不打开引用文件。
def _acceptance_plan_ref_lines(entry: dict[str, Any]) -> list[str]:
    names = ("test_execution_ref", "failure_handoff_ref", "takeover_readiness_ref")
    return [f"  - {name}={entry[name]}" for name in names if entry.get(name)]


# LLM: _acceptance_next_action_entry_lines keeps one advisory action compact and refs-only.
# 函数用途:渲染单条父级下一动作建议；不执行 command，也不读取 refs 指向的正文。
def _acceptance_next_action_entry_lines(entry: dict[str, Any]) -> list[str]:
    lines = [
        f"- {entry.get('run_id', '')} action={entry.get('action', '')} "
        f"mutates_task_state={bool(entry.get('mutates_task_state', False))}"
    ]
    reason = entry.get("reason")
    if reason:
        lines.append(f"  - reason={reason}")
    command = entry.get("command")
    if command:
        lines.append(f"  - command={command}")
    lines.extend(_acceptance_next_action_ref_lines(entry))
    return lines


# LLM: _acceptance_next_action_ref_lines emits only references already present on the advisory action.
# 函数用途:输出父级下一动作关联的审计/交接路径，不打开路径内容。
def _acceptance_next_action_ref_lines(entry: dict[str, Any]) -> list[str]:
    names = (
        "decision_ref",
        "apply_ref",
        "test_execution_ref",
        "failure_handoff_ref",
        "takeover_readiness_ref",
    )
    return [f"  - {name}={entry[name]}" for name in names if entry.get(name)]


# LLM: _acceptance_plan_entries_from_panels flattens prepared dry-run decisions for CLI rendering.
# 函数用途:复用 shared-progress payload 中的验收计划摘要，避免 status 和看板各自拼字段。
def _acceptance_plan_entries_from_panels(panels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for panel in panels:
        raw_entries = panel.get("acceptance_plan_entries", [])
        if isinstance(raw_entries, list):
            entries.extend(item for item in raw_entries if isinstance(item, dict))
    return entries


# LLM: _acceptance_next_action_entries_from_panels flattens prepared advisory actions for display.
# 函数用途:复用 shared-progress payload 中的下一动作建议，避免 status 和看板重复拼字段。
def _acceptance_next_action_entries_from_panels(panels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for panel in panels:
        raw_entries = panel.get("acceptance_next_action_entries", [])
        if isinstance(raw_entries, list):
            entries.extend(item for item in raw_entries if isinstance(item, dict))
    return entries


# LLM: _decision_payload narrows parent decisions to refs-only fields safe for status JSON.
# 函数用途:把真实或测试替身决策转为 CLI payload；保留路径/摘要，不读取路径内容。
def _decision_payload(decision: Any) -> dict[str, Any]:
    to_dict = getattr(decision, "to_dict", None)
    payload = to_dict() if callable(to_dict) else {}
    if not isinstance(payload, dict):
        payload = {}
    return {
        "run_id": payload.get("run_id", getattr(decision, "run_id", "")),
        "decision": payload.get("decision", getattr(decision, "decision", "")),
        "reason": payload.get("reason", getattr(decision, "reason", "")),
        "risk_level": payload.get("risk_level", getattr(decision, "risk_level", "")),
        "requires_human_confirmation": bool(
            payload.get("requires_human_confirmation", getattr(decision, "requires_human_confirmation", False))
        ),
        "evidence_refs": list(payload.get("evidence_refs", []) or []),
        "test_execution_ref": payload.get("test_execution_ref", getattr(decision, "test_execution_ref", "")),
        "failure_handoff_ref": payload.get("failure_handoff_ref", getattr(decision, "failure_handoff_ref", "")),
        "takeover_readiness_ref": payload.get("takeover_readiness_ref", getattr(decision, "takeover_readiness_ref", "")),
        "next_actions": list(payload.get("next_actions", []) or []),
    }


# LLM: _next_action_payload narrows advisory actions to refs-only fields safe for status JSON.
# 函数用途:把真实或测试替身下一动作建议转为 CLI payload；保留命令/路径/摘要，不执行命令或读取路径。
def _next_action_payload(action: Any) -> dict[str, Any]:
    to_dict = getattr(action, "to_dict", None)
    payload = to_dict() if callable(to_dict) else {}
    if not isinstance(payload, dict):
        payload = {}
    return {
        "run_id": payload.get("run_id", getattr(action, "run_id", "")),
        "action": payload.get("action", getattr(action, "action", "")),
        "reason": payload.get("reason", getattr(action, "reason", "")),
        "decision": payload.get("decision", getattr(action, "decision", "")),
        "command": payload.get("command", getattr(action, "command", "")),
        "decision_ref": payload.get("decision_ref", getattr(action, "decision_ref", "")),
        "apply_ref": payload.get("apply_ref", getattr(action, "apply_ref", "")),
        "test_execution_ref": payload.get("test_execution_ref", getattr(action, "test_execution_ref", "")),
        "failure_handoff_ref": payload.get("failure_handoff_ref", getattr(action, "failure_handoff_ref", "")),
        "takeover_readiness_ref": payload.get("takeover_readiness_ref", getattr(action, "takeover_readiness_ref", "")),
        "requires_human_confirmation": bool(
            payload.get("requires_human_confirmation", getattr(action, "requires_human_confirmation", False))
        ),
        "mutates_task_state": bool(payload.get("mutates_task_state", getattr(action, "mutates_task_state", False))),
    }
