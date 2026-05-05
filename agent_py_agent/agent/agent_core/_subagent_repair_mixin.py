"""_SubagentRepairMixin: structured output repair and recovery snapshot writing."""

from __future__ import annotations

from ..memory_archive import write_recovery_snapshot
from ..memory_archive.snapshots import RecoverySnapshotInput
from ..subagent import parse_subagent_runner_output


class _SubagentRepairMixin:
    """Internal: structured output repair and recovery snapshot writing."""

    def _handle_subagent_repair(
        self,
        context,
        result,
        structured,
        prompt_for_log,
        response_for_log,
        backend_name,
        message,
    ):
        """Handle structured output repair when initial parse fails."""
        from .runner_prompts import (
            _append_runner_repair_failure,
            _append_runner_repair_prompt,
            _append_runner_repair_response,
            _build_subagent_runner_repair_prompt,
        )

        structured_repair_attempted = True
        structured_repair_ok = False
        structured_repair_error = ""

        repair_prompt = _build_subagent_runner_repair_prompt(
            context,
            original_prompt=result.prompt,
            original_response=result.response,
            parse_error=structured.parse_error,
        )
        try:
            repair_response = self.backend.generate(repair_prompt)
        except Exception as exc:
            structured_repair_error = str(exc)
            response_for_log = _append_runner_repair_failure(result.response, exc)
            return (
                structured,
                structured_repair_ok,
                structured_repair_error,
                backend_name,
                prompt_for_log,
                response_for_log,
                message,
            )

        repaired = parse_subagent_runner_output(repair_response.text)
        prompt_for_log = _append_runner_repair_prompt(result.prompt, repair_prompt)
        response_for_log = _append_runner_repair_response(result.response, repair_response.text)
        backend_name = repair_response.backend or result.backend

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
        *,
        user_prompt: str,
        response_text: str,
        backend: str,
        status: str,
        error_code: str,
        tool_calls: list[dict[str, object]],
    ) -> None:
        """LLM: write a best-effort run_id recovery snapshot after subagent-run finishes.

        给人看的解释：
        子代理 runner 用 `save=False`，避免把大 prompt 写进普通对话记忆。
        但任务恢复需要一个小锚点，所以 runner 结果写回后单独写 hook，并附上任务目录里的权威文件路径。
        """

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
                user_prompt=user_prompt,
                response_text=response_text,
                backend=backend,
                source="subagent_run",
                request_id=f"subagent-run:{run_id}",
                run_id=run_id,
                task_id=run_id,
                status=status.lower() or "unknown",
                error_code=error_code,
                tool_calls=tool_calls,
                task_refs=[run_id],
                content_paths=content_paths,
                next_actions=next_actions,
                archive_level=int(getattr(self.config, "memory_hook_archive_level", 3)),
            ),
        )