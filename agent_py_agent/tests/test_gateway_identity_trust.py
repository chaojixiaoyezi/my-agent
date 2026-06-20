"""审计 #2 修复真测:网关按来源可信度提取身份,不信可伪造的 X-User-Id/X-Channel 头。

中间件级(安全边界):远程不可信来源 → 匿名 USER(伪造 X-Channel:chat / X-User-Id:admin 都骗不到 admin);
回环本机 → honor 真实身份;token 供暴露部署。真 HTTP:回环转发 X-User-Id → 网关 inbox 记真实渠道用户
(修"渠道用户全跑成 admin");真驱动 AdapterManager 验证适配器确实带上身份头。学 长期助手 局部凭据信任。
"""

from __future__ import annotations

import json
import socket
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.auth.manager import AuthManager
from agent_py_agent.agent.auth.middleware import AuthMiddleware
from agent_py_agent.agent.auth.models import Role
from agent_py_agent.agent.gateway_parts.http_service import GatewayHTTPServer, GatewayHTTPServerParams

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
        self.responses = self.root / "responses"
        self.stop_request = self.root / "stop"
        self.state = self.root / "state.json"
        for p in (self.inbox, self.processing, self.responses):
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
        rid = mgr._submit_gateway_ask(msg)  # 真驱动适配器转发
        assert rid
        data = json.loads((server.paths.inbox / f"{rid}.json").read_text(encoding="utf-8"))
        assert data["user_id"] == "alice"  # 适配器把真实渠道用户传到网关(不再 admin)
    finally:
        server.stop()
