# LLM: Dispatch state contract summarizes touched run statuses for the parent model.
# 模块用途: 把当前 root 轮次的 run 状态压成机器可读合同，避免父级翻大 records 或凭记忆继续调度。

from __future__ import annotations

from ..contracts.error_taxonomy import error_contract
from ..contracts.state_machine import run_state_snapshot_from_task
from .orchestration_run_scope import remembered_orchestration_run_ids

_RUNNING_STATUSES = {"RUNNING"}
_BLOCKED_STATUSES = {"BLOCKED", "FAILED", "TIMEOUT", "CHANNEL_ERROR"}


# LLM: dispatch_state_contract_payload is refs-first status guidance for top-level dispatch.
# 函数用途: 返回本轮已创建/调度 run 的状态桶、下一步建议和可复制工具调用；不读取产物正文。
def dispatch_state_contract_payload(agent: object) -> dict[str, object]:
    tasks, missing = _remembered_tasks(agent)
    if not tasks and not missing:
        return {}
    state = _state_payload(tasks, missing)
    _attach_state_next_action(state)
    return {"current_turn_run_state": state}


# LLM: _remembered_tasks loads only explicit current-turn ids, never scans old workspaces broadly.
# 函数用途: 根据 memory 中的当前轮 run ids 读取任务状态；缺失 id 进入 missing_run_ids。
def _remembered_tasks(agent: object) -> tuple[list[object], list[str]]:
    load = getattr(getattr(agent, "subagents", None), "load", None)
    if not callable(load):
        return [], []
    tasks: list[object] = []
    missing: list[str] = []
    for run_id in sorted(remembered_orchestration_run_ids(agent)):
        try:
            task = load(run_id)
        except Exception:
            missing.append(run_id)
            continue
        tasks.append(task)
    return tasks, missing


# LLM: _state_payload folds task lifecycle fields into stable buckets.
# 函数用途: 生成 status 计数和 dispatchable/running/blocked/verified 等 run_id 列表。
def _state_payload(tasks: list[object], missing_run_ids: list[str]) -> dict[str, object]:
    buckets = _empty_state_buckets()
    for task in tasks:
        _append_task_state(buckets, task)
    return {
        "state_machine_contract": "state_machine.v1",
        "total": len(tasks),
        "by_status": buckets["by_status"],
        "dispatchable_run_ids": _clean_ids(buckets["dispatchable"]),
        "running_run_ids": _clean_ids(buckets["running"]),
        "blocked_run_ids": _clean_ids(buckets["blocked"]),
        "verified_run_ids": _clean_ids(buckets["verified"]),
        "unfinished_run_ids": _clean_ids(buckets["unfinished"]),
        "missing_run_ids": _clean_ids(missing_run_ids),
        "recovery_recommendations": buckets["recovery_recommendations"],
    }


# LLM: _empty_state_buckets keeps _state_payload shallow for code-size guard clarity.
# 函数用途: 创建状态桶容器；各桶只存 run_id，by_status 存计数。
def _empty_state_buckets() -> dict[str, object]:
    return {
        "by_status": {},
        "dispatchable": [],
        "running": [],
        "blocked": [],
        "verified": [],
        "unfinished": [],
        "recovery_recommendations": [],
    }


# LLM: _append_task_state classifies one task into status buckets without reading artifacts.
# 函数用途: 把单个任务的状态加入 dispatchable/running/blocked/verified/unfinished 等桶。
def _append_task_state(buckets: dict[str, object], task: object) -> None:
    run_id = _run_id(task)
    snapshot = run_state_snapshot_from_task(task)
    status = str(snapshot["status"])
    by_status = buckets["by_status"]
    by_status[status] = by_status.get(status, 0) + 1
    _append_if(buckets["dispatchable"], run_id, bool(snapshot["can_dispatch"]))
    _append_if(buckets["running"], run_id, status in _RUNNING_STATUSES)
    _append_if(buckets["blocked"], run_id, status in _BLOCKED_STATUSES)
    verified = bool(snapshot["can_closeout"])
    _append_if(buckets["verified"], run_id, verified)
    _append_if(buckets["unfinished"], run_id, not verified)
    if status in _BLOCKED_STATUSES:
        _append_recovery_recommendation(buckets, snapshot)


