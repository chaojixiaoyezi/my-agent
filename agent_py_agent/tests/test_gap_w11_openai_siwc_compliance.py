"""SIWC真实回环OAuth/JWKS、私有档案与Responses组包；不代表真实OpenAI账号验收。"""
from __future__ import annotations

import base64
import json
import os
import stat
import threading
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import UUID, uuid4

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from agent_py_agent.agent.common.json_io import locked_json_path
from agent_py_agent.agent.settings import model_oauth as runtime
from agent_py_agent.agent.settings import model_oauth_schema as schema
from agent_py_agent.agent.settings import model_oauth_wire as wire
from agent_py_agent.agent.settings.model_profiles import (
    _save_profiles,
    execute_model_profile_operation,
    model_profiles_path,
    read_model_profiles,
    selected_model_config,
)
from agent_py_agent.agent.settings.model_provider_schema import ModelProfileError
from agent_py_agent.tests.test_model_oauth import host

API = "https://api.openai.com/v1"
ISSUER = "https://auth.openai.com"
SCOPE = "openid profile email offline_access resource.invoke chatgpt.tokens.use.direct"
CALLBACK = "http://127.0.0.1:1455/auth/callback"
CLIENT = "oaiapp_local-test-registration"


@pytest.fixture
def responses_replay_server():
    """完整经过本地 HTTP/SSE；拒绝丢失身份的回放，并核对停机后的描述符数。"""
    def fd_count():
        directory = "/dev/fd" if os.path.isdir("/dev/fd") else "/proc/self/fd"
        if not os.path.isdir(directory):
            return None
        with os.scandir(directory) as entries:
            return len(list(entries))

    before = fd_count()
    state = SimpleNamespace(calls=[], namespace="my_agent")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state.calls.append((self.path, body, dict(self.headers)))
            calls = [item for item in body["input"] if item.get("type") == "function_call"]
            if any(item.get("namespace") != state.namespace for item in calls):
                raw = json.dumps({"error": {"type": "invalid_request_error", "code": "invalid_value",
                    "param": "input", "message": "历史函数身份不一致"}}).encode()
                self.send_response(400)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            if len(state.calls) == 1:
                item = {"type": "function_call", "id": "fc-local", "call_id": "local-replay-call",
                        "name": "local_echo", "arguments": '{"text":"本机"}', "status": "completed"}
                if state.namespace is not None:
                    item["namespace"] = state.namespace
                events = [{"type": "response.output_item.done", "output_index": 0, "item": item},
                          {"type": "response.completed", "response": {"status": "completed", "output": []}}]
            else:
                events = [{"type": "response.output_text.delta", "delta": "本机回放完成"},
                          {"type": "response.completed", "response": {"status": "completed", "output": [
                              {"type": "message", "content": [{"type": "output_text", "text": "本机回放完成"}]}]}}]
            raw = "".join("data: " + json.dumps(event, ensure_ascii=False) + "\n\n" for event in events).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.base = f"http://127.0.0.1:{server.server_port}/v1"
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    worker.start()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
        assert not worker.is_alive() and server.fileno() == -1
        after = fd_count()
        if before is not None:
            fd_delta = after - before
            assert fd_delta == 0


