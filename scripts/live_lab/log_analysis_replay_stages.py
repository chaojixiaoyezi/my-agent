# LLM: Live Lab validation script; keep CLI flags, artifact paths, and replay outputs stable for scenario tests.
# 模块用途: 支撑可见验收和回放场景，负责启动案例、整理输出或生成报告。

"""Stage helpers for run_security_alert_v1_replay — each returns (summary_updates, stage_result) or raises."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_py_agent.agent.log_analysis.cases.case_store import CaseStore
from agent_py_agent.agent.log_analysis.cases.scheduler import schedule_findings
from agent_py_agent.agent.log_analysis.ingest.pipeline import ingest_file
from agent_py_agent.agent.log_analysis.security.correlation import build_route_draft
from agent_py_agent.agent.log_analysis.storage.local_store import LocalLogStore
from agent_py_agent.agent.log_analysis.tools import trace_case


# LLM: ReplayStageError 是Live Lab 验收的数据契约；字段名会被调用方和测试读取。
# 类用途: 标记本流程的专用异常，方便入口层给出明确失败信息。
class ReplayStageError(RuntimeError):
    """Raised when a replay stage finishes without the expected artifact."""


# LLM: EvidenceStageParams 是Live Lab 验收的数据契约；字段名会被调用方和测试读取。
# 类用途: 保存一次调用所需参数，避免 CLI 和服务层之间散传字段。
@dataclass(frozen=True)
class EvidenceStageParams:
    case: Any
    store_root: Path
    start_time: str
    end_time: str
    limit: int
    simulate_failure_stage: str | None


# LLM: ReportStageParams 是Live Lab 验收的数据契约；字段名会被调用方和测试读取。
# 类用途: 保存一次调用所需参数，避免 CLI 和服务层之间散传字段。
@dataclass(frozen=True)
class ReportStageParams:
    case: Any
    route: Any
    findings: list
    traced: Any
    artifacts_root: Path
    simulate_failure_stage: str | None


# LLM: run_ingest_stage 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
# 函数用途: 执行对应流程阶段，并把成功、失败和产物写入汇总状态。
def run_ingest_stage(
    fixture_path: Path,
    store_root: Path,
    source_id: str,
    fixture_format: str | None,
) -> tuple[dict[str, Any], Any]:
    """Ingest fixture file and return result."""
    result = ingest_file(
        fixture_path,
        root=store_root,
        source_id=source_id,
        file_format=fixture_format,
    )
    return {
        "fixture_format": result.file_format,
        "parsed_events": result.parsed_count,
        "stored_events": result.stored_count,
        "dead_letter_events": result.dead_letter_count,
        "duplicate_events": result.duplicate_count,
        "skipped_events": result.skipped_count,
        "stored_event_ids": list(result.stored_event_ids),
        "manifest_path": result.manifest_path,
        "checkpoint_path": result.checkpoint_path,
        "events_path": result.events_path,
    }, result


# LLM: run_detector_stage 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
# 函数用途: 执行对应流程阶段，并把成功、失败和产物写入汇总状态。
def run_detector_stage(store_root: Path) -> tuple[dict[str, Any], Any]:
    """Run soft detectors on stored events."""
    from agent_py_agent.agent.log_analysis.analytics.detectors import run_soft_detectors

    store = LocalLogStore(store_root)
    events = store.list_events()
    if not events:
        raise ReplayStageError("ingest produced no readable events")
    total_events = len(events)
    findings = run_soft_detectors(events)
    if not findings:
        raise ReplayStageError("detectors produced no findings")
    return {
        "total_events": total_events,
        "finding_count": len(findings),
        "finding_ids": [finding.finding_id for finding in findings],
        "detectors": sorted({finding.detector_id for finding in findings}),
    }, findings


# LLM: run_case_stage 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
# 函数用途: 执行对应流程阶段，并把成功、失败和产物写入汇总状态。
def run_case_stage(
    findings: list, store_root: Path, artifacts_root: Path
) -> tuple[dict[str, Any], Any]:
    """Schedule findings into cases and write the top case to artifacts."""
    case_store = CaseStore(store_root)
    schedule = schedule_findings(findings, store=case_store)
    cases = schedule.cases
    if not cases:
        raise ReplayStageError("case scheduler produced no cases")
    case = max(cases, key=lambda item: (item.risk_score, len(item.finding_refs)))
    case_path = artifacts_root / "case.json"
    case_path.write_text(
        json.dumps(case.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "case_count": len(cases),
        "case_ids": [case.case_id for case in cases],
        "low_confidence_findings": list(schedule.low_confidence_findings),
        "case_id": case.case_id,
        "case_path": str(case_path),
        "case_store_path": str(case_store.store.cases_path),
    }, case


# LLM: run_route_stage 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
# 函数用途: 执行对应流程阶段，并把成功、失败和产物写入汇总状态。
def run_route_stage(case: Any, findings: list, artifacts_root: Path) -> tuple[dict[str, Any], Any]:
    """Build route draft and write to artifacts."""
    route = build_route_draft(case, findings=findings)
    route_path = artifacts_root / "route.json"
    route_path.write_text(
        json.dumps(route.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "route_path": str(route_path),
        "route_evidence_refs": list(route.evidence_refs),
        "route_next_query_count": len(route.next_queries),
    }, route


# LLM: run_evidence_stage 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
# 函数用途: 执行对应流程阶段，并把成功、失败和产物写入汇总状态。
def run_evidence_stage(params: EvidenceStageParams) -> tuple[dict[str, Any], Any]:
    """Trace case and collect evidence paths."""
    if params.simulate_failure_stage == "evidence":
        raise ReplayStageError("simulated evidence stage failure")
    traced = trace_case(
        params.case.case_id,
        root=params.store_root,
        start_time=params.start_time,
        end_time=params.end_time,
        limit=params.limit,
    )
    evidence_paths = [
        str(query.get("evidence_path"))
        for query in traced.get("queries", [])
        if query.get("evidence_path")
    ]
    if not evidence_paths and not traced.get("evidence_refs"):
        raise ReplayStageError("trace-case produced no evidence refs")
    return {
        "trace": {
            "case_id": traced.get("case_id"),
            "query_count": traced.get("query_count"),
            "row_count": traced.get("row_count"),
            "truncated": traced.get("truncated"),
        },
        "evidence_refs": list(traced.get("evidence_refs", [])),
        "evidence_paths": evidence_paths,
    }, traced


# LLM: run_report_stage 属于Live Lab 验收；改行为前先对齐调用方和快照/单测。
# 函数用途: 执行对应流程阶段，并把成功、失败和产物写入汇总状态。
def run_report_stage(params: ReportStageParams) -> tuple[dict[str, Any], Any]:
    """Write first-response report and forensic package."""
    from agent_py_agent.agent.log_analysis.reports import (
        first_response_report_content,
        forensic_package_content,
    )

    if params.simulate_failure_stage == "report":
        raise ReplayStageError("simulated report stage failure")
    report_path = params.artifacts_root / "first_response_report.md"
    report_path.write_text(
        first_response_report_content(params.case, params.route, findings=params.findings),
        encoding="utf-8",
    )
    package_path = params.artifacts_root / "forensic_package.json"
    package_path.write_text(
        forensic_package_content(
            params.case,
            params.route,
            findings=params.findings,
            query_history=params.traced.get("queries", []),
            raw_refs=list(params.route.evidence_refs),
            frozen=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return {
        "report_path": str(report_path),
        "forensic_package_path": str(package_path),
    }, None
