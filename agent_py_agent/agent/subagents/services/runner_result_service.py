
# LLM: 此服务是初次结果提交的依赖装配边界；runner 结果服务先核对 exact attempt，再组装并保存结果；运行账结算和父级通知交给唯一提交模块按原顺序推进。
#   lesson 账本（record_lesson 工具写的 lessons.jsonl）在提取阶段读回并合并进 lessons；账本经验不依赖结构化输出也会记成候选。
# 模块用途: 保存通过准入的子代理本轮结果与文件交接，调用既有可恢复收口链。
from __future__ import annotations

"""Runner result recording and debrief persistence service."""

import time
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

from .. import debug_trace, runner_completion_wake
from ..lesson_ledger import (
    LEDGER_ABSENT,
    LessonLedgerReport,
    merge_lesson_texts,
    read_lesson_ledger,
)
from ..manager_runner_result_payload import (
    BuildAndPersistContext,
    RecordRunnerResultParams,
    _ApplyStatusParams,
    _ExtractedOutput,
    apply_status_and_build_payload,
)
from ..models import SubAgentParsedOutput, SubAgentRunnerResult, SubAgentTask, TaskStatus
from ..result_processors import (
    RunnerResultContext,
    _append_runner_debrief_content,
    _build_runner_result,
    _process_structured_output,
    _write_runner_result_files,
    merge_actual_tools_for_unparsed,
)
from ..result_registered_artifacts import collect_registered_artifacts
from ..runner_rendering import render_runner_result_markdown
from ..runner_result_admission import reject_stale_runner_result
from ..tool_failure_ledger import record_tool_failure_ledger
from ..utils import _apply_missing_paths
from .runner_result_commit import commit_runner_result
from .runtime_closeout import deliver_parent_wake


def _runner_append_debrief(task, parsed):
    """Append runner debrief content."""
    _append_runner_debrief_content(task, parsed)


# LLM: 自学习提案是结果交付后的可选旁支：manager.skill_proposals 仅在 enable_self_learning 开启时由组合根注入；
#   这里只把本批已落盘的 Candidate 交给它，任何异常只变成工作日志片段，绝不影响结果保存、提交或父级通知。
# 函数用途: 尝试为本次 lesson 候选生成待确认 Skill 提案，返回追加到工作日志的简短结果。
def _skill_proposal_note(manager, memory_candidates: list) -> str:
    service = getattr(manager, "skill_proposals", None)
    if service is None or not memory_candidates:
        return ""
    try:
        created = service.propose_from_candidates(memory_candidates)
    except Exception as exc:  # noqa: BLE001 - 可选提案失败不得影响子代理结果交付。
        return f" skill_proposals_error={type(exc).__name__}"
    return f" skill_proposals={len(created)}"


# LLM: lessons 为合并后的最终列表；lesson_ledger 默认 absent，保持旧调用方按位置构造不变。
# 类用途: 打包结果落盘后副作用需要的载荷、解析结果与经验来源。
@dataclass
class _PostResultSideEffectParams:
    output_payload: dict
    dry_run: bool
    parsed: SubAgentParsedOutput
    lessons: list
    lesson_ledger: LessonLedgerReport = field(default_factory=LessonLedgerReport)


# LLM: 账本是 record_lesson 写下的宿主事实源，与结构化输出里的 lessons 并列；只按账本合同读回、合并去重，
#   不解析模型正文。读回报告挂到 extracted 上供候选使用；路径为空或文件不存在时 lessons 原样不变。
# 函数用途: 把本 run 账本里的经验合并进结果 lessons（结构化在前、账本在后、保持顺序去重）。
def _merge_lesson_ledger(task: SubAgentTask, extracted: _ExtractedOutput) -> None:
    ledger = read_lesson_ledger(str(getattr(task, "agent_run_lessons_jsonl", "") or ""), run_id=str(task.id or ""))
    extracted.lesson_ledger = ledger
    extracted.lessons = merge_lesson_texts(extracted.lessons, ledger.entries)


