"""R4 contracts through disposable authenticated loopback listeners (not LAN proof)."""
from __future__ import annotations

import gc
import json
import os
import secrets
import threading
import time
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from functools import partial
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from agent_py_agent.agent.auth.manager import AuthManager
from agent_py_agent.agent.auth.middleware import AuthMiddleware
from agent_py_agent.agent.auth.models import Role
from agent_py_agent.agent.contracts.tool_approval import (
    ToolApprovalDecision,
    build_tool_approval_request,
)
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.gateway_parts.admin_control_service import pending_tool_approvals
from agent_py_agent.agent.gateway_parts.control_service import (
    GatewayControlScope,
    _scope_request_payload,
)
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_chunk_path, gateway_paths_from_root
from agent_py_agent.agent.gateway_parts.permission_bridge import gateway_permission_decision_path
from agent_py_agent.agent.gateway_parts.queue_service import archive_request
from agent_py_agent.agent.gateway_parts.stream_writer import (
    BufferedChunkStreamWriter,
    write_chunk_event,
)
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.tooling.runtime_contracts import ToolCall
from agent_py_agent.cli.chat_parts.gateway_client import (
    GatewayChunkPollRequest,
    NetworkGatewayObserver,
)
from agent_py_agent.cli.chat_parts.tui_runtime import (
    TuiRuntime,
    TuiTurnEventAdapter,
    TuiTurnSummary,
)


# LLM: Each test owns an authenticated real listener, disposable queue and random in-memory token; no existing daemon or G1 client credential is used.
# 函数用途: 准备真实认证服务与普通私有通道用户，测试结束关闭原监听器。
@pytest.fixture
def gateway(tmp_path):
    agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(tmp_path / "home"),
        gateway_per_user_owner_scoping=True, gateway_native_stream_max=2), tmp_path / "workspace")
    paths = gateway_paths_from_root(tmp_path / "gateway")
    for folder in (paths.inbox, paths.processing, paths.done, paths.failed, paths.responses, paths.terminal):
        folder.mkdir(parents=True, exist_ok=True)
    middleware = AuthMiddleware(AuthManager(), secrets.token_urlsafe(24))
    assert middleware.auth_manager.authenticate("tui", "alice").role is Role.USER
    server = GatewayHTTPServer(0, paths, params=GatewayHTTPServerParams(agent=agent, auth_middleware=middleware))
    server.start()
    state = SimpleNamespace(agent=agent, paths=paths, server=server, middleware=middleware,
        url=f"http://127.0.0.1:{server.server.server_address[1]}")
    try:
        yield state
    finally:
        server.stop()


# LLM: Explicit providers isolate the other regressions from the configured-token bug; identity still comes solely from the original projector.
# 函数用途: 创建真实网络客户端，按需使用显式远程头并绑定原 GET/POST 合同。
def client(gateway, *, user="alice", channel="tui", provider=True):
    agent = SimpleNamespace(config=SimpleNamespace(gateway_auth_token=gateway.middleware.auth_token,
        gateway_request_timeout=5), gateway_request_identity=lambda: {"user_id": user, "channel": channel})
    if provider:
        agent.gateway_network_headers = lambda: {AuthMiddleware.HEADER_TOKEN: gateway.middleware.auth_token}
    observer = NetworkGatewayObserver(agent, gateway.url)
    observer.bind_client_transport()
    return observer


# LLM: The authoritative hot request freezes exact user/channel/conversation and the current lease before any writer event.
# 函数用途: 在测试队列建立活动请求，供原 ACL、pending 审批和归档逻辑共同读取。
def hot_request(gateway, *, request_id="r", user="alice", channel="tui", session="session-a"):
    scope = GatewayControlScope(user, channel, session)
    payload = {**_scope_request_payload(scope), "id": request_id, "request_id": request_id,
        "execution_attempt_id": "attempt-a", "lease_started_at": time.time(), "status": "processing"}
    path = gateway.paths.processing / f"{request_id}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return SimpleNamespace(scope=scope, payload=payload, path=path,
        chunk=gateway_chunk_path(gateway.paths, request_id), request_id=request_id)


