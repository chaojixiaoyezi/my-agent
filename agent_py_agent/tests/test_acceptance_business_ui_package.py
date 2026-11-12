"""包内 UI 的真实 HTTP 回归；隔离 home，不访问正式 Gateway 或模型。"""
from __future__ import annotations

import http.client
import json
import socket
from html.parser import HTMLParser
from importlib.resources import files
from types import SimpleNamespace
from urllib.parse import urljoin, urlsplit

import pytest

from agent_py_agent.agent.gateway_parts import business_ui_http as ui
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.agent.user_space.home_layout import home_paths

ASSETS = {
    "index.html": ("/client/business/ui", "text/html; charset=utf-8"),
    "app.js": ("/client/business/ui/app.js", "application/javascript; charset=utf-8"),
}
USER = "ui-package-alice"


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.meta = {}
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta" and attrs.get("name"):
            self.meta[attrs["name"]] = attrs.get("content", "")
        if tag == "script" and attrs.get("src"):
            self.scripts.append(attrs["src"])


def asset_bytes(name):
    return files("agent_py_agent").joinpath("static", "business_space_ui", name).read_bytes()


@pytest.fixture
def ui_server(tmp_path, monkeypatch):
    root = tmp_path / "home"
    root.mkdir()
    for key in ("HOME", "USERPROFILE", "MY_AGENT_HOME"):
        monkeypatch.setenv(key, str(root))
    agent = SimpleNamespace(
        home_paths=home_paths(root),
        config=AgentConfig(business_spaces_enabled=True, model_backend="echo", my_agent_home=str(root)),
    )
    server = GatewayHTTPServer(
        0, gateway_paths_from_root(root / "gateway"),
        params=GatewayHTTPServerParams(agent=agent, bind_host="127.0.0.1"),
    )
    try:
        server.start()
        yield server
    finally:
        server.stop()


def exchange(server, method, path, *, authenticated=True, user=USER, headers=None, body=None):
    port = server.server.server_address[1]
    request_headers = {
        "Host": f"127.0.0.1:{port}", "Connection": "close",
        "X-User-Id": user, "X-Channel": "feishu",
    }
    if authenticated:
        request_headers["X-Gateway-Token"] = server._local_client_credential
    request_headers.update(headers or {})
    if method == "HEAD":
        # HTTPResponse 会隐藏 HEAD 正文，用原始 socket 防止空正文假阳性。
        request = f"HEAD {path} HTTP/1.1\r\n"
        request += "".join(f"{key}: {value}\r\n" for key, value in request_headers.items())
        with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
            connection.sendall((request + "\r\n").encode("ascii"))
            with connection.makefile("rb") as stream:
                status = int(stream.readline().split(b" ", 2)[1])
                response_headers = http.client.parse_headers(stream)
                return status, {k.lower(): v for k, v in response_headers.items()}, stream.read()
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
    finally:
        connection.close()


def security_headers(headers, content_type):
    assert headers["content-type"] == content_type
    assert headers["content-security-policy"] == "default-src 'self'; style-src 'self' 'unsafe-inline'"
    assert headers["cache-control"] == "no-store"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["connection"].lower() == "close"


def test_native_get_follows_emitted_script_url(ui_server):
    status, headers, body = exchange(ui_server, "GET", "/client/business/ui?view=acceptance")
    assert status == 200
    security_headers(headers, ASSETS["index.html"][1])
    assert int(headers["content-length"]) == len(body)
    page = Page()
    page.feed(body.decode())
    token = page.meta["my-agent-csrf"]
    assert token == page.meta["csrf-token"]
    owner = f"providers/feishu/users/{USER}"
    assert ui.verify_csrf_token(ui_server, token, owner, USER)
    assert not ui.verify_csrf_token(ui_server, token, owner, "other-user")
    assert not ui.verify_csrf_token(ui_server, token, "other-owner", USER)
    assert not ui.verify_csrf_token(SimpleNamespace(_business_csrf_secret=b"other"), token, owner, USER)
    assert len(page.scripts) == 1
    script_url = urlsplit(urljoin("http://localhost/client/business/ui", page.scripts[0]))
    assert script_url.path == ASSETS["app.js"][0]
    status, headers, script = exchange(ui_server, "GET", script_url.path)
    assert status == 200
    security_headers(headers, ASSETS["app.js"][1])
    assert script == asset_bytes("app.js")
    expected = asset_bytes("index.html").decode()
    for name in ("csrf-token", "my-agent-csrf"):
        expected = expected.replace(f'<meta name="{name}" content="">', f'<meta name="{name}" content="{token}">')
    assert body == expected.encode()


@pytest.mark.parametrize("filename", ASSETS)
def test_native_head_matches_get_without_wire_body(ui_server, filename):
    path, content_type = ASSETS[filename]
    status, headers, body = exchange(ui_server, "HEAD", path + "?v=1")
    assert status == 200
    assert body == b""
    security_headers(headers, content_type)
    get_status, get_headers, get_body = exchange(ui_server, "GET", path + "?v=1")
    assert get_status == 200
    assert headers["content-length"] == str(len(get_body))
    for name in ("content-type", "content-length", "content-security-policy", "cache-control", "x-content-type-options"):
        assert headers[name] == get_headers[name]


