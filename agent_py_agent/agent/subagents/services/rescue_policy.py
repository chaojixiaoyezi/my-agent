# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""rescue/escalation annotations for due-check action plans."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..reports import DueCheckIssue


# LLM: rescue_fields_for_issue 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 处理rescue字段issue相关的数据流，连接当前职责的前后步骤；关键副作用: 需保持任务状态、报告记录和持久化副作用上的返回值和副作用边界稳定。
def rescue_fields_for_issue(issue: DueCheckIssue, action: str) -> dict[str, object]:
    """Build non-mutating rescue metadata for one due-check issue."""
    strategy, target = _strategy_and_target(issue.kind, action)
    return {
        "rescue_trigger": issue.kind,
        "rescue_strategy": strategy,
        "escalation_target": target,
        "rescue_context_refs": _context_refs(issue),
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
    }


# LLM: _strategy_and_target 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 处理strategytarget相关的数据流，连接当前职责的前后步骤；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _strategy_and_target(kind: str, action: str) -> tuple[str, str]:
    if kind in {"run_timeout", "heartbeat_stale", "status_timeout"}:
        return "takeover_or_shrink_scope_before_retry", "parent"
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


# LLM: _merge_refs 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 更新refs对应的任务或运行状态，并保留既有字段语义；关键副作用: 会更新任务状态、报告记录和持久化副作用，需避免破坏既有状态机约定。
def _merge_refs(existing: list[str], incoming: list[str]) -> list[str]:
    refs = list(existing)
    for ref in incoming:
        if ref and ref not in refs:
            refs.append(ref)
    return refs
