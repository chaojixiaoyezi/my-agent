from __future__ import annotations

"""LLM contract: runner result recording and debrief persistence.

给人看的解释：
这个 mixin 只放一类 SubAgentManager 能力。它不单独实例化，
由 public SubAgentManager 组合使用，避免单个文件重新长成大杂烩。
"""

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from ..capabilities import CapabilityRouter
from ..capability_config import CapabilityConfig
from ..file_io import append_jsonl
from .models import SubAgentParsedOutput, SubAgentRunnerResult, SubAgentTask
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
    _runner_next_action,
    _select_capability_hits,
    _severity_weight,
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
from .result_processors import (
    OutputPayloadContext,
    RunnerResultContext,
    _append_runner_debrief_content,
    _build_output_payload,
    _build_runner_result,
    _process_structured_output,
    _write_runner_result_files,
)
from .runner_rendering import _render_runner_item_line, render_runner_result_markdown
from .runner_result_state import apply_runner_result_fields
from .utils import (
    _apply_missing_paths,
    _apply_paths,
    _merge_list,
    _new_id,
    _read_json_object,
    _write_if_missing,
    _write_json_if_missing,
)

if TYPE_CHECKING:
    from ..local_store import LocalStore


@dataclass
class RecordRunnerResultParams:
    """Parameters for record_runner_result (18 fields replacing 18 positional params)."""
    run_id: str
    dry_run: bool
    ok: bool
    message: str
    attempt_id: str = ""
    prompt: str = ""
    response: str = ""
    backend: str = ""
    tool_rounds: int = 0
    status: str = ""
    verification_status: str = ""
    failure_type: str = ""
    structured_output: SubAgentParsedOutput | None = None
    actual_tools: list[str] | None = None
    structured_repair_attempted: bool = False
    structured_repair_ok: bool = False
    structured_repair_error: str = ""


