"""ChatGPT 浏览器登录（授权码 + PKCE）后台两步的定向验证：开始、回调核对、兑换、CAS 与期限。"""
import base64
import hashlib
import json
import stat
import time
from urllib.parse import parse_qs, urlsplit

import pytest

from agent_py_agent.agent.settings import model_oauth as auth_runtime
from agent_py_agent.agent.settings import model_oauth_wire as wire
from agent_py_agent.agent.settings.model_oauth_schema import (
    CHATGPT_BASE,
    CHATGPT_CLIENT,
    CHATGPT_ISSUER,
    oauth_config,
    stored_oauth,
)
from agent_py_agent.agent.settings.model_profiles import (
    _save_profiles,
    execute_model_profile_operation,
    model_profiles_path,
    read_model_profiles,
    selected_model_config,
)
from agent_py_agent.agent.settings.model_provider_schema import ModelProfileError
from agent_py_agent.tests.test_model_oauth import host, setup

CALLBACK = "http://127.0.0.1:1455/auth/callback"


def start(owner):
    return execute_model_profile_operation(owner, "auth_browser_start", {"provider_id": "account", "redirect_uri": CALLBACK})


def complete(owner, started, **overrides):
    payload = {"provider_id": "account", "attempt_id": started["attempt_id"], "code": "auth-code",
               "state": started["state"], **overrides}
    return execute_model_profile_operation(owner, "auth_browser_complete", payload)


def fake_exchange(monkeypatch, seen, *, during=None):
    def exchange(auth, pending, code):
        seen.append((code, pending["code_verifier"], pending["redirect_uri"]))
        if during:
            during()
        return {"access_token": "browser-access", "refresh_token": "browser-refresh",
                "account_id": "browser-account", "expires_at": time.time() + 300}

    monkeypatch.setattr(auth_runtime, "exchange_browser_code", exchange)


def pending_on_disk(owner):
    return read_model_profiles(model_profiles_path(owner.home_paths))["providers"]["account"]["auth"].get("pending")


def test_start_keeps_pkce_private_and_returns_only_the_official_authorize_url(tmp_path):
    alice = host(tmp_path)
    setup(alice, "chatgpt")
    started = start(alice)
    assert started["status"] == "pending" and started["attempt_id"]
    parts = urlsplit(started["authorize_url"])
    query = {key: values[0] for key, values in parse_qs(parts.query).items()}
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == CHATGPT_ISSUER + "/oauth/authorize"
    assert query["response_type"] == "code" and query["client_id"] == CHATGPT_CLIENT
    assert query["redirect_uri"] == CALLBACK and query["state"] == started["state"]
    assert query["code_challenge_method"] == "S256" and "offline_access" in query["scope"].split()
    assert "originator" not in query
    pending = pending_on_disk(alice)
    verifier = pending["code_verifier"]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    assert query["code_challenge"] == challenge
    assert pending["flow"] == "browser" and pending["redirect_uri"] == CALLBACK
    assert verifier not in json.dumps(started)
    assert stat.S_IMODE(model_profiles_path(alice.home_paths).stat().st_mode) == 0o600


def test_complete_exchanges_with_the_saved_verifier_and_connects(tmp_path, monkeypatch):
    alice = host(tmp_path)
    key = setup(alice, "chatgpt")
    started = start(alice)
    verifier = pending_on_disk(alice)["code_verifier"]
    seen = []
    fake_exchange(monkeypatch, seen)
    assert complete(alice, started)["status"] == "connected"
    assert seen == [("auth-code", verifier, CALLBACK)]
    assert pending_on_disk(alice) is None
    execute_model_profile_operation(alice, "set_default", {"profile_id": key})
    cfg = selected_model_config(alice)
    assert auth_runtime.request_credentials(cfg.model_auth_ref, cfg.api_base) == (
        "browser-access", {"ChatGPT-Account-ID": "browser-account"})


def test_mismatched_callbacks_are_refused_without_cancelling_the_real_login(tmp_path, monkeypatch):
    alice = host(tmp_path)
    setup(alice, "chatgpt")
    started = start(alice)
    seen = []
    fake_exchange(monkeypatch, seen)
    for overrides in ({"state": "x" * 43}, {"attempt_id": "someone-else"}, {"code": ""}, {"code": "has space"}):
        with pytest.raises(ModelProfileError):
            complete(alice, started, **overrides)
    assert seen == [] and pending_on_disk(alice)["id"] == started["attempt_id"]
    assert complete(alice, started)["status"] == "connected"


