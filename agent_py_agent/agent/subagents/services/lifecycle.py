
from __future__ import annotations

"""Lifecycle mutation service for subagent task records.

Manual status mutations pass through the current TaskStatus protocol instead
of accepting old success/failure aliases.
"""

import time
from dataclasses import dataclass
from typing import Any

from ...memory_routing import load_routes, match_routes, resolve_required_paths
from ...runtime_errors import runtime_error_report
from ..capability_request_identity import find_equivalent_capability_request
from ..models import (
    SUBAGENT_WAKE_STATUSES,
    CapabilityGap,
    CapabilityGrant,
    CapabilityRequest,
    SubAgentTask,
    VerificationEvidence,
    normalize_task_status,
    task_status_in,
)
from ..utils import _merge_list
from .lifecycle_capability_records import (
    BuildCapabilityGapInput,
    build_capability_gap,
    build_capability_grant,
    build_capability_request,
)
from .lifecycle_runner_attempts import (
    abandon_runner_attempt as abandon_runner_attempt_for_manager,
)
from .lifecycle_runner_attempts import (
    prepare_runner_attempt as prepare_runner_attempt_for_manager,
)


@dataclass(frozen=True)
class RecordCapabilityGrantParams:
    """Params bundle for record_capability_grant."""

    request_id: str
    grant_type: str = "generic"
    skills: list[str] | None = None
    tools: list[str] | None = None
    mcp_tools: list[str] | None = None
    command_allowlist: list[str] | None = None
    capability_cards: list[dict[str, str]] | None = None
    reason: str = ""
    constraints: dict[str, str] | None = None
    path_scope: list[str] | None = None
    network_scope: list[str] | None = None
    output_budget: dict[str, object] | None = None
    risk_level: str = ""
    request_scope: dict[str, object] | None = None
    expires_after_task: bool = True
    expires_at: float = 0.0


@dataclass(frozen=True)
class RecordCapabilityGapParams:
    """Params bundle for record_capability_gap."""

    missing_capability: str
    why_failed: str
    gap_type: str = "generic"
    attempted_skills: list[str] | None = None
    attempted_tools: list[str] | None = None
    needed_outputs: list[str] | None = None
    suggested_skill: str = ""
    suggested_tool: str = ""
    requested_scope: dict[str, object] | None = None
    constraints: dict[str, str] | None = None
    escalation_chain: list[str] | None = None
    next_record_refs: list[str] | None = None


@dataclass(frozen=True)
class RecordCapabilityRequestParams:
    """Params bundle for record_capability_request."""

    problem: str
    needed_capability: str
    expected_output: str = ""
    capability_type: str = "generic"
    tried: list[str] | None = None
    evidence: list[str] | None = None
    constraints: dict[str, str] | None = None
    requested_tools: list[str] | None = None
    requested_skills: list[str] | None = None
    requested_mcp_tools: list[str] | None = None
    requested_commands: list[str] | None = None
    cwd_scope: list[str] | None = None
    path_scope: list[str] | None = None
    network_scope: list[str] | None = None
    output_budget: dict[str, object] | None = None
    risk_level: str = ""
    alternatives_attempted: list[str] | None = None
    escalation_target: str = ""


@dataclass(frozen=True)
class RecordEvidenceParams:
    """Params bundle for record_evidence."""

    kind: str
    summary: str
    command: str = ""
    path: str = ""
    url: str = ""
    ok: bool = True


@dataclass(frozen=True)
class SetStatusParams:
    """Params bundle for set_status."""

    run_id: str
    status: str
    result: str = ""
    failure_type: str = ""
    require_evidence: bool = False


