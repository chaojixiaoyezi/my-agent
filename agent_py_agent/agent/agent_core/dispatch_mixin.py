from __future__ import annotations

"""LLM: implements SimpleAgent parent-dispatch and watch-loop orchestration.

给人看的解释：
这个文件负责一轮或多轮父代理调度：due-check、自动动作、能力路由、runner、patch 审核、验收。
各阶段的小规则已经拆到 planner/runner_dispatch/lock 等模块，避免一个函数吞下所有事情。
"""

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from ..capabilities import CapabilityRouter
from ..capability_config import CapabilityConfig
from ..subagent import DispatchReport, DispatchWatchReport, SubAgentRunnerResult
from .dispatch_lock import _DispatchWatchLock
from .parameters import _sleep_with_stop
from .planner import _combine_runner_instruction
from .runner_dispatch import (
    _dispatch_patch_review_run_ids,
    _dispatch_runner_candidates,
    _resolve_runner_concurrency,
    _resolve_runner_start_rate,
    _resolve_runner_timeout_seconds,
    _run_subagent_worker,
    _runner_dispatch_record,
    _runner_max_attempts,
    _runner_retry_reason,
)


class SimpleAgentDispatchMixin:
    """LLM: mixin for audited parent dispatch and recurring watch mode.

    给人看的解释：
    用户说“推进一下子代理”或 daemon 定时巡检，都会走这里。
    它负责串阶段，不把底层文件操作和规则判断都塞在自己身上。
    """

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
                effective_runner_instruction = _combine_runner_instruction(
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

        if normalized_workflow_mode in {"plan", "auto"}:
            workflow_candidates = [
                task
                for task in self.subagents.list_runs()
                if not task.parent_id
                and not task.workflow_parent_run_id
                and task.status not in {"DONE", "FAILED", "TIMEOUT", "CHANNEL_ERROR", "TAKEN_OVER"}
            ]
            if limit > 0:
                workflow_candidates = workflow_candidates[:limit]
            for task in workflow_candidates:
                preview = task.workflow_plan or self.subagents._try_workflow_plan(
                    task.goal,
                    quality_contract=task.quality_contract,
                    context_manifest=task.context_manifest,
                    allowed_write_roots=self.subagents._workflow_extra_write_roots(task),
                ) or {}
                worker_count = len(preview.get("workers") or []) if isinstance(preview, dict) else 0
                if not apply:
                    records.append(
                        self.subagents.make_dispatch_record(
                            step="workflow",
                            action="plan_workflow",
                            run_id=task.id,
                            dry_run=True,
                            applied=False,
                            ok=bool(preview),
                            message=(
                                f"dry-run: 将为父任务写入 workflow 计划，template="
                                f"{preview.get('selected_template_id', '') or 'none'} workers={worker_count}。"
                            ),
                            before_status=task.status,
                            after_status=task.status,
                            before_verification_status=task.verification_status,
                            after_verification_status=task.verification_status,
                            evidence_paths=[task.task_dir],
                        )
                    )
                    if normalized_workflow_mode == "auto" and preview.get("ok"):
                        records.append(
                            self.subagents.make_dispatch_record(
                                step="workflow",
                                action="spawn_workflow_workers",
                                run_id=task.id,
                                dry_run=True,
                                applied=False,
                                ok=True,
                                message=f"dry-run: apply 时会根据 workflow 计划创建 {worker_count} 个 worker 子工单。",
                                before_status=task.status,
                                after_status=task.status,
                                before_verification_status=task.verification_status,
                                after_verification_status=task.verification_status,
                                evidence_paths=[task.task_dir],
                            )
                        )
                    continue

                planned = self.subagents.ensure_workflow_plan(task.id, workflow_mode=normalized_workflow_mode)
                records.append(
                    self.subagents.make_dispatch_record(
                        step="workflow",
                        action="plan_workflow",
                        run_id=task.id,
                        dry_run=False,
                        applied=bool(planned.workflow_plan),
                        ok=bool(planned.workflow_plan),
                        message=(
                            f"已写入 workflow 计划，template={planned.workflow_template_id or 'none'} "
                            f"workers={len(planned.workflow_plan.get('workers') or []) if planned.workflow_plan else 0}。"
                            if planned.workflow_plan
                            else "未能生成 workflow 计划。"
                        ),
                        before_status=task.status,
                        after_status=planned.status,
                        before_verification_status=task.verification_status,
                        after_verification_status=planned.verification_status,
                        evidence_paths=[planned.task_dir],
                    )
                )
                if normalized_workflow_mode == "auto" and planned.workflow_plan.get("ok"):
                    before_child_count = len(planned.workflow_child_run_ids)
                    planned, created_children = self.subagents.realize_workflow_plan(task.id)
                    records.append(
                        self.subagents.make_dispatch_record(
                            step="workflow",
                            action="spawn_workflow_workers",
                            run_id=task.id,
                            dry_run=False,
                            applied=bool(created_children) or before_child_count > 0,
                            ok=True,
                            message=(
                                f"已创建 {len(created_children)} 个 workflow worker 子工单。"
                                if created_children
                                else "workflow worker 子工单已存在，本轮未重复创建。"
                            ),
                            before_status=task.status,
                            after_status=planned.status,
                            before_verification_status=task.verification_status,
                            after_verification_status=planned.verification_status,
                            evidence_paths=[planned.task_dir, *[child.task_dir for child in created_children]],
                        )
                    )

        due_report = self.subagents.write_due_check(cfg) if apply else self.subagents.due_check(cfg)
        records.append(
            self.subagents.make_dispatch_record(
                step="due_check",
                action="scan",
                dry_run=not apply,
                applied=False,
                ok=True,
                message=f"发现 {due_report.summary.get('total', 0)} 个 due-check issue。",
                evidence_paths=[str(self.subagents.workspace / "subagent_due_check.json")],
            )
        )

        action_report = (
            self.subagents.write_action_apply_report(
                cfg,
                apply=apply,
                take_over_by=take_over_by,
                locked_files=locked_files or [],
                limit=limit,
            )
            if apply
            else self.subagents.apply_actions(
                cfg,
                apply=False,
                take_over_by=take_over_by,
                locked_files=locked_files or [],
                limit=limit,
            )
        )
        for item in action_report.records:
            records.append(
                self.subagents.make_dispatch_record(
                    step="action_apply",
                    action=item.action,
                    run_id=item.run_id,
                    dry_run=item.dry_run,
                    applied=item.applied,
                    ok=item.ok,
                    message=item.message,
                    before_status=item.before_status,
                    after_status=item.after_status,
                    evidence_paths=item.evidence_paths,
                )
            )

        route_report = (
            self.subagents.write_capability_route_report(
                router,
                cfg,
                apply=apply,
                limit=limit,
            )
            if apply
            else self.subagents.route_capability_requests(
                router,
                cfg,
                apply=False,
                limit=limit,
            )
        )
        for item in route_report.records:
            records.append(
                self.subagents.make_dispatch_record(
                    step="capability_route",
                    action=item.status.lower(),
                    run_id=item.run_id,
                    dry_run=item.dry_run,
                    applied=not item.dry_run,
                    ok=item.status in {"WOULD_GRANT", "GRANTED"},
                    message=item.message,
                    evidence_paths=[str(self.subagents.workspace / "subagent_capability_route_report.json")],
                )
            )

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

        runner_start_rate = _resolve_runner_start_rate(self.config.runner_start_rate, len(pending_runner_jobs))
        if runner_start_rate and runner_start_rate < len(pending_runner_jobs):
            pending_runner_jobs = pending_runner_jobs[:runner_start_rate]
        runner_concurrency = _resolve_runner_concurrency(self.config.runner_concurrency, len(pending_runner_jobs))
        runner_timeout_seconds = _resolve_runner_timeout_seconds(self.config.runner_timeout_seconds)
        if pending_runner_jobs and runner_concurrency > 1 and execute_runners:
            future_to_job = {}
            with ThreadPoolExecutor(max_workers=runner_concurrency) as executor:
                for run_id, before, retry_reason in pending_runner_jobs:
                    future = executor.submit(
                        _run_subagent_worker,
                        self.config,
                        self.root,
                        run_id,
                        effective_runner_instruction,
                        not execute_runners,
                        max_cards,
                        probe,
                        retry_reason,
                        runner_timeout_seconds,
                    )
                    future_to_job[future] = (run_id, before, retry_reason)
                completed: dict[str, tuple[SubAgentRunnerResult, object]] = {}
                for future in as_completed(future_to_job):
                    run_id, before, retry_reason = future_to_job[future]
                    try:
                        result = future.result()
                    except Exception as exc:
                        result = self.subagents.record_runner_result(
                            run_id,
                            dry_run=False,
                            ok=False,
                            message=f"runner worker failed: {exc}",
                            status="BLOCKED",
                            verification_status="UNVERIFIED",
                            failure_type="runner_worker_error",
                        )
                    after = self.subagents.load(run_id)
                    completed[run_id] = (result, after)
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
        else:
            for run_id, before, retry_reason in pending_runner_jobs:
                if execute_runners and runner_timeout_seconds > 0:
                    result = _run_subagent_worker(
                        self.config,
                        self.root,
                        run_id,
                        effective_runner_instruction,
                        False,
                        max_cards,
                        probe,
                        retry_reason,
                        runner_timeout_seconds,
                    )
                else:
                    result = self.run_subagent(
                        run_id,
                        instruction=effective_runner_instruction,
                        dry_run=not execute_runners,
                        max_cards=max_cards,
                        probe=probe,
                        retry_reason=retry_reason,
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

        patch_run_ids = _dispatch_patch_review_run_ids(self.subagents.list_runs())
        if patch_run_ids:
            patch_report = (
                self.subagents.write_patch_review_report(
                    patch_run_ids,
                    apply=True,
                    reviewer=reviewer,
                    note=note,
                    limit=limit,
                )
                if apply
                else self.subagents.review_patches(
                    patch_run_ids,
                    apply=False,
                    reviewer=reviewer,
                    note=note,
                    limit=limit,
                )
            )
            for item in patch_report.records:
                records.append(
                    self.subagents.make_dispatch_record(
                        step="patch_review",
                        action=item.decision.lower(),
                        run_id=item.run_id,
                        dry_run=item.dry_run,
                        applied=item.applied,
                        ok=item.ok,
                        message=item.message,
                        evidence_paths=item.evidence_paths,
                    )
                )

        acceptance_report = (
            self.subagents.write_acceptance_review_report(
                apply=True,
                reviewer=reviewer,
                note=note,
                limit=limit,
            )
            if apply
            else self.subagents.review_acceptances(
                apply=False,
                reviewer=reviewer,
                note=note,
                limit=limit,
            )
        )
        for item in acceptance_report.records:
            records.append(
                self.subagents.make_dispatch_record(
                    step="acceptance",
                    action=item.decision.lower(),
                    run_id=item.run_id,
                    dry_run=item.dry_run,
                    applied=item.applied,
                    ok=item.ok,
                    message=item.message,
                    before_status=item.before_status,
                    after_status=item.after_status,
                    before_verification_status=item.before_verification_status,
                    after_verification_status=item.after_verification_status,
                    evidence_paths=item.evidence_paths,
                )
            )

        report = self.subagents.build_dispatch_report(records, dry_run=not apply)
        return self.subagents.write_dispatch_report(report, append_log=apply)

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
        with _DispatchWatchLock(lock_path, force=force_lock) as lock:
            cycle = 0
            while max_cycles == 0 or cycle < max_cycles:
                if stop_path and stop_path.exists():
                    break
                cycle += 1
                started_at = time.time()
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
                except Exception as exc:
                    ok = False
                    message = f"dispatch cycle failed: {exc}"
                    record_count = 0
                    dispatch_summary = {}
                    evidence_paths = []

                ended_at = time.time()
                record = self.subagents.make_dispatch_watch_record(
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
                if _sleep_with_stop(interval, stop_path):
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
