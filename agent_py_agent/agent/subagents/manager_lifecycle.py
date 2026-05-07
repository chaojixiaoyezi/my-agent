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
    RecordCapabilityRequestParams,
    RecordEvidenceParams,
    SetStatusParams,
    SubAgentLifecycleService,
)

# LLM: lifecycle facade accepts bundle params while preserving legacy manager entrypoints.
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
        params: RecordCapabilityRequestParams,
    ):
        """Record a capability request on a subagent task."""

        return self._lifecycle_service().record_capability_request(run_id, params)

    def record_capability_grant(
        self,
        run_id: str,
        params: RecordCapabilityGrantParams,
    ):
        """Record a capability grant on a subagent task."""
        return self._lifecycle_service().record_capability_grant(run_id, params=params)

    def record_capability_gap(
        self,
        run_id: str,
        params: RecordCapabilityGapParams,
    ):
        """Record a capability gap on a subagent task."""
        return self._lifecycle_service().record_capability_gap(run_id, params=params)

    def record_evidence(
        self,
        run_id: str,
        params: RecordEvidenceParams,
    ):
        """Record verification evidence on a subagent task."""

        return self._lifecycle_service().record_evidence(run_id, params)

    def touch_heartbeat(self, run_id: str) -> None:
        """Refresh subagent heartbeat timestamp."""

        self._lifecycle_service().touch_heartbeat(run_id)

    def set_status(
        self,
        run_id: str | SetStatusParams,
        status: str = "",
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
