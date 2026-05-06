from __future__ import annotations

"""Capability and evidence dataclasses used by subagent runs."""

from dataclasses import dataclass, field


@dataclass
class CapabilityRequest:
    """Request from a subagent for a missing capability."""

    id: str
    from_run_id: str
    problem: str
    needed_capability: str
    expected_output: str = ""
    tried: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    constraints: dict[str, str] = field(default_factory=dict)
    status: str = "OPEN"
    created_at: float = 0.0


@dataclass
class CapabilityGrant:
    """Capability granted by a parent/supervisor to a subagent."""

    id: str
    request_id: str
    grant_to_run_id: str
    skills: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    capability_cards: list[dict[str, str]] = field(default_factory=list)
    reason: str = ""
    constraints: dict[str, str] = field(default_factory=dict)
    expires_after_task: bool = True
    created_at: float = 0.0


@dataclass
class CapabilityGap:
    """A capability gap left after routing/search could not find a match."""

    id: str
    run_id: str
    missing_capability: str
    source_task: str
    why_failed: str
    attempted_skills: list[str] = field(default_factory=list)
    attempted_tools: list[str] = field(default_factory=list)
    needed_outputs: list[str] = field(default_factory=list)
    suggested_skill: str = ""
    suggested_tool: str = ""
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
