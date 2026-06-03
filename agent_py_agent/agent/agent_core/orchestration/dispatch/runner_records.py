
from __future__ import annotations

from ....subagents.services.dispatch.params import DispatchRecordParams
from .params import DispatchContext


def collaboration_candidate_load_error_record(agent, ctx: DispatchContext, load_errors: list[dict[str, object]]):
    return agent.subagents.make_dispatch_record(
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
    return agent.subagents.make_dispatch_record(
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


def dry_runner_message(retry_reason: str) -> str:
    if retry_reason:
        return f"dry-run: 将重试 runner（{retry_reason}）。"
    return "dry-run: apply 时会生成执行上下文；带 --start-runners 时会调用模型。"


def multi_runner_instruction_record(agent, ctx: DispatchContext, evidence_paths: list[str]):
    return agent.subagents.make_dispatch_record(
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
