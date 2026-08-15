from __future__ import annotations

"""Refs-only budget reports for subagent runner executions."""

import json
from dataclasses import asdict, replace

from ..run_budget import (
    SubagentRunBudgetReport,
    SubagentRunBudgetRequest,
    build_subagent_run_budget_report,
)


class SubAgentBudgetService:
    """Builds and writes runner budget summaries without loading artifact bodies."""

    def __init__(self, manager: object) -> None:
        self.manager = manager

    def write_run_budget_report(self, *, params: SubagentRunBudgetRequest) -> SubagentRunBudgetReport:
        request = replace(params, manager=self.manager)
        report = build_subagent_run_budget_report(request)
        workspace = self.manager.workspace
        (workspace / "subagent_run_budget.json").write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (workspace / "SUBAGENT_RUN_BUDGET.md").write_text(
            render_run_budget_report(report),
            encoding="utf-8",
        )
        return report


def render_run_budget_report(report: SubagentRunBudgetReport) -> str:
    lines = [
        "# Subagent Run Budget",
        "",
        f"- root_id: `{report.root_id or 'all'}`",
        f"- totals: `{report.totals}`",
        f"- limits: `{report.limits}`",
        f"- exceeded: `{report.exceeded}`",
        "",
        "## Records",
    ]
    if not report.records:
        lines.append("- none")
    for record in report.records:
        lines.append(
            f"- `{record.run_id}` status={record.status} backend={record.backend or 'unknown'} "
            f"tool_rounds={record.tool_rounds} tokens≈"
            f"{record.prompt_token_estimate + record.response_token_estimate}"
        )
        lines.append(f"  - result: `{record.result_json}`")
    if report.load_errors:
        lines.extend(["", "## Load Errors"])
        for error in report.load_errors:
            lines.append(
                f"- `{error.get('context', 'unknown')}` run_id={error.get('run_id', '')} "
                f"path=`{error.get('path', '')}`"
            )
    return "\n".join(lines) + "\n"


__all__ = ["SubAgentBudgetService", "render_run_budget_report"]
