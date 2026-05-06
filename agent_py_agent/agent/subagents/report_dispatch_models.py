from __future__ import annotations

"""Dispatch report models."""

from dataclasses import dataclass, field


@dataclass
class DispatchRecord:
    """鐖朵唬鐞嗚皟搴﹀櫒鐨勪竴姝ュ璁¤褰曘€?"""

    id: str
    step: str
    action: str
    run_id: str
    dry_run: bool
    applied: bool
    ok: bool
    message: str
    before_status: str = ""
    after_status: str = ""
    before_verification_status: str = ""
    after_verification_status: str = ""
    evidence_paths: list[str] = field(default_factory=list)
    created_at: float = 0.0


@dataclass
class DispatchReport:
    """鐖朵唬鐞嗚皟搴﹀櫒鎶ュ憡銆?"""

    generated_at: float
    dry_run: bool
    summary: dict[str, int]
    records: list[DispatchRecord]


@dataclass
class DispatchWatchRecord:
    """鐖朵唬鐞?watch 妯″紡鐨勪竴杞惊鐜褰曘€?"""

    id: str
    cycle: int
    dry_run: bool
    ok: bool
    message: str
    dispatch_record_count: int
    dispatch_summary: dict[str, int] = field(default_factory=dict)
    started_at: float = 0.0
    ended_at: float = 0.0
    evidence_paths: list[str] = field(default_factory=list)


@dataclass
class DispatchWatchReport:
    """鐖朵唬鐞?watch 妯″紡鎶ュ憡銆?"""

    generated_at: float
    dry_run: bool
    summary: dict[str, int]
    records: list[DispatchWatchRecord]
