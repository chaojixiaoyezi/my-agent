"""R2 / GPT-P1-03：真实配置的外部 file: 宿主来源进入全部插件启动路径的最终隐藏规格。

凭据文件与部署配置文件都放在 HOME 与 MY_AGENT_HOME 之外，经真实 load_config 与原宿主投影进入四条实际启动路径：
管理启用（预检 + _candidate）、业务显式调用、工具注册表同步、Gateway 面板/事件共用池；每条覆盖 legacy-wide、
restricted、v8 三种策略。替身只在进程边界：候选 start/目录发现/stop、业务 start、MCP 传输与平台就绪自检；
规格工厂、预检、客户端构造、重连重建与 Seatbelt 规则编译都是真实代码。平台固定为 Darwin 编译规则，Linux 精确
单文件隔离缺口经平台替身验证拒绝，不宣称真实 Seatbelt/bwrap 车道通过。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent import plugin_runtime, plugin_sandbox
from agent_py_agent.agent.attempt.sandbox import AttemptExecutionSandbox, SandboxUnavailableError
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts import plugin_panels_http
from agent_py_agent.agent.path_access_policy import configured_host_source_paths
from agent_py_agent.agent.plugin_display.service import plugin_display_client
from agent_py_agent.agent.plugin_enable_tool import PluginEnablePolicy
from agent_py_agent.agent.plugin_management import PluginManagement, plugin_management_context
from agent_py_agent.agent.plugin_runtime import PluginMCPClient
from agent_py_agent.agent.settings.config import load_config
from agent_py_agent.agent.tooling.mcp_client import MCPError, MCPStdioClient
from agent_py_agent.agent.tooling.plugin_registration import synchronize_plugin_clients
from agent_py_agent.agent.tooling.process_registry import ProcessTerminationReceipt
from agent_py_agent.agent.tooling.registry import ToolRegistryParams
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import home_paths_with_owner, resolve_owner_home
from agent_py_agent.tests.plugin_enable_fixtures import confirm_legacy_enable
from agent_py_agent.tests.test_plugin_any_language import PLUGIN_ID, _declaration, _enable, _package
from agent_py_agent.tests.test_plugin_manifest_v8 import _bundle as v8_bundle
from agent_py_agent.tests.test_plugin_manifest_v8 import _v8

_KINDS = ("legacy-wide", "restricted", "v8")
# 插件程序永不执行：每次启动都在进程边界被替身截住。
_PROGRAM = b"raise RuntimeError('fixture plugin must not execute')\n"
_LINUX_FILE_GAP = "host_exclusive_file_isolation_unavailable"


# 类用途: 一次用例的目录布局；HOME、数据根与两份外部来源互不包含，同级业务文件用来证明不隐藏父目录。
@dataclass(frozen=True)
class _Layout:
    tmp: Path
    data_root: Path
    credential: Path
    config_file: Path
    sibling: Path

    @property
    def sources(self) -> tuple[Path, ...]:
        return (self.credential, self.config_file)


# 类用途: 进程边界上观察到的预检/启动规格、编译出的启动命令和构造的插件客户端。
@dataclass
class _Boundary:
    specs: list = field(default_factory=list)
    launches: list = field(default_factory=list)
    clients: list = field(default_factory=list)


# 函数用途: 建立临时 HOME、数据根与外部凭据目录，并确认外部来源同时位于两者之外。
def _layout(tmp_path: Path, monkeypatch) -> _Layout:
    tmp = tmp_path.resolve()
    os_home, data_root = tmp / "os-home", tmp / "home"
    credential = tmp / "external-credentials" / "feishu-app-secret"
    config_file = tmp / "deploy" / "agent.yaml"
    for directory in (os_home, data_root, credential.parent, config_file.parent):
        directory.mkdir(parents=True)
    credential.write_text("fixture-not-a-secret\n")
    sibling = credential.with_name("business-notes.txt")
    sibling.write_text("ordinary business file next to the credential\n")
    monkeypatch.setenv("HOME", str(os_home))
    monkeypatch.setenv("MY_AGENT_HOME", str(data_root))
    for source in (credential, config_file):
        assert not source.is_relative_to(Path.home()) and not source.is_relative_to(data_root)
    return _Layout(tmp, data_root, credential, config_file, sibling)


# 函数用途: 写外部部署配置并经真实加载器读回；三种策略只差沙箱/事件开关，宿主来源相同。
def _config(layout: _Layout, kind: str, **extra):
    settings = {"feishu_app_secret": f"file:{layout.credential}", "plugin_process_sandbox": kind == "legacy-wide",
                "plugin_legacy_sandbox_default": kind != "legacy-wide", "plugin_events_enabled": kind == "v8", **extra}
    layout.config_file.write_text("".join(f"{key}: {json.dumps(value)}\n" for key, value in settings.items()))
    config = load_config(layout.config_file)
    assert {str(source) for source in layout.sources} <= set(config.host_exclusive_path_roots)
    return config


# 函数用途: 经真实 plugin_management_context 组装管理服务，宿主来源只来自上面的真实配置。
def _management(layout: _Layout, config, kind: str) -> PluginManagement:
    owner = resolve_owner_home(layout.data_root)
    threads = ConversationStore(owner.home_dir / "conversations", initialize=False).threads
    home = home_paths_with_owner(home_paths(layout.data_root), owner)
    context = plugin_management_context(owner, home, config, threads, actor_id="tester", channel="chat",
                                        conversation_id="session", is_admin=True)
    assert set(layout.sources) <= set(context.host_exclusive_roots)
    if kind == "v8":
        # 本机管理员资格判定不在本题范围；宿主来源仍完全来自真实配置投影。
        context = replace(context, events_owner_allowed=True)
    return PluginManagement(context)


# 函数用途: 经原管理命令安装老格式 v6 或 v8 包（安装本身不启动插件）；包放在原路径策略授权的会话工作区里。
def _install(service: PluginManagement, kind: str) -> None:
    workspace = service.context.workspace
    workspace.mkdir(parents=True, exist_ok=True)
    if kind == "v8":
        source = workspace / "event-plugin.zip"
        source.write_bytes(v8_bundle(_v8()))
    else:
        source = _package(workspace, _declaration(), {"bin/server.py": _PROGRAM})
    result = service.command(f'/plugins install "{source}"', revision=service.catalog().revision, request_id="install")
    assert result["state"] == "succeeded", result


# 函数用途: 固定 Darwin 平台，记录全部预检与启动规格、真实编译的启动命令和构造出的插件客户端。
def _launch_boundary(patch) -> _Boundary:
    boundary = _Boundary()
    patch.setattr(plugin_sandbox.platform, "system", lambda: "Darwin")
    patch.setattr(AttemptExecutionSandbox, "require_ready", lambda self: boundary.specs.append(self.spec))

    # 真实包装函数；只补上永不执行的 sandbox-exec 路径，任何车道都编译同一份 Seatbelt 规则。
    def launch(argv, spec):
        command = plugin_sandbox.sandboxed_plugin_argv(argv, replace(spec, macos_sandbox_exec="/usr/bin/sandbox-exec"))
        boundary.launches.append(command)
        return command

    patch.setattr(plugin_runtime, "sandboxed_plugin_argv", launch)
    initialize = PluginMCPClient.__init__

    def observe(self, *args, **kwargs):
        initialize(self, *args, **kwargs)
        boundary.clients.append(self)

    patch.setattr(PluginMCPClient, "__init__", observe)
    return boundary


# 函数用途: 候选连接只替换进程收发；客户端构造（含规格）与原启用链照常执行。
def _no_candidate_process(patch) -> None:
    patch.setattr(PluginMCPClient, "start", lambda _self: object())
    patch.setattr(PluginMCPClient, "discover_tools", lambda _self, _transport: ())
    patch.setattr(PluginMCPClient, "stop", lambda _self: SimpleNamespace(
        confirmed=True, record={"session_id": "fixture-candidate"}, terminations=()))


# 函数用途: 按真实两步确认协议启用插件，返回预览与成功回执。
def _enable_plugin(service: PluginManagement, kind: str) -> tuple[dict, dict]:
    preview = _enable(service, "enable")
    assert preview["details"]["reason"] == "confirmation_required", preview
    if kind == "v8":
        enabled = _enable(service, "confirm", preview["details"]["confirmation"]["confirm_code"])
    else:
        enabled = confirm_legacy_enable(service, preview)
    assert enabled["state"] == "succeeded", enabled
    return preview, enabled


# 函数用途: 安装并启用插件（候选不起进程），供运行期启动路径的用例使用。
def _enabled_service(layout: _Layout, config, kind: str, monkeypatch) -> PluginManagement:
    service = _management(layout, config, kind)
    _install(service, kind)
    with monkeypatch.context() as patch:
        _launch_boundary(patch)
        _no_candidate_process(patch)
        _enable_plugin(service, kind)
    return service


# 函数用途: 用原 MCP 启动/重连链但替换传输，记录每次实际启动使用的完整命令。
def _fake_transport(patch) -> list[list[str]]:
    launched: list[list[str]] = []

    def start_transport(client):
        closed = threading.Event()
        transport = SimpleNamespace(inbox=SimpleNamespace(closed=closed), ready=True,
                                    is_running=lambda: not closed.is_set(), revoke=closed.set,
                                    terminate=lambda **_: ProcessTerminationReceipt("fixture-transport", True, 0, 1))
        client._transport, client._starting = transport, False
        launched.append([client.config.command, *client.config.args])
        return transport

    patch.setattr(MCPStdioClient, "_start_transport", start_transport)
    return launched


# 函数用途: 规格隐藏集合（真实路径），用于集合比较。
def _hidden(spec) -> frozenset[Path]:
    return frozenset(Path(path).resolve() for path in spec.hidden_paths)


# 函数用途: 断言外部来源按精确文件留在隐藏集合里，既不丢失，也不扩成父目录或同级业务文件。
def _assert_exact_sources(spec, layout: _Layout) -> None:
    hidden = _hidden(spec)
    for source in layout.sources:
        assert source in hidden, (source, sorted(hidden))
        assert not any(path in source.parents for path in hidden), "宿主来源不能扩成父目录"
    assert not any(path == layout.sibling or path in layout.sibling.parents for path in hidden)


# 函数用途: 断言编译出的 Seatbelt 规则精确拒绝来源文件，插件自身 argv 不含来源路径。
def _assert_compiled_exactly(command: list[str], layout: _Layout) -> None:
    profile = command[command.index("-p") + 1]
    for source in layout.sources:
        literal = json.dumps(str(source), ensure_ascii=False)
        assert f"(deny file-read* file-write* (subpath {literal}))" in profile
        assert f"(deny process-exec (subpath {literal}))" in profile
        assert f"(subpath {json.dumps(str(source.parent), ensure_ascii=False)})" not in profile
    assert str(layout.sibling) not in profile
    plugin_argv = command[command.index("--") + 1:]
    assert not any(str(source) in part for part in plugin_argv for source in layout.sources)


# 函数用途: 断言来源只进隐藏规格：客户端冻结了它们，但插件原始 argv 与 env 里都没有。
def _assert_client_sources(client: PluginMCPClient, layout: _Layout) -> None:
    assert set(layout.sources) <= set(client._host_exclusive_roots)
    _assert_exact_sources(client._source_sandbox_spec, layout)
    exposed = [*client._source_raw_argv, *client.config.env.values()]
    assert not any(str(source) in str(value) for value in exposed for source in layout.sources)


# 函数用途: 断言公开回执不含宿主来源路径。
def _assert_payload_clean(payload: dict, layout: _Layout) -> None:
    text = json.dumps(payload, ensure_ascii=False, default=str)
    assert not any(str(source) in text for source in layout.sources)


# 函数用途: 经真实 start → 断开 → reconnect 两次实际启动，确认两次都按冻结来源编译且规格不变。
def _assert_reconnect_keeps_sources(client: PluginMCPClient, launched: list, layout: _Layout) -> None:
    first = client.start()
    first.revoke()
    second = client.reconnect()
    assert second is not first and len(launched) == 2
    _assert_exact_sources(client._source_sandbox_spec, layout)
    for command in launched:
        _assert_compiled_exactly(command, layout)


@pytest.mark.parametrize("kind", _KINDS)
def test_management_preflight_and_candidate_hide_external_file_sources(tmp_path, monkeypatch, kind):
    layout = _layout(tmp_path, monkeypatch)
    service = _management(layout, _config(layout, kind), kind)
    _install(service, kind)
    with monkeypatch.context() as patch:
        boundary = _launch_boundary(patch)
        _no_candidate_process(patch)
        preview, enabled = _enable_plugin(service, kind)
    (candidate,) = boundary.clients
    _assert_client_sources(candidate, layout)
    (launch,) = boundary.launches
    _assert_compiled_exactly(launch, layout)
    # 预览与确认两次启用预检、客户端内部就绪门与实际启动规格隐藏同一集合，预检不是另一套推导。
    assert len(boundary.specs) >= 3
    assert {_hidden(spec) for spec in boundary.specs} == {_hidden(candidate._source_sandbox_spec)}
    for payload in (preview, enabled):
        _assert_payload_clean(payload, layout)


@pytest.mark.parametrize("kind", _KINDS)
def test_business_invocation_client_hides_external_file_sources(tmp_path, monkeypatch, kind):
    layout = _layout(tmp_path, monkeypatch)
    service = _enabled_service(layout, _config(layout, kind), kind, monkeypatch)
    target = service.context.workspace / "input.txt"
    target.write_text("business input\n")

    # 业务连接只在实际起进程处截住；客户端构造与业务预检照常执行。
    def no_process(_self):
        raise MCPError("fixture business plugin process is not started", code="MCP_SERVER_START_FAILED")

    with monkeypatch.context() as patch:
        boundary = _launch_boundary(patch)
        patch.setattr(PluginMCPClient, "start", no_process)
        result = service.command(f'/plugins@{PLUGIN_ID} read "{target}"', revision=service.catalog().revision,
                                 request_id="business")
    assert result["state"] != "succeeded", result
    (client,) = boundary.clients
    _assert_client_sources(client, layout)
    _assert_compiled_exactly(boundary.launches[-1], layout)
    assert {_hidden(spec) for spec in boundary.specs} == {_hidden(client._source_sandbox_spec)}
    _assert_payload_clean(result, layout)


@pytest.mark.parametrize("kind", _KINDS)
def test_registry_adapter_and_sync_keep_external_file_sources_across_reconnect(tmp_path, monkeypatch, kind):
    from agent_py_agent.agent import core

    layout = _layout(tmp_path, monkeypatch)
    config = _config(layout, kind, model_backend="echo", my_agent_home=str(layout.data_root))
    _enabled_service(layout, config, kind, monkeypatch)
    # core 的字段敏感适配器只在 ToolRegistryParams 声明该字段时转运来源（core.py 本题只读）。
    assert "host_exclusive_paths" in ToolRegistryParams.__dataclass_fields__
    adapted = core._tool_registry_host_exclusive_kwargs(home_paths(layout.data_root), config)
    assert set(layout.sources) <= set(adapted["host_exclusive_paths"])
    agent = core.SimpleAgent(config, layout.tmp)
    try:
        params = agent.tools._construction_params
        assert set(layout.sources) <= set(params.host_exclusive_paths)
        assert "host_exclusive_paths" not in repr(params)
        with monkeypatch.context() as patch:
            _launch_boundary(patch)
            launched = _fake_transport(patch)
            synchronize_plugin_clients(agent.tools)
            (client,) = [item for item in agent.tools._mcp_clients if isinstance(item, PluginMCPClient)]
            _assert_client_sources(client, layout)
            _assert_reconnect_keeps_sources(client, launched, layout)
    finally:
        agent.tools.close_mcp_clients()


@pytest.mark.parametrize("kind", _KINDS)
def test_gateway_shared_pool_factory_keeps_external_file_sources_across_reconnect(tmp_path, monkeypatch, kind):
    layout = _layout(tmp_path, monkeypatch)
    config = _config(layout, kind)
    service = _enabled_service(layout, config, kind, monkeypatch)
    entry = service.installations.snapshot()[0]
    server = SimpleNamespace(agent=SimpleNamespace(config=config), plugin_channel_pool=None,
                             plugin_display=None, plugin_event_hub=None)
    client = None
    try:
        pool = plugin_panels_http.plugin_channel_pool(server)
        # 面板服务与事件中心取同一个池，所以这一个 factory 冻结的来源覆盖两条启动路径。
        assert plugin_panels_http.plugin_display_service(server)._pool is pool
        plugin_panels_http.plugin_event_hub(server)
        assert server.plugin_channel_pool is pool
        assert set(layout.sources) <= set(pool.client_factory.keywords["host_exclusive_roots"])
        with monkeypatch.context() as patch:
            _launch_boundary(patch)
            launched = _fake_transport(patch)
            client = pool.client_factory(service.context.owner, entry)
            _assert_client_sources(client, layout)
            _assert_reconnect_keeps_sources(client, launched, layout)
    finally:
        if client is not None:
            client.stop()
        plugin_panels_http.close_plugin_channel(server)


def test_linux_exact_file_gap_refuses_preflight_and_launch_wrapper(tmp_path, monkeypatch):
    layout = _layout(tmp_path, monkeypatch)
    roots = configured_host_source_paths(_config(layout, "legacy-wide"))
    owner_home = resolve_owner_home(layout.data_root).home_dir
    owner_home.mkdir(parents=True)
    monkeypatch.setattr(AttemptExecutionSandbox, "require_ready", lambda _self: None)
    monkeypatch.setattr(plugin_sandbox.platform, "system", lambda: "Linux")
    assert plugin_sandbox.plugin_sandbox_problem(True, owner_home) == ""
    assert plugin_sandbox.plugin_sandbox_problem(True, owner_home, host_exclusive_roots=roots) == _LINUX_FILE_GAP
    spec = plugin_sandbox.plugin_sandbox_spec(cwd=owner_home, data_dir=owner_home, owner_home=owner_home,
                                              host_exclusive_roots=roots)
    # 拒绝而不是丢弃来源或扩成父目录：规格仍按精确文件保留。
    _assert_exact_sources(spec, layout)
    with pytest.raises(SandboxUnavailableError, match="HOST_EXCLUSIVE_FILE_ISOLATION_UNAVAILABLE"):
        plugin_sandbox.sandboxed_plugin_argv(["/usr/bin/true"], replace(spec, bwrap_path="/usr/bin/bwrap"))


@pytest.mark.parametrize("kind", _KINDS)
def test_linux_exact_file_gap_refuses_enable_before_side_effects(tmp_path, monkeypatch, kind):
    layout = _layout(tmp_path, monkeypatch)
    service = _management(layout, _config(layout, kind), kind)
    _install(service, kind)
    monkeypatch.setattr(AttemptExecutionSandbox, "require_ready", lambda _self: None)
    monkeypatch.setattr(plugin_sandbox.platform, "system", lambda: "Linux")
    monkeypatch.setattr(PluginMCPClient, "__init__", lambda *_args, **_kwargs: pytest.fail("拒绝必须先于候选客户端"))
    refused = _enable(service, "enable-linux")
    assert refused["state"] == "failed" and refused["details"]["reason"] == _LINUX_FILE_GAP, refused
    assert refused["error_code"] != "PLUGIN_CONFIRMATION_REQUIRED", refused
    assert service.installations.snapshot()[0].activation is None
    environments = service.context.owner.plugins_dir / "environments"
    assert not environments.exists() or not any(environments.iterdir())


@pytest.mark.parametrize("kind", _KINDS)
def test_linux_exact_file_gap_refuses_business_and_shared_pool_launch(tmp_path, monkeypatch, kind):
    layout = _layout(tmp_path, monkeypatch)
    config = _config(layout, kind)
    service = _enabled_service(layout, config, kind, monkeypatch)
    entry = service.installations.snapshot()[0]
    target = service.context.workspace / "input.txt"
    target.write_text("business input\n")
    monkeypatch.setattr(AttemptExecutionSandbox, "require_ready", lambda _self: None)
    monkeypatch.setattr(plugin_sandbox.platform, "system", lambda: "Linux")
    monkeypatch.setattr(PluginMCPClient, "start", lambda _self: pytest.fail("不能启动无法精确隔离来源的插件"))
    result = service.command(f'/plugins@{PLUGIN_ID} read "{target}"', revision=service.catalog().revision,
                             request_id="business-linux")
    assert result["state"] == "rejected", result
    if kind != "restricted":
        # 沙箱档开启的业务预检在建运行前给出结构化原因；restricted 由客户端内部就绪门在启动前拒绝。
        assert result["error_code"] == "PLUGIN_RUNTIME_UNAVAILABLE", result
        assert result["details"] == {"reason": _LINUX_FILE_GAP}, result
    with pytest.raises(SandboxUnavailableError) as refused:
        plugin_display_client(service.context.owner, entry, process_sandbox=kind == "legacy-wide",
                              host_exclusive_roots=configured_host_source_paths(config))
    assert _LINUX_FILE_GAP in str(refused.value).lower()


def test_host_sources_stay_out_of_policy_and_registry_reprs(tmp_path):
    source = tmp_path / "external-credentials" / "feishu-app-secret"
    policy = PluginEnablePolicy(host_exclusive_roots=(source,))
    params = ToolRegistryParams(tmp_path, 1, 1, 1, 1, 1, 1, 1, False, host_exclusive_paths=(source,))
    assert policy.host_exclusive_roots == (source,) and params.host_exclusive_paths == (source,)
    assert str(source) not in repr(policy) and str(source) not in repr(params)
