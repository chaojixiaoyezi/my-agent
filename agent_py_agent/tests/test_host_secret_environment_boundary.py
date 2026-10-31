"""只用合成凭据验证真实配置来源、宿主克隆和全部命令启动入口的环境隔离。"""

from __future__ import annotations

import json
import os
import shlex
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.attempt.sandbox import AttemptExecutionSandbox, AttemptSandboxSpec
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.owner_scoped_pool import _config_with_owner
from agent_py_agent.agent.plugin_environment_process import preparation_environment
from agent_py_agent.agent.settings.config import AgentConfig, load_config
from agent_py_agent.agent.settings.config_sources import public_config_keys
from agent_py_agent.agent.settings.model_profiles import (
    execute_model_profile_operation,
    selected_model_config,
)
from agent_py_agent.agent.settings.parameter_registry import parameter_registry
from agent_py_agent.agent.settings.secret_ref import (
    freeze_managed_secret_environment,
    managed_secret_environment_names,
    resolve_secret_ref,
)
from agent_py_agent.agent.settings.services.runtime_config_env import (
    apply_runtime_config_environment,
)
from agent_py_agent.agent.settings.services.runtime_config_task import (
    project_task_runtime_config_overlay,
)
from agent_py_agent.agent.subagents.shell_gateway import ShellGatewayRequest
from agent_py_agent.agent.subagents.shell_gateway_execution import execute_shell_command
from agent_py_agent.agent.tooling import shell
from agent_py_agent.agent.tooling.process_registry import process_registry
from agent_py_agent.agent.tooling.pty_sessions import TerminalSessionTool, pty_session_registry
from agent_py_agent.agent.tooling.shell import ShellTool, ShellToolOptions
from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
from agent_py_agent.tests._tool_runtime_harness import (
    execute_approved_registry_test_call,
    execute_registry_test_call,
)


def test_secret_sources_use_exact_reference_and_loader_metadata_without_values(monkeypatch):
    monkeypatch.setenv("CHANNEL_SOURCE", "synthetic-marker")
    config = AgentConfig(api_key_env="MODEL_SOURCE", feishu_app_secret=" env: CHANNEL_SOURCE ",
                         qq_app_secret="file:/synthetic-only/path", host_credential_env_names=("OLDER_SOURCE",),
                         config_sources={"api_key": {"source": "env:RESOLVED_SOURCE", "priority": 80}})
    assert resolve_secret_ref(config.feishu_app_secret) == "synthetic-marker"
    assert managed_secret_environment_names(config) == {"MODEL_SOURCE", "CHANNEL_SOURCE", "OLDER_SOURCE", "RESOLVED_SOURCE"}
    frozen = freeze_managed_secret_environment(config)
    assert "synthetic-marker" not in frozen.host_credential_env_names
    assert "OLDER_SOURCE" not in repr(frozen)
    assert config.host_credential_env_names == ("OLDER_SOURCE",)
    assert json.loads(json.dumps(asdict(frozen)))["host_credential_env_names"] == list(frozen.host_credential_env_names)
    stripped = replace(frozen, feishu_app_secret="", api_key_env="", config_sources={})
    assert managed_secret_environment_names(stripped) == set(frozen.host_credential_env_names)


def test_plain_prompts_and_unrelated_source_records_never_control_environment_filtering():
    config = AgentConfig(api_key_env="", system_prompt="env:HOME",
                         model_custom_headers={"context": "env:XDG_CACHE_HOME"},
                         config_sources={"system_prompt": {"source": "env:USER", "priority": 80}})
    assert managed_secret_environment_names(config) == frozenset()
    env = shell._subprocess_text_env(host_credential_env_names=managed_secret_environment_names(config))
    for name in ("HOME", "PATH", "USER"):
        if name in os.environ:
            assert env[name] == os.environ[name]


