from __future__ import annotations

"""LLM contract: SubAgentBoardMixin methods grouped by one subagent responsibility.

Human version:
这个 mixin 是 SubAgentManager 的一块业务能力，不单独实例化。
拆成 mixin 是为了让每个文件只有一个变化原因，而不是把所有父代理逻辑塞进一个巨型文件。
"""

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from .models import SubAgentTask
from .reports import ActionPlanItem, ActionPlanReport, DueCheckIssue, DueCheckReport, SubAgentBoard, SubAgentBoardItem
from .rendering import render_action_plan_markdown, render_board_markdown, render_due_check_markdown
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

class SubAgentBoardMixin:
    def build_board(self, *, recent_limit: int = 20) -> SubAgentBoard:
        """构建子代理红绿灯看板。"""

        items = [self._to_board_item(task) for task in self.list_runs()]
        summary: dict[str, int] = {"total": len(items)}
        for item in items:
            summary[item.status] = summary.get(item.status, 0) + 1
            summary[item.verification_status] = summary.get(item.verification_status, 0) + 1
            summary[f"channel_{item.channel_status}"] = (
                summary.get(f"channel_{item.channel_status}", 0) + 1
            )
        hot_list = [item for item in items if item.risk_flags]
        hot_list.sort(key=lambda item: (-_risk_weight(item.risk_flags), -(item.updated_at or 0)))
        recent = items[:recent_limit]
        return SubAgentBoard(
            generated_at=time.time(),
            summary=summary,
            hot_list=hot_list,
            recent=recent,
            items=items,
        )

    def write_board(self, *, recent_limit: int = 20) -> SubAgentBoard:
        """写出机器 JSON 和人类 Markdown 看板。"""

        board = self.build_board(recent_limit=recent_limit)
        (self.workspace / "subagent_board.json").write_text(
            json.dumps(asdict(board), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (self.workspace / "SUBAGENT_BOARD.md").write_text(
            render_board_markdown(board),
            encoding="utf-8",
        )
        return board

    def due_check(self, config: CapabilityConfig | None = None) -> DueCheckReport:
        """巡检所有子代理运行，找出需要父代理介入的事项。

        它只产出判断和建议，不会自动接管或重派。
        这样可以先把“应该处理谁”做准，再把自动动作接上去。
        """

        cfg = config or CapabilityConfig()
        now = time.time()
        heartbeat_timeout = cfg.subagent_heartbeat_timeout
        run_timeout = cfg.subagent_run_timeout
        min_evidence = cfg.subagent_min_evidence_for_done
        issues: list[DueCheckIssue] = []

        for task in self.list_runs():
            open_request_count = sum(
                1 for item in task.capability_requests if item.status == "OPEN"
            )
            open_gap_count = sum(1 for item in task.capability_gaps if item.status == "OPEN")
            risk_flags = self._risk_flags(
                task,
                open_request_count=open_request_count,
                open_gap_count=open_gap_count,
            )
            age_seconds = max(0.0, now - (task.created_at or now))
            stale_seconds = max(0.0, now - (task.heartbeat_at or task.updated_at or now))

            validation = self.validate_work_order(task.id)
            if not validation.ok:
                issues.append(
                    _make_due_issue(
                        task,
                        severity="P0",
                        kind="missing_work_order_files",
                        message=f"工单目录缺少 {len(validation.missing)} 个关键路径，后续接管和验收不可靠。",
                        suggested_action="repair_work_order",
                        risk_flags=risk_flags,
                        open_request_count=open_request_count,
                        open_gap_count=open_gap_count,
                        age_seconds=age_seconds,
                        stale_seconds=stale_seconds,
                    )
                )

            if task.status in {"FAILED", "TIMEOUT", "CHANNEL_ERROR", "BLOCKED"}:
                severity = {
                    "FAILED": "P0",
                    "TIMEOUT": "P0",
                    "CHANNEL_ERROR": "P0",
                    "BLOCKED": "P1",
                }[task.status]
                action = {
                    "FAILED": "inspect_failure_and_reassign_or_takeover",
                    "TIMEOUT": "shrink_scope_or_takeover",
                    "CHANNEL_ERROR": "probe_channel_before_reassign",
                    "BLOCKED": "classify_blocker_and_route_capability",
                }[task.status]
                issues.append(
                    _make_due_issue(
                        task,
                        severity=severity,
                        kind=f"status_{task.status.lower()}",
                        message=f"任务状态为 {task.status}，需要父代理确认原因，不能当作完成。",
                        suggested_action=action,
                        risk_flags=risk_flags,
                        open_request_count=open_request_count,
                        open_gap_count=open_gap_count,
                        age_seconds=age_seconds,
                        stale_seconds=stale_seconds,
                    )
                )

            if task.channel_status == "BROKEN":
                issues.append(
                    _make_due_issue(
                        task,
                        severity="P0",
                        kind="channel_broken",
                        message="最近一次通道检查为 BROKEN，优先修复 runtime / workdir / JSON 现场。",
                        suggested_action="run_channel_probe_and_fix_runtime",
                        risk_flags=risk_flags,
                        open_request_count=open_request_count,
                        open_gap_count=open_gap_count,
                        age_seconds=age_seconds,
                        stale_seconds=stale_seconds,
                    )
                )
            if task.channel_status == "DEGRADED":
                issues.append(
                    _make_due_issue(
                        task,
                        severity="P1",
                        kind="channel_degraded",
                        message="最近一次通道检查为 DEGRADED，建议先修复弱项再继续派工。",
                        suggested_action="inspect_channel_probe_evidence",
                        risk_flags=risk_flags,
                        open_request_count=open_request_count,
                        open_gap_count=open_gap_count,
                        age_seconds=age_seconds,
                        stale_seconds=stale_seconds,
                    )
                )
            if task.status == "CHANNEL_ERROR" and not task.last_probe_at:
                issues.append(
                    _make_due_issue(
                        task,
                        severity="P0",
                        kind="channel_probe_missing",
                        message="任务状态为 CHANNEL_ERROR，但还没有 probe 证据。",
                        suggested_action="run_channel_probe",
                        risk_flags=risk_flags,
                        open_request_count=open_request_count,
                        open_gap_count=open_gap_count,
                        age_seconds=age_seconds,
                        stale_seconds=stale_seconds,
                    )
                )

            if task.status == "DONE" and min_evidence > 0 and len(task.evidence) < min_evidence:
                issues.append(
                    _make_due_issue(
                        task,
                        severity="P0",
                        kind="fake_done_risk",
                        message=(
                            f"DONE 任务只有 {len(task.evidence)} 条证据，"
                            f"少于配置要求的 {min_evidence} 条。"
                        ),
                        suggested_action="require_evidence_or_reopen",
                        risk_flags=risk_flags,
                        open_request_count=open_request_count,
                        open_gap_count=open_gap_count,
                        age_seconds=age_seconds,
                        stale_seconds=stale_seconds,
                    )
                )
            if task.status == "DONE" and task.verification_status != "VERIFIED":
                issues.append(
                    _make_due_issue(
                        task,
                        severity="P1",
                        kind="unverified_done",
                        message="任务已标记 DONE，但 verification_status 还不是 VERIFIED。",
                        suggested_action="run_acceptance_or_assign_reviewer",
                        risk_flags=risk_flags,
                        open_request_count=open_request_count,
                        open_gap_count=open_gap_count,
                        age_seconds=age_seconds,
                        stale_seconds=stale_seconds,
                    )
                )

            if open_request_count:
                issues.append(
                    _make_due_issue(
                        task,
                        severity="P1",
                        kind="open_capability_request",
                        message=f"存在 {open_request_count} 条未处理能力请求。",
                        suggested_action="route_capability_request",
                        risk_flags=risk_flags,
                        open_request_count=open_request_count,
                        open_gap_count=open_gap_count,
                        age_seconds=age_seconds,
                        stale_seconds=stale_seconds,
                    )
                )
            if open_gap_count:
                issues.append(
                    _make_due_issue(
                        task,
                        severity="P2",
                        kind="open_capability_gap",
                        message=f"存在 {open_gap_count} 条未关闭能力缺口。",
                        suggested_action="triage_gap_for_learning_or_tooling",
                        risk_flags=risk_flags,
                        open_request_count=open_request_count,
                        open_gap_count=open_gap_count,
                        age_seconds=age_seconds,
                        stale_seconds=stale_seconds,
                    )
                )

            if _is_active(task.status):
                if heartbeat_timeout > 0 and stale_seconds > heartbeat_timeout:
                    severity = "P0" if stale_seconds > heartbeat_timeout * 3 else "P1"
                    issues.append(
                        _make_due_issue(
                            task,
                            severity=severity,
                            kind="heartbeat_stale",
                            message=(
                                f"心跳已停滞 {stale_seconds:.0f}s，"
                                f"超过配置阈值 {heartbeat_timeout}s。"
                            ),
                            suggested_action="check_runtime_or_takeover",
                            risk_flags=risk_flags,
                            open_request_count=open_request_count,
                            open_gap_count=open_gap_count,
                            age_seconds=age_seconds,
                            stale_seconds=stale_seconds,
                        )
                    )
                if run_timeout > 0 and age_seconds > run_timeout:
                    issues.append(
                        _make_due_issue(
                            task,
                            severity="P0",
                            kind="run_timeout",
                            message=(
                                f"任务已运行 {age_seconds:.0f}s，"
                                f"超过配置阈值 {run_timeout}s。"
                            ),
                            suggested_action="shrink_scope_reassign_or_takeover",
                            risk_flags=risk_flags,
                            open_request_count=open_request_count,
                            open_gap_count=open_gap_count,
                            age_seconds=age_seconds,
                            stale_seconds=stale_seconds,
                        )
                    )

        issues.sort(key=lambda issue: (-_issue_weight(issue), issue.run_id, issue.kind))
        summary: dict[str, int] = {"total": len(issues)}
        for issue in issues:
            summary[issue.severity] = summary.get(issue.severity, 0) + 1
            summary[issue.kind] = summary.get(issue.kind, 0) + 1
        return DueCheckReport(generated_at=now, summary=summary, issues=issues)

    def write_due_check(self, config: CapabilityConfig | None = None) -> DueCheckReport:
        """写出 due-check JSON 和 Markdown 报告。"""

        report = self.due_check(config)
        (self.workspace / "subagent_due_check.json").write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (self.workspace / "SUBAGENT_DUE_CHECK.md").write_text(
            render_due_check_markdown(report),
            encoding="utf-8",
        )
        return report

    def plan_actions(self, config: CapabilityConfig | None = None) -> ActionPlanReport:
        """把 due-check 问题转成 dry-run 动作清单。

        这里故意只生成计划，不自动修改任务。
        后续真正接 takeover / reassign / capability routing 时，再按这些 action 增加 apply 层。
        """

        due_report = self.due_check(config)
        merged: dict[tuple[str, str], ActionPlanItem] = {}
        for issue in due_report.issues:
            action, priority, would_change_status_to = _action_for_issue(issue)
            key = (issue.run_id, action)
            if key not in merged:
                merged[key] = ActionPlanItem(
                    id=_new_id("action"),
                    run_id=issue.run_id,
                    severity=issue.severity,
                    priority=priority,
                    action=action,
                    reason=issue.message,
                    source_issue_kinds=[issue.kind],
                    suggested_commands=_commands_for_action(action, issue.run_id),
                    would_change_status_to=would_change_status_to,
                    owner=issue.owner,
                    final_owner=issue.final_owner,
                    task_dir=issue.task_dir,
                    created_at=time.time(),
                )
                continue
            item = merged[key]
            item.source_issue_kinds = _merge_list(item.source_issue_kinds, [issue.kind])
            item.reason = f"{item.reason} / {issue.message}"
            if _severity_weight(issue.severity) > _severity_weight(item.severity):
                item.severity = issue.severity
            item.priority = max(item.priority, priority)

        actions = list(merged.values())
        actions.sort(key=lambda item: (-item.priority, item.run_id, item.action))
        summary: dict[str, int] = {"total": len(actions)}
        for action in actions:
            summary[action.severity] = summary.get(action.severity, 0) + 1
            summary[action.action] = summary.get(action.action, 0) + 1
        return ActionPlanReport(
            generated_at=time.time(),
            summary=summary,
            actions=actions,
        )

    def write_action_plan(self, config: CapabilityConfig | None = None) -> ActionPlanReport:
        """写出 dry-run 动作计划 JSON 和 Markdown。"""

        report = self.plan_actions(config)
        (self.workspace / "subagent_action_plan.json").write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (self.workspace / "SUBAGENT_ACTION_PLAN.md").write_text(
            render_action_plan_markdown(report),
            encoding="utf-8",
        )
        return report

    def _to_board_item(self, task: SubAgentTask) -> SubAgentBoardItem:
        """把运行记录压缩成看板行。"""

        open_request_count = sum(1 for item in task.capability_requests if item.status == "OPEN")
        open_gap_count = sum(1 for item in task.capability_gaps if item.status == "OPEN")
        flags = self._risk_flags(task, open_request_count=open_request_count, open_gap_count=open_gap_count)
        return SubAgentBoardItem(
            id=task.id,
            root_id=task.root_id,
            parent_id=task.parent_id,
            depth=task.depth,
            status=task.status,
            verification_status=task.verification_status,
            channel_status=task.channel_status,
            owner=task.owner,
            supervisor=task.supervisor,
            final_owner=task.final_owner,
            goal=task.goal,
            updated_at=task.updated_at,
            heartbeat_at=task.heartbeat_at,
            evidence_count=len(task.evidence),
            open_request_count=open_request_count,
            open_gap_count=open_gap_count,
            child_count=len(task.child_ids),
            takeover_by=task.takeover_by,
            locked_file_count=len(task.locked_files),
            risk_flags=flags,
            task_dir=task.task_dir,
            output_json=task.output_json,
        )

    def _risk_flags(
        self,
        task: SubAgentTask,
        *,
        open_request_count: int,
        open_gap_count: int,
    ) -> list[str]:
        """给看板行打风险标记，让 100+ 子代理时异常能浮上来。"""

        flags: list[str] = []
        if task.status in {"BLOCKED", "FAILED", "TIMEOUT", "CHANNEL_ERROR"}:
            flags.append(task.status.lower())
        if task.status == "DONE" and not task.evidence:
            flags.append("done_without_evidence")
        if task.status == "DONE" and task.verification_status != "VERIFIED":
            flags.append("done_without_verification")
        if open_request_count:
            flags.append("open_capability_request")
        if open_gap_count:
            flags.append("open_capability_gap")
        if task.takeover_by:
            flags.append("taken_over")
        if task.channel_status == "BROKEN":
            flags.append("channel_broken")
        if task.channel_status == "DEGRADED":
            flags.append("channel_degraded")
        validation = self.validate_work_order(task.id)
        if not validation.ok:
            flags.append("missing_work_order_files")
        return flags
