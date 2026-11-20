import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.adapter import feishu
from agent_py_agent.agent.adapter import feishu_agent_panel as panel
from agent_py_agent.agent.agent_core.native_tool_protocol import (
    ToolProtocolSelectionError,
    select_tool_protocol,
)
from agent_py_agent.agent.backends.base import BackendOptions
from agent_py_agent.agent.backends.http import invalidate_capability_probe_memo
from agent_py_agent.agent.backends.openai_chat import OpenAICompatibleBackend
from agent_py_agent.agent.contracts.model_call_ledger import ModelCallLedger
from agent_py_agent.agent.settings import model_oauth
from agent_py_agent.agent.settings.model_profiles import (
    execute_model_profile_operation,
    selected_model_config,
)
from agent_py_agent.agent.tooling.runtime_contracts import ProviderToolCapability
from agent_py_agent.tests.test_model_oauth import host, login, setup


def test_same_login_revocation_fences_other_route_probe(monkeypatch):
    entered, release = Event(), Event()
    attempts = []
    first = ProviderToolCapability("chat", "https://model.invalid", "first", False, True, "old")
    fresh = ProviderToolCapability("chat", "https://model.invalid", "first", False, True, "fresh")

    def probe(_backend):
        attempts.append(1)
        if len(attempts) == 1:
            entered.set()
            assert release.wait(5)
            return first
        return fresh

    def backend(model):
        instance = OpenAICompatibleBackend(BackendOptions(
            api_base="https://model.invalid/v1", api_key="fixture", model_name=model,
        ))
        instance.auth_ref = {"provider_id": "fixture", "generation": "m62"}
        return instance

    monkeypatch.setattr(OpenAICompatibleBackend, "_probe_tool_capability_uncached", probe)
    a, b = backend("first"), backend("second")
    host = SimpleNamespace(config=SimpleNamespace(enable_tools=True), backend=a, _model_call_ledger=ModelCallLedger())
    invalidate_capability_probe_memo()
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            task = executor.submit(select_tool_protocol, host, run_id="in-flight")
            try:
                assert entered.wait(5)
                invalidate_capability_probe_memo(b, reason="http_401")
            finally:
                release.set()
            with pytest.raises(ToolProtocolSelectionError):
                task.result(timeout=5)
        assert a.probe_tool_capability() is fresh
    finally:
        invalidate_capability_probe_memo()


def test_logout_fences_running_probe_of_saved_login(tmp_path, monkeypatch):
    agent = host(tmp_path)
    profile_id = setup(agent)
    login(agent, monkeypatch)
    execute_model_profile_operation(agent, "set_default", {"profile_id": profile_id})
    config = selected_model_config(agent)
    backend = OpenAICompatibleBackend(BackendOptions(
        api_base=config.api_base, api_key="fixture", model_name="fixture",
    ))
    backend.auth_ref = config.model_auth_ref
    entered, release = Event(), Event()

    def probe(_backend):
        entered.set()
        assert release.wait(5)
        return ProviderToolCapability("chat", config.api_base, "fixture", False, True, "late")

    monkeypatch.setattr(OpenAICompatibleBackend, "_probe_tool_capability_uncached", probe)
    monkeypatch.setattr(model_oauth, "revoke_session", lambda _auth: True)
    runner = SimpleNamespace(config=SimpleNamespace(enable_tools=True), backend=backend, _model_call_ledger=ModelCallLedger())
    with ThreadPoolExecutor(max_workers=1) as executor:
        task = executor.submit(select_tool_protocol, runner, run_id="logout-during-probe")
        try:
            assert entered.wait(5)
            result = execute_model_profile_operation(agent, "auth_logout", {"provider_id": config.model_auth_ref["provider_id"]})
            assert result["status"] == "signed_out"
        finally:
            release.set()
        with pytest.raises(ToolProtocolSelectionError, match="probe_revocation_generation_changed"):
            task.result(timeout=5)
    assert backend._provider_tool_capability_cache is None


def _expired_panel(tmp_path, monkeypatch):
    claims = {"binding_id": "ab12", "owner": {"provider": "feishu", "owner_kind": "user", "owner_id": "ou_test"},
              "actor_id": "ou_test", "audience": "dm"}
    monkeypatch.setattr(panel, "verify_panel_binding", lambda *_: claims)
    monkeypatch.setattr(panel, "resolve_owner_home", lambda *_: SimpleNamespace(home_dir=tmp_path))
    monkeypatch.setattr(panel, "_check_session_lock", lambda *_: None)
    monkeypatch.setattr(panel, "sign_panel_binding", lambda _home, value: value["binding_id"])
    monkeypatch.setattr(panel, "_fetch_panel_data", lambda *_: (200, {}))
    monkeypatch.setattr(panel, "build_panel_view", lambda *_a, **_kw: {})
    monkeypatch.setattr(panel, "render_progress_card", lambda _view: {})
    path = panel._panel_state_path(tmp_path, claims["binding_id"])
    panel._write_panel_state(path, {
        "schema_version": "feishu_panel_state.v1", "binding_id": claims["binding_id"],
        "message_id": "om_old", "chat_id": "oc_test", "display_generation": 1,
        "created_at": time.time() - panel.FEISHU_CARD_MAX_AGE_SECONDS - 10, "expired": False,
    })
    callback = {"value": {"kind": "my_agent_panel_action", "binding_ref": "old", "action": "panel.refresh"},
                "operator_open_id": "ou_test", "chat_id": "oc_test", "message_id": "om_old"}
    return path, callback


