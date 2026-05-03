from __future__ import annotations

"""LLM: thin facade for SimpleAgent parent-dispatch and watch-loop orchestration.

给人看的解释：
这个文件是调度 facade，所有实现都代理到 service 模块。
保持 mixin 签名完全兼容，业务逻辑委托给 dispatch_service、planner_service、runner_gate、acceptance_gate。
"""

import os
from pathlib import Path
from typing import TYPE_CHECKING

from ..capabilities import CapabilityRouter
from ..capability_config import CapabilityConfig
from ..subagent import DispatchReport, DispatchWatchReport

if TYPE_CHECKING:
    from ..core import SimpleAgent
    from ..subagent import SubAgentRunnerResult, SubAgentTask

from .dispatch_service import (
    build_workflow_records,
    make_acceptance_records,
    make_action_apply_records,
    make_capability_route_records,
    make_dispatch_watch_record,
    make_due_check_record,
    make_patch_review_records,
    update_pending_work_state,
)
from .planner_service import combine_runner_instruction
from .runner_dispatch import (
    _dispatch_patch_review_run_ids,
    _dispatch_runner_candidates,
    _resolve_runner_concurrency,
    _resolve_runner_start_rate,
    _resolve_runner_timeout_seconds,
    _runner_dispatch_record,
    _runner_max_attempts,
    _runner_retry_reason,
)
from .runner_gate import get_task_timeout, handle_runner_failure, resolve_runner_config, run_concurrent_runners, run_single_runner
from .parameters import _sleep_with_stop


