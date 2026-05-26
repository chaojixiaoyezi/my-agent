# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪和汇总子代理任务。

from __future__ import annotations

"""Action apply record and log helpers."""

import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .action_context import ActionHandlerContext
from .action_handlers import (
    apply_probe_or_repair_channel,
    apply_record_only_action,
    apply_reopen_for_evidence,
    apply_repair_work_order,
    apply_stop_no_progress_and_escalate,
)
from .action_leadership import apply_recover_coordinator_leadership
from .action_options import ActionApplyOptions
from .action_takeover import apply_takeover_or_reassign
from .indexing_params import LocalRecordParams
from .rescue_policy import action_rescue_record_fields

if TYPE_CHECKING:
    from ..models import SubAgentTask
    from ..reports import ActionApplyRecord, ActionPlanItem


# LLM: ACTION_DISPATCH is the only apply-action registry; new actions must stay explicit and audited.
# 函数用途: 把 action-plan 的动作名映射到受控 handler，避免字符串分发散落在服务层。
ACTION_DISPATCH = {
    "probe_or_repair_channel": apply_probe_or_repair_channel,
    "inspect_channel_probe": apply_probe_or_repair_channel,
    "repair_work_order": apply_repair_work_order,
    "reopen_for_evidence": apply_reopen_for_evidence,
    "takeover_or_reassign": apply_takeover_or_reassign,
    "recover_coordinator_leadership": apply_recover_coordinator_leadership,
    # LLM: parent-timeout child recovery is audit-only until an explicit handoff apply exists.
    "recover_child_after_parent_timeout": apply_record_only_action,
    "route_capability_request": apply_record_only_action,
    "triage_capability_gap": apply_record_only_action,
    "inspect_failure": apply_record_only_action,
    "classify_blocker": apply_record_only_action,
    "stop_no_progress_and_escalate": apply_stop_no_progress_and_escalate,
}


# LLM: ActionRecordContext 属于子代理服务层的类边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 类用途: 集中保存动作记录上下文字段，让调用方按同一参数包传递上下文；关键副作用: 本身不执行输入输出；字段变化会影响构造点、序列化和测试读取。
@dataclass(frozen=True)
class ActionRecordContext:
    manager: Any
    action: ActionPlanItem
    now: float
    before_status: str = ""
    before_channel_status: str = ""
    task: SubAgentTask | None = None
    opts: ActionApplyOptions | None = None
    exc: FileNotFoundError | None = None


# LLM: action_apply_summary 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 处理动作应用summary相关的数据流，连接当前职责的前后步骤；关键副作用: 会更新任务状态、报告记录和持久化副作用，需避免破坏既有状态机约定。
def action_apply_summary(records: list[ActionApplyRecord]) -> dict[str, int]:
    summary: dict[str, int] = {"total": len(records)}
    for record in records:
        summary[record.action] = summary.get(record.action, 0) + 1
        status_key = "ok" if record.ok else "failed"
        mode_key = "applied" if record.applied else "dry_run"
        summary[status_key] = summary.get(status_key, 0) + 1
        summary[mode_key] = summary.get(mode_key, 0) + 1
    return summary


# LLM: action_handler_context 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 处理动作handler上下文相关的数据流，连接当前职责的前后步骤；关键副作用: 会影响任务状态、报告记录和持久化副作用，需保持重试、超时和状态迁移语义。
def action_handler_context(
    opts: ActionApplyOptions,
    now: float,
    before_status: str,
    before_channel_status: str,
) -> ActionHandlerContext:
    return ActionHandlerContext(
        before_status=before_status,
        before_channel_status=before_channel_status,
        now=now,
        take_over_by=opts.take_over_by,
        locked_files=opts.locked_files or [],
    )


# LLM: missing_task_action_record 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 处理missing任务动作记录相关的数据流，连接当前职责的前后步骤；关键副作用: 会改动任务状态、报告记录和持久化副作用，调用方依赖写入顺序和文件格式。
def missing_task_action_record(ctx: ActionRecordContext) -> ActionApplyRecord:
    from ..reports import ActionApplyRecord

    return ActionApplyRecord(
        id=ctx.manager._new_id("apply"),
        action_id=ctx.action.id,
        run_id=ctx.action.run_id,
        action=ctx.action.action,
        dry_run=not ctx.opts.apply,
        applied=False,
        ok=False,
        message=str(ctx.exc),
        **action_rescue_record_fields(ctx.action),
        created_at=ctx.now,
    )