# LLM: Only the real canonical archive commits completion; the response file alone is never a terminal fact.
# 函数用途: 提交真实终态，使网络结果路由按原权限投影返回。
def finish(gateway, record):
    result = {**record.payload, "ok": True, "response": "finished"}
    archived = archive_request(gateway.paths, record.path, gateway.paths.done, record.request_id,
        terminal_response=result)
    assert archived.disposition.value in {"committed", "already_committed"}, archived


# LLM: A poll request's paths provide identifiers only; the network observer must get all payloads through HTTP.
# 函数用途: 记录网络事件和计数，不以客户端文件读取替代服务端路由。
def poll_request(gateway, record, consume):
    return GatewayChunkPollRequest(record.chunk, gateway.paths.terminal / f"{record.request_id}.json",
        time.time() + 5, lambda _: None, [0], [0], on_event=consume)


def test_configured_token_authenticates_real_listener(gateway):
    record = hot_request(gateway)
    finish(gateway, record)
    observer = client(gateway, provider=False)
    status, body = observer.request_json("GET", "/result/r")
    assert status == 200
    assert body["response"] == "finished"
    assert "Authorization" not in observer._headers()
    assert observer._headers()[AuthMiddleware.HEADER_TOKEN] == gateway.middleware.auth_token


def test_provider_cannot_replace_original_identity(gateway):
    observer = client(gateway)
    observer.agent.gateway_network_headers = lambda: {AuthMiddleware.HEADER_TOKEN: gateway.middleware.auth_token,
        "x-user-id": "admin", "x-channel": "chat", "Authorization": "custom-provider-value"}
    headers = observer._headers()
    assert headers["X-User-Id"] == "alice"
    assert headers["X-Channel"] == "tui"
    assert headers["Authorization"] == "custom-provider-value"


def test_control_receipt_round_trip_keeps_exact_conversation(gateway):
    observer = client(gateway)
    status, accepted = observer.agent.post_gateway_json("/control", {"command": "/status",
        "conversation_id": "session-a", "metadata": {"message_id": "receipt-r4"}}, timeout=5)
    assert status == 200, accepted
    operation_id = accepted["operation_id"]
    path = f"/control-status/{operation_id}"
    status, receipt = observer.agent.get_gateway_json(path, timeout=5, conversation_id="session-a")
    assert status == 200, receipt
    assert receipt["operation_id"] == operation_id
    assert observer.agent.get_gateway_json(path, timeout=5, conversation_id="session-b")[0] in {403, 404}
    assert client(gateway, user="bob").agent.get_gateway_json(path, timeout=5,
        conversation_id="session-a")[0] in {403, 404}


def test_terminal_http_drain_emits_all_450_real_capped_progress_rows(gateway):
    record = hot_request(gateway)
    for number in range(450):
        write_chunk_event(record.chunk, {"kind": "assistant_commentary", "text": f"row-{number}"})
    finish(gateway, record)
    observer = client(gateway)
    status, page = observer.request_json("GET", "/progress/r?since=0")
    assert status == 200
    assert len(page["events"]) == 200 and page["next"] == 200
    observer._http_only = True
    emitted = []
    request = poll_request(gateway, record, lambda event: emitted.append(event) or True)
    assert observer(request)["response"] == "finished"
    assert [event["text"] for event in emitted] == [f"row-{number}" for number in range(450)]
    assert observer.line_cursor == 450
    assert request.chunks_printed_ref == [450]


def test_tail_appended_and_terminal_committed_between_reads_is_emitted(gateway, monkeypatch):
    record = hot_request(gateway)
    write_chunk_event(record.chunk, {"kind": "assistant_commentary", "text": "before"})
    observer = client(gateway)
    observer._http_only = True
    actual = observer.request_json
    pages = []

    def append_after_first_page(method, path, payload=None, **kwargs):
        response = actual(method, path, payload, **kwargs)
        if path.startswith("/progress/"):
            pages.append(response[1])
            if len(pages) == 1:
                write_chunk_event(record.chunk, {"kind": "assistant_commentary", "text": "late tail"})
                finish(gateway, record)
        return response

    monkeypatch.setattr(observer, "request_json", append_after_first_page)
    emitted = []
    result = observer(poll_request(gateway, record, lambda event: emitted.append(event) or True))
    assert result["response"] == "finished"
    assert [event["text"] for event in emitted] == ["before", "late tail"]
    assert observer.line_cursor == 2
    assert pages[-1]["next"] == 2 and not pages[-1]["events"]


