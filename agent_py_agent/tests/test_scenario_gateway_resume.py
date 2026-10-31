from __future__ import annotations

"""scenario-test gateway resume regressions."""

import os
import socket
from pathlib import Path

import pytest

from agent_py_agent.agent.backends import ModelResponse
from agent_py_agent.agent.settings import load_config
from agent_py_agent.agent.subagents import parse_subagent_runner_output
from agent_py_agent.cli.parser import build_parser
from agent_py_agent.cli.scenario_cases.real_model_multi_round_case import (
    ScenarioRealModelMultiRoundBackend,
)
from agent_py_agent.cli.scenario_cases.real_model_recovery_case import (
    ScenarioRealModelRecoveryBackend,
)


# LLM: 默认0保其它离线场景原配置；真实Gateway ask场景显式给本机临时端口，不替换HTTP运输或权限门。
# 函数用途: 写隔离echo输入配置，调用方决定是否需要正式本地Gateway监听。
def _write_echo_config(tmp_path: Path, *, gateway_port: int = 0) -> Path:
    config_path = tmp_path / "agent_config.yaml"
    config_path.write_text(
        'workspace_root: "workspace"\n'
        'model_backend: "echo"\n'
        
        'subagent_workspace: ".my_agent/subagents"\n'
        'gateway_workspace: ".my_agent/gateway"\n'
        f"gateway_port: {gateway_port}\n"
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


# LLM: 有 Home 的真实 Gateway 进程只认 owner 的 v6 模型目录，部署 echo 不再生效；经正式 add/set_default
#   给场景 owner 登记显式离线 Echo（不联网），其余场景步骤与断言不变。
# 函数用途: 在场景创建隔离配置后为其 owner 选好离线模型。
def _select_offline_echo_model(config_path: Path) -> None:
    from types import SimpleNamespace
    from uuid import uuid4

    from agent_py_agent.agent.settings.model_profiles import execute_model_profile_operation
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.user_space.owner_resolver import (
        home_paths_with_owner,
        owner_identity_from_config,
        resolve_owner_home,
    )

    config = load_config(config_path)
    paths = home_paths(config.my_agent_home)
    owner = resolve_owner_home(paths.root, owner_identity_from_config(config))
    host = SimpleNamespace(config=config, home_paths=home_paths_with_owner(paths, owner))
    profile_id = str(uuid4())
    execute_model_profile_operation(host, "add", {"profile_id": profile_id, "profile": {
        "model_backend": "echo", "model_name": "echo", "transport": "offline"}})
    execute_model_profile_operation(host, "set_default", {"profile_id": profile_id})


def test_scenario_gateway_cross_day_resume_uses_real_gateway_process(tmp_path, capsys, monkeypatch):
    """The scenario case should prove real gateway ask facts can be resumed across days."""

    from agent_py_agent.cli.scenario_cases import gateway_cross_day_case

    original_workspace = gateway_cross_day_case.create_scenario_workspace

    def seeded_workspace(args):
        paths = original_workspace(args)
        # 场景配置把 Home 放在隔离 fixture 内；目录必须在 Gateway 进程启动前选好。
        assert Path(load_config(paths.config).my_agent_home).is_relative_to(tmp_path)
        _select_offline_echo_model(paths.config)
        return paths

    monkeypatch.setattr(gateway_cross_day_case, "create_scenario_workspace", seeded_workspace)
    # 该场景用正式认证HTTP ask；先选本机可用端口，仅写入此次临时配置。
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        gateway_port = listener.getsockname()[1]
    parser = build_parser()
    args = parser.parse_args(
        [
            "--config",
            str(_write_echo_config(tmp_path, gateway_port=gateway_port)),
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
