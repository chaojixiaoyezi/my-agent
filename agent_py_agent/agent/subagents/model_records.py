from __future__ import annotations

"""Work-order, takeover, and channel-probe dataclasses."""

from dataclasses import dataclass, field


@dataclass
class WorkOrderValidation:
    """Validation result for a subagent work-order directory."""

    run_id: str
    ok: bool
    missing: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class TakeoverRecord:
    """Record of a parent/supervisor taking over a subagent task."""

    id: str
    run_id: str
    take_over_by: str
    reason: str
    locked_files: list[str] = field(default_factory=list)
    previous_owner: str = ""
    created_at: float = 0.0


@dataclass
class ChannelProbeCheck:
    """One health check item in a channel probe."""

    name: str
    ok: bool
    summary: str
    severity: str = "P1"
    evidence_path: str = ""
    error: str = ""
    created_at: float = 0.0


@dataclass
class ChannelProbeResult:
    """Channel probe result for one subagent run."""

    run_id: str
    channel_status: str
    checks: list[ChannelProbeCheck]
    task_dir: str = ""
    goal: str = ""
    created_at: float = 0.0


@dataclass
class ChannelProbeReport:
    """Batch channel probe report."""

    generated_at: float
    summary: dict[str, int]
    results: list[ChannelProbeResult]
