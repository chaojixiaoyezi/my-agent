# LLM: Fallback reports preserve child-agent results when normal artifact writes fail.
# 模块用途: 在子代理写文件失败或通道异常时，由父级保存简短兜底报告和 JSON 索引。

from __future__ import annotations

"""Fallback report writer for subagent runner recovery."""

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


# LLM: FallbackReportRequest is the bundle for parent-saved recovery reports.
# 类用途: 保存兜底报告写入所需的任务目录、run、摘要、正文和引用列表。
@dataclass(frozen=True)
class FallbackReportRequest:
    task_dir: str | Path
    run_id: str
    title: str
    summary: str
    details: str = ""
    reason: str = ""
    artifact_refs: list[str] = field(default_factory=list)
    evidence_refs: list[str] = field(default_factory=list)


# LLM: FallbackReportResult returns report refs without embedding recovered details into caller context.
# 类用途: 返回兜底报告是否写入成功、Markdown/JSON 路径和阻断原因。
@dataclass
class FallbackReportResult:
    written: bool
    markdown_ref: str = ""
    json_ref: str = ""
    blockers: list[str] = field(default_factory=list)


# LLM: write_fallback_report is side-effect bounded to task_dir/reports.
# 函数用途: 把子代理兜底结果写入 reports/fallback_report.md 和 fallback_report.json。
def write_fallback_report(request: FallbackReportRequest) -> FallbackReportResult:
    task_dir = Path(request.task_dir).expanduser().resolve()
    if not str(request.run_id).strip():
        return FallbackReportResult(False, blockers=["missing_run_id"])
    try:
        reports = task_dir / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        markdown = reports / "fallback_report.md"
        payload = _fallback_payload(request)
        markdown.write_text(_render_fallback_markdown(payload), encoding="utf-8")
        json_ref = reports / "fallback_report.json"
        json_ref.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        return FallbackReportResult(False, blockers=[f"write_failed:{exc}"])
    return FallbackReportResult(True, str(markdown), str(json_ref))


# LLM: _fallback_payload keeps the JSON and Markdown reports from drifting.
# 函数用途: 生成兜底报告的统一数据载荷，后续字段扩展只改这里。
def _fallback_payload(request: FallbackReportRequest) -> dict[str, object]:
    return {
        "run_id": request.run_id,
        "title": request.title,
        "summary": request.summary,
        "details": request.details,
        "reason": request.reason,
        "artifact_refs": list(request.artifact_refs),
        "evidence_refs": list(request.evidence_refs),
        "created_at": time.time(),
        "reserved": {},
    }


# LLM: _render_fallback_markdown is human-readable but still refs-first.
# 函数用途: 将兜底报告 payload 渲染成简短 Markdown，方便人工快速查看。
def _render_fallback_markdown(payload: dict[str, object]) -> str:
    lines = [
        f"# {payload['title'] or 'Fallback Report'}",
        "",
        f"- run_id: {payload['run_id']}",
        f"- reason: {payload['reason'] or 'fallback'}",
        "",
        "## Summary",
        str(payload["summary"]),
    ]
    if payload.get("details"):
        lines.extend(["", "## Details", str(payload["details"])])
    lines.extend(["", "## Refs"])
    for ref in payload.get("artifact_refs", []):
        lines.append(f"- artifact: {ref}")
    for ref in payload.get("evidence_refs", []):
        lines.append(f"- evidence: {ref}")
    return "\n".join(lines).rstrip() + "\n"


# LLM: fallback_result_to_dict keeps tests and future CLI rendering stable.
# 函数用途: 将兜底报告写入结果转换为 JSON 友好字典。
def fallback_result_to_dict(result: FallbackReportResult) -> dict[str, object]:
    return asdict(result)
