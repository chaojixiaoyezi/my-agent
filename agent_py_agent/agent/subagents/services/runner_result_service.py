
from __future__ import annotations

"""Runner result recording and debrief persistence service."""

import time
from pathlib import Path

from ...runtime_db.operations import AGENT_RUN_TERMINAL_STATUSES, RUN_STATUS_LEGACY_CREATED
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
from .runtime_closeout import (
    REJECTED_CLOSEOUT_STATES,
    RETRYABLE_CLOSEOUT_STATES,
    clear_closeout,
    closeout_target_run_status,
    deliver_parent_wake,
    ensure_closeout_fact,
    mark_closeout_delivered,
    record_closeout_event,
    record_unpersisted_closeout,
    settle_runtime_run_for_result,
)


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

    # LLM: 正式结果先持久化恢复事实再收口和通知；执行器退出且副作用未知的 BLOCKED 也保留通知 WAL。
    # 函数用途: 写回当前子代理结果并可靠通知父级，旧 attempt 不得覆盖新轮，UNKNOWN 不得自动重跑。
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
        # 顺序（可恢复）：先写待重试事实 → runtime 权威 run 收口 → 提交成功才通知父级 → 持久化"已交付"再清账。
        # 1) 事实先行：收口写库失败、或进程在收口后通知前中断时，"还要补收口/补通知"必须已经
        #    持久化，否则 task/wake 终态而 runtime 仍 created 的矛盾会永久留档。
        # 2) 事实必须以**某种**权威介质落下来才算数：canonical task WAL 写不进去时，退到 runtime
        #    事件账本（另一个存储域）留同身份的可达恢复事实；两处都写不进去时**不再继续**收口与
        #    通知（否则会留下"父级永远收不到通知且无人可查"的窗口），改为留下响亮诊断后返回。
        target_run_status = closeout_target_run_status(params, result, task)
        wal_state = "not_required"
        if target_run_status or params.failure_type == "executor_effects_unknown":
            wal_state = ensure_closeout_fact(
                self.manager, task, params, result,
                {"state": "pending", "target_run_status": target_run_status},
            )
            if wal_state == "unpersisted":
                record_unpersisted_closeout(self.manager, task, params, result)
                return result
        outcome = settle_runtime_run_for_result(self.manager, task, params, result)
        state = str(outcome.get("state") or "")
        if state in RETRYABLE_CLOSEOUT_STATES:
            # 权威事实尚未落成：刷新待重试事实，由确定性恢复链补收口与补通知，本片不冒充完成。
            if wal_state != "not_required":
                ensure_closeout_fact(self.manager, task, params, result, outcome)
            record_closeout_event(
                self.manager, task, event_type="closeout_pending", params=params, outcome=outcome
            )
            return result
        if state in REJECTED_CLOSEOUT_STATES:
            # 与权威终态冲突或已换代：拒绝交付，只留诊断（绝不覆盖既有终态）。
            record_closeout_event(
                self.manager, task, event_type="closeout_blocked", params=params, outcome=outcome
            )
            if wal_state == "persisted":
                clear_closeout(self.manager, task)
            return result
        from ..debug_trace import SubAgentRunnerTraceRequest, trace_runner_result

        trace_runner_result(SubAgentRunnerTraceRequest(self.manager, task, result, params))
        delivery = deliver_parent_wake(
            self.manager, task, result, output_payload,
            attempt_id=str(getattr(params, "attempt_id", "") or ""),
        )
        if wal_state == "persisted":
            if delivery == "failed":
                # 收口已提交但通知未成：事实留档，恢复链只补通知，不重跑业务。
                ensure_closeout_fact(self.manager, task, params, result, outcome)
            else:
                # 「不重」要求先把"已交付"持久化再清账：清账失败时恢复链会看到 delivered 而跳过重发。
                mark_closeout_delivered(self.manager, task, params, result, outcome)
                clear_closeout(self.manager, task)
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
                # 终态冲突绝不能静默：真实事故里 run=created + attempt=done 的组合被这里拒掉，
                # 既不写 runner_result 也不写任何诊断，任务永久停在 RUNNING 且无人可查。
                self._record_conflict_diagnostic(task, params, conflict=conflict)
                return self._make_quick_result(task, dry_run, False, conflict)
        return None

    # LLM: 终态冲突是"账本已收口、任务投影未收口"的唯一信号源；必须落一条结构化事件
    # （closeout_blocked + reason=runner_result_conflict）才能被恢复链与排障看到。
    # 诊断写入本身 fail-soft：诊断失败不得改变拒绝语义，也不能吞掉原始冲突原因。
    # 函数用途: 记录一次被运行时终态闸拒绝的 runner 结果。
    def _record_conflict_diagnostic(
        self,
        task: SubAgentTask,
        params: RecordRunnerResultParams,
        *,
        conflict: str,
    ) -> None:
        repo = getattr(self, "runtime_db", None)
        append = getattr(repo, "append_event", None)
        if not callable(append):
            return
        try:
            authority = repo.runner_result_commit_authority(
                run_id=str(task.id or ""),
                attempt_id=str(params.attempt_id or ""),
            ) or {}
        except Exception:
            authority = {}
        try:
            append(
                event_type="closeout_blocked",
                attempt_id=str(params.attempt_id or ""),
                agent_run_id=str((authority or {}).get("agent_run_id") or ""),
                task_run_id=str((authority or {}).get("task_run_id") or ""),
                payload={
                    "schema_version": "subagent-closeout-conflict.v1",
                    "reason": "runner_result_conflict",
                    "conflict": str(conflict),
                    "run_status": str((authority or {}).get("run_status") or ""),
                    "attempt_status": str((authority or {}).get("attempt_status") or ""),
                    "incoming_status": str(getattr(params, "status", "") or ""),
                    "incoming_turn_end_reason": str(getattr(params, "turn_end_reason", "") or ""),
                    "task_status": str(_task_text_attr(task, "status") or ""),
                },
            )
        except Exception:
            return

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
    if run_status not in RUN_STATUS_LEGACY_CREATED and run_status not in AGENT_RUN_TERMINAL_STATUSES:
        # LLM: 未知/脏 run 状态**不是**"权威终态事实"，不能据此丢弃 runner 的最终结论。
        # 真机缺口：状态未知时这里直接判冲突，结论被静默丢掉——没有 runner_result、没有待重试
        # 事实、没有父级通知，子代理永久 RUNNING（现场：注入后 status_conflict + 挂住）。
        # 现在让结论照常落账，run 收口由 closeout 记为 unknown_status 待重试：不猜成功、不覆盖
        # 未知状态、不改写权威行；记录修复后再补收口与通知（恢复只补状态与通知）。
        return ""
    incoming_terminal = _runner_runtime_terminal_status(params)
    if _runner_result_matches_settled_attempt(run_status, attempt_status, params):
        return ""
    if run_status in AGENT_RUN_TERMINAL_STATUSES:
        # 权威 run 已终态：只接受**同一 exact current attempt 的一致终态**重入。
        # 这是"run 收口后、父级通知前中断"以及重复交付的幂等补写路径：
        # 收口可能只写了 run（settle_agent_run 不动已 ended 的 attempt），因此
        # run=failed + attempt=done + incoming=failed 是**一致**组合，必须放行；
        # 过期/换代在更上面已拒，冲突终态（run=done 收 FAILED 等）仍一律拒绝。
        if incoming_terminal and incoming_terminal == run_status:
            return ""
        if not incoming_terminal and run_status == "done" and attempt_status == "done":
            # 既有非终态投影放行规则保持不变，不引入新的拒绝面。
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
    """Decide whether one exact attempt's settled state still accepts this result.

    ``run=created + attempt=done`` 是宿主按"可续跑族"（如 MODEL_STREAM_INCOMPLETE）
    提前结清 attempt、把 run 留给 resume 的签名。此时该 attempt 已经没有后续产出，
    runner 对**同一个 attempt** 的终态回写是唯一事实来源：
      - turn_end 映射为 PENDING/BLOCKED（可续跑）→ 按原规则接受；
      - 映射为 FAILED/CANCELLED（本次事故形态）→ 也要接受并按该终态收口 run，
        否则任务永久停在 RUNNING，且 runner_result/唤醒全部写不出来。
    过期与换代保护不变：调用方只在 authority.is_current 为真时走到这里，
    stale/superseded/abandoned 的 attempt 仍在前面被拒。
    """

    if run_status not in {"", "created"} or attempt_status != "done":
        return False
    raw_status = str(params.status or "").strip().upper()
    expected_status, _failure_type, _ok = subagent_outcome_for_turn_end(
        params.turn_end_reason
    )
    if expected_status in {
        TaskStatus.PENDING.value,
        TaskStatus.BLOCKED.value,
    }:
        return raw_status == expected_status
    if expected_status in {
        TaskStatus.FAILED.value,
        TaskStatus.CANCELLED.value,
    }:
        return raw_status == expected_status
    return False
