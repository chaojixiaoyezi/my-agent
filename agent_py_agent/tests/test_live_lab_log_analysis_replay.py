from __future__ import annotations

from pathlib import Path

from scripts.live_lab.log_analysis_replay import REPO_ROOT, run_security_alert_v1_replay

NO_FINDINGS_FIXTURE = REPO_ROOT / "validation" / "security_fixtures" / "security_alert_v1_no_findings.jsonl"


def test_security_alert_v1_live_lab_replay_success(tmp_path):
    summary = run_security_alert_v1_replay(output_root=tmp_path / "replay")

    assert summary["ok"] is True
    assert summary["failed_stage"] is None
    assert summary["stored_events"] == 3
    assert summary["total_events"] == 3
    assert summary["parsed_events"] == 3
    assert summary["fixture_format"] == "jsonl"
    assert summary["finding_count"] >= 1
    assert summary["case_id"]
    assert summary["route_path"]
    assert summary["report_path"]
    assert summary["evidence_paths"]
    assert all(Path(path).exists() for path in summary["evidence_paths"])

    for key in ("case_path", "route_path", "report_path", "forensic_package_path", "summary_path"):
        assert Path(summary[key]).exists()

    report = Path(summary["report_path"]).read_text(encoding="utf-8")
    assert "# First Response Report" in report
    assert "## Evidence References" in report


def test_security_alert_v1_live_lab_replay_reports_failed_stage(tmp_path):
    summary = run_security_alert_v1_replay(
        output_root=tmp_path / "replay",
        fixture_path=tmp_path / "missing.jsonl",
    )

    assert summary["ok"] is False
    assert summary["failed_stage"] == "ingest"
    assert summary["stages"]["ingest"] == "fail"
    assert summary["error_type"] == "FileNotFoundError"
    assert summary["stored_events"] == 0
    assert Path(summary["summary_path"]).exists()


def test_security_alert_v1_live_lab_replay_no_findings_reports_detector_stage(tmp_path):
    summary = run_security_alert_v1_replay(
        output_root=tmp_path / "replay",
        fixture_path=NO_FINDINGS_FIXTURE,
    )

    assert summary["ok"] is False
    assert summary["failed_stage"] == "detector"
    assert summary["stages"]["ingest"] == "pass"
    assert summary["stages"]["detector"] == "fail"
    assert summary["parsed_events"] == 2
    assert summary["stored_events"] == 2
    assert summary["total_events"] == 2
    assert summary["finding_count"] == 0
    assert summary["case_count"] == 0
    assert summary["error_type"] == "ReplayStageError"
    assert summary["error_message"] == "detectors produced no findings"
    assert Path(summary["summary_path"]).exists()


def test_security_alert_v1_live_lab_replay_reports_evidence_stage_failure(tmp_path):
    summary = run_security_alert_v1_replay(
        output_root=tmp_path / "replay",
        simulate_failure_stage="evidence",
    )

    assert summary["ok"] is False
    assert summary["failed_stage"] == "evidence"
    assert summary["stages"]["ingest"] == "pass"
    assert summary["stages"]["detector"] == "pass"
    assert summary["stages"]["case"] == "pass"
    assert summary["stages"]["route"] == "pass"
    assert summary["stages"]["evidence"] == "fail"
    assert summary["case_id"]
    assert summary["route_path"]
    assert summary["evidence_paths"] == []
    assert summary["error_type"] == "ReplayStageError"
    assert summary["error_message"] == "simulated evidence stage failure"
    assert Path(summary["summary_path"]).exists()


def test_security_alert_v1_live_lab_replay_reports_report_stage_failure(tmp_path):
    summary = run_security_alert_v1_replay(
        output_root=tmp_path / "replay",
        simulate_failure_stage="report",
    )

    assert summary["ok"] is False
    assert summary["failed_stage"] == "report"
    assert summary["stages"]["ingest"] == "pass"
    assert summary["stages"]["detector"] == "pass"
    assert summary["stages"]["case"] == "pass"
    assert summary["stages"]["route"] == "pass"
    assert summary["stages"]["evidence"] == "pass"
    assert summary["stages"]["report"] == "fail"
    assert summary["evidence_paths"]
    assert summary["report_path"] == ""
    assert summary["forensic_package_path"] == ""
    assert summary["error_type"] == "ReplayStageError"
    assert summary["error_message"] == "simulated report stage failure"
    assert Path(summary["summary_path"]).exists()