# LLM: 结构化输出的 lesson/finding 仍只在解析成功时记录；账本经验来自 record_lesson 工具，自然回复（没有结构化输出）
#   也记录。候选是结果交付后的旁支，任何异常只变成工作日志片段，不能阻断结果保存、提交与父级通知。
# 函数用途: 按结构化输出与 lesson 账本记录本次 owner 记忆候选，返回候选列表和工作日志附注。
def _record_memory_candidates(manager, task: SubAgentTask, params: _PostResultSideEffectParams) -> tuple[list, str]:
    structured = params.parsed.found and params.parsed.ok
    entries = params.lesson_ledger.entries
    note = _lesson_ledger_note(params.lesson_ledger)
    if params.dry_run or not (structured or entries):
        return [], note
    try:
        candidates = manager.memory_candidates.record_result_candidates(
            task,
            lessons=params.lessons,
            findings=list(getattr(task, "findings", []) or []) if structured else [],
            lesson_entries=entries,
        )
    except Exception as exc:  # noqa: BLE001 - 可选候选失败不得影响子代理结果交付。
        return [], f"{note} memory_candidates_error={type(exc).__name__}"
    return list(candidates), note


# LLM: 只在账本存在时写附注，状态/采用/拒绝数都是结构化计数，账本不存在时保持旧日志格式。
# 函数用途: 生成 lesson 账本读回情况的工作日志片段。
def _lesson_ledger_note(ledger: LessonLedgerReport) -> str:
    if ledger.status == LEDGER_ABSENT and not ledger.rejected:
        return ""
    return (
        f" lesson_ledger={ledger.status} lesson_ledger_entries={len(ledger.entries)}"
        f" lesson_ledger_rejected={ledger.rejected}"
    )


class _RunnerResultBuildParams:

    def __init__(self, params: RecordRunnerResultParams, extracted: _ExtractedOutput, now: float):
        self.params = params
        self.extracted = extracted
        self.now = now


