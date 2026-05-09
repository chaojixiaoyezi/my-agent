# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""rescue/escalation annotations for due-check action plans."""

from pathlib import Path
from typing import TYPE_CHECKING

from .takeover_readiness import takeover_readiness_ref_order

if TYPE_CHECKING:
    from ..reports import DueCheckIssue


# LLM: rescue_fields_for_issue 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 处理rescue字段issue相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、报告记录和持久化副作用上的返回值和副作用边界稳定。
def rescue_fields_for_issue(issue: DueCheckIssue, action: str) -> dict[str, object]:
    """Build non-mutating rescue metadata for one due-check issue."""
    strategy, target = _strategy_and_target(issue.kind, action)
    refs = _context_refs(issue)
    return {
        "rescue_trigger": issue.kind,
        "rescue_strategy": strategy,
        "escalation_target": target,
        "rescue_context_refs": refs,
        "rescue_packet": _build_rescue_packet(issue, action, refs),
    }


# LLM: merge_rescue_fields 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 更新rescue字段对应的任务或运行状态，并保留既有字段语义；关键副作用: 会更新任务状态、报告记录和持久化副作用，需避免破坏既有状态机约定。
def merge_rescue_fields(item, issue: DueCheckIssue, action: str) -> None:
    """Merge another due-check issue into an existing action's rescue metadata."""
    fields = rescue_fields_for_issue(issue, action)
    triggers = [value for value in item.rescue_trigger.split(",") if value]
    trigger = str(fields["rescue_trigger"])
    if trigger not in triggers:
        triggers.append(trigger)
    item.rescue_trigger = ",".join(triggers)
    item.rescue_context_refs = _merge_refs(
        item.rescue_context_refs,
        [str(value) for value in fields["rescue_context_refs"]],
    )
    _merge_rescue_packet(item, fields, issue)
    if not item.rescue_strategy:
        item.rescue_strategy = str(fields["rescue_strategy"])
    if not item.escalation_target or issue.severity == "P0":
        item.escalation_target = str(fields["escalation_target"])


# LLM: action_rescue_record_fields 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 处理动作rescue记录字段相关的数据流，连接当前职责的前后步骤；关键副作用: 会改动任务状态、报告记录和持久化副作用，调用方依赖写入顺序和文件格式。
def action_rescue_record_fields(action) -> dict[str, object]:
    """Copy rescue metadata from action plan into apply records."""
    return {
        "rescue_trigger": getattr(action, "rescue_trigger", ""),
        "rescue_strategy": getattr(action, "rescue_strategy", ""),
        "escalation_target": getattr(action, "escalation_target", ""),
        "rescue_context_refs": list(getattr(action, "rescue_context_refs", []) or []),
        "rescue_packet": dict(getattr(action, "rescue_packet", {}) or {}),
    }


# LLM: _strategy_and_target 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 处理strategytarget相关的数据流，连接当前职责的前后步骤；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _strategy_and_target(kind: str, action: str) -> tuple[str, str]:
    if kind in {"run_timeout", "heartbeat_stale", "status_timeout"}:
        return "takeover_or_shrink_scope_before_retry", "parent"
    # LLM: parent-timeout child recovery is guidance-only until a human selects a handoff path.
    if kind == "parent_timeout_with_unfinished_children":
        return "recover_unfinished_children_after_parent_timeout", "parent"
    if kind in {"status_failed", "status_blocked"}:
        return "inspect_failure_then_rescue_or_escalate", "parent"
    if kind in {"channel_broken", "channel_probe_missing", "status_channel_error"}:
        return "repair_channel_before_retry", "runtime_owner"
    if kind == "open_capability_request":
        return "route_capability_request_before_retry", "capability_router"
    if kind == "open_capability_gap":
        return "escalate_capability_gap_for_tooling_or_learning", "capability_owner"
    if kind == "fake_done_risk":
        return "reopen_and_request_missing_evidence", "parent"
    if kind == "missing_work_order_files":
        return "repair_work_order_before_any_retry", "parent"
    return action or "inspect_manually", "parent"


