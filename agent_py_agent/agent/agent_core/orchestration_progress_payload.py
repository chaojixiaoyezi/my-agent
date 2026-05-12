# LLM: Progress payload helpers keep orchestration tool responses refs-only and compact.
# 模块用途: 给 runner-context dispatch 返回直接 child 进度摘要，避免主工具文件继续膨胀。

from __future__ import annotations

import json
from pathlib import Path

from ..subagents.services.hierarchy_qa_scheduler import qa_orchestration_advice
from .orchestration_quality_payload import quality_repair_advice_payload
from .runner_context import current_subagent_run_id


# LLM: direct_children_progress_payload teaches parent runners to continue pending children.
# 函数用途: runner 内 dispatch 后返回直接 child 状态，避免把限速未跑完的 PLANNING 误判为失败。
def direct_children_progress_payload(agent) -> dict[str, object]:
    parent_run_id = current_subagent_run_id(agent)
    if not parent_run_id:
        return {}
    direct_children = _direct_children(agent, parent_run_id)
    if direct_children is None:
        return {}
    payload = _progress_payload(parent_run_id, direct_children)
    payload["direct_children"].update(quality_repair_advice_payload(agent, parent_run_id))
    _attach_quality_advice(agent, parent_run_id, payload["direct_children"])
    _attach_direct_child_next_action(payload["direct_children"])
    return payload


# LLM: _direct_children keeps list_runs exception handling out of the payload builder.
# 函数用途: 读取当前 runner 的直接 child；读取失败时返回 None 让调用方保持空 payload。
def _direct_children(agent, parent_run_id: str) -> list | None:
    try:
        return [
            item for item in agent.subagents.list_runs()
            if str(getattr(item, "parent_id", "")) == parent_run_id
        ]
    except Exception:
        return None


# LLM: _attach_direct_child_next_action centralizes model-facing progress guidance.
# 函数用途: 根据 child 状态把继续调度、恢复或收口建议补进直接 child payload。
def _attach_direct_child_next_action(children: dict[str, object]) -> None:
    if children.get("needs_repair_wave"):
        children["ready_for_parent_acceptance"] = False
        children["next_action"] = "create_repair_child_from_qa_refs"
        return
    if children["needs_more_dispatch"]:
        children.update(_continue_dispatch_payload(children["unfinished_run_ids"]))
        return
    if children["needs_recovery"]:
        children.update(_recovery_dispatch_payload(children["recovery_run_ids"]))
        return
    if children.get("quality_advice"):
        children["ready_for_parent_acceptance"] = False
        children.update(_quality_wave_payload(children["quality_advice"]))
        return
    if children["ready_for_parent_acceptance"]:
        children.update(_closeout_payload())


# LLM: _continue_dispatch_payload describes the safe next dispatch without expanding child artifacts.
# 函数用途: 生成仍有 PLANNING/RUNNING child 时的继续调度提示。
def _continue_dispatch_payload(run_ids: list[str]) -> dict[str, object]:
    return {
        "next_action": "continue_dispatch_direct_children",
        "suggested_tool_call": _dispatch_tool_call(run_ids),
        "continue_hint": (
            "仍有直接 child 处于 PLANNING/RUNNING；这通常是限速或串行调度造成的。"
            "继续调用 dispatch_subagents，不要把 PLANNING 直接判为失败。"
        ),
    }


# LLM: _recovery_dispatch_payload keeps failed-child recovery refs-first and bounded.
# 函数用途: 生成 BLOCKED/FAILED/TIMEOUT child 的重试或恢复 child 建议。
def _recovery_dispatch_payload(run_ids: list[str]) -> dict[str, object]:
    return {
        "next_action": "inspect_or_rescue_direct_children",
        "suggested_tool_call": _dispatch_tool_call(run_ids),
        "suggested_recovery_child_tool_call": _recovery_child_tool_call(run_ids),
        "recovery_hint": (
            "有直接 child 已 BLOCKED/FAILED/TIMEOUT；先用这些 run_ids 尝试受控重试。"
            "如果仍不可重试，按 suggested_recovery_child_tool_call 创建恢复 child。"
            "恢复 child 默认可以是 worker；只有确实需要继续拆多层时，父节点才改成 coordinator。"
            "在执行恢复动作前不要反复 read_file/read_artifact 打开 child 产物正文；先按 refs 和建议工具调用推进。"
        ),
    }


# LLM: _closeout_payload tells parent runners to summarize refs rather than rereading bodies.
# 函数用途: 生成所有 child 可验收时的 refs-first 收口提示。
def _closeout_payload() -> dict[str, object]:
    return {
        "next_action": "summarize_direct_children_refs",
        "closeout_hint": (
            "所有直接 child 已等待验收或完成；不要反复 read_file/read_artifact 读取子产物正文。"
            "请只汇总 child run_id、状态、产物 refs 和阻塞项，写入自己的 output.json 或最终结果块后等待父级验收。"
        ),
    }


