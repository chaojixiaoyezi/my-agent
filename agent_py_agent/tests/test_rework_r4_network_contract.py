"""R4 contracts through disposable authenticated loopback listeners (not LAN proof)."""
from __future__ import annotations

import json
import secrets
import threading
import time
from contextlib import contextmanager
from copy import deepcopy
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
from agent_py_agent.cli.chat_parts.tui_runtime import TuiRuntime, TuiTurnEventAdapter


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
def approval_request(record):
    return build_tool_approval_request(ToolCall(call_id="call-r4", tool_name="controlled_exec",
        arguments={"command": ["python3", "-V"]}, source_protocol="native", schema_hash="sha256:r4",
        run_id="run-r4", turn_id=record.request_id, attempt_id="attempt-a"), request_id=record.request_id,
        round_number=1, call_index=1, description="Inspect /private/tmp/r4-example.txt",
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
            finished=finished, thread=thread)
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
