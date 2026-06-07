
from __future__ import annotations

from ....runtime_errors import runtime_error_report
from ....subagents.models import (
    SUBAGENT_FAILURE_STATUSES,
    SUBAGENT_TASK_STATUSES,
    TaskStatus,
    task_status_in,
)
from ....subagents.services.hierarchy.qa_scheduler import (
    qa_orchestration_advice,
    quality_advice_payload,
)
from ....subagents.services.recovery.modes import is_rerun_mode
from ....subagents.services.recovery.strategy import (
    SubagentRecoveryStrategyRequest,
    build_subagent_recovery_strategy,
)
from ...runner.context import current_subagent_run_id
from ..quality_payload import quality_repair_advice_payload
from ..recovery_batches import (
    recovery_batches_from_strategies,
    recovery_counts_by_field,
)


def direct_children_progress_payload(agent) -> dict[str, object]:
    parent_run_id = current_subagent_run_id(agent)
    if not parent_run_id:
        return {}
    direct_children, load_error = _direct_children(agent, parent_run_id)
    if load_error:
        return {"direct_children": _children_load_error_payload(parent_run_id, load_error)}
    payload = _progress_payload(parent_run_id, direct_children)
    payload["direct_children"].update(quality_repair_advice_payload(agent, parent_run_id))
    _attach_quality_advice(agent, parent_run_id, payload["direct_children"])
    _attach_recovery_strategies(agent, payload["direct_children"])
    _attach_direct_child_next_action(payload["direct_children"])
    return payload


def _direct_children(agent, parent_run_id: str) -> tuple[list, BaseException | None]:
    try:
        return [
            item for item in agent.subagents.list_runs()
            if str(getattr(item, "parent_id", "")) == parent_run_id
        ], None
    except Exception as exc:
        return [], exc


def _children_load_error_payload(parent_run_id: str, exc: BaseException) -> dict[str, object]:
    return {
        "parent_run_id": parent_run_id,
        "total": 0,
        "by_status": {},
        "planning_run_ids": [],
        "running_run_ids": [],
        "recovery_run_ids": [],
        "unfinished_run_ids": [],
        "needs_more_dispatch": False,
        "needs_recovery": False,
        "ready_for_closeout": False,
        "load_error": runtime_error_report(exc, context="direct_children.list_runs"),
        "load_error_hint": "直接子代理列表读取失败；这不是没有子代理，也不是所有子代理已完成。",
    }


def _attach_direct_child_next_action(children: dict[str, object]) -> None:
    if children["needs_recovery"]:
        children.update(_recovery_dispatch_payload(children["recovery_run_ids"], children.get("recovery_strategies")))
        if children.get("needs_repair_wave"):
            children["repair_wave_deferred_by_recovery"] = True
        return
    if children.get("needs_repair_wave"):
        children["ready_for_closeout"] = False
        children["next_action"] = "create_repair_child_from_qa_refs"
        return
    if children["needs_more_dispatch"]:
        children.update(_unfinished_child_payload(children))
        return
    if children.get("needs_status_review"):
        children.update(_status_review_payload(children.get("unverified_run_ids") or []))
        return
    if children.get("quality_advice"):
        children["ready_for_closeout"] = False
        children.update(_quality_wave_payload(children["quality_advice"]))
        return
    if children["ready_for_closeout"]:
        children.update(_closeout_payload())


def _unfinished_child_payload(children: dict[str, object]) -> dict[str, object]:
    running = [str(item) for item in children.get("running_run_ids") or [] if str(item)]
    if running:
        return _wait_for_running_children_payload(running)
    return _continue_dispatch_payload(children.get("unfinished_run_ids") or [])


def _wait_for_running_children_payload(run_ids: list[str]) -> dict[str, object]:
    return {
        "next_action": "wait_for_running_direct_children",
        "suggested_tool_call": {
            "tool": "wait",
            "seconds": 120,
            "reason": "等待运行中的直接子代理完成或产出新事件",
        },
        "wait_hint": (
            "仍有直接 child 正在 RUNNING；这是正常后台执行状态。"
            "登记 wait 后继续自己的工作或回复用户，不要因为等待而重复 inspect_agent_tree、dispatch_subagents 或重新 create_subagents。"
        ),
        "running_run_ids": run_ids,
    }


def _continue_dispatch_payload(run_ids: list[str]) -> dict[str, object]:
    return {
        "next_action": "continue_dispatch_direct_children",
        "suggested_tool_call": _dispatch_tool_call(run_ids),
        "continue_hint": (
            "仍有直接 child 处于 PLANNING，且没有 RUNNING 子代理；这通常是限速、串行调度或显式 defer_start 造成的。"
            "继续调用 dispatch_subagents，不要把 PLANNING 直接判为失败。"
        ),
    }


