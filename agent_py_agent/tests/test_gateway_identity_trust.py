"""审计 #2 修复真测:网关按来源可信度提取身份,不信可伪造的 X-User-Id/X-Channel 头。

中间件级(安全边界):远程不可信来源 → 匿名 USER(伪造 X-Channel:chat / X-User-Id:admin 都骗不到 admin);
回环本机 → honor 真实身份;token 供暴露部署。真 HTTP:回环转发 X-User-Id → 网关 inbox 记真实渠道用户
(修"渠道用户全跑成 admin");真驱动 AdapterManager 验证适配器确实带上身份头。学 长期助手 局部凭据信任。
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.auth.manager import AuthManager
from agent_py_agent.agent.auth.middleware import AuthMiddleware
from agent_py_agent.agent.auth.models import Role
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)

REMOTE = "203.0.113.7"  # 非回环(模拟远程攻击者)
LOCAL = "127.0.0.1"


def _mw(token: str = "") -> AuthMiddleware:
    return AuthMiddleware(AuthManager(admin_user_id="admin", auth_enabled=True), auth_token=token)


def test_remote_no_header_is_anonymous_not_admin() -> None:
    mw = _mw()
    assert mw.extract_identity({}, REMOTE)[0] == "anonymous"  # 远程缺头 → 匿名,不是 admin
    perm = mw.get_permission({}, REMOTE)
    assert perm.role == Role.USER and not perm.can_access_all_users


def test_remote_spoofed_terminal_channel_not_admin() -> None:
    # 远程伪造 X-Channel:chat + X-User-Id:admin 想骗成终端 admin
    perm = _mw().get_permission({"X-Channel": "chat", "X-User-Id": "admin"}, REMOTE)
    assert perm.role == Role.USER  # 不可信来源:伪造头全无效,降匿名 USER
    assert perm.user_id == "anonymous"


def test_loopback_honors_channel_identity() -> None:
    mw = _mw()
    assert mw.extract_identity({"X-Channel": "feishu", "X-User-Id": "bob"}, LOCAL) == ("bob", "feishu")
    assert mw.get_permission({"X-Channel": "feishu", "X-User-Id": "bob"}, LOCAL).role == Role.USER


def test_loopback_no_header_is_admin() -> None:
    assert _mw().extract_identity({}, LOCAL) == ("admin", "chat")  # 回环终端=admin(单机不破)


def test_peer_none_is_trusted_backward_compat() -> None:
    assert _mw().extract_identity({}, None) == ("admin", "chat")  # 未提供来源=可信(既有调用/单测)


def test_token_grants_trust_to_remote() -> None:
    mw = _mw(token="s3cret")
    assert mw.extract_identity(
        {"X-Gateway-Token": "s3cret", "X-User-Id": "bob", "X-Channel": "feishu"}, REMOTE
    ) == ("bob", "feishu")  # 远程 + 合法 token → 可信
    assert mw.extract_identity({"X-Gateway-Token": "wrong", "X-User-Id": "bob"}, REMOTE)[0] == "anonymous"


def test_check_trusted_gate() -> None:
    mw = _mw(token="t")
    assert mw.check_trusted({}, LOCAL) is True
    assert mw.check_trusted({}, REMOTE) is False
    assert mw.check_trusted({"X-Gateway-Token": "t"}, REMOTE) is True


def test_require_admin_denies_remote_and_user() -> None:
    mw = _mw()
    ok_remote, _p, status, _b = mw.require_admin({"X-Channel": "chat", "X-User-Id": "admin"}, REMOTE)
    assert not ok_remote and status == 403  # 远程伪造 admin → 拒
    ok_user, _p2, _s2, _b2 = mw.require_admin({"X-Channel": "feishu", "X-User-Id": "bob"}, LOCAL)
    assert not ok_user  # 可信但非管理员 → 拒


# ---- 真 HTTP:回环转发身份 → 网关记真实渠道用户 ----
class _Paths:
    def __init__(self, root: Path) -> None:
        self.root = root / "gw"
        self.inbox = self.root / "requests" / "pending"
        self.processing = self.root / "requests" / "processing"
        self.done = self.root / "requests" / "done"
        self.failed = self.root / "requests" / "failed"
        self.responses = self.root / "responses"
        self.stop_request = self.root / "stop"
        self.state = self.root / "state.json"
        for p in (self.inbox, self.processing, self.done, self.failed, self.responses):
            p.mkdir(parents=True, exist_ok=True)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _server(tmp_path) -> GatewayHTTPServer:
    return GatewayHTTPServer(_free_port(), _Paths(tmp_path), params=GatewayHTTPServerParams(auth_middleware=_mw()))


def test_channel_identity_propagates_to_inbox(tmp_path) -> None:
    server = _server(tmp_path)
    port = server.port
    server.start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/ask",
            data=json.dumps({"kind": "ask", "goal": "hi"}).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-User-Id": "bob", "X-Channel": "feishu"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as r:
            rid = json.loads(r.read())["request_id"]
        data = json.loads((server.paths.inbox / f"{rid}.json").read_text(encoding="utf-8"))
        assert data["user_id"] == "bob"  # 网关记真实渠道用户,不是 admin
        assert data["metadata"]["channel"] == "feishu"
    finally:
        server.stop()


def test_adapter_submit_propagates_identity(tmp_path) -> None:
    from agent_py_agent.agent.adapter.manager import ChannelManager

    server = _server(tmp_path)
    port = server.port
    server.start()
    try:
        mgr = ChannelManager(gateway_port=port)
        msg = SimpleNamespace(content="hi", channel="feishu", user_id="alice", message_id="m1")
        submission = mgr._submit_gateway_ask(msg)  # 真驱动适配器转发
        assert submission.request_id
        data = json.loads(
            (server.paths.inbox / f"{submission.request_id}.json").read_text(encoding="utf-8")
        )
        assert data["user_id"] == "alice"  # 适配器把真实渠道用户传到网关(不再 admin)
    finally:
        server.stop()


def test_finished_result_uses_archived_request_owner(tmp_path) -> None:
    server = _server(tmp_path)
    request_id = "req-finished-alice"
    (server.paths.done / f"{request_id}.json").write_text(
        json.dumps({"id": request_id, "user_id": "alice", "metadata": {"channel": "feishu"}}),
        encoding="utf-8",
    )
    (server.paths.responses / f"{request_id}.json").write_text(
        json.dumps({"id": request_id, "status": "done", "ok": True, "response": "完成"}),
        encoding="utf-8",
    )
    server.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.port}/result/{request_id}",
            headers={"X-User-Id": "alice", "X-Channel": "feishu"},
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.status == 200
            assert json.loads(response.read())["response"] == "完成"

        other_user_request = urllib.request.Request(
            f"http://127.0.0.1:{server.port}/result/{request_id}",
            headers={"X-User-Id": "bob", "X-Channel": "feishu"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(other_user_request, timeout=5)
        assert exc_info.value.code == 403
    finally:
        server.stop()


def _write_finished_result_with_private_fields(server, request_id: str) -> dict:
    (server.paths.done / f"{request_id}.json").write_text(
        json.dumps({"id": request_id, "user_id": "alice", "metadata": {"channel": "feishu"}}),
        encoding="utf-8",
    )
    stored = {
        "id": request_id,
        "status": "done",
        "ok": True,
        "response": "完成",
        "backend": "anthropic_compatible",
        "request_file": "/root/.my-agent/private/request.json",
        "chunk_stream_path": "/root/.my-agent/private/chunks.jsonl",
        "lease_owner": "internal-worker",
        "prompt": "private prompt",
        "channel_delivery": {
            "content": "完成",
            "artifact_names": ["report.md"],
            "internal_signal": False,
            "projection_status": "plain_text",
            "path": "/root/.my-agent/private/report.md",
        },
    }
    (server.paths.responses / f"{request_id}.json").write_text(
        json.dumps(stored),
        encoding="utf-8",
    )
    return stored


def test_user_finished_result_hides_internal_paths(tmp_path) -> None:
    server = _server(tmp_path)
    request_id = "req-finished-public-projection"
    _write_finished_result_with_private_fields(server, request_id)
    server.start()
    try:
        user_request = urllib.request.Request(
            f"http://127.0.0.1:{server.port}/result/{request_id}",
            headers={"X-User-Id": "alice", "X-Channel": "feishu"},
        )
        with urllib.request.urlopen(user_request, timeout=5) as response:
            public = json.loads(response.read())
        assert public["response"] == "完成"
        assert public["channel_delivery"]["artifact_names"] == ["report.md"]
        assert "request_file" not in public
        assert "chunk_stream_path" not in public
        assert "lease_owner" not in public
        assert "prompt" not in public
        assert "path" not in public["channel_delivery"]
    finally:
        server.stop()


def test_admin_finished_result_keeps_internal_diagnostics(tmp_path) -> None:
    server = _server(tmp_path)
    request_id = "req-finished-admin-diagnostics"
    stored = _write_finished_result_with_private_fields(server, request_id)
    server.start()
    try:

        with urllib.request.urlopen(
            f"http://127.0.0.1:{server.port}/result/{request_id}",
            timeout=5,
        ) as response:
            admin = json.loads(response.read())
        assert admin["request_file"] == stored["request_file"]
        assert admin["chunk_stream_path"] == stored["chunk_stream_path"]
    finally:
        server.stop()


def test_user_cannot_probe_corrupt_pending_request(tmp_path) -> None:
    server = _server(tmp_path)
    request_id = "req-corrupt-pending"
    (server.paths.inbox / f"{request_id}.json").write_text("{bad json", encoding="utf-8")
    server.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.port}/result/{request_id}",
            headers={"X-User-Id": "alice", "X-Channel": "feishu"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(request, timeout=5)
        assert exc_info.value.code == 403
        assert "/root/" not in exc_info.value.read().decode()
    finally:
        server.stop()


def test_user_cannot_read_finished_result_without_request_record(tmp_path) -> None:
    server = _server(tmp_path)
    request_id = "req-orphan-response"
    (server.paths.responses / f"{request_id}.json").write_text(
        json.dumps({"id": request_id, "status": "done", "ok": True, "response": "不可泄漏"}),
        encoding="utf-8",
    )
    server.start()
    try:
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.port}/result/{request_id}",
            headers={"X-User-Id": "alice", "X-Channel": "feishu"},
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(request, timeout=5)
        assert exc_info.value.code == 403
    finally:
        server.stop()