class SubAgentRunnerResultMixin:
    def record_runner_result(
        self,
        params: RecordRunnerResultParams,
    ) -> SubAgentRunnerResult:
        """LLM: record a runner invocation result back into the standard work order."""
        run_id = params.run_id
        dry_run = params.dry_run
        ok = params.ok
        message = params.message
        attempt_id = params.attempt_id
        prompt = params.prompt
        response = params.response
        backend = params.backend
        tool_rounds = params.tool_rounds
        status = params.status
        verification_status = params.verification_status
        failure_type = params.failure_type
        structured_output = params.structured_output
        actual_tools = params.actual_tools
        structured_repair_attempted = params.structured_repair_attempted
        structured_repair_ok = params.structured_repair_ok
        structured_repair_error = params.structured_repair_error

        task = self.load(run_id)
        stale_result = self._check_stale_runner_result(task, attempt_id, dry_run)
        if stale_result:
            return stale_result

        _apply_missing_paths(task, self._build_work_order_paths(task.id, task.task_dir or None))
        self.save(task)
        now = time.time()

        parsed = structured_output or SubAgentParsedOutput()
        ignored_tools, ignored_skills, structured_evidence_count, structured_request_count, created_request_ids, artifacts, tests, patches, lessons, next_actions = self._process_parsed_output(task, parsed, now, actual_tools)
        blockers = self._compute_blockers(ok, task.status, parsed, message)
        result_meta = {"ok": ok, "message": message, "response": response, "dry_run": dry_run}
        runner_meta = {"dry_run": dry_run, "ok": ok, "message": message, "backend": backend, "tool_rounds": tool_rounds, "now": now}
        cap_data = {"parsed": parsed, "structured_evidence_count": structured_evidence_count, "structured_request_count": structured_request_count, "created_request_ids": created_request_ids, "structured_repair_attempted": structured_repair_attempted, "structured_repair_ok": structured_repair_ok, "structured_repair_error": structured_repair_error}
        tools_info = {"actual_tools": actual_tools, "ignored_tools": ignored_tools, "ignored_skills": ignored_skills}
        output_items = {"artifacts": artifacts, "tests": tests, "patches": patches, "lessons": lessons, "blockers": blockers, "next_actions": next_actions}
        status_context = {"status": status, "verification_status": verification_status, "failure_type": failure_type}
        apply_runner_result_fields(task, result_meta, status_context, parsed, now)
        output_payload = self._build_output_payload_wrapper(task, runner_meta, cap_data, tools_info, output_items)
        result = _build_runner_result(
            RunnerResultContext(
                task=task,
                dry_run=dry_run,
                ok=result_meta["ok"],
                message=result_meta["message"],
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
        )
        _write_runner_result_files(task, result, output_payload, prompt=prompt, response=response)
        Path(task.runner_result_file).write_text(render_runner_result_markdown(result), encoding="utf-8")
        self.save(task)
        if parsed.found and parsed.ok:
            _append_runner_debrief_content(task, parsed)
        learning_candidates = []
        if not dry_run and parsed.found and parsed.ok and lessons:
            learning_candidates = self.record_learning_candidates(task, lessons)
        self._append_task_work_log(task, f"subagent_runner: dry_run={dry_run} ok={result_meta['ok']} status={task.status} message={result_meta['message']} learning_candidates={len(learning_candidates)}")
        self._index_runner_result(result, output_payload)
        return result

    def _check_stale_runner_result(self, task, attempt_id, dry_run):
        normalized_attempt_id = str(attempt_id or "").strip()
        if normalized_attempt_id:
            if normalized_attempt_id in task.runner_abandoned_attempt_ids:
                return self._make_quick_result(task, dry_run, False, f"ignored stale runner result for abandoned attempt {normalized_attempt_id}")
            active_attempt_id = str(task.runner_active_attempt_id or "").strip()
            if active_attempt_id and active_attempt_id != normalized_attempt_id:
                return self._make_quick_result(task, dry_run, False, f"ignored stale runner result for non-active attempt {normalized_attempt_id}")
        return None

    def _make_quick_result(self, task, dry_run, ok, message):
        return SubAgentRunnerResult(
            run_id=task.id, dry_run=dry_run, ok=ok, status=task.status,
            verification_status=task.verification_status, message=message,
            runner_attempts=task.runner_attempts, runner_last_error=task.runner_last_error,
            execution_context_json=task.execution_context_json, execution_context_file=task.execution_context_file,
            prompt_file=task.runner_prompt_file, response_file=task.runner_response_file,
            result_file=task.runner_result_file, result_json=task.runner_result_json,
            output_json=task.output_json, created_at=time.time(),
        )

    def _process_parsed_output(self, task, parsed, now, actual_tools):
        if not (parsed.found and parsed.ok):
            return [], [], 0, 0, [], [], [], [], [], []
        proc = _process_structured_output(task, parsed, now, actual_tools)
        return (proc["ignored_tools"], proc["ignored_skills"], proc["structured_evidence_count"],
                proc["structured_request_count"], proc["created_request_ids"], proc["artifacts"],
                proc["tests"], proc["patches"], proc["lessons"], proc["next_actions"])

    def _compute_blockers(self, ok, status, parsed, message):
        if not ok or status in {"BLOCKED", "FAILED", "CHANNEL_ERROR", "TIMEOUT"}:
            return [parsed.blocked_reason or message]
        return []

    def _build_output_payload_wrapper(self, task, runner_meta, cap_data, tools_info, output_items):
        dry_run = runner_meta["dry_run"]
        ok = runner_meta["ok"]
        message = runner_meta["message"]
        backend = runner_meta["backend"]
        tool_rounds = runner_meta["tool_rounds"]
        now = runner_meta["now"]
        parsed = cap_data["parsed"]
        structured_evidence_count = cap_data["structured_evidence_count"]
        structured_request_count = cap_data["structured_request_count"]
        created_request_ids = cap_data["created_request_ids"]
        actual_tools = tools_info["actual_tools"]
        ignored_tools = tools_info["ignored_tools"]
        ignored_skills = tools_info["ignored_skills"]
        artifacts = output_items["artifacts"]
        tests = output_items["tests"]
        patches = output_items["patches"]
        lessons = output_items["lessons"]
        blockers = output_items["blockers"]
        next_actions = output_items["next_actions"]
        ctx = OutputPayloadContext(
            task=task,
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
            structured_repair_attempted=cap_data.get("structured_repair_attempted", False),
            structured_repair_ok=cap_data.get("structured_repair_ok", False),
            structured_repair_error=cap_data.get("structured_repair_error", ""),
            now=now,
        )
        return _build_output_payload(ctx)

    def _append_runner_debrief(self, task, parsed):
        _append_runner_debrief_content(task, parsed)
