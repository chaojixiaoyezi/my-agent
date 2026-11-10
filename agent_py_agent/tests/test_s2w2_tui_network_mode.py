"""Focused network transport tests, including real loopback SSE reconnects (not external reachability)."""
from __future__ import annotations

import builtins
import gc
import io
import json
import os
import socket
import threading
import time
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.gateway_parts.business_stream_pool import BusinessStreamPool
from agent_py_agent.agent.gateway_parts.http_service import GatewayHTTPServer
from agent_py_agent.agent.gateway_parts.paths import gateway_chunk_path, gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.progress_text_delta import TextDeltaBuffer
from agent_py_agent.agent.gateway_parts.queue_service import archive_request
from agent_py_agent.agent.gateway_parts.request_event_hub import RequestEventHub, bind_hub
from agent_py_agent.agent.gateway_parts.request_event_routes import encode_sse
from agent_py_agent.cli.chat_parts.gateway_client import (
    GatewayChunkPollRequest,
    NetworkGatewayObserver,
    network_gateway_observer,
)


# LLM: HTTP tests own disposable queue roots/listeners and close their latest restarted instance; no daemon outside this test is modified.
# 函数用途: 准备真实本机HTTP入口，允许在同路径同端口重建监听器和中心。
@pytest.fixture
def live_gateway(tmp_path):
    paths = gateway_paths_from_root(tmp_path / "gateway")
    for folder in (paths.inbox, paths.processing, paths.done, paths.failed, paths.responses, paths.terminal):
        folder.mkdir(parents=True, exist_ok=True)
    state = {"paths": paths}
    def start(*, ring=8, port=0, streams=2):
        server = GatewayHTTPServer(port, paths)
        server.request_event_hub = RequestEventHub(ring_events=ring, topic_capacity=32, subscribers_per_topic=8,
            idle_seconds=1, fallback_poll_seconds=0.05)
        bind_hub(paths, server.request_event_hub)
        server.start()
        pool = BusinessStreamPool(max_streams=streams, per_owner_max=streams, write_timeout_seconds=0.2)
        pool.start()
        server.business_stream_pool = pool
        state.update(server=server, pool=pool, port=server.server.server_address[1])
        return server
    state["start"] = start
    yield state
    if "server" in state:
        state["server"].stop()


# LLM: Generate genuine committed text-delta events through the production buffering/redaction layer, not hand-made transport-only fragments.
# 函数用途: 生成超过2万字的真实TextDeltaBuffer事件和准确原文。
def text_events():
    text = ("山河远阔烟火人间。" * 3000)[:20000]
    buffer = TextDeltaBuffer(holdback_chars=32, flush_chars=32)
    events = buffer.begin("model-call")
    for offset in range(0, len(text), 160):
        events.extend(buffer.append(text[offset:offset + 160], model_call_id="model-call"))
    events.extend(buffer.finish("model-call", text=text))
    assert "".join(str(e.get("text", "")) for e in events if e["kind"] == "assistant_text_delta") == text
    return text, events


# LLM: Restrict filesystem assertions to the actual client thread; server threads must still read their authoritative files.
# 函数用途: 禁止网络客户端读取服务器目录或G1文件，同时允许真实服务端读取队列。
def prohibit_client_files(monkeypatch, root):
    client = threading.get_ident()
    original = Path.open
    observed = []
    def is_client():
        return (threading.get_ident() == client
                or threading.current_thread().name.startswith("r2-network-client"))
    def guarded(path, *args, **kwargs):
        if is_client() and (path.is_relative_to(root) or "credential" in path.name):
            observed.append(str(path))
            raise AssertionError("network client attempted a server/G1 file read")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", guarded)
    original_open = builtins.open
    def guarded_open(file, *args, **kwargs):
        if isinstance(file, (str, Path)):
            path = Path(file)
            if is_client() and (path.is_relative_to(root) or "credential" in path.name):
                observed.append(str(path))
                raise AssertionError("network client attempted a server/G1 open")
        return original_open(file, *args, **kwargs)
    monkeypatch.setattr(builtins, "open", guarded_open)
    return observed


