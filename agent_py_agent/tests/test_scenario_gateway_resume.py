from __future__ import annotations

"""scenario-test gateway resume regressions."""

from pathlib import Path

from agent_py_agent.__main__ import build_parser


def _write_echo_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "agent_config.yaml"
    config_path.write_text(
        'workspace_root: "workspace"\n'
        'model_backend: "echo"\n'
        'subagent_workspace: ".my_agent/subagents"\n'
        'gateway_workspace: ".my_agent/gateway"\n'
        'memory_path: ".my_agent/memory.jsonl"\n'
        'local_store_path: ".my_agent/local_store/local.db"\n'
        'local_store_files_dir: ".my_agent/local_store/files"\n'
        'local_store_events_path: ".my_agent/local_store/events.jsonl"\n',
        encoding="utf-8",
    )
    return config_path


def test_scenario_gateway_cross_day_resume_uses_real_gateway_process(tmp_path, capsys):
    """The scenario case should prove real gateway ask facts can be resumed across days."""

    parser = build_parser()
    args = parser.parse_args(
        [
            "--config",
            str(_write_echo_config(tmp_path)),
            "scenario-test",
            "--case",
            "gateway-cross-day-resume",
            "--workspace",
            str(tmp_path / "scenario-runs"),
            "--timeout",
            "30",
        ]
    )

    code = args.func(args)
    output = capsys.readouterr().out

    assert code == 0, output
    assert "case=gateway-cross-day-resume" in output
    assert "SCENARIO_PASS" in output


def test_scenario_gateway_stale_lease_requeues_and_completes(tmp_path, capsys):
    """The scenario case should recover an interrupted processing lease and complete the request."""

    parser = build_parser()
    args = parser.parse_args(
        [
            "--config",
            str(_write_echo_config(tmp_path)),
            "scenario-test",
            "--case",
            "gateway-stale-lease",
            "--workspace",
            str(tmp_path / "scenario-runs"),
        ]
    )

    code = args.func(args)
    output = capsys.readouterr().out

    assert code == 0, output
    assert "case=gateway-stale-lease" in output
    assert "SCENARIO_PASS" in output


def test_scenario_gateway_multi_worker_processes_each_request_once(tmp_path, capsys):
    """The scenario case should prove concurrent gateway workers do not duplicate queue claims."""

    parser = build_parser()
    args = parser.parse_args(
        [
            "--config",
            str(_write_echo_config(tmp_path)),
            "scenario-test",
            "--case",
            "gateway-multi-worker",
            "--workspace",
            str(tmp_path / "scenario-runs"),
            "--count",
            "4",
        ]
    )

    code = args.func(args)
    output = capsys.readouterr().out

    assert code == 0, output
    assert "case=gateway-multi-worker" in output
    assert "SCENARIO_PASS" in output


def test_scenario_gateway_delayed_response_archives_duplicate_without_rerun(tmp_path, capsys):
    """The scenario case should archive a duplicate request when its response already exists."""

    parser = build_parser()
    args = parser.parse_args(
        [
            "--config",
            str(_write_echo_config(tmp_path)),
            "scenario-test",
            "--case",
            "gateway-delayed-response",
            "--workspace",
            str(tmp_path / "scenario-runs"),
        ]
    )

    code = args.func(args)
    output = capsys.readouterr().out

    assert code == 0, output
    assert "case=gateway-delayed-response" in output
    assert "SCENARIO_PASS" in output


def test_scenario_parent_subagent_cross_day_resume_uses_runner_task_facts(tmp_path, capsys):
    """The scenario case should recover a real subagent runner result from task fact sources."""

    parser = build_parser()
    args = parser.parse_args(
        [
            "--config",
            str(_write_echo_config(tmp_path)),
            "scenario-test",
            "--case",
            "parent-subagent-cross-day-resume",
            "--workspace",
            str(tmp_path / "scenario-runs"),
        ]
    )

    code = args.func(args)
    output = capsys.readouterr().out

    assert code == 0, output
    assert "case=parent-subagent-cross-day-resume" in output
    assert "SCENARIO_PASS" in output
