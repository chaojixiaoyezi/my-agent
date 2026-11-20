from __future__ import annotations

import gc
import json
import os
import pickle
import sys
import weakref
from concurrent.futures import ThreadPoolExecutor
from copy import copy
from dataclasses import FrozenInstanceError, asdict, fields, make_dataclass, replace
from importlib.util import find_spec
from inspect import signature
from threading import Barrier, Event, Lock
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_py_agent.agent import plugin_runtime
from agent_py_agent.agent.agent_core.runner import worker
from agent_py_agent.agent.common.cancellation import CancellationToken
from agent_py_agent.agent.common.fast_dataclass import fast_replace
from agent_py_agent.agent.plugin_runtime import DeclaredMCPClient, PluginMCPClient, plugin_tool_name
from agent_py_agent.agent.tooling import mcp_client, mcp_protocol, plugin_registration
from agent_py_agent.agent.tooling import registry as registry_module
from agent_py_agent.agent.tooling.computer_use_profile import (
    COMPUTER_USE_MCP_SERVER_NAME,
    ComputerUseTier,
    computer_use_mcp_servers,
    with_computer_use_observation,
)
from agent_py_agent.agent.tooling.mcp_declarations import (
    MCPServerWithCatalog,
    ResolvedServerDeclarations,
    ResolvedToolDeclaration,
)
from agent_py_agent.agent.tooling.registry import (
    CatalogRenderConfig,
    ToolRegistry,
    ToolRegistryParams,
)
from agent_py_agent.agent.tooling.runtime_contracts import ToolCall
from agent_py_agent.tests.plugin_activation_fixtures import invoke_registered_tool, plugin_registry
from agent_py_agent.tests.test_mcp_client import _ECHO_SERVER
from agent_py_agent.tests.test_workspace_peek_package import (
    installed_peek,  # noqa: F401 - pytest fixture
    installed_sdk,  # noqa: F401 - pytest fixture
    manager,
)

_TOOLS = [
    {
        "name": "echo",
        "description": "Echo back the provided text",
        "inputSchema": {
            "type": "object", "properties": {"text": {"type": "string", "description": "text to echo"}},
            "required": ["text"],
        },
    },
    {
        "name": "add",
        "description": "Add two integers",
        "inputSchema": {
            "type": "object",
            "properties": {
                "a": {"type": "integer", "description": "first addend"},
                "b": {"type": "integer", "description": "second addend"},
            },
            "required": ["a", "b"],
        },
    },
]
_ECHO = "mcp__computer_use__echo"
_PEEK = "mcp__workspace_peek__echo"
_WAIT = 10


@pytest.fixture
def processes(monkeypatch):
    started = []
    lock = Lock()
    popen = mcp_client.subprocess.Popen

    def start(argv, **kwargs):
        process = popen(argv, **kwargs)
        if argv[:2] == [sys.executable, "-c"]:
            with lock:
                started.append((argv[-1], process))
        return process

    monkeypatch.setattr(mcp_client.subprocess, "Popen", start)
    return started


def _server(name, *, script=_ECHO_SERVER, declared=True):
    config = {"command": sys.executable, "args": ["-c", script, name], "default_effect": "read_only"}
    return MCPServerWithCatalog(config, _TOOLS) if declared else config


def _registry(tmp_path, *, defer_mcp_connect=True, servers=None, **settings):
    return ToolRegistry(ToolRegistryParams(
        workspace_root=tmp_path, max_chars=1000, max_entries=20, max_matches=10, web_max_chars=1000,
        http_timeout=10, catalog_limit=10, retrieval_limit=3, vector_search_enabled=False,
        mcp_servers=servers if servers is not None else {
            name: _server(name) for name in ("computer_use", "workspace_peek")
        },
        defer_mcp_connect=defer_mcp_connect,
        **settings,
    ))


def _worker(tmp_path, *, servers=None, **settings):
    params = SimpleNamespace(root=tmp_path, request_transport=None)

    def agent(config, root, *, defer_mcp_connect=False):
        return SimpleNamespace(tools=_registry(root, defer_mcp_connect=defer_mcp_connect, servers=config, **settings))

    return worker._construct_transported_worker(agent, params, servers), params


def _invoke(registry, snapshot, name=_ECHO, *, token=None):
    spec = snapshot.runtime(name).model_spec
    call = ToolCall(uuid4().hex, name, {"text": "m25"}, "native", spec.schema_hash, "r", "t", "a")
    return registry.execute_tool(call, write_boundary=None, runtime_snapshot=snapshot, cancellation_token=token).result


