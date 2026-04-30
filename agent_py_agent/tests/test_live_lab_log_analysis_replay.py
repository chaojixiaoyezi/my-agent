from __future__ import annotations

from pathlib import Path

from scripts.live_lab.log_analysis_replay import run_security_alert_v1_replay


def test_security_alert_v1_live_lab_replay_success(tmp_path):
    summary = run_security_alert_v1_replay(output_root=tmp_path / "replay")

    assert summary["ok"] is True
    assert summary["failed_stage"] is None
    assert summary["stored_events"] == 3
    assert summary["total_events"] == 3
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
    assert summary["stored_events"] == 0
    assert Path(summary["summary_path"]).exists()