def test_recorded_card_receipt_recovers_after_interrupted_publish(tmp_path, monkeypatch):
    path, callback = _expired_panel(tmp_path, monkeypatch)
    sends = []

    def send(_target, _card, *, idempotency_key, **options):
        sends.append((idempotency_key, options))
        return {"chat_id": "oc_test", "message_id": "om_new"}

    adapter = SimpleNamespace(my_agent_home=tmp_path, send_control_card=send)
    write = panel._write_panel_state

    def interrupted_publish(target, record):
        if target != path and record.get("message_id") == "om_new":
            raise OSError("simulated successor publication interruption")
        write(target, record)

    with monkeypatch.context() as patch:
        patch.setattr(panel, "_write_panel_state", interrupted_publish)
        with pytest.raises(OSError, match="publication interruption"):
            panel.handle_panel_card_action(adapter, callback)
    assert panel.handle_panel_card_action(adapter, callback)["toast"]["type"] == "info"
    assert len(sends) == 1
    assert panel._read_panel_state(path)["expired"] is True
    assert sends[0][1]["uuid_value"] == panel._read_panel_state(path)["successor_id"]


def test_parallel_callbacks_do_not_repeat_send_or_hold_binding_lock(tmp_path, monkeypatch):
    path, callback = _expired_panel(tmp_path, monkeypatch)
    entered, release, sends = Event(), Event(), []

    def send(_target, _card, *, idempotency_key, uuid_value):
        sends.append(uuid_value)
        entered.set()
        assert release.wait(5)
        return {"chat_id": "oc_test", "message_id": "om_new"}

    adapter = SimpleNamespace(my_agent_home=tmp_path, send_control_card=send)
    with ThreadPoolExecutor(max_workers=2) as executor:
        leader = executor.submit(panel.handle_panel_card_action, adapter, callback)
        try:
            assert entered.wait(5)
            second = executor.submit(panel.handle_panel_card_action, adapter, callback)
            assert second.result(timeout=2)["toast"]["type"] == "warning"
        finally:
            release.set()
        assert leader.result(timeout=5)["toast"]["type"] == "info"
    state = panel._read_panel_state(path)
    assert sends == [state["successor_id"]]
    assert state["display_generation"] == 2
    assert state["expired"] is True


def test_lost_replacement_cas_records_reserved_successor(tmp_path, monkeypatch):
    path, callback = _expired_panel(tmp_path, monkeypatch)
    sends = []

    def send(_target, _card, *, idempotency_key, uuid_value):
        sends.append(uuid_value)
        with panel.locked_json_path(path):
            state = panel._read_panel_state(path)
            state.update(display_generation=3, successor_id="beef", expired=True)
            panel._write_panel_state(path, state)
        return {"chat_id": "oc_test", "message_id": "om_superseded"}

    adapter = SimpleNamespace(my_agent_home=tmp_path, send_control_card=send)
    assert panel.handle_panel_card_action(adapter, callback)["toast"]["type"] == "warning"
    assert panel._read_panel_state(path)["successor_id"] == "beef"
    successor = panel._read_panel_state(panel._panel_state_path(tmp_path, sends[0]))
    assert successor["replacement_state"] == "superseded"
    assert successor["expired"] is True
    assert successor["message_id"] == "om_superseded"
    panel.handle_panel_card_action(adapter, callback)
    assert len(sends) == 1


def test_uncertain_send_reuses_reserved_uuid_and_frozen_card(tmp_path, monkeypatch):
    path, callback = _expired_panel(tmp_path, monkeypatch)
    sends = []
    render = iter([{"frozen": True}, {"expired": True}])
    monkeypatch.setattr(panel, "render_progress_card", lambda _: next(render))

    def send(_target, card, *, idempotency_key, uuid_value):
        sends.append((uuid_value, card))
        if len(sends) == 1:
            raise OSError("simulated missing send response")
        return {"chat_id": "oc_test", "message_id": "om_new"}

    adapter = SimpleNamespace(my_agent_home=tmp_path, send_control_card=send)
    with pytest.raises(OSError, match="missing send response"):
        panel.handle_panel_card_action(adapter, callback)
    assert panel._read_panel_state(path)["expired"] is False
    assert panel.handle_panel_card_action(adapter, callback)["toast"]["type"] == "info"
    assert sends[0] == sends[1]


def test_reserved_uuid_is_forwarded_as_provider_uuid(monkeypatch):
    calls = []

    def transport(_method, _path, body, _token):
        calls.append(body)
        return {"code": 0, "data": {"message_id": "om_test", "chat_id": "oc_test"}}

    monkeypatch.setattr(feishu, "_feishu_msg_api_with_retry", transport)
    adapter = SimpleNamespace(_get_tenant_access_token=lambda: "synthetic")
    feishu._send_feishu_control_card(adapter, "ou_test", {}, idempotency_key="reissue:abcd", uuid_value="abcd")
    assert calls[0]["uuid"] == "abcd"


def test_uncertain_card_past_provider_window_preserves_reservation(tmp_path, monkeypatch):
    path, callback = _expired_panel(tmp_path, monkeypatch)
    sends = []

    def send(_target, _card, *, idempotency_key, uuid_value):
        sends.append(uuid_value)
        raise OSError("simulated missing send response")

    adapter = SimpleNamespace(my_agent_home=tmp_path, send_control_card=send)
    with pytest.raises(OSError, match="missing send response"):
        panel.handle_panel_card_action(adapter, callback)
    with panel.locked_json_path(path):
        current = panel._read_panel_state(path)
        current["replacement"]["send_started_at"] = time.time() - 3601
        panel._write_panel_state(path, current)
    assert panel.handle_panel_card_action(adapter, callback)["toast"]["type"] == "error"
    assert sends == [current["successor_id"]]
    assert panel._read_panel_state(path) == current