# LLM: Resume uses a durable row id after a genuine closed HTTP response. Ring overflow and listener/hub restart cannot select an offset.
# 函数用途: 通过真实HTTP断连/重连验证2万字无漏无重，并在客户端线程禁止任何本地服务器/G1读取。
@pytest.mark.parametrize("ring", [8, 1024])
@pytest.mark.parametrize("restart", [False, True])
def test_real_sse_twenty_thousand_chars_reconnect(live_gateway, monkeypatch, ring, restart):
    state = live_gateway
    paths = state["paths"]
    text, events = text_events()
    record = paths.processing / "r.json"
    record.write_text(json.dumps({"id": "r", "execution_attempt_id": "attempt-a", "user_id": "alice", "channel": "chat"}))
    gateway_chunk_path(paths, "r").write_text("".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events))
    archived = archive_request(paths, record, paths.done, "r", terminal_response={"ok": True, "response": text,
        "id": "r", "request_id": "r", "user_id": "alice", "channel": "chat", "execution_attempt_id": "attempt-a"})
    assert archived.disposition.value in {"committed", "already_committed"}, archived
    server = state["start"](ring=ring)
    for _ in range(ring + 9):
        server.request_event_hub.publish(("request", "r"), "chunk", "r", {})
    import agent_py_agent.agent.common.authenticated_http as transport
    actual_open = transport.open_authenticated_request
    opened, consumed = [], []
    class Disconnect:
        def __init__(self, raw):
            self.raw, self.frames = raw, 0
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.raw.close()
        def readline(self, size):
            if self.frames >= 12:
                self.raw.close()
                if restart:
                    port = state["port"]
                    state["server"].stop()
                    state["start"](ring=ring, port=port)
                    assert state["server"].request_event_hub.publish(("request", "r"), "chunk", "r", {}) == 1
                return b""
            line = self.raw.readline(size)
            if line in (b"\n", b"\r\n"):
                self.frames += 1
            return line
    def opener(request, *, timeout):
        opened.append(request)
        raw = actual_open(request, timeout=timeout)
        return Disconnect(raw) if len(opened) == 1 else raw
    monkeypatch.setattr(transport, "open_authenticated_request", opener)
    agent = SimpleNamespace(config=SimpleNamespace(gateway_port=state["port"], gateway_request_timeout=15, gateway_auth_token=""))
    observer = NetworkGatewayObserver(agent, f"http://127.0.0.1:{state['port']}")
    def consume(event):
        consumed.append(event)
        return True
    request = GatewayChunkPollRequest(gateway_chunk_path(paths, "r"), paths.terminal / "r.json", time.time() + 15,
        lambda text: None, [0], [0], on_event=consume)
    observed = prohibit_client_files(monkeypatch, paths.root)
    result = observer(request)
    assert result["ok"] is True
    assert result["response"] == text
    assert len(opened) == 2
    resumed = opened[1].get_header("Last-event-id")
    assert resumed and resumed.startswith("v1:r:attempt-a:")
    assert "".join(str(e.get("text", "")) for e in consumed if e["kind"] == "assistant_text_delta") == text
    sequences = [e["seq"] for e in consumed if e["kind"] == "assistant_text_delta"]
    assert sequences == list(range(len(sequences)))
    assert not observed


# LLM: Incomplete UTF-8 and blank-line-less frames must have no cursor effect across reconnect.
# 函数用途: 对半帧注入断连，核对恢复请求不带未消费的游标。
def test_half_utf8_frame_does_not_advance(monkeypatch, tmp_path):
    import agent_py_agent.agent.common.authenticated_http as transport
    first = encode_sse(("progress", {"event": {"kind": "assistant_commentary", "text": "中文"}}, "v1:r:a:1:99"))
    partial = first[:first.index("中".encode()) + 1]
    terminal = encode_sse(("terminal", {"result": {"ok": True, "response": "done"}}, "v1:r:a:1:99"))
    replies = [io.BytesIO(partial), io.BytesIO(first + terminal)]
    headers, events = [], []
    def opener(request, *, timeout):
        headers.append(dict(request.header_items()))
        return replies.pop(0)
    monkeypatch.setattr(transport, "open_authenticated_request", opener)
    observer = NetworkGatewayObserver(SimpleNamespace(config=SimpleNamespace(gateway_request_timeout=3)), "http://example.invalid")
    result = observer(GatewayChunkPollRequest(tmp_path / "r.chunks.jsonl", tmp_path / "r.json", time.time()+3,
        lambda t: None, [0], on_event=lambda e: events.append(e)))
    assert result["response"] == "done"
    assert len(events) == 1
    assert all("Last-event-id" not in value for value in headers)


# LLM: Busy/failed authentication/repeated connection failures must permanently choose HTTP for this request, not server-file polling.
# 函数用途: 分别反证503、刷新失败与重复断连后的本地文件降级。
@pytest.mark.parametrize("failure", ["busy", "auth", "connect"])
def test_http_only_fallback_for_rest_of_request(monkeypatch, tmp_path, failure):
    import agent_py_agent.agent.common.authenticated_http as transport
    calls, polled = [], []
    def opener(request, *, timeout):
        calls.append(request)
        if failure == "connect":
            raise OSError("disconnected")
        raise urllib.error.HTTPError(request.full_url, 503 if failure == "busy" else 401, "denied", {}, io.BytesIO())
    monkeypatch.setattr(transport, "open_authenticated_request", opener)
    agent = SimpleNamespace(config=SimpleNamespace(gateway_request_timeout=3), refresh_gateway_network_credentials=lambda: False)
    observer = NetworkGatewayObserver(agent, "http://example.invalid")
    def request_json(method, path, payload=None, **kwargs):
        polled.append(path)
        return (200, {"events": [], "next": 0}) if path.startswith("/progress/") else (200, {"ok": True, "response": "result"})
    monkeypatch.setattr(observer, "request_json", request_json)
    observed = prohibit_client_files(monkeypatch, tmp_path)
    result = observer(GatewayChunkPollRequest(tmp_path/"r.chunks.jsonl", tmp_path/"r.json", time.time()+3, lambda _: None, [0]))
    assert result["response"] == "result"
    assert observer._http_only
    assert len(calls) == (2 if failure == "connect" else 1)
    assert len(polled) == 2
    assert not observed


