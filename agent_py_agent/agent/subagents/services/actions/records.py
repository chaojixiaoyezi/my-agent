
from __future__ import annotations

"""Action apply record and log helpers."""

import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...policies import (
    ACTION_CLASSIFY_BLOCKER,
    ACTION_INSPECT_CHANNEL_PROBE,
    ACTION_INSPECT_FAILURE,
    ACTION_PROBE_OR_REPAIR_CHANNEL,
    ACTION_RECOVER_CHILD_AFTER_PARENT_TIMEOUT,
    ACTION_RECOVER_COORDINATOR_LEADERSHIP,
    ACTION_REPAIR_WORK_ORDER,
    ACTION_ROUTE_CAPABILITY_REQUEST,
    ACTION_STOP_NO_PROGRESS_AND_ESCALATE,
    ACTION_TAKEOVER_OR_REASSIGN,
    ACTION_TRIAGE_CAPABILITY_GAP,
)
from ..indexing.records import LocalRecordParams
from ..rescue_policy import action_rescue_record_fields
from .handlers import (
    ActionHandlerContext,
    apply_probe_or_repair_channel,
    apply_record_only_action,
    apply_repair_work_order,
    apply_stop_no_progress_and_escalate,
)
from .leadership import apply_recover_coordinator_leadership
from .takeover import apply_takeover_or_reassign

if TYPE_CHECKING:
    from ...models import SubAgentTask
    from ...reports import ActionApplyRecord, ActionPlanItem


ACTION_DISPATCH = {
    ACTION_PROBE_OR_REPAIR_CHANNEL: apply_probe_or_repair_channel,
    ACTION_INSPECT_CHANNEL_PROBE: apply_probe_or_repair_channel,
    ACTION_REPAIR_WORK_ORDER: apply_repair_work_order,
    ACTION_TAKEOVER_OR_REASSIGN: apply_takeover_or_reassign,
    ACTION_RECOVER_COORDINATOR_LEADERSHIP: apply_recover_coordinator_leadership,
    ACTION_RECOVER_CHILD_AFTER_PARENT_TIMEOUT: apply_record_only_action,
    ACTION_ROUTE_CAPABILITY_REQUEST: apply_record_only_action,
    ACTION_TRIAGE_CAPABILITY_GAP: apply_record_only_action,
    ACTION_INSPECT_FAILURE: apply_record_only_action,
    ACTION_CLASSIFY_BLOCKER: apply_record_only_action,
    ACTION_STOP_NO_PROGRESS_AND_ESCALATE: apply_stop_no_progress_and_escalate,
}


@dataclass(frozen=True)
class ActionApplyOptions:
    """User-selected filters and apply flags for action execution."""

    apply: bool = False
    action_filter: str = ""
    run_id: str = ""
    take_over_by: str = ""
    locked_files: list[str] | None = None
    limit: int = 0
    root_id: str = ""
    include_run_ids: list[str] | None = None
    exclude_run_ids: list[str] | None = None

    @classmethod
    def from_values(
        cls,
        options: ActionApplyOptions | None = None,
        *,
        apply: bool | None = None,
        action_filter: str | None = None,
        run_id: str | None = None,
        take_over_by: str | None = None,
        locked_files: list[str] | None = None,
        limit: int | None = None,
        root_id: str | None = None,
        include_run_ids: list[str] | None = None,
        exclude_run_ids: list[str] | None = None,
    ):
        base = options or cls()
        updates = {
            "apply": apply,
            "action_filter": action_filter,
            "run_id": run_id,
            "take_over_by": take_over_by,
            "locked_files": locked_files,
            "limit": limit,
            "root_id": root_id,
            "include_run_ids": include_run_ids,
            "exclude_run_ids": exclude_run_ids,
        }
        clean = {key: value for key, value in updates.items() if value is not None}
        return replace(base, **clean)


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


def action_apply_summary(records: list[ActionApplyRecord]) -> dict[str, int]:
    summary: dict[str, int] = {"total": len(records)}
    for record in records:
        summary[record.action] = summary.get(record.action, 0) + 1
        status_key = "ok" if record.ok else "failed"
        mode_key = "applied" if record.applied else "dry_run"
        summary[status_key] = summary.get(status_key, 0) + 1
        summary[mode_key] = summary.get(mode_key, 0) + 1
    return summary


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


def missing_task_action_record(ctx: ActionRecordContext) -> ActionApplyRecord:
    from ...reports import ActionApplyRecord

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


def dry_run_action_record(ctx: ActionRecordContext) -> ActionApplyRecord:
    from ...reports import ActionApplyRecord

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


def unsupported_action_record(ctx: ActionRecordContext) -> ActionApplyRecord:
    from ...reports import ActionApplyRecord

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


# LLM: 动作日志走私有写（0600/0700）；JSONL 行格式不变。
# 函数用途: 把一条动作应用记录追加进 JSONL 日志并补齐 Markdown 头。
def append_action_apply_log(manager: Any, record: ActionApplyRecord) -> None:
    from ....common.json_io import (
        append_private_jsonl_records,
        append_private_text,
        write_private_text_file_atomic,
    )

    jsonl = manager.workspace / "subagent_action_apply_log.jsonl"
    # 动作应用账属宿主运行数据：私有追加（0600/0700），存量宽权限文件下次写入即收紧；内容逐字节不变。
    append_private_jsonl_records(jsonl, [asdict(record)], sort_keys=False)

    markdown = manager.workspace / "ACTION_APPLY_LOG.md"
    if not markdown.exists():
        write_private_text_file_atomic(markdown, "# ACTION APPLY LOG\n\n")
    status = "OK" if record.ok else "FAIL"
    append_private_text(
        markdown,
        f"- [{status}] {record.id} run={record.run_id} action={record.action} "
        f"applied={record.applied} message={record.message}\n",
    )
    manager.indexing.index_action_apply(record)


# LLM: work log 是宿主运行数据：目录缺失按 0700 新建（pdp），文件私有写（0600）；已有内容只追加。
# 函数用途: 向子代理任务的 WORK_LOG 追加一行带时间戳的进展。
def append_task_work_log(manager: Any, task: SubAgentTask, message: str) -> None:
    from ....common.json_io import append_private_text, write_private_text_file_atomic

    path = Path(task.work_log_file)
    # 目录缺失时按 0700 新建（pdp 2026-10-03：私有写只动自己建的东西；已存在的目录一律不动）。
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.exists():
        # 子代理工作日志属宿主运行数据：私有写（0600/0700），存量宽权限文件下次写入即收紧；内容逐字节不变。
        write_private_text_file_atomic(path, "# WORK_LOG\n\n")
    append_private_text(path, f"- {time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    manager.indexing.log_local_record(
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
