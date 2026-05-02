from __future__ import annotations

"""LLM contract: runner result recording and debrief persistence.

给人看的解释：
这个 mixin 只放一类 SubAgentManager 能力。它不单独实例化，
由 public SubAgentManager 组合使用，避免单个文件重新长成大杂烩。
"""

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from .models import *
from .reports import *
from .rendering import *
from .runner_rendering import *
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
from .result_processors import (
    _append_runner_debrief_content,
    _build_output_payload,
    _build_runner_result,
    _process_structured_output,
    _write_runner_result_files,
)
from ..capabilities import CapabilityRouter
from ..capability_config import CapabilityConfig
from ..file_io import append_jsonl

if TYPE_CHECKING:
    from ..local_store import LocalStore

class SubAgentRunnerResultMixin:
    def record_runner_result(
        self,
        run_id: str,
        *,
        attempt_id: str = "",
        dry_run: bool,
        ok: bool,
        message: str,
        prompt: str = "",
        response: str = "",
        backend: str = "",
        tool_rounds: int = 0,
        status: str = "",
        verification_status: str = "",
        failure_type: str = "",
        structured_output: SubAgentParsedOutput | None = None,
        actual_tools: list[str] | None = None,
        structured_repair_attempted: bool = False,
        structured_repair_ok: bool = False,
        structured_repair_error: str = "",
    ) -> SubAgentRunnerResult:
        """LLM: record a runner invocation result back into the standard work order.

        新手说明:
        把 runner 调用结果写回标准工单。包括解析结构化输出、更新任务状态、
        写 output.json 和 runner_result.json、追加 debrief 段落等。
        """

        task = self.load(run_id)
        normalized_attempt_id = str(attempt_id or "").strip()
        if normalized_attempt_id:
            if normalized_attempt_id in task.runner_abandoned_attempt_ids:
                return SubAgentRunnerResult(
                    run_id=task.id,
                    dry_run=dry_run,
                    ok=False,
                    status=task.status,
                    verification_status=task.verification_status,
                    message=f"ignored stale runner result for abandoned attempt {normalized_attempt_id}",
                    runner_attempts=task.runner_attempts,
                    runner_last_error=task.runner_last_error,
                    execution_context_json=task.execution_context_json,
                    execution_context_file=task.execution_context_file,
                    prompt_file=task.runner_prompt_file,
                    response_file=task.runner_response_file,
                    result_file=task.runner_result_file,
                    result_json=task.runner_result_json,
                    output_json=task.output_json,
                    created_at=time.time(),
                )
            active_attempt_id = str(task.runner_active_attempt_id or "").strip()
            if active_attempt_id and active_attempt_id != normalized_attempt_id:
                return SubAgentRunnerResult(
                    run_id=task.id,
                    dry_run=dry_run,
                    ok=False,
                    status=task.status,
                    verification_status=task.verification_status,
                    message=f"ignored stale runner result for non-active attempt {normalized_attempt_id}",
                    runner_attempts=task.runner_attempts,
                    runner_last_error=task.runner_last_error,
                    execution_context_json=task.execution_context_json,
                    execution_context_file=task.execution_context_file,
                    prompt_file=task.runner_prompt_file,
                    response_file=task.runner_response_file,
                    result_file=task.runner_result_file,
                    result_json=task.runner_result_json,
                    output_json=task.output_json,
                    created_at=time.time(),
                )
        _apply_missing_paths(task, self._build_work_order_paths(task.id, task.task_dir or None))
        self.save(task)
        now = time.time()
        parsed = structured_output or SubAgentParsedOutput()
        structured_evidence_count = 0
        structured_request_count = 0
        created_request_ids: list[str] = []
        ignored_tools: list[str] = []
        ignored_skills: list[str] = []
        artifacts: list[dict[str, object]] = []
        tests: list[dict[str, object]] = []
        patches: list[dict[str, object]] = []
        lessons: list[str] = []
        next_actions: list[str] = []

        if parsed.found and parsed.ok:
            proc = _process_structured_output(task, parsed, now, actual_tools)
            ignored_tools = proc["ignored_tools"]
            ignored_skills = proc["ignored_skills"]
            structured_evidence_count = proc["structured_evidence_count"]
            structured_request_count = proc["structured_request_count"]
            created_request_ids = proc["created_request_ids"]
            artifacts = proc["artifacts"]
            tests = proc["tests"]
            patches = proc["patches"]
            lessons = proc["lessons"]
            next_actions = proc["next_actions"]

            if parsed.summary:
                message = parsed.summary
            if parsed.blocked_reason:
                message = f"{message} / blocked: {parsed.blocked_reason}" if message else parsed.blocked_reason
            if not failure_type and parsed.failure_type:
                failure_type = parsed.failure_type
            if structured_request_count and not failure_type:
                failure_type = "capability_request"
            if not status:
                status = _status_from_structured_output(parsed)
            if not verification_status:
                verification_status = _verification_from_runner_status(status)
        elif parsed.found and not parsed.ok:
            ok = False
            status = status or "BLOCKED"
            verification_status = verification_status or "UNVERIFIED"
            failure_type = failure_type or "structured_output_parse_error"
            message = f"{message} / structured output parse failed: {parsed.parse_error}"

        if status:
            task.status = status.upper()
        if verification_status:
            task.verification_status = verification_status.upper()
        if failure_type:
            task.failure_type = failure_type
        elif not ok:
            task.failure_type = task.failure_type or "runner_error"
        if response:
            task.result = response
        elif message:
            task.result = message
        if task.status in {"DONE", "FAILED", "BLOCKED", "CHANNEL_ERROR", "TIMEOUT"}:
            task.ended_at = now
        task.updated_at = now
        task.heartbeat_at = now
        if not dry_run:
            task.runner_attempts = max(0, int(task.runner_attempts or 0)) + 1
            task.runner_last_attempt_at = now
            if not ok or task.status in {"BLOCKED", "FAILED", "CHANNEL_ERROR", "TIMEOUT"}:
                task.runner_last_error = message
            else:
                task.runner_last_error = ""
            if normalized_attempt_id and task.runner_active_attempt_id == normalized_attempt_id:
                task.runner_active_attempt_id = ""
        blockers = []
        if not ok or task.status in {"BLOCKED", "FAILED", "CHANNEL_ERROR", "TIMEOUT"}:
            blockers = [parsed.blocked_reason or message]

        output_payload = _build_output_payload(
            task,
            dry_run=dry_run,
            ok=ok,
            message=message,
            backend=backend,
            tool_rounds=tool_rounds,
            parsed=parsed,
            actual_tools=actual_tools,
            structured_evidence_count=structured_evidence_count,
            structured_request_count=structured_request_count,
            created_request_ids=created_request_ids,
            ignored_tools=ignored_tools,
            ignored_skills=ignored_skills,
            artifacts=artifacts,
            tests=tests,
            patches=patches,
            lessons=lessons,
            blockers=blockers,
            next_actions=next_actions,
            structured_repair_attempted=structured_repair_attempted,
            structured_repair_ok=structured_repair_ok,
            structured_repair_error=structured_repair_error,
            now=now,
        )

        result = _build_runner_result(
            task,
            dry_run=dry_run,
            ok=ok,
            message=message,
            backend=backend,
            tool_rounds=tool_rounds,
            prompt=prompt,
            response=response,
            parsed=parsed,
            structured_repair_attempted=structured_repair_attempted,
            structured_repair_ok=structured_repair_ok,
            structured_repair_error=structured_repair_error,
            structured_evidence_count=structured_evidence_count,
            structured_request_count=structured_request_count,
            artifact_count=len(artifacts),
            test_count=len(tests),
            patch_count=len(patches),
            lesson_count=len(lessons),
            now=now,
        )

        _write_runner_result_files(task, result, output_payload, prompt=prompt, response=response)
        Path(task.runner_result_file).write_text(
            render_runner_result_markdown(result),
            encoding="utf-8",
        )
        self.save(task)
        if parsed.found and parsed.ok:
            _append_runner_debrief_content(task, parsed)
        learning_candidates = []
        if not dry_run and parsed.found and parsed.ok and lessons:
            learning_candidates = self.record_learning_candidates(task, lessons)
        self._append_task_work_log(
            task,
            (
                f"subagent_runner: dry_run={dry_run} ok={ok} status={task.status} "
                f"message={message} learning_candidates={len(learning_candidates)}"
            ),
        )
        self._index_runner_result(result, output_payload)
        return result

    def _append_runner_debrief(
        self,
        task: SubAgentTask,
        parsed: SubAgentParsedOutput,
    ) -> None:
        """LLM: delegate to result_processors._append_runner_debrief_content.

        新手说明:
        把结构化 runner 产出追加到 DEBRIEF，方便人接管。
        """

        _append_runner_debrief_content(task, parsed)
