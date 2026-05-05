from __future__ import annotations

"""LLM: lifecycle mutation service for subagent task records.

给人看的解释：
这里承接能力请求、能力授权、能力缺口、验收证据和基础状态更新。
SubAgentManager 继续暴露旧方法名，内部逐步改成服务委托。
"""

import time
from dataclasses import dataclass
from typing import Any

from ...memory_routing import load_routes, match_routes, resolve_required_paths
from ..models import (
    CapabilityGap,
    CapabilityGrant,
    CapabilityRequest,
    SubAgentTask,
    VerificationEvidence,
)
from ..utils import _merge_list, _new_id


@dataclass(frozen=True)
class RecordCapabilityGrantParams:
    """Params bundle for record_capability_grant."""

    request_id: str
    skills: list[str] | None = None
    tools: list[str] | None = None
    capability_cards: list[dict[str, str]] | None = None
    reason: str = ""
    constraints: dict[str, str] | None = None
    expires_after_task: bool = True


@dataclass(frozen=True)
class RecordCapabilityGapParams:
    """Params bundle for record_capability_gap."""

    missing_capability: str
    why_failed: str
    attempted_skills: list[str] | None = None
    attempted_tools: list[str] | None = None
    needed_outputs: list[str] | None = None
    suggested_skill: str = ""
    suggested_tool: str = ""


@dataclass(frozen=True)
class RecordCapabilityRequestParams:
    """Params bundle for record_capability_request."""

    problem: str
    needed_capability: str
    expected_output: str = ""
    tried: list[str] | None = None
    evidence: list[str] | None = None
    constraints: dict[str, str] | None = None


@dataclass(frozen=True)
class RecordEvidenceParams:
    """Params bundle for record_evidence."""

    kind: str
    summary: str
    command: str = ""
    path: str = ""
    url: str = ""
    ok: bool = True


class SubAgentLifecycleService:
    """Mutate lifecycle fields on subagent tasks through the manager facade."""

    def __init__(self, manager: Any):
        self.manager = manager

    def record_capability_request(
        self,
        run_id: str,
        params: RecordCapabilityRequestParams,
    ) -> CapabilityRequest:
        task = self.manager.load(run_id)
        request = CapabilityRequest(
            id=_new_id("capreq"),
            from_run_id=run_id,
            problem=params.problem,
            needed_capability=params.needed_capability,
            expected_output=params.expected_output,
            tried=params.tried or [],
            evidence=params.evidence or [],
            constraints=params.constraints or {},
            created_at=time.time(),
        )
        task.capability_requests.append(request)
        task.updated_at = time.time()
        self.manager.save(task)
        return request

    def record_capability_grant(
        self,
        run_id: str,
        *,
        params: RecordCapabilityGrantParams,
    ) -> CapabilityGrant:
        task = self.manager.load(run_id)
        grant = CapabilityGrant(
            id=_new_id("capgrant"),
            request_id=params.request_id,
            grant_to_run_id=run_id,
            skills=params.skills or [],
            tools=params.tools or [],
            capability_cards=params.capability_cards or [],
            reason=params.reason,
            constraints=params.constraints or {},
            expires_after_task=params.expires_after_task,
            created_at=time.time(),
        )
        task.capability_grants.append(grant)
        task.allowed_skills = _merge_list(task.allowed_skills, grant.skills)
        task.allowed_tools = _merge_list(task.allowed_tools, grant.tools)
        task.updated_at = time.time()
        self.manager.save(task)
        return grant

    def record_capability_gap(
        self,
        run_id: str,
        *,
        params: RecordCapabilityGapParams,
    ) -> CapabilityGap:
        task = self.manager.load(run_id)
        injected_rule_paths, memory_routes = self._match_memory_routes(params.missing_capability, params.why_failed, task)
        gap = CapabilityGap(
            id=_new_id("capgap"),
            run_id=run_id,
            missing_capability=params.missing_capability,
            source_task=task.goal,
            why_failed=params.why_failed,
            attempted_skills=params.attempted_skills or [],
            attempted_tools=params.attempted_tools or [],
            needed_outputs=params.needed_outputs or [],
            suggested_skill=params.suggested_skill,
            suggested_tool=params.suggested_tool,
            memory_routes=memory_routes,
            injected_rule_paths=injected_rule_paths,
            created_at=time.time(),
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
        task.verification_status = "VERIFIED" if ok else "FAILED"
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
        run_id: str,
        status: str,
        *,
        result: str = "",
        failure_type: str = "",
        require_evidence: bool = False,
    ) -> SubAgentTask:
        task = self.manager.load(run_id)
        normalized = status.upper()
        if require_evidence and normalized == "DONE" and not task.evidence:
            raise ValueError("缺少验收证据，不能标记为 DONE。")
        task.status = normalized
        if result:
            task.result = result
        if failure_type:
            task.failure_type = failure_type
        if normalized in {"DONE", "FAILED", "BLOCKED", "CHANNEL_ERROR", "TIMEOUT"}:
            task.ended_at = time.time()
        task.updated_at = time.time()
        self.manager.save(task)
        return task

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
        except Exception:
            return [], []
