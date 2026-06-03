from __future__ import annotations

"""Compatibility facade for subagent lifecycle service."""

from .services.lifecycle import (
    RecordCapabilityGapParams,
    RecordCapabilityGrantParams,
    RecordCapabilityRequestParams,
    RecordEvidenceParams,
    SetStatusParams,
    SubAgentLifecycleService,
)


class SubAgentLifecycleMixin:
    """Legacy mixin wrapper; new code should use SubAgentLifecycleService."""

    def _lifecycle_service(self):
        lifecycle = getattr(self, "lifecycle", None)
        if lifecycle is None:
            lifecycle = SubAgentLifecycleService(self)
            self.lifecycle = lifecycle
        return lifecycle

    def record_capability_request(self, run_id: str, params: RecordCapabilityRequestParams):
        return self._lifecycle_service().record_capability_request(run_id, params)

    def record_capability_grant(self, run_id: str, params: RecordCapabilityGrantParams):
        return self._lifecycle_service().record_capability_grant(run_id, params=params)

    def record_capability_gap(self, run_id: str, params: RecordCapabilityGapParams):
        return self._lifecycle_service().record_capability_gap(run_id, params=params)

    def record_evidence(self, run_id: str, params: RecordEvidenceParams):
        return self._lifecycle_service().record_evidence(run_id, params)

    def touch_heartbeat(self, run_id: str) -> None:
        self._lifecycle_service().touch_heartbeat(run_id)

    def set_status(
        self,
        params: str | SetStatusParams,
        status: str = "",
        *,
        result: str = "",
        failure_type: str = "",
        require_evidence: bool = False,
    ):
        return self._lifecycle_service().set_status(
            params,
            status,
            result=result,
            failure_type=failure_type,
            require_evidence=require_evidence,
        )

    def prepare_runner_attempt(self, run_id: str, *, retry_reason: str = ""):
        return self._lifecycle_service().prepare_runner_attempt(run_id, retry_reason=retry_reason)

    def abandon_runner_attempt(self, run_id: str, attempt_id: str, *, reason: str = ""):
        return self._lifecycle_service().abandon_runner_attempt(run_id, attempt_id, reason=reason)


__all__ = [
    "RecordCapabilityGapParams",
    "RecordCapabilityGrantParams",
    "RecordCapabilityRequestParams",
    "RecordEvidenceParams",
    "SetStatusParams",
    "SubAgentLifecycleMixin",
    "SubAgentLifecycleService",
]
