from __future__ import annotations

from types import SimpleNamespace

from agent_py_agent.cli import shared_progress


def test_takeover_view_reports_bad_readiness_packet(tmp_path):
    readiness = tmp_path / "takeover_readiness.json"
    readiness.write_text("{bad json", encoding="utf-8")
    run = SimpleNamespace(
        run_id="child-1",
        status="BLOCKED",
        current_step="等待接管",
        latest_summary="接管包坏了也要显示诊断。",
        metadata={
            "takeover_readiness_ref": str(readiness),
            "failure_handoff_ref": str(tmp_path / "failure.md"),
        },
    )
    panel = shared_progress._panel_payload(SimpleNamespace(runs=[run], blocked_runs=[run], context=None))

    lines = shared_progress.format_takeover_view_lines([panel])

    text = "\n".join(lines)
    assert "takeover_readiness_load_error=cli.shared_progress.takeover_readiness.read" in text
    assert "read_order[0]=" in text
