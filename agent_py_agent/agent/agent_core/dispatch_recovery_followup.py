# LLM: Dispatch recovery follow-up closes runner-failure -> action-apply -> replacement-run loop.
# 模块用途: runner 在同一轮 dispatch 里超时/阻塞后，自动应用恢复动作并推进新 takeover/repair run 一次。

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..subagents.services.recovery_strategy import (
    SubagentRecoveryStrategyRequest,
    build_subagent_recovery_strategy,
)
from .dispatch_mixin_helpers import DispatchRunnerStageRequest, run_dispatch_runner_stage
from .dispatch_record_params import ActionApplyRecordParams
from .dispatch_service import make_action_apply_records

_RECOVERY_ACTIONS = {
    "takeover_or_reassign",
    "create_repair_child_from_artifact_integrity_refs",
}


# LLM: PostRunnerRecoveryFollowupParams keeps the bounded recovery pass explicit.
# 类用途: 保存 runner 失败后恢复闭环需要的 agent、上下文、参数和已有 dispatch records。
@dataclass(frozen=True)
class PostRunnerRecoveryFollowupParams:
    agent: Any
    ctx: Any
    params: Any
    records: list


# LLM: run_post_runner_recovery_followup runs one safe recovery wave after runner failures.
# 函数用途: 真实执行中 runner 刚失败时，自动 apply takeover/repair action，并让新 run 接着跑一次。
def run_post_runner_recovery_followup(params: PostRunnerRecoveryFollowupParams) -> list:
    records = list(params.records)
    if not _should_follow_up(params, records):
        return records
    continuable_run_id, continuable_instruction = _continuable_failed_runner(params, records)
    if continuable_run_id:
        params.ctx.include_run_ids = [continuable_run_id]
        params.ctx.runner_instruction = _with_instruction(params, continuable_instruction)
        return run_dispatch_runner_stage(
            request=DispatchRunnerStageRequest(params.agent, params.ctx, params.params, records)
        )
    action_records = make_action_apply_records(
        ActionApplyRecordParams(
            params.agent,
            params.ctx.cfg,
            True,
            params.ctx.take_over_by,
            params.ctx.locked_files,
            params.ctx.limit,
            params.ctx.root_id,
            params.ctx.include_run_ids,
            params.ctx.exclude_run_ids,
        )
    )
    records.extend(action_records)
    if not _created_recovery_work(action_records):
        return records
    params.ctx.runner_instruction = _with_recovery_followup_instruction(params)
    _include_recovery_run_ids(params, action_records)
    return run_dispatch_runner_stage(
        request=DispatchRunnerStageRequest(params.agent, params.ctx, params.params, records)
    )


# LLM: _should_follow_up limits recovery to real runner execution failures.
# 函数用途: dry-run、纯计划或没有 runner 失败的调度不额外调用模型。
def _should_follow_up(params: PostRunnerRecoveryFollowupParams, records: list) -> bool:
    return bool(params.params.apply and params.params.execute_runners and _has_failed_runner(records))


# LLM: _has_failed_runner detects the exact same-machine failure point that action recovery can handle.
# 函数用途: 只在 runner/acceptance 已经写出失败记录后触发恢复闭环。
def _has_failed_runner(records: list) -> bool:
    return any(
        str(getattr(item, "step", "") or "") in {"runner", "acceptance"}
        and not bool(getattr(item, "ok", True))
        for item in records
    )


# LLM: _created_recovery_work identifies actions that create or reuse concrete recovery workers.
# 函数用途: 只有 takeover/repair 这类产生可继续执行 run 的 action 才触发第二次 runner stage。
def _created_recovery_work(records: list) -> bool:
    return any(
        str(getattr(item, "step", "") or "") == "action_apply"
        and str(getattr(item, "action", "") or "") in _RECOVERY_ACTIONS
        and bool(getattr(item, "ok", False))
        and bool(getattr(item, "applied", False))
        for item in records
    )


