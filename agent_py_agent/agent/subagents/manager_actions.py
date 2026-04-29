from __future__ import annotations

"""LLM contract: SubAgentActionMixin methods grouped by one subagent responsibility.

Human version:
这个 mixin 是 SubAgentManager 的一块业务能力，不单独实例化。
拆成 mixin 是为了让每个文件只有一个变化原因，而不是把所有父代理逻辑塞进一个巨型文件。
"""

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from .models import *
from .reports import *
from .rendering import *
from .runner_rendering import *
from .runner_rendering import _render_runner_item_line
from .parsing import (
    _dict_list,
    _normalize_runner_items,
    _split_allowed_items,
    _string_dict,
    _string_list,
)
from .policies import (
    _action_for_issue,
    _capability_request_query,
    _commands_for_action,
    _dedupe_granted_cards,
    _default_forbidden_write_roots,
    _execution_context_instructions,
    _filter_action_plan_items,
    _is_active,
    _issue_weight,
    _make_due_issue,
    _risk_weight,
    _route_card_payload,
    _severity_weight,
    _runner_next_action,
    _select_capability_hits,
    _status_from_structured_output,
    _verification_from_runner_status,
)
from .probe import (
    _channel_status,
    _probe_fail,
    _probe_json_file,
    _probe_ok,
    _probe_writable_dir,
)
from .utils import (
    _apply_missing_paths,
    _apply_paths,
    _merge_list,
    _new_id,
    _read_json_object,
    _write_if_missing,
    _write_json_if_missing,
)
from ..capabilities import CapabilityRouter
from ..capability_config import CapabilityConfig
from ..file_io import append_jsonl

if TYPE_CHECKING:
    from ..local_store import LocalStore

class SubAgentActionMixin:
    def apply_actions(
        self,
        config: CapabilityConfig | None = None,
        *,
        apply: bool = False,
        action_filter: str = "",
        run_id: str = "",
        take_over_by: str = "",
        locked_files: list[str] | None = None,
        limit: int = 0,
    ) -> ActionApplyReport:
        """执行或 dry-run 执行动作计划。

        默认 `apply=False`，只产出会做什么。
        真正执行时只支持低风险动作，并把所有动作写入审计日志。
        """

        plan = self.plan_actions(config)
        actions = _filter_action_plan_items(
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
                "ok" if record.ok else "failed",
                0,
            ) + 1
            summary["applied" if record.applied else "dry_run"] = summary.get(
                "applied" if record.applied else "dry_run",
                0,
            ) + 1
        return ActionApplyReport(
            generated_at=time.time(),
            dry_run=not apply,
            summary=summary,
            records=records,
        )

    def write_action_apply_report(
        self,
        config: CapabilityConfig | None = None,
        *,
        apply: bool = False,
        action_filter: str = "",
        run_id: str = "",
        take_over_by: str = "",
        locked_files: list[str] | None = None,
        limit: int = 0,
    ) -> ActionApplyReport:
        """写出 action apply 报告。"""

        report = self.apply_actions(
            config,
            apply=apply,
            action_filter=action_filter,
            run_id=run_id,
            take_over_by=take_over_by,
            locked_files=locked_files or [],
            limit=limit,
        )
        (self.workspace / "subagent_action_apply_report.json").write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (self.workspace / "SUBAGENT_ACTION_APPLY.md").write_text(
            render_action_apply_markdown(report),
            encoding="utf-8",
        )
        self._index_report(
            "subagent_action_apply_report",
            "latest",
            "Subagent action apply report",
            report,
            event_type="subagent_action_apply_report_written",
        )
        return report

    def _apply_action_item(
        self,
        action: ActionPlanItem,
        *,
        apply: bool,
        take_over_by: str,
        locked_files: list[str],
    ) -> ActionApplyRecord:
        """执行单条 action plan item。"""

        now = time.time()
        try:
            task = self.load(action.run_id)
        except FileNotFoundError as exc:
            return ActionApplyRecord(
                id=_new_id("apply"),
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
                id=_new_id("apply"),
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
            result = self.probe_channel(action.run_id)
            task = self.load(action.run_id)
            return ActionApplyRecord(
                id=_new_id("apply"),
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
            self.save(task)
            validation = self.validate_work_order(action.run_id)
            task = self.load(action.run_id)
            ok = validation.ok
            message = "已补齐标准工单现场。" if ok else f"工单仍缺少 {len(validation.missing)} 个路径。"
            self._append_task_work_log(task, f"action_apply repair_work_order: {message}")
            return ActionApplyRecord(
                id=_new_id("apply"),
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
            self.save(task)
            self._append_task_work_log(task, "action_apply reopen_for_evidence: 已重开任务并等待验收证据。")
            return self._record_after_task_action(
                action,
                task,
                before_status,
                before_channel_status,
                "已把缺证据的 DONE 任务改为 BLOCKED。",
            )

        if action.action == "run_acceptance":
            task.verification_status = "NEEDS_ACCEPTANCE"
            task.updated_at = now
            self.save(task)
            self._append_task_work_log(task, "action_apply run_acceptance: 已标记为需要验收。")
            return self._record_after_task_action(
                action,
                task,
                before_status,
                before_channel_status,
                "已标记为需要验收，未自动执行未知命令。",
            )

        if action.action == "takeover_or_reassign":
            if not take_over_by:
                return ActionApplyRecord(
                    id=_new_id("apply"),
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
                self.probe_channel(action.run_id)
                task = self.load(action.run_id)
            if task.channel_status != "OK":
                return ActionApplyRecord(
                    id=_new_id("apply"),
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
            self.record_takeover(
                action.run_id,
                take_over_by=take_over_by,
                reason=action.reason,
                locked_files=locked_files,
            )
            task = self.load(action.run_id)
            self._append_task_work_log(task, f"action_apply takeover_or_reassign: 已由 {take_over_by} 接管。")
            return self._record_after_task_action(
                action,
                task,
                before_status,
                before_channel_status,
                f"已由 {take_over_by} 接管任务。",
                evidence_paths=[task.takeover_file, task.work_log_file],
            )

        if action.action in {
            "route_capability_request",
            "triage_capability_gap",
            "inspect_failure",
            "classify_blocker",
        }:
            task.updated_at = now
            self.save(task)
            self._append_task_work_log(task, f"action_apply {action.action}: 已记录待人工处理，不自动修改能力授权。")
            return self._record_after_task_action(
                action,
                task,
                before_status,
                before_channel_status,
                f"已记录 {action.action} 待人工处理。",
            )

        return ActionApplyRecord(
            id=_new_id("apply"),
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
        """创建修改任务后的 apply 记录。"""

        return ActionApplyRecord(
            id=_new_id("apply"),
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
        """写入全局 action apply 审计日志。"""

        jsonl = self.workspace / "subagent_action_apply_log.jsonl"
        append_jsonl(jsonl, asdict(record))

        markdown = self.workspace / "ACTION_APPLY_LOG.md"
        if not markdown.exists():
            markdown.write_text("# ACTION APPLY LOG\n\n", encoding="utf-8")
        with markdown.open("a", encoding="utf-8") as handle:
            status = "OK" if record.ok else "FAIL"
            handle.write(
                f"- [{status}] {record.id} run={record.run_id} action={record.action} "
                f"applied={record.applied} message={record.message}\n"
            )
        self._index_action_apply(record)

    def _append_task_work_log(self, task: SubAgentTask, message: str) -> None:
        """把 apply 过程写入任务自己的 WORK_LOG。"""

        path = Path(task.work_log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text("# WORK_LOG\n\n", encoding="utf-8")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"- {time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
        self._log_local_record(
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

