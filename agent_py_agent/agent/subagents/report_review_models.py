from __future__ import annotations

"""Acceptance and patch review report models."""

from dataclasses import dataclass, field


@dataclass
class AcceptanceReviewFinding:
    """涓€娆￠獙鏀舵鏌ヤ腑鐨勫崟椤圭粨璁恒€?"""

    name: str
    ok: bool
    severity: str
    message: str
    evidence_path: str = ""
    created_at: float = 0.0


@dataclass
class AcceptanceReviewRecord:
    """鍗曚釜瀛愪唬鐞嗚繍琛岀殑楠屾敹璁板綍銆?"""

    id: str
    run_id: str
    dry_run: bool
    applied: bool
    ok: bool
    decision: str
    message: str
    before_status: str
    after_status: str
    before_verification_status: str
    after_verification_status: str
    reviewer: str = ""
    note: str = ""
    evidence_count: int = 0
    test_count: int = 0
    artifact_count: int = 0
    findings: list[AcceptanceReviewFinding] = field(default_factory=list)
    evidence_paths: list[str] = field(default_factory=list)
    created_at: float = 0.0


@dataclass
class AcceptanceReviewReport:
    """鐖朵唬鐞嗛獙鏀舵姤鍛娿€?"""

    generated_at: float
    dry_run: bool
    summary: dict[str, int]
    records: list[AcceptanceReviewRecord]


@dataclass
class PatchReviewRecord:
    """runner patch 杈撳嚭鐨勫鏍歌褰曘€?"""

    id: str
    run_id: str
    dry_run: bool
    applied: bool
    ok: bool
    decision: str
    message: str
    patch_count: int
    approved_count: int = 0
    blocked_count: int = 0
    reviewer: str = ""
    note: str = ""
    evidence_paths: list[str] = field(default_factory=list)
    patches: list[dict[str, object]] = field(default_factory=list)
    created_at: float = 0.0


@dataclass
class PatchReviewReport:
    """鎵归噺 patch 瀹℃牳鎶ュ憡銆?"""

    generated_at: float
    dry_run: bool
    summary: dict[str, int]
    records: list[PatchReviewRecord]


@dataclass
class PatchApplyRecord:
    """鐙珛 patch apply 瀹℃牳閾剧殑涓€娆¤褰曘€?"""

    id: str
    run_id: str
    dry_run: bool
    applied: bool
    ok: bool
    decision: str
    message: str
    patch_count: int
    applied_count: int = 0
    blocked_count: int = 0
    rollback_performed: bool = False
    applier: str = ""
    note: str = ""
    evidence_paths: list[str] = field(default_factory=list)
    test_commands: list[str] = field(default_factory=list)
    test_results: list[dict[str, object]] = field(default_factory=list)
    patches: list[dict[str, object]] = field(default_factory=list)
    created_at: float = 0.0


@dataclass
class PatchApplyReport:
    """鎵归噺 patch apply 鎶ュ憡銆?"""

    generated_at: float
    dry_run: bool
    summary: dict[str, int]
    records: list[PatchApplyRecord]
