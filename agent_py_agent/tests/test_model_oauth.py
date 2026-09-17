"""登录、刷新、取消、隔离及订阅请求复用原后端的定向验证。"""
import base64
import json
import stat
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_py_agent.agent.backends import get_backend
from agent_py_agent.agent.backends.base import ProviderRequestOptions
from agent_py_agent.agent.settings import model_oauth as auth_runtime
from agent_py_agent.agent.settings import model_oauth_wire as wire
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.settings.model_oauth_schema import CHATGPT_BASE, CHATGPT_ISSUER
from agent_py_agent.agent.settings.model_profiles import (
    _save_profiles,
    execute_model_profile_operation,
    model_profiles_path,
    read_model_profiles,
    selected_model_config,
)
from agent_py_agent.agent.settings.model_provider_schema import ModelProfileError


def host(tmp_path, owner="alice"):
    return SimpleNamespace(home_paths=SimpleNamespace(root=tmp_path, config_dir=tmp_path / "config",
        owner_provider="local", owner_kind="user", owner_id=owner), config=AgentConfig())


def setup(host, mode="oauth_device"):
    config = {"mode": mode} if mode == "chatgpt" else {"mode": mode, "client_id": "public-client",
        "device_url": "https://login.example.test/device", "token_url": "https://login.example.test/token", "scope": "models offline_access"}
    base = CHATGPT_BASE if mode == "chatgpt" else "https://api.example.test/v1"
    execute_model_profile_operation(host, "save_provider", {"provider_id": "account", "provider": {
        "display_name": "本人账号", "api_base": base, "auth": config}})
    key = str(uuid4())
    execute_model_profile_operation(host, "save_model", {"profile_id": key, "profile": {"provider_id": "account",
        "model_name": "explicit-model", "model_backend": "openai_responses", "model_context_window_tokens": 128000}})
    return key


def login(host, monkeypatch, *, token="private-access", refresh="private-refresh"):
    monkeypatch.setattr(auth_runtime, "start_device", lambda auth: {"device_code": "private-device", "user_code": "ABCD",
        "verification_uri": "https://login.example.test/activate", "interval": 1, "expires_at": time.time() + 600})
    monkeypatch.setattr(auth_runtime, "poll_device", lambda auth, pending: ("connected", {
        "access_token": token, "refresh_token": refresh, "account_id": "private-account", "expires_at": time.time() + 300}))
    attempt = execute_model_profile_operation(host, "auth_start", {"provider_id": "account"})
    path = model_profiles_path(host.home_paths)
    data = read_model_profiles(path)
    data["providers"]["account"]["auth"]["pending"]["next_poll_at"] = 0
    _save_profiles(path, data)
    return execute_model_profile_operation(host, "auth_poll", {"provider_id": "account", "attempt_id": attempt["attempt_id"]})


def test_private_login_and_frozen_reference(tmp_path, monkeypatch):
    alice, bob = host(tmp_path), host(tmp_path, "bob")
    key = setup(alice)
    with pytest.raises(ModelProfileError, match="尚未登录"):
        execute_model_profile_operation(alice, "set_default", {"profile_id": key})
    assert login(alice, monkeypatch)["status"] == "connected"
    public = execute_model_profile_operation(alice, "set_default", {"profile_id": key})
    assert public["providers"][0]["signed_in"]
    cfg = selected_model_config(alice)
    assert cfg.api_key == "" and cfg.model_auth_ref["path"] == str(model_profiles_path(alice.home_paths))
    assert "private-" not in json.dumps(public) + repr(cfg)
    assert execute_model_profile_operation(bob, "list", {})["providers"] == []
    with pytest.raises(ModelProfileError):
        execute_model_profile_operation(bob, "auth_status", {"provider_id": "account"})
    assert stat.S_IMODE(model_profiles_path(alice.home_paths).stat().st_mode) == 0o600
    assert auth_runtime.request_credentials(cfg.model_auth_ref, cfg.api_base)[0] == "private-access"
    execute_model_profile_operation(alice, "auth_logout", {"provider_id": "account"})
    with pytest.raises(ModelProfileError, match="登录已退出"):
        auth_runtime.request_credentials(cfg.model_auth_ref, cfg.api_base)
    text = model_profiles_path(alice.home_paths).read_text()
    assert "private-access" not in text and "private-refresh" not in text