# LLM: Build a canonical request from the same ToolCall contract as the executor; labels and parameters cannot select authorization.
# 函数用途: 生成带完整 binding 与有限选项的真实前台审批 DTO。
def approval_request(record, *, call_id="call-r4", call_index=1):
    return build_tool_approval_request(ToolCall(call_id=call_id, tool_name="controlled_exec",
        arguments={"command": ["python3", "-V"]}, source_protocol="native", schema_hash="sha256:r4",
        run_id="run-r4", turn_id=record.request_id, attempt_id="attempt-a"), request_id=record.request_id,
        round_number=1, call_index=call_index, description="Inspect /private/tmp/r4-example.txt",
        grant_key="controlled_exec:read")


# LLM: Wait for the production pending reader, not a guessed sleep; cancellation and join in finally keep failing tests from leaving a live waiter.
# 函数用途: 启动原 writer 等待链并证明它仍持有当前 lease 的待决审批。
@contextmanager
def waiting_approval(gateway, *, channel="tui"):
    record = hot_request(gateway, channel=channel)
    request = approval_request(record)
    writer = BufferedChunkStreamWriter(record.chunk, interactive_approvals=True, rich_transcript=True,
        delivery_channel=channel)
    cancelled, finished = threading.Event(), threading.Event()
    result, errors = {}, []
    cancellation = SimpleNamespace(is_cancelled=cancelled.is_set)

    def wait():
        try:
            result.update(writer.request_permission(request.to_dict(), cancellation_token=cancellation))
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=wait)
    thread.start()
    try:
        until = time.monotonic() + 5
        while time.monotonic() < until and not pending_tool_approvals(gateway.agent, gateway.paths, record.scope):
            assert not errors, errors
            assert not finished.is_set(), result
            finished.wait(0.01)
        pending = pending_tool_approvals(gateway.agent, gateway.paths, record.scope)
        assert len(pending) == 1
        assert pending[0].request == request
        yield SimpleNamespace(record=record, request=request, writer=writer, result=result,
            finished=finished, thread=thread, cancelled=cancelled)
    finally:
        cancelled.set()
        thread.join(5)
        writer.close()
        assert not thread.is_alive()
        assert not errors, errors


# LLM: The HTTP body contains the exact verified request/decision and binding run id; only the original waiter may consume the decision.
# 函数用途: 使用已有网络审批路由发送一份精确决定。
def approval_body(waiting, *, decision="approved"):
    return {"conversation_id": "session-a", "run_id": waiting.request.binding["run_id"],
        "request": waiting.request.to_dict(), "decision": ToolApprovalDecision(
            waiting.request.permission_id, decision, "read-only verification").to_dict()}