@pytest.mark.parametrize("filename", ASSETS)
@pytest.mark.parametrize("fault,code", [
    ("missing", "BUSINESS_UI_ASSET_MISSING"),
    ("directory", "BUSINESS_UI_ASSET_UNREADABLE"),
    ("invalid_utf8", "BUSINESS_UI_ASSET_INVALID"),
])
def test_resource_fault_is_structured_500(ui_server, tmp_path, monkeypatch, filename, fault, code):
    broken = tmp_path / "broken" / filename
    if fault == "directory":
        broken.mkdir(parents=True)
    elif fault == "invalid_utf8":
        broken.parent.mkdir(parents=True)
        broken.write_bytes(b"\xff")
    try:
        broken.read_bytes().decode("utf-8")
    except (OSError, UnicodeError) as error:
        cause_name = type(error).__name__
    else:
        raise AssertionError("fault fixture unexpectedly readable")
    original = ui._locate_ui_file
    monkeypatch.setattr(ui, "_locate_ui_file", lambda name: broken if name == filename else original(name))
    status, headers, body = exchange(ui_server, "GET", ASSETS[filename][0])
    assert status == 500
    payload = json.loads(body)
    assert payload["error"]["code"] == code
    assert payload["effect_outcome"] == payload["error"]["outcome"] == "not_started"
    assert payload["error"]["retryable"] is False
    assert filename in payload["error"]["message"]
    assert cause_name in payload["error"]["message"]
    assert str(broken) not in body.decode()
    assert ui_server._local_client_credential not in body.decode()
    assert b"csrf-" not in body
    assert headers["content-type"] == "application/json; charset=utf-8"
    assert int(headers["content-length"]) == len(body)
    status, head_headers, head_body = exchange(ui_server, "HEAD", ASSETS[filename][0])
    assert status == 500
    assert head_body == b""
    assert head_headers["content-length"] == headers["content-length"]


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_auth_and_cross_site_before_asset_read(ui_server, monkeypatch, method):
    def forbidden(_name):
        raise AssertionError("rejected request read resource")

    monkeypatch.setattr(ui, "_locate_ui_file", forbidden)
    for path, _ in ASSETS.values():
        for authenticated, headers in [
            (False, {}), (True, {"X-Gateway-Token": "invalid"}),
            (True, {"Origin": "https://untrusted.invalid"}),
            (True, {"Sec-Fetch-Site": "cross-site"}),
        ]:
            status, _, body = exchange(ui_server, method, path, authenticated=authenticated, headers=headers)
            assert status == 403
            assert b"csrf-" not in body
            if method == "HEAD":
                assert body == b""


def test_injected_csrf_consumed_by_native_business_route(ui_server):
    status, _, content = exchange(ui_server, "GET", ASSETS["index.html"][0])
    assert status == 200
    page = Page()
    page.feed(content.decode())
    token = page.meta["my-agent-csrf"]
    origin = f"http://127.0.0.1:{ui_server.server.server_address[1]}"
    cases = [
        ({}, USER, 200), ({"Origin": origin}, USER, 403),
        ({"Origin": origin, "X-CSRF-Token": "invalid"}, USER, 403),
        ({"Origin": origin, "X-CSRF-Token": token}, USER, 200),
        ({"Origin": origin, "X-CSRF-Token": token, "Sec-Fetch-Site": "cross-site"}, USER, 403),
        ({"Origin": "https://untrusted.invalid", "X-CSRF-Token": token}, USER, 403),
        ({"Origin": origin, "X-CSRF-Token": token}, "ui-package-bob", 403),
    ]
    for index, (headers, user, expected) in enumerate(cases):
        request = {"action": "system.capabilities", "params": {}, "request_id": f"ui-cap-{index}"}
        status, _, body = exchange(ui_server, "POST", "/client/business", user=user,
            headers={"Content-Type": "application/json", **headers}, body=json.dumps(request).encode())
        assert status == expected
        payload = json.loads(body)
        if expected == 200:
            assert payload["request_id"] == request["request_id"]
            assert payload["state"] == "completed"
            assert payload["error"] is None
            assert isinstance(payload["data"]["supported_actions"], list)
        else:
            assert payload["error"]["code"] == "CAPABILITY_DENIED"


@pytest.mark.parametrize("path", ["/status", "/metrics", "/client/requests/a/events", "/client/business/ui/", "/client/business/ui/other.js"])
def test_head_does_not_expand_other_get_routes(ui_server, path):
    status, _, body = exchange(ui_server, "HEAD", path)
    assert status == 404
    assert body == b""


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "OPTIONS"])
def test_other_methods_not_added(ui_server, method):
    for path, _ in ASSETS.values():
        status, _, _ = exchange(ui_server, method, path)
        assert status == (404 if method == "POST" else 501)
