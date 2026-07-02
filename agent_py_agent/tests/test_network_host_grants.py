"""N1(真机回归② 新根因)钉子:跨机私网监控数据源被出站闸 NETWORK_PRIVATE_HOST_BLOCKED 拦死,
5 个盯源子代理全放弃、编队整体崩、0/22。闸本身早内置 allowed_private_hosts 白名单,缺的是
「任务/能力层 → 白名单」的接线。本文件钉住整条新链:
  authorize_network_host(属主确认) → owner network_grants 存储 → write_boundary 构造新鲜灌入
  → network_safety 出站闸 + path_url_command 预检闸放行;
以及撞闸错误自带授权自纠通路(子代理别再一撞就 abandon)。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.tool_runtime_ledger import write_boundary_with_runtime_ledger
from agent_py_agent.agent.capability.network_authorization_tool import AuthorizeNetworkHostTool
from agent_py_agent.agent.contracts.gates.network_safety import (
    NetworkSafetyFacts,
    always_blocked_host,
    evaluate_network_safety_gate,
)
from agent_py_agent.agent.contracts.gates.path_url_command import (
    PathUrlCommandFacts,
    evaluate_path_url_command_gate,
)
from agent_py_agent.agent.tooling.web import _network_safety_error
from agent_py_agent.agent.user_space.network_grants import (
    CreateNetworkHostGrant,
    active_private_hosts,
    create_network_host_grant,
    list_network_host_grants,
    network_grants_dir,
    normalized_grant_host,
    revoke_network_host_grant,
)

_LAN_HOST = "192.168.77.10"
_LAN_URL = f"http://{_LAN_HOST}:8901/pull?since=0&limit=5"


def _agent(tmp_path):
    home = SimpleNamespace(owner_home_dir=str(tmp_path), owner_id="u-watch")
    return SimpleNamespace(home_paths=home)


# ---- 主机归一:模型会传裸主机 / host:port / 完整 URL,都要落成闸认的主机名 ----

def test_normalized_grant_host_variants():
    assert normalized_grant_host(_LAN_HOST) == _LAN_HOST
    assert normalized_grant_host(f"{_LAN_HOST}:8901") == _LAN_HOST
    assert normalized_grant_host(_LAN_URL) == _LAN_HOST
    assert normalized_grant_host("HTTP://LAN-Box.local:9000/x") == "lan-box.local"
    assert normalized_grant_host("[::1]:8080") == "::1"
    assert normalized_grant_host("") == ""
    assert normalized_grant_host("   ") == ""


# ---- 存储层:落盘/幂等/吊销/过期,坏文件不拖垮 ----

def test_grant_store_roundtrip(tmp_path):
    grant = create_network_host_grant(
        tmp_path, CreateNetworkHostGrant(host=f"{_LAN_HOST}:8901", reason="用户点名监控", granted_by="u-watch")
    )
    assert grant.host == _LAN_HOST
    assert active_private_hosts(tmp_path) == (_LAN_HOST,)
    # 同主机重复授权幂等:不堆第二个文件
    again = create_network_host_grant(tmp_path, CreateNetworkHostGrant(host=_LAN_URL, reason="重复"))
    assert again.grant_id == grant.grant_id
    assert len(list_network_host_grants(tmp_path)) == 1
    # 吊销后立即从 active 集消失
    revoked = revoke_network_host_grant(tmp_path, _LAN_HOST)
    assert [g.host for g in revoked] == [_LAN_HOST]
    assert active_private_hosts(tmp_path) == ()


def test_grant_store_expiry_and_malformed(tmp_path):
    expired_at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    create_network_host_grant(tmp_path, CreateNetworkHostGrant(host="10.0.0.8", reason="过期授权", expires_at=expired_at))
    create_network_host_grant(tmp_path, CreateNetworkHostGrant(host=_LAN_HOST, reason="现役授权"))
    (network_grants_dir(tmp_path) / "broken.json").write_text("{not json", encoding="utf-8")
    assert active_private_hosts(tmp_path) == (_LAN_HOST,)


# ---- 工具层:属主确认闸(update_persona 先例)+ 永久拦截段拒绝 ----

def test_authorize_tool_requires_confirmed(tmp_path):
    result = AuthorizeNetworkHostTool(_agent(tmp_path)).execute(
        {"action": "grant", "hosts": [_LAN_HOST], "reason": "用户点名"}
    )
    assert result.ok is False
    assert result.error_code == "APPROVAL_REQUIRED"
    assert active_private_hosts(tmp_path) == ()


def test_authorize_tool_grant_revoke_list(tmp_path):
    tool = AuthorizeNetworkHostTool(_agent(tmp_path))
    result = tool.execute({"action": "grant", "hosts": [_LAN_URL, "10.1.2.3:9000"], "reason": "用户点名监控 5 路数据源", "confirmed": True})
    assert result.ok
    assert active_private_hosts(tmp_path) == ("10.1.2.3", _LAN_HOST)
    # 审计字段落盘
    grants = list_network_host_grants(tmp_path)
    assert all(g.reason and g.granted_by == "u-watch" for g in grants)
    listing = json.loads(tool.execute({"action": "list"}).output)
    assert {row["host"] for row in listing["grants"]} == {"10.1.2.3", _LAN_HOST}
    assert tool.execute({"action": "revoke", "hosts": [_LAN_HOST]}).ok
    assert active_private_hosts(tmp_path) == ("10.1.2.3",)


def test_authorize_tool_rejects_always_blocked(tmp_path):
    tool = AuthorizeNetworkHostTool(_agent(tmp_path))
    for host in ("metadata.google.internal", "169.254.169.254"):
        result = tool.execute({"action": "grant", "hosts": [host], "reason": "x", "confirmed": True})
        assert result.ok is False, host
        assert result.error_code == "NETWORK_ALWAYS_BLOCKED_HOST"
    assert active_private_hosts(tmp_path) == ()
    assert always_blocked_host("metadata.goog") and not always_blocked_host(_LAN_HOST)


def test_authorize_tool_requires_reason_and_hosts(tmp_path):
    tool = AuthorizeNetworkHostTool(_agent(tmp_path))
    assert tool.execute({"action": "grant", "hosts": [_LAN_HOST], "confirmed": True}).error_code == "TOOL_INVALID_ARGUMENTS"
    assert tool.execute({"action": "grant", "reason": "x", "confirmed": True}).error_code == "TOOL_INVALID_ARGUMENTS"


# ---- boundary 接线:授权在 run 中途落盘,下一次工具调用就能看到(新鲜读,不走 init 缓存) ----

def test_write_boundary_attaches_owner_network_grants(tmp_path):
    agent = _agent(tmp_path)
    params = SimpleNamespace(write_boundary=None, run_id="", task_attributes=None, delivery_contract=None)
    # 无授权:不添 allowed_private_hosts 键(闸维持默认拦截)
    empty = write_boundary_with_runtime_ledger(agent, params) or {}
    assert "allowed_private_hosts" not in empty
    # run 中途落一条授权 → 同一 agent 下一次构造立即可见
    create_network_host_grant(tmp_path, CreateNetworkHostGrant(host=_LAN_HOST, reason="用户点名"))
    boundary = write_boundary_with_runtime_ledger(agent, params)
    assert boundary["allowed_private_hosts"] == [_LAN_HOST]
    # 与调用方显式传入的白名单取并集,不覆盖
    params2 = SimpleNamespace(
        write_boundary={"allowed_private_hosts": ["10.9.9.9"]},
        run_id="", task_attributes=None, delivery_contract=None,
    )
    merged = write_boundary_with_runtime_ledger(agent, params2)
    assert merged["allowed_private_hosts"] == ["10.9.9.9", _LAN_HOST]


# ---- 两道闸:同一份白名单同时放行(接线终点) ----

def test_network_safety_gate_honors_granted_host():
    resolver = lambda _host: (_LAN_HOST,)
    blocked = evaluate_network_safety_gate(NetworkSafetyFacts(url=_LAN_URL, resolver=resolver))
    assert not blocked.allowed and blocked.finding_codes[0] == "NETWORK_PRIVATE_HOST_BLOCKED"
    allowed = evaluate_network_safety_gate(
        NetworkSafetyFacts(url=_LAN_URL, resolver=resolver, allowed_private_hosts=(_LAN_HOST,))
    )
    assert allowed.allowed
    # 白名单救不回永久拦截段(授权≠放松安全模型)
    metadata = evaluate_network_safety_gate(
        NetworkSafetyFacts(
            url="http://169.254.169.254/latest/meta-data",
            resolver=lambda _h: ("169.254.169.254",),
            allowed_private_hosts=("169.254.169.254",),
        )
    )
    assert not metadata.allowed


def test_path_url_command_gate_honors_granted_host(tmp_path):
    payload = {"tool": "web_fetch", "url": _LAN_URL}
    blocked = evaluate_path_url_command_gate(PathUrlCommandFacts(payload=payload, workspace_root=tmp_path))
    assert not blocked.allowed
    allowed = evaluate_path_url_command_gate(
        PathUrlCommandFacts(payload=payload, workspace_root=tmp_path, allowed_private_hosts=(_LAN_HOST,))
    )
    assert allowed.allowed


# ---- 撞闸错误自带授权自纠通路(真机②:子代理不知有授权路,一撞就 abandon 拖崩编队) ----

def test_private_block_error_carries_authorization_path():
    result = _network_safety_error("web_fetch", _LAN_URL, lambda _h: (_LAN_HOST,))
    assert result is not None and result.error_code == "NETWORK_PRIVATE_HOST_BLOCKED"
    for needle in ("authorize_network_host", "capability_request", _LAN_HOST, "放弃"):
        assert needle in result.output, needle
    # 白名单命中 → 无错误(同一入口放行)
    assert _network_safety_error("web_fetch", _LAN_URL, lambda _h: (_LAN_HOST,), allowed_private_hosts=(_LAN_HOST,)) is None


# ---- 端到端(无 LLM):授权工具 → owner 存储 → boundary 构造 → registry 贴闸 → web_fetch 真取数 ----

def test_end_to_end_authorize_then_fetch_private_host(tmp_path):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from pathlib import Path as _Path

    from agent_py_agent.agent.tooling.registry_invoke import RegistryToolInvokeRequest, invoke_registry_tool
    from agent_py_agent.agent.tooling.web import WebFetchTool

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server 接口
            body = json.dumps({"items": [{"seq": 1, "result": "captured"}], "next_cursor": 2}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{port}/pull?since=0&limit=2"
        agent = _agent(tmp_path)
        tools = {"web_fetch": WebFetchTool(max_chars=2000, timeout=5)}

        def invoke():
            params = SimpleNamespace(write_boundary=None, run_id="", task_attributes=None, delivery_contract=None)
            boundary = write_boundary_with_runtime_ledger(agent, params)
            return invoke_registry_tool(RegistryToolInvokeRequest(
                tool_name="web_fetch", payload={"tool": "web_fetch", "url": url, "mode": "json"},
                tools=tools, workspace_root=_Path.cwd(), workspace_roots=None,
                allowed_tools=["web_fetch"], write_boundary=boundary,
            ))

        blocked = invoke()
        assert blocked.ok is False and blocked.error_code == "NETWORK_PRIVATE_HOST_BLOCKED"
        assert "authorize_network_host" in blocked.output
        grant = AuthorizeNetworkHostTool(agent).execute(
            {"action": "grant", "hosts": [f"127.0.0.1:{port}"], "reason": "e2e", "confirmed": True}
        )
        assert grant.ok
        fetched = invoke()  # 授权在 run 中途生效:同一条调用链立即放行并真取到数据
        assert fetched.ok is True and "captured" in fetched.output
    finally:
        server.shutdown()
