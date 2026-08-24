
from __future__ import annotations

"""Runner result recording and debrief persistence service."""

import time
from pathlib import Path

from ...turn_end import subagent_outcome_for_turn_end
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
from ..runner_rendering import render_runner_result_markdown
from ..tool_failure_ledger import record_tool_failure_ledger
from ..utils import _apply_missing_paths


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
            return _ExtractedOutput(
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
        merge_actual_tools_for_unparsed(task, actual_tools, now)
        return _ExtractedOutput(parsed=parsed)

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

    def record_runner_result(
        self,
        params: RecordRunnerResultParams,
    ) -> SubAgentRunnerResult:
        task = self.manager.load(params.run_id)
        stale_result = self._check_stale_runner_result(task, params)
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
        from ..debug_trace import SubAgentRunnerTraceRequest, trace_runner_result
        from ..runner_completion_wake import notify_parent_on_runner_result

        trace_runner_result(SubAgentRunnerTraceRequest(self.manager, task, result, params))
        notify_parent_on_runner_result(self.manager, task, result, output_payload)
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

    # LLM: Canonical task projection and runtime.db form one commit fence. In
    # MANAGED mode an exact current attempt may project while active, after its
    # resumable slice was settled with the AgentRun still created, after a
    # successful completed run, or after matching cancelled/failed settlement;
    # a mismatched late result is archival evidence only.
    # 函数用途: 在写入子代理最终状态前，拦住旧轮次和与已取消/失败事实冲突的迟到回复。
    def _check_stale_runner_result(
        self,
        task: SubAgentTask,
        params: RecordRunnerResultParams,
    ) -> SubAgentRunnerResult | None:
        attempt_id = params.attempt_id
        dry_run = params.dry_run
        if not dry_run and (_task_text_attr(task, "status").upper() == "TAKEN_OVER" or _task_text_attr(task, "takeover_by")):
            return self._make_quick_result(task, dry_run, False, "ignored runner result for already taken-over run")
        normalized_attempt_id = str(attempt_id or "").strip()
        if normalized_attempt_id:
            if normalized_attempt_id in _task_list_attr(task, "runner_abandoned_attempt_ids"):
                return self._make_quick_result(task, dry_run, False, f"ignored stale runner result for abandoned attempt {normalized_attempt_id}")
            active_attempt_id = _task_text_attr(task, "runner_active_attempt_id")
            if not active_attempt_id:
                return self._make_quick_result(task, dry_run, False, f"ignored duplicate runner result for inactive attempt {normalized_attempt_id}")
            if active_attempt_id and active_attempt_id != normalized_attempt_id:
                return self._make_quick_result(task, dry_run, False, f"ignored stale runner result for non-active attempt {normalized_attempt_id}")
            conflict = self._managed_runtime_result_conflict(task, params)
            if conflict:
                return self._make_quick_result(task, dry_run, False, conflict)
        return None

    # LLM: Keep the service class as orchestration only; the commit-fence rules
    # live in one module helper so adding lifecycle cases does not grow this class.
    # 函数用途: 核对 runtime.db 的当前 attempt 与本次回写是否同一事实。
    def _managed_runtime_result_conflict(
        self,
        task: SubAgentTask,
        params: RecordRunnerResultParams,
    ) -> str:
        return _managed_runtime_result_conflict(self.manager, task, params)

    def _make_quick_result(self, task, dry_run, ok, message):
        return SubAgentRunnerResult(
            run_id=task.id, dry_run=dry_run, ok=ok, status=task.status,
            verification_status=task.verification_status, message=message,
            turn_end_reason=str(getattr(task, "turn_end_reason", "") or ""),
            runner_attempts=task.runner_attempts, runner_last_error=task.runner_last_error,
            execution_context_json=task.execution_context_json, execution_context_file=task.execution_context_file,
            prompt_file=task.runner_prompt_file, response_file=task.runner_response_file,
            result_file=task.runner_result_file, result_json=task.runner_result_json,
            output_json=task.output_json, created_at=time.time(),
        )


# LLM: Runtime terminal states are host-owned facts. A settled nonterminal
# slice accepts only the matching host turn reason; completed/cancelled/failed
# results still require the corresponding run-level settlement.
# 函数用途: 按 runtime.db 权威链判断 runner 结果是否与当前轮次冲突。
def _managed_runtime_result_conflict(
    manager: object,
    task: SubAgentTask,
    params: RecordRunnerResultParams,
) -> str:
    repo = getattr(manager, "runtime_db", None)
    if repo is None or params.dry_run:
        return ""
    authority = repo.runner_result_commit_authority(
        run_id=str(task.id or ""),
        attempt_id=str(params.attempt_id or ""),
    )
    if authority is None:
        return f"ignored runner result without runtime authority for attempt {params.attempt_id}"
    if not bool(authority.get("is_current")):
        return f"ignored stale runner result for superseded attempt {params.attempt_id}"
    run_status = str(authority.get("run_status") or "")
    attempt_status = str(authority.get("attempt_status") or "")
    if run_status in {"", "created"} and attempt_status == "running":
        return ""
    incoming_terminal = _runner_runtime_terminal_status(params)
    if _runner_result_matches_settled_attempt(run_status, attempt_status, params):
        return ""
    if run_status == "done" and attempt_status == "done":
        return ""
    if run_status in {"failed", "cancelled"} and (
        attempt_status == run_status and incoming_terminal == run_status
    ):
        return ""
    return (
        "ignored runner result conflicting with runtime terminal fact "
        f"run={run_status or 'unknown'} attempt={attempt_status or 'unknown'} "
        f"incoming={incoming_terminal or 'nonterminal'}"
    )


def _task_text_attr(task, name: str) -> str:
    value = getattr(task, name, "")
    return value.strip() if isinstance(value, str) else ""


def _task_list_attr(task, name: str) -> list[str]:
    value = getattr(task, name, [])
    if not isinstance(value, list | tuple | set):
        return []
    return [str(item) for item in value if str(item or "").strip()]


# LLM: This mapping consumes only the structured runner status contract. It is
# deliberately narrower than task display statuses and never reads response text.
# 函数用途: 把子代理回写状态归一到 runtime.db 的三种终态，供冲突校验。
def _runner_runtime_terminal_status(params: RecordRunnerResultParams) -> str:
    raw_status = str(params.status or "").strip().upper()
    if not raw_status and params.structured_output is not None:
        raw_status = str(getattr(params.structured_output, "status", "") or "").strip().upper()
    if raw_status == TaskStatus.DONE.value:
        return "done"
    if raw_status in {
        TaskStatus.CANCELLED.value,
        TaskStatus.ABANDONED.value,
        TaskStatus.TAKEN_OVER.value,
    }:
        return "cancelled"
    if raw_status in {
        TaskStatus.FAILED.value,
        TaskStatus.TIMEOUT.value,
        TaskStatus.CHANNEL_ERROR.value,
    }:
        return "failed"
    return ""


# LLM: A settled current attempt may update only to the exact host-facing
# nonterminal state derived from its typed turn_end_reason while AgentRun stays
# created. Model prose and parsed output cannot obtain post-settlement authority.
# 函数用途: 判断一次已结束的执行片段是否正把同一子代理交回可续跑或等待状态。
def _runner_result_matches_settled_attempt(
    run_status: str,
    attempt_status: str,
    params: RecordRunnerResultParams,
) -> bool:
    if run_status not in {"", "created"} or attempt_status != "done":
        return False
    raw_status = str(params.status or "").strip().upper()
    expected_status, _failure_type, _ok = subagent_outcome_for_turn_end(
        params.turn_end_reason
    )
    if expected_status not in {
        TaskStatus.PENDING.value,
        TaskStatus.BLOCKED.value,
    }:
        return False
    return raw_status == expected_status