def test_worker_prepares_and_enumerates_without_starting_helpers(tmp_path, processes):
    agent, params = _worker(tmp_path)
    tools = agent.tools
    try:
        tools.prepare_for_run()
        assert [name for name, _ in processes] == []
        specs = tools.model_visible_specs(allowed_tools=[_ECHO])
        assert [spec.name for spec in specs] == [_ECHO]
        assert specs[0].input_schema["required"] == ["text"]
        snapshot = tools.runtime_snapshot(allowed_tools=[_ECHO])
        hidden = tools.tools[_PEEK].model_spec
        call = ToolCall("outside-scope", _PEEK, {"text": "m25"}, "native", hidden.schema_hash, "r", "t", "a")
        assert not tools.execute_tool(call, write_boundary=None, runtime_snapshot=snapshot).result.ok
        assert processes == []
        clients = list(tools._mcp_clients)
    finally:
        worker._close_worker_mcp_clients(agent, params)
    assert len(clients) == 2 and all(client.is_closed() for client in clients)
    assert tools._mcp_borrow_count == [0]
    assert processes == []


def test_worker_starts_only_the_server_used_by_16_first_calls(tmp_path, processes):
    agent, params = _worker(tmp_path)
    tools = agent.tools
    try:
        tools.prepare_for_run()
        snapshot = tools.runtime_snapshot(allowed_tools=[_ECHO, _PEEK])
        barrier = Barrier(16)

        def invoke():
            barrier.wait(_WAIT)
            return _invoke(tools, snapshot)

        with ThreadPoolExecutor(max_workers=16) as pool:
            calls = [pool.submit(invoke) for _ in range(16)]
            assert all(call.result(_WAIT).ok for call in calls)
        assert [name for name, _ in processes] == ["computer_use"]
    finally:
        worker._close_worker_mcp_clients(agent, params)
    assert all(process.poll() is not None for _, process in processes)


def test_owner_closes_all_derived_views_before_clients(tmp_path, monkeypatch, processes):
    agent, params = _worker(tmp_path)
    tools = agent.tools
    view = tools.with_access_policy(access_mode="full-access", path_access_mode="full", owner_scope_root="")
    nested = view.with_access_policy(access_mode="read-only", path_access_mode="normal", owner_scope_root="")
    try:
        nested.prepare_for_run()
        assert _invoke(nested, nested.runtime_snapshot(allowed_tools=[_ECHO])).ok
        counts = []
        for client in tools._mcp_clients:
            stop = client.stop

            def record_stop(stop=stop):
                counts.append(tools._mcp_borrow_count[0])
                return stop()

            monkeypatch.setattr(client, "stop", record_stop)
        worker._close_worker_mcp_clients(agent, params)
        assert tools._mcp_borrow_count == [0]
        assert counts and set(counts) == {0}
        assert all(process.poll() is not None for _, process in processes)
        for facade in (view, nested):
            assert facade._mcp_borrow_released
            assert _ECHO not in facade.runtime_snapshot().available_tool_names
    finally:
        nested.close_mcp_clients()
        view.close_mcp_clients()
        tools.close_mcp_clients()


def test_close_racing_first_process_creation_waits_for_cleanup(tmp_path, processes, monkeypatch):
    agent, params = _worker(tmp_path, servers={"computer_use": _server("computer_use")})
    tools = agent.tools
    try:
        tools.prepare_for_run()
        assert not processes
        snapshot = tools.runtime_snapshot(allowed_tools=[_ECHO])
        process_created, close_entered, release = Event(), Event(), Event()
        start = mcp_client.subprocess.Popen
        client = tools._mcp_clients[0]
        stop = client.stop

        def pause_after_start(argv, **kwargs):
            process = start(argv, **kwargs)
            if argv[:2] == [sys.executable, "-c"]:
                process_created.set()
                assert release.wait(_WAIT)
            return process

        def observe_close():
            close_entered.set()
            return stop()

        monkeypatch.setattr(mcp_client.subprocess, "Popen", pause_after_start)
        monkeypatch.setattr(client, "stop", observe_close)
        with ThreadPoolExecutor(max_workers=2) as pool:
            call = pool.submit(_invoke, tools, snapshot)
            try:
                assert process_created.wait(_WAIT)
                close = pool.submit(worker._close_worker_mcp_clients, agent, params)
                assert close_entered.wait(_WAIT)
                assert client._closed.wait(_WAIT)
                assert not close.done()
            finally:
                release.set()
            close.result(_WAIT)
            assert not call.result(_WAIT).ok
        assert len(processes) == 1 and processes[0][1].poll() is not None
        assert not tools._mcp_clients
    finally:
        worker._close_worker_mcp_clients(agent, params)


