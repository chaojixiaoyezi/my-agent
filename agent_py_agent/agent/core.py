from __future__ import annotations

"""智能体核心循环。"""

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from .backend import get_backend
from .capabilities import CapabilityRouter
from .capability_config import CapabilityConfig
from .config import AgentConfig
from .memory import JsonlMemory
from .prompting import PromptBuilder
from .subagent import (
    DispatchReport,
    DispatchWatchReport,
    ParentPlannerRecord,
    SubAgentExecutionContext,
    SubAgentManager,
    SubAgentRunnerResult,
    SubAgentTask,
    parse_parent_planner_output,
    parse_subagent_runner_output,
)
from .tools import ToolRegistry


@dataclass
class AgentRunResult:
    """一次 `run()` 调用的结果。"""

    prompt: str
    response: str
    backend: str
    used_memories: int
    tool_rounds: int = 0


PARENT_PLANNER_READ_TOOLS = ["list_files", "read_file", "search_text"]


class SimpleAgent:
    """CLI 智能体的主调度器。

    它做的事情可以概括成一句话：
    “把记忆、prompt、模型后端和工具循环串成一条能跑的主链路。”
    """

    def __init__(self, config: AgentConfig, root: str | Path):
        self.config = config
        self.root = Path(root)
        self.memory = JsonlMemory(self.root / config.memory_path)
        self.prompts = PromptBuilder(config, self.root)
        self.backend = get_backend(config.model_backend, config)
        self.subagents = SubAgentManager(self.root / config.subagent_workspace)

        # CLI 入口传进来的 root 通常是包目录 `agent_py_agent`。
        # 但工具更适合看到整个项目根目录，不然它只能读到包内部文件。
        workspace_root = self.root.parent if (self.root / "__main__.py").exists() else self.root
        self.tools = ToolRegistry(
            workspace_root,
            max_chars=config.tool_read_max_chars,
            max_entries=config.tool_list_max_entries,
            max_matches=config.tool_search_max_matches,
            web_max_chars=config.tool_web_max_chars,
            http_timeout=config.tool_http_timeout,
            catalog_limit=config.tool_catalog_limit,
            retrieval_limit=config.tool_retrieval_limit,
            vector_search_enabled=config.tool_vector_search_enabled,
        )

    def run(
        self,
        user_prompt: str,
        *,
        inject: list[str] | None = None,
        prompt_files: list[str] | None = None,
        save: bool | None = None,
        allowed_tools: list[str] | None = None,
    ) -> AgentRunResult:
        """执行一轮智能体请求。"""

        memories = self.memory.search(user_prompt, self.config.memory_top_k)
        tool_catalog_section = (
            self.tools.render_catalog_section(allowed_tools=allowed_tools)
            if self.config.enable_tools
            else ""
        )
        tool_recommendations_section = (
            self.tools.render_recommended_tools_section(user_prompt, allowed_tools=allowed_tools)
            if self.config.enable_tools
            else ""
        )
        tool_context: list[str] = []
        tool_rounds = 0
        final_prompt = ""
        final_response = None

        while True:
            final_prompt = self.prompts.build(
                user_prompt,
                memories,
                inject=inject,
                prompt_files=prompt_files,
                tool_catalog_section=tool_catalog_section,
                tool_recommendations_section=tool_recommendations_section,
                tool_context=tool_context,
            )
            response = self.backend.generate(final_prompt)
            final_response = response

            if not self.config.enable_tools:
                break

            calls = self.tools.parse_tool_calls(response.text)
            if not calls:
                break

            if tool_rounds >= self.config.max_tool_rounds:
                tool_context.append("[tool-system]\n已达到最大工具轮数限制，停止继续调用工具。")
                break

            tool_rounds += 1
            tool_context.append(f"[assistant-tool-round-{tool_rounds}]\n{response.text}")
            for idx, payload in enumerate(calls, start=1):
                result = self.tools.execute_call(payload, allowed_tools=allowed_tools)
                tool_context.append(
                    f"[tool-call-{tool_rounds}-{idx}]\n{payload}\n"
                    f"[tool-result-{tool_rounds}-{idx}]\n{result.render_for_prompt()}"
                )

        assert final_response is not None
        do_save = self.config.auto_save_memory if save is None else save
        if do_save:
            self.memory.add("user", user_prompt)
            self.memory.add("agent", final_response.text, tags=[final_response.backend])

        return AgentRunResult(
            prompt=final_prompt,
            response=final_response.text,
            backend=final_response.backend,
            used_memories=len(memories),
            tool_rounds=tool_rounds,
        )

    def remember(self, content: str, *, kind: str = "note"):
        """手动写入一条记忆。"""

        return self.memory.add("user", content, kind=kind)

    def recall(self, query: str, top_k: int | None = None):
        """召回相关记忆。"""

        return self.memory.search(query, top_k or self.config.memory_top_k)

    def spawn_subagents(self, goal: str, count: int | None = None) -> list[SubAgentTask]:
        """生成子任务记录。"""

        if not self.config.enable_subagents:
            raise RuntimeError("配置已禁用 subagent。")
        n = min(count or self.config.max_subagents, self.config.max_subagents)
        return self.subagents.split(goal, n)

    def run_subagent(
        self,
        run_id: str,
        *,
        instruction: str = "",
        dry_run: bool = True,
        max_cards: int = 0,
        probe: bool = True,
    ) -> SubAgentRunnerResult:
        """按执行上下文运行一个子代理入口。

        第一版 runner 不负责并行调度，只负责把“上下文 -> 模型执行 -> 工单回写”
        这条最小链路打通。默认 dry-run，避免误触真实模型接口。
        """

        context = self.subagents.write_execution_context(run_id, max_cards=max_cards)
        prompt = _build_subagent_runner_prompt(context, instruction)
        if dry_run:
            return self.subagents.record_runner_result(
                run_id,
                dry_run=True,
                ok=True,
                message="dry-run: 已生成执行上下文和 runner prompt，未调用模型。",
                prompt=prompt,
            )

        if probe:
            probe_result = self.subagents.probe_channel(run_id)
            if probe_result.channel_status == "BROKEN":
                context = self.subagents.write_execution_context(run_id, max_cards=max_cards)
                prompt = _build_subagent_runner_prompt(context, instruction)
                return self.subagents.record_runner_result(
                    run_id,
                    dry_run=False,
                    ok=False,
                    message="通道健康检查为 BROKEN，未启动模型执行。",
                    prompt=prompt,
                    status="CHANNEL_ERROR",
                    verification_status="UNVERIFIED",
                    failure_type="channel",
                )
            context = self.subagents.write_execution_context(run_id, max_cards=max_cards)
            prompt = _build_subagent_runner_prompt(context, instruction)

        try:
            result = self.run(
                prompt,
                save=False,
                allowed_tools=context.allowed_tools,
            )
        except Exception as exc:
            return self.subagents.record_runner_result(
                run_id,
                dry_run=False,
                ok=False,
                message=f"runner 执行失败: {exc}",
                prompt=prompt,
                status="BLOCKED",
                verification_status="UNVERIFIED",
                failure_type="runner_error",
            )

        structured = parse_subagent_runner_output(result.response)
        return self.subagents.record_runner_result(
            run_id,
            dry_run=False,
            ok=structured.ok if structured.found else True,
            message="runner 已完成模型调用，等待独立验收。",
            prompt=result.prompt,
            response=result.response,
            backend=result.backend,
            tool_rounds=result.tool_rounds,
            status="" if structured.found else "AWAITING_ACCEPTANCE",
            verification_status="" if structured.found else "NEEDS_ACCEPTANCE",
            structured_output=structured,
        )

    def run_parent_planner(
        self,
        router: CapabilityRouter,
        capability_config: CapabilityConfig | None = None,
        *,
        apply: bool = False,
        execute_runners: bool = False,
        max_runners: int = 1,
        limit: int = 20,
        reviewer: str = "parent-dispatch",
        note: str = "",
        runner_instruction: str = "",
    ) -> ParentPlannerRecord:
        """运行一轮父代理 LLM planner，并写出审计报告。

        planner 不是 heartbeat 的浅层 OK，而是一个完整模型 turn。只有状态门禁发现
        有 active/pending/stalled/needs-intervention 事项时，才真正调用模型；如果模型
        在有事时只回 HEARTBEAT_OK，会被标记为失败。
        """

        cfg = capability_config or CapabilityConfig()
        state = _build_parent_planner_state(
            self,
            cfg,
            max_runners=max_runners,
            limit=limit,
            reviewer=reviewer,
            note=note,
        )
        gate = state["gate"]
        gate_summary = {key: int(value) for key, value in gate.items() if isinstance(value, int)}
        if not gate.get("needs_planner", 0):
            record = self.subagents.make_parent_planner_record(
                dry_run=not apply,
                triggered=False,
                ok=True,
                decision="HEARTBEAT_OK",
                message="planner gate 确认无 active/pending/stalled/needs-intervention 事项，允许 HEARTBEAT_OK。",
                gate_summary=gate_summary,
                summary="no work",
            )
            report = self.subagents.build_parent_planner_report([record], dry_run=not apply)
            self.subagents.write_parent_planner_report(report, append_log=apply)
            return record

        prompt = _build_parent_planner_prompt(
            state,
            apply=apply,
            execute_runners=execute_runners,
            max_runners=max_runners,
            runner_instruction=runner_instruction,
        )
        prompt_path, response_path = self.subagents.write_parent_planner_exchange(prompt)
        try:
            result = self.run(
                prompt,
                save=False,
                allowed_tools=PARENT_PLANNER_READ_TOOLS,
            )
        except Exception as exc:
            record = self.subagents.make_parent_planner_record(
                dry_run=not apply,
                triggered=True,
                ok=False,
                decision="PLANNER_ERROR",
                message=f"父代理 planner 调用失败: {exc}",
                gate_summary=gate_summary,
                prompt_path=prompt_path,
                response_path=response_path,
                evidence_paths=[prompt_path],
            )
            report = self.subagents.build_parent_planner_report([record], dry_run=not apply)
            self.subagents.write_parent_planner_report(report, append_log=apply)
            return record

        prompt_path, response_path = self.subagents.write_parent_planner_exchange(
            result.prompt,
            result.response,
        )
        parsed = parse_parent_planner_output(result.response)
        ok = parsed.found and parsed.ok
        decision = parsed.decision or "PARSE_ERROR"
        message = parsed.summary or "父代理 planner 已完成完整 LLM turn。"
        parse_error = parsed.parse_error
        if not parsed.found:
            ok = False
            decision = "PARSE_ERROR"
            parse_error = "缺少 [PARENT_PLANNER_RESULT] 结构化结果块。"
            message = "父代理 planner 有模型回复，但缺少结构化结果，不能当作 OK。"
        if parsed.decision == "HEARTBEAT_OK" and gate.get("needs_planner", 0):
            ok = False
            parse_error = parse_error or "planner gate blocked HEARTBEAT_OK"
            message = "状态门禁发现仍有待处理事项，禁止 planner 只返回 HEARTBEAT_OK。"

        record = self.subagents.make_parent_planner_record(
            dry_run=not apply,
            triggered=True,
            ok=ok,
            decision=decision,
            message=message,
            gate_summary=gate_summary,
            backend=result.backend,
            tool_rounds=result.tool_rounds,
            parse_error=parse_error,
            summary=parsed.summary,
            actions=parsed.actions,
            blockers=parsed.blockers,
            risks=parsed.risks,
            notes=parsed.notes,
            runner_instruction=parsed.runner_instruction,
            suggested_max_runners=parsed.suggested_max_runners,
            prompt_path=prompt_path,
            response_path=response_path,
            evidence_paths=[prompt_path, response_path],
        )
        report = self.subagents.build_parent_planner_report([record], dry_run=not apply)
        self.subagents.write_parent_planner_report(report, append_log=apply)
        return record

    def dispatch_subagents(
        self,
        router: CapabilityRouter,
        capability_config: CapabilityConfig | None = None,
        *,
        apply: bool = False,
        execute_runners: bool = False,
        planner: bool = False,
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

        runner_candidates = _dispatch_runner_candidates(self.subagents.list_runs(), effective_max_runners)
        for task in runner_candidates:
            before = self.subagents.load(task.id)
            if not apply:
                records.append(
                    self.subagents.make_dispatch_record(
                        step="runner",
                        action="execute_runner",
                        run_id=task.id,
                        dry_run=True,
                        applied=False,
                        ok=True,
                        message="dry-run: apply 时会生成执行上下文；带 --execute-runners 时会调用模型。",
                        before_status=before.status,
                        after_status=before.status,
                        before_verification_status=before.verification_status,
                        after_verification_status=before.verification_status,
                        evidence_paths=[before.task_dir],
                    )
                )
                continue

            result = self.run_subagent(
                task.id,
                instruction=effective_runner_instruction,
                dry_run=not execute_runners,
                max_cards=max_cards,
                probe=probe,
            )
            after = self.subagents.load(task.id)
            records.append(
                self.subagents.make_dispatch_record(
                    step="runner",
                    action="execute_runner" if execute_runners else "runner_dry_run",
                    run_id=task.id,
                    dry_run=result.dry_run,
                    applied=not result.dry_run,
                    ok=result.ok,
                    message=result.message,
                    before_status=before.status,
                    after_status=after.status,
                    before_verification_status=before.verification_status,
                    after_verification_status=after.verification_status,
                    evidence_paths=[
                        result.execution_context_json,
                        result.result_json,
                        result.output_json,
                    ],
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
    ) -> DispatchWatchReport:
        """以 watch 模式持续执行父代理调度。"""

        if max_cycles < 0:
            raise ValueError("max_cycles 不能小于 0。")
        if interval < 0:
            raise ValueError("interval 不能小于 0。")

        cfg = capability_config or CapabilityConfig()
        records = []
        lock_path = self.subagents.workspace / "subagent_dispatch_watch.lock"
        with _DispatchWatchLock(lock_path, force=force_lock) as lock:
            cycle = 0
            while max_cycles == 0 or cycle < max_cycles:
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
                self.subagents.write_dispatch_watch_heartbeat(
                    cycle=cycle,
                    status="sleeping" if more_cycles else "stopping",
                    lock_path=str(lock_path),
                    pid=os.getpid(),
                    message=message,
                )
                if not more_cycles:
                    break
                time.sleep(interval)

            self.subagents.write_dispatch_watch_heartbeat(
                cycle=cycle,
                status="stopped",
                lock_path=str(lock_path),
                pid=os.getpid(),
                message=f"watch stopped; lock={lock.token}",
            )

        report = self.subagents.build_dispatch_watch_report(records, dry_run=not apply)
        return self.subagents.write_dispatch_watch_report(report)


def _build_parent_planner_state(
    agent: SimpleAgent,
    cfg: CapabilityConfig,
    *,
    max_runners: int,
    limit: int,
    reviewer: str,
    note: str,
) -> dict[str, object]:
    """收集父代理 planner 的状态快照和 heartbeat gate。"""

    tasks = agent.subagents.list_runs()
    board = agent.subagents.build_board(recent_limit=limit)
    due_report = agent.subagents.due_check(cfg)
    action_plan = agent.subagents.plan_actions(cfg)
    runner_candidates = _dispatch_runner_candidates(tasks, max_runners)
    patch_run_ids = _dispatch_patch_review_run_ids(tasks)[:limit]
    acceptance_report = agent.subagents.review_acceptances(
        apply=False,
        reviewer=reviewer,
        note=note,
        limit=limit,
    )
    active_tasks = [
        task
        for task in tasks
        if task.status not in {"DONE", "FAILED", "TIMEOUT", "CHANNEL_ERROR", "TAKEN_OVER"}
        or task.verification_status == "NEEDS_ACCEPTANCE"
    ]
    open_requests = []
    open_gaps = []
    for task in tasks:
        for request in task.capability_requests:
            if request.status == "OPEN":
                open_requests.append(
                    {
                        "run_id": task.id,
                        "request_id": request.id,
                        "needed_capability": request.needed_capability,
                        "problem": request.problem,
                        "expected_output": request.expected_output,
                    }
                )
        for gap in task.capability_gaps:
            if gap.status == "OPEN":
                open_gaps.append(
                    {
                        "run_id": task.id,
                        "gap_id": gap.id,
                        "needed_capability": gap.needed_capability,
                        "problem": gap.problem,
                    }
                )

    gate_summary = {
        "total_tasks": len(tasks),
        "active_tasks": len(active_tasks),
        "due_issues": due_report.summary.get("total", 0),
        "action_items": action_plan.summary.get("total", 0),
        "runner_candidates": len(runner_candidates),
        "patch_reviews": len(patch_run_ids),
        "acceptance_records": len(acceptance_report.records),
        "open_capability_requests": len(open_requests),
        "open_capability_gaps": len(open_gaps),
    }
    gate_summary["needs_planner"] = int(
        any(
            gate_summary[key] > 0
            for key in (
                "active_tasks",
                "due_issues",
                "action_items",
                "runner_candidates",
                "patch_reviews",
                "acceptance_records",
                "open_capability_requests",
                "open_capability_gaps",
            )
        )
    )

    return {
        "gate": gate_summary,
        "board_summary": board.summary,
        "active_tasks": [_task_state_for_planner(task) for task in active_tasks[:limit]],
        "due_issues": [
            {
                "run_id": issue.run_id,
                "severity": issue.severity,
                "kind": issue.kind,
                "message": issue.message,
                "suggested_action": issue.suggested_action,
                "status": issue.status,
                "goal": issue.goal,
                "risk_flags": issue.risk_flags,
            }
            for issue in due_report.issues[:limit]
        ],
        "action_items": [
            {
                "run_id": item.run_id,
                "severity": item.severity,
                "priority": item.priority,
                "action": item.action,
                "reason": item.reason,
                "would_change_status_to": item.would_change_status_to,
            }
            for item in action_plan.actions[:limit]
        ],
        "runner_candidates": [_task_state_for_planner(task) for task in runner_candidates],
        "patch_review_run_ids": patch_run_ids,
        "acceptance_records": [
            {
                "run_id": record.run_id,
                "decision": record.decision,
                "ok": record.ok,
                "message": record.message,
                "evidence_count": record.evidence_count,
                "test_count": record.test_count,
            }
            for record in acceptance_report.records[:limit]
        ],
        "open_capability_requests": open_requests[:limit],
        "open_capability_gaps": open_gaps[:limit],
    }


def _build_parent_planner_prompt(
    state: dict[str, object],
    *,
    apply: bool,
    execute_runners: bool,
    max_runners: int,
    runner_instruction: str,
) -> str:
    """构建父代理 planner 的完整 LLM turn prompt。"""

    payload = json.dumps(state, ensure_ascii=False, indent=2)
    mode = "apply" if apply else "dry-run"
    return (
        "# Parent Planner Tick\n\n"
        "你是父代理 planner。这个 tick 来自定时 watch，不是浅层 heartbeat。\n"
        "你必须根据状态快照判断是否有待处理事项；如果有 active/pending/stalled/"
        "needs-intervention，不允许只返回 HEARTBEAT_OK。\n\n"
        "你可以使用只读工具核对状态，但不要直接写文件。真正写回由调度器按审计流程执行。\n\n"
        "## Runtime\n\n"
        f"- mode: {mode}\n"
        f"- execute_runners: {execute_runners}\n"
        f"- cli_max_runners: {max_runners}\n"
        f"- existing_runner_instruction: {runner_instruction or 'none'}\n\n"
        "## State Snapshot\n\n"
        "```json\n"
        f"{payload}\n"
        "```\n\n"
        "## Decision Rules\n\n"
        "- 如果 gate.needs_planner 为 0，可以返回 HEARTBEAT_OK。\n"
        "- 如果 gate.needs_planner 为 1，必须给出 DISPATCH 或 BLOCKED_REPORT。\n"
        "- 你可以建议 runner_instruction，但不能提高 cli_max_runners，只能建议更小或相等的数量。\n"
        "- 你输出的 actions 只是建议；系统会再用规则调度器验证和执行。\n\n"
        "## Required Output\n\n"
        "最后必须输出一个机器可解析结果块，格式如下。结果块里只能放裸 JSON object，"
        "不要使用 Markdown 代码围栏。\n\n"
        "[PARENT_PLANNER_RESULT]\n"
        "{\n"
        '  "decision": "DISPATCH",\n'
        '  "summary": "本轮父代理判断摘要",\n'
        '  "should_dispatch": true,\n'
        '  "runner_instruction": "给本轮 runner 的额外指令，可为空",\n'
        '  "suggested_max_runners": 1,\n'
        '  "actions": [\n'
        '    {"action": "execute_runner|review_acceptance|route_capability|takeover|report_blocker", "run_id": "", "priority": 1, "reason": ""}\n'
        "  ],\n"
        '  "blockers": [],\n'
        '  "risks": [],\n'
        '  "notes": []\n'
        "}\n"
        "[/PARENT_PLANNER_RESULT]\n"
    )


def _task_state_for_planner(task: SubAgentTask) -> dict[str, object]:
    """压缩任务状态，避免把完整工单塞进 planner prompt。"""

    return {
        "run_id": task.id,
        "status": task.status,
        "verification_status": task.verification_status,
        "channel_status": task.channel_status,
        "goal": task.goal,
        "owner": task.owner,
        "final_owner": task.final_owner,
        "updated_at": task.updated_at,
        "heartbeat_at": task.heartbeat_at,
        "evidence_count": len(task.evidence),
        "open_request_count": sum(1 for item in task.capability_requests if item.status == "OPEN"),
        "open_gap_count": sum(1 for item in task.capability_gaps if item.status == "OPEN"),
    }


def _combine_runner_instruction(base: str, planner_instruction: str) -> str:
    """合并 CLI 指令和 planner 指令，保持 CLI 指令优先可见。"""

    base = base.strip()
    planner_instruction = planner_instruction.strip()
    if base and planner_instruction:
        return f"{base}\n\n父代理 planner 补充指令：{planner_instruction}"
    return base or planner_instruction


def _build_subagent_runner_prompt(
    context: SubAgentExecutionContext,
    instruction: str = "",
) -> str:
    """把执行上下文压成子代理 runner 的用户任务。"""

    payload = json.dumps(asdict(context), ensure_ascii=False, indent=2)
    extra = instruction.strip() or "按执行上下文完成任务；如果能力不足，说明需要上抛的 capability_request。"
    return (
        "# SubAgent Runner Task\n\n"
        "你是一个被父代理授权的子代理，只能依据下面的执行上下文工作。\n"
        "不要使用上下文之外的 skill/tool，不要假完成；没有验收证据时只能标记等待验收或上抛能力请求。\n\n"
        "## Extra Instruction\n\n"
        f"{extra}\n\n"
        "## Execution Context JSON\n\n"
        "```json\n"
        f"{payload}\n"
        "```\n\n"
        "## Required Output\n\n"
        "- 说明完成了什么或卡在哪里。\n"
        "- 列出使用过的授权工具或 skill。\n"
        "- 给出可验收证据；如果没有证据，明确写出还需要什么能力或工具。\n"
        "- 最后必须输出一个机器可解析结果块，格式如下：\n\n"
        "注意：结果块里面只能放裸 JSON object，不要使用 ```json 或任何 Markdown 代码围栏。\n"
        "在最终结果块之前，不要把 [SUBAGENT_RESULT] 或 [/SUBAGENT_RESULT] 当作普通说明文字重复引用。\n\n"
        "[SUBAGENT_RESULT]\n"
        "{\n"
        '  "status": "AWAITING_ACCEPTANCE",\n'
        '  "summary": "本轮完成或卡住的摘要",\n'
        '  "used_tools": [],\n'
        '  "used_skills": [],\n'
        '  "evidence": [\n'
        '    {"kind": "command", "summary": "验证摘要", "command": "", "path": "", "url": "", "ok": true}\n'
        "  ],\n"
        '  "capability_requests": [\n'
        '    {"problem": "缺少什么", "needed_capability": "能力名", "expected_output": "希望得到什么", "tried": [], "evidence": [], "constraints": {}}\n'
        "  ],\n"
        '  "artifacts": [\n'
        '    {"path": "产物路径", "kind": "file|report|log", "summary": "产物说明"}\n'
        "  ],\n"
        '  "tests": [\n'
        '    {"name": "测试名称", "command": "运行命令", "ok": true, "summary": "测试结果摘要"}\n'
        "  ],\n"
        '  "patches": [\n'
        '    {"path": "改动文件", "status": "applied|planned|blocked", "summary": "改了什么或准备改什么"}\n'
        "  ],\n"
        '  "lessons": ["可沉淀经验，适合未来变成 skill 或规则"],\n'
        '  "next_actions": ["建议父代理下一步动作"],\n'
        '  "blocked_reason": "",\n'
        '  "failure_type": ""\n'
        "}\n"
        "[/SUBAGENT_RESULT]\n"
    )


def _dispatch_runner_candidates(tasks: list[SubAgentTask], max_runners: int) -> list[SubAgentTask]:
    """挑选一轮 dispatch 可推进的 runner。"""

    if max_runners <= 0:
        return []
    candidates: list[SubAgentTask] = []
    for task in tasks:
        if not _is_dispatch_runner_candidate(task):
            continue
        candidates.append(task)
        if len(candidates) >= max_runners:
            break
    return candidates


def _is_dispatch_runner_candidate(task: SubAgentTask) -> bool:
    """判断任务是否可以由 dispatch 启动 runner。"""

    if task.status in {
        "AWAITING_ACCEPTANCE",
        "DONE",
        "FAILED",
        "TIMEOUT",
        "CHANNEL_ERROR",
        "TAKEN_OVER",
    }:
        return False
    if task.verification_status in {"NEEDS_ACCEPTANCE", "VERIFIED"}:
        return False
    if task.channel_status == "BROKEN":
        return False
    if any(item.status == "OPEN" for item in task.capability_requests):
        return False
    if any(item.status == "OPEN" for item in task.capability_gaps):
        return False
    if task.status == "BLOCKED":
        return task.failure_type == "capability_request" and bool(task.capability_grants)
    return task.status in {"PLANNING", "RUNNING"}


def _dispatch_patch_review_run_ids(tasks: list[SubAgentTask]) -> list[str]:
    """挑选本轮调度需要审核 patch 的 run。"""

    run_ids: list[str] = []
    for task in tasks:
        if task.status != "AWAITING_ACCEPTANCE" and task.verification_status != "NEEDS_ACCEPTANCE":
            continue
        if _task_has_runner_patches(task):
            run_ids.append(task.id)
    return run_ids


def _task_has_runner_patches(task: SubAgentTask) -> bool:
    """读取 output.json 判断是否有 patch 记录。"""

    try:
        payload = json.loads(Path(task.output_json).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return False
    return isinstance(payload.get("patches"), list) and bool(payload.get("patches"))


class _DispatchWatchLock:
    """简单跨平台文件锁，避免多个父代理同时 watch。"""

    def __init__(self, path: Path, *, force: bool = False):
        self.path = path
        self.force = force
        self.token = uuid.uuid4().hex
        self.acquired = False

    def __enter__(self) -> "_DispatchWatchLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.force and self.path.exists():
            self.path.unlink()
        payload = {
            "token": self.token,
            "pid": os.getpid(),
            "created_at": time.time(),
        }
        try:
            with self.path.open("x", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, indent=2))
        except FileExistsError as exc:
            raise RuntimeError(
                f"dispatch watch lock 已存在: {self.path}；确认没有父代理在运行后可使用 --force-lock。"
            ) from exc
        self.acquired = True
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if not self.acquired or not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if payload.get("token") == self.token:
            self.path.unlink()