def _status_review_payload(run_ids: list[object]) -> dict[str, object]:
    ids = [str(item) for item in run_ids if str(item)]
    return {
        "next_action": "inspect_unverified_direct_children",
        "suggested_tool_call": {"tool": "inspect_agent_tree"},
        "unverified_run_ids": ids,
        "status_review_hint": (
            "存在直接 child 的状态不是当前协议完成、运行、待派发或可恢复状态；"
            "先查看 agent tree 和 canonical state，不要把旧状态别名当成完成。"
        ),
    }


def _recovery_dispatch_payload(
    run_ids: list[str],
    strategies: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    primary = _primary_recovery_strategy(strategies or [])
    return {
        "next_action": "inspect_or_rescue_direct_children",
        "suggested_tool_call": _recovery_dispatch_tool_call(run_ids, primary),
        "suggested_recovery_child_tool_call": _recovery_child_tool_call(run_ids),
        "recovery_hint": (
            "有直接 child 已 BLOCKED/FAILED/TIMEOUT；先看 recovery_strategies。"
            "如果单个 run 的 latest_continue_packet 可用，优先按 runner_instruction 续跑原 run。"
            "如果仍不可重试，按 suggested_recovery_child_tool_call 创建恢复 child。"
            "恢复 child 默认可以是 worker；只有确实需要继续拆多层时，父节点才改成 coordinator。"
            "在执行恢复动作前不要反复 read_file/read_artifact 打开 child 产物正文；先按 refs 和建议工具调用推进。"
        ),
    }


def _closeout_payload() -> dict[str, object]:
    return {
        "next_action": "summarize_direct_children_refs",
        "closeout_hint": (
            "所有直接 child 已完成或没有阻塞；不要反复 read_file/read_artifact 读取子产物正文。"
            "请只汇总 child run_id、状态、产物 refs 和阻塞项，写入自己的 output.json 或最终结果块。"
        ),
    }


def _quality_wave_payload(advice: dict[str, object]) -> dict[str, object]:
    return {
        "next_action": "create_quality_children_from_ready_refs",
        "suggested_tool_call": {
            "tool": "schedule_child_subagents",
            "dry_run": False,
            "children": list(advice.get("suggested_children") or []),
        },
        "quality_hint": (
            "父任务还缺真实 QA 角色；先按 suggested_tool_call 创建 tester/bug_finder，"
            "不要反复 read_file/read_artifact 读取产物正文来替代 QA 子代理。"
        ),
    }


def _dispatch_tool_call(run_ids: list[str]) -> dict[str, object]:
    return {
        "tool": "dispatch_subagents",
        "dry_run": False,
        "run_ids": run_ids,
        "workflow_mode": "off",
    }


def _recovery_dispatch_tool_call(
    run_ids: list[str],
    primary_strategy: dict[str, object] | None,
) -> dict[str, object]:
    call = _dispatch_tool_call(run_ids)
    if len([item for item in run_ids if item]) != 1 or not primary_strategy:
        return call
    recovery_mode = str(primary_strategy.get("recovery_mode") or "")
    if not is_rerun_mode(recovery_mode):
        return call
    call["recovery_mode"] = recovery_mode
    instruction = str(primary_strategy.get("runner_instruction") or "").strip()
    if instruction:
        call["runner_instruction"] = instruction
    return call


def _primary_recovery_strategy(strategies: list[dict[str, object]]) -> dict[str, object] | None:
    return strategies[0] if len(strategies) == 1 else None


def _attach_quality_advice(agent, parent_run_id: str, children: dict[str, object]) -> None:
    if (
        children.get("needs_more_dispatch")
        or children.get("needs_recovery")
        or children.get("needs_repair_wave")
    ):
        return
    try:
        parent = agent.subagents.load(parent_run_id)
    except Exception as exc:
        children["quality_advice_load_error"] = runtime_error_report(
            exc,
            context="direct_children.quality_advice.parent_load",
        )
        return
    advice = qa_orchestration_advice(manager=agent.subagents, parent=parent, specs=[])
    if advice is None or advice.phase != "quality_wave_ready":
        return
    children["quality_advice"] = quality_advice_payload(advice)


def _attach_recovery_strategies(agent, children: dict[str, object]) -> None:
    strategies: list[dict[str, object]] = []
    load_errors: list[dict[str, object]] = []
    all_tasks, all_tasks_error = _safe_all_tasks(agent)
    if all_tasks_error:
        children["recovery_context_load_error"] = runtime_error_report(
            all_tasks_error,
            context="direct_children.recovery_context.list_runs",
        )
    for run_id in children.get("recovery_run_ids") or []:
        task, load_error = _load_recovery_task(agent, str(run_id))
        if load_error:
            load_errors.append(_recovery_task_load_error(str(run_id), load_error))
        if task is None:
            continue
        strategies.append(build_subagent_recovery_strategy(_strategy_request(agent, task, all_tasks)).to_dict())
    if load_errors:
        children["recovery_load_errors"] = load_errors
    if not strategies:
        return
    children["recovery_strategies"] = strategies
    children["recovery_action_counts"] = recovery_counts_by_field(strategies, "recommended_action")
    children["recovery_mode_counts"] = recovery_counts_by_field(strategies, "recovery_mode")
    children["recovery_batches"] = recovery_batches_from_strategies(strategies)


def _load_recovery_task(agent, run_id: str):
    try:
        task = agent.subagents.load(run_id)
    except Exception as exc:
        return None, exc
    return (task, None) if str(getattr(task, "id", "") or "") == run_id else (None, None)


def _safe_all_tasks(agent) -> tuple[list, BaseException | None]:
    try:
        return list(agent.subagents.list_runs()), None
    except Exception as exc:
        return [], exc


def _recovery_task_load_error(run_id: str, exc: BaseException) -> dict[str, object]:
    return {
        "run_id": run_id,
        **runtime_error_report(exc, context="direct_children.recovery_task.load"),
    }


def _strategy_request(agent, task, all_tasks: list | None = None) -> SubagentRecoveryStrategyRequest:
    config = getattr(agent, "config", None)
    return SubagentRecoveryStrategyRequest(
        task=task,
        all_tasks=list(all_tasks or []),
        packet_max_age_seconds=float(getattr(config, "subagent_recovery_packet_max_age_seconds", 0.0) or 0.0),
        no_progress_attempt_limit=_no_progress_attempt_limit(config),
    )


def _no_progress_attempt_limit(config: object) -> int:
    value = getattr(config, "subagent_no_progress_attempt_limit", 4)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 4


def _progress_payload(parent_run_id: str, direct_children: list) -> dict[str, object]:
    by_status: dict[str, int] = {}
    planning_ids: list[str] = []
    running_ids: list[str] = []
    recovery_ids: list[str] = []
    unverified_ids: list[str] = []
    for item in direct_children:
        status = str(getattr(item, "status", "") or "UNKNOWN")
        item_id = str(getattr(item, "id", "") or "")
        by_status[status] = by_status.get(status, 0) + 1
        if task_status_in(status, {TaskStatus.PLANNING.value}):
            planning_ids.append(item_id)
        if task_status_in(status, {TaskStatus.RUNNING.value}):
            running_ids.append(item_id)
        if task_status_in(status, SUBAGENT_FAILURE_STATUSES):
            recovery_ids.append(item_id)
        if not task_status_in(status, SUBAGENT_TASK_STATUSES):
            unverified_ids.append(item_id)
    unfinished_ids = [item for item in [*planning_ids, *running_ids] if item]
    recovery_ids = [item for item in recovery_ids if item]
    unverified_ids = [item for item in unverified_ids if item]
    return {
        "direct_children": {
            "parent_run_id": parent_run_id,
            "total": len(direct_children),
            "by_status": by_status,
            "planning_run_ids": [item for item in planning_ids if item],
            "running_run_ids": [item for item in running_ids if item],
            "recovery_run_ids": recovery_ids,
            "unverified_run_ids": unverified_ids,
            "unfinished_run_ids": unfinished_ids,
            "needs_more_dispatch": bool(unfinished_ids),
            "needs_recovery": bool(recovery_ids),
            "needs_status_review": bool(unverified_ids),
            "ready_for_closeout": bool(direct_children) and not unfinished_ids and not recovery_ids and not unverified_ids,
        }
    }


def _recovery_child_tool_call(recovery_run_ids: list[str]) -> dict[str, object]:
    ids = [item for item in recovery_run_ids if item]
    joined_ids = ", ".join(ids)
    return {
        "tool": "schedule_child_subagents",
        "dry_run": False,
        "role_selection_hint": "默认用 worker；只有恢复本身需要继续拆下级任务时，父节点才把 role 改成 coordinator/lead。",
        "children": [
            {
                "role": "worker",
                "agent_name": "recovery-worker",
                "goal": (
                    "接管或修复这些直接 child runs："
                    f"{joined_ids}。先读取它们的 status/failure_handoff/takeover refs，"
                    "不要改写健康分支；如果只是单点修复就直接完成，"
                    "如果确实需要继续拆多层，再由父节点改派 coordinator。"
                ),
                "allowed_tools": [
                    "inspect_agent_tree",
                    "read_file",
                    "list_files",
                ],
            }
        ],
    }
