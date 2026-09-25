"""插件进程 OS 沙箱试点（plugin_process_sandbox）。

覆盖：配置默认关闭、bwrap 整根只读参数布局、客户端包装与 TMPDIR、沙箱不可用时启用与显式调用都结构化拒绝，
以及真实平台沙箱（macOS Seatbelt / Linux bwrap，本机不可用时跳过）下"能读、能写数据目录、不能写别处"。
全部写入都在 tmp_path 的临时 owner 里。
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent import plugin_enable_tool, plugin_management, plugin_runtime
from agent_py_agent.agent.plugin_management import PluginManagement
from agent_py_agent.agent.plugin_runtime import PluginMCPClient, plugin_data_dir, plugin_tool_name
from agent_py_agent.agent.plugin_sandbox import SANDBOX_TMP_DIRECTORY, plugin_sandbox_problem
from agent_py_agent.agent.settings import load_config
from agent_py_agent.agent.settings.config import AgentConfig, normalize_agent_config
from agent_py_agent.agent.tooling.sandbox import SandboxSpec, build_bwrap_argv
from agent_py_agent.tests.plugin_activation_fixtures import invoke_registered_tool, plugin_registry
from agent_py_agent.tests.test_plugin_any_language import (
    _TOOL,
    PLUGIN_ID,
    _declaration,
    _enable,
    _package,
)
from agent_py_agent.tests.test_plugin_management import manager
from scripts.build_plugin_files_package import build_files_package

_WRITE_TOOL = {"name": "write", "description": "写文件（沙箱测试用）", "requested_effect": "read_only",
               "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "text": {"type": "string"}},
                                "required": ["path", "text"]}}
_WRITER = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
tools = __TOOLS__
for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    method = request["method"]
    if method == "initialize":
        result = {"protocolVersion": request["params"]["protocolVersion"], "capabilities": {"tools": {}},
                  "serverInfo": {"name": "sandbox-fixture", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": tools}
    elif method == "tools/call":
        arguments = request["params"]["arguments"]
        try:
            if request["params"]["name"] == "write":
                Path(arguments["path"]).write_text(arguments["text"])
                text = "written tmp=" + os.environ.get("TMPDIR", "")
            else:
                text = "read:" + Path(arguments["path"]).read_text()
        except OSError as exc:
            text = f"os-error:{exc.errno}"
        result = {"content": [{"type": "text", "text": text}]}
    else:
        raise RuntimeError("unsupported fixture method")
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
'''.replace("__TOOLS__", repr([{"name": tool["name"], "description": tool["description"], "inputSchema": tool["input_schema"]}
                               for tool in (_TOOL, _WRITE_TOOL)])).encode()


# 函数用途: 在临时 owner 里安装并带码启用写读两用的测试插件；process_sandbox 决定管理上下文的沙箱开关。
def _enabled_writer(tmp_path, *, process_sandbox: bool):
    service, _ = manager(tmp_path, process_sandbox=process_sandbox)
    source = _package(tmp_path, _declaration(tools=[_TOOL, _WRITE_TOOL]), {"bin/server.py": _WRITER})
    installed = service.command(f'/plugins install "{source}"', revision=service.catalog().revision, request_id="install")
    assert installed["state"] == "succeeded", installed
    first = _enable(service, "enable")
    assert first["details"]["reason"] == "confirmation_required", first
    enabled = _enable(service, "confirmed", first["details"]["confirmation"]["confirm_code"])
    assert enabled["state"] == "succeeded", enabled
    return service


def test_switch_defaults_off_in_yaml_dataclass_and_normalizer():
    shipped = load_config(Path(__file__).parents[1] / "config" / "agent_config.yaml")
    normalized, warnings = normalize_agent_config({"plugin_process_sandbox": "false"})
    assert AgentConfig().plugin_process_sandbox is False and shipped.plugin_process_sandbox is False
    assert normalized["plugin_process_sandbox"] is False and warnings == []


def test_read_only_root_layout_keeps_reads_and_binds_only_write_roots(tmp_path):
    data, cwd = tmp_path / "data", tmp_path / "cwd"
    data.mkdir()
    cwd.mkdir()
    argv = build_bwrap_argv(SandboxSpec(owner_home=tmp_path, workspace=cwd, write_roots=(data,),
                                        bwrap_path="/usr/bin/bwrap", read_only_root=True))
    joined = " ".join(argv)
    assert argv[0] == "/usr/bin/bwrap" and argv[-2:] == ["--chdir", str(cwd)]
    assert "--ro-bind / /" in joined and "--bind / /" not in joined.replace("--ro-bind / /", "")
    assert joined.index("--ro-bind / /") < joined.index("--dev /dev") < joined.index(f"--bind {data.resolve()} {data.resolve()}")
    assert "--tmpfs" not in joined and "--die-with-parent" in argv and "--share-net" in argv
    plain = build_bwrap_argv(SandboxSpec(owner_home=tmp_path, workspace=cwd, write_roots=(data,), bwrap_path="/usr/bin/bwrap"))
    assert "--ro-bind / /" not in " ".join(plain)


def test_client_wraps_launch_and_points_tmpdir_into_data_dir(tmp_path, monkeypatch):
    service = _enabled_writer(tmp_path, process_sandbox=False)
    owner, entry = service.context.owner, service.installations.snapshot()[0]
    calls = []
    monkeypatch.setattr(plugin_runtime, "sandboxed_plugin_argv",
                        lambda argv, **kwargs: calls.append(kwargs) or ["fake-sandbox", "--", *argv])
    plain = PluginMCPClient(owner, entry)
    assert Path(plain.config.command).name == "server.py" and "TMPDIR" not in plain.config.env and not calls
    wrapped = PluginMCPClient(owner, entry, process_sandbox=True)
    data_dir = plugin_data_dir(owner, PLUGIN_ID)
    assert wrapped.config.command == "fake-sandbox" and wrapped.config.args[1] == plain.config.command
    assert wrapped.config.args[2:] == plain.config.args and wrapped.config.cwd == plain.config.cwd
    assert wrapped.config.env["TMPDIR"] == str(data_dir / SANDBOX_TMP_DIRECTORY)
    assert (data_dir / SANDBOX_TMP_DIRECTORY).is_dir()
    assert calls == [{"cwd": Path(plain.config.cwd), "data_dir": data_dir, "owner_home": owner.home_dir}]


def test_unavailable_sandbox_refuses_enable_and_explicit_calls(tmp_path, monkeypatch):
    unavailable = lambda enabled, home: "sandbox_unavailable" if enabled else ""  # noqa: E731
    monkeypatch.setattr(plugin_enable_tool, "plugin_sandbox_problem", unavailable)
    monkeypatch.setattr(plugin_management, "plugin_sandbox_problem", unavailable)
    service, _ = manager(tmp_path, process_sandbox=True)
    source = _package(tmp_path, _declaration(tools=[_TOOL, _WRITE_TOOL]), {"bin/server.py": _WRITER})
    assert service.command(f'/plugins install "{source}"', revision=service.catalog().revision,
                           request_id="install")["state"] == "succeeded"
    refused = _enable(service, "enable")
    assert refused["state"] == "failed" and refused["details"]["reason"] == "sandbox_unavailable", refused
    assert "沙箱不可用" in refused["message"]
    assert service.installations.snapshot()[0].activation is None
    environments = service.context.owner.plugins_dir / "environments"
    assert not environments.exists() or not any(environments.iterdir())
    relaxed = PluginManagement(replace(service.context, process_sandbox=False))
    code = _enable(relaxed, "enable-off")["details"]["confirmation"]["confirm_code"]
    assert _enable(relaxed, "confirmed-off", code)["state"] == "succeeded"
    (tmp_path / "input.txt").write_text("x")
    call = service.command(f'/plugins@{PLUGIN_ID} read "{tmp_path / "input.txt"}"', revision=service.catalog().revision,
                           request_id="call")
    assert call["state"] == "rejected" and call["error_code"] == "PLUGIN_RUNTIME_UNAVAILABLE", call
    assert call["details"] == {"reason": "sandbox_unavailable"}


# LLM: 真实平台沙箱：启用验收的候选进程与业务进程都在沙箱里启动；只验写边界与读放行，不验网络或进程隔离细节。
def test_real_platform_sandbox_blocks_writes_outside_the_data_dir(tmp_path):
    if plugin_sandbox_problem(True, tmp_path):
        pytest.skip("本机平台沙箱不可用（Linux 需要可用的 bwrap，macOS 需要 sandbox-exec）")
    service = _enabled_writer(tmp_path, process_sandbox=True)
    owner = service.context.owner
    data_dir = plugin_data_dir(owner, PLUGIN_ID)
    (tmp_path / "input.txt").write_text("工作区内容")
    registry = plugin_registry(service, plugin_process_sandbox=True)
    try:
        registry.prepare_for_run()
        write, read = plugin_tool_name(PLUGIN_ID, "write"), plugin_tool_name(PLUGIN_ID, "read")
        inside = invoke_registered_tool(service, registry, write, {"path": str(data_dir / "note.txt"), "text": "ok"},
                                        request_id="inside")
        assert f"written tmp={data_dir / SANDBOX_TMP_DIRECTORY}" in json.dumps(inside, ensure_ascii=False), inside
        assert (data_dir / "note.txt").read_text() == "ok"
        outside = invoke_registered_tool(service, registry, write, {"path": str(tmp_path / "victim.txt"), "text": "x"},
                                         request_id="outside")
        assert "os-error:" in json.dumps(outside, ensure_ascii=False), outside
        assert not (tmp_path / "victim.txt").exists()
        # 环境目录本身对用户可写（只有随包文件是只读），在里面新建文件只会被沙箱拦住
        files_dir = next(path for path in (owner.plugins_dir / "environments").rglob("server.py")).parent.parent
        assert stat.S_IMODE(files_dir.stat().st_mode) & stat.S_IWUSR
        tamper = invoke_registered_tool(service, registry, write, {"path": str(files_dir / "injected.txt"), "text": "x"},
                                        request_id="tamper")
        assert "os-error:" in json.dumps(tamper, ensure_ascii=False), tamper
        assert not (files_dir / "injected.txt").exists()
        readback = invoke_registered_tool(service, registry, read, {"path": str(tmp_path / "input.txt")}, request_id="read")
        assert "read:工作区内容" in json.dumps(readback, ensure_ascii=False), readback
    finally:
        registry.close_mcp_clients()
    assert os.path.exists(data_dir / "note.txt")


# LLM: 解释器类型在沙箱里也要能读到固定的解释器（例如 Homebrew Cellar 下的 node）、随包文件与按次授权的工作区；
#   停用后沙箱包装进程与内层解释器进程都不能残留。
@pytest.mark.skipif(shutil.which("node") is None, reason="本机没有 node")
def test_node_sample_runs_inside_the_platform_sandbox(tmp_path):
    if plugin_sandbox_problem(True, tmp_path):
        pytest.skip("本机平台沙箱不可用（Linux 需要可用的 bwrap，macOS 需要 sandbox-exec）")
    project = Path(__file__).parents[2] / "plugins" / "hello-node"
    declaration = json.loads((project / "declaration.json").read_text(encoding="utf-8"))
    service, _ = manager(tmp_path, process_sandbox=True)
    package = build_files_package(declaration, project, tmp_path / "hello-node.zip")
    assert service.command(f'/plugins install "{package}"', revision=service.catalog().revision,
                           request_id="install")["state"] == "succeeded"
    first = service.command("/plugins enable hello-node", revision=service.catalog().revision, request_id="enable")
    code = first["details"]["confirmation"]["confirm_code"]
    enabled = service.command(f"/plugins enable hello-node --confirm {code}", revision=service.catalog().revision,
                              request_id="confirmed")
    assert enabled["state"] == "succeeded", enabled
    (tmp_path / "notes.txt").write_text("沙箱里的笔记", encoding="utf-8")
    registry = plugin_registry(service, plugin_process_sandbox=True)
    try:
        registry.prepare_for_run()
        greeting = invoke_registered_tool(service, registry, plugin_tool_name("hello-node", "hello"), {}, request_id="hello")
        assert "来自 Node.js" in json.dumps(greeting, ensure_ascii=False), greeting
        read = invoke_registered_tool(service, registry, plugin_tool_name("hello-node", "read_text"), {"path": "notes.txt"},
                                      request_id="read")
        assert "沙箱里的笔记" in json.dumps(read, ensure_ascii=False), read
    finally:
        registry.close_mcp_clients()
    disabled = service.command("/plugins disable hello-node", revision=service.catalog().revision, request_id="disable")
    assert disabled["state"] == "succeeded" and disabled["details"]["released"], disabled
    environments = service.context.owner.plugins_dir / "environments"
    assert not environments.exists() or not any(environments.iterdir())
    leftovers = subprocess.run(["ps", "-axo", "command"], capture_output=True, text=True, check=True).stdout
    assert str(service.context.owner.plugins_dir) not in leftovers


@pytest.mark.parametrize("sandbox", [True, False])
def test_config_reaches_model_registry_and_panel_clients(tmp_path, monkeypatch, sandbox):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.gateway_parts import plugin_panels_http

    monkeypatch.setattr(PluginMCPClient, "start", lambda *_: pytest.fail("构造 Agent 不应启动插件"))
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"),
                                    plugin_process_sandbox=sandbox), tmp_path)
    try:
        assert agent.tools._construction_params.plugin_process_sandbox is sandbox
    finally:
        agent.tools.close_mcp_clients()
    server = SimpleNamespace(agent=SimpleNamespace(config=SimpleNamespace(plugin_process_sandbox=sandbox)))
    service = plugin_panels_http.plugin_display_service(server)
    try:
        assert service._client_factory.keywords == {"process_sandbox": sandbox}
    finally:
        service.close()


@pytest.mark.parametrize("setting", ["true", "false"])
def test_config_reaches_real_management_context(tmp_path, setting):
    from agent_py_agent.agent.plugin_management import plugin_management_context
    from agent_py_agent.agent.user_space.home_layout import home_paths
    from agent_py_agent.agent.user_space.owner_resolver import home_paths_with_owner

    config_path = tmp_path / "agent.yaml"
    config_path.write_text(f"plugin_process_sandbox: {setting}\n")
    service, _ = manager(tmp_path)
    owner = service.context.owner
    home = home_paths_with_owner(home_paths(tmp_path / "home"), owner)
    context = plugin_management_context(owner, home, load_config(config_path), service.context.threads,
                                        actor_id="tester", channel="chat", conversation_id="session", is_admin=True)
    assert context.process_sandbox is (setting == "true")