def _encoded(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _integer(value: int) -> str:
    return _encoded(value.to_bytes((value.bit_length() + 7) // 8, "big"))


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def identity_server(monkeypatch, signing_key):
    """真实HTTP处理器解析表单并签发可验签JWT；不以假成功字典替代产品传输。"""
    state = SimpleNamespace(calls=[], claims={}, scope=SCOPE, nonce="", revoke_status=200,
                            lock_path=None, lock_free=None, before_token=None, alternate_key=None,
                            token_status=200, token_error="", before_revoke=None, incomplete_path="")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def send_json(self, value, status=200):
            raw = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            state.calls.append(("GET", self.path, None))
            if self.path == state.incomplete_path:
                return self.send_incomplete()
            if self.path == "/.well-known/openid-configuration":
                self.send_json({"issuer": state.base, "jwks_uri": state.base + "/jwks"})
            elif self.path == "/jwks":
                numbers = signing_key.public_key().public_numbers()
                self.send_json({"keys": [{"kty": "RSA", "use": "sig", "kid": "local-key", "alg": "RS256",
                                           "n": _integer(numbers.n), "e": _integer(numbers.e)}]})
            elif self.path == "/v1/models":
                self.send_json(state.catalog)
            else:
                self.send_json({}, 404)

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
            fields = {k: v[0] for k, v in parse_qs(raw).items()}
            state.calls.append(("POST", self.path, fields))
            if self.path.endswith("/oauth/revoke"):
                if state.before_revoke:
                    state.before_revoke()
                if state.lock_path:
                    try:
                        with locked_json_path(state.lock_path, blocking=False):
                            state.lock_free = True
                    except BlockingIOError:
                        state.lock_free = False
                if self.path == state.incomplete_path:
                    return self.send_incomplete()
                self.send_response(state.revoke_status)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path != "/api/accounts/oauth/token":
                self.send_json({}, 404)
                return
            if fields.get("client_id") != CLIENT or fields.get("resource") != API:
                self.send_json({"error": "invalid_client"}, 400)
                return
            if state.token_status != 200:
                self.send_json({"error": state.token_error}, state.token_status)
                return
            if state.before_token:
                state.before_token()
            claim = {"iss": state.base, "aud": CLIENT, "sub": "local-subject", "email": "fixture@example.test",
                     "exp": time.time() + 3600, "nonce": state.nonce, **state.claims}
            signing_input = _encoded(b'{"alg":"RS256","kid":"local-key"}') + "." + _encoded(json.dumps(claim).encode())
            signature = (state.alternate_key or signing_key).sign(signing_input.encode(), padding.PKCS1v15(), hashes.SHA256())
            refreshed = fields.get("grant_type") == "refresh_token"
            self.send_json({"access_token": "test-rotated-access" if refreshed else "test-access",
                            "refresh_token": "test-rotated-refresh" if refreshed else "test-refresh",
                            "id_token": signing_input + "." + _encoded(signature),
                            "token_type": "Bearer", "expires_in": 3600, "scope": state.scope})

        def send_incomplete(self):
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(b"10\r\ncut")
            self.wfile.flush()
            self.close_connection = True

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    state.base = f"http://127.0.0.1:{server.server_port}"
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    worker.start()
    monkeypatch.setattr(schema, "CHATGPT_ISSUER", state.base)
    monkeypatch.setattr(wire, "CHATGPT_ISSUER", state.base)
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
        assert not worker.is_alive()


def _new_owner(tmp_path):
    owner = host(tmp_path)
    execute_model_profile_operation(owner, "save_provider", {"provider_id": "account", "provider": {
        "display_name": "my-agent 本人ChatGPT", "api_base": API, "auth": {"mode": "chatgpt"}}})
    key = str(uuid4())
    execute_model_profile_operation(owner, "save_model", {"profile_id": key, "profile": {"provider_id": "account",
        "model_name": "siwc-test-model", "model_backend": "openai_responses", "model_context_window_tokens": 128000}})
    return owner, key


def _auth(owner):
    return read_model_profiles(model_profiles_path(owner.home_paths))["providers"]["account"]["auth"]


def _start(owner, redirect=CALLBACK):
    return execute_model_profile_operation(owner, "auth_browser_start", {"provider_id": "account", "redirect_uri": redirect})


def _complete(owner, started, **overrides):
    return execute_model_profile_operation(owner, "auth_browser_complete", {"provider_id": "account",
        "attempt_id": started["attempt_id"], "state": started["state"], "code": "test-code", "client_id": CLIENT, **overrides})


def _connect(owner, server):
    started = _start(owner)
    server.nonce = _auth(owner)["pending"]["nonce"]
    return _complete(owner, started)


def _config(owner, key):
    execute_model_profile_operation(owner, "set_default", {"profile_id": key})
    return selected_model_config(owner)


def test_official_config_and_legacy_records_both_remain_readable():
    direct = schema.oauth_config({"mode": "chatgpt"}, API)
    assert direct["client_id"] == "dynamic_agent_client"
    assert direct["token_url"] == ISSUER + "/api/accounts/oauth/token"
    assert direct["revocation_url"] == ISSUER + "/api/accounts/oauth/revoke"
    assert direct["resource"] == API and set(direct["scope"].split()) == set(SCOPE.split())
    legacy = schema.stored_oauth({"mode": "chatgpt", "client_id": schema.CHATGPT_CLIENT,
                                 "access_token": "old-test-access", "account_id": "old-account", "generation": "old-generation"}, schema.CHATGPT_BASE)
    assert legacy["client_id"] == schema.CHATGPT_CLIENT and legacy["access_token"] == "old-test-access"


def test_host_id_is_persisted_before_authorize_and_reused(tmp_path):
    owner, _ = _new_owner(tmp_path)
    first = _start(owner)
    auth = _auth(owner)
    host_id = auth["ext_agent_host_id"]
    assert host_id.startswith("urn:uuid:") and UUID(host_id.removeprefix("urn:uuid:")).version == 4
    query = parse_qs(urlsplit(first["authorize_url"]).query)
    assert first["authorize_url"].startswith(ISSUER + "/api/accounts/authorize?")
    assert query["agent_name_hint"] == ["my-agent"] and query["ext_agent_host_id"] == [host_id]
    assert query["resource"] == [API] and query["nonce"] == [auth["pending"]["nonce"]]
    assert "codex_cli_simplified_flow" not in query and "id_token_add_organizations" not in query
    second = _start(owner)
    assert _auth(owner)["ext_agent_host_id"] == host_id
    assert second["state"] != first["state"]
    execute_model_profile_operation(owner, "auth_logout", {"provider_id": "account"})
    assert _auth(owner)["ext_agent_host_id"] == host_id


def test_signed_code_exchange_and_returning_account_roundtrip(tmp_path, identity_server):
    owner, key = _new_owner(tmp_path)
    assert _connect(owner, identity_server)["status"] == "connected"
    auth = _auth(owner)
    assert auth["client_id"] == CLIENT and auth["subject"] == "local-subject"
    assert auth["id_token"] and auth["scopes"] == SCOPE.split()
    assert "pending" not in auth and stat.S_IMODE(model_profiles_path(owner.home_paths).stat().st_mode) == 0o600
    posts = [row for row in identity_server.calls if row[0] == "POST"]
    assert posts[0][2]["client_id"] == CLIENT and posts[0][2]["resource"] == API
    assert posts[0][2]["redirect_uri"] == CALLBACK and posts[0][2]["code_verifier"]
    config = _config(owner, key)
    assert runtime.request_credentials(config.model_auth_ref, API) == ("test-access", {})
    again = _start(owner, "http://127.0.0.1:54321/auth/callback")
    query = parse_qs(urlsplit(again["authorize_url"]).query)
    assert query["client_id"] == [CLIENT] and "agent_name_hint" not in query
    assert query["id_token_hint"] == [auth["id_token"]] and query["login_hint"] == ["fixture@example.test"]
    assert query["ext_agent_host_id"] == [auth["ext_agent_host_id"]]
    assert "prompt" not in query and "force_reconsent" not in query
    identity_server.nonce = _auth(owner)["pending"]["nonce"]
    assert _complete(owner, again, client_id="")["status"] == "connected"


@pytest.mark.parametrize("overrides", [{"client_id": ""}, {"client_id": "dynamic_agent_client"},
                                       {"client_id": "oaiapp_other", "state": "wrong"}])
def test_initial_callback_requires_issued_client_before_network(tmp_path, identity_server, overrides):
    owner, _ = _new_owner(tmp_path)
    started = _start(owner)
    if overrides.get("client_id") == "oaiapp_other":
        overrides = {**overrides, "state": "wrong"}
    with pytest.raises(ModelProfileError):
        _complete(owner, started, **overrides)
    assert identity_server.calls == []
    assert _auth(owner)["pending"]["id"] == started["attempt_id"]


def test_reauthorization_mismatched_client_is_rejected_before_exchange(tmp_path, identity_server):
    owner, _ = _new_owner(tmp_path)
    _connect(owner, identity_server)
    started = _start(owner)
    before = len(identity_server.calls)
    with pytest.raises(ModelProfileError):
        _complete(owner, started, client_id="oaiapp_other")
    assert len(identity_server.calls) == before and _auth(owner)["client_id"] == CLIENT


@pytest.mark.parametrize("claims", [{"iss": "https://elsewhere.example.test"}, {"aud": "oaiapp_other"},
                                    {"exp": 1}, {"nonce": "wrong-nonce"}, {"sub": ""}])
def test_invalid_id_claims_never_replace_credentials(tmp_path, identity_server, claims):
    owner, _ = _new_owner(tmp_path)
    started = _start(owner)
    identity_server.nonce = _auth(owner)["pending"]["nonce"]
    identity_server.claims = claims
    with pytest.raises(ModelProfileError):
        _complete(owner, started)
    assert not _auth(owner).get("access_token") and _auth(owner)["client_id"] == "dynamic_agent_client"


def test_signature_and_returning_subject_are_verified(tmp_path, identity_server):
    owner, _ = _new_owner(tmp_path)
    started = _start(owner)
    identity_server.nonce = _auth(owner)["pending"]["nonce"]
    identity_server.alternate_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(ModelProfileError):
        _complete(owner, started)
    identity_server.alternate_key = None
    assert _connect(owner, identity_server)["status"] == "connected"
    original = _auth(owner)
    started = _start(owner)
    identity_server.nonce = _auth(owner)["pending"]["nonce"]
    identity_server.claims = {"sub": "another-subject"}
    with pytest.raises(ModelProfileError):
        _complete(owner, started)
    assert _auth(owner)["generation"] == original["generation"]
    assert _auth(owner)["subject"] == original["subject"]


def test_initial_callback_ports_are_fixed_but_registered_client_ports_can_vary(tmp_path, identity_server):
    owner, _ = _new_owner(tmp_path)
    with pytest.raises(ModelProfileError):
        _start(owner, "http://127.0.0.1:54321/auth/callback")
    _connect(owner, identity_server)
    assert _start(owner, "http://127.0.0.1:54321/auth/callback")["status"] == "pending"
    for invalid in ("http://localhost:54321/auth/callback", "http://127.0.0.1:54321/callback",
                    "http://127.0.0.1:54321/auth/callback?secret=1"):
        with pytest.raises(ModelProfileError):
            _start(owner, invalid)


def test_missing_plan_scope_retains_identity_but_disables_inference(tmp_path, identity_server):
    owner, key = _new_owner(tmp_path)
    identity_server.scope = "openid email"
    result = _connect(owner, identity_server)
    assert result["status"] == "scope_not_granted" and result["plan_usage_enabled"] is False
    assert _auth(owner)["subject"] == "local-subject" and _auth(owner)["id_token"]
    assert schema.has_credential({"auth": _auth(owner)}) is False
    with pytest.raises(ModelProfileError):
        _config(owner, key)


def test_refresh_uses_issued_client_resource_and_atomically_rotates(tmp_path, identity_server):
    owner, key = _new_owner(tmp_path)
    _connect(owner, identity_server)
    config = _config(owner, key)
    path = model_profiles_path(owner.home_paths)
    data = read_model_profiles(path)
    data["providers"]["account"]["auth"]["expires_at"] = 1
    _save_profiles(path, data)
    assert runtime.request_credentials(config.model_auth_ref, API) == ("test-rotated-access", {})
    saved = _auth(owner)
    assert saved["refresh_token"] == "test-rotated-refresh" and saved["access_token"] == "test-rotated-access"
    assert saved["scopes"] == SCOPE.split() and saved["expires_at"] > time.time()
    refresh = [row[2] for row in identity_server.calls if row[0] == "POST"][-1]
    assert refresh == {"client_id": CLIENT, "grant_type": "refresh_token", "refresh_token": "test-refresh", "resource": API}


@pytest.mark.parametrize("remote_status", [200, 503])
def test_logout_revokes_outside_profile_lock_and_retains_registration(tmp_path, identity_server, remote_status):
    owner, key = _new_owner(tmp_path)
    _connect(owner, identity_server)
    config = _config(owner, key)
    previous = _auth(owner)
    identity_server.revoke_status = remote_status
    identity_server.lock_path = model_profiles_path(owner.home_paths)
    result = execute_model_profile_operation(owner, "auth_logout", {"provider_id": "account"})
    assert result["status"] == "signed_out" and identity_server.lock_free is True
    assert result["remote_revocation_confirmed"] is (remote_status == 200)
    if remote_status != 200:
        assert "ChatGPT" in result["message"] and "https://chatgpt.com/settings" in result["message"]
    saved = _auth(owner)
    assert not set(saved).intersection({"access_token", "refresh_token", "id_token", "pending"})
    assert saved["client_id"] == CLIENT and saved["ext_agent_host_id"] == previous["ext_agent_host_id"]
    assert saved["subject"] == previous["subject"]
    revoked = [row[2] for row in identity_server.calls if row[1].endswith("/oauth/revoke")]
    assert revoked == [{"token": "test-refresh", "token_type_hint": "refresh_token", "client_id": CLIENT}]
    with pytest.raises(ModelProfileError):
        runtime.request_credentials(config.model_auth_ref, API)
    again = _start(owner)
    query = parse_qs(urlsplit(again["authorize_url"]).query)
    assert query["client_id"] == [CLIENT] and "id_token_hint" not in query and "agent_name_hint" not in query


def test_cancel_during_real_exchange_does_not_revive_login(tmp_path, identity_server):
    owner, _ = _new_owner(tmp_path)
    started = _start(owner)
    identity_server.nonce = _auth(owner)["pending"]["nonce"]
    identity_server.before_token = lambda: execute_model_profile_operation(owner, "auth_cancel", {
        "provider_id": "account", "attempt_id": started["attempt_id"]})
    with pytest.raises(ModelProfileError, match="迟到"):
        _complete(owner, started)
    assert not _auth(owner).get("access_token")


def test_siwc_model_catalog_uses_public_models_and_data_array(tmp_path, identity_server, monkeypatch):
    from dataclasses import replace

    from agent_py_agent.agent.settings import model_provider_network as network

    owner, _ = _new_owner(tmp_path)
    _connect(owner, identity_server)
    sent = []
    actual_get = network.get_json
    identity_server.catalog = {"data": [
        {"id": "shown-model", "display_name": "Shown", "context_window": 128000, "visibility": "list"},
        {"slug": "hidden-model", "context_window": 128000, "visibility": "hide"},
        {"slug": "capacity-unspecified", "display_name": "Ask capacity"}]}

    def local_transport(request):
        sent.append(request)
        return actual_get(replace(request, api_base=identity_server.base + "/v1"))

    monkeypatch.setattr(network, "get_json", local_transport)
    result = execute_model_profile_operation(owner, "discover", {"provider_id": "account"})
    assert [row["model_name"] for row in result["models"]] == ["shown-model", "capacity-unspecified"]
    assert result["models"][0]["display_name"] == "Shown"
    assert result["models"][1]["model_context_window_tokens"] == 0
    assert sent[0].url == API + "/models" and sent[0].allow_redirects is False
    assert "ChatGPT-Account-ID" not in sent[0].headers


@pytest.mark.parametrize("with_tools", [False, True])
def test_direct_responses_omits_sampling_and_uses_http_sse(tmp_path, identity_server, monkeypatch, with_tools):
    from agent_py_agent.agent.backends import get_backend
    from agent_py_agent.agent.backends.base import ProviderRequestOptions
    from agent_py_agent.agent.backends.http import HttpBackend
    from agent_py_agent.agent.tooling.runtime_contracts import ToolChoice

    owner, key = _new_owner(tmp_path)
    _connect(owner, identity_server)
    backend = get_backend("openai_responses", _config(owner, key))
    backend.temperature_explicit, backend.temperature, backend.top_p = True, 0.7, 0.9
    sent = []

    def http_stream(self, path, payload, headers, *, options=None):
        sent.append(self._gateway_request(path, payload, headers, options=options))
        output = ([{"type": "function_call", "name": "local_echo", "namespace": "my_agent",
                    "call_id": "local-call", "arguments": '{"text":"本机"}'}] if with_tools else
                  [{"type": "message", "content": [{"type": "output_text", "text": "local fixture completed"}]}])
        yield json.dumps({"type": "response.completed", "response": {"status": "completed", "output": output}})

    monkeypatch.setattr(HttpBackend, "request_stream_iter", http_stream)
    tools = [{"name": "local_echo", "description": "本机回声", "input_schema": {"type": "object",
              "properties": {"text": {"type": "string"}}, "required": ["text"]}}] if with_tools else None
    response = backend.generate("你好", tools=tools, tool_choice=ToolChoice.specific("local_echo") if with_tools else None,
                                request_options=ProviderRequestOptions(system_instruction="my-agent 指令"))
    if with_tools:
        assert response.tool_use_blocks[0]["name"] == "local_echo"
        assert response.tool_use_blocks[0]["input"] == {"text": "本机"}
    else:
        assert response.text == "local fixture completed"
    assert sent[0].url == API + "/responses" and sent[0].allow_redirects is False
    body = sent[0].payload
    unsupported = {"temperature", "top_p", "max_output_tokens", "truncation", "user", "metadata", "max_tool_calls",
                   "background", "conversation", "prompt", "prompt_cache_retention", "safety_identifier",
                   "top_logprobs", "moderation", "multi_agent", "previous_response_id"}
    assert not unsupported.intersection(body)
    assert body["stream"] is True and body["store"] is False and isinstance(body["input"], list)
    assert body["instructions"] == "my-agent 指令" and all(item.get("role") != "system" for item in body["input"])
    if with_tools:
        assert body["tools"][0]["type"] == "namespace" and body["tools"][0]["name"] == "my_agent"
        assert body["tools"][0]["tools"][0]["name"] == "local_echo"
        assert body["tool_choice"] == {"type": "function", "name": "local_echo", "namespace": "my_agent"}
    assert backend.supports_remote_compaction() is False


@pytest.mark.parametrize("with_tools", [True, False])
@pytest.mark.parametrize("route", ["siwc", "api_key", "legacy"])
def test_function_call_history_roundtrip_keeps_route_identity(
    tmp_path, identity_server, responses_replay_server, monkeypatch, with_tools, route,
):
    from copy import deepcopy

    from agent_py_agent.agent.backends import get_backend, responses_websocket
    from agent_py_agent.agent.backends.base import ProviderRequestOptions
    from agent_py_agent.agent.backends.gateway_helpers import post_stream_iter
    from agent_py_agent.agent.tooling.runtime_contracts import ToolChoice

    owner, key = _new_owner(tmp_path)
    _connect(owner, identity_server)
    if route == "legacy":
        path = model_profiles_path(owner.home_paths)
        data = read_model_profiles(path)
        data["providers"]["account"].update(api_base=schema.CHATGPT_BASE, auth={
            "mode": "chatgpt", "access_token": "legacy-test-access", "account_id": "legacy-test-account",
            "generation": "legacy-test-generation", "expires_at": time.time() + 3600})
        _save_profiles(path, data)
    config = _config(owner, key)
    if route == "api_key":
        config = replace(config, model_auth_ref={}, api_key="test-api-key")
    backend = get_backend("openai_responses", config)
    server = responses_replay_server
    server.namespace = "my_agent" if route == "siwc" else None
    envelopes, websocket_calls = [], []
    original_request = backend._gateway_request

    def local_request(*args, **kwargs):
        request = original_request(*args, **kwargs)
        envelopes.append(request)
        return replace(request, api_base=server.base)

    # 只替换外部目的地；旧订阅仍须进入自己的 WS 入口，本例用实际 SSE 承接该外部边界。
    def local_websocket(request, **_kwargs):
        websocket_calls.append(request)
        return post_stream_iter(request)

    monkeypatch.setattr(backend, "_gateway_request", local_request)
    monkeypatch.setattr(responses_websocket, "iter_responses_websocket", local_websocket)
    tools = [{"name": "local_echo", "description": "本机回声", "input_schema": {
        "type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}]
    options = ProviderRequestOptions(system_instruction="本机回放检查", total_deadline_seconds=5)
    first = backend.generate("检查资料", tools=tools, tool_choice=ToolChoice.specific("local_echo"), request_options=options)
    expected = {"type": "tool_use", "id": "local-replay-call", "name": "local_echo", "input": {"text": "本机"}}
    assert first.tool_use_blocks == [expected] and first.assistant_content_blocks == [expected]
    history = [{"role": "assistant", "content": first.assistant_content_blocks},
               {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "local-replay-call", "content": "已核对"}]}]
    original_history = deepcopy(history)
    chunks = []
    second = backend.generate("", messages=history, tools=tools if with_tools else None,
                              on_chunk=chunks.append, request_options=options)
    assert second.text == "本机回放完成" and chunks == ["本机回放完成"]
    assert history == original_history
    assert len(server.calls) == 2 and all(path == "/v1/responses" for path, _body, _headers in server.calls)
    assert len(websocket_calls) == (2 if route == "legacy" else 0)
    expected_base = schema.CHATGPT_BASE if route == "legacy" else API
    assert all(request.url == expected_base + "/responses" for request in envelopes)
    for _path, body, headers in server.calls:
        assert body["store"] is False and body["stream"] is True
        assert "previous_response_id" not in body
        assert headers["Authorization"] == "Bearer " + (
            "legacy-test-access" if route == "legacy" else "test-api-key" if route == "api_key" else "test-access")
    first_body, replay = server.calls[0][1], server.calls[1][1]
    call = next(item for item in replay["input"] if item.get("type") == "function_call")
    result = next(item for item in replay["input"] if item.get("type") == "function_call_output")
    assert call["call_id"] == result["call_id"] == "local-replay-call"
    assert call["name"] == "local_echo" and json.loads(call["arguments"]) == {"text": "本机"}
    assert result["output"] == "已核对" and "namespace" not in result
    assert ("tools" in replay) is with_tools
    if route == "siwc":
        assert call["namespace"] == "my_agent"
        assert first_body["tools"][0]["type"] == "namespace" and first_body["tools"][0]["name"] == "my_agent"
        assert first_body["tool_choice"] == {"type": "function", "name": "local_echo", "namespace": "my_agent"}
        if with_tools:
            assert replay["tools"] == first_body["tools"]
    else:
        assert "namespace" not in call
        assert first_body["tools"][0]["type"] == "function"
        assert first_body["tool_choice"] == {"type": "function", "name": "local_echo"}
        if with_tools:
            assert replay["tools"] == first_body["tools"]


@pytest.mark.parametrize("code,kind", [
    ("subscription_sharing_usage_limit_exceeded", "ProviderUsageLimitError"),
    ("subscription_sharing_usage_unavailable", "ProviderTransientError"),
    ("subscription_sharing_user_unavailable", "ProviderTransientError"),
    ("subscription_sharing_user_not_eligible", "ProviderResponseError"),
    ("subscription_sharing_route_not_supported", "ProviderResponseError"),
    ("chatpass_v2_scope_not_authorized", "ProviderResponseError"),
    ("chatpass_v2_invalid_authorization_context", "ProviderResponseError"),
    ("subscription_sharing_invalid_user", "ProviderResponseError"),
    ("subscription_sharing_unsupported_capability", "ProviderResponseError"),
])
def test_siwc_codes_override_message_heuristics_and_preserve_diagnostics(code, kind):
    from agent_py_agent.agent.backends.responses_failure import failed_event_error

    failure = failed_event_error({"type": "response.failed", "request_id": "test-request", "response": {
        "error": {"code": code, "param": "temperature", "message": "quota exhausted; token limit"}}})
    assert type(failure).__name__ == kind
    assert failure.details["provider_error"]["code"] == code
    assert failure.details["provider_error"]["param"] == "temperature"
    assert failure.details["provider_error"]["request_id"] == "test-request"
    if code == "subscription_sharing_usage_limit_exceeded":
        assert "https://chatgpt.com/settings/usage" in str(failure)


def test_loopback_callback_preserves_issued_client_and_constant_time_state(monkeypatch):
    import hmac

    from agent_py_agent.cli.chat_parts import tui_browser_login

    comparisons = []
    compare = hmac.compare_digest

    def observed(left, right):
        comparisons.append(True)
        return compare(left, right)

    monkeypatch.setattr(hmac, "compare_digest", observed)
    state = "s" * 43
    status, _message, result = tui_browser_login._callback_outcome("/auth/callback", {
        "code": "test-code", "state": state, "client_id": CLIENT}, state)
    assert status == 200 and result["client_id"] == CLIENT and comparisons
    assert tui_browser_login._callback_outcome("/auth/callback", {"code": "test-code", "state": "错"}, state)[2] is None


def test_tui_new_account_uses_own_name_official_route_and_transparent_label(monkeypatch):
    import asyncio

    from agent_py_agent.cli.chat_parts import tui_model_auth

    selected, saved = [], []

    async def choose(_app, _title, values):
        selected.extend(values)
        return "chatgpt"

    async def request(_app, _agent, _session, operation, payload):
        assert operation == "save_provider"
        saved.append(payload)
        return {"ok": True}

    monkeypatch.setattr(tui_model_auth, "_choose", choose)
    monkeypatch.setattr(tui_model_auth, "_request", request)
    assert asyncio.run(tui_model_auth._new_provider(None, None, "test"))[1] == "chatgpt"
    assert saved[0]["provider"]["api_base"] == API
    labels = " ".join(label for _value, label in selected)
    assert "Continue with ChatGPT" in labels and "my-agent" in labels


def test_id_token_is_included_in_provider_diagnostic_redaction():
    from agent_py_agent.agent.settings.model_provider_network import _redact_network_text

    data = {"providers": {"account": {"api_key": "", "custom_headers": {}, "auth": {
        "id_token": "synthetic.id-token.signature", "access_token": "test-access", "refresh_token": "test-refresh"}}}}
    assert "synthetic.id-token.signature" not in _redact_network_text("received synthetic.id-token.signature", data)


@pytest.mark.parametrize("status,code,clear", [(400, "invalid_grant", True), (503, "server_error", False)])
def test_refresh_terminal_error_clears_only_unusable_session(tmp_path, identity_server, status, code, clear):
    owner, key = _new_owner(tmp_path)
    _connect(owner, identity_server)
    config = _config(owner, key)
    path = model_profiles_path(owner.home_paths)
    data = read_model_profiles(path)
    data["providers"]["account"]["auth"]["expires_at"] = 1
    _save_profiles(path, data)
    identity_server.token_status, identity_server.token_error = status, code
    with pytest.raises(ModelProfileError):
        runtime.request_credentials(config.model_auth_ref, API)
    saved = _auth(owner)
    assert bool(saved.get("refresh_token")) is not clear
    assert saved["client_id"] == CLIENT and saved["ext_agent_host_id"]


def test_late_logout_preserves_new_authorization_attempt(tmp_path, identity_server):
    owner, _ = _new_owner(tmp_path)
    _connect(owner, identity_server)
    identity_server.before_revoke = lambda: _start(owner)
    with pytest.raises(ModelProfileError, match="状态已变化"):
        execute_model_profile_operation(owner, "auth_logout", {"provider_id": "account"})
    assert _auth(owner)["pending"]["id"] and _auth(owner)["access_token"] == "test-access"


def test_real_initial_loopback_ui_exchange_keeps_client_id_and_releases_fds(tmp_path, identity_server, monkeypatch):
    import asyncio
    import os
    from urllib.request import ProxyHandler, build_opener

    from agent_py_agent.cli.chat_parts import tui_browser_login

    owner, _ = _new_owner(tmp_path)
    before = None
    if os.path.isdir("/dev/fd"):
        with os.scandir("/dev/fd") as entries:
            before = len(list(entries))
    callback = tui_browser_login.LoopbackCallback((1455, 1457))
    try:
        started = _start(owner, callback.redirect_uri)
        identity_server.nonce = _auth(owner)["pending"]["nonce"]
        callback.expect(started["state"])
        url = callback.redirect_uri + "?" + urlencode({"state": started["state"], "code": "test-code", "client_id": CLIENT})
        with build_opener(ProxyHandler({})).open(url, timeout=3) as response:
            assert response.status == 200
            response.read()
        sent = []

        async def gateway(agent, _session, operation, payload):
            sent.append(dict(payload))
            return execute_model_profile_operation(agent, operation, payload)

        monkeypatch.setattr(tui_browser_login, "_request_data", gateway)
        flow = tui_browser_login._BrowserLoginFlow(None, owner, "local-test", callback)
        flow.attempt = {"provider_id": "account", "attempt_id": started["attempt_id"]}
        result = asyncio.run(flow._complete_after_callback())
        assert result["status"] == "connected" and sent[0]["client_id"] == CLIENT
    finally:
        callback.close()
    assert callback._server.fileno() == -1
    if before is not None:
        with os.scandir("/dev/fd") as entries:
            after = len(list(entries))
        assert after - before == 0


def test_missing_verifier_is_reported_before_starting_authorization(tmp_path, monkeypatch):
    import builtins

    owner, _ = _new_owner(tmp_path)
    path = model_profiles_path(owner.home_paths)
    before = path.read_bytes()
    original_import = builtins.__import__

    def without_verifier(name, *args, **kwargs):
        if name == "cryptography" or name.startswith("cryptography."):
            raise ModuleNotFoundError("test optional verifier unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_verifier)
    with pytest.raises(ModelProfileError, match="secrets"):
        _start(owner)
    assert path.read_bytes() == before


def test_invalid_refresh_preserves_newer_login_and_allows_its_completion(tmp_path, identity_server, monkeypatch):
    owner, key = _new_owner(tmp_path)
    _connect(owner, identity_server)
    config = _config(owner, key)
    path = model_profiles_path(owner.home_paths)
    data = read_model_profiles(path)
    data["providers"]["account"]["auth"]["expires_at"] = 1
    _save_profiles(path, data)
    original_post = wire.oauth_post
    started = {}

    def after_real_response(url, body, **kwargs):
        result = original_post(url, body, **kwargs)
        if body.get("grant_type") == "refresh_token":
            started.update(_start(owner))
        return result

    monkeypatch.setattr(wire, "oauth_post", after_real_response)
    identity_server.token_status, identity_server.token_error = 400, "invalid_grant"
    with pytest.raises(ModelProfileError):
        runtime.request_credentials(config.model_auth_ref, API)
    saved = _auth(owner)
    assert saved.get("pending", {}).get("id") == started["attempt_id"]
    assert not saved.get("access_token") and not saved.get("refresh_token")
    identity_server.token_status = 200
    identity_server.nonce = saved["pending"]["nonce"]
    assert _complete(owner, started)["status"] == "connected"


def test_initial_invalid_code_retains_registration_without_adopting_identity(tmp_path, identity_server):
    owner, _ = _new_owner(tmp_path)
    started = _start(owner)
    identity_server.token_status, identity_server.token_error = 400, "invalid_grant"
    with pytest.raises(ModelProfileError):
        _complete(owner, started)
    saved = _auth(owner)
    assert saved["client_id"] == CLIENT
    assert not saved.get("access_token") and not saved.get("subject") and not saved.get("pending")
    execute_model_profile_operation(owner, "auth_cancel", {"provider_id": "account", "attempt_id": started["attempt_id"]})
    restarted = _start(owner, "http://127.0.0.1:54321/auth/callback")
    query = parse_qs(urlsplit(restarted["authorize_url"]).query)
    assert query["client_id"] == [CLIENT] and "agent_name_hint" not in query
    identity_server.token_status = 200
    identity_server.nonce = _auth(owner)["pending"]["nonce"]
    assert _complete(owner, restarted)["status"] == "connected"


def test_incomplete_remote_revocation_still_clears_local_tokens(tmp_path, identity_server):
    from http.client import IncompleteRead

    owner, _ = _new_owner(tmp_path)
    _connect(owner, identity_server)
    identity_server.incomplete_path = "/api/accounts/oauth/revoke"
    result = None
    try:
        result = execute_model_profile_operation(owner, "auth_logout", {"provider_id": "account"})
    except IncompleteRead:
        pass
    assert result is not None, "撤销断流不能阻止本地退出"
    assert result["remote_revocation_confirmed"] is False and result["status"] == "signed_out"
    assert not set(_auth(owner)).intersection({"access_token", "refresh_token", "id_token", "pending"})


@pytest.mark.parametrize("path", ["/.well-known/openid-configuration", "/jwks"])
def test_incomplete_signing_document_is_a_clean_authentication_error(tmp_path, identity_server, path):
    owner, _ = _new_owner(tmp_path)
    started = _start(owner)
    identity_server.nonce = _auth(owner)["pending"]["nonce"]
    identity_server.incomplete_path = path
    with pytest.raises(ModelProfileError, match="签名资料"):
        _complete(owner, started)
    assert not _auth(owner).get("access_token")


def test_cancel_closes_an_accepted_incomplete_callback_connection(monkeypatch):
    import os
    import socket

    from agent_py_agent.cli.chat_parts import tui_browser_login

    accepted, finished = threading.Event(), threading.Event()
    original_setup = tui_browser_login._CallbackHandler.setup
    original_finish = tui_browser_login._CallbackHandler.finish

    def setup(handler):
        original_setup(handler)
        accepted.set()

    def finish(handler):
        try:
            original_finish(handler)
        finally:
            finished.set()

    monkeypatch.setattr(tui_browser_login._CallbackHandler, "setup", setup)
    monkeypatch.setattr(tui_browser_login._CallbackHandler, "finish", finish)
    before = None
    if os.path.isdir("/dev/fd"):
        with os.scandir("/dev/fd") as entries:
            before = len(list(entries))
    callback = tui_browser_login.LoopbackCallback((0,))
    client = socket.create_connection(("127.0.0.1", callback._server.server_port), timeout=2)
    ended = False
    try:
        client.sendall(b"GET /auth/callback HTTP/1.1\r\nHost: local-test")
        assert accepted.wait(2)
        callback.close()
        client.settimeout(0.5)
        try:
            ended = client.recv(1) == b""
        except ConnectionResetError:
            ended = True
        except TimeoutError:
            pass
    finally:
        client.close()
        callback.close()
        assert finished.wait(2)
    assert ended, "关闭登录必须同时关闭已接受的半开连接"
    if before is not None:
        with os.scandir("/dev/fd") as entries:
            after = len(list(entries))
        assert after - before == 0


@pytest.mark.parametrize("next_action", ["cancel", "restart"])
def test_initial_invalid_code_cannot_save_registration_after_attempt_changes(tmp_path, identity_server, monkeypatch, next_action):
    owner, _ = _new_owner(tmp_path)
    started = _start(owner)
    identity_server.token_status, identity_server.token_error = 400, "invalid_grant"
    original_post = wire.oauth_post
    replacement = {}

    def after_real_response(url, body, **kwargs):
        result = original_post(url, body, **kwargs)
        if body.get("grant_type") == "authorization_code":
            if next_action == "cancel":
                execute_model_profile_operation(owner, "auth_cancel", {
                    "provider_id": "account", "attempt_id": started["attempt_id"]})
            else:
                replacement.update(_start(owner))
        return result

    monkeypatch.setattr(wire, "oauth_post", after_real_response)
    with pytest.raises(ModelProfileError, match="迟到"):
        _complete(owner, started)
    saved = _auth(owner)
    assert saved["client_id"] == "dynamic_agent_client"
    assert not saved.get("access_token") and not saved.get("subject")
    if next_action == "restart":
        assert saved["pending"]["id"] == replacement["attempt_id"]
    else:
        assert not saved.get("pending")