# LLM: _context_refs 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 处理上下文refs相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、报告记录和持久化副作用上的返回值和副作用边界稳定。
def _context_refs(issue: DueCheckIssue) -> list[str]:
    refs = [issue.task_dir]
    refs.extend(_readiness_refs_for_issue(issue))
    # LLM: related_refs preserves child/run recovery entrypoints without scraping localized text.
    refs.extend(str(ref) for ref in getattr(issue, "related_refs", []) or [])
    refs.extend([
        f"status:{issue.status}",
        f"severity:{issue.severity}",
        f"age_seconds:{issue.age_seconds:.0f}",
        f"stale_seconds:{issue.stale_seconds:.0f}",
    ])
    if issue.open_request_count:
        refs.append(f"open_capability_requests:{issue.open_request_count}")
    if issue.open_gap_count:
        refs.append(f"open_capability_gaps:{issue.open_gap_count}")
    return [ref for ref in refs if ref]


# LLM: _readiness_refs_for_issue exposes recovery pointers while keeping large artifact bodies out of action plans.
# 函数用途: 根据 due-check 的 task_dir 查找 takeover_readiness.json，只读取 refs 顺序，不读取 artifact 正文。
def _readiness_refs_for_issue(issue: DueCheckIssue) -> list[str]:
    if not issue.task_dir:
        return []
    path = Path(issue.task_dir) / "reports" / "takeover_readiness.json"
    if not path.exists():
        return []
    return takeover_readiness_ref_order(str(path))


# LLM: _build_rescue_packet is a refs-only recovery plan; it records policy but never executes retry/takeover.
# 函数用途: 为 action plan 生成可审计 rescue 包，记录去重、重试上限、上抛和人工确认建议。
def _build_rescue_packet(
    issue: DueCheckIssue,
    action: str,
    refs: list[str],
) -> dict[str, object]:
    strategy, target = _strategy_and_target(issue.kind, action)
    return {
        "schema_name": "subagent_rescue_packet",
        "schema_version": 1,
        "run_id": issue.run_id,
        "action": action,
        "dedupe_key": f"{issue.run_id}:{action}",
        "issue_kinds": [issue.kind],
        "repeat_count": 1,
        "retry_policy": {
            "max_attempts": 1,
            "current_attempts": 0,
            "auto_retry": False,
        },
        "escalation": {
            "target": target,
            "strategy": strategy,
        },
        "manual_confirmation": {
            "required": True,
            "reason": "rescue_action_is_a_plan_not_auto_execution",
        },
        "recovery_entrypoints": _unique_strings(refs[1:] if refs and refs[0] == issue.task_dir else refs),
        "reserved": {
            "reads_artifact_bodies": False,
            "auto_execute": False,
            "packet_is_action_plan_index": True,
        },
    }


# LLM: _merge_rescue_packet keeps duplicate due-check issues visible without creating duplicate actions.
# 函数用途: 合并同一 run/action 的 rescue 包，更新重复问题计数和恢复入口 refs。
def _merge_rescue_packet(item, fields: dict[str, object], issue: DueCheckIssue) -> None:
    incoming = fields.get("rescue_packet")
    if not isinstance(incoming, dict):
        return
    packet = dict(getattr(item, "rescue_packet", {}) or {})
    if not packet:
        item.rescue_packet = dict(incoming)
        return
    issue_kinds = _merge_refs(_string_list(packet.get("issue_kinds")), _string_list(incoming.get("issue_kinds")))
    packet["issue_kinds"] = issue_kinds
    packet["repeat_count"] = len(issue_kinds)
    packet["recovery_entrypoints"] = _merge_refs(
        _string_list(packet.get("recovery_entrypoints")),
        _string_list(incoming.get("recovery_entrypoints")),
    )
    if issue.severity == "P0":
        packet["escalation"] = incoming.get("escalation", packet.get("escalation", {}))
    item.rescue_packet = packet


# LLM: _merge_refs 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 更新refs对应的任务或运行状态，并保留既有字段语义；关键副作用: 会更新任务状态、报告记录和持久化副作用，需避免破坏既有状态机约定。
def _merge_refs(existing: list[str], incoming: list[str]) -> list[str]:
    refs = list(existing)
    for ref in incoming:
        if ref and ref not in refs:
            refs.append(ref)
    return refs


# LLM: _unique_strings preserves rescue packet ref order while dropping empty duplicates.
# 函数用途: 生成 rescue packet 的恢复入口列表，避免重复 refs 扰乱接管顺序。
def _unique_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in values if item))


# LLM: _string_list accepts only printable strings from loose packet fields.
# 函数用途: 合并 rescue packet 字段前做窄化，避免坏 JSON 影响 action plan。
def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if item not in (None, "")]
