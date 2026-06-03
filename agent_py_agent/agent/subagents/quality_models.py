
from __future__ import annotations

"""quality contract dataclasses shared by subagent task and workflow state.

这里只放任务质量标准和上下文清单，避免核心 models.py 继续膨胀。
"""

from dataclasses import dataclass, field


@dataclass
class QualityContract:
    """Structured quality bar passed from the parent session to a worker."""

    user_visible_goal: str = ""
    benchmark_sample: str = ""
    quality_bar: str = ""
    failure_conditions: list[str] = field(default_factory=list)
    forbidden_delivery: list[str] = field(default_factory=list)
    must_check: list[str] = field(default_factory=list)
    sampling_plan: list[str] = field(default_factory=list)
    evidence_required: list[str] = field(default_factory=list)
    risk_report_required: str = ""
    allowed_degradation: list[str] = field(default_factory=list)


@dataclass
class ContextManifest:
    """Manifest describing the focused context packs given to a worker."""

    core_pack_version: str = "subagent-quality-contract-v1"
    task_pack_refs: list[str] = field(default_factory=list)
    role_pack: str = ""
    required_read_paths: list[str] = field(default_factory=list)
    hint_read_paths: list[str] = field(default_factory=list)
    quality_contract_ref: str = ""
    omitted_context: list[str] = field(default_factory=list)
    token_budget: int = 0
