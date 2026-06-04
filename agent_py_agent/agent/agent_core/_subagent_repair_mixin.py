

from __future__ import annotations

from dataclasses import dataclass

from ..memory_archive import write_recovery_snapshot
from ..memory_archive.snapshots import RecoverySnapshotInput
from ..subagents import parse_subagent_runner_output
from .provider_transient_auto_resume import run_with_provider_transient_auto_resume
from .runner.prompts import (
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


@dataclass(frozen=True)
class RecoverySnapshotParams:
    run_id: str
    user_prompt: str
    response_text: str
    backend: str
    status: str
    error_code: str
    tool_calls: list[dict[str, object]]


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
            repair_response = run_with_provider_transient_auto_resume(
                lambda: self.backend.generate(repair_prompt),
                policy=getattr(self, "runtime_guard_policy", None),
            )
        except Exception as exc:
            return _repair_failure_tuple(params, exc)

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
        run_id: str | None = None,
        *,
        params: RecoverySnapshotParams | None = None,
        user_prompt: str = "",
        response_text: str = "",
        backend: str = "",
        status: str = "",
        error_code: str = "",
        tool_calls: list[dict] | None = None,
    ) -> None:

        if not bool(getattr(self.config, "memory_hook_enabled", True)):
            return
        snapshot = _recovery_snapshot_params(
            params,
            run_id=run_id,
            user_prompt=user_prompt,
            response_text=response_text,
            backend=backend,
            status=status,
            error_code=error_code,
            tool_calls=tool_calls,
        )
        try:
            task = self.subagents.load(snapshot.run_id)
        except (FileNotFoundError, TypeError):
            task = None
        write_recovery_snapshot(
            self.root,
            params=_recovery_snapshot_input(self, snapshot, _recovery_content_paths(task)),
        )


def _repair_failure_tuple(params: SubagentRepairParams, exc: Exception) -> tuple:
    return (
        params.structured,
        False,
        str(exc),
        params.backend_name,
        params.prompt_for_log,
        _append_runner_repair_failure(params.result.response, exc),
        params.message,
    )


def _recovery_snapshot_params(
    params: RecoverySnapshotParams | None,
    *,
    run_id: str | None,
    user_prompt: str,
    response_text: str,
    backend: str,
    status: str,
    error_code: str,
    tool_calls: list[dict] | None,
) -> RecoverySnapshotParams:
    if params is not None:
        if not isinstance(params, RecoverySnapshotParams):
            raise TypeError("subagent recovery snapshot requires params: RecoverySnapshotParams")
        return params
    return RecoverySnapshotParams(
        run_id=str(run_id or ""),
        user_prompt=str(user_prompt),
        response_text=str(response_text),
        backend=str(backend),
        status=str(status),
        error_code=str(error_code),
        tool_calls=list(tool_calls or []),
    )


def _recovery_content_paths(task) -> list[str]:
    if task is None:
        return []
    return [
        task.status_file,
        task.work_log_file,
        task.runner_result_file,
        task.runner_result_json,
        task.output_json,
        task.handoff_file,
    ]


def _recovery_snapshot_input(agent, snapshot: RecoverySnapshotParams, content_paths: list[str]):
    next_actions = [
        "先读取子代理 STATUS/WORK_LOG/RUNNER_RESULT/output.json，再判断是否可以验收或重跑。"
    ]
    return RecoverySnapshotInput(
        session_id=getattr(agent, "session_id", agent.config.agent_name),
        user_prompt=snapshot.user_prompt,
        response_text=snapshot.response_text,
        backend=snapshot.backend,
        source="subagent_run",
        request_id=f"subagent-run:{snapshot.run_id}",
        run_id=snapshot.run_id,
        task_id=snapshot.run_id,
        status=str(snapshot.status).lower() or "unknown",
        error_code=snapshot.error_code,
        tool_calls=snapshot.tool_calls,
        task_refs=[snapshot.run_id],
        content_paths=content_paths,
        next_actions=next_actions,
        archive_level=int(getattr(agent.config, "memory_hook_archive_level", 3)),
    )