@pytest.mark.parametrize("transport", ["sse", "http"])
@pytest.mark.parametrize("decision", ["approved", "denied", "approved_session", "approved_owner"])
def test_real_writer_network_tui_http_decision_releases_original_waiter(gateway, transport, decision):
    with waiting_approval(gateway) as waiting:
        observer = client(gateway)
        observer._http_only = transport == "http"
        runtime = TuiRuntime("session-a")
        adapter = TuiTurnEventAdapter(runtime, "r")
        adapter.configure_gateway_permission_sink(partial(observer.permission_sink, conversation_id="session-a"))
        emitted, resolved, routes = [], [], []
        actual = observer.request_json

        def track(method, path, payload=None, **kwargs):
            routes.append((method, urlsplit(path).path, payload))
            return actual(method, path, payload, **kwargs)

        observer.request_json = track

        def consume(event):
            emitted.append(event)
            consumed = adapter.on_gateway_event(event)
            if event.get("kind") == "permission_requested":
                overlay = runtime.store.snapshot().permission
                if overlay is not None:
                    resolved.append(adapter.resolve_permission(overlay.permission_id, decision,
                        feedback="read-only verification"))
                    if resolved[-1] and waiting.finished.wait(3):
                        finish(gateway, waiting.record)
            return consumed

        result = observer(poll_request(gateway, waiting.record, consume))
        permissions = [event for event in emitted if event.get("kind") == "permission_requested"]
        assert permissions and permissions[0].get("permission") == waiting.request.to_dict()
        assert resolved == [True]
        assert waiting.finished.wait(3)
        assert waiting.result["decision"] == decision
        assert waiting.result["feedback"] == "read-only verification"
        assert result["ok"] is True
        assert runtime.store.snapshot().permission is None
        writes = [(path, payload) for method, path, payload in routes if method == "POST"]
        assert [path for path, _ in writes] == ["/client/agent-permission"]
        assert writes[0][1]["run_id"] == waiting.request.binding["run_id"]
        assert writes[0][1]["request"] == waiting.request.to_dict()


@pytest.mark.parametrize("wrong", ["owner", "channel", "session", "run", "binding", "permission_id",
    "decision_id", "description", "options", "deadline", "expired", "old_lease"])
def test_wrong_approval_identity_or_lifetime_keeps_original_waiter_pending(gateway, monkeypatch, wrong):
    with waiting_approval(gateway) as waiting:
        observer = client(gateway, user="bob" if wrong == "owner" else "alice",
            channel="gateway-cli" if wrong == "channel" else "tui")
        body = deepcopy(approval_body(waiting))
        if wrong == "session":
            body["conversation_id"] = "session-b"
        elif wrong == "run":
            body["run_id"] = "another-run"
        elif wrong == "binding":
            body["request"]["binding"]["args_hash"] = "another-arguments-hash"
        elif wrong == "permission_id":
            body["request"]["permission_id"] = "another-permission"
        elif wrong == "decision_id":
            body["decision"]["permission_id"] = "another-permission"
        elif wrong == "description":
            body["request"]["description"] = "different displayed operation"
        elif wrong == "options":
            body["request"]["options"][0]["label"] = "different label"
        elif wrong == "deadline":
            body["request"]["expires_at"] = time.time() + 3600
        elif wrong == "expired":
            from agent_py_agent.agent.gateway_parts import http_handlers
            future = time.time() + 100000
            monkeypatch.setattr(http_handlers, "time", SimpleNamespace(time=lambda: future))
        elif wrong == "old_lease":
            waiting.record.payload["lease_started_at"] = time.time() + 1
            waiting.record.path.write_text(json.dumps(waiting.record.payload), encoding="utf-8")
        status, _ = observer.request_json("POST", "/client/agent-permission", body)
        assert status in {404, 409}
        assert not gateway_permission_decision_path(waiting.record.chunk, waiting.request).exists()
        assert not waiting.finished.is_set()
        assert waiting.result == {}


def test_external_im_keeps_summary_only_even_with_all_user_view(gateway):
    with waiting_approval(gateway, channel="feishu") as waiting:
        for user in ("alice", "admin"):
            status, batch = client(gateway, user=user, channel="feishu").request_json("GET", "/progress/r")
            assert status == 200
            event = batch["events"][0]
            assert set(event) == {"kind", "tool", "summary"}
            assert event["kind"] == "permission_requested"
            assert "/private/tmp/" not in event["summary"]
        assert not waiting.finished.is_set()


def test_invalid_permission_dto_is_not_exposed_to_private_viewer(gateway):
    record = hot_request(gateway)
    write_chunk_event(record.chunk, {"kind": "permission_requested", "permission": {
        "tool_name": "controlled_exec", "description": "invalid incomplete DTO"}})
    status, batch = client(gateway).request_json("GET", "/progress/r")
    assert status == 200
    assert all("permission" not in event for event in batch["events"])


