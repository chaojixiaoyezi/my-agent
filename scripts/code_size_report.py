# LLM: Code-size governance helper; keep report identities, thresholds, and baseline behavior stable.
# 模块用途: 支撑代码规模守卫，统计文件/函数/类大小并生成可审查的报告。

from __future__ import annotations

"""Markdown report rendering for the code-size checker."""

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# LLM: ReportRenderContext 是 code-size 报告渲染参数契约。
# 类用途: 保存 mode、blocked 和 baseline 状态，供 Markdown 报告头部读取。
@dataclass(frozen=True)
class ReportRenderContext:
    mode: str
    blocked: bool
    baseline_path: str | None
    baseline_loaded: bool


# LLM: _format_table 负责 finding 表格行；列顺序是报告格式契约。
# 函数用途: 把 findings 渲染成 Markdown 表格，并按 limit 截断长列表。
def _format_table(findings: list[Any], limit: int = 0) -> list[str]:
    if not findings:
        return ["- none"]
    lines = [
        "| Severity | Kind | Path | Name | Value | Limit | Message |",
        "| --- | --- | --- | --- | ---: | ---: | --- |",
    ]
    items = findings[:limit] if limit > 0 else findings
    for item in items:
        lines.append(
            f"| {item.severity} | {item.kind} | `{item.path}` | `{item.name}` | "
            f"{item.value} | {item.limit} | {item.message} |"
        )
    if limit > 0 and len(findings) > limit:
        lines.append(f"| ... | ... | ... | ... | ... | ... | *{len(findings) - limit} more* |")
    return lines


# LLM: _section 组装报告章节；标题和表格紧邻输出。
# 函数用途: 返回一个 Markdown 小节，包括空行、标题和 findings 表格。
def _section(title: str, findings: list[Any], limit: int = 0) -> list[str]:
    return ["", title, *_format_table(findings, limit)]


# LLM: _findings_by_kind 固定报告分类；新增 finding kind 要同步这里。
# 函数用途: 按 kind 把 findings 分桶，供后续章节按类别读取。
def _findings_by_kind(findings: list[Any]) -> dict[str, list[Any]]:
    kinds = {
        "file",
        "function",
        "class",
        "mixin",
        "params",
        "nesting",
        "high_risk_growth",
        "import_star",
        "decode_error",
        "junk_name",
    }
    return {kind: [item for item in findings if item.kind == kind] for kind in kinds}


# LLM: _summary_lines 生成报告摘要；字段名要和 CI 阅读口径一致。
# 函数用途: 统计 hard、high-risk、soft 数量，并输出 mode、baseline 和 blocked 状态。
def _summary_lines(findings: list[Any], context: ReportRenderContext) -> list[str]:
    hard = [item for item in findings if item.severity == "hard"]
    high_risk = [item for item in findings if item.severity == "high-risk"]
    soft = [item for item in findings if item.severity == "soft"]
    return [
        f"- mode: {context.mode}",
        f"- baseline: {context.baseline_path or 'none'}",
        f"- baseline_loaded: {context.baseline_loaded}",
        f"- blocked: {context.blocked}",
        f"- total_findings: {len(findings)}",
        f"- hard_findings: {len(hard)}",
        f"- high_risk_findings: {len(high_risk)}",
        f"- soft_findings: {len(soft)}",
    ]


# LLM: _finding_sections 控制 CODE_SIZE_REPORT 主体顺序。
# 函数用途: 按文件、函数、类、参数、嵌套等类别拼接所有 findings 章节。
def _finding_sections(findings: list[Any]) -> list[str]:
    by_kind = _findings_by_kind(findings)
    high_risk = [item for item in findings if item.severity == "high-risk"]
    soft = [item for item in findings if item.severity == "soft"]
    return [
        *_section("## 1. Oversized Files Top 20", by_kind["file"], 20),
        *_section("## 2. Oversized Functions Top 20", by_kind["function"], 20),
        *_section("## 3. Oversized Classes Top 20", by_kind["class"], 20),
        *_section("## 4. Oversized Mixins Top 20", by_kind["mixin"], 20),
        *_section("## 5. Too Many Params Top 20", by_kind["params"], 20),
        *_section("## 6. Deep Nesting Top 20", by_kind["nesting"], 20),
        *_section("## 7. High-risk / near-soft Top 100", high_risk, 100),
        *_section("## 8. High-risk File Growth", by_kind["high_risk_growth"]),
        *_section("## 9. import * Violations", by_kind["import_star"]),
        *_section("## 10. Decode Error Violations", by_kind["decode_error"]),
        *_section("## 11. Junk File / Junk Name Violations", by_kind["junk_name"]),
        *_section("## 12. Historical Soft Findings", soft[:100]),
    ]


# LLM: _recommendations 写报告末尾治理建议；内容面向后续拆分工作。
# 函数用途: 返回 code-size 报告里的下一步维护建议。
def _recommendations() -> list[str]:
    return [
        "",
        "## 13. Next Recommendations",
        "- Keep `cli/parser.py` thin and route registration through `cli/commands/`.",
        "- Continue extracting `cli/chat.py` into chat session, input loop, renderer, and gateway client modules.",
        "- Move SubAgent mixin logic into services and repositories behind the manager facade.",
        "- Split memory archive query/runtime and log analysis tools by query, rendering, and persistence responsibilities.",
        "- Run `--write-baseline` to capture current state, then use `--mode strict --baseline` to block only new violations.",
    ]


# LLM: write_report 是 Markdown 报告唯一写盘入口。
# 函数用途: 汇总摘要、分类章节、建议和阻断状态，并写入 CODE_SIZE_REPORT.md。
def write_report(report_path: Path, findings: list[Any], context: ReportRenderContext) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        "# CODE SIZE REPORT",
        "",
        f"Generated at: {now}",
        f"Generated by: `python scripts/check_code_size.py --mode {context.mode}`",
        "",
        *_summary_lines(findings, context),
        *_finding_sections(findings),
        *_recommendations(),
        "",
        "## 14. Strict Blocked",
        f"- {'**yes**' if context.blocked else 'no'}",
        "",
    ]
    report_path.write_text("\n".join(lines), encoding="utf-8")