# LLM: _quality_wave_payload points coordinators to QA child creation before they reread artifacts.
# 函数用途: 已有可验收实现但缺 tester/bug_finder/acceptor 时，返回 refs-first QA 波次建议。
def _quality_wave_payload(advice: dict[str, object]) -> dict[str, object]:
    return {
        "next_action": "create_quality_children_from_ready_refs",
        "suggested_tool_call": {
            "tool": "schedule_child_subagents",
            "apply": True,
            "children": list(advice.get("suggested_children") or []),
        },
        "quality_hint": (
            "父任务还缺真实 QA 角色；先按 suggested_tool_call 创建 tester/bug_finder/acceptor，"
            "不要反复 read_file/read_artifact 读取产物正文来替代 QA 子代理。"
        ),
    }


# LLM: _dispatch_tool_call keeps suggested dispatch calls structurally identical across progress states.
# 函数用途: 生成建议模型复制的 dispatch_subagents 工具参数。
def _dispatch_tool_call(run_ids: list[str]) -> dict[str, object]:
    return {
        "tool": "dispatch_subagents",
        "apply": True,
        "execute_runners": True,
        "run_ids": run_ids,
        "workflow_mode": "off",
    }


# LLM: _attach_quality_advice exposes missing QA roles through dispatch, not only schedule dry-runs.
# 函数用途: 让父 runner 在实现 child ready 后直接看到 QA 缺口和候选 child specs，避免自己读正文猜验收流程。
def _attach_quality_advice(agent, parent_run_id: str, children: dict[str, object]) -> None:
    if children.get("needs_more_dispatch") or children.get("needs_recovery") or children.get("needs_repair_wave"):
        return
    try:
        parent = agent.subagents.load(parent_run_id)
    except Exception:
        return
    advice = qa_orchestration_advice(manager=agent.subagents, parent=parent, specs=[])
    if advice is None or advice.phase != "quality_wave_ready":
        return
    children["quality_advice"] = _quality_advice_payload(advice)


# LLM: _quality_advice_payload keeps QA suggestions compact and copyable for the next tool call.
# 函数用途: 把 QA advice dataclass 转成模型可读 JSON，不读取任何业务产物正文。
def _quality_advice_payload(advice) -> dict[str, object]:
    return {
        "phase": advice.phase,
        "llm_next_step": advice.llm_next_step,
        "guardrails": list(advice.guardrails),
        "suggested_roles": list(advice.suggested_roles),
        "suggested_children": [
            {
                "goal": item.goal,
                "agent_name": item.agent_name,
                "role": item.role,
                "acceptance_checks": list(item.acceptance_checks),
            }
            for item in advice.suggested_children
        ],
    }


# LLM: _progress_payload folds task statuses without expanding child artifacts.
# 函数用途: 只统计直接 child 的 id/status，保持工具响应小而可恢复。
def _progress_payload(parent_run_id: str, direct_children: list) -> dict[str, object]:
    by_status: dict[str, int] = {}
    planning_ids: list[str] = []
    running_ids: list[str] = []
    recovery_ids: list[str] = []
    rejected_ids: list[str] = []
    for item in direct_children:
        status = str(getattr(item, "status", "") or "UNKNOWN").upper()
        item_id = str(getattr(item, "id", "") or "")
        by_status[status] = by_status.get(status, 0) + 1
        if status == "PLANNING":
            planning_ids.append(item_id)
        if status == "RUNNING":
            running_ids.append(item_id)
        if _latest_acceptance_rejected(item):
            rejected_ids.append(item_id)
        if status in {"BLOCKED", "FAILED", "TIMEOUT", "CHANNEL_ERROR"} or item_id in rejected_ids:
            recovery_ids.append(item_id)
    unfinished_ids = [item for item in [*planning_ids, *running_ids] if item]
    recovery_ids = [item for item in recovery_ids if item]
    return {
        "direct_children": {
            "parent_run_id": parent_run_id,
            "total": len(direct_children),
            "by_status": by_status,
            "planning_run_ids": [item for item in planning_ids if item],
            "running_run_ids": [item for item in running_ids if item],
            "recovery_run_ids": recovery_ids,
            "rejected_acceptance_run_ids": [item for item in rejected_ids if item],
            "unfinished_run_ids": unfinished_ids,
            "needs_more_dispatch": bool(unfinished_ids),
            "needs_recovery": bool(recovery_ids),
            "ready_for_parent_acceptance": bool(direct_children) and not unfinished_ids and not recovery_ids,
        }
    }


# LLM: _recovery_child_tool_call suggests a flexible child recovery step without forcing a coordinator.
# 函数用途: 生成 refs-only 恢复 child 创建建议；真实创建仍必须由父 runner 自己调用 schedule_child_subagents。
def _recovery_child_tool_call(recovery_run_ids: list[str]) -> dict[str, object]:
    ids = [item for item in recovery_run_ids if item]
    joined_ids = ", ".join(ids)
    return {
        "tool": "schedule_child_subagents",
        "apply": True,
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
                    "subagent_board",
                    "read_file",
                    "list_files",
                ],
            }
        ],
    }


# LLM: _latest_acceptance_rejected makes rejected child acceptance visible as recovery work.
# 函数用途: 读取 child 本地验收报告；如果最新验收为 REJECT，父级 dispatch payload 不再提示直接收口。
def _latest_acceptance_rejected(item) -> bool:
    reports_dir = str(getattr(item, "reports_dir", "") or "")
    if not reports_dir:
        return False
    path = Path(reports_dir) / "acceptance_review.json"
    if not path.exists():
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return str(payload.get("decision") or "").upper() == "REJECT"