# LLM: Measure only resources owned by this listener after libc resolver initialization; failures still execute teardown and the zero-delta assertion.
# 函数用途: 对成功、断连和畸形输入用例统一核对文件描述符没有净增。
@pytest.fixture
def measured_gateway(tmp_path):
    """原认证监听器完整退出后核对描述符，包含断连与失败用例的 finally。"""
    import socket

    # macOS 的 libc 首次反查会保留进程级 mDNS 连接；先初始化，再测本用例拥有的资源。
    socket.getfqdn("127.0.0.1")
    fd_root = "/proc/self/fd" if os.path.isdir("/proc/self/fd") else "/dev/fd"
    if not os.path.isdir(fd_root):
        pytest.skip("descriptor census requires procfs or devfs")
    gc.collect()
    before = len(os.listdir(fd_root))
    fixture = gateway.__wrapped__(tmp_path)
    try:
        yield next(fixture)
    finally:
        fixture.close()
        gc.collect()
        delta = len(os.listdir(fd_root)) - before
        print(f"resource_check fd_delta={delta} teardown_complete=true")
        assert delta == 0


# LLM: One production writer has at most one sequential waiter; finally cancels and joins it even on the pre-fix callback failure.
# 函数用途: 建立同一前台请求的两个审批，测试失败也不遗留等待线程。
@contextmanager
def sequential_approvals(gateway):
    """同一前台 writer 顺序等待两份规范审批，退出必取消并回收原线程。"""
    record = hot_request(gateway)
    requests = (approval_request(record), approval_request(record, call_id="call-r3-b", call_index=2))
    writer = BufferedChunkStreamWriter(record.chunk, interactive_approvals=True, rich_transcript=True,
        delivery_channel="tui")
    cancelled, finished = threading.Event(), threading.Event()
    results, errors = [], []

    def wait():
        try:
            for request in requests:
                results.append(writer.request_permission(request.to_dict(),
                    cancellation_token=SimpleNamespace(is_cancelled=cancelled.is_set)))
                if cancelled.is_set():
                    return
            finish(gateway, record)
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()

    thread = threading.Thread(target=wait)
    thread.start()
    try:
        until = time.monotonic() + 5
        while time.monotonic() < until and not pending_tool_approvals(gateway.agent, gateway.paths, record.scope):
            assert not errors, errors
            assert not finished.is_set()
            finished.wait(0.01)
        assert [pending.request for pending in pending_tool_approvals(
            gateway.agent, gateway.paths, record.scope)] == [requests[0]]
        yield SimpleNamespace(record=record, requests=requests, writer=writer, results=results,
            finished=finished, thread=thread)
    finally:
        cancelled.set()
        thread.join(5)
        writer.close()
        assert not thread.is_alive()
        assert not errors, errors


