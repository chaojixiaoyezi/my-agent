"""MCP 生命周期的确定性交错与临时进程组件测试；不属于真实 TUI 或模型验收。"""
from __future__ import annotations

import json
import os
import threading
import time
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.tooling import mcp_client, mcp_protocol, mcp_registration, mcp_transport
from agent_py_agent.agent.tooling.mcp_client import MCPError, MCPStdioClient
from agent_py_agent.agent.tooling.mcp_registration import refresh_registered_mcp_client
from agent_py_agent.agent.tooling.process_registry import (
    ProcessTerminationReceipt,
    _process_instance_terminated,
    capture_process_birth_token,
    terminate_process_tree,
)
from agent_py_agent.agent.tooling.registry import ToolRegistry, ToolRegistryParams
from agent_py_agent.tests.test_mcp_client import _ECHO_SERVER, _HANG_ON_CALL_SERVER, _config


# LLM: 组件测试线程只运行传入的被测操作；异常原样留给断言，join 必须由测试显式完成。
# 函数用途: 记录并发操作的返回值和错误，避免线程内断言失败被测试忽略。
def _run_thread(action):
    values, errors = [], []

    def invoke():
        try:
            values.append(action())
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=invoke, daemon=True)
    thread.start()
    return thread, values, errors


def _joined_failure(thread, errors, code="MCP_CONNECTION_CLOSED"):
    thread.join(5)
    assert not thread.is_alive()
    assert len(errors) == 1 and isinstance(errors[0], MCPError)
    assert errors[0].code == code


@pytest.mark.parametrize("queue", ["request_lock", "write_lock"])
def test_stop_rejects_queued_request_without_sending(queue, tmp_path):
    observed = tmp_path / "calls"
    script = _ECHO_SERVER.replace('            params = req.get("params") or {}',
        f'            open({str(observed)!r}, "a").write("called\\n")\n'
        '            params = req.get("params") or {}')
    client = MCPStdioClient(_config(script))
    transport = client.start()
    lock = getattr(transport, queue)
    lock.acquire()
    try:
        thread, _, errors = _run_thread(lambda: client.call_tool("echo", {"text": "old"}))
        receipt = client.stop()
        assert receipt.confirmed
        _joined_failure(thread, errors)
        assert errors[0].effect_outcome == "not_started"
        assert not observed.exists()
        assert transport.inbox.pending == set()
        for action in (client.start, client.reconnect):
            with pytest.raises(MCPError, match="关闭"):
                action()
    finally:
        lock.release()
        client.stop()