class SubAgentLifecycleService:
    """Mutate lifecycle fields on subagent tasks through SubAgentManager."""

    def __init__(self, manager: Any):
        self.manager = manager

    def record_capability_request(
        self,
        run_id: str,
        params: RecordCapabilityRequestParams,
    ) -> CapabilityRequest:
        task = self.manager.load(run_id)
        if existing := find_equivalent_capability_request(task.capability_requests, params):
            return existing
        request = build_capability_request(run_id, params)
        task.capability_requests.append(request)
        task.updated_at = time.time()
        self.manager.save(task)
        return request

    def record_capability_grant(
        self,
        run_id: str,
        params: RecordCapabilityGrantParams,
    ) -> CapabilityGrant:
        task = self.manager.load(run_id)
        grant = build_capability_grant(run_id, params)
        task.capability_grants.append(grant)
        task.allowed_skills = _merge_list(task.allowed_skills, grant.skills)
        task.allowed_tools = _merge_list(task.allowed_tools, grant.tools)
        task.updated_at = time.time()
        self.manager.save(task)
        return grant

    def record_capability_gap(
        self,
        run_id: str,
        params: RecordCapabilityGapParams,
    ) -> CapabilityGap:
        task = self.manager.load(run_id)
        injected_rule_paths, memory_routes = self._match_memory_routes(params.missing_capability, params.why_failed, task)
        gap = build_capability_gap(
            BuildCapabilityGapInput(run_id, task.goal, params, memory_routes, injected_rule_paths)
        )
        task.capability_gaps.append(gap)
        if injected_rule_paths:
            task.context_manifest.required_read_paths = _merge_list(
                task.context_manifest.required_read_paths,
                injected_rule_paths,
            )
        task.updated_at = time.time()
        self.manager.save(task)
        return gap

    def record_evidence(
        self,
        run_id: str,
        params: RecordEvidenceParams,
    ) -> VerificationEvidence:
        task = self.manager.load(run_id)
        evidence = VerificationEvidence(
            kind=params.kind,
            summary=params.summary,
            command=params.command,
            path=params.path,
            url=params.url,
            ok=params.ok,
            created_at=time.time(),
        )
        task.evidence.append(evidence)
        task.verification_status = "VERIFIED" if params.ok else "FAILED"
        task.updated_at = time.time()
        self.manager.save(task)
        return evidence

    def touch_heartbeat(self, run_id: str) -> None:
        task = self.manager.load(run_id)
        task.heartbeat_at = time.time()
        task.updated_at = task.heartbeat_at
        self.manager.save(task)

    def set_status(
        self,
        params: str | SetStatusParams,
        status: str = "",
        *,
        result: str = "",
        failure_type: str = "",
        require_evidence: bool = False,
    ) -> SubAgentTask:
        if isinstance(params, SetStatusParams):
            status_params = params
        else:
            status_params = SetStatusParams(
                run_id=params,
                status=status,
                result=result,
                failure_type=failure_type,
                require_evidence=require_evidence,
            )

        task = self.manager.load(status_params.run_id)
        normalized = normalize_task_status(status_params.status)
        if status_params.require_evidence and normalized == "DONE" and not task.evidence:
            raise ValueError("缺少验收证据，不能标记为 DONE。")
        task.status = normalized
        if status_params.result:
            task.result = status_params.result
        if status_params.failure_type:
            task.failure_type = status_params.failure_type
        if task_status_in(normalized, SUBAGENT_WAKE_STATUSES):
            task.ended_at = time.time()
        task.updated_at = time.time()
        self.manager.save(task)
        return task

    def prepare_runner_attempt(self, run_id: str, *, retry_reason: str = "") -> SubAgentTask:
        return prepare_runner_attempt_for_manager(self.manager, run_id, retry_reason=retry_reason)

    def abandon_runner_attempt(self, run_id: str, attempt_id: str, *, reason: str = "") -> SubAgentTask:
        return abandon_runner_attempt_for_manager(self.manager, run_id, attempt_id, reason=reason)

    def _match_memory_routes(
        self,
        missing_capability: str,
        why_failed: str,
        task: SubAgentTask,
    ) -> tuple[list[str], list[dict[str, str]]]:
        route_query = " ".join([missing_capability, why_failed, task.goal]).strip()
        if not route_query:
            return [], []
        try:
            index_path = (self.manager.workspace_root / "memory" / "routing" / "INDEX.md").resolve()
            if not index_path.exists():
                return [], []
            routes = load_routes(index_path)
            matches = match_routes(route_query, routes, limit=5)
            resolution = resolve_required_paths(matches, mode="strict", auto_read_limit=3)
            injected_rule_paths = list(dict.fromkeys([*resolution.required_read_paths, *resolution.candidate_paths]))
            memory_routes = [
                {
                    "route_id": match.route.route_id,
                    "source_file": match.route.authority_file(),
                    "inject_mode": match.route.inject_mode,
                }
                for match in matches
            ]
            return injected_rule_paths, memory_routes
        except Exception as exc:
            report = runtime_error_report(exc, context="capability_gap.memory_routes")
            return [], [_memory_route_load_error(report)]


def _memory_route_load_error(report: dict[str, object]) -> dict[str, str]:
    return {
        "route_id": "_memory_route_load_error",
        "source_file": "",
        "inject_mode": "diagnostic",
        "context": str(report.get("context") or "capability_gap.memory_routes"),
        "category": str(report.get("category") or ""),
        "error_type": str(report.get("error_type") or ""),
        "message": str(report.get("message") or ""),
    }
