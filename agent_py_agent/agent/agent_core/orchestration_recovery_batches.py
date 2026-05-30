# LLM: Recovery batch planning separates rerun/takeover/leader-fuse actions before the parent acts.
# 模块用途: 把多个失败 child 的 recovery_strategies 按动作分批，生成可复制但不串线的下一步工具调用。

from __future__ import annotations

from typing import Any


# LLM: recovery_batches_from_strategies is the stable grouping entry used by dispatch progress payloads.
# 函数用途: 将恢复策略按 recommended_action 分组，避免多个失败 child 共享错误 runner_instruction。
def recovery_batches_from_strategies(strategies: list[dict[str, object]]) -> list[dict[str, object]]:
    groups: dict[str, list[dict[str, object]]] = {}
    for item in strategies:
        action = str(item.get("recommended_action") or "manual_review_missing_recovery_refs")
        groups.setdefault(action, []).append(item)
    return [_batch_payload(action, items) for action, items in groups.items()]


# LLM: _batch_payload keeps each recovery action narrow and explicit.
# 函数用途: 为一组相同恢复动作生成 run_ids、执行模式和建议工具调用。
def _batch_payload(action: str, items: list[dict[str, object]]) -> dict[str, object]:
    run_ids = _run_ids(items)
    payload: dict[str, object] = {
        "action": action,
        "run_ids": run_ids,
        "count": len(run_ids),
        "execution_mode": _execution_mode(action),
        "suggested_tool_call": _suggested_tool_call(action, run_ids, items),
    }
    if action == "recover_coordinator_leadership":
        payload["requires_leader_selection"] = True
        payload["child_run_ids_by_leader"] = _child_run_ids_by_leader(items)
    if action == "stop_no_progress_and_escalate":
        payload["must_not_auto_retry"] = True
    return {key: value for key, value in payload.items() if value not in ({}, [], "")}


# LLM: _execution_mode lets parents distinguish ordinary reruns from state-mutating recovery.
# 函数用途: 将 action 名字映射成父级下一步类型。
def _execution_mode(action: str) -> str:
    if action.startswith("rerun_original"):
        return "rerun_original"
    if action.startswith("create_takeover_run"):
        return "takeover_apply"
    if action == "recover_coordinator_leadership":
        return "leader_recovery"
    if action == "stop_no_progress_and_escalate":
        return "stop_and_report"
    return "manual_review"


# LLM: _suggested_tool_call returns copy-safe calls only when the system has enough structured facts.
# 函数用途: 针对普通续跑和接管写回生成不同工具参数；需要人工选 leader 时不伪造接管者。
def _suggested_tool_call(action: str, run_ids: list[str], items: list[dict[str, object]]) -> dict[str, object]:
    if action.startswith("rerun_original"):
        return _rerun_tool_call(run_ids, items)
    if action.startswith("create_takeover_run"):
        return _takeover_tool_call(run_ids)
    return {}


# LLM: _rerun_tool_call may include runner_instruction only for a single exact run.
# 函数用途: 多个恢复 run 不能共享一条 packet 指令，单个 run 才附带 runner_instruction。
def _rerun_tool_call(run_ids: list[str], items: list[dict[str, object]]) -> dict[str, object]:
    call = _dispatch_tool_call(run_ids, execute_runners=True)
    if len(run_ids) == 1 and len(items) == 1:
        instruction = str(items[0].get("runner_instruction") or "").strip()
        if instruction:
            call["runner_instruction"] = instruction
    return call


# LLM: _takeover_tool_call writes recovery state first; it does not start more live runners.
# 函数用途: 接管类恢复先走 dispatch 的 action apply 写回，不自动启动 runner，防止挂死后无限扩容。
def _takeover_tool_call(run_ids: list[str]) -> dict[str, object]:
    call = _dispatch_tool_call(run_ids, execute_runners=False)
    call["max_runners"] = 0
    return call


# LLM: _dispatch_tool_call centralizes the orchestration tool shape used in recovery batches.
# 函数用途: 生成稳定 dispatch_subagents 参数，调用方只调整 execute_runners/max_runners。
def _dispatch_tool_call(run_ids: list[str], *, execute_runners: bool) -> dict[str, Any]:
    return {
        "tool": "dispatch_subagents",
        "dry_run": not execute_runners,
        "run_ids": list(run_ids),
        "workflow_mode": "off",
    }


# LLM: _run_ids preserves strategy order while removing empty or duplicate ids.
# 函数用途: 提取恢复批次涉及的 run_id，保持父级看到的顺序稳定。
def _run_ids(items: list[dict[str, object]]) -> list[str]:
    result: list[str] = []
    for item in items:
        run_id = str(item.get("run_id") or "").strip()
        if run_id and run_id not in result:
            result.append(run_id)
    return result


# LLM: _child_run_ids_by_leader keeps leadership recovery refs visible without picking a leader.
# 函数用途: 展示失联 coordinator 旗下孩子，等待父级或专门 leader recovery 工具选择新 leader。
def _child_run_ids_by_leader(items: list[dict[str, object]]) -> dict[str, list[str]]:
    mapping: dict[str, list[str]] = {}
    for item in items:
        run_id = str(item.get("run_id") or "").strip()
        if not run_id:
            continue
        children = [str(child) for child in item.get("child_run_ids") or [] if str(child).strip()]
        mapping[run_id] = children
    return mapping


__all__ = ["recovery_batches_from_strategies"]