# LLM: Explicit network mode does not depend on loopback reachability or local file existence.
# 函数用途: 验证远端地址自动选HTTP，本机默认不改，强制开关生效。
def test_network_mode_selection(monkeypatch):
    agent = SimpleNamespace(config=SimpleNamespace(gateway_port=42))
    monkeypatch.delenv("MY_AGENT_TUI_NETWORK", raising=False)
    monkeypatch.delenv("MY_AGENT_GATEWAY_URL", raising=False)
    assert network_gateway_observer(agent) is None
    agent.gateway_url = "https://remote.example"
    assert isinstance(network_gateway_observer(agent), NetworkGatewayObserver)
    agent.gateway_url = "http://localhost:42"
    monkeypatch.setenv("MY_AGENT_TUI_NETWORK", "1")
    assert isinstance(network_gateway_observer(agent), NetworkGatewayObserver)


# LLM: 服务端/客户端独立owner目录与真实认证监听器属于此fixture；原stop关闭监听及流池后必须恢复全部FD。
# 函数用途: 用隔离服务端为启动、认证、审批和连续交互提供真实HTTP入口，并核对teardown无泄漏。
@pytest.fixture
def startup_gateway(tmp_path, monkeypatch, request):
    """真实服务端与客户端目录独立；仅模型执行由各回归另行驱动。"""
    from agent_py_agent.agent.auth.manager import AuthManager
    from agent_py_agent.agent.auth.middleware import AuthMiddleware
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.gateway_parts.http_service import GatewayHTTPServerParams
    from agent_py_agent.agent.gateway_parts.io import write_json_file
    from agent_py_agent.agent.settings import AgentConfig

    # 原HTTPServer.server_bind也调用getfqdn；macOS解析器首次保留进程级FD。
    # 独立库调用已反证其与业务无关，先完成初始化并回收旧对象，再检查启动/关闭无净增长。
    socket.getfqdn("127.0.0.1")
    gc.collect()
    before_fd = _fd_count()
    server_home = tmp_path / "server-home"
    client_home = tmp_path / "client-home"
    token = "r2-disposable-test-deployment-token"
    monkeypatch.setenv("MY_AGENT_HOME", str(server_home))
    host = SimpleAgent(AgentConfig(my_agent_home=str(server_home), model_backend="echo",
                                  memory_curator_enabled=False,
                                  gateway_native_stream_max=getattr(request, "param", 0)),
                       server_home / "owners" / "local" / "main" / "workspace")
    paths = gateway_paths_from_root(tmp_path / "server-gateway")
    for folder in (paths.inbox, paths.processing, paths.done, paths.failed, paths.responses, paths.terminal):
        folder.mkdir(parents=True, exist_ok=True)
    write_json_file(paths.state, {"status": "running", "started_at": time.time()})
    server = GatewayHTTPServer(0, paths, params=GatewayHTTPServerParams(
        agent=host, auth_middleware=AuthMiddleware(AuthManager(), auth_token=token)))
    server.start()
    monkeypatch.setenv("MY_AGENT_HOME", str(client_home))
    monkeypatch.setenv("MY_AGENT_TUI_NETWORK", "1")
    monkeypatch.setenv("MY_AGENT_GATEWAY_URL", f"http://127.0.0.1:{server.server.server_address[1]}")
    probe = NetworkGatewayObserver(SimpleNamespace(config=host.config),
                                   f"http://127.0.0.1:{server.server.server_address[1]}")
    status, body = probe.request_json("GET", "/status", total_deadline_seconds=1)
    assert status == 200 and body["status"] == "running"
    monkeypatch.delenv("MY_AGENT_RUNTIME_CONFIG", raising=False)
    monkeypatch.delenv("MY_AGENT_RUNTIME_CONFIG_LAYERS", raising=False)
    config = tmp_path / "network-client.yaml"
    config.write_text(f'gateway_port: 1\ngateway_auth_token: "{token}"\n'
                      'gateway_ready_timeout_seconds: 0.2\ntui_follow_gateway_upgrade: false\n', encoding="utf-8")
    try:
        yield SimpleNamespace(server=server, host=host, paths=paths, config=config, client_home=client_home,
                              server_home=server_home, token=token)
    finally:
        server.stop()
        _assert_fd_restored(before_fd)


def _startup_args(gateway, *, plain=False):
    from agent_py_agent.cli.parser import build_parser
    return build_parser().parse_args(["--config", str(gateway.config), *(["--plain"] if plain else []),
                                      "chat", "--gateway"])


def test_network_factory_binds_before_local_credential_preflight(startup_gateway, monkeypatch):
    from agent_py_agent.cli import chat_client_context

    calls = []
    actual = chat_client_context.preflight_gateway_credential
    def local_preflight(agent):
        calls.append("local-credential")
        actual(agent)
    monkeypatch.setattr(chat_client_context, "preflight_gateway_credential", local_preflight)
    client = chat_client_context.make_gateway_chat_client(_startup_args(startup_gateway))
    status, body = client.get_gateway_json("/status", timeout=1)
    assert status == 200 and body["status"] == "running"
    assert calls == []
    assert client.home_paths.root == startup_gateway.client_home
    assert client.home_paths.root != startup_gateway.server_home