def test_expired_browser_login_is_cleared_before_any_exchange(tmp_path, monkeypatch):
    alice = host(tmp_path)
    setup(alice, "chatgpt")
    started = start(alice)
    path = model_profiles_path(alice.home_paths)
    data = read_model_profiles(path)
    data["providers"]["account"]["auth"]["pending"]["expires_at"] = time.time() - 1
    _save_profiles(path, data)
    seen = []
    fake_exchange(monkeypatch, seen)
    with pytest.raises(ModelProfileError, match="超时"):
        complete(alice, started)
    assert seen == [] and pending_on_disk(alice) is None


@pytest.mark.parametrize("action", ["auth_cancel", "auth_logout"])
def test_cancel_or_logout_during_exchange_cannot_revive_the_login(tmp_path, monkeypatch, action):
    alice = host(tmp_path)
    setup(alice, "chatgpt")
    started = start(alice)
    fake_exchange(monkeypatch, [], during=lambda: execute_model_profile_operation(
        alice, action, {"provider_id": "account", "attempt_id": started["attempt_id"]}))
    with pytest.raises(ModelProfileError, match="迟到"):
        complete(alice, started)
    assert execute_model_profile_operation(alice, "auth_status", {"provider_id": "account"})["status"] == "signed_out"
    assert "browser-access" not in model_profiles_path(alice.home_paths).read_text()


def test_only_registered_loopback_callbacks_and_chatgpt_providers_are_accepted(tmp_path):
    alice, bob = host(tmp_path), host(tmp_path, "bob")
    setup(alice, "chatgpt")
    for redirect in ("http://127.0.0.1:9999/auth/callback", "http://localhost:1455/auth/callback",
                     "https://evil.example.test/auth/callback", CALLBACK + "?x=1"):
        with pytest.raises(ModelProfileError, match="回调地址"):
            execute_model_profile_operation(alice, "auth_browser_start", {"provider_id": "account", "redirect_uri": redirect})
    assert pending_on_disk(alice) is None
    assert start(alice)["status"] == "pending"
    execute_model_profile_operation(alice, "auth_browser_start", {"provider_id": "account",
                                                                  "redirect_uri": "http://127.0.0.1:1457/auth/callback"})
    setup(bob)
    with pytest.raises(ModelProfileError, match="只支持"):
        start(bob)


def test_wire_exchange_sends_the_pkce_form_and_keeps_no_id_token(monkeypatch):
    config = oauth_config({"mode": "chatgpt"}, CHATGPT_BASE)
    url, pending = wire.browser_authorize(config, CALLBACK)
    assert url.startswith(CHATGPT_ISSUER + "/oauth/authorize?") and pending["expires_at"] > time.time()
    claims = base64.urlsafe_b64encode(json.dumps({"https://api.openai.com/auth": {"chatgpt_account_id": "acct"}}).encode())
    id_token = "h." + claims.decode().rstrip("=") + ".s"
    sent = []

    def post(target, body, **kwargs):
        sent.append((target, body, kwargs))
        return 200, {"access_token": "access", "refresh_token": "refresh", "id_token": id_token}

    monkeypatch.setattr(wire, "oauth_post", post)
    fields = wire.exchange_browser_code(config, pending, "the-code")
    assert sent == [(CHATGPT_ISSUER + "/oauth/token", {
        "grant_type": "authorization_code", "code": "the-code", "redirect_uri": CALLBACK,
        "client_id": CHATGPT_CLIENT, "code_verifier": pending["code_verifier"]}, {})]
    assert fields["account_id"] == "acct" and "id_token" not in fields
    monkeypatch.setattr(wire, "oauth_post", lambda *a, **k: (400, {"error": "invalid_grant"}))
    with pytest.raises(ModelProfileError, match="兑换失败"):
        wire.exchange_browser_code(config, pending, "the-code")


def test_stored_browser_pending_rejects_tampered_fields():
    config = oauth_config({"mode": "chatgpt"}, CHATGPT_BASE)
    _, pending = wire.browser_authorize(config, CALLBACK)
    good = {**config, "pending": {"id": "attempt", **pending}}
    assert stored_oauth(good, CHATGPT_BASE)["pending"]["code_verifier"] == pending["code_verifier"]
    for field, value in (("redirect_uri", "http://127.0.0.1:1/auth/callback"), ("state", "short"),
                         ("code_verifier", "has space " * 6), ("expires_at", float("inf"))):
        with pytest.raises(ModelProfileError):
            stored_oauth({**config, "pending": {"id": "attempt", **pending, field: value}}, CHATGPT_BASE)
