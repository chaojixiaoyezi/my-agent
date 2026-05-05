from __future__ import annotations

"""LLM: parent-planner report dataclasses split from reports.py.

给人看的解释：
父级 planner 的记录字段比较多，单独放这里，让 reports.py 继续作为兼容导出入口。
"""

from dataclasses import dataclass, field


@dataclass
class ParentPlannerParsedOutput:
    """父代理 planner 的结构化模型输出。"""

    found: bool
    ok: bool
    decision: str = ""
    summary: str = ""
    should_dispatch: bool = True
    runner_instruction: str = ""
    suggested_max_runners: int = 0
    actions: list[dict[str, object]] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    parse_error: str = ""
    raw_json: dict[str, object] = field(default_factory=dict)


@dataclass
class ParentPlannerRecord:
    """父代理 planner 的一次审计记录。"""

    id: str
    dry_run: bool
    triggered: bool
    ok: bool
    decision: str
    message: str
    gate_summary: dict[str, int] = field(default_factory=dict)
    backend: str = ""
    tool_rounds: int = 0
    parse_error: str = ""
    summary: str = ""
    actions: list[dict[str, object]] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    runner_instruction: str = ""
    suggested_max_runners: int = 0
    prompt_path: str = ""
    response_path: str = ""
    evidence_paths: list[str] = field(default_factory=list)
    created_at: float = 0.0


@dataclass
class ParentPlannerReport:
    """父代理 planner 报告。"""

    generated_at: float
    dry_run: bool
    summary: dict[str, int]
    records: list[ParentPlannerRecord]
