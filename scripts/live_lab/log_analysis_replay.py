
from __future__ import annotations

"""Offline SecurityAlertV1 scenario replay for the Live Lab."""

import argparse
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .log_analysis_replay_stages import (
    EvidenceStageParams,
    ReportStageParams,
    run_case_stage,
    run_detector_stage,
    run_evidence_stage,
    run_ingest_stage,
    run_report_stage,
    run_route_stage,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_FIXTURE = REPO_ROOT / "validation" / "security_fixtures" / "security_alert_v1.jsonl"
DEFAULT_OUTPUTS_DIR = REPO_ROOT / "validation" / "live_lab" / "log_analysis_replay"
DEFAULT_START_TIME = "2026-04-30T09:30:00Z"
DEFAULT_END_TIME = "2026-04-30T10:30:00Z"
SIMULATED_FAILURE_STAGES = frozenset({"evidence", "report"})


@dataclass(frozen=True)
class RunSecurityAlertV1ReplayParams:
    """Parameter bundle for run_security_alert_v1_replay."""

    output_root: str | Path
    fixture_path: str | Path = DEFAULT_FIXTURE
    fixture_format: str | None = None
    source_id: str = "security-alert-v1-live-lab"
    start_time: str = DEFAULT_START_TIME
    end_time: str = DEFAULT_END_TIME
    limit: int = 50
    dry_run: bool = True
    simulate_failure_stage: str | None = None


@dataclass
class ReplayState:
    findings: Any = None
    case: Any = None
    route: Any = None
    traced: Any = None


@dataclass(frozen=True)
class ReplayStageContext:
    summary: dict[str, Any]
    fixture_path: Path
    store_root: Path
    artifacts_root: Path
    params: RunSecurityAlertV1ReplayParams


def _base_summary(
    output_root: Path, fixture_path: Path, fixture_format, dry_run: bool
) -> dict[str, Any]:
    """Build the base summary dict with all fields initialized."""
    store_root = output_root / "store"
    return {
        "ok": False,
        "scenario": "SecurityAlertV1",
        "dry_run": dry_run,
        "fixture_path": str(fixture_path),
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


def _run_stage(
    summary: dict[str, Any],
    stage: str,
    handler: Any,
) -> tuple[bool, Any]:
    """Run a single stage handler, update summary on success, record failure and return (False, None) on error."""
    try:
        summary["stages"][stage] = "running"
        updates, result = handler()
        summary.update(updates)
        summary["stages"][stage] = "pass"
        return True, result
    except Exception as exc:
        _record_failure(summary, stage, exc)
        return False, None


def _write_summary(summary: dict[str, Any]) -> dict[str, Any]:
    """Write summary to disk and return it."""
    Path(summary["summary_path"]).write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


def _capture_stage_result(stage_name: str, result: Any, state: ReplayState) -> ReplayState:
    """Capture a stage result into the appropriate variable, returning updated tuple."""
    if stage_name == "detector":
        state.findings = result
    if stage_name == "case":
        state.case = result
    if stage_name == "route":
        state.route = result
    return state


def _run_replay_stages(context: ReplayStageContext) -> tuple[dict[str, Any], Any, Any, Any, Any]:
    state = ReplayState()
    if not _run_ingest_replay(context):
        return _replay_result(context, state)
    if not _run_detector_replay(context, state):
        return _replay_result(context, state)
    if not _run_case_replay(context, state):
        return _replay_result(context, state)
    if not _run_route_replay(context, state):
        return _replay_result(context, state)
    if not _run_evidence_replay(context, state):
        return _replay_result(context, state)
    _run_report_replay(context, state)
    return _replay_result(context, state)


def _replay_result(context: ReplayStageContext, state: ReplayState) -> tuple[dict[str, Any], Any, Any, Any, Any]:
    return context.summary, state.findings, state.case, state.route, state.traced


def _run_ingest_replay(context: ReplayStageContext) -> bool:
    ok, _ = _run_stage(
        context.summary,
        "ingest",
        lambda: run_ingest_stage(
            context.fixture_path,
            context.store_root,
            context.params.source_id,
            context.params.fixture_format,
        ),
    )
    return ok


def _run_detector_replay(context: ReplayStageContext, state: ReplayState) -> bool:
    ok, state.findings = _run_stage(context.summary, "detector", lambda: run_detector_stage(context.store_root))
    if not ok:
        from agent_py_agent.agent.log_analysis.storage.local_store import LocalLogStore

        context.summary["total_events"] = len(LocalLogStore(context.store_root).list_events())
    return ok


def _run_case_replay(context: ReplayStageContext, state: ReplayState) -> bool:
    ok, state.case = _run_stage(
        context.summary,
        "case",
        lambda: run_case_stage(state.findings, context.store_root, context.artifacts_root),
    )
    return ok


def _run_route_replay(context: ReplayStageContext, state: ReplayState) -> bool:
    ok, state.route = _run_stage(
        context.summary,
        "route",
        lambda: run_route_stage(state.case, state.findings, context.artifacts_root),
    )
    return ok


def _run_evidence_replay(context: ReplayStageContext, state: ReplayState) -> bool:
    ok, state.traced = _run_stage(
        context.summary,
        "evidence",
        lambda: run_evidence_stage(
            EvidenceStageParams(
                state.case,
                context.store_root,
                context.params.start_time,
                context.params.end_time,
                context.params.limit,
                context.params.simulate_failure_stage,
            )
        ),
    )
    return ok


def _run_report_replay(context: ReplayStageContext, state: ReplayState) -> bool:
    ok, _ = _run_stage(
        context.summary,
        "report",
        lambda: run_report_stage(
            ReportStageParams(
                state.case,
                state.route,
                state.findings,
                state.traced,
                context.artifacts_root,
                context.params.simulate_failure_stage,
            )
        ),
    )
    return ok


def run_security_alert_v1_replay(
    params: RunSecurityAlertV1ReplayParams,
) -> dict[str, Any]:
    output_root = Path(params.output_root)
    fixture_path = Path(params.fixture_path)
    store_root = output_root / "store"
    artifacts_root = output_root / "artifacts"
    output_root.mkdir(parents=True, exist_ok=True)
    artifacts_root.mkdir(parents=True, exist_ok=True)

    if params.simulate_failure_stage and params.simulate_failure_stage not in SIMULATED_FAILURE_STAGES:
        allowed = ", ".join(sorted(SIMULATED_FAILURE_STAGES))
        raise ValueError(f"simulate_failure_stage must be one of: {allowed}")

    summary = _base_summary(output_root, fixture_path, params.fixture_format, params.dry_run)
    summary, findings, case, route, traced = _run_replay_stages(
        ReplayStageContext(summary, fixture_path, store_root, artifacts_root, params)
    )

    if summary["stages"].get("report") == "pass":
        summary["ok"] = True

    return _write_summary(summary)


def _record_failure(summary: dict[str, Any], stage: str, exc: Exception) -> None:
    summary["ok"] = False
    summary["failed_stage"] = stage
    summary["stages"][stage] = "fail"
    summary["error_type"] = type(exc).__name__
    summary["error_message"] = str(exc)
    summary["error"] = f"{type(exc).__name__}: {exc}"


def new_output_root() -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    return DEFAULT_OUTPUTS_DIR / stamp


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay the SecurityAlertV1 log-analysis scenario offline."
    )
    parser.add_argument(
        "--fixture", default=str(DEFAULT_FIXTURE), help="SecurityAlertV1 JSONL fixture path."
    )
    parser.add_argument(
        "--fixture-format", default=None, help="Fixture format override: jsonl, csv, or log."
    )
    parser.add_argument(
        "--output-root", default=str(new_output_root()), help="Replay output directory."
    )
    parser.add_argument(
        "--source-id", default="security-alert-v1-live-lab", help="Ingest source id."
    )
    parser.add_argument("--start-time", default=DEFAULT_START_TIME, help="Trace/query start time.")
    parser.add_argument("--end-time", default=DEFAULT_END_TIME, help="Trace/query end time.")
    parser.add_argument("--limit", type=int, default=50, help="Trace/query row limit.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    summary = run_security_alert_v1_replay(
        RunSecurityAlertV1ReplayParams(
            output_root=args.output_root,
            fixture_path=args.fixture,
            fixture_format=args.fixture_format,
            source_id=args.source_id,
            start_time=args.start_time,
            end_time=args.end_time,
            limit=args.limit,
        )
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if summary["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
