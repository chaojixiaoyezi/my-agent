"""G1：只用临时数据根和随机端口传输替身，不读真实 secrets。

Full Access 下模型可读数据根、关闭沙箱的插件与宿主同信任域；本片只验证
持久文件和隐藏路径声明，不把声明当成真实 Seatbelt/bwrap 隔离证据。
"""
from __future__ import annotations

import http.client
import json
import os
import re
import stat
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.auth.manager import AuthManager
from agent_py_agent.agent.auth.middleware import AuthMiddleware
from agent_py_agent.agent.gateway_parts import http_service, local_client_token
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.local_client_token import (
    LocalClientCredentialError,
    ensure_local_client_credential,
    load_local_client_credential,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.plugin_sandbox import plugin_sandbox_spec
from agent_py_agent.agent.tooling.shell import _subprocess_text_env


def _credential_path(root):
    return root / "secrets" / "gateway-local-client-token"


def test_missing_creates_private_credential_and_restart_reuses(tmp_path, monkeypatch):
    token = ensure_local_client_credential(tmp_path)
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", token)
    path = _credential_path(tmp_path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    before = path.read_bytes(), path.stat().st_mtime_ns
    monkeypatch.setattr(local_client_token, "secrets", SimpleNamespace(token_urlsafe=lambda _: pytest.fail("不得轮换")), raising=False)
    assert ensure_local_client_credential(tmp_path) == token
    assert load_local_client_credential(tmp_path) == token
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_concurrent_ensure_only_generates_once(tmp_path):
    with ThreadPoolExecutor(max_workers=4) as pool:
        tokens = list(pool.map(ensure_local_client_credential, [tmp_path] * 8))
    assert tokens[0] and len(set(tokens)) == 1
    assert load_local_client_credential(tmp_path) == tokens[0]


def test_read_missing_has_reason_without_creating(tmp_path):
    with pytest.raises(LocalClientCredentialError) as caught:
        load_local_client_credential(tmp_path)
    assert caught.value.reason_code == "LOCAL_CLIENT_CREDENTIAL_MISSING"
    assert not (tmp_path / "secrets").exists()


@pytest.mark.parametrize("target,mode", [("file", 0o644), ("directory", 0o755)])
def test_read_rejects_permissions_without_repair_or_rotation(tmp_path, target, mode):
    token = ensure_local_client_credential(tmp_path)
    assert token
    path = _credential_path(tmp_path)
    (path if target == "file" else path.parent).chmod(mode)
    with pytest.raises(LocalClientCredentialError) as caught:
        load_local_client_credential(tmp_path)
    assert caught.value.reason_code == "LOCAL_CLIENT_CREDENTIAL_PERMISSIONS"
    with pytest.raises(LocalClientCredentialError):
        ensure_local_client_credential(tmp_path)
    assert path.read_text().strip() == token
    assert token not in str(caught.value)


@pytest.mark.parametrize("content", [b"", b"bad", b"\xff", b"a" * 4096])
def test_read_corrupt_has_separate_reason_and_never_echoes(tmp_path, content):
    path = _credential_path(tmp_path)
    path.parent.mkdir(mode=0o700)
    path.write_bytes(content)
    path.chmod(0o600)
    with pytest.raises(LocalClientCredentialError) as caught:
        load_local_client_credential(tmp_path)
    assert caught.value.reason_code == "LOCAL_CLIENT_CREDENTIAL_INVALID"
    with pytest.raises(LocalClientCredentialError):
        ensure_local_client_credential(tmp_path)
    assert path.read_bytes() == content


def test_read_rejects_symlink(tmp_path):
    token = ensure_local_client_credential(tmp_path)
    assert token
    path = _credential_path(tmp_path)
    target = tmp_path / "elsewhere"
    path.replace(target)
    path.symlink_to(target)
    with pytest.raises(LocalClientCredentialError) as caught:
        load_local_client_credential(tmp_path)
    assert caught.value.reason_code == "LOCAL_CLIENT_CREDENTIAL_INVALID"


def test_plugin_hidden_paths_include_credential_and_directory(tmp_path):
    home = tmp_path / "owners" / "local" / "main"
    spec = plugin_sandbox_spec(cwd=home, data_dir=home / "plugin-data", owner_home=home)
    assert _credential_path(tmp_path) in spec.hidden_paths
    # Linux H2 只盖目录；不能只登记文件却让 bwrap 忽略。
    assert tmp_path / "secrets" in spec.hidden_paths


@pytest.mark.parametrize("owner_scoped", [False, True])
def test_credential_never_enters_model_environment(tmp_path, monkeypatch, owner_scoped):
    monkeypatch.setattr(os, "environ", {"PATH": "/bin", "LANG": "C.UTF-8"})
    token = ensure_local_client_credential(tmp_path)
    assert token
    env = _subprocess_text_env(tmp_path if owner_scoped else None, sandboxed=True)
    assert token not in json.dumps(env)
    assert "GATEWAY_TOKEN" not in " ".join(env)


def test_start_hook_uses_canonical_root_and_restart_does_not_delete(tmp_path, monkeypatch, caplog):
    from agent_py_agent.agent.gateway_parts.http_handlers import handle_status

    root = tmp_path / "data-root"
    agent = SimpleNamespace(home_paths=SimpleNamespace(root=root))
    paths = gateway_paths_from_root(tmp_path / "separate-queue")
    class Listener:
        server_address = ("127.0.0.1", 0)
        def serve_forever(self):
            pass
        def shutdown(self):
            pass
        def server_close(self):
            pass
    monkeypatch.setattr(http_service, "GatewayBoundedHTTPServer", lambda *_: Listener())
    monkeypatch.setattr(http_service, "update_json_file_atomic", lambda *_: None)
    server = GatewayHTTPServer(0, paths, params=GatewayHTTPServerParams(agent=agent))
    server.start()
    try:
        assert _credential_path(root).is_file()
        token = load_local_client_credential(root)
        responses = []
        handler = SimpleNamespace(_send_json=lambda status, body: responses.append((status, body)))
        handle_status(handler, server)
        assert responses[-1][0] == 200
        assert token not in json.dumps(responses)
        assert token not in caplog.text
    finally:
        server.stop()
    server.start()
    try:
        assert load_local_client_credential(root) == token
    finally:
        server.stop()


def _get(port, path):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


@pytest.mark.parametrize("damage,expected_reason", [
    ("corrupt", "LOCAL_CLIENT_CREDENTIAL_INVALID"),
    ("file_mode", "LOCAL_CLIENT_CREDENTIAL_PERMISSIONS"),
    ("dir_mode", "LOCAL_CLIENT_CREDENTIAL_PERMISSIONS"),
])
def test_unavailable_credential_still_starts_and_reports_reason(tmp_path, damage, expected_reason):
    # G2a 降级：凭据坏/权限不对不拦启动；/status 只报原因码；坏文件与坏权限不被覆盖、不轮换。
    root = tmp_path / "data-root"
    ensure_local_client_credential(root)
    path = _credential_path(root)
    if damage == "corrupt":
        path.write_bytes(b"not-a-credential")
    elif damage == "file_mode":
        path.chmod(0o644)
    else:
        path.parent.chmod(0o755)
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    agent = SimpleNamespace(home_paths=SimpleNamespace(root=root))
    server = GatewayHTTPServer(0, gateway_paths_from_root(tmp_path / "queue"),
                               params=GatewayHTTPServerParams(agent=agent))
    server.start()
    try:
        assert server.local_credential_status == f"unavailable:{expected_reason}"
        status, body = _get(server.server.server_address[1], "/status")
        assert status == 200 and body["local_credential"] == f"unavailable:{expected_reason}"
        server.stop()
        server.start()
        assert server.local_credential_status == f"unavailable:{expected_reason}"
    finally:
        server.stop()
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_missing_home_paths_reports_no_data_root_and_keeps_counting(tmp_path, monkeypatch):
    # 假 Agent 没有 home_paths：不回退真实默认 home，记 no_data_root 照常启动；回环请求照常计数。
    monkeypatch.setattr(http_service.GatewayHTTPHandler, "_handle_result",
                        lambda handler: handler._send_json(200, {"ok": True}))
    middleware = AuthMiddleware(AuthManager(admin_user_id="admin", auth_enabled=True))
    server = GatewayHTTPServer(0, gateway_paths_from_root(tmp_path / "queue"),
                               params=GatewayHTTPServerParams(agent=SimpleNamespace(), auth_middleware=middleware))
    server.start()
    try:
        assert server.local_credential_status == "unavailable:no_data_root"
        assert middleware.has_gateway_credential({}) is False
        port = server.server.server_address[1]
        assert _get(port, "/status")[1]["local_credential"] == "unavailable:no_data_root"
        assert _get(port, "/result/fixture-request")[0] == 200
        facts = _get(port, "/status")[1]["uncredentialed_loopback_by_endpoint"]
        assert facts.get("/result/*", {}).get("count") == 1
    finally:
        server.stop()