def test_refresh_is_single_flight_and_owner_state_is_not_exposed(tmp_path, monkeypatch):
    alice = host(tmp_path)
    key = setup(alice)
    login(alice, monkeypatch)
    execute_model_profile_operation(alice, "set_default", {"profile_id": key})
    cfg = selected_model_config(alice)
    path = model_profiles_path(alice.home_paths)
    data = read_model_profiles(path)
    data["providers"]["account"]["auth"]["expires_at"] = 1
    _save_profiles(path, data)
    calls = []

    def refresh(auth):
        calls.append(auth["refresh_token"])
        return {"access_token": "new-access", "refresh_token": "new-refresh", "expires_at": time.time() + 300}

    monkeypatch.setattr(auth_runtime, "refresh_tokens", refresh)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: auth_runtime.request_credentials(cfg.model_auth_ref, cfg.api_base), range(8)))
    assert calls == ["private-refresh"]
    assert all(row[0] == "new-access" for row in results)
    assert cfg.api_key == "" and "new-access" not in repr(cfg)


@pytest.mark.parametrize("action", ["auth_cancel", "auth_logout"])
def test_late_login_cannot_revive_cancelled_account(tmp_path, monkeypatch, action):
    alice = host(tmp_path)
    setup(alice)

    def start(auth):
        attempt = auth["pending"]["id"]
        execute_model_profile_operation(alice, action, {"provider_id": "account", "attempt_id": attempt})
        return {"device_code": "device", "user_code": "CODE", "verification_uri": "https://example.test/verify",
                "expires_at": time.time() + 600, "interval": 1}

    monkeypatch.setattr(auth_runtime, "start_device", start)
    with pytest.raises(ModelProfileError, match="迟到"):
        execute_model_profile_operation(alice, "auth_start", {"provider_id": "account"})
    assert execute_model_profile_operation(alice, "auth_status", {"provider_id": "account"})["status"] == "signed_out"


def test_provider_edit_cannot_forward_old_tokens_or_accept_token_injection(tmp_path, monkeypatch):
    alice = host(tmp_path)
    key = setup(alice)
    login(alice, monkeypatch)
    execute_model_profile_operation(alice, "set_default", {"profile_id": key})
    cfg = selected_model_config(alice)
    execute_model_profile_operation(alice, "save_provider", {"provider_id": "account", "editing": True,
        "provider": {"api_base": "https://different.example.test/v1"}})
    with pytest.raises(ModelProfileError):
        auth_runtime.request_credentials(cfg.model_auth_ref, cfg.api_base)
    assert "private-access" not in model_profiles_path(alice.home_paths).read_text()
    with pytest.raises(ModelProfileError, match="表单填写令牌"):
        execute_model_profile_operation(alice, "save_provider", {"provider_id": "account", "editing": True,
            "provider": {"auth": {"mode": "chatgpt", "access_token": "injected"}, "api_base": CHATGPT_BASE}})


def test_failed_login_start_clears_pending_but_keeps_existing_credential(tmp_path, monkeypatch):
    alice = host(tmp_path)
    setup(alice)
    login(alice, monkeypatch)

    def rejected(auth):
        raise ModelProfileError("设备码请求失败")

    monkeypatch.setattr(auth_runtime, "start_device", rejected)
    with pytest.raises(ModelProfileError, match="设备码请求失败"):
        execute_model_profile_operation(alice, "auth_start", {"provider_id": "account"})
    auth = read_model_profiles(model_profiles_path(alice.home_paths))["providers"]["account"]["auth"]
    assert "pending" not in auth and auth["access_token"] == "private-access"
    assert execute_model_profile_operation(alice, "auth_status", {"provider_id": "account"})["status"] == "connected"


def test_chatgpt_uses_existing_responses_stream_and_account_header(tmp_path, monkeypatch):
    alice = host(tmp_path)
    key = setup(alice, "chatgpt")
    login(alice, monkeypatch)
    execute_model_profile_operation(alice, "set_default", {"profile_id": key})
    cfg = selected_model_config(alice)
    cfg.stream_enabled = False
    backend = get_backend(cfg.model_backend, cfg)
    requests = []

    def stream(path, payload, headers, **kwargs):
        requests.append(backend._gateway_request(path, payload, headers))
        yield json.dumps({"type": "response.completed", "response": {"status": "completed", "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "你好"}]}],
            "usage": {"input_tokens": 12, "output_tokens": 2}}})

    monkeypatch.setattr(backend, "request_stream_iter", stream)
    response = backend.generate("你好", request_options=ProviderRequestOptions(system_instruction="保持简洁"))
    req = requests[0]
    assert response.text == "你好" and response.usage["input_tokens"] == 12
    assert req.url == CHATGPT_BASE + "/responses" and not req.allow_redirects
    assert req.headers["ChatGPT-Account-ID"] == "private-account"
    assert req.payload["instructions"] == "保持简洁"
    assert req.payload["stream"] and req.payload["store"] is False
    assert "max_output_tokens" not in req.payload and "private-" not in json.dumps(req.payload)
    assert all(item.get("role") != "system" for item in req.payload["input"])