def test_user_yaml_and_parameter_directory_cannot_set_or_show_host_source_names(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("model_backend: echo\nhost_credential_env_names: [FORGED_SOURCE]\n", encoding="utf-8")
    config = load_config(path)
    assert config.host_credential_env_names == ()
    assert "host_credential_env_names" not in public_config_keys(AgentConfig)
    assert "host_credential_env_names" not in parameter_registry()
    assert any("host_credential_env_names" in warning for warning in config.config_warnings)


def test_mcp_declared_source_filters_default_baseline_but_preserves_native_explicit_env(monkeypatch):
    from agent_py_agent.agent.tooling.mcp_client import MCPServerConfig, build_safe_env

    monkeypatch.setenv("XDG_SOURCE", "synthetic-marker")
    monkeypatch.setenv("XDG_CACHE_HOME", "/synthetic-cache")
    names = managed_secret_environment_names(AgentConfig(feishu_app_secret="env:XDG_SOURCE"))
    default = build_safe_env({}, host_credential_env_names=names)
    assert "XDG_SOURCE" not in default and default["XDG_CACHE_HOME"] == "/synthetic-cache"
    explicit = build_safe_env({"XDG_SOURCE": "explicit-native-value"}, host_credential_env_names=names)
    assert explicit["XDG_SOURCE"] == "explicit-native-value"
    raw = MCPServerConfig.from_mapping("synthetic", {"command": sys.executable, "host_credential_env_names": ["FORGED"]})
    assert raw.host_credential_env_names == frozenset()


@pytest.mark.parametrize("explicit", [False, True])
def test_real_root_mcp_handshake_uses_frozen_source_and_retains_native_env(tmp_path, monkeypatch, explicit):
    from agent_py_agent.tests.test_mcp_client import _ENV_ECHO_SERVER

    monkeypatch.setenv("XDG_SOURCE", "synthetic-marker")
    script = _ENV_ECHO_SERVER.replace("json.dumps(os.environ.get(key))", "json.dumps(key in os.environ)")
    config = AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"), feishu_app_secret="env:XDG_SOURCE",
                         mcp_servers={"source-probe": {"command": sys.executable, "args": ["-c", script],
                                                      "env": {"XDG_SOURCE": "native-value"} if explicit else {},
                                                      "host_credential_env_names": ["FORGED"]}})
    agent = SimpleAgent(config, tmp_path / "source")
    try:
        assert len(agent.tools._mcp_clients) == 1
        client = agent.tools._mcp_clients[0]
        assert "XDG_SOURCE" in client.config.host_credential_env_names and "FORGED" not in client.config.host_credential_env_names
        assert json.loads(client.call_tool("getenv", {"key": "XDG_SOURCE"})["content"]) is explicit
    finally:
        agent.tools.close_mcp_clients()


def test_runtime_and_child_overlays_cannot_drop_host_upper_bound_or_inject_internal_field(tmp_path):
    base = AgentConfig(api_key_env="MODEL_SOURCE", feishu_app_secret="env:CHANNEL_SOURCE")
    overlay = tmp_path / "runtime.yaml"
    overlay.write_text('api_key_env: ""\nfeishu_app_secret: ""\nhost_credential_env_names: [FORGED_SOURCE]\n', encoding="utf-8")
    runtime = apply_runtime_config_environment(base, env={"MY_AGENT_RUNTIME_CONFIG": str(overlay)})
    assert {"MODEL_SOURCE", "CHANNEL_SOURCE"} <= set(runtime.host_credential_env_names)
    assert "FORGED_SOURCE" not in runtime.host_credential_env_names
    task = SimpleNamespace(runtime_identity=SimpleNamespace(config_overlay_ref=str(overlay), config_scope="run"))
    child = project_task_runtime_config_overlay(runtime, task, workspace_root=tmp_path)
    grandchild = project_task_runtime_config_overlay(child, task, workspace_root=tmp_path)
    assert grandchild.host_credential_env_names == child.host_credential_env_names == runtime.host_credential_env_names
    assert any("host_credential_env_names" in warning for warning in child.config_warnings)


