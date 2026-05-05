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
    ReplayStageError,
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
    findings = case = route = traced = None

    ok, _ = _run_stage(
        summary,
        "ingest",
        lambda: run_ingest_stage(fixture_path, store_root, params.source_id, params.fixture_format),
    )
    if not ok:
        return _write_and_return(summary)

    ok, findings = _run_stage(summary, "detector", lambda: run_detector_stage(store_root))
    if not ok:
        # Ensure total_events is preserved even when detector stage fails
        from agent_py_agent.agent.log_analysis.storage.local_store import LocalLogStore
        store = LocalLogStore(store_root)
        summary["total_events"] = len(store.list_events())
        return _write_and_return(summary)

    ok, case = _run_stage(
        summary, "case", lambda: run_case_stage(findings, store_root, artifacts_root)
    )
    if not ok:
        return _write_and_return(summary)

    ok, route = _run_stage(
        summary, "route", lambda: run_route_stage(case, findings, artifacts_root)
    )
    if not ok:
        return _write_and_return(summary)

    ok, traced = _run_stage(
        summary,
        "evidence",
        lambda: run_evidence_stage(
            case, store_root, params.start_time, params.end_time, params.limit, params.simulate_failure_stage
        ),
    )
    if not ok:
        return _write_and_return(summary)

    ok, _ = _run_stage(
        summary,
        "report",
        lambda: run_report_stage(
            case, route, findings, traced, artifacts_root, params.simulate_failure_stage
        ),
    )
    if not ok:
        return _write_and_return(summary)

    summary["ok"] = True
    return _write_and_return(summary)


def _record_failure(summary: dict[str, Any], stage: str, exc: Exception) -> None:
    summary["ok"] = False
    summary["failed_stage"] = stage
    summary["stages"][stage] = "fail"
    summary["error_type"] = type(exc).__name__
    summary["error_message"] = str(exc)
    summary["error"] = f"{type(exc).__name__}: {exc}"


def _write_and_return(summary: dict[str, Any]) -> dict[str, Any]:
    Path(summary["summary_path"]).write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return summary


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
