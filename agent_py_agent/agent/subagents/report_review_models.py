
from __future__ import annotations

"""Patch review report models."""

from dataclasses import dataclass, field


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
    load_errors: list[dict[str, object]] = field(default_factory=list)
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
    load_errors: list[dict[str, object]] = field(default_factory=list)
    created_at: float = 0.0


@dataclass
class PatchApplyReport:
    """鎵归噺 patch apply 鎶ュ憡銆?"""

    generated_at: float
    dry_run: bool
    summary: dict[str, int]
    records: list[PatchApplyRecord]