# LLM: The peer resolves A through the actual HTTP sink; only the delivered durable resolution may let the foreground adapter open B.
# 函数用途: 在两种真实网络运输及一次重连下复现并阻止审批弹窗卡死。
@pytest.mark.parametrize(("transport", "disconnect"), [("sse", False), ("http", False), ("sse", True)])
def test_peer_resolution_preserves_next_foreground_approval(measured_gateway, monkeypatch, transport, disconnect):
    import agent_py_agent.agent.common.authenticated_http as network

    gateway = measured_gateway
    with sequential_approvals(gateway) as waiting:
        first, second = waiting.requests
        observer, peer = client(gateway), client(gateway)
        observer._http_only = transport == "http"
        runtime = TuiRuntime("session-a")
        adapter = TuiTurnEventAdapter(runtime, waiting.record.request_id)
        adapter.configure_gateway_permission_sink(partial(observer.permission_sink, conversation_id="session-a"))
        emitted, closed, actionable, approved, errors, pending_counts, streams = [], [], [], [], [], [], []
        aborted = threading.Event()
        actual_open = network.open_authenticated_request

        class DisconnectAfterResolution:
            def __init__(self, raw):
                self.raw, self.target_frame, self.close_next = raw, False, False

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.raw.close()

            def readline(self, size):
                if self.close_next:
                    self.raw.close()
                    return b""
                line = self.raw.readline(size)
                if line.startswith(b"data:"):
                    event = json.loads(line[5:]).get("event", {})
                    self.target_frame = event.get("kind") == "permission_resolved" and event.get("permission_id") == first.permission_id
                if line in (b"\n", b"\r\n") and self.target_frame:
                    self.close_next = True
                return line

        def open_request(request, *, timeout):
            raw = actual_open(request, timeout=timeout)
            if urlsplit(request.full_url).path.endswith("/events"):
                streams.append(request.get_header("Last-event-id"))
                if disconnect and len(streams) == 1:
                    return DisconnectAfterResolution(raw)
            return raw

        monkeypatch.setattr(network, "open_authenticated_request", open_request)

        def consume(event):
            emitted.append(event)
            try:
                consumed = adapter.on_gateway_event(event)
                if event.get("kind") == "permission_requested":
                    overlay = runtime.store.snapshot().permission
                    pending_counts.append(len(pending_tool_approvals(gateway.agent, gateway.paths, waiting.record.scope)))
                    if event["permission"]["permission_id"] == first.permission_id:
                        assert overlay is not None and overlay.permission_id == first.permission_id
                        peer.permission_sink(first, ToolApprovalDecision(first.permission_id, "approved"),
                            conversation_id="session-a")
                    else:
                        actionable.append(overlay is not None and overlay.permission_id == second.permission_id)
                        approved.append(adapter.resolve_permission(second.permission_id, "approved"))
                elif event.get("kind") == "permission_resolved" and event.get("permission_id") == first.permission_id:
                    closed.append(runtime.store.snapshot().permission is None)
                return consumed
            except Exception as exc:
                errors.append((type(exc).__name__, str(exc), exc.__cause__))
                aborted.set()
                raise

        request = replace(poll_request(gateway, waiting.record, consume), is_wait_cancelled=aborted.is_set)
        result = observer(request)
        rows = [json.loads(line) for line in waiting.record.chunk.read_text().splitlines()]
        durable = [row for row in rows if row.get("kind") == "permission_resolved"
                   and row.get("permission_id") == first.permission_id]
        resolutions = [event for event in emitted if event.get("kind") == "permission_resolved"
                       and event.get("permission_id") == first.permission_id]
        overlay = runtime.store.snapshot().permission
        trace = {"transport": transport, "disconnect": disconnect, "durable_a": len(durable),
            "delivered_a": len(resolutions), "a_pending": overlay is not None and overlay.permission_id == first.permission_id,
            "b_actionable": actionable, "b_waiter_completed": len(waiting.results) == 2,
            "consumer_errors": [(name, text, cause is None) for name, text, cause in errors]}
        print("permission_trace " + json.dumps(trace, sort_keys=True))
        assert len(durable) == 1
        assert len(resolutions) == 1, trace
        assert closed == [True] and actionable == [True] and approved == [True], trace
        assert not errors, trace
        assert waiting.finished.wait(3) and len(waiting.results) == 2
        assert all(row["decision"] == "approved" for row in waiting.results)
        assert result["ok"] is True and runtime.store.snapshot().permission is None
        assert not pending_tool_approvals(gateway.agent, gateway.paths, waiting.record.scope)
        assert pending_counts and max(pending_counts) <= 1
        assert [(event["kind"], event.get("permission_id", event.get("permission", {}).get("permission_id")))
                for event in emitted if event["kind"] in {"permission_requested", "permission_resolved"}] == [
            ("permission_requested", first.permission_id), ("permission_resolved", first.permission_id),
            ("permission_requested", second.permission_id), ("permission_resolved", second.permission_id)]
        assert observer._http_only is (transport == "http")
        assert len(streams) == (2 if disconnect else 0 if transport == "http" else 1)
        if disconnect:
            assert streams[1] and streams[1].startswith("v1:r:attempt-a:")
        adapter.finalize(TuiTurnSummary(response_text="finished"))
        _headless_followup_after_approvals(runtime, gateway)


