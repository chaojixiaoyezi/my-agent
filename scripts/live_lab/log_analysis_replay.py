from __future__ import annotations

"""Offline SecurityAlertV1 scenario replay for the Live Lab."""

import argparse
import json
import time
import uuid
from pathlib import Path
from typing import Any

from agent_py_agent.agent.log_analysis.analytics.detectors import run_soft_detectors
from agent_py_agent.agent.log_analysis.cases.case_store import CaseStore
from agent_py_agent.agent.log_analysis.cases.scheduler import schedule_findings
from agent_py_agent.agent.log_analysis.ingest.pipeline import ingest_file
from agent_py_agent.agent.log_analysis.reports import (
    first_response_report_content,
    forensic_package_content,
)
from agent_py_agent.agent.log_analysis.security.correlation import build_route_draft
from agent_py_agent.agent.log_analysis.storage.local_store import LocalLogStore
from agent_py_agent.agent.log_analysis.tools import trace_case

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FIXTURE = REPO_ROOT / "validation" / "security_fixtures" / "security_alert_v1.jsonl"
DEFAULT_OUTPUTS_DIR = REPO_ROOT / "validation" / "live_lab" / "log_analysis_replay"
DEFAULT_START_TIME = "2026-04-30T09:30:00Z"
DEFAULT_END_TIME = "2026-04-30T10:30:00Z"
SIMULATED_FAILURE_STAGES = frozenset({"evidence", "report"})


class ReplayStageError(RuntimeError):
    """Raised when a replay stage finishes without the expected artifact."""