def test_plain_entry_network_startup_uses_real_status_without_local_pid(startup_gateway, monkeypatch):
    from agent_py_agent.cli import chat
    from agent_py_agent.cli.chat_parts import plain

    seen = []
    # 只替换终端输入与空闲worker；启动、会话、退出及认证HTTP保持真实。
    monkeypatch.setattr(plain, "_start_plain_worker", lambda *_: None)
    monkeypatch.setattr(plain, "_read_user_input", lambda *_: seen.append("input") or "/exit")
    assert chat.cmd_chat(_startup_args(startup_gateway, plain=True)) == 0
    assert seen == ["input"]


def test_real_prompt_toolkit_network_preflight_reaches_workers(startup_gateway, monkeypatch):
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input.defaults import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from agent_py_agent.cli import chat
    from agent_py_agent.cli.chat_parts import tui

    monkeypatch.setattr(chat, "_has_prompt_toolkit", lambda: True)
    ready = []
    def started(*, params):
        ready.append(params.tui_runtime.store.snapshot())
        params.app_ref[0].exit(result=0)
        params.stop_event.set()
    monkeypatch.setattr(tui, "_start_worker_threads", started)
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        result = chat.cmd_chat(_startup_args(startup_gateway))
    assert result == 0
    assert len(ready) == 1
    assert not ready[0].active_blocks


def test_bound_network_post_retains_group_owner(startup_gateway, monkeypatch):
    from agent_py_agent.agent.gateway_parts import http_handlers
    from agent_py_agent.agent.gateway_parts.control_service import _scope_request_payload
    from agent_py_agent.agent.gateway_parts.request_worker import _owner_from_request
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
    from agent_py_agent.cli import chat_client_context

    group = OwnerIdentity.provider_group("feishu", "room-r2")
    monkeypatch.setattr(chat_client_context, "owner_identity_from_config", lambda _config: group)
    client = chat_client_context.make_gateway_chat_client(_startup_args(startup_gateway))
    scopes = []
    actual_scope = http_handlers._gateway_control_scope

    def record_scope(handler, payload, **kwargs):
        scope = actual_scope(handler, payload, **kwargs)
        scopes.append(scope)
        return scope

    monkeypatch.setattr(http_handlers, "_gateway_control_scope", record_scope)
    history = client.request_chat_history("group-session-r2", max_turns=1)
    assert history.get("ok") is True, history
    assert scopes
    assert _owner_from_request(startup_gateway.host, _scope_request_payload(scopes[-1])) == group


def test_network_readiness_does_not_probe_after_budget(startup_gateway, monkeypatch):
    from agent_py_agent.agent.gateway_parts.io import write_json_file
    from agent_py_agent.cli import chat_client_context
    from agent_py_agent.cli.chat_parts import gateway_client

    write_json_file(startup_gateway.paths.state, {"status": "starting", "started_at": time.time()})
    client = chat_client_context.make_gateway_chat_client(_startup_args(startup_gateway))
    observer = client.wait_for_gateway_readiness.__self__
    clock = [100.0]
    calls = []
    actual_request = observer.request_json

    def request(method, path, payload=None, **kwargs):
        calls.append(clock[0])
        return actual_request(method, path, payload, **kwargs)

    def advance(seconds):
        clock[0] += seconds

    monkeypatch.setattr(observer, "request_json", request)
    monkeypatch.setattr(gateway_client, "time", SimpleNamespace(
        monotonic=lambda: clock[0], time=time.time, sleep=advance))
    outcome = observer.wait_for_readiness(None, timeout=0.2)
    assert not outcome.ready
    assert outcome.reason == "GATEWAY_START_TIMEOUT"
    assert calls == [100.0]
    assert outcome.elapsed_seconds == 0.2


def _fd_count():
    folder = "/dev/fd" if Path("/dev/fd").is_dir() else "/proc/self/fd"
    if not Path(folder).is_dir():
        pytest.skip("此平台未公开当前进程FD计数")
    with os.scandir(folder) as entries:
        return sum(1 for _ in entries)


def _wait_until(predicate, *, timeout=10):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "在有界时间内未观察到所需状态"
        threading.Event().wait(0.01)


def _assert_fd_restored(before):
    try:
        _wait_until(lambda: _fd_count() <= before, timeout=5)
    except AssertionError:
        after = _fd_count()
        raise AssertionError(f"FD未恢复: before={before}, after={after}, fd_delta={after - before}; "
                             f"threads={[thread.name for thread in threading.enumerate()]}") from None
    assert _fd_count() <= before


def test_fd_restoration_allows_unrelated_descriptor_cleanup(tmp_path):
    gc.collect()
    with (tmp_path / "prior-test-file").open("wb"):
        before = _fd_count()
    # 批内旧对象的 FD 已关闭时，负增量表示资源减少，不能报成泄漏。
    _assert_fd_restored(before)


