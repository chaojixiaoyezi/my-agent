"""Focused tests for the real-e2e CLI command."""

from __future__ import annotations

import argparse
import json


def test_cmd_real_e2e_writes_report_and_json_output(tmp_path, capsys):
    from agent_py_agent.cli.real_e2e_commands import cmd_real_e2e

    report_path = tmp_path / "report.json"
    args = argparse.Namespace(
        workspace=str(tmp_path / "workspace"),
        report=str(report_path),
        json=True,
        include_real_model=False,
        artifact=[],
    )

    exit_code = cmd_real_e2e(args)

    assert exit_code == 0
    saved = json.loads(report_path.read_text(encoding="utf-8"))
    printed = json.loads(capsys.readouterr().out)
    assert saved["ok"] is True
    assert saved["summary"]["total"] == 12
    assert printed["report_ref"] == str(report_path)
    assert printed["foundation"]["summary"]["failed"] == 0


def test_cmd_real_e2e_includes_artifact_acceptance_findings(tmp_path, capsys):
    from agent_py_agent.cli.real_e2e_commands import cmd_real_e2e

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = workspace / "index.html"
    artifact.write_text(
        '<!doctype html><html><head><link rel="stylesheet" href="https://fonts.example/font.css"></head><body><main>Bad</main>',
        encoding="utf-8",
    )
    args = argparse.Namespace(
        workspace=str(workspace),
        report="",
        json=True,
        include_real_model=False,
        artifact=[str(artifact)],
    )

    exit_code = cmd_real_e2e(args)

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["ok"] is False
    codes = {item["code"] for item in payload["artifact_acceptance"][0]["findings"]}
    assert "HTML_INCOMPLETE_DOCUMENT" in codes


def test_cmd_real_e2e_requested_real_model_cannot_pass_as_skipped(tmp_path, capsys):
    from agent_py_agent.cli.real_e2e_commands import cmd_real_e2e

    args = argparse.Namespace(
        workspace=str(tmp_path / "workspace"),
        report="",
        json=True,
        include_real_model=True,
        artifact=[],
    )

    exit_code = cmd_real_e2e(args)

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["ok"] is False
    assert payload["summary"]["failed"] >= 1
