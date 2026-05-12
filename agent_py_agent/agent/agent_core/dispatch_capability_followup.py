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