def test_chatgpt_device_wire_and_generic_slow_down(monkeypatch):
    from agent_py_agent.agent.settings.model_oauth_schema import oauth_config

    config = oauth_config({"mode": "chatgpt"}, CHATGPT_BASE)
    claims = base64.urlsafe_b64encode(json.dumps({"https://api.openai.com/auth": {"chatgpt_account_id": "account"}}).encode()).decode().rstrip("=")
    replies = [(200, {"device_auth_id": "device", "user_code": "code", "interval": "5"}),
               (403, {}), (200, {"authorization_code": "code", "code_verifier": "verifier"}),
               (200, {"access_token": "access", "refresh_token": "refresh", "id_token": "h." + claims + ".s"})]
    sent = []

    def post(url, body, **kwargs):
        sent.append((url, body, kwargs))
        return replies.pop(0)

    monkeypatch.setattr(wire, "oauth_post", post)
    pending = wire.start_device(config)
    assert wire.poll_device(config, pending)[0] == "pending"
    status, fields = wire.poll_device(config, pending)
    assert status == "connected" and fields["account_id"] == "account"
    assert "id_token" not in fields
    assert sent[-1][0] == CHATGPT_ISSUER + "/oauth/token"
    assert sent[-1][1]["code_verifier"] == "verifier"
    config = oauth_config({"mode": "oauth_device", "client_id": "client", "device_url": "https://example.test/device",
        "token_url": "https://example.test/token"}, "https://example.test/v1")
    monkeypatch.setattr(wire, "oauth_post", lambda *a, **k: (400, {"error": "slow_down"}))
    assert wire.poll_device(config, {"device_code": "code"}) == ("slow_down", {})


def test_oauth_rejects_insecure_remote_endpoint():
    from agent_py_agent.agent.settings.model_oauth_schema import auth_url, oauth_config

    with pytest.raises(ModelProfileError):
        oauth_config({"mode": "chatgpt"}, "https://other.example.test")
    with pytest.raises(ModelProfileError):
        auth_url("http://192.168.1.1/token")
    assert auth_url("http://127.0.0.1:8181/token")


def test_generic_parameter_edit_keeps_secret_unless_explicitly_cleared(tmp_path):
    alice = host(tmp_path)
    setup(alice)
    parameters = execute_model_profile_operation(alice, "auth_parameters", {"provider_id": "account"})["parameters"]
    execute_model_profile_operation(alice, "save_provider", {"provider_id": "account", "editing": True,
        "provider": {"auth": {**parameters, "client_secret": "private-client"}}})
    public = execute_model_profile_operation(alice, "auth_parameters", {"provider_id": "account"})
    assert "client_secret" not in public["parameters"] and "private-client" not in repr(public)
    for clear in (False, True):
        execute_model_profile_operation(alice, "save_provider", {"provider_id": "account", "editing": True,
            "clear_auth_secret": clear, "provider": {"auth": {**parameters, "client_secret": ""}}})
        auth = read_model_profiles(model_profiles_path(alice.home_paths))["providers"]["account"]["auth"]
        assert auth["client_secret"] == ("" if clear else "private-client")


def test_refresh_cannot_recreate_logged_out_credentials(tmp_path, monkeypatch):
    alice = host(tmp_path)
    key = setup(alice)
    login(alice, monkeypatch)
    execute_model_profile_operation(alice, "set_default", {"profile_id": key})
    cfg = selected_model_config(alice)
    path = model_profiles_path(alice.home_paths)
    data = read_model_profiles(path)
    data["providers"]["account"]["auth"]["expires_at"] = 1
    _save_profiles(path, data)

    def refresh(auth):
        execute_model_profile_operation(alice, "auth_logout", {"provider_id": "account"})
        return {"access_token": "late-token", "expires_at": time.time() + 600}

    monkeypatch.setattr(auth_runtime, "refresh_tokens", refresh)
    with pytest.raises(ModelProfileError, match="登录已退出"):
        auth_runtime.request_credentials(cfg.model_auth_ref, cfg.api_base)
    assert "late-token" not in path.read_text()


def test_oauth_account_cannot_be_published_as_shared_model(tmp_path, monkeypatch):
    from agent_py_agent.agent.settings.shared_model_catalog import set_shared_profile
    from agent_py_agent.tests.test_shared_model_catalog import admin_host

    admin = admin_host(tmp_path)
    key = setup(admin)
    login(admin, monkeypatch)
    with pytest.raises(ModelProfileError, match="不能跨用户共享"):
        set_shared_profile(admin, key, True)


def test_login_verification_uri_accepts_query_without_loosening_token_destinations():
    from agent_py_agent.agent.settings.model_oauth_schema import auth_url, verification_url

    url = "https://login.example.test/activate?tenant=example"
    assert verification_url(url) == url
    with pytest.raises(ModelProfileError):
        auth_url(url)
    for invalid in ("https://login.example.test/#token", "https://user:secret@example.test/verify", "http://example.test/verify"):
        with pytest.raises(ModelProfileError):
            verification_url(invalid)