# LLM: dry_run_action_record 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 处理dryrun动作记录相关的数据流，连接当前职责的前后步骤；关键副作用: 会改动任务状态、报告记录和持久化副作用，调用方依赖写入顺序和文件格式。
def dry_run_action_record(ctx: ActionRecordContext) -> ActionApplyRecord:
    from ..reports import ActionApplyRecord

    return ActionApplyRecord(
        id=ctx.manager._new_id("apply"),
        action_id=ctx.action.id,
        run_id=ctx.action.run_id,
        action=ctx.action.action,
        dry_run=True,
        applied=False,
        ok=True,
        message=f"dry-run: would {ctx.action.action}",
        before_status=ctx.before_status,
        after_status=ctx.before_status,
        before_channel_status=ctx.before_channel_status,
        after_channel_status=ctx.before_channel_status,
        **action_rescue_record_fields(ctx.action),
        evidence_paths=[ctx.task.task_dir],
        created_at=ctx.now,
    )


# LLM: unsupported_action_record 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 处理unsupported动作记录相关的数据流，连接当前职责的前后步骤；关键副作用: 会改动任务状态、报告记录和持久化副作用，调用方依赖写入顺序和文件格式。
def unsupported_action_record(ctx: ActionRecordContext) -> ActionApplyRecord:
    from ..reports import ActionApplyRecord

    return ActionApplyRecord(
        id=ctx.manager._new_id("apply"),
        action_id=ctx.action.id,
        run_id=ctx.action.run_id,
        action=ctx.action.action,
        dry_run=False,
        applied=False,
        ok=False,
        message=f"暂不支持 apply 动作: {ctx.action.action}",
        before_status=ctx.before_status,
        after_status=ctx.before_status,
        before_channel_status=ctx.before_channel_status,
        after_channel_status=ctx.before_channel_status,
        evidence_paths=[ctx.task.task_dir],
        created_at=ctx.now,
    )


# LLM: append_action_apply_log 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 写入动作应用log的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、报告记录和持久化副作用，调用方依赖写入顺序和文件格式。
def append_action_apply_log(manager: Any, record: ActionApplyRecord) -> None:
    from ...file_io import append_jsonl

    jsonl = manager.workspace / "subagent_action_apply_log.jsonl"
    append_jsonl(jsonl, asdict(record))

    markdown = manager.workspace / "ACTION_APPLY_LOG.md"
    if not markdown.exists():
        markdown.write_text("# ACTION APPLY LOG\n\n", encoding="utf-8")
    with markdown.open("a", encoding="utf-8") as handle:
        status = "OK" if record.ok else "FAIL"
        handle.write(
            f"- [{status}] {record.id} run={record.run_id} action={record.action} "
            f"applied={record.applied} message={record.message}\n"
        )
    manager._index_action_apply(record)


# LLM: append_task_work_log 属于子代理服务层的函数边界；调整时先确认任务状态、报告记录和持久化副作用仍按原契约工作。
# 函数用途: 写入任务worklog的状态、日志或审计记录，保持持久化格式兼容；关键副作用: 会改动任务状态、报告记录和持久化副作用，调用方依赖写入顺序和文件格式。
def append_task_work_log(manager: Any, task: SubAgentTask, message: str) -> None:
    path = Path(task.work_log_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("# WORK_LOG\n\n", encoding="utf-8")
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"- {time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    manager._log_local_record(
        params=LocalRecordParams(
            source_type="subagent_work_log",
            source_id=f"{task.id}:{time.time():.6f}",
            title=f"Subagent work log {task.id}",
            content=f"{task.id}\n{task.goal}\n{message}",
            metadata={
                "run_id": task.id,
                "goal": task.goal,
                "status": task.status,
                "verification_status": task.verification_status,
                "work_log_file": task.work_log_file,
            },
            event_type="subagent_work_log_appended",
        ),
    )