@pytest.mark.parametrize("status,ready,reason", [
    ("running", True, "GATEWAY_READY"),
    ("starting", False, "GATEWAY_START_TIMEOUT"),
    ("failed", False, "GATEWAY_START_FAILED"),
    ("interrupted", False, "GATEWAY_START_FAILED"),
    ("http_server_error", False, "GATEWAY_START_FAILED"),
    ("RUNNING", False, "GATEWAY_NOT_READY"),
])
def test_readiness_uses_real_status_and_original_outcome(startup_gateway, monkeypatch, status, ready, reason):
    from agent_py_agent.agent.gateway_parts.io import write_json_file
    from agent_py_agent.agent.gateway_parts.status_rendering import GatewayReadinessWait
    from agent_py_agent.cli.chat_client_context import make_gateway_chat_client

    write_json_file(startup_gateway.paths.state, {"status": status, "started_at": time.time()})
    client = make_gateway_chat_client(_startup_args(startup_gateway))
    observed = prohibit_client_files(monkeypatch, startup_gateway.paths.root)
    before = _fd_count()
    observations = []
    outcome = client.wait_for_gateway_readiness(None, timeout=0, on_observation=observations.append)
    assert isinstance(outcome, GatewayReadinessWait)
    assert outcome.ready is ready and outcome.reason == reason
    assert outcome.polls == 1 and outcome.last == observations[0]
    assert not observed
    _assert_fd_restored(before)


@pytest.mark.parametrize("token", ["", "invalid-disposable-r2-token"])
def test_public_readiness_is_not_authenticated_admission(startup_gateway, token):
    from agent_py_agent.cli.chat_client_context import make_gateway_chat_client

    client = make_gateway_chat_client(_startup_args(startup_gateway))
    client.config.gateway_auth_token = token
    before = _fd_count()
    assert client.wait_for_gateway_readiness(None, timeout=1).ready
    status, _body = client.post_gateway_json("/ask", {
        "goal": "不应被未认证受理", "conversation_id": "auth-denial-r2",
        "metadata": {"message_id": "r2-denied"},
    }, timeout=1)
    assert status in {401, 403}
    assert not list(startup_gateway.paths.inbox.glob("*.json"))
    _assert_fd_restored(before)


@pytest.mark.parametrize("plain", [False, True])
def test_unavailable_endpoint_exits_before_input_or_workers(startup_gateway, monkeypatch, plain):
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input.defaults import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from agent_py_agent.cli import chat
    from agent_py_agent.cli.chat_parts import tui

    monkeypatch.setattr(chat, "_has_prompt_toolkit", lambda: True)
    monkeypatch.setattr(chat, "run_plain", lambda *_: pytest.fail("不可用端点不得进入输入"))
    monkeypatch.setattr(tui, "_start_worker_threads", lambda **_: pytest.fail("不可用端点不得启动worker"))
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        monkeypatch.setenv("MY_AGENT_GATEWAY_URL", f"http://127.0.0.1:{reserved.getsockname()[1]}")
        before = _fd_count()
        started = time.monotonic()
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            assert chat.cmd_chat(_startup_args(startup_gateway, plain=plain)) == 2
        assert time.monotonic() - started < 5
        _assert_fd_restored(before)


def test_local_factory_still_performs_original_credential_preflight(startup_gateway, monkeypatch):
    from agent_py_agent.cli import chat_client_context

    monkeypatch.delenv("MY_AGENT_TUI_NETWORK", raising=False)
    monkeypatch.delenv("MY_AGENT_GATEWAY_URL", raising=False)
    calls = []
    original = chat_client_context.preflight_gateway_credential

    def preflight(client):
        calls.append(client)
        original(client)

    monkeypatch.setattr(chat_client_context, "preflight_gateway_credential", preflight)
    client = chat_client_context.make_gateway_chat_client(_startup_args(startup_gateway))
    assert calls == [client]
    assert not hasattr(client, "wait_for_gateway_readiness")


def test_group_permission_uses_same_owner_as_group_history(startup_gateway, monkeypatch):
    from agent_py_agent.agent.contracts.tool_approval import (
        ToolApprovalDecision,
        build_tool_approval_request,
    )
    from agent_py_agent.agent.gateway_parts.admin_control_service import pending_tool_approvals
    from agent_py_agent.agent.gateway_parts.control_service import (
        GatewayControlScope,
        _scope_request_payload,
    )
    from agent_py_agent.agent.gateway_parts.stream_writer import BufferedChunkStreamWriter
    from agent_py_agent.agent.tooling.runtime_contracts import ToolCall
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity
    from agent_py_agent.cli import chat_client_context

    group = OwnerIdentity.provider_group("feishu", "room-r2")
    monkeypatch.setattr(chat_client_context, "owner_identity_from_config", lambda _config: group)
    client = chat_client_context.make_gateway_chat_client(_startup_args(startup_gateway))
    observer = client.wait_for_gateway_readiness.__self__
    scope = GatewayControlScope("room-r2", "feishu", "group-approval-r2",
        metadata={"channel_chat_type": "group", "channel_chat_id": "room-r2"})
    request_id = "r2-group-approval"
    record = startup_gateway.paths.processing / f"{request_id}.json"
    record.write_text(json.dumps({**_scope_request_payload(scope), "id": request_id,
        "request_id": request_id, "execution_attempt_id": "r2-group-attempt",
        "lease_started_at": time.time(), "status": "processing"}), encoding="utf-8")
    request = build_tool_approval_request(ToolCall(call_id="r2-group-read", tool_name="read_file",
        arguments={"path": "r2-note.txt"}, source_protocol="native", schema_hash="sha256:r2",
        run_id="r2-group-run", turn_id=request_id, attempt_id="r2-group-attempt"),
        request_id=request_id, round_number=1, call_index=1,
        description="核对群内只读样本", grant_key="read_file:r2-group")
    writer = BufferedChunkStreamWriter(gateway_chunk_path(startup_gateway.paths, request_id),
        interactive_approvals=True, rich_transcript=True, delivery_channel="feishu")
    cancelled = threading.Event()
    decisions, errors = [], []

    def wait():
        try:
            decisions.append(writer.request_permission(request.to_dict(),
                cancellation_token=SimpleNamespace(is_cancelled=cancelled.is_set)))
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=wait, name="r2-group-approval", daemon=True)
    thread.start()
    try:
        _wait_until(lambda: bool(pending_tool_approvals(startup_gateway.host, startup_gateway.paths, scope)))
        observer.permission_sink(request.to_dict(), ToolApprovalDecision(
            request.permission_id, "approved", "群内只读核对").to_dict(), conversation_id=scope.conversation_id)
        thread.join(5)
        assert not thread.is_alive()
        assert decisions[0]["decision"] == "approved"
    finally:
        cancelled.set()
        thread.join(5)
        writer.close()
        assert not thread.is_alive()
        assert not errors, repr(errors)