def test_real_core_owner_and_model_profile_keep_host_source_names(tmp_path):
    base = AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"),
                       api_key_env="MODEL_SOURCE", feishu_app_secret="env:CHANNEL_SOURCE")
    owner_config = _config_with_owner(base, OwnerIdentity.provider_user("feishu", "synthetic-user"))
    agent = SimpleAgent(owner_config, tmp_path / "source-root")
    tool = agent.tools.tools["run_command"]
    assert tool.host_credential_env_names == {"MODEL_SOURCE", "CHANNEL_SOURCE"}
    assert agent.tools.tools["terminal_session"].shell_tool is tool
    key = "af7e907f-5d9b-4d38-8587-5c6aeb2e6f01"
    execute_model_profile_operation(agent, "add", {"profile_id": key, "profile": {
        "model_backend": "openai_compatible", "model_name": "synthetic-model", "api_base": "https://example.test/v1",
        "api_key": "synthetic-profile-value", "model_context_window_tokens": 96000,
        "host_credential_env_names": ["FORGED_SOURCE"],
    }})
    selected = selected_model_config(agent, profile_id=key)
    assert selected.api_key_env == "" and selected.host_credential_env_names == agent.config.host_credential_env_names
    assert "FORGED_SOURCE" not in selected.host_credential_env_names
    assert "host_credential_env_names" not in tool.model_spec.input_schema["properties"]
    rejected = execute_registry_test_call(agent.tools, "run_command", {
        "command": "echo should-not-start", "host_credential_env_names": [],
    })
    assert not rejected.ok and not rejected.handler_executed and rejected.error_code == "TOOL_INVALID_ARGUMENTS"


@pytest.mark.skipif(os.name == "nt", reason="真实 POSIX 原批处理进程；Windows 基线擦洗共用纯函数")
@pytest.mark.parametrize("mode", ["controlled", "attempt", "plugin_preparation"])
def test_secondary_real_spawn_entries_preserve_host_source_upper_bound(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("CHANNEL_SOURCE", "synthetic-marker")
    monkeypatch.setenv("ORDINARY_VALUE", "keep")
    names = frozenset({"CHANNEL_SOURCE"})
    argv = [sys.executable, "-c", 'import os,json; print(json.dumps({"secret_present":"CHANNEL_SOURCE" in os.environ,"ordinary":os.environ.get("ORDINARY_VALUE")}))']
    if mode == "controlled":
        request = ShellGatewayRequest(argv, tmp_path, allowed_roots=[tmp_path], command_allowlist=[sys.executable], dry_run=False)
        result = execute_shell_command(request, host_credential_env_names=names)
        assert result.executed and result.exit_code == 0
        output = result.stdout_preview
    elif mode == "attempt":
        sandbox = AttemptExecutionSandbox(AttemptSandboxSpec(tmp_path, tmp_path, tmp_path, tmp_path,
                                                            host_credential_env_names=names))
        # 隔开平台包装以定位 env 参数传递；原 readiness/沙箱权限由 Linux 实际 bwrap 矩阵另验。
        monkeypatch.setattr(sandbox, "require_ready", lambda: None)
        monkeypatch.setattr(sandbox, "build_argv", lambda args: args)
        output = sandbox.run(argv, timeout=5).stdout
    else:
        import subprocess

        output = subprocess.run(argv, check=True, capture_output=True, text=True,
                                env=preparation_environment(tmp_path, host_credential_env_names=names)).stdout
    assert json.loads(output) == {"secret_present": False, "ordinary": "keep"}
    assert os.environ["CHANNEL_SOURCE"] == "synthetic-marker"


def test_controlled_registry_uses_host_source_without_grant_or_argument_overrides(tmp_path, monkeypatch):
    from agent_py_agent.tests.test_subagent_controlled_exec_tool import _controlled_exec_boundary

    monkeypatch.setenv("CHANNEL_SOURCE", "synthetic-marker")
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"),
                                    feishu_app_secret="env:CHANNEL_SOURCE"), tmp_path / "source")
    workspace = agent.effective_workspace_root
    boundary = _controlled_exec_boundary(workspace)
    boundary["controlled_exec_grants"][0]["command_allowlist"] = [sys.executable]
    result = execute_approved_registry_test_call(agent.tools, "controlled_exec", {
        "grant_id": "grant-shell-1", "apply": True, "cwd": str(workspace),
        "command": [sys.executable, "-c", 'import os; print("visible" if "CHANNEL_SOURCE" in os.environ else "hidden")'],
    }, write_boundary=boundary, allowed_tools=["controlled_exec"], register_with=agent)
    assert result.ok, result.output
    payload = json.loads(result.output)
    assert payload["execution"]["stdout_preview"].strip() == "hidden"
    assert "host_credential_env_names" not in payload


