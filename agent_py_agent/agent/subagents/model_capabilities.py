
from __future__ import annotations

"""Capability and evidence dataclasses used by subagent runs."""

from dataclasses import dataclass, field

CAPABILITY_REQUEST_OPEN_STATUS = "OPEN"
CAPABILITY_REQUEST_TERMINAL_STATUSES = frozenset({"GRANTED", "GAP", "CLOSED"})
CAPABILITY_REQUEST_CURRENT_STATUSES = frozenset({
    CAPABILITY_REQUEST_OPEN_STATUS,
    *CAPABILITY_REQUEST_TERMINAL_STATUSES,
})


def normalize_capability_request_status(status: object) -> str:
    normalized = str(status or CAPABILITY_REQUEST_OPEN_STATUS).strip().upper()
    return normalized or CAPABILITY_REQUEST_OPEN_STATUS


def capability_request_counts_as_open(status: object) -> bool:
    normalized = normalize_capability_request_status(status)
    return normalized not in CAPABILITY_REQUEST_TERMINAL_STATUSES


def capability_request_suppresses_duplicate(status: object) -> bool:
    normalized = normalize_capability_request_status(status)
    return normalized == "GRANTED" or capability_request_counts_as_open(normalized)


@dataclass
class CapabilityRequest:
    """Request from a subagent for a missing capability."""

    id: str
    from_run_id: str
    problem: str
    needed_capability: str
    expected_output: str = ""
    capability_type: str = "generic"
    tried: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    constraints: dict[str, str] = field(default_factory=dict)
    requested_tools: list[str] = field(default_factory=list)
    requested_skills: list[str] = field(default_factory=list)
    requested_mcp_tools: list[str] = field(default_factory=list)
    requested_commands: list[str] = field(default_factory=list)
    cwd_scope: list[str] = field(default_factory=list)
    path_scope: list[str] = field(default_factory=list)
    network_scope: list[str] = field(default_factory=list)
    output_budget: dict[str, object] = field(default_factory=dict)
    risk_level: str = ""
    alternatives_attempted: list[str] = field(default_factory=list)
    escalation_target: str = ""
    status: str = "OPEN"
    created_at: float = 0.0


@dataclass
class CapabilityGrant:
    """Capability granted by a parent/supervisor to a subagent."""

    id: str
    request_id: str
    grant_to_run_id: str
    grant_type: str = "generic"
    skills: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    mcp_tools: list[str] = field(default_factory=list)
    command_allowlist: list[str] = field(default_factory=list)
    capability_cards: list[dict[str, str]] = field(default_factory=list)
    reason: str = ""
    constraints: dict[str, str] = field(default_factory=dict)
    path_scope: list[str] = field(default_factory=list)
    network_scope: list[str] = field(default_factory=list)
    output_budget: dict[str, object] = field(default_factory=dict)
    risk_level: str = ""
    request_scope: dict[str, object] = field(default_factory=dict)
    expires_after_task: bool = True
    expires_at: float = 0.0
    created_at: float = 0.0


@dataclass
class CapabilityGap:
    """A capability gap left after routing/search could not find a match."""

    id: str
    run_id: str
    missing_capability: str
    source_task: str
    why_failed: str
    gap_type: str = "generic"
    attempted_skills: list[str] = field(default_factory=list)
    attempted_tools: list[str] = field(default_factory=list)
    needed_outputs: list[str] = field(default_factory=list)
    suggested_skill: str = ""
    suggested_tool: str = ""
    requested_scope: dict[str, object] = field(default_factory=dict)
    constraints: dict[str, str] = field(default_factory=dict)
    escalation_chain: list[str] = field(default_factory=list)
    next_record_refs: list[str] = field(default_factory=list)
    memory_routes: list[dict[str, str]] = field(default_factory=list)
    injected_rule_paths: list[str] = field(default_factory=list)
    status: str = "OPEN"
    created_at: float = 0.0


@dataclass
class VerificationEvidence:
    """Evidence produced by a subagent for verification/acceptance."""

    kind: str
    summary: str
    command: str = ""
    path: str = ""
    url: str = ""
    ok: bool = True
    created_at: float = 0.0
