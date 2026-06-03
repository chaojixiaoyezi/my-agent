from __future__ import annotations

from types import SimpleNamespace

from scripts.live_lab.runner import _LabInterface


def test_lab_interface_exposes_case_workspace_dirs(tmp_path):
    session = SimpleNamespace(
        run_root=tmp_path / "run",
        fixture_root=tmp_path / "run" / "fixture_project",
        prompts_dir=tmp_path / "run" / "prompts",
        responses_dir=tmp_path / "run" / "responses",
        config_path=tmp_path / "run" / "live_agent_config.yaml",
        transcript_path=tmp_path / "run" / "TRANSCRIPT.md",
        summary_path=tmp_path / "run" / "live_lab_summary.json",
        stop_file=tmp_path / "run" / "STOP",
        gateway_wait_timeout=960,
    )
    runner = SimpleNamespace(
        _session=session,
        _reporter=SimpleNamespace(log=lambda message="": None, section=lambda title: None, log_output=lambda *_: None),
        args=SimpleNamespace(),
    )

    lab = _LabInterface(runner)

    assert lab.run_root == session.run_root
    assert lab.prompts_dir == session.prompts_dir
    assert lab.responses_dir == session.responses_dir
    assert lab.summary_path == session.summary_path
    assert lab.gateway_wait_timeout == 960