class SimpleAgentDispatchMixin:
    """LLM: mixin for audited parent dispatch and recurring watch mode.

    给人看的解释：
    用户说"推进一下子代理"或 daemon 定时巡检，都会走这里。
    它负责串阶段，不把底层文件操作和规则判断都塞在自己身上。
    """

    # 闭环检测相关属性
    _has_pending_work: bool = False
    _consecutive_dispatch_rounds: int = 0

    @property
    def has_pending_work(self) -> bool:
        """公开的待处理工作状态属性。"""
        return self._has_pending_work

    def dispatch_subagents(
        self,
        router: CapabilityRouter,
        capability_config: CapabilityConfig | None = None,
        *,
        apply: bool = False,
        execute_runners: bool = False,
        planner: bool = False,
        workflow_mode: str = "off",
        max_runners: int = 1,
        limit: int = 20,
        reviewer: str = "parent-dispatch",
        note: str = "",
        runner_instruction: str = "",
        max_cards: int = 0,
        probe: bool = True,
        take_over_by: str = "",
        locked_files: list[str] | None = None,
    ) -> DispatchReport:
        """执行一轮父代理调度。

        dry-run 只汇总会做什么；apply 会依次执行低风险动作、能力路由、
        runner、patch 审核和父代理验收。真实模型调用还需要额外打开
        `execute_runners`，避免普通 apply 意外消耗 API。
        """
        cfg = capability_config or CapabilityConfig()
        records = []
        effective_runner_instruction = runner_instruction
        effective_max_runners = max_runners
        normalized_workflow_mode = str(workflow_mode or "off").strip().lower()

        # Planner step
        if planner:
            planner_record = self.run_parent_planner(
                router,
                cfg,
                apply=apply,
                execute_runners=execute_runners,
                max_runners=max_runners,
                limit=limit,
                reviewer=reviewer,
                note=note,
                runner_instruction=runner_instruction,
            )
            if planner_record.runner_instruction:
                effective_runner_instruction = combine_runner_instruction(
                    runner_instruction,
                    planner_record.runner_instruction,
                )
            if planner_record.suggested_max_runners > 0 and max_runners > 0:
                effective_max_runners = min(max_runners, planner_record.suggested_max_runners)
            records.append(
                self.subagents.make_dispatch_record(
                    step="parent_planner",
                    action=planner_record.decision.lower(),
                    dry_run=not apply,
                    applied=False,
                    ok=planner_record.ok,
                    message=planner_record.message,
                    evidence_paths=planner_record.evidence_paths,
                )
            )

        # Workflow step
        if normalized_workflow_mode in {"plan", "auto"}:
            workflow_records = build_workflow_records(
                self,
                self.subagents.list_runs(),
                normalized_workflow_mode,
                limit,
                apply,
            )
            records.extend(workflow_records)

        # Due check step
        records.append(make_due_check_record(self, cfg, apply))

        # Action apply step
        action_records = make_action_apply_records(
            self, cfg, apply, take_over_by, locked_files, limit
        )
        records.extend(action_records)

        # Capability route step
        route_records = make_capability_route_records(self, router, cfg, apply, limit)
        records.extend(route_records)

        # Runner step
        runner_max_attempts = _runner_max_attempts(self.config.runner_failure_policy)
        runner_candidates = _dispatch_runner_candidates(
            self.subagents.list_runs(),
            effective_max_runners,
            runner_max_attempts=runner_max_attempts,
        )
        pending_runner_jobs = []
        for task in runner_candidates:
            before = self.subagents.load(task.id)
            retry_reason = _runner_retry_reason(before, runner_max_attempts)
            action_name = "retry_runner" if retry_reason else "execute_runner"
            if not apply:
                records.append(
                    self.subagents.make_dispatch_record(
                        step="runner",
                        action=action_name,
                        run_id=task.id,
                        dry_run=True,
                        applied=False,
                        ok=True,
                        message=(
                            f"dry-run: 将重试 runner（{retry_reason}）。"
                            if retry_reason
                            else "dry-run: apply 时会生成执行上下文；带 --execute-runners 时会调用模型。"
                        ),
                        before_status=before.status,
                        after_status=before.status,
                        before_verification_status=before.verification_status,
                        after_verification_status=before.verification_status,
                        evidence_paths=[before.task_dir],
                    )
                )
                continue
            pending_runner_jobs.append((task.id, before, retry_reason))

        if pending_runner_jobs:
            runner_timeout_seconds, runner_concurrency, runner_start_rate = resolve_runner_config(
                self.config, len(pending_runner_jobs)
            )
            if runner_start_rate and runner_start_rate < len(pending_runner_jobs):
                pending_runner_jobs = pending_runner_jobs[:runner_start_rate]

            if pending_runner_jobs and runner_concurrency > 1 and execute_runners:
                completed = run_concurrent_runners(
                    self,
                    pending_runner_jobs,
                    runner_concurrency,
                    runner_timeout_seconds,
                    effective_runner_instruction,
                    execute_runners,
                    max_cards,
                    probe,
                )
                for run_id, before, retry_reason in pending_runner_jobs:
                    result, after = completed[run_id]
                    records.append(
                        _runner_dispatch_record(
                            self,
                            run_id=run_id,
                            before=before,
                            after=after,
                            result=result,
                            retry_reason=retry_reason,
                            execute_runners=execute_runners,
                        )
                    )
                    if not result.ok and execute_runners:
                        effective_runner_instruction = handle_runner_failure(
                            self, run_id, before, result, effective_runner_instruction
                        )
            else:
                for run_id, before, retry_reason in pending_runner_jobs:
                    task_timeout = get_task_timeout(before, runner_timeout_seconds, self.config)
                    result = run_single_runner(
                        self,
                        run_id,
                        task_timeout,
                        effective_runner_instruction,
                        execute_runners,
                        max_cards,
                        probe,
                        retry_reason,
                    )
                    after = self.subagents.load(run_id)
                    records.append(
                        _runner_dispatch_record(
                            self,
                            run_id=run_id,
                            before=before,
                            after=after,
                            result=result,
                            retry_reason=retry_reason,
                            execute_runners=execute_runners,
                        )
                    )
                    if not result.ok and execute_runners:
                        effective_runner_instruction = handle_runner_failure(
                            self, run_id, before, result, effective_runner_instruction
                        )

        # Patch review step
        patch_run_ids = _dispatch_patch_review_run_ids(self.subagents.list_runs())
        patch_records = make_patch_review_records(
            self, patch_run_ids, apply, reviewer, note, limit
        )
        records.extend(patch_records)

        # Acceptance step
        acceptance_records = make_acceptance_records(self, apply, reviewer, note, limit)
        records.extend(acceptance_records)

        # Build and write report
        report = self.subagents.build_dispatch_report(records, dry_run=not apply)
        report = self.subagents.write_dispatch_report(report, append_log=apply)

        # Notify completed tasks
        if apply:
            self._notify_completed_tasks(records)

        # Update pending work state
        self._has_pending_work = update_pending_work_state(self)

        return report

    def _update_pending_work_state(self) -> None:
        """更新待处理工作状态。"""
        self._has_pending_work = update_pending_work_state(self)

    def _handle_failure_introspection(self, run_id, task_before, runner_result):
        """失败后调用 LLM 自省，分析原因并给出调参建议。"""
        try:
            from .failure_analyzer import SubAgentFailureAnalyzer

            task = self.subagents.load(run_id)
            analyzer = SubAgentFailureAnalyzer()
            failure_analysis = analyzer.analyze(task, runner_result)

            from .failure_introspector import FailureIntrospector

            introspector = FailureIntrospector(agent=self)
            introspection = introspector.introspect(task, runner_result, failure_analysis)

            introspection_data = {
                "analysis_reason": introspection.analysis_reason,
                "root_cause": introspection.root_cause,
                "suggested_params": introspection.suggested_params,
                "should_retry": introspection.should_retry,
                "should_split": introspection.should_split,
                "confidence": introspection.confidence,
            }
            task.attributes["failure_introspection_data"] = introspection_data

            if introspection.suggested_params:
                self._apply_introspection_params(task, introspection.suggested_params)

            self.subagents.save(task)

        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning(f"Failure introspection failed: {exc}")

    def _apply_introspection_params(self, task, params):
        """根据 LLM 自省结果调整任务参数。"""
        applied = []
        if "new_timeout_seconds" in params:
            timeout = float(params["new_timeout_seconds"])
            task.attributes["dynamic_timeout_seconds"] = timeout
            applied.append(f"timeout={timeout}s")
        if "max_tool_rounds" in params:
            task.attributes["max_tool_rounds"] = int(params["max_tool_rounds"])
            applied.append(f"max_tool_rounds={params['max_tool_rounds']}")
        if "split_goal" in params:
            split_goal = str(params["split_goal"])
            if split_goal and task.goal:
                task.goal = f"{task.goal} | {split_goal}"
                applied.append(f"goal_adjustment={split_goal}")

        if applied:
            import logging
            logging.getLogger(__name__).info(
                f"Applied LLM introspection params to {task.id}: {applied}"
            )

        return task

    def _increment_dispatch_rounds(self) -> None:
        """递增连续 dispatch 轮数。"""
        self._consecutive_dispatch_rounds += 1

    def _reset_dispatch_rounds(self) -> None:
        """重置连续 dispatch 轮数。"""
        self._consecutive_dispatch_rounds = 0

    def _notify_completed_tasks(self, records: list) -> None:
        """对达到终态的任务触发通知。"""
        if not getattr(self.config, "notification_enabled", False):
            return

        final_statuses = {"DONE", "FAILED", "TIMEOUT"}
        notified_run_ids: set[str] = set()

        for record in records:
            if record.step not in {"runner", "acceptance"}:
                continue
            if not record.applied:
                continue
            run_id = record.run_id
            if run_id in notified_run_ids:
                continue

            after_status = getattr(record, "after_status", "")
            if after_status not in final_statuses:
                continue

            try:
                task = self.subagents.load(run_id)
            except FileNotFoundError:
                continue

            if task.status not in final_statuses:
                continue

            notified_run_ids.add(run_id)

            try:
                from ..notification import NotificationManager, NotificationRouter

                notif_manager = NotificationManager(self.config)
                channel = getattr(task, "last_active_channel", "") or "chat"
                message = (
                    f"任务 {run_id} 已完成\n"
                    f"状态: {task.status}\n"
                    f"目标: {task.goal[:100]}\n"
                    f"尝试次数: {task.runner_attempts}"
                )
                notification = notif_manager.create_notification(
                    task_id=run_id,
                    user_id=getattr(self.config, "user_id", "admin"),
                    session_id=task.root_id or "",
                    channel=channel,
                    message=message,
                )
                router = NotificationRouter(notif_manager, self.config)
                router.deliver(notification.notification_id)
            except Exception:
                pass

    def watch_subagents(
        self,
        router: CapabilityRouter,
        capability_config: CapabilityConfig | None = None,
        *,
        apply: bool = False,
        execute_runners: bool = False,
        planner: bool = False,
        workflow_mode: str = "off",
        max_runners: int = 1,
        limit: int = 20,
        reviewer: str = "parent-dispatch",
        note: str = "",
        runner_instruction: str = "",
        max_cards: int = 0,
        probe: bool = True,
        take_over_by: str = "",
        locked_files: list[str] | None = None,
        interval: float = 30.0,
        max_cycles: int = 0,
        force_lock: bool = False,
        stop_file: str | Path | None = None,
    ) -> DispatchWatchReport:
        """以 watch 模式持续执行父代理调度。"""
        if max_cycles < 0:
            raise ValueError("max_cycles 不能小于 0。")
        if interval < 0:
            raise ValueError("interval 不能小于 0。")

        cfg = capability_config or CapabilityConfig()
        records = []
        lock_path = self.subagents.workspace / "subagent_dispatch_watch.lock"
        stop_path = Path(stop_file) if stop_file else None

        active_interval = getattr(self.config, "dispatch_active_interval", 5)
        idle_interval = getattr(self.config, "dispatch_idle_interval", 30)
        max_consecutive = getattr(self.config, "dispatch_max_consecutive_rounds", 20)

        self._reset_dispatch_rounds()

        from .dispatch_lock import _DispatchWatchLock

        with _DispatchWatchLock(lock_path, force=force_lock) as lock:
            cycle = 0
            last_dispatch_had_changes = False
            while max_cycles == 0 or cycle < max_cycles:
                if stop_path and stop_path.exists():
                    break
                cycle += 1
                started_at = __import__("time").time()
                self.subagents.write_dispatch_watch_heartbeat(
                    cycle=cycle,
                    status="running",
                    lock_path=str(lock_path),
                    pid=os.getpid(),
                    message="dispatch cycle started",
                )
                try:
                    dispatch_report = self.dispatch_subagents(
                        router,
                        cfg,
                        apply=apply,
                        execute_runners=execute_runners,
                        planner=planner,
                        workflow_mode=workflow_mode,
                        max_runners=max_runners,
                        limit=limit,
                        reviewer=reviewer,
                        note=note,
                        runner_instruction=runner_instruction,
                        max_cards=max_cards,
                        probe=probe,
                        take_over_by=take_over_by,
                        locked_files=locked_files or [],
                    )
                    ok = all(item.ok for item in dispatch_report.records)
                    message = f"完成一轮 dispatch，records={len(dispatch_report.records)}。"
                    record_count = len(dispatch_report.records)
                    dispatch_summary = dispatch_report.summary
                    evidence_paths = [
                        str(self.subagents.workspace / "subagent_dispatch_report.json"),
                        str(self.subagents.workspace / "SUBAGENT_DISPATCH.md"),
                    ]
                    last_dispatch_had_changes = record_count > 0

                except Exception as exc:
                    ok = False
                    message = f"dispatch cycle failed: {exc}"
                    record_count = 0
                    dispatch_summary = {}
                    evidence_paths = []
                    last_dispatch_had_changes = False

                ended_at = __import__("time").time()
                record = make_dispatch_watch_record(
                    self,
                    cycle=cycle,
                    dry_run=not apply,
                    ok=ok,
                    message=message,
                    dispatch_record_count=record_count,
                    dispatch_summary=dispatch_summary,
                    started_at=started_at,
                    ended_at=ended_at,
                    evidence_paths=evidence_paths,
                )
                records.append(record)
                self.subagents.append_dispatch_watch_log(record)

                self._increment_dispatch_rounds()

                if self._consecutive_dispatch_rounds >= max_consecutive:
                    message = f"{message} 已达到最大连续轮数限制 ({max_consecutive})，停止调度。"
                    self.subagents.write_dispatch_watch_heartbeat(
                        cycle=cycle,
                        status="stopped_by_limit",
                        lock_path=str(lock_path),
                        pid=os.getpid(),
                        message=message,
                    )
                    break

                more_cycles = max_cycles == 0 or cycle < max_cycles
                stop_requested = bool(stop_path and stop_path.exists())
                if stop_requested:
                    more_cycles = False
                    message = f"{message} stop requested."
                self.subagents.write_dispatch_watch_heartbeat(
                    cycle=cycle,
                    status="sleeping" if more_cycles else "stopping",
                    lock_path=str(lock_path),
                    pid=os.getpid(),
                    message=message,
                )
                if not more_cycles:
                    break

                current_interval = active_interval if last_dispatch_had_changes else idle_interval

                if _sleep_with_stop(current_interval, stop_path):
                    break

            self.subagents.write_dispatch_watch_heartbeat(
                cycle=cycle,
                status="stopped",
                lock_path=str(lock_path),
                pid=os.getpid(),
                message=f"watch stopped; lock={lock.token}",
            )

        report = self.subagents.build_dispatch_watch_report(records, dry_run=not apply)
        return self.subagents.write_dispatch_watch_report(report)