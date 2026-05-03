from __future__ import annotations

"""LLM: action apply service for subagent tasks.

给人看的解释：
这里承接动作执行逻辑（apply_actions, _apply_action_item 等）。
SubAgentManager 通过 facade 方法委托到这里。
"""

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..models import ActionApplyRecord, ActionPlanItem, SubAgentTask
    from ..reports import ActionApplyRecord


class SubAgentActionService:
    """Action apply execution service."""

    def __init__(self, manager: Any):
        self.manager = manager

    def apply_actions(
        self,
        config: Any = None,
        *,
        apply: bool = False,
        action_filter: str = "",
        run_id: str = "",
        take_over_by: str = "",
        locked_files: list[str] | None = None,
        limit: int = 0,
    ) -> ActionApplyReport:
        """Execute or dry-run an action plan."""
        from ..reports import ActionApplyReport

        plan = self.manager.plan_actions(config)
        actions = self.manager._filter_action_plan_items(
            plan.actions,
            action_filter=action_filter,
            run_id=run_id,
            limit=limit,
        )
        records: list[ActionApplyRecord] = []
        for action in actions:
            record = self._apply_action_item(
                action,
                apply=apply,
                take_over_by=take_over_by,
                locked_files=locked_files or [],
            )
            records.append(record)
            if apply:
                self._append_action_apply_log(record)

        summary: dict[str, int] = {"total": len(records)}
        for record in records:
            summary[record.action] = summary.get(record.action, 0) + 1
            summary["ok" if record.ok else "failed"] = summary.get(
                "ok" if record.ok else "failed", 0,
            ) + 1
            summary["applied" if record.applied else "dry_run"] = summary.get(
                "applied" if record.applied else "dry_run", 0,
            ) + 1
        return ActionApplyReport(
            generated_at=time.time(),
            dry_run=not apply,
            summary=summary,
            records=records,
        )

    def _apply_action_item(
        self,
        action: ActionPlanItem,
        *,
        apply: bool,
        take_over_by: str,
        locked_files: list[str],
    ) -> ActionApplyRecord:
        """Execute a single action plan item."""
        from ..reports import ActionApplyRecord

        now = time.time()
        try:
            task = self.manager.load(action.run_id)
        except FileNotFoundError as exc:
            return ActionApplyRecord(
                id=self.manager._new_id("apply"),
                action_id=action.id,
                run_id=action.run_id,
                action=action.action,
                dry_run=not apply,
                applied=False,
                ok=False,
                message=str(exc),
                created_at=now,
            )

        before_status = task.status
        before_channel_status = task.channel_status
        if not apply:
            return ActionApplyRecord(
                id=self.manager._new_id("apply"),
                action_id=action.id,
                run_id=action.run_id,
                action=action.action,
                dry_run=True,
                applied=False,
                ok=True,
                message=f"dry-run: would {action.action}",
                before_status=before_status,
                after_status=before_status,
                before_channel_status=before_channel_status,
                after_channel_status=before_channel_status,
                evidence_paths=[task.task_dir],
                created_at=now,
            )

        if action.action in {"probe_or_repair_channel", "inspect_channel_probe"}:
            result = self.manager.probe_channel(action.run_id)
            task = self.manager.load(action.run_id)
            return ActionApplyRecord(
                id=self.manager._new_id("apply"),
                action_id=action.id,
                run_id=action.run_id,
                action=action.action,
                dry_run=False,
                applied=True,
                ok=True,
                message=f"已执行 channel probe，结果为 {result.channel_status}。",
                before_status=before_status,
                after_status=task.status,
                before_channel_status=before_channel_status,
                after_channel_status=task.channel_status,
                evidence_paths=[task.channel_probe_file, str(Path(task.logs_dir) / "last_channel_probe.json")],
                created_at=now,
            )

        if action.action == "repair_work_order":
            self.manager.save(task)
            validation = self.manager.validate_work_order(action.run_id)
            task = self.manager.load(action.run_id)
            ok = validation.ok
            message = "已补齐标准工单现场。" if ok else f"工单仍缺少 {len(validation.missing)} 个路径。"
            self._append_task_work_log(task, f"action_apply repair_work_order: {message}")
            return ActionApplyRecord(
                id=self.manager._new_id("apply"),
                action_id=action.id,
                run_id=action.run_id,
                action=action.action,
                dry_run=False,
                applied=ok,
                ok=ok,
                message=message,
                before_status=before_status,
                after_status=task.status,
                before_channel_status=before_channel_status,
                after_channel_status=task.channel_status,
                evidence_paths=[task.task_dir, task.work_log_file],
                created_at=now,
            )

        if action.action == "reopen_for_evidence":
            task.status = "BLOCKED"
            task.failure_type = "missing_evidence"
            task.verification_status = "UNVERIFIED"
            task.updated_at = now
            task.result = task.result or "缺少验收证据，等待补充 evidence 后再完成。"
            self.manager.save(task)
            self._append_task_work_log(task, "action_apply reopen_for_evidence: 已重开任务并等待验收证据。")
            return self._record_after_task_action(
                action, task, before_status, before_channel_status,
                "已把缺证据的 DONE 任务改为 BLOCKED。",
            )

        if action.action == "run_acceptance":
            task.verification_status = "NEEDS_ACCEPTANCE"
            task.updated_at = now
            self.manager.save(task)
            self._append_task_work_log(task, "action_apply run_acceptance: 已标记为需要验收。")
            return self._record_after_task_action(
                action, task, before_status, before_channel_status,
                "已标记为需要验收，未自动执行未知命令。",
            )

        if action.action == "takeover_or_reassign":
            if not take_over_by:
                return ActionApplyRecord(
                    id=self.manager._new_id("apply"),
                    action_id=action.id,
                    run_id=action.run_id,
                    action=action.action,
                    dry_run=False,
                    applied=False,
                    ok=False,
                    message="takeover_or_reassign 需要 --take-over-by。",
                    before_status=before_status,
                    after_status=before_status,
                    before_channel_status=before_channel_status,
                    after_channel_status=before_channel_status,
                    evidence_paths=[task.task_dir],
                    created_at=now,
                )
            if task.channel_status != "OK":
                self.manager.probe_channel(action.run_id)
                task = self.manager.load(action.run_id)
            if task.channel_status != "OK":
                return ActionApplyRecord(
                    id=self.manager._new_id("apply"),
                    action_id=action.id,
                    run_id=action.run_id,
                    action=action.action,
                    dry_run=False,
                    applied=False,
                    ok=False,
                    message=f"通道状态为 {task.channel_status}，未接管。请先修复通道。",
                    before_status=before_status,
                    after_status=task.status,
                    before_channel_status=before_channel_status,
                    after_channel_status=task.channel_status,
                    evidence_paths=[task.channel_probe_file],
                    created_at=now,
                )
            self.manager.record_takeover(
                action.run_id,
                take_over_by=take_over_by,
                reason=action.reason,
                locked_files=locked_files,
            )
            task = self.manager.load(action.run_id)
            self._append_task_work_log(task, f"action_apply takeover_or_reassign: 已由 {take_over_by} 接管。")
            return self._record_after_task_action(
                action, task, before_status, before_channel_status,
                f"已由 {take_over_by} 接管任务。",
                evidence_paths=[task.takeover_file, task.work_log_file],
            )

        if action.action in {"route_capability_request", "triage_capability_gap", "inspect_failure", "classify_blocker"}:
            task.updated_at = now
            self.manager.save(task)
            self._append_task_work_log(task, f"action_apply {action.action}: 已记录待人工处理，不自动修改能力授权。")
            return self._record_after_task_action(
                action, task, before_status, before_channel_status,
                f"已记录 {action.action} 待人工处理。",
            )

        return ActionApplyRecord(
            id=self.manager._new_id("apply"),
            action_id=action.id,
            run_id=action.run_id,
            action=action.action,
            dry_run=False,
            applied=False,
            ok=False,
            message=f"暂不支持 apply 动作: {action.action}",
            before_status=before_status,
            after_status=before_status,
            before_channel_status=before_channel_status,
            after_channel_status=before_channel_status,
            evidence_paths=[task.task_dir],
            created_at=now,
        )

    def _record_after_task_action(
        self,
        action: ActionPlanItem,
        task: SubAgentTask,
        before_status: str,
        before_channel_status: str,
        message: str,
        *,
        evidence_paths: list[str] | None = None,
    ) -> ActionApplyRecord:
        """Create an apply record after task modification."""
        from ..reports import ActionApplyRecord

        return ActionApplyRecord(
            id=self.manager._new_id("apply"),
            action_id=action.id,
            run_id=action.run_id,
            action=action.action,
            dry_run=False,
            applied=True,
            ok=True,
            message=message,
            before_status=before_status,
            after_status=task.status,
            before_channel_status=before_channel_status,
            after_channel_status=task.channel_status,
            evidence_paths=evidence_paths or [task.work_log_file],
            created_at=time.time(),
        )

    def _append_action_apply_log(self, record: ActionApplyRecord) -> None:
        """Write global action apply audit log."""
        from dataclasses import asdict

        from ...file_io import append_jsonl

        jsonl = self.manager.workspace / "subagent_action_apply_log.jsonl"
        append_jsonl(jsonl, asdict(record))

        markdown = self.manager.workspace / "ACTION_APPLY_LOG.md"
        if not markdown.exists():
            markdown.write_text("# ACTION APPLY LOG\n\n", encoding="utf-8")
        with markdown.open("a", encoding="utf-8") as handle:
            status = "OK" if record.ok else "FAIL"
            handle.write(
                f"- [{status}] {record.id} run={record.run_id} action={record.action} "
                f"applied={record.applied} message={record.message}\n"
            )
        self.manager._index_action_apply(record)

    def _append_task_work_log(self, task: SubAgentTask, message: str) -> None:
        """Write apply progress to task's own WORK_LOG."""
        from dataclasses import asdict

        path = Path(task.work_log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text("# WORK_LOG\n\n", encoding="utf-8")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"- {time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
        self.manager._log_local_record(
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
        )