"""LLM contract: SubAgentLifecycleMixin - thin facade delegating to lifecycle service.

Human version:
这个 mixin 是 SubAgentManager 的一块业务能力，不单独实例化。
内部已委托给 services/lifecycle.py 中的 SubAgentLifecycleService。
本文件只做薄包装，保持向后兼容。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .services.lifecycle import (
    RecordCapabilityGapParams,
    RecordCapabilityGrantParams,
    SubAgentLifecycleService,
)

if TYPE_CHECKING:
    from ..local_store import LocalStore


class SubAgentLifecycleMixin:
    """Thin facade for lifecycle operations delegating to SubAgentLifecycleService."""

    def _lifecycle_service(self):
        """Lazily get or create the lifecycle service."""

        lifecycle = getattr(self, "lifecycle", None)
        if lifecycle is None:
            lifecycle = SubAgentLifecycleService(self)
            self.lifecycle = lifecycle
        return lifecycle

    def record_capability_request(
        self,
        run_id: str,
        *,
        problem: str,
        needed_capability: str,
        expected_output: str = "",
        tried=None,
        evidence=None,
        constraints=None,
    ):
        """Record a capability request on a subagent task."""

        return self._lifecycle_service().record_capability_request(
            run_id,
            problem=problem,
            needed_capability=needed_capability,
            expected_output=expected_output,
            tried=tried,
            evidence=evidence,
            constraints=constraints,
        )

    def record_capability_grant(
        self,
        run_id: str,
        *,
        params: RecordCapabilityGrantParams | None = None,
        request_id: str | None = None,
        skills: list[str] | None = None,
        tools: list[str] | None = None,
        capability_cards: list[dict[str, str]] | None = None,
        reason: str = "",
        constraints: dict[str, str] | None = None,
        expires_after_task: bool = True,
    ):
        """Record a capability grant on a subagent task."""
        if params is None:
            params = RecordCapabilityGrantParams(
                request_id=request_id or "",
                skills=skills,
                tools=tools,
                capability_cards=capability_cards,
                reason=reason,
                constraints=constraints,
                expires_after_task=expires_after_task,
            )
        return self._lifecycle_service().record_capability_grant(run_id, params=params)

    def record_capability_gap(
        self,
        run_id: str,
        *,
        params: RecordCapabilityGapParams | None = None,
        missing_capability: str | None = None,
        why_failed: str | None = None,
        attempted_skills: list[str] | None = None,
        attempted_tools: list[str] | None = None,
        needed_outputs: list[str] | None = None,
        suggested_skill: str = "",
        suggested_tool: str = "",
    ):
        """Record a capability gap on a subagent task."""
        if params is None:
            params = RecordCapabilityGapParams(
                missing_capability=missing_capability or "",
                why_failed=why_failed or "",
                attempted_skills=attempted_skills,
                attempted_tools=attempted_tools,
                needed_outputs=needed_outputs,
                suggested_skill=suggested_skill,
                suggested_tool=suggested_tool,
            )
        return self._lifecycle_service().record_capability_gap(run_id, params=params)

    def record_evidence(
        self,
        run_id: str,
        *,
        kind: str,
        summary: str,
        command="",
        path="",
        url="",
        ok=True,
    ):
        """Record verification evidence on a subagent task."""

        return self._lifecycle_service().record_evidence(
            run_id,
            kind=kind,
            summary=summary,
            command=command,
            path=path,
            url=url,
            ok=ok,
        )

    def touch_heartbeat(self, run_id: str) -> None:
        """Refresh subagent heartbeat timestamp."""

        self._lifecycle_service().touch_heartbeat(run_id)

    def set_status(
        self,
        run_id: str,
        status: str,
        *,
        result: str = "",
        failure_type: str = "",
        require_evidence: bool = False,
    ):
        """Update task status with optional evidence requirement."""

        return self._lifecycle_service().set_status(
            run_id,
            status,
            result=result,
            failure_type=failure_type,
            require_evidence=require_evidence,
        )

    def prepare_runner_attempt(self, run_id: str, *, retry_reason: str = ""):
        """Prepare task for runner execution (set RUNNING, reset verification)."""

        from .models import SubAgentTask
        from .utils import _new_id

        task = self.load(run_id)
        previous = f"{task.status}/{task.failure_type or 'none'}"
        attempt_id = _new_id("attempt")
        task.status = "RUNNING"
        task.verification_status = "UNVERIFIED"
        task.failure_type = ""
        task.ended_at = 0.0
        task.runner_active_attempt_id = attempt_id
        task.updated_at = time.time()
        task.heartbeat_at = task.updated_at
        self.save(task)
        suffix = f" retry_reason={retry_reason}" if retry_reason else ""
        self._append_task_work_log(
            task,
            f"runner_attempt: start previous={previous} attempt={task.runner_attempts + 1} "
            f"attempt_id={attempt_id}{suffix}",
        )
        return task

    def abandon_runner_attempt(self, run_id: str, attempt_id: str, *, reason: str = ""):
        """Mark a runner attempt as abandoned."""

        task = self.load(run_id)
        normalized = str(attempt_id or "").strip()
        if not normalized:
            return task
        if normalized not in task.runner_abandoned_attempt_ids:
            task.runner_abandoned_attempt_ids.append(normalized)
        if task.runner_active_attempt_id == normalized:
            task.runner_active_attempt_id = ""
        task.updated_at = time.time()
        self.save(task)
        if reason:
            self._append_task_work_log(
                task,
                f"runner_attempt: abandon attempt_id={normalized} reason={reason}",
            )
        return task
