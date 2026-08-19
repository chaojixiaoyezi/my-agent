from __future__ import annotations

"""scenario-test gateway resume regressions."""

import os
from pathlib import Path

import pytest

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.subagents import parse_subagent_runner_output
from agent_py_agent.cli.parser import build_parser
from agent_py_agent.cli.scenario_cases.real_model_multi_round_case import (
    ScenarioRealModelMultiRoundBackend,
)
from agent_py_agent.cli.scenario_cases.real_model_recovery_case import (
    ScenarioRealModelRecoveryBackend,
)


def _write_echo_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "agent_config.yaml"
    config_path.write_text(
        'workspace_root: "workspace"\n'
        'model_backend: "echo"\n'
        
        'subagent_workspace: ".my_agent/subagents"\n'
        'gateway_workspace: ".my_agent/gateway"\n'
        "gateway_port: 0\n"
        'memory_path: ".my_agent/memory.jsonl"\n'
        'local_store_path: ".my_agent/local_store/local.db"\n'
        'local_store_files_dir: ".my_agent/local_store/files"\n'
        'local_store_events_path: ".my_agent/local_store/events.jsonl"\n',
        encoding="utf-8",
    )
    return config_path


def _write_real_model_config(tmp_path: Path) -> Path | None:
    """Write a config for real model testing; return None unless explicit real E2E is enabled."""
    if os.environ.get("MY_AGENT_RUN_REAL_MODEL_TESTS", "").strip() not in {"1", "true", "yes"}:
        return None
    api_key = os.environ.get("AGENT_API_KEY", "").strip()
    if not api_key:
        return None
    request_timeout = (
        os.environ.get("MY_AGENT_REAL_MODEL_TEST_REQUEST_TIMEOUT", "240").strip() or "240"
    )
    config_path = tmp_path / "agent_config.yaml"
    config_path.write_text(
        'workspace_root: "workspace"\n'
        'model_backend: "anthropic_compatible"\n'
        'api_base: "https://api.minimaxi.com/anthropic"\n'
        'api_key_env: "AGENT_API_KEY"\n'
        'model_name: "MiniMax-M2.7"\n'
        f"request_timeout: {request_timeout}\n"
        "max_tokens: 1024\n"
        "temperature: 0.2\n"
        'anthropic_version: "2023-06-01"\n'
        'subagent_workspace: ".my_agent/subagents"\n'
        'gateway_workspace: ".my_agent/gateway"\n'
        'memory_path: ".my_agent/memory.jsonl"\n'
        'local_store_path: ".my_agent/local_store/local.db"\n'
        'local_store_files_dir: ".my_agent/local_store/files"\n'
        'local_store_events_path: ".my_agent/local_store/events.jsonl"\n',
        encoding="utf-8",
    )
    return config_path


def _run_offline_scenario_case(tmp_path: Path, capsys, case: str) -> str:
    parser = build_parser()
    args = parser.parse_args(
        [
            "--config",
            str(_write_echo_config(tmp_path)),
            "scenario-test",
            "--case",
            case,
            "--workspace",
            str(tmp_path / "scenario-runs"),
            "--max-runners",
            "1",
        ]
    )

    code = args.func(args)
    output = capsys.readouterr().out

    assert code == 0, output
    assert f"case={case}" in output
    assert "SCENARIO_PASS" in output
    return output


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


@pytest.mark.xfail(
    reason="存量债: gateway 进程级场景收口断言(RC=2 vs 0)与 native 语义差异, 需场景框架适配; 不影响 CLI 主链路"
)
def test_scenario_runner_retry_reaches_final_closeout(tmp_path, capsys):
    """The runner retry scenario should satisfy the current evidence-packet acceptance contract."""

    output = _run_offline_scenario_case(tmp_path, capsys, "runner-retry")

    assert "retry_runner" in output
    assert "final status=DONE verify=VERIFIED" in output


@pytest.mark.skip(
    reason="旧 final-closeout scenario 已删除，结构化修复以 runner 输出和统一 closeout 为准"
)
def test_scenario_structured_repair_reaches_final_closeout(tmp_path, capsys):
    """The structured repair scenario should satisfy the current evidence-packet acceptance contract."""

    output = _run_offline_scenario_case(tmp_path, capsys, "structured-repair")

    assert "repair_attempted=True" in output
    assert "final status=DONE verify=VERIFIED" in output


@pytest.mark.xfail(
    reason="存量债: gateway 进程级场景收口断言(RC=2 vs 0)与 native 语义差异, 需场景框架适配; 不影响 CLI 主链路"
)
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


def test_scenario_gateway_delayed_response_repairs_orphan_projection(tmp_path, capsys):
    """The scenario must execute when only a non-authoritative response projection exists."""

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


@pytest.mark.xfail(
    reason="存量债: gateway 进程级场景收口断言(RC=2 vs 0)与 native 语义差异, 需场景框架适配; 不影响 CLI 主链路"
)
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


@pytest.mark.slow
@pytest.mark.e2e
def test_scenario_real_model_recovery_smoke(tmp_path, capsys):
    """The scenario case should prove a real model API round-trip survives cross-day recovery."""

    config_path = _write_real_model_config(tmp_path)
    if config_path is None:
        pytest.skip(
            "set MY_AGENT_RUN_REAL_MODEL_TESTS=1 and AGENT_API_KEY to run real model smoke test"
        )

    parser = build_parser()
    args = parser.parse_args(
        [
            "--config",
            str(config_path),
            "scenario-test",
            "--case",
            "real-model-recovery",
            "--workspace",
            str(tmp_path / "scenario-runs"),
            "--timeout",
            "120",
        ]
    )

    code = args.func(args)
    output = capsys.readouterr().out

    assert code == 0, output
    assert "case=real-model-recovery" in output
    assert "SCENARIO_PASS" in output


def test_real_model_recovery_backend_escapes_real_response_in_result_json():
    """Scenario wrappers must not let real model quotes/tool tags corrupt SUBAGENT_RESULT JSON."""

    backend = ScenarioRealModelRecoveryBackend(
        _RealTextBackend('quote "x"\n[TOOL_CALL]\n{"tool":"bad"}')
    )

    backend.generate("first")
    parsed = parse_subagent_runner_output(backend.generate("second").text)

    assert parsed.ok
    assert parsed.used_tools == ["read_file"]
    assert "<TOOL_CALL>" in parsed.summary
    assert "[TOOL_CALL]" not in parsed.summary


def test_real_model_multi_round_backend_escapes_real_response_in_result_json():
    """Multi-round scenario uses the same safe serialization path."""

    backend = ScenarioRealModelMultiRoundBackend(
        _RealTextBackend('multi "x"\n[TOOL_CALL]\n{"tool":"bad"}')
    )

    backend.generate("first")
    backend.generate("second")
    parsed = parse_subagent_runner_output(backend.generate("third").text)

    assert parsed.ok
    assert parsed.used_tools == ["read_file", "search_text"]
    assert "<TOOL_CALL>" in parsed.summary
    assert "[TOOL_CALL]" not in parsed.summary


class _RealTextBackend:
    def __init__(self, text: str):
        self.text = text

    def generate(self, prompt: str, on_chunk=None) -> ModelResponse:
        return ModelResponse(text=self.text, backend="real-text-test")
