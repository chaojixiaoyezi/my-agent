
# LLM: runner 结果服务先核对 exact attempt，再组装并保存结果；运行账结算和父级通知交给唯一提交模块按原顺序推进。
# 模块用途: 保存通过准入的子代理本轮结果与文件交接，调用既有可恢复收口链。
from __future__ import annotations

"""Runner result recording and debrief persistence service."""

import time
from pathlib import Path

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


def _runner_append_debrief(task, parsed):
    """Append runner debrief content."""
    _append_runner_debrief_content(task, parsed)


class _PostResultSideEffectParams:

    def __init__(self, output_payload: dict, dry_run: bool, parsed: SubAgentParsedOutput, lessons: list):
        self.output_payload = output_payload
        self.dry_run = dry_run
        self.parsed = parsed
        self.lessons = lessons


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

    # LLM: 结构化输出是可选内容，工具账本是独立事实源；自然 final 同样交付产物，不要求模型复述 JSON。
    # 函数用途: 收集合规的结果字段和真实文件记录，修改 task 的交接投影但不代写业务文件。
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
        return extracted

    def _apply_status_and_build_payload(
        self,
        params: RecordRunnerResultParams,
        extracted: _ApplyStatusParams,
        now: float,
    ) -> tuple[dict, BuildAndPersistContext]:
        """Apply status to task and build output payload."""
        return apply_status_and_build_payload(params, extracted, now)

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
        memory_candidates = []
        if not params.dry_run and parsed.found and parsed.ok:
            memory_candidates = self.manager.memory_candidates.record_result_candidates(
                task,
                lessons=params.lessons,
                findings=list(getattr(task, "findings", []) or []),
            )
        self.manager.actions._append_task_work_log(
            task,
            f"subagent_runner: dry_run={params.dry_run} ok={result.ok} status={task.status} "
            f"message={result.message} memory_candidates={len(memory_candidates)}",
        )
        self.manager.indexing.index_runner_result(result, output_payload)
        return len(memory_candidates)

    # LLM: 先核对 exact attempt，再落结果文件和任务投影；提交模块随后持久化恢复事实、收口与通知。
    # 函数用途: 写回通过准入的子代理结果并启动原可靠交接；旧轮被拒，UNKNOWN 不自动重跑。
    def record_runner_result(
        self,
        params: RecordRunnerResultParams,
    ) -> SubAgentRunnerResult:
        task = self.manager.load(params.run_id)
        stale_result = reject_stale_runner_result(self.manager, task, params)
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
            _PostResultSideEffectParams(output_payload, params.dry_run, extracted.parsed, extracted.lessons),
        )
        commit_runner_result(self.manager, task, params, result, output_payload)
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
