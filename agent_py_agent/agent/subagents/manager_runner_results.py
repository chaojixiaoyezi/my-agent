from __future__ import annotations

"""LLM contract: runner result recording and debrief persistence.

Human version:
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
        """把 runner 调用结果写回标准工单。"""

        task = self.load(run_id)
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

        if prompt:
            Path(task.runner_prompt_file).write_text(prompt, encoding="utf-8")
        if response:
            Path(task.runner_response_file).write_text(response, encoding="utf-8")

        if parsed.found and parsed.ok:
            allowed_tools = set(task.allowed_tools)
            allowed_skills = set(task.allowed_skills)
            used_tools, ignored_tools = _split_allowed_items(parsed.used_tools, allowed_tools)
            used_skills, ignored_skills = _split_allowed_items(parsed.used_skills, allowed_skills)
            if actual_tools is not None:
                actual_allowed_tools = [item for item in actual_tools if item in allowed_tools]
                task.used_tools = _merge_list(task.used_tools, actual_allowed_tools)
                ignored_tools = _merge_list(
                    ignored_tools,
                    [item for item in used_tools if item not in actual_allowed_tools],
                )
                for tool_name in actual_allowed_tools:
                    if not any(
                        item.ok
                        and (
                            item.kind == tool_name
                            or item.command == tool_name
                            or item.command.startswith(f"{tool_name} ")
                        )
                        for item in task.evidence
                    ):
                        task.evidence.append(
                            VerificationEvidence(
                                kind=tool_name,
                                summary=f"系统记录 runner 实际执行过 {tool_name}。",
                                command=tool_name,
                                ok=True,
                                created_at=now,
                            )
                        )
            else:
                task.used_tools = _merge_list(task.used_tools, used_tools)
            task.used_skills = _merge_list(task.used_skills, used_skills)

            for item in parsed.evidence:
                summary = str(item.get("summary", "")).strip()
                if not summary:
                    continue
                task.evidence.append(
                    VerificationEvidence(
                        kind=str(item.get("kind", "note") or "note"),
                        summary=summary,
                        command=str(item.get("command", "") or ""),
                        path=str(item.get("path", "") or ""),
                        url=str(item.get("url", "") or ""),
                        ok=bool(item.get("ok", True)),
                        created_at=now,
                    )
                )
                structured_evidence_count += 1

            for item in parsed.capability_requests:
                problem = str(item.get("problem", "")).strip()
                needed = str(item.get("needed_capability", "")).strip()
                if not problem or not needed:
                    continue
                request = CapabilityRequest(
                    id=_new_id("capreq"),
                    from_run_id=task.id,
                    problem=problem,
                    needed_capability=needed,
                    expected_output=str(item.get("expected_output", "") or ""),
                    tried=_string_list(item.get("tried", [])),
                    evidence=_string_list(item.get("evidence", [])),
                    constraints=_string_dict(item.get("constraints", {})),
                    created_at=now,
                )
                task.capability_requests.append(request)
                created_request_ids.append(request.id)
                structured_request_count += 1

            if parsed.summary:
                message = parsed.summary
            if parsed.blocked_reason:
                message = f"{message} / blocked: {parsed.blocked_reason}" if message else parsed.blocked_reason
            artifacts = _normalize_runner_items(parsed.artifacts)
            tests = _normalize_runner_items(parsed.tests)
            patches = _normalize_runner_items(parsed.patches)
            lessons = parsed.lessons
            next_actions = parsed.next_actions
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
        blockers = []
        if not ok or task.status in {"BLOCKED", "FAILED", "CHANNEL_ERROR", "TIMEOUT"}:
            blockers = [parsed.blocked_reason or message]

        output_payload = {
            "run_id": task.id,
            "dry_run": dry_run,
            "ok": ok,
            "status": task.status,
            "verification_status": task.verification_status,
            "message": message,
            "backend": backend,
            "tool_rounds": tool_rounds,
            "runner_attempts": task.runner_attempts,
            "runner_last_error": task.runner_last_error,
            "response": response,
            "structured_output": {
                "found": parsed.found,
                "ok": parsed.ok,
                "parse_error": parsed.parse_error,
                "repair_attempted": structured_repair_attempted,
                "repair_ok": structured_repair_ok,
                "repair_error": structured_repair_error,
                "summary": parsed.summary,
                "status": parsed.status,
                "blocked_reason": parsed.blocked_reason,
                "evidence_count": structured_evidence_count,
                "capability_request_count": structured_request_count,
                "capability_request_ids": created_request_ids,
                "artifact_count": len(artifacts),
                "test_count": len(tests),
                "patch_count": len(patches),
                "lesson_count": len(lessons),
                "actual_tools": actual_tools or [],
                "ignored_unauthorized_tools": ignored_tools,
                "ignored_unauthorized_skills": ignored_skills,
            },
            "used_tools": task.used_tools,
            "used_skills": task.used_skills,
            "artifacts": artifacts,
            "tests": tests,
            "patches": patches,
            "acceptance": [item.summary for item in task.evidence],
            "lessons": lessons,
            "blockers": blockers,
            "next_action": _runner_next_action(
                dry_run=dry_run,
                ok=ok,
                status=task.status,
                capability_request_count=structured_request_count,
                next_actions=next_actions,
            ),
            "next_actions": next_actions,
            "created_at": now,
        }
        Path(task.output_json).write_text(
            json.dumps(output_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        result = SubAgentRunnerResult(
            run_id=task.id,
            dry_run=dry_run,
            ok=ok,
            status=task.status,
            verification_status=task.verification_status,
            message=message,
            backend=backend,
            tool_rounds=tool_rounds,
            runner_attempts=task.runner_attempts,
            runner_last_error=task.runner_last_error,
            execution_context_json=task.execution_context_json,
            execution_context_file=task.execution_context_file,
            prompt_file=task.runner_prompt_file if prompt else "",
            response_file=task.runner_response_file if response else "",
            result_file=task.runner_result_file,
            result_json=task.runner_result_json,
            output_json=task.output_json,
            structured_output_found=parsed.found,
            structured_output_ok=parsed.ok,
            structured_parse_error=parsed.parse_error,
            structured_repair_attempted=structured_repair_attempted,
            structured_repair_ok=structured_repair_ok,
            structured_repair_error=structured_repair_error,
            structured_summary=parsed.summary,
            evidence_count=structured_evidence_count,
            capability_request_count=structured_request_count,
            artifact_count=len(artifacts),
            test_count=len(tests),
            patch_count=len(patches),
            lesson_count=len(lessons),
            blocked_reason=parsed.blocked_reason,
            created_at=now,
        )
        Path(task.runner_result_json).write_text(
            json.dumps(asdict(result), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        Path(task.runner_result_file).write_text(
            render_runner_result_markdown(result),
            encoding="utf-8",
        )
        self.save(task)
        if parsed.found and parsed.ok:
            self._append_runner_debrief(task, parsed)
        self._append_task_work_log(
            task,
            f"subagent_runner: dry_run={dry_run} ok={ok} status={task.status} message={message}",
        )
        self._index_runner_result(result, output_payload)
        return result

    def _append_runner_debrief(
        self,
        task: SubAgentTask,
        parsed: SubAgentParsedOutput,
    ) -> None:
        """把结构化 runner 产出追加到 DEBRIEF，方便人接管。"""

        sections: list[str] = []
        if parsed.artifacts:
            sections.append("## Runner Artifacts")
            sections.extend(_render_runner_item_line(item) for item in parsed.artifacts)
        if parsed.tests:
            sections.append("## Runner Tests")
            sections.extend(_render_runner_item_line(item) for item in parsed.tests)
        if parsed.patches:
            sections.append("## Runner Patches")
            sections.extend(_render_runner_item_line(item) for item in parsed.patches)
        if parsed.lessons:
            sections.append("## Runner Lessons")
            sections.extend(f"- {item}" for item in parsed.lessons)
        if parsed.next_actions:
            sections.append("## Runner Next Actions")
            sections.extend(f"- {item}" for item in parsed.next_actions)
        if not sections:
            return

        path = Path(task.debrief_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text("# DEBRIEF\n\n", encoding="utf-8")
        with path.open("a", encoding="utf-8") as handle:
            handle.write("\n## Runner Structured Output\n\n")
            handle.write(f"- created_at: {time.time()}\n")
            handle.write(f"- run_id: {task.id}\n\n")
            handle.write("\n\n".join(sections))
            handle.write("\n")

