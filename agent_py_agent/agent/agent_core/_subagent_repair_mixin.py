
from __future__ import annotations

from dataclasses import dataclass

from ..memory_archive import write_recovery_snapshot
from ..memory_archive.snapshots import RecoverySnapshotInput
from ..subagent import parse_subagent_runner_output
from .runner_prompts import (
    _append_runner_repair_failure,
    _append_runner_repair_prompt,
    _append_runner_repair_response,
    _build_subagent_runner_repair_prompt,
)


@dataclass(frozen=True)
class SubagentRepairParams:
    context: object
    result: object
    structured: object
    prompt_for_log: str
    response_for_log: str
    backend_name: str
    message: str


class _SubagentRepairMixin:

    def _handle_subagent_repair(self, params: SubagentRepairParams):
        structured_repair_ok = False
        structured_repair_error = ""

        repair_prompt = _build_subagent_runner_repair_prompt(
            params.context,
            original_prompt=params.result.prompt,
            original_response=params.result.response,
            parse_error=params.structured.parse_error,
        )
        try:
            repair_response = self.backend.generate(repair_prompt)
        except Exception as exc:
            structured_repair_error = str(exc)
            response_for_log = _append_runner_repair_failure(params.result.response, exc)
            return (
                params.structured,
                structured_repair_ok,
                structured_repair_error,
                params.backend_name,
                params.prompt_for_log,
                response_for_log,
                params.message,
            )

        repaired = parse_subagent_runner_output(repair_response.text)
        prompt_for_log = _append_runner_repair_prompt(params.result.prompt, repair_prompt)
        response_for_log = _append_runner_repair_response(params.result.response, repair_response.text)
        backend_name = repair_response.backend or params.result.backend
        structured = params.structured
        message = params.message

        if repaired.found and repaired.ok:
            structured = repaired
            structured_repair_ok = True
            message = "runner 已完成模型调用，并已修复结构化结果，等待独立验收。"
        elif not structured.found and repaired.found:
            structured = repaired
            structured_repair_error = repaired.parse_error
        else:
            structured_repair_error = (
                repaired.parse_error or "repair response still missing structured output"
            )

        return (
            structured,
            structured_repair_ok,
            structured_repair_error,
            backend_name,
            prompt_for_log,
            response_for_log,
            message,
        )

    def _write_subagent_recovery_snapshot(
        self,
        run_id: str,
        **kwargs,
    ) -> None:

        if not bool(getattr(self.config, "memory_hook_enabled", True)):
            return
        try:
            task = self.subagents.load(run_id)
        except (FileNotFoundError, TypeError):
            task = None
        content_paths = []
        next_actions = [
            "先读取子代理 STATUS/WORK_LOG/RUNNER_RESULT/output.json，再判断是否可以验收或重跑。"
        ]
        if task is not None:
            content_paths = [
                task.status_file,
                task.work_log_file,
                task.runner_result_file,
                task.runner_result_json,
                task.output_json,
                task.handoff_file,
            ]
        write_recovery_snapshot(
            self.root,
            params=RecoverySnapshotInput(
                session_id=getattr(self, "session_id", self.config.agent_name),
                user_prompt=kwargs["user_prompt"],
                response_text=kwargs["response_text"],
                backend=kwargs["backend"],
                source="subagent_run",
                request_id=f"subagent-run:{run_id}",
                run_id=run_id,
                task_id=run_id,
                status=str(kwargs["status"]).lower() or "unknown",
                error_code=kwargs["error_code"],
                tool_calls=kwargs["tool_calls"],
                task_refs=[run_id],
                content_paths=content_paths,
                next_actions=next_actions,
                archive_level=int(getattr(self.config, "memory_hook_archive_level", 3)),
            ),
        )