# LLM: Derive negative rows from a real writer resolution, preserving DTO authority and both transport boundaries rather than inventing a parallel wire contract.
# 函数用途: 验证坏决议被丢弃、额外字段不下发且后续有效行仍能到达。
@pytest.mark.parametrize("transport", ["sse", "http"])
@pytest.mark.parametrize(("field", "value"), [
    ("permission_id", ""), ("decision", "unknown"), ("decided_at", [1]),
    ("decided_at", "invalid-time"), ("reason_code", "unknown-reason"),
    pytest.param("decided_at", 10**1000, id="timestamp-overflow")])
def test_resolution_projection_rejects_malformed_durable_dto(measured_gateway, transport, field, value):
    gateway = measured_gateway
    with waiting_approval(gateway) as waiting:
        observer = client(gateway)
        observer._http_only = transport == "http"
        observer.permission_sink(waiting.request, ToolApprovalDecision(waiting.request.permission_id, "approved"),
            conversation_id="session-a")
        assert waiting.finished.wait(3)
        rows = [json.loads(line) for line in waiting.record.chunk.read_text().splitlines()]
        canonical = next(row for row in rows if row.get("kind") == "permission_resolved")
        malformed = {**canonical, field: value}
        # 负例从真实 writer 的规范行改坏一个字段，不另造客户端/服务端 Wire 合同。
        write_chunk_event(waiting.record.chunk, malformed)
        write_chunk_event(waiting.record.chunk, {**canonical, "private_extra": "not-a-dto-field"})
        finish(gateway, waiting.record)
        runtime = TuiRuntime("session-a")
        adapter = TuiTurnEventAdapter(runtime, "r")
        emitted = []

        def consume(event):
            emitted.append(event)
            return adapter.on_gateway_event(event)

        assert observer(poll_request(gateway, waiting.record, consume))["ok"] is True
        resolutions = [event for event in emitted if event.get("kind") == "permission_resolved"]
        expected = {"kind": "permission_resolved", **ToolApprovalDecision.from_mapping(canonical).to_dict()}
        assert resolutions == [expected, expected]
        assert runtime.store.snapshot().permission is None
        assert not pending_tool_approvals(gateway.agent, gateway.paths, waiting.record.scope)
        print(f"resolution_contract_trace transport={transport} malformed_rejected=true canonical_dto_only=true")


# LLM: External viewers, including the admin identity, receive only the original request summary and never canonical resolution fields.
# 函数用途: 核对普通用户和管理员都不能在外部通道获得完整审批决议。
@pytest.mark.parametrize("transport", ["sse", "http"])
def test_external_channels_do_not_disclose_canonical_resolution(measured_gateway, transport):
    gateway = measured_gateway
    with waiting_approval(gateway, channel="feishu") as waiting:
        waiting.cancelled.set()
        assert waiting.finished.wait(3)
        rows = [json.loads(line) for line in waiting.record.chunk.read_text().splitlines()]
        assert any(row.get("kind") == "permission_resolved" for row in rows)
        finish(gateway, waiting.record)
        for user in ("alice", "admin"):
            observer = client(gateway, user=user, channel="feishu")
            observer._http_only = transport == "http"
            emitted = []
            assert observer(poll_request(gateway, waiting.record, lambda event: emitted.append(event) or True))["ok"] is True
            assert [event["kind"] for event in emitted] == ["permission_requested"]
            assert set(emitted[0]) == {"kind", "tool", "summary"}
        print(f"resolution_contract_trace transport={transport} external_resolution_hidden=true")


# LLM: The shared projector reuses the existing DTO normalization; an empty timestamp does not justify a new validation policy in this slice.
# 函数用途: 锁住原 DTO 的空值兼容行为，避免测试自造更严格的合同。
def test_resolution_projection_keeps_existing_dto_empty_timestamp_default():
    from agent_py_agent.agent.gateway_parts.http_handlers import _project_public_progress_rows

    row = {"kind": "permission_resolved", **ToolApprovalDecision("approval-empty-time", "approved").to_dict()}
    row["decided_at"] = []
    expected = ToolApprovalDecision.from_mapping(row).to_dict()
    assert expected["decided_at"] == 0.0
    assert _project_public_progress_rows((json.dumps(row),), channel="tui", next_cursor=7) == (
        [{"kind": "permission_resolved", **expected}], 7)