def run_security_alert_v1_replay(
    *,
    output_root: str | Path,
    fixture_path: str | Path = DEFAULT_FIXTURE,
    fixture_format: str | None = None,
    source_id: str = "security-alert-v1-live-lab",
    start_time: str = DEFAULT_START_TIME,
    end_time: str = DEFAULT_END_TIME,
    limit: int = 50,
    dry_run: bool = True,
    simulate_failure_stage: str | None = None,
) -> dict[str, Any]:
    output_root = Path(output_root)
    store_root = output_root / "store"
    artifacts_root = output_root / "artifacts"
    output_root.mkdir(parents=True, exist_ok=True)
    artifacts_root.mkdir(parents=True, exist_ok=True)

    summary: dict[str, Any] = {
        "ok": False,
        "scenario": "SecurityAlertV1",
        "dry_run": dry_run,
        "fixture_path": str(Path(fixture_path)),
        "fixture_format": fixture_format,
        "output_root": str(output_root),
        "store_root": str(store_root),
        "failed_stage": None,
        "stages": {},
        "stored_events": 0,
        "total_events": 0,
        "parsed_events": 0,
        "dead_letter_events": 0,
        "duplicate_events": 0,
        "skipped_events": 0,
        "stored_event_ids": [],
        "finding_count": 0,
        "finding_ids": [],
        "case_count": 0,
        "case_ids": [],
        "case_id": "",
        "case_path": "",
        "route_path": "",
        "report_path": "",
        "forensic_package_path": "",
        "evidence_refs": [],
        "evidence_paths": [],
        "summary_path": str(output_root / "replay_summary.json"),
    }

    stage = "setup"
    try:
        if simulate_failure_stage and simulate_failure_stage not in SIMULATED_FAILURE_STAGES:
            allowed = ", ".join(sorted(SIMULATED_FAILURE_STAGES))
            raise ValueError(f"simulate_failure_stage must be one of: {allowed}")

        stage = "ingest"
        summary["stages"][stage] = "running"
        ingest_result = ingest_file(
            fixture_path,
            root=store_root,
            source_id=source_id,
            file_format=fixture_format,
        )
        summary["fixture_format"] = ingest_result.file_format
        summary["parsed_events"] = ingest_result.parsed_count
        summary["stored_events"] = ingest_result.stored_count
        summary["dead_letter_events"] = ingest_result.dead_letter_count
        summary["duplicate_events"] = ingest_result.duplicate_count
        summary["skipped_events"] = ingest_result.skipped_count
        summary["stored_event_ids"] = list(ingest_result.stored_event_ids)
        summary["manifest_path"] = ingest_result.manifest_path
        summary["checkpoint_path"] = ingest_result.checkpoint_path
        summary["events_path"] = ingest_result.events_path
        summary["stages"][stage] = "pass"

        stage = "detector"
        summary["stages"][stage] = "running"
        store = LocalLogStore(store_root)
        events = store.list_events()
        summary["total_events"] = len(events)
        if not events:
            raise ReplayStageError("ingest produced no readable events")
        findings = run_soft_detectors(events)
        summary["finding_count"] = len(findings)
        summary["finding_ids"] = [finding.finding_id for finding in findings]
        summary["detectors"] = sorted({finding.detector_id for finding in findings})
        if not findings:
            raise ReplayStageError("detectors produced no findings")
        summary["stages"][stage] = "pass"

        stage = "case"
        summary["stages"][stage] = "running"
        case_store = CaseStore(store_root)
        schedule = schedule_findings(findings, store=case_store)
        cases = schedule.cases
        summary["case_count"] = len(cases)
        summary["case_ids"] = [case.case_id for case in cases]
        summary["low_confidence_findings"] = list(schedule.low_confidence_findings)
        if not cases:
            raise ReplayStageError("case scheduler produced no cases")
        case = max(cases, key=lambda item: (item.risk_score, len(item.finding_refs)))
        case_path = artifacts_root / "case.json"
        case_path.write_text(
            json.dumps(case.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        summary["case_id"] = case.case_id
        summary["case_path"] = str(case_path)
        summary["case_store_path"] = str(case_store.store.cases_path)
        summary["stages"][stage] = "pass"

        stage = "route"
        summary["stages"][stage] = "running"
        route = build_route_draft(case, findings=findings)
        route_path = artifacts_root / "route.json"
        route_path.write_text(
            json.dumps(route.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        summary["route_path"] = str(route_path)
        summary["route_evidence_refs"] = list(route.evidence_refs)
        summary["route_next_query_count"] = len(route.next_queries)
        summary["stages"][stage] = "pass"

        stage = "evidence"
        summary["stages"][stage] = "running"
        if simulate_failure_stage == stage:
            raise ReplayStageError("simulated evidence stage failure")
        traced = trace_case(
            case.case_id,
            root=store_root,
            start_time=start_time,
            end_time=end_time,
            limit=limit,
        )
        evidence_paths = [
            str(query.get("evidence_path"))
            for query in traced.get("queries", [])
            if query.get("evidence_path")
        ]
        summary["trace"] = {
            "case_id": traced.get("case_id"),
            "query_count": traced.get("query_count"),
            "row_count": traced.get("row_count"),
            "truncated": traced.get("truncated"),
        }
        summary["evidence_refs"] = list(traced.get("evidence_refs", []))
        summary["evidence_paths"] = evidence_paths
        if not evidence_paths and not summary["evidence_refs"]:
            raise ReplayStageError("trace-case produced no evidence refs")
        summary["stages"][stage] = "pass"

        stage = "report"
        summary["stages"][stage] = "running"
        if simulate_failure_stage == stage:
            raise ReplayStageError("simulated report stage failure")
        report_path = artifacts_root / "first_response_report.md"
        report_path.write_text(first_response_report_content(case, route, findings=findings), encoding="utf-8")
        package_path = artifacts_root / "forensic_package.json"
        package_path.write_text(
            forensic_package_content(
                case,
                route,
                findings=findings,
                query_history=traced.get("queries", []),
                raw_refs=list(route.evidence_refs),
                frozen=True,
            )
            + "\n",
            encoding="utf-8",
        )
        summary["report_path"] = str(report_path)
        summary["forensic_package_path"] = str(package_path)
        summary["stages"][stage] = "pass"
        summary["ok"] = True
    except Exception as exc:
        summary["ok"] = False
        summary["failed_stage"] = stage
        summary["stages"][stage] = "fail"
        summary["error_type"] = type(exc).__name__
        summary["error_message"] = str(exc)
        summary["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        Path(summary["summary_path"]).write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return summary


def new_output_root() -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    return DEFAULT_OUTPUTS_DIR / stamp


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Replay the SecurityAlertV1 log-analysis scenario offline.")
    parser.add_argument("--fixture", default=str(DEFAULT_FIXTURE), help="SecurityAlertV1 JSONL fixture path.")
    parser.add_argument("--fixture-format", default=None, help="Fixture format override: jsonl, csv, or log.")
    parser.add_argument("--output-root", default=str(new_output_root()), help="Replay output directory.")
    parser.add_argument("--source-id", default="security-alert-v1-live-lab", help="Ingest source id.")
    parser.add_argument("--start-time", default=DEFAULT_START_TIME, help="Trace/query start time.")
    parser.add_argument("--end-time", default=DEFAULT_END_TIME, help="Trace/query end time.")
    parser.add_argument("--limit", type=int, default=50, help="Trace/query row limit.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = run_security_alert_v1_replay(
        output_root=args.output_root,
        fixture_path=args.fixture,
        fixture_format=args.fixture_format,
        source_id=args.source_id,
        start_time=args.start_time,
        end_time=args.end_time,
        limit=args.limit,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if summary["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
