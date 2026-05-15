# LLM: Dispatch capability follow-up closes the request/grant/rerun loop after a runner asks for a tool.
# 模块用途: 当 runner 在同一轮 dispatch 里写出 capability_request 时，自动做一次授权路由并重跑，减少父级模型空转。

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .dispatch_mixin_helpers import DispatchRunnerStageRequest, run_dispatch_runner_stage
from .dispatch_record_params import CapabilityRouteRecordParams
from .dispatch_service import make_capability_route_records


# LLM: PostRunnerCapabilityFollowupParams bundles dispatch state for one safe follow-up pass.
# 类用途: 保存 dispatch 后置能力闭环需要的 agent、上下文、参数和已有记录；只允许一轮 route+rerun，防止无限循环。
@dataclass(frozen=True)
class PostRunnerCapabilityFollowupParams:
    agent: Any
    ctx: Any
    params: Any
    records: list


# LLM: run_post_runner_capability_followup routes new requests and reruns once when a grant was created.
# 函数用途: runner 刚产出 OPEN capability_request 后，自动路由授权并重跑候选任务；无授权或非真实执行时保持原记录。
def run_post_runner_capability_followup(params: PostRunnerCapabilityFollowupParams) -> list:
    records = list(params.records)
    if not _should_follow_up(params):
        return records
    route_records = make_capability_route_records(
        CapabilityRouteRecordParams(
            params.agent,
            params.ctx.router,
            params.ctx.cfg,
            True,
            params.ctx.limit,
        )
    )
    records.extend(route_records)
    if not _created_grant(route_records):
        return records
    params.ctx.runner_instruction = _with_capability_followup_instruction(params)
    return run_dispatch_runner_stage(
        request=DispatchRunnerStageRequest(params.agent, params.ctx, params.params, records)
    )


# LLM: _should_follow_up limits the feature to real apply+execute dispatch runs.
# 函数用途: dry-run、纯 route、纯 acceptance 或没有 runner 执行的场景不做后置重跑。
def _should_follow_up(params: PostRunnerCapabilityFollowupParams) -> bool:
    return bool(params.params.apply and params.params.execute_runners)


# LLM: _created_grant keeps rerun tied to actual GRANTED records only.
# 函数用途: 只有能力路由真的生成 grant 时才重跑，GAP/无匹配/空记录都不触发额外模型调用。
def _created_grant(records: list) -> bool:
    return any(
        str(getattr(item, "step", "")) == "capability_route"
        and str(getattr(item, "action", "")).lower() == "granted"
        for item in records
    )


# LLM: _with_capability_followup_instruction turns a grant into concrete continuation guidance.
# 函数用途: 父级刚授权后，给同一轮重跑补一段通用续跑提示，避免子代理重新理解任务或继续卡在旧缺口。
def _with_capability_followup_instruction(params: PostRunnerCapabilityFollowupParams) -> str:
    addition = _capability_followup_instruction(params)
    existing = str(getattr(params.ctx, "runner_instruction", "") or "").strip()
    return "\n\n".join(item for item in [existing, addition] if item)


# LLM: _capability_followup_instruction stays refs-first and generic across code/doc/web tasks.
# 函数用途: 根据 GRANTED 记录和当前 task 快照生成短续跑说明，不读取大产物正文。
def _capability_followup_instruction(params: PostRunnerCapabilityFollowupParams) -> str:
    run_ids = _granted_run_ids(params.records)
    snippets = _granted_task_snippets(params.agent, run_ids)
    lines = [
        "父级已处理上一轮 capability_request；本轮是同一个 run 的授权后续跑。",
        "不要从头重做任务；先看当前 task 的 blockers、next_actions、artifact_refs 和已有文件，再继续完成缺失部分。",
        "如果获批了 write_file/append_file/replace_in_file，优先补齐或修复目标产物；完成后写标准 SUBAGENT_RESULT 或 execution_context.output_json。",
    ]
    if snippets:
        lines.extend(["", "授权后续跑上下文：", *snippets])
    return "\n".join(lines)


# LLM: _granted_run_ids extracts affected children from the existing dispatch records.
# 函数用途: 收集本轮能力授权影响的 run_id，供后续提示只描述相关子代理。
def _granted_run_ids(records: list) -> list[str]:
    run_ids: list[str] = []
    for item in records:
        if str(getattr(item, "step", "")) != "capability_route":
            continue
        if str(getattr(item, "action", "")).lower() != "granted":
            continue
        run_id = str(getattr(item, "run_id", "") or "").strip()
        if run_id and run_id not in run_ids:
            run_ids.append(run_id)
    return run_ids


# LLM: _granted_task_snippets summarizes only machine state needed for the next runner call.
# 函数用途: 从任务状态里抽取短 blocker/action/tool/path 线索，避免把产物正文塞回 prompt。
def _granted_task_snippets(agent: Any, run_ids: list[str]) -> list[str]:
    snippets: list[str] = []
    for run_id in run_ids[:5]:
        try:
            task = agent.subagents.load(run_id)
        except Exception:
            continue
        tools = sorted({tool for grant in getattr(task, "capability_grants", []) for tool in getattr(grant, "tools", [])})
        blockers = [str(item) for item in (getattr(task, "blockers", []) or []) if str(item).strip()]
        next_actions = _task_next_actions(task)
        paths = [str(item) for item in (getattr(task, "artifact_refs", []) or []) if str(item).strip()]
        snippets.append(
            f"- run_id={run_id}; granted_tools={','.join(tools[:8]) or 'none'}; "
            f"blockers={'; '.join(blockers[:2]) or 'none'}; "
            f"next_actions={'; '.join(next_actions[:2]) or 'none'}; "
            f"artifact_refs={'; '.join(paths[:3]) or 'none'}"
        )
    return snippets


# LLM: _task_next_actions reads the lightweight next_actions report when present.
# 函数用途: 复用 runner 输出的下一步建议；坏 JSON 或缺文件时退回空列表。
def _task_next_actions(task: Any) -> list[str]:
    import json
    from pathlib import Path

    path = str(getattr(task, "next_actions_json", "") or "").strip()
    if not path:
        return []
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return []
    values = payload.get("next_actions", []) if isinstance(payload, dict) else []
    return [str(item) for item in values if str(item or "").strip()]
