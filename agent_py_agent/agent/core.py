from __future__ import annotations

"""智能体核心循环。"""

import json
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
    SubAgentExecutionContext,
    SubAgentManager,
    SubAgentRunnerResult,
    SubAgentTask,
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

    def dispatch_subagents(
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

        runner_candidates = _dispatch_runner_candidates(self.subagents.list_runs(), max_runners)
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
                instruction=runner_instruction,
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
