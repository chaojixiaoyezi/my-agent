# LLM: 派工记录只投影结构化准入/执行事实；坏历史记录不得假称已应用或把自然语言指令当拒绝门。
# 模块用途: 生成批次的预演、准入拒绝与执行提示，状态修改仍归原生命周期写门。
from __future__ import annotations

from ....subagents.services.dispatch.params import DispatchRecordParams
from ....subagents.services.recovery.modes import RecoveryMode
from .conversation_lifecycle_gate import ConversationLifecycleDecision
from .params import DispatchContext


# LLM: 调用方已按纯计数或类型化冲突拒绝；本函数只构造原DispatchRecord，action为机器可读人工复核模式，不写任务。
# 函数用途: 为坏恢复历史返回未应用的明确拒绝记录，保留原run和状态，不取消或清零任务。
def recovery_history_blocked_runner_record(agent, ctx: DispatchContext, task: object):
    return agent.subagents.dispatch.make_dispatch_record(
        params=DispatchRecordParams(
            step="runner_selection",
            action=RecoveryMode.MANUAL_REVIEW_UNKNOWN_HISTORY.value,
            run_id=str(task.id), dry_run=ctx.preview_only, applied=False, ok=False,
            message="恢复历史未知或损坏，已拒绝接纳；请人工复核原canonical，不会预留或激活新执行轮。",
            before_status=str(task.status), after_status=str(task.status),
            evidence_paths=[str(task.agent_run_checkpoint_json)],
        ),
    )


def collaboration_candidate_load_error_record(agent, ctx: DispatchContext, load_errors: list[dict[str, object]]):
    return agent.subagents.dispatch.make_dispatch_record(
        params=DispatchRecordParams(
            step="collaboration_candidates",
            action="candidate_scan_load_error",
            dry_run=ctx.preview_only,
            applied=False,
            ok=True,
            message=(
                "协作候选扫描有账本读取错误；这不是没有待响应协作请求，"
                "请结合 load_errors 刷新协作状态或按 run_id 检查对应代理。"
            ),
            collaboration_candidate_load_errors=load_errors[:20],
        ),
    )


def dry_runner_record(agent, ctx: DispatchContext, runner_snapshot: tuple[object, object, str]):
    task, before, retry_reason = runner_snapshot
    return agent.subagents.dispatch.make_dispatch_record(
        params=DispatchRecordParams(
            step="runner",
            action="retry_runner" if retry_reason else "execute_runner",
            run_id=task.id,
            dry_run=True,
            applied=False,
            ok=True,
            message=dry_runner_message(retry_reason),
            before_status=before.status,
            after_status=before.status,
            before_verification_status=before.verification_status,
            after_verification_status=before.verification_status,
            evidence_paths=[before.task_dir],
        ),
    )


def conversation_lifecycle_gate_record(
    agent,
    ctx: DispatchContext,
    task: object,
    decision: ConversationLifecycleDecision,
):
    return agent.subagents.dispatch.make_dispatch_record(
        params=DispatchRecordParams(
            step="runner_selection",
            action="conversation_lifecycle_blocked",
            run_id=str(getattr(task, "id", "") or ""),
            dry_run=ctx.preview_only,
            applied=False,
            ok=False,
            message=str(decision.payload()),
            before_status=str(getattr(task, "status", "") or ""),
            after_status=str(getattr(task, "status", "") or ""),
            evidence_paths=[str(getattr(task, "task_dir", "") or "")],
        ),
    )


def dry_runner_message(retry_reason: str) -> str:
    if retry_reason:
        return f"dry-run: 将重试 runner（{retry_reason}）。"
    return "dry-run: apply 时会生成执行上下文；带 --start-runners 时会调用模型。"


def multi_runner_instruction_record(agent, ctx: DispatchContext, evidence_paths: list[str]):
    return agent.subagents.dispatch.make_dispatch_record(
        params=DispatchRecordParams(
            step="runner_instruction",
            action="ignore_multi_runner_instruction",
            dry_run=ctx.preview_only,
            applied=False,
            ok=True,
            message=(
                "已忽略本轮共享 runner_instruction：同一次 dispatch 选中了多个 runner，"
                "为避免某个子任务专属提示污染其他分支，请改为分别 dispatch 单个 run_id，"
                "或把通用要求写入每个 child goal/context bundle。"
            ),
            evidence_paths=evidence_paths[:20],
        ),
    )