@pytest.mark.skipif(os.name == "nt", reason="真实 POSIX 子进程和 PTY；Windows spawn 环境由同入口覆盖")
@pytest.mark.parametrize("mode", ["foreground", "background", "pty"])
def test_real_spawn_uses_declared_source_scrub_without_rewriting_host_environment(tmp_path, monkeypatch, mode):
    monkeypatch.setenv("CHANNEL_SOURCE", "synthetic-marker")
    monkeypatch.setenv("ORDINARY_VALUE", "keep")
    # 环境传递回归隔开平台包装；最终 Linux 验收另用原 bwrap，不用此 seam 冒充沙箱证据。
    monkeypatch.setattr(shell, "_sandbox_exec", lambda command, *a, **k: (["/bin/sh", "-c", command], False))
    monkeypatch.setattr("agent_py_agent.agent.tooling.pty_sessions._sandbox_exec", shell._sandbox_exec)
    # 新后台启动须有规范 owner home（5c029e4fb），不能从 cwd 补造；三种模式共用同一宿主 owner 范围。
    tool = ShellTool(tmp_path, options=ShellToolOptions(host_credential_env_names=frozenset({"CHANNEL_SOURCE"}),
                                                        owner_scope_root=str(tmp_path)))
    script = 'import os,json; print(json.dumps({"secret_present":"CHANNEL_SOURCE" in os.environ,"ordinary":os.environ.get("ORDINARY_VALUE")}))'
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
    try:
        if mode == "foreground":
            result = tool.execute({"command": command})
            assert result.ok, result.output
            output = result.output
        elif mode == "background":
            result = tool.execute({"command": command, "run_in_background": True})
            assert result.ok, result.output
            path = Path(json.loads(result.output)["output_file"])
            output = _wait_output(lambda: path.read_text(encoding="utf-8"))
        else:
            terminal = TerminalSessionTool(tool)
            result = terminal.execute({"action": "start", "command": command})
            assert result.ok, result.output
            session_id = json.loads(result.output)["session_id"]
            output = _wait_output(lambda: json.loads(terminal.execute({"action": "read", "session_id": session_id, "cursor": 0}).output)["output"])
        assert '"secret_present": false' in output and '"ordinary": "keep"' in output
        assert os.environ["CHANNEL_SOURCE"] == "synthetic-marker"
    finally:
        process_registry.clear()
        pty_session_registry.clear()


# LLM: 只轮询这次合成进程的输出，固定五秒上界；不读宿主凭据或业务产物。
# 函数用途: 等真实后台或 PTY 子进程写出测试布尔值，失败保留最后输出。
def _wait_output(read):
    deadline = time.monotonic() + 5
    output = ""
    while time.monotonic() < deadline:
        output = read()
        if '"secret_present"' in output:
            break
        time.sleep(0.02)
    return output