@pytest.mark.parametrize("startup_gateway", [2], indirect=True)
def test_real_network_tui_stream_steer_approval_stop_new_next_turn(startup_gateway, monkeypatch):
    """模型时序可控；UI、worker、认证HTTP、控制、审批和规范终态均走原实现。"""
    from prompt_toolkit.application import create_app_session
    from prompt_toolkit.input.defaults import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from agent_py_agent.agent.backends.base import EchoBackend, ModelResponse
    from agent_py_agent.agent.concurrency.interrupt import (
        is_interrupted,
        register_interrupt_callback,
    )
    from agent_py_agent.agent.contracts.tool_approval import build_tool_approval_request
    from agent_py_agent.agent.gateway_parts import request_execution
    from agent_py_agent.agent.gateway_parts.io import read_json_file
    from agent_py_agent.agent.gateway_parts.request_worker import _process_gateway_requests
    from agent_py_agent.agent.gateway_parts.response_renderer import (
        read_gateway_terminal_response_file,
    )
    from agent_py_agent.agent.tooling.runtime_contracts import ToolCall
    from agent_py_agent.cli import chat
    from agent_py_agent.cli.chat_parts import tui

    state = startup_gateway
    note = state.host.root / "r2-note.txt"
    note.write_text("本轮只读样本", encoding="utf-8")
    ready, streaming, resume, approved, waiting_stop = (threading.Event() for _ in range(5))
    release, stopped, gateway_stop = (threading.Event() for _ in range(3))
    next_streaming, next_finish = threading.Event(), threading.Event()
    silent_streaming, silent_finish = threading.Event(), threading.Event()
    synthetic_secret = "sk-" + "SyntheticCredential" * 40
    workers, writers, controls, failures, results = [], [], [], [], []
    permission_posts = []
    received_events = []
    terminal_snapshots = {}
    terminal_ids = []
    generated = []
    active = [0, 0]
    lock = threading.Lock()

    class Writer(request_execution.BufferedChunkStreamWriter):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            writers.append(self)

    class Provider(EchoBackend):
        def generate(self, prompt, on_chunk=None, **kwargs):
            with lock:
                index = len(generated)
                generated.append(prompt + json.dumps(kwargs.get("messages", []), ensure_ascii=False))
                active[0] += 1
                active[1] = max(active)
            try:
                if index < 4:
                    assert on_chunk is not None
                if index == 0:
                    on_chunk("首轮正在生成。" * 100 + " " + synthetic_secret + "\n")
                    streaming.set()
                    assert resume.wait(15), "插话未到达"
                    return ModelResponse(text="首阶段完成", backend=self.name)
                if index == 1:
                    return ModelResponse(text="读取样本后继续", backend=self.name, tool_use_blocks=[{
                        "id": "r2-read-note", "name": "read_file", "input": {"path": str(note)},
                    }])
                if index == 2:
                    record, = state.paths.processing.glob("*.json")
                    payload = read_json_file(record)
                    request = build_tool_approval_request(ToolCall(
                        call_id="r2-permission", tool_name="read_file", arguments={"path": str(note)},
                        source_protocol="native", schema_hash="sha256:r2", run_id="r2-sequence",
                        turn_id=payload["id"], attempt_id=payload["execution_attempt_id"]),
                        request_id=payload["id"], round_number=2, call_index=1,
                        description="核对本轮只读样本", grant_key="read_file:r2")
                    decision = writers[-1].request_permission(request.to_dict())
                    assert decision["decision"] == "approved"
                    approved.set()
                    with register_interrupt_callback(release.set):
                        on_chunk("审批已收到，继续生成。" * 20)
                        waiting_stop.set()
                        assert release.wait(15), "未收到停止"
                        assert is_interrupted(), "测试清理不能代替业务停止"
                        stopped.set()
                        raise InterruptedError("首轮已停止")
                if index == 3:
                    # 超过原256字符安全holdback，才能验证终稿前候选而非等完成时补发。
                    on_chunk("下一轮完整完成。" * 100)
                    next_streaming.set()
                    assert next_finish.wait(15), "默认流式下一轮未释放"
                    return ModelResponse(text="下一轮完整完成。", backend=self.name)
                if on_chunk is not None:
                    on_chunk("静默模式候选不应可见。" * 100)
                silent_streaming.set()
                assert silent_finish.wait(15), "静默模式下一轮未释放"
                return ModelResponse(text="关闭流式后完整完成。", backend=self.name)
            finally:
                with lock:
                    active[0] -= 1

    original_start = tui._start_worker_threads
    original_json = NetworkGatewayObserver.request_json
    original_emit = NetworkGatewayObserver._emit

    def start(*, params):
        workers.append(params)
        original_start(params=params)
        ready.set()

    def request(observer, method, path, payload=None, **kwargs):
        result = original_json(observer, method, path, payload, **kwargs)
        if method == "POST" and path == "/control":
            controls.append((payload, result))
        if method == "POST" and path == "/client/agent-permission":
            permission_posts.append((payload, result))
        return result

    def emit(observer, request, event):
        received_events.append(event)
        return original_emit(observer, request, event)

    def gateway_work():
        try:
            while not gateway_stop.wait(0.01):
                processed = _process_gateway_requests(state.host, state.paths, worker_id="r2-sequence")
                if processed:
                    for path in state.paths.terminal.glob("*.json"):
                        terminal_snapshots.setdefault(path.stem, path.read_bytes())
        except BaseException as exc:
            failures.append(exc)

    monkeypatch.setattr(request_execution, "BufferedChunkStreamWriter", Writer)
    monkeypatch.setattr(tui, "_start_worker_threads", start)
    monkeypatch.setattr(NetworkGatewayObserver, "request_json", request)
    monkeypatch.setattr(NetworkGatewayObserver, "_emit", emit)
    monkeypatch.setattr(chat, "_has_prompt_toolkit", lambda: True)
    state.host.backend = Provider()
    gateway_thread = threading.Thread(target=gateway_work, name="r2-server-worker", daemon=True)
    ui_thread = None
    before_threads = set(threading.enumerate())
    before_fd = _fd_count()
    with create_pipe_input() as pipe:
        def client_work():
            try:
                with create_app_session(input=pipe, output=DummyOutput()):
                    results.append(chat.cmd_chat(_startup_args(state)))
            except BaseException as exc:
                failures.append(exc)

        ui_thread = threading.Thread(target=client_work, name="r2-network-client", daemon=True)
        try:
            with monkeypatch.context() as guarded:
                observed = prohibit_client_files(guarded, state.paths.root)
                gateway_thread.start()
                ui_thread.start()
                assert ready.wait(15), repr(failures)
                assert len(workers) == 1
                cfg = workers[0]
                runtime = cfg.tui_runtime
                assert not controls  # 全新TUI首轮没有显式切换verbose。
                pipe.send_text("开始首轮\r")
                assert streaming.wait(15), repr(failures)
                _wait_until(lambda: bool(cfg.running_request_id_ref[0]))
                first_id = cfg.running_request_id_ref[0]
                terminal_ids.append(first_id)
                _wait_until(lambda: any(event.get("kind") == "assistant_text_delta"
                    and "首轮正在生成" in str(event.get("text", "")) for event in received_events))
                # 默认流式仍只能显示宿主已净化候选，不能提前冻结为已采纳正文。
                assert "SyntheticCredential" not in json.dumps(received_events, ensure_ascii=False)
                assert any("首轮正在生成" in block.text
                    for block in runtime.store.snapshot().active_blocks)
                assert not any("首轮正在生成" in block.text
                    for block in runtime.store.snapshot().stable_blocks)
                # /btw必须等原后台快照发布实际执行绑定，收到流片段不等于控制目标已可用。
                _wait_until(lambda: bool(runtime.execution_target()), timeout=5)
                assert runtime.execution_target()["turn_id"] == first_id
                pipe.send_text("/btw 保留这条补充\r")
                _wait_until(lambda: any(body.get("kind") == "steer"
                    for _payload, (_status, body) in controls))
                steer_payload, (status, receipt) = controls[-1]
                assert status == 202 and receipt["delivery_status"] == "unknown"
                assert steer_payload["metadata"]["expected_turn_id"] == first_id
                resume.set()
                _wait_until(lambda: runtime.store.snapshot().permission is not None, timeout=15)
                permission_id = runtime.store.snapshot().permission.permission_id
                # 真正从审批快捷键批准；不能直接调adapter绕过原UI焦点归还步骤。
                pipe.send_text("y")
                assert approved.wait(10) and waiting_stop.wait(10), repr(failures)
                _wait_until(lambda: any(event.get("kind") == "assistant_text_reset"
                    and event.get("reason") == "superseded" for event in received_events))
                assert not any("首轮正在生成" in block.text
                    for block in runtime.store.snapshot().active_blocks)
                assert "保留这条补充" in generated[1]
                # 原首答因插话被superseded；只有工具轮实际提交的commentary可以成为公开正文。
                _wait_until(lambda: any("读取样本后继续" in block.text
                    for block in runtime.store.snapshot().stable_blocks))
                assert not any("首阶段完成" in block.text
                    for block in runtime.store.snapshot().stable_blocks)
                # permission_resolved是原私有chunk，不在HTTP公开白名单；核真实审批POST回执与原waiter，再等弹窗关闭。
                _wait_until(lambda: bool(permission_posts))
                assert len(permission_posts) == 1
                permission_payload, (permission_status, permission_receipt) = permission_posts[0]
                assert permission_status == 200 and permission_receipt["ok"]
                assert permission_payload["decision"]["permission_id"] == permission_id
                assert permission_payload["decision"]["decision"] == "approved"
                _wait_until(lambda: runtime.store.snapshot().permission is None)
                pipe.send_text("/stop\r")
                assert stopped.wait(15), repr((failures, controls, runtime.notice(),
                    runtime.store.snapshot().permission))
                _wait_until(lambda: not cfg.is_running_ref[0] and not runtime.store.snapshot().has_active_work,
                    timeout=15)
                assert runtime.store.snapshot().permission is None
                assert cfg.running_request_id_ref == [""]
                pipe.send_text("/new\r")
                _wait_until(lambda: any(body.get("kind") == "new" and body.get("ok")
                    for _payload, (_status, body) in controls))
                _wait_until(lambda: runtime.conversation_reset_generation == 1)
                pipe.send_text("下一轮消息\r")
                assert next_streaming.wait(15), repr(failures)
                _wait_until(lambda: bool(cfg.running_request_id_ref[0]))
                _wait_until(lambda: any("下一轮完整完成" in block.text
                    for block in runtime.store.snapshot().active_blocks))
                next_finish.set()
                _wait_until(lambda: any(block.text == "下一轮完整完成。"
                    for block in runtime.store.snapshot().stable_blocks), timeout=15)
                _wait_until(lambda: not cfg.is_running_ref[0] and not runtime.store.snapshot().has_active_work)
                assert sum(block.text == "下一轮完整完成。"
                    for block in runtime.store.snapshot().stable_blocks) == 1
                assert sum("下一轮完整完成" in block.text
                    for block in runtime.store.snapshot().stable_blocks) == 1
                pipe.send_text("/verbose off\r")
                _wait_until(lambda: any(body.get("kind") == "verbose" and body.get("ok")
                    for _payload, (_status, body) in controls))
                pipe.send_text("关闭流式后的消息\r")
                assert silent_streaming.wait(15), repr(failures)
                _wait_until(lambda: bool(cfg.running_request_id_ref[0]))
                assert not any("静默模式候选不应可见" in block.text
                    for block in runtime.store.snapshot().active_blocks)
                silent_finish.set()
                _wait_until(lambda: any(block.text == "关闭流式后完整完成。"
                    for block in runtime.store.snapshot().stable_blocks), timeout=15)
                _wait_until(lambda: not cfg.is_running_ref[0] and not runtime.store.snapshot().has_active_work)
                snapshot = runtime.store.snapshot()
                assert sum(block.text == "关闭流式后完整完成。" for block in snapshot.stable_blocks) == 1
                assert snapshot.permission is None and not snapshot.pending_steers and not snapshot.queued_inputs
                assert sum(block.text == "下一轮完整完成。" for block in snapshot.stable_blocks) == 1
                assert sum("下一轮完整完成" in block.text for block in snapshot.stable_blocks) == 1
                assert not any("静默模式候选不应可见" in block.text
                    or "SyntheticCredential" in block.text
                    for block in (*snapshot.active_blocks, *snapshot.stable_blocks))
                assert not any("静默模式候选不应可见" in str(event.get("text", ""))
                    for event in received_events)
                assert "SyntheticCredential" not in json.dumps(received_events, ensure_ascii=False)
                assert not cfg.pending_jobs_ref[0] and cfg.running_request_id_ref == [""]
                _wait_until(lambda: not runtime.notice(), timeout=5)
                pipe.send_text("/exit\r")
                ui_thread.join(10)
                assert not ui_thread.is_alive(), repr(failures)
                assert not observed
        finally:
            resume.set()
            release.set()
            next_finish.set()
            silent_finish.set()
            gateway_stop.set()
            if workers:
                cfg = workers[0]
                cfg.stop_event.set()
                cfg.refresh_stop.set()
                app = cfg.app_ref[0]
                if ui_thread.is_alive() and app.loop is not None and not app.loop.is_closed():
                    app.loop.call_soon_threadsafe(lambda: app.exit(result=0)
                        if app.future is not None and not app.future.done() else None)
            ui_thread.join(10)
            gateway_thread.join(10)
            assert not ui_thread.is_alive() and not gateway_thread.is_alive()
    assert not failures, repr(failures)
    assert results == [0] and active == [0, 1]
    assert [body["kind"] for _payload, (_status, body) in controls] == ["steer", "stop", "new", "verbose"]
    terminals = list(state.paths.terminal.glob("*.json"))
    assert len(terminals) == 3
    first = state.paths.terminal / f"{terminal_ids[0]}.json"
    retained = terminal_snapshots[first.stem]
    terminal = read_gateway_terminal_response_file(first, request_id=first.stem)
    assert terminal["status"] == "interrupted" and terminal["error_code"] == "INTERRUPTED"
    completed = [read_gateway_terminal_response_file(path, request_id=path.stem)
        for path in terminals if path != first]
    assert len(completed) == 2 and all(result["ok"] for result in completed)
    assert {result["response"] for result in completed} == {"下一轮完整完成。", "关闭流式后完整完成。"}
    assert first.read_bytes() == retained
    for thread in set(threading.enumerate()) - before_threads:
        if thread.name.startswith(("my-agent-tui", "Thread-")) and thread.daemon:
            thread.join(6)
    _assert_fd_restored(before_fd)