def test_cancelled_worker_releases_borrow_and_next_worker_uses_service(tmp_path, processes, monkeypatch):
    owner = _registry(tmp_path)
    first, first_params = _worker(tmp_path)
    second, second_params = _worker(tmp_path)
    try:
        assert first.tools.share_same_owner_mcp_state_from(owner)
        first.tools.prepare_for_run()
        snapshot = first.tools.runtime_snapshot(allowed_tools=[_ECHO])
        token = CancellationToken()
        assert processes == []
        client = owner._mcp_clients[0]
        entered, resume = Event(), Event()
        await_response = mcp_protocol.MCPInbox.await_response

        def pause_business_response(inbox, request_id, method, deadline):
            if method == "tools/call":
                entered.set()
                assert resume.wait(_WAIT)
            return await_response(inbox, request_id, method, deadline)

        with monkeypatch.context() as patch, ThreadPoolExecutor(max_workers=1) as pool:
            patch.setattr(mcp_protocol.MCPInbox, "await_response", pause_business_response)
            call = pool.submit(_invoke, first.tools, snapshot, token=token)
            try:
                assert entered.wait(_WAIT)
                token.cancel()
            finally:
                resume.set()
            result = call.result(_WAIT)
            assert not result.ok and result.reported_error_code == "MCP_CANCELLED"
        worker._close_worker_mcp_clients(first, first_params)
        assert owner._mcp_borrow_count == [0]
        assert len(processes) == 1 and client.is_running()
        assert second.tools.share_same_owner_mcp_state_from(owner)
        second.tools.prepare_for_run()
        assert _invoke(second.tools, second.tools.runtime_snapshot(allowed_tools=[_ECHO])).ok
        assert [name for name, _ in processes] == ["computer_use"]
        assert owner._mcp_clients[0] is client
        worker._close_worker_mcp_clients(second, second_params)
        assert owner._mcp_borrow_count == [0]
    finally:
        first.tools.close_mcp_clients()
        second.tools.close_mcp_clients()
        owner.close_mcp_clients()
    assert all(process.poll() is not None for _, process in processes)


def test_declared_worker_keeps_dynamic_discovery_and_main_eagerness(tmp_path, processes):
    servers = {
        "computer_use": _server("computer_use"),
        "workspace_peek": _server("workspace_peek", declared=False),
    }
    main = _registry(tmp_path, servers=servers)
    agent, params = _worker(tmp_path, servers=servers)
    try:
        assert agent.tools.share_same_owner_mcp_state_from(main)
        agent.tools.prepare_for_run()
        assert [name for name, _ in processes] == ["workspace_peek"]
        assert {spec.name for spec in agent.tools.model_visible_specs(allowed_tools=[_ECHO, _PEEK])} == {_ECHO, _PEEK}
        main.prepare_for_run()
        main.prepare_for_run()
        assert [name for name, _ in processes] == ["workspace_peek", "computer_use"]
        assert _invoke(main, main.runtime_snapshot(allowed_tools=[_ECHO])).ok
    finally:
        worker._close_worker_mcp_clients(agent, params)
        main.close_mcp_clients()


def test_declared_worker_idle_stop_uses_configured_window(tmp_path, processes, monkeypatch):
    interval = 37
    agent, params = _worker(tmp_path, capability_pack_mcp_idle_stop_seconds=interval)
    tools = agent.tools
    try:
        tools.prepare_for_run()
        snapshot = tools.runtime_snapshot(allowed_tools=[_ECHO])
        assert _invoke(tools, snapshot).ok
        client = tools._mcp_clients[0]
        initial = client.last_used
        clock = SimpleNamespace(now=initial + interval - 1)
        monkeypatch.setattr(registry_module, "time", SimpleNamespace(monotonic=lambda: clock.now))
        tools.prepare_for_run()
        assert client.is_running()
        clock.now = initial + interval
        tools.prepare_for_run()
        assert not client.is_running() and not client.is_closed()
        assert processes[0][1].poll() is not None
        assert _invoke(tools, snapshot).ok
        assert [name for name, _ in processes] == ["computer_use", "computer_use"]
        assert tools._mcp_clients[0] is client
    finally:
        worker._close_worker_mcp_clients(agent, params)


