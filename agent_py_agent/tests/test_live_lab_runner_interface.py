from __future__ import annotations

from types import SimpleNamespace

from scripts.live_lab.runner import _LabInterface


# LLM: Live Lab cases still use the old lab surface; this test keeps the adapter honest.
# 函数用途: 复现真实 Live Lab gateway_ask 中 responses_dir 未转发导致真实模型 case 失败的问题。
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