# LLM: 此服务编排准入和结果持久化；终态提交与父级通知由 runner_result_commit 按原顺序推进，不能旁路写终态。
# 类用途: 收集子代理的自然与结构化结果，保存后交给唯一可恢复收口入口。
class SubAgentRunnerResultService:
    """Persist parsed runner output and update the owning subagent task."""

    def __init__(self, manager):
        self.manager = manager

    def _build_and_persist_result(
        self,
        ctx: BuildAndPersistContext,
    ) -> SubAgentRunnerResult:
        """Build runner result and persist files."""
        result = _build_runner_result(
            RunnerResultContext(
                task=ctx.task,
                dry_run=ctx.params.dry_run,
                ok=ctx.final_ok,
                message=ctx.final_message,
                backend=ctx.params.backend,
                tool_rounds=ctx.params.tool_rounds,
                prompt=ctx.params.prompt,
                response=ctx.params.response,
                turn_end_reason=ctx.params.turn_end_reason,
                parsed=ctx.parsed,
                structured_repair_attempted=ctx.params.structured_repair_attempted,
                structured_repair_ok=ctx.params.structured_repair_ok,
                structured_repair_error=ctx.params.structured_repair_error,
                structured_evidence_count=ctx.structured_evidence_count,
                structured_request_count=ctx.structured_request_count,
                artifact_count=len(ctx.artifacts),
                test_count=len(ctx.tests),
                patch_count=len(ctx.patches),
                lesson_count=len(ctx.lessons),
                now=ctx.now,
            )
        )
        _write_runner_result_files(ctx.task, result, ctx.output_payload, prompt=ctx.params.prompt, response=ctx.params.response)
        Path(ctx.task.runner_result_file).write_text(render_runner_result_markdown(result), encoding="utf-8")
        return result

    # LLM: 结构化输出是可选内容，工具账本是独立事实源；自然 final 同样交付产物与 lesson 账本经验，不要求模型复述 JSON。
    # 函数用途: 收集合规的结果字段、真实文件记录与 lesson 账本，修改 task 的交接投影但不代写业务文件。
    def _extract_parsed_output(
        self,
        task: SubAgentTask,
        structured_output: SubAgentParsedOutput | None,
        now: float,
        actual_tools: list[str] | None,
    ) -> _ExtractedOutput:
        """Extract parsed output or return defaults."""
        parsed = structured_output or SubAgentParsedOutput()
        if parsed.found and parsed.ok:
            proc = _process_structured_output(task, parsed, now, actual_tools)
            extracted = _ExtractedOutput(
                parsed=parsed,
                ignored_tools=proc["ignored_tools"],
                ignored_skills=proc["ignored_skills"],
                structured_evidence_count=proc["structured_evidence_count"],
                structured_request_count=proc["structured_request_count"],
                created_request_ids=proc["created_request_ids"],
                artifacts=proc["artifacts"],
                evidence_packets=proc["evidence_packets"],
                findings=proc["findings"],
                tests=proc["tests"],
                patches=proc["patches"],
                lessons=proc["lessons"],
                next_actions=proc["next_actions"],
            )
        else:
            merge_actual_tools_for_unparsed(task, actual_tools, now)
            extracted = _ExtractedOutput(parsed=parsed)
        observed = collect_registered_artifacts(task)
        by_path = {str(item.get("path") or ""): item for item in extracted.artifacts}
        by_path.update({str(item["path"]): item for item in observed})
        extracted.artifacts = list(by_path.values())
        _merge_lesson_ledger(task, extracted)
        return extracted

    def _apply_status_and_build_payload(
        self,
        params: RecordRunnerResultParams,
        extracted: _ApplyStatusParams,
        now: float,
    ) -> tuple[dict, BuildAndPersistContext]:
        """Apply status to task and build output payload."""
        return apply_status_and_build_payload(params, extracted, now)

    # LLM: 结果保存后的副作用顺序固定：保存 task→debrief→统一 Memory 候选→可选 Skill 提案→工作日志→索引；
    #   候选含结构化输出与 lesson 账本两个来源；候选与 Skill 提案失败都只写工作日志，返回值仍是候选数量。
    # 函数用途: 处理子代理结果落盘后的保存、交接、候选、提案与索引副作用。
    def _post_result_side_effects(
        self,
        task: SubAgentTask,
        result: SubAgentRunnerResult,
        params: _PostResultSideEffectParams,
    ) -> int:
        """Handle save, debrief, unified Memory candidate and indexing side effects."""
        output_payload = params.output_payload
        parsed = params.parsed
        if parsed.found and parsed.ok:
            task.latest_summary = parsed.summary or task.latest_summary
            if str(task.status or "").strip().upper() == TaskStatus.RUNNING.value:
                task.current_step = parsed.status or task.status
        for blocker in output_payload.get("blockers", []) or []:
            text = str(blocker or "").strip()
            if text and text not in task.blockers:
                task.blockers.append(text)
        self.manager.save(task)
        if parsed.found and parsed.ok:
            _runner_append_debrief(task, parsed)
        memory_candidates, candidate_note = _record_memory_candidates(self.manager, task, params)
        proposal_note = _skill_proposal_note(self.manager, memory_candidates)
        self.manager.actions._append_task_work_log(
            task,
            f"subagent_runner: dry_run={params.dry_run} ok={result.ok} status={task.status} "
            f"message={result.message} memory_candidates={len(memory_candidates)}{candidate_note}{proposal_note}",
        )
        self.manager.indexing.index_runner_result(result, output_payload)
        return len(memory_candidates)

    # LLM: 此处装配原 RuntimeDB、save 及只持四项必要依赖的通知器；提交和终态通知均不接收 manager；
    # 仅向准入传入原 RuntimeDB 与 canonical task；核对 exact attempt 后再落盘，提交模块按原顺序收口与通知。
    # lesson 账本读回报告随副作用参数交给候选记录；重放同一结果不会重复候选（observation_id 幂等）。
    # 函数用途: 写回通过准入的子代理结果并启动原可靠交接；旧轮被拒，UNKNOWN 不自动重跑。
    def record_runner_result(
        self,
        params: RecordRunnerResultParams,
    ) -> SubAgentRunnerResult:
        task = self.manager.load(params.run_id)
        stale_result = reject_stale_runner_result(self.manager.runtime_db, task, params)
        if stale_result:
            return stale_result

        _apply_missing_paths(task, self.manager._build_work_order_paths(task.id, task.task_dir or None))
        self.manager.save(task)
        now = time.time()

        # 系统级工具失败账本(A1):在解析模型输出之前先落系统事实,
        # 后续 build/persist 链路会随任务一起落盘。None(超时/异常)不覆盖旧账本。
        record_tool_failure_ledger(task, params.tool_failures, now)
        extracted = self._extract_parsed_output(task, params.structured_output, now, params.actual_tools)
        output_payload, build_ctx = self._runner_result_build_context(
            _RunnerResultBuildParams(params, extracted, now),
            task,
        )
        result = self._build_and_persist_result(build_ctx)
        self._post_result_side_effects(
            task,
            result,
            _PostResultSideEffectParams(
                output_payload, params.dry_run, extracted.parsed, extracted.lessons, extracted.lesson_ledger
            ),
        )
        trace_result = partial(
            debug_trace.trace_runner_result,
            debug_trace.SubAgentRunnerTraceRequest(self.manager, task, result, params),
        )
        store = self.manager.conversation_store
        notifier = runner_completion_wake.RunnerCompletionNotifier(
            tasks=store.tasks if store is not None else None,
            wakes=store.wakes if store is not None else None,
            load_task=self.manager.load, save_task=self.manager.save,
        )
        notify_parent = partial(
            notifier.notify_result, task, result, output_payload, attempt_id=str(params.attempt_id or ""),
        )

        # LLM: 仅闭包绑定本轮原追踪与通知；提交模块在 WAL／RuntimeDB 结算后调用，不能提前执行。
        # 函数用途: 先写原调试记录，再投递本轮父级通知；通知失败转为可恢复结果。
        def deliver_result() -> str:
            trace_result()
            return deliver_parent_wake(notify_parent)

        commit_runner_result(
            self.manager.runtime_db, task, params, result,
            save_task=self.manager.save, deliver_result=deliver_result,
        )
        return result

    def _runner_result_build_context(
        self,
        build_params: _RunnerResultBuildParams,
        task: SubAgentTask,
    ):
        params = build_params.params
        extracted = build_params.extracted
        output_payload, build_ctx = self._apply_status_and_build_payload(
            params,
            _ApplyStatusParams(
                task=task,
                parsed=extracted.parsed,
                structured_evidence_count=extracted.structured_evidence_count,
                structured_request_count=extracted.structured_request_count,
                created_request_ids=extracted.created_request_ids,
                structured_repair_attempted=params.structured_repair_attempted,
                structured_repair_ok=params.structured_repair_ok,
                structured_repair_error=params.structured_repair_error,
                actual_tools=params.actual_tools,
                ignored_tools=extracted.ignored_tools,
                ignored_skills=extracted.ignored_skills,
                artifacts=extracted.artifacts,
                evidence_packets=extracted.evidence_packets,
                findings=extracted.findings,
                tests=extracted.tests,
                patches=extracted.patches,
                lessons=extracted.lessons,
                next_actions=extracted.next_actions,
                turn_end_reason=params.turn_end_reason,
            ),
            build_params.now,
        )
        # Override params in context with actual params object for full field access
        build_ctx.params = params
        return output_payload, build_ctx