def test_old_queue_cannot_send_to_reconnected_transport():
    client = MCPStdioClient(_config(_ECHO_SERVER))
    old = client.start()
    old.request_lock.acquire()
    captured = threading.Event()
    try:
        def invoke_old():
            selected = client.connection()
            captured.set()
            return selected.request("tools/call", {"name": "echo", "arguments": {"text": "old"}}, timeout=10)
        thread, _, errors = _run_thread(invoke_old)
        assert captured.wait(2)
        assert client.disconnect(transport=old).confirmed
        new = client.reconnect()
        assert new is not old
        old.request_lock.release()
        _joined_failure(thread, errors)
        assert errors[0].effect_outcome == "not_started"
        old.inbox.dispatch({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
        assert not new.inbox.tools_changed
        assert client.disconnect(transport=old).confirmed
        assert client.call_tool("echo", {"text": "new"})["content"] == "new"
    finally:
        if old.request_lock.locked():
            old.request_lock.release()
        client.stop()


def test_unknown_cleanup_keeps_original_transport_and_forbids_spawn(monkeypatch):
    client = MCPStdioClient(_config(_ECHO_SERVER))
    original = client.start()
    try:
        original.revoke()
        with monkeypatch.context() as patch:
            patch.setattr(original, "terminate", lambda **kw: ProcessTerminationReceipt("unconfirmed", False, None, 1))
            patch.setattr(client, "_start_transport", lambda: pytest.fail("unknown cleanup started a new process"))
            with pytest.raises(MCPError) as error:
                client.reconnect()
            assert error.value.code == "MCP_CLEANUP_UNKNOWN"
            assert client._transport is original
            assert not client.is_closed()
    finally:
        assert client.stop().confirmed


def test_cleanup_exception_is_unknown_and_keeps_original_identity(monkeypatch):
    client = MCPStdioClient(_config(_ECHO_SERVER))
    original = client.start()
    original_terminate = mcp_transport.terminate_process_tree
    try:
        with monkeypatch.context() as patch:
            def fail(*args, **kwargs):
                raise OSError("controlled cleanup failure")
            patch.setattr(mcp_transport, "terminate_process_tree", fail)
            receipt = client.disconnect(transport=original)
            assert not receipt.confirmed and receipt.method == "cleanup_error"
            with pytest.raises(MCPError) as error:
                client.reconnect()
            assert error.value.code == "MCP_CLEANUP_UNKNOWN"
            assert client._transport is original
    finally:
        # 组件故障注入结束后仅清理测试持有的固定进程；不把此收尾算作被测路径成功。
        original_terminate(original.binding.process.pid, original.binding.process,
                           expected_birth_token=original.binding.birth_token, grace_seconds=0)
        client.stop()


@pytest.mark.parametrize("initial", [False, True])
def test_discovery_cleanup_failure_does_not_break_other_servers(monkeypatch, initial):
    class FaultyClient:
        config = SimpleNamespace(name="broken")
        tools_changed = False

        def __init__(self):
            self.transport = object()
            self.cleaned = []

        def is_running(self):
            return False

        def is_closed(self):
            return False

        def start(self):
            return self.transport

        def reconnect(self):
            return self.transport

        def list_tools(self, *, transport):
            assert transport is self.transport
            raise MCPError("discovery failed")

        def disconnect(self, *, transport):
            self.cleaned.append(transport)
            raise OSError("cleanup failed")

    broken = FaultyClient()
    registry = object.__new__(ToolRegistry)
    registry.tools = {}
    registry._mcp_clients = [broken]
    registry._mcp_retry_state = {}
    registry._mcp_prepare_lock = threading.Lock()
    registry._mcp_closed = threading.Event()
    registry._construction_params = SimpleNamespace(plugin_owner=None)
    if initial:
        real_client = mcp_registration.MCPStdioClient
        monkeypatch.setattr(mcp_registration, "MCPStdioClient", lambda config: broken if config.name == "broken" else real_client(config))
        clients = mcp_registration.register_mcp_servers(registry, {
            name: {"command": _config(_ECHO_SERVER).command, "args": ["-c", _ECHO_SERVER]}
            for name in ("broken", "healthy")
        })
        try:
            assert clients[0] is broken and clients[1].is_running()
            assert "mcp__healthy__echo" in registry.tools
        finally:
            clients[1].stop()
    else:
        registry.prepare_for_run()
        assert registry._mcp_retry_state[id(broken)][0] == 1
    assert broken.cleaned == [broken.transport]


def test_dead_transport_is_not_reaped_by_availability_before_tree_cleanup():
    client = MCPStdioClient(_config(_ECHO_SERVER))
    original = client.start()
    try:
        original.binding.process.kill()
        assert original.inbox.closed.wait(3)
        assert not client.is_running()
        assert original.binding.process.returncode is None
        new = client.reconnect()
        assert new is not original and original.binding.process.returncode is not None
        assert client.call_tool("echo", {"text": "recovered"})["content"] == "recovered"
    finally:
        client.stop()


@pytest.mark.skipif(os.name == "nt", reason="POSIX 未回收组长及进程组的组件验证")
def test_exited_group_leader_keeps_owned_child_cleanup_identity():
    script = _ECHO_SERVER.replace('            params = req.get("params") or {}', '''            import os, subprocess
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": str(child.pid)}]}})
            sys.exit(0)
            params = req.get("params") or {}''')
    client = MCPStdioClient(_config(script))
    transport = client.start()
    child_pid, birth = 0, ""
    try:
        child_pid = int(client.call_tool("echo", {})["content"])
        birth = capture_process_birth_token(child_pid)
        assert birth and transport.inbox.closed.wait(3)
        assert transport.binding.process.returncode is None
        receipt = client.stop()
        assert receipt.confirmed and receipt.observed_processes >= 2
        assert _process_instance_terminated(child_pid, birth)
    finally:
        client.stop()
        if birth and not _process_instance_terminated(child_pid, birth):
            terminate_process_tree(child_pid, None, expected_birth_token=birth, grace_seconds=0)


def test_explicit_stop_before_start_permanently_prevents_spawn(monkeypatch):
    client = MCPStdioClient(_config(_ECHO_SERVER))
    assert client.stop().confirmed
    monkeypatch.setattr(client, "_start_transport", lambda: pytest.fail("closed client started"))
    with pytest.raises(MCPError):
        client.start()
    with pytest.raises(MCPError):
        client.reconnect()


def test_stop_during_spawn_returns_unknown_then_cleans_returning_candidate(monkeypatch):
    client = MCPStdioClient(_config(_ECHO_SERVER))
    spawned, release = threading.Event(), threading.Event()
    original_popen = mcp_client.subprocess.Popen
    processes = []

    def delayed_spawn(argv, **kwargs):
        process = original_popen(argv, **kwargs)
        if argv[0] == client.config.command:
            processes.append(process)
            spawned.set()
            assert release.wait(5)
        return process

    with monkeypatch.context() as patch:
        patch.setattr(mcp_client.subprocess, "Popen", delayed_spawn)
        thread, _, errors = _run_thread(client.start)
        try:
            assert spawned.wait(3)
            before = time.monotonic()
            receipt = client.stop()
            assert time.monotonic() - before < 0.5
            assert not receipt.confirmed and receipt.method == "launch_pending"
        finally:
            release.set()
            _joined_failure(thread, errors)
            assert client.stop().confirmed
    assert len(processes) == 1 and processes[0].returncode is not None


def test_stop_between_handshake_and_publication_cannot_restore_ready(monkeypatch):
    client = MCPStdioClient(_config(_ECHO_SERVER))
    ready, release = threading.Event(), threading.Event()
    handshake = client._handshake

    def delayed_handshake(transport):
        handshake(transport)
        ready.set()
        assert release.wait(5)

    monkeypatch.setattr(client, "_handshake", delayed_handshake)
    thread, _, errors = _run_thread(client.start)
    try:
        assert ready.wait(3)
        assert client.stop().confirmed
    finally:
        release.set()
        _joined_failure(thread, errors)
        client.stop()
    assert client.is_closed() and not client.is_running()


def test_stop_wakes_inflight_call_and_closes_owned_streams(monkeypatch):
    client = MCPStdioClient(_config(_HANG_ON_CALL_SERVER, timeout=30))
    transport = client.start()
    sent = threading.Event()
    send = transport.send

    def observed_send(message, **kwargs):
        send(message, **kwargs)
        if message.get("method") == "tools/call":
            sent.set()

    monkeypatch.setattr(transport, "send", observed_send)
    thread, _, errors = _run_thread(lambda: client.call_tool("slow", {}))
    try:
        assert sent.wait(3)
        assert client.stop().confirmed
        _joined_failure(thread, errors)
        assert errors[0].effect_outcome == "unknown"
        assert not transport.reader.is_alive() and not transport.stderr_reader.is_alive()
        process = transport.binding.process
        assert process.stdin.closed and process.stdout.closed and process.stderr.closed
        assert client.stop().confirmed
    finally:
        client.stop()


def test_list_result_cannot_publish_after_stop(monkeypatch):
    client = MCPStdioClient(_config(_ECHO_SERVER))
    transport = client.start()
    registry = SimpleNamespace(tools={"core": object()})
    old_catalog = registry.tools
    list_tools = client.list_tools

    def stopped_list(**kwargs):
        tools = list_tools(**kwargs)
        client.stop()
        return tools

    monkeypatch.setattr(client, "list_tools", stopped_list)
    with pytest.raises(MCPError):
        refresh_registered_mcp_client(registry, client, transport=transport)
    assert registry.tools is old_catalog


def test_old_discovery_cannot_publish_into_new_transport():
    client = MCPStdioClient(_config(_ECHO_SERVER))
    original = client.start()
    try:
        assert client.disconnect(transport=original).confirmed
        new = client.reconnect()
        published = []
        with pytest.raises(MCPError):
            client.publish_tools(original, lambda: published.append("stale"))
        assert not published
        assert client.publish_tools(new, lambda: "current") == "current"
    finally:
        client.stop()


def test_queue_clock_advance_never_passes_negative_timeout(monkeypatch):
    timestamps = iter([0.0, 2.0, 2.0])
    monkeypatch.setattr(mcp_protocol.time, "monotonic", lambda: next(timestamps))
    lock = threading.Lock()
    with pytest.raises(MCPError) as error:
        with mcp_protocol.bounded_mcp_lock(lock, 1.0, closed=threading.Event()):
            pytest.fail("expired queue entered")
    assert error.value.code == "MCP_TIMEOUT"
    assert not lock.locked()


# LLM: 所有路径由 tmp_path 提供，服务是临时组件；不连接真实用户 MCP 或修改真实 Gateway。
# 函数用途: 构造原 ToolRegistry，验证共享权限视图和真实本地 MCP 进程的关系。
def _registry(tmp_path):
    return ToolRegistry(ToolRegistryParams(
        workspace_root=tmp_path, max_chars=1000, max_entries=20, max_matches=10,
        web_max_chars=1000, http_timeout=10, catalog_limit=10, retrieval_limit=3,
        vector_search_enabled=False,
        mcp_servers={"echo": {"command": _config(_ECHO_SERVER).command, "args": ["-c", _ECHO_SERVER]}},
    ))


def test_close_shared_permission_view_prevents_other_view_reconnect(tmp_path):
    registry = _registry(tmp_path)
    view = registry.with_access_policy(access_mode="full-access", path_access_mode="full-access", owner_scope_root="")
    client = registry._mcp_clients[0]
    assert view is not registry and view._mcp_clients is registry._mcp_clients
    view.close_mcp_clients()
    registry.prepare_for_run()
    assert not registry._mcp_clients and not registry._mcp_retry_state
    assert client.is_closed()
    assert all(not name.startswith("mcp__") for name in registry.runtime_snapshot().available_tool_names)


def test_registry_close_interrupts_discovery_without_waiting_business_lock(tmp_path, monkeypatch):
    registry = _registry(tmp_path)
    client = registry._mcp_clients[0]
    transport = client.connection()
    transport.inbox.tools_changed = True
    requested = threading.Event()
    original_request = transport.request

    def stalled_discovery(method, params, **kwargs):
        if method == "tools/list":
            requested.set()
            assert transport.inbox.closed.wait(5)
            raise MCPError("closed during discovery", code="MCP_CONNECTION_CLOSED")
        return original_request(method, params, **kwargs)

    monkeypatch.setattr(transport, "request", stalled_discovery)
    thread, _, errors = _run_thread(registry.prepare_for_run)
    try:
        assert requested.wait(3)
        registry.close_mcp_clients()
        thread.join(3)
        assert not thread.is_alive() and not errors
        assert not registry._mcp_clients and not registry._mcp_retry_state
    finally:
        client.stop()


def test_complete_catalog_pages_keep_original_connection(monkeypatch):
    client = MCPStdioClient(_config(_ECHO_SERVER))
    transport = client.start()
    pages = []

    def request(method, params, **kwargs):
        pages.append(params)
        if not params:
            return {"tools": [], "nextCursor": "next"}
        assert client.disconnect(transport=transport).confirmed
        client.reconnect()
        return {"tools": []}

    monkeypatch.setattr(transport, "request", request)
    try:
        registry = SimpleNamespace(tools={"core": object()})
        with pytest.raises(MCPError):
            refresh_registered_mcp_client(registry, client, transport=transport)
        assert pages == [{}, {"cursor": "next"}]
        assert list(registry.tools) == ["core"]
        assert json.loads(json.dumps(client.server_info))["name"] == "echo-server"
    finally:
        client.stop()