# LLM: Stream/steer/stop use the original headless TUI control boundary; reset requires a completed real /new receipt and a new network result, not a synthetic reset alone.
# 函数用途: 审批后连续走界面控制、真实新会话回执和下一轮，核对旧弹窗、目标及页脚状态全部释放。
def _headless_followup_after_approvals(runtime, gateway):
    """不以本地出箱证明服务端 steer/stop 或模型执行；/new 则核对真实 HTTP 回执。"""
    from agent_py_agent.cli.chat_parts import tui_actions
    from agent_py_agent.cli.chat_parts.tui_worker import _reset_worker_refs
    from agent_py_agent.tests.test_tui_control_delivery import _post_turn_params, _request_target

    steers, controls = [], []
    params = _post_turn_params(runtime, steers)
    params.control_operation_reconciler = SimpleNamespace(enqueue_background=controls.append)
    runtime.register_gateway_request("gwreq-turn-1")
    turn = runtime.begin_turn("chat-turn-1", task_progress_generation_id="gwreq-turn-1")
    turn.write_model("正在生成第一段")
    turn.write_model("继续生成下一段")
    assert runtime.store.snapshot().has_active_work
    runtime.publish_execution_target(_request_target())
    assert tui_actions._tui_submit_active_turn_input(params, "补充证据", display_text="补充证据") is True
    assert len(steers) == 1 and steers[0].expected_turn_id == "gwreq-turn-1"
    assert tui_actions._tui_submit_control_operation(params, "/stop") is True
    assert len(controls) == 1 and controls[0].expected_turn_id == "gwreq-turn-1"
    assert controls[0].command_kind == "stop" and runtime.request_interrupt()
    runtime.complete_turn("chat-turn-1", TuiTurnSummary(ok=False, interrupted=True))
    _reset_worker_refs(params)
    observer = client(gateway)
    status, receipt = observer.agent.post_gateway_json("/control", {"command": "/new",
        "conversation_id": runtime.session_id, "metadata": {"message_id": "r3-followup-new"}}, timeout=5)
    assert status == 200 and receipt["control_state"] == "completed" and receipt["ok"] is True, receipt
    assert receipt["kind"] == "new" and receipt["details"]["view_reset"] is True
    status, replay = observer.agent.get_gateway_json("/control-status/" + receipt["operation_id"], timeout=5,
        conversation_id=runtime.session_id)
    assert status == 200 and replay == receipt
    runtime.publish_conversation_reset(receipt["details"]["thread_id"])
    assert runtime.publish_execution_target(_request_target()) is False
    assert tui_actions._tui_submit_active_turn_input(params, "下一轮对话", display_text="下一轮对话") is False
    tui_actions._tui_enqueue_job(params, "下一轮对话")
    assert params.jobs.get_nowait().user == "下一轮对话"
    record = hot_request(gateway, request_id="r3-next-turn")
    finish(gateway, record)
    status, result = observer.request_json("GET", "/result/" + record.request_id)
    assert status == 200 and result["ok"] is True and result["request_id"] == record.request_id
    next_turn = runtime.begin_turn("chat-turn-2")
    next_turn.write_model("新的回答")
    runtime.complete_turn("chat-turn-2", TuiTurnSummary(response_text="下一轮完成"))
    snapshot = runtime.store.snapshot()
    assert snapshot.permission is None and snapshot.pending_steers == () and not snapshot.has_active_work
    assert runtime.execution_target() is None
    assert not runtime._turns and not runtime._completing_turns
    assert not runtime._foreground_gateway_bindings and not runtime._owned_gateway_requests
    print("headless_sequence stream->steer->stop=tui_control_outbox new=real_completed_http_receipt "
          "next_turn=real_http_result_and_tui clean=true")
