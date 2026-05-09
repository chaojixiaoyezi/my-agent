# LLM: Subagent budget manager facade; keep reporting refs-only and side-effect light.
# 模块用途: 给 SubAgentManager 增加 runner 预算报告写入入口，不参与 runner 执行或调度决策。

from __future__ import annotations

"""Manager mixin for subagent runner budget reports."""

import json
from dataclasses import asdict, replace

from .run_budget import (
    SubagentRunBudgetReport,
    SubagentRunBudgetRequest,
    build_subagent_run_budget_report,
)


# LLM: SubAgentBudgetMixin adds a small persisted budget-report capability to the public manager.
# 类用途: 写入子代理运行预算报告，方便大型 E2E 后快速看模型调用、工具轮数和 token 估算。
class SubAgentBudgetMixin:
    """Facade for refs-only subagent runner budget reports."""

    # LLM: write_run_budget_report persists the same summary used by CLI and E2E harnesses.
    # 函数用途: 生成并写入预算 JSON/Markdown；不会读取大 artifact，也不会阻断 runner。
    def write_run_budget_report(self, *, params: SubagentRunBudgetRequest) -> SubagentRunBudgetReport:
        request = replace(params, manager=self)
        report = build_subagent_run_budget_report(request)
        (self.workspace / "subagent_run_budget.json").write_text(
            json.dumps(asdict(report), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        (self.workspace / "SUBAGENT_RUN_BUDGET.md").write_text(
            _render_run_budget_report(report),
            encoding="utf-8",
        )
        return report


# LLM: _render_run_budget_report keeps human output compact and refs-only.
# 函数用途: 渲染预算 Markdown 摘要，展示阈值、超限项和每个 runner 的文件引用。
def _render_run_budget_report(report: SubagentRunBudgetReport) -> str:
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
    return "\n".join(lines) + "\n"