@pytest.mark.parametrize("changed", ["launch", "catalog"])
def test_config_revision_retires_previous_declared_lease(tmp_path, processes, changed):
    servers = {"computer_use": _server("computer_use")}
    agent, params = _worker(tmp_path, servers=servers)
    tools = agent.tools
    try:
        tools.prepare_for_run()
        old = tools.runtime_snapshot(allowed_tools=[_ECHO])
        assert _invoke(tools, old).ok
        client = tools._mcp_clients[0]
        if changed == "launch":
            servers["computer_use"] = _server("computer-use-revision-two")
            second_launch = "computer-use-revision-two"
        else:
            settings = dict(servers["computer_use"])
            catalog = [dict(entry, description="Revised " + entry["description"]) for entry in _TOOLS]
            servers["computer_use"] = MCPServerWithCatalog(settings, catalog)
            second_launch = "computer_use"
        tools.prepare_for_run()
        assert client.is_closed() and processes[0][1].poll() is not None
        assert tools._mcp_clients[0] is not client
        assert not _invoke(tools, old).ok
        assert len(processes) == 1
        assert _invoke(tools, tools.runtime_snapshot(allowed_tools=[_ECHO])).ok
        assert [name for name, _ in processes] == ["computer_use", second_launch]
    finally:
        worker._close_worker_mcp_clients(agent, params)


def test_live_schema_drift_prevents_tool_send(tmp_path, processes):
    marker = tmp_path / "received"
    script = _ECHO_SERVER.replace('"required": ["text"]', '"required": []', 1).replace(
        '            params = req.get("params") or {}',
        f'            open({str(marker)!r}, "w").close()\n'
        '            params = req.get("params") or {}',
    )
    agent, params = _worker(tmp_path, servers={"computer_use": _server("computer_use", script=script)})
    tools = agent.tools
    try:
        tools.prepare_for_run()
        result = _invoke(tools, tools.runtime_snapshot(allowed_tools=[_ECHO]))
        assert not result.ok and result.effect_outcome == "not_started"
        assert result.reported_error_code == "MCP_PROTOCOL_ERROR"
        assert tools._mcp_clients[0].publication.reasons == ({"tool": "echo", "code": "schema_mismatch"},)
        assert not marker.exists()
        assert len(processes) == 1 and processes[0][1].poll() is not None
    finally:
        worker._close_worker_mcp_clients(agent, params)


def test_owner_drains_derived_lease_queued_by_gc(tmp_path, processes):
    agent, params = _worker(tmp_path)
    tools = agent.tools
    try:
        tools.prepare_for_run()
        with tools._mcp_lifecycle_lock:
            view = tools.with_access_policy(access_mode="full-access", path_access_mode="full", owner_scope_root="")
            completed = view._mcp_borrow_release_completed
            ref = weakref.ref(view)
            del view
            gc.collect()
            assert ref() is None and not completed.is_set()
            worker._close_worker_mcp_clients(agent, params)
            assert tools._mcp_borrow_count == [0] and completed.is_set()
        assert processes == []
    finally:
        tools.close_mcp_clients()


@pytest.mark.parametrize(("full", "observation"), [(True, False), (True, True), (False, True)])
def test_computer_use_static_catalog_matches_live_tools_list(full, observation):
    modules = ("mcp", "pyautogui", "computer_control_mcp") if full else ("mcp",)
    missing = [name for name in modules if find_spec(name) is None]
    if missing:
        pytest.skip(f"Computer Use live adapter unavailable: missing {', '.join(missing)}")
    if full and sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        pytest.skip("Computer Use live adapter unavailable: no X11 DISPLAY for the full tier")
    servers = computer_use_mcp_servers({}, ComputerUseTier(full, True, "full-access", observe_only=not full))
    servers = with_computer_use_observation(servers, enabled=observation)
    raw = servers[COMPUTER_USE_MCP_SERVER_NAME]
    client = mcp_client.MCPStdioClient(mcp_client.MCPServerConfig.from_mapping(COMPUTER_USE_MCP_SERVER_NAME, raw))
    try:
        transport = client.start()
        live = client.list_tools(transport=transport)
        expected = {entry["name"]: (entry["description"], entry["inputSchema"]) for entry in raw.catalog}
        assert {tool.name: (tool.description, tool.input_schema) for tool in live} == expected
    finally:
        assert client.stop().confirmed


