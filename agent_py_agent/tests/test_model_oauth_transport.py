"""真实回环 HTTP 验证认证传输；服务端是假协议夹具，不算真实账号登录验收。"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent_py_agent.agent.settings.model_oauth_wire import oauth_post
from agent_py_agent.agent.settings.model_provider_schema import ModelProfileError


@pytest.fixture
def oauth_http():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            calls.append(self.path)
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path == "/redirect":
                self.send_response(307)
                self.send_header("Location", "/secret-sink")
                self.end_headers()
            elif self.path == "/pending":
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b"not JSON; no confidential body may escape")
            else:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True}).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


def test_login_does_not_forward_form_secrets_to_redirect(oauth_http):
    base, calls = oauth_http
    with pytest.raises(ModelProfileError) as result:
        oauth_post(base + "/redirect", {"refresh_token": "private-refresh"})
    assert calls == ["/redirect"] and "private-refresh" not in str(result.value)


def test_non_json_authorization_pending_preserves_status_without_raw_body(oauth_http):
    base, _ = oauth_http
    assert oauth_post(base + "/pending", {"device_code": "private-device"}) == (403, {})
