
from __future__ import annotations

"""Action apply record and log helpers."""

import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..indexing.params import LocalRecordParams
from ..rescue_policy import action_rescue_record_fields
from .context import ActionHandlerContext
from .handlers import (
    apply_probe_or_repair_channel,
    apply_record_only_action,
    apply_reopen_for_evidence,
    apply_repair_work_order,
    apply_stop_no_progress_and_escalate,
)
from .leadership import apply_recover_coordinator_leadership
from .options import ActionApplyOptions
from .takeover import apply_takeover_or_reassign

if TYPE_CHECKING:
    from ...models import SubAgentTask
    from ...reports import ActionApplyRecord, ActionPlanItem


ACTION_DISPATCH = {
    "probe_or_repair_channel": apply_probe_or_repair_channel,
    "inspect_channel_probe": apply_probe_or_repair_channel,
    "repair_work_order": apply_repair_work_order,
    "reopen_for_evidence": apply_reopen_for_evidence,
    "takeover_or_reassign": apply_takeover_or_reassign,
    "recover_coordinator_leadership": apply_recover_coordinator_leadership,
    "recover_child_after_parent_timeout": apply_record_only_action,
    "route_capability_request": apply_record_only_action,
    "triage_capability_gap": apply_record_only_action,
    "inspect_failure": apply_record_only_action,
    "classify_blocker": apply_record_only_action,
    "stop_no_progress_and_escalate": apply_stop_no_progress_and_escalate,
}


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


def append_action_apply_log(manager: Any, record: ActionApplyRecord) -> None:
    from ....io import append_jsonl

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