# LLM: _continuable_failed_runner is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _continuable_failed_runner(params: PostRunnerRecoveryFollowupParams, records: list) -> tuple[str, str]:
    for record in records:
        if str(getattr(record, "step", "") or "") != "runner" or bool(getattr(record, "ok", True)):
            continue
        run_id = str(getattr(record, "run_id", "") or "").strip()
        strategy = _recovery_strategy_for_run(params.agent, run_id)
        if str(getattr(strategy, "recommended_action", "") or "").startswith("rerun_original"):
            return run_id, str(getattr(strategy, "runner_instruction", "") or "")
    return "", ""


# LLM: _recovery_strategy_for_run is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _recovery_strategy_for_run(agent: Any, run_id: str) -> Any:
    if not run_id:
        return None
    try:
        task = agent.subagents.load(run_id)
    except Exception:
        return None
    return build_subagent_recovery_strategy(SubagentRecoveryStrategyRequest(task=task))


# LLM: _include_recovery_run_ids is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _include_recovery_run_ids(params: PostRunnerRecoveryFollowupParams, records: list) -> None:
    recovery_run_ids = _recovery_run_ids(params, records)
    if not recovery_run_ids:
        return
    params.ctx.include_run_ids = recovery_run_ids


# LLM: _recovery_run_ids is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _recovery_run_ids(params: PostRunnerRecoveryFollowupParams, records: list) -> list[str]:
    run_ids: list[str] = []
    for record in records:
        if not _is_applied_recovery_action(record):
            continue
        run_ids.extend(_string_list(getattr(record, "created_run_ids", []) or []))
        run_ids.extend(_string_list(getattr(record, "runner_created_child_ids", []) or []))
        run_ids.extend(_task_recovery_run_ids(params.agent, str(getattr(record, "run_id", "") or "")))
    return _unique_strings(run_ids)


# LLM: _is_applied_recovery_action is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _is_applied_recovery_action(record: Any) -> bool:
    return (
        str(getattr(record, "step", "") or "") == "action_apply"
        and str(getattr(record, "action", "") or "") in _RECOVERY_ACTIONS
        and bool(getattr(record, "ok", False))
        and bool(getattr(record, "applied", False))
    )


# LLM: _task_recovery_run_ids is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _task_recovery_run_ids(agent: Any, run_id: str) -> list[str]:
    if not run_id:
        return []
    try:
        task = agent.subagents.load(run_id)
    except Exception:
        return []
    return _string_list(
        [
            getattr(task, "takeover_by", ""),
            getattr(task, "final_owner", ""),
        ]
    )


# LLM: _string_list is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _string_list(values: Any) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple, set)):
        return []
    return [text for item in values if (text := str(item or "").strip())]


# LLM: _unique_strings is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _unique_strings(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


# LLM: _with_recovery_followup_instruction gives replacement workers continuation context.
# 函数用途: 告诉 takeover/repair run 读 checkpoint/continue refs 接续，不要从头乱猜。
def _with_recovery_followup_instruction(params: PostRunnerRecoveryFollowupParams) -> str:
    addition = (
        "上一轮 runner 失败后，父级已自动创建或复用了 takeover/repair run。"
        "本轮只推进这个恢复 run：先读取 takeover_source_refs、failure_handoff、checkpoint 或 artifact refs，"
        "按已有目标继续完成，不要重建无关任务。"
    )
    existing = str(getattr(params.ctx, "runner_instruction", "") or "").strip()
    return "\n\n".join(item for item in [existing, addition] if item)


# LLM: _with_instruction is part of this module's structured runtime path; keep callers and tests aligned before changing it.
# 函数用途: 完成本模块中的转换、校验或状态整理，供相邻流程继续使用。
def _with_instruction(params: PostRunnerRecoveryFollowupParams, instruction: str) -> str:
    existing = str(getattr(params.ctx, "runner_instruction", "") or "").strip()
    addition = str(instruction or "").strip()
    return "\n\n".join(item for item in [existing, addition] if item)
