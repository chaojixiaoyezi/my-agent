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
from .failure_introspector import FailureIntrospector, FailureIntrospection
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
from .dynamic_timeout import calculate_dynamic_timeout, estimate_task_tokens


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

        # 为每个任务计算动态超时的辅助函数
        def _get_task_timeout(before: SubAgentTask) -> float:
            # 检查任务是否有预先计算的动态超时
            if before.attributes and "dynamic_timeout_seconds" in before.attributes:
                timeout = float(before.attributes["dynamic_timeout_seconds"])
                if timeout > 0:
                    return timeout

            # 如果没有，使用配置的静态超时
            if runner_timeout_seconds > 0:
                return runner_timeout_seconds

            # 否则动态计算
            estimated_input_tokens, estimated_output_tokens = estimate_task_tokens(before.goal, before.plan)
            return calculate_dynamic_timeout(
                self.config,
                estimated_input_tokens,
                estimated_output_tokens,
            )

        if pending_runner_jobs and runner_concurrency > 1 and execute_runners:
            future_to_job = {}
            with ThreadPoolExecutor(max_workers=runner_concurrency) as executor:
                for run_id, before, retry_reason in pending_runner_jobs:
                    task_timeout = _get_task_timeout(before)
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
                        task_timeout,
                        local_store=self.local_store,
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

                # 失败自动触发：如果 runner 执行失败（BLOCKED/TIMEOUT）且任务仍可重试，
                # 在当前 dispatch 循环内标记需要重调度，让下一轮立即捡起
                if not result.ok and execute_runners:
                    failure_type = str(result.status or "").strip().upper()
                    if failure_type in {"BLOCKED", "TIMEOUT"} and retry_reason:
                        self._has_pending_work = True
                        try:
                            from ..memory_push import push_relevant_memories, format_memories_for_injection

                            task_context = {
                                "task_id": run_id,
                                "goal": before.goal if hasattr(before, "goal") else "",
                                "failure_type": failure_type.lower(),
                            }
                            relevant_memories = push_relevant_memories(self, failure_type.lower(), task_context, limit=3)
                            if relevant_memories:
                                memory_hint = format_memories_for_injection(relevant_memories)
                                if effective_runner_instruction:
                                    effective_runner_instruction = f"{effective_runner_instruction}\n\n{memory_hint}"
                                else:
                                    effective_runner_instruction = memory_hint
                        except Exception:
                            pass
                        # LLM 自省：分析失败原因并给出调参建议
                        self._handle_failure_introspection(run_id, before, result)
        else:
            for run_id, before, retry_reason in pending_runner_jobs:
                task_timeout = _get_task_timeout(before)
                if execute_runners and task_timeout > 0:
                    result = _run_subagent_worker(
                        self.config,
                        self.root,
                        run_id,
                        effective_runner_instruction,
                        False,
                        max_cards,
                        probe,
                        retry_reason,
                        task_timeout,
                        local_store=self.local_store,
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

                # 失败自动触发：如果 runner 执行失败（BLOCKED/TIMEOUT）且任务仍可重试，
                # 在当前 dispatch 循环内标记需要重调度，让下一轮立即捡起
                if not result.ok and execute_runners:
                    failure_type = str(result.status or "").strip().upper()
                    if failure_type in {"BLOCKED", "TIMEOUT"} and retry_reason:
                        # 设置 _has_pending_work 让闭环检测知道还有工作要做
                        self._has_pending_work = True

                        # 注入相关记忆（推模式）
                        try:
                            from ..memory_push import push_relevant_memories, format_memories_for_injection

                            task_context = {
                                "task_id": run_id,
                                "goal": before.goal if hasattr(before, "goal") else "",
                                "failure_type": failure_type.lower(),
                            }
                            relevant_memories = push_relevant_memories(self, failure_type.lower(), task_context, limit=3)
                            if relevant_memories:
                                memory_hint = format_memories_for_injection(relevant_memories)
                                # 把记忆提示追加到 runner_instruction
                                if effective_runner_instruction:
                                    effective_runner_instruction = f"{effective_runner_instruction}\n\n{memory_hint}"
                                else:
                                    effective_runner_instruction = memory_hint
                        except Exception:
                            pass  # 记忆注入失败不影响主流程

                        # LLM 自省：分析失败原因并给出调参建议
                        self._handle_failure_introspection(run_id, before, result)

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
        report = self.subagents.write_dispatch_report(report, append_log=apply)

        # 任务达到终态时触发通知
        if apply:
            self._notify_completed_tasks(records)

        # 闭环检测：检查是否还有可调度的任务
        self._update_pending_work_state()

        return report

    def _update_pending_work_state(self) -> None:
        """更新待处理工作状态。

        在 dispatch_subagents 末尾调用，检查是否还有可调度的 runner 候选任务。
        """
        runner_max_attempts = _runner_max_attempts(self.config.runner_failure_policy)
        candidates = _dispatch_runner_candidates(
            self.subagents.list_runs(),
            max_runners=999,  # 用较大值确保不遗漏
            runner_max_attempts=runner_max_attempts,
        )
        self._has_pending_work = len(candidates) > 0

    def _handle_failure_introspection(
        self,
        run_id: str,
        task_before: SubAgentTask,
        runner_result: SubAgentRunnerResult,
    ) -> None:
        """失败后调用 LLM 自省，分析原因并给出调参建议。

        分析结果存入 task.attributes['failure_introspection_data']，
        如果 LLM 建议调整参数，应用到任务属性中。
        """
        try:
            from .failure_analyzer import SubAgentFailureAnalyzer

            task = self.subagents.load(run_id)
            analyzer = SubAgentFailureAnalyzer()
            failure_analysis = analyzer.analyze(task, runner_result)

            introspector = FailureIntrospector(agent=self)
            introspection = introspector.introspect(task, runner_result, failure_analysis)

            # 存储自省结果到 attributes
            introspection_data = {
                "analysis_reason": introspection.analysis_reason,
                "root_cause": introspection.root_cause,
                "suggested_params": introspection.suggested_params,
                "should_retry": introspection.should_retry,
                "should_split": introspection.should_split,
                "confidence": introspection.confidence,
            }
            task.attributes["failure_introspection_data"] = introspection_data

            # 如果 LLM 建议调整参数，应用到任务
            if introspection.suggested_params:
                self._apply_introspection_params(task, introspection.suggested_params)

            self.subagents.save(task)

        except Exception as exc:
            # 自省失败不影响主流程
            import logging
            logging.getLogger(__name__).warning(f"Failure introspection failed: {exc}")

    def _apply_introspection_params(
        self,
        task: SubAgentTask,
        params: dict,
    ) -> SubAgentTask:
        """根据 LLM 自省结果调整任务参数。

        Args:
            task: 任务
            params: LLM 建议的参数调整

        Returns:
            调整后的任务
        """
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

        # 自适应间隔配置
        active_interval = getattr(self.config, "dispatch_active_interval", 5)
        idle_interval = getattr(self.config, "dispatch_idle_interval", 30)
        max_consecutive = getattr(self.config, "dispatch_max_consecutive_rounds", 20)

        # 重置连续轮数计数
        self._reset_dispatch_rounds()

        with _DispatchWatchLock(lock_path, force=force_lock) as lock:
            cycle = 0
            last_dispatch_had_changes = False
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

                    # 检查本轮是否有状态变化
                    last_dispatch_had_changes = record_count > 0

                except Exception as exc:
                    ok = False
                    message = f"dispatch cycle failed: {exc}"
                    record_count = 0
                    dispatch_summary = {}
                    evidence_paths = []
                    last_dispatch_had_changes = False

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

                # 递增连续轮数
                self._increment_dispatch_rounds()

                # 检查是否达到最大轮数限制
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

                # 自适应间隔：有变化时用短间隔，无变化时用长间隔
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