# LLM: _append_if keeps bucket mutation compact and empty-id safe.
# 函数用途: 条件成立且 run_id 非空时追加到目标列表。
def _append_if(values: object, run_id: str, condition: bool) -> None:
    if condition and run_id and isinstance(values, list):
        values.append(run_id)


# LLM: _append_recovery_recommendation exposes recovery facts without forcing the parent action.
# 函数用途: 把失败/阻塞 run 的错误类型、建议动作和中文提示写进状态合同，供父级判断下一步。
def _append_recovery_recommendation(buckets: dict[str, object], snapshot: dict[str, object]) -> None:
    values = buckets["recovery_recommendations"]
    run_id = str(snapshot.get("run_id") or "").strip()
    if not run_id or not isinstance(values, list):
        return
    decision = dict(snapshot.get("recovery_decision") or {})
    contract = error_contract(str(snapshot.get("failure_type") or "UNKNOWN_ERROR"))
    values.append({
        "run_id": run_id,
        "status": str(snapshot.get("status") or ""),
        "failure_type": contract.code,
        "recommended_action": str(decision.get("action") or ""),
        "allow_new_run": bool(decision.get("allow_new_run")),
        "reason": str(decision.get("reason") or ""),
        "recovery_hint": contract.recovery_hint,
    })


# LLM: _attach_state_next_action gives root a clear next move without hard-coding workflow order.
# 函数用途: 根据状态桶设置 next_action 和建议工具调用；真实阻塞先暴露，可调度 run 优先于验收波次。
def _attach_state_next_action(state: dict[str, object]) -> None:
    blocked = list(state.get("blocked_run_ids") or [])
    dispatchable = list(state.get("dispatchable_run_ids") or [])
    running = list(state.get("running_run_ids") or [])
    missing = list(state.get("missing_run_ids") or [])
    if blocked:
        state["next_action"] = "inspect_or_rescue_blocked_run_ids"
        state["suggested_tool_call"] = _dispatch_tool_call(blocked, execute_runners=False)
        return
    if dispatchable:
        state["next_action"] = "continue_dispatch_unfinished_run_ids"
        state["suggested_tool_call"] = _dispatch_tool_call(dispatchable, execute_runners=True)
        return
    if running:
        state["next_action"] = "wait_or_check_subagent_board"
        state["suggested_tool_call"] = {"tool": "subagent_board", "limit": 20}
        return
    if missing:
        state["next_action"] = "refresh_subagent_board_for_missing_run_ids"
        state["suggested_tool_call"] = {"tool": "subagent_board", "limit": 20}
        return
    state["next_action"] = "summarize_or_report_verified_runs"


# LLM: _dispatch_tool_call keeps suggested dispatch calls copyable and status-aware.
# 函数用途: 生成给模型复制的 dispatch_subagents 参数；阻塞状态默认只 inspect，不直接重跑。
def _dispatch_tool_call(run_ids: list[str], *, execute_runners: bool) -> dict[str, object]:
    return {
        "tool": "dispatch_subagents",
        "apply": True,
        "execute_runners": execute_runners,
        "run_ids": _clean_ids(run_ids),
        "workflow_mode": "off",
    }

# LLM: _clean_ids drops empty ids while preserving order.
# 函数用途: 清理 run_id 列表，避免空字符串进入模型建议工具参数。
def _clean_ids(values: list[object]) -> list[str]:
    result: list[str] = []
    for item in values:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
    return result


# LLM: _run_id reads task identity from persisted tasks or SimpleNamespace test doubles.
# 函数用途: 安全返回 task.id。
def _run_id(task: object) -> str:
    return str(getattr(task, "id", "") or "").strip()