def test_workspace_peek_wheel_worker_starts_on_use_with_read_context(tmp_path, request, monkeypatch):
    missing = [name for name in ("pip", "setuptools") if find_spec(name) is None]
    if missing:
        pytest.skip(f"Workspace Peek live package unavailable: missing {', '.join(missing)}")
    python, manifest, _ = request.getfixturevalue("installed_peek")
    service, _ = manager(tmp_path)
    owner = service.context.owner
    owner.root.mkdir()
    activation = SimpleNamespace(
        phase="active", activation_id="test",
        plan=SimpleNamespace(environment_ref="test", interpreter_fingerprint="test"),
    )
    installation = SimpleNamespace(activation=activation, manifest=manifest, settings_json="{}")
    ref = SimpleNamespace(scope=SimpleNamespace(activation_id="test"), owner=lambda: owner,
                          require=lambda **_: installation, available=lambda: True)
    # 仅装配测试 wheel 的现成安装事实和独立 stdio，保留原模型工具、Schema、执行器与上下文运输。
    monkeypatch.setattr(plugin_runtime.PluginActivationRef, "from_owner", lambda *_args, **_kwargs: ref)
    monkeypatch.setattr(plugin_runtime, "_launch_argv", lambda *_args: (str(python), ["-I", "-m", "workspace_peek"], tmp_path))
    monkeypatch.setattr(plugin_runtime, "legacy_launch_policy", lambda *_args: None)
    client = PluginMCPClient(owner, installation)
    client._activation = None
    monkeypatch.setattr(plugin_registration, "synchronize_plugin_clients", lambda registry: registry._mcp_clients.append(client))
    (tmp_path / "note.txt").write_text("workspace context retained", encoding="utf-8")
    params = SimpleNamespace(root=tmp_path, request_transport=None)

    def agent(_config, _root, *, defer_mcp_connect=True):
        return SimpleNamespace(tools=plugin_registry(service, defer_mcp_connect=defer_mcp_connect))

    child = worker._construct_transported_worker(agent, params, None)
    tools = child.tools
    try:
        tools.prepare_for_run()
        assert tools._mcp_clients == [client]
        assert not client.is_running()
        name = plugin_tool_name("workspace-peek", "show")
        declared = next(tool for tool in manifest.tools if tool.name == "show")
        specs = tools.model_visible_specs(allowed_tools=[name])
        assert [spec.input_schema for spec in specs] == [declared.input_schema]
        call = invoke_registered_tool(service, tools, name, {"path": "note.txt"})
        assert call["state"] == "succeeded", call
        assert json.loads(json.loads(call["stored_output"])["result"])["text"] == "workspace context retained"
        process = client.connection().binding.process
    finally:
        worker._close_worker_mcp_clients(child, params)
    assert process.poll() is not None and tools._mcp_borrow_count == [0]


@pytest.mark.parametrize(("cls", "arguments", "changes"), [
    (CatalogRenderConfig, ("compact", 0, 5, [], False, 10, True, 0), {"limit": 1}),
    (ToolRegistryParams, (None, 1, 2, 3, 4, 5, 6, 7, False), {"access_mode": "read-only"}),
    (ResolvedToolDeclaration, ("read_only", "always"), {"approval": "mutating"}),
    (ResolvedServerDeclarations, ({},), {"notices": ({"tool": "echo", "code": "example"},)}),
])
def test_fast_frozen_dto_matches_plain_dataclass(cls, arguments, changes):
    plain = make_dataclass(cls.__name__, [(f.name, f.type, copy(f)) for f in fields(cls)], frozen=True)
    assert signature(cls) == signature(plain)
    baseline, converted = plain(*arguments), cls(*arguments)
    assert repr(baseline) == repr(converted)
    assert asdict(baseline) == asdict(converted)
    assert baseline == plain(*arguments) and converted == cls(*arguments)
    name, value = next(iter(changes.items()))
    for instance in (baseline, converted):
        with pytest.raises(FrozenInstanceError):
            setattr(instance, name, value)
    try:
        expected_hash = hash(baseline)
    except TypeError:
        with pytest.raises(TypeError):
            hash(converted)
    else:
        assert hash(converted) == expected_hash
    assert asdict(replace(baseline, **changes)) == asdict(fast_replace(converted, **changes))
    assert pickle.loads(pickle.dumps(converted)) == converted


def test_declared_client_direct_base_calls_keep_mro():
    descendants = [DeclaredMCPClient]
    for cls in descendants:
        descendants.extend(cls.__subclasses__())
        mro = cls.__mro__
        assert mro[mro.index(DeclaredMCPClient) + 1] is mcp_client.MCPStdioClient
