"""已有真实模型卡的私人表单外发，使用原registry/Delivery，只有provider运输替身。"""

import json
import os
import re
from contextlib import contextmanager

import pytest

from agent_py_agent.agent.adapter.feishu import FeishuAdapter
from agent_py_agent.agent.gateway_parts import model_control_cards as cards
from agent_py_agent.agent.gateway_parts.daemon_control import write_pid_record
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.cli.adapter import _write_adapter_state
from agent_py_agent.tests.test_coordinator_model_card_transactions import model_case, seed


# LLM: 真实C、原卡/HostActor/Store由冻D22创建；配置和daemon事实在进入C前合成发布，不启动adapter或使用真实secret。
# 函数用途: 只替换原Feishu发送方法，校验整个registry/Delivery与门外运输路径。
@contextmanager
def delivery_case(tmp_path, monkeypatch, *, decision="add", kind="user"):
    original_card = seed._card

    def configured_card(*args):
        result = original_card(*args)
        result[0].config.feishu_app_id = "synthetic-app"
        result[0].config.feishu_app_secret = "SYNTHETIC_APP_SECRET"
        return result

    monkeypatch.setattr(seed, "_card", configured_card)
    paths = gateway_paths_from_root(tmp_path / "gateway")
    write_pid_record(paths.adapter_pid)
    _write_adapter_state(
        paths,
        "running",
        {"channels": [{"name": "feishu", "health": {"state": "healthy", "error_code": ""}}]},
    )
    delivered = []

    def send(_adapter, target, card, *, idempotency_key):
        delivered.append((target, card, idempotency_key))
        return {"message_id": "om_cold_" + str(len(delivered)), "chat_id": "oc_cold_private"}

    monkeypatch.setattr(FeishuAdapter, "send_control_card", send)
    with model_case(tmp_path, monkeypatch, kind=kind) as case:
        agent, host, path, row, body, server, runtime, sent = case
        body["decision"] = decision
        if decision == "add":
            body["fields"] = {"profile_id": ""}
        yield case, delivered


@pytest.mark.parametrize("kind", ["user", "group"])
@pytest.mark.parametrize("decision", ["add", "edit"])
def test_cold_existing_selection_delivers_original_private_form_once(
    tmp_path, monkeypatch, kind, decision
):
    with delivery_case(tmp_path, monkeypatch, decision=decision, kind=kind) as (case, delivered):
        _agent, host, path, row, body, server, _runtime, _sent = case
        status, result = seed.post(server, row, body)
        assert status == 200 and result["ok"] is True, result
        assert len(delivered) == 1 and delivered[0][0] == row["actor_id"]
        parent = json.loads(path.read_text())
        assert (
            parent["status"] == "completed"
            and parent["decision"] == decision
            and parent["dispatch_id"]
        )
        assert seed.post(server, row, body)[1]["ok"] is True
        assert len(delivered) == 1
        reference = re.search(r'"reference": "([^"]+)"', json.dumps(delivered[0][1])).group(1)
        from agent_py_agent.agent.user_space.interaction_refs import verify_interaction_reference

        payload = verify_interaction_reference(
            server.control_plane.home_root, reference, channel="feishu", actor_id=row["actor_id"]
        )
        child = json.loads(
            cards._record_path(host.home_paths.owner_home_dir, payload["control_id"]).read_text()
        )
        assert child["status"] == "active" and child["parent_control_id"] == row["control_id"]
        assert child["thread_id"] == row["thread_id"] and child["owner"] == row["owner"]
        assert child["profile_id"] == (body["fields"]["profile_id"] if decision == "edit" else "")
        assert server.agent is None and os.getpid() > 0


def _child(host, delivered, server, actor):
    from agent_py_agent.agent.user_space.interaction_refs import verify_interaction_reference

    reference = re.search(r'"reference": "([^"]+)"', json.dumps(delivered[-1][1])).group(1)
    payload = verify_interaction_reference(
        server.control_plane.home_root, reference, channel="feishu", actor_id=actor
    )
    path = cards._record_path(host.home_paths.owner_home_dir, payload["control_id"])
    return json.loads(path.read_text()), reference


@pytest.mark.parametrize("decision", ["add", "edit"])
def test_cold_delivered_form_saves_original_profile_and_empty_key_contract(
    tmp_path, monkeypatch, decision
):
    from agent_py_agent.agent.settings.model_profiles import (
        model_profiles_path,
        read_model_profiles,
    )

    with delivery_case(tmp_path, monkeypatch, decision=decision) as (case, delivered):
        _agent, host, _path, row, body, server, _runtime, _sent = case
        assert seed.post(server, row, body)[1]["ok"] is True
        child, reference = _child(host, delivered, server, row["actor_id"])
        save = {
            "reference": reference,
            "decision": "save",
            "message_id": child["message_id"],
            "chat_id": child["chat_id"],
            "fields": {
                "model_backend": "openai_compatible",
                "api_base": "https://model.example.invalid/v1",
                "model_name": "saved-from-private-form",
                "model_context_window_tokens": "8192",
                "api_key": "" if decision == "edit" else "SYNTHETIC_NEW_MODEL_KEY",
            },
        }
        assert seed.post(server, child, save)[1]["ok"] is True
        data = read_model_profiles(model_profiles_path(host.home_paths))
        assert len(data["profiles"]) == (1 if decision == "edit" else 2)
        saved = next(
            value
            for value in data["profiles"].values()
            if value["model_name"] == "saved-from-private-form"
        )
        assert data["providers"][saved["provider_id"]]["api_key"] == (
            "SYNTHETIC_MODEL_OLD_KEY" if decision == "edit" else "SYNTHETIC_NEW_MODEL_KEY"
        )
        assert len(delivered) == 1
        assert seed.post(server, child, save)[1]["ok"] is True and len(delivered) == 1


def test_cold_delivery_never_holds_original_writer_window(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts.private_interaction_transaction import (
        _PrivateInteractionTransaction,
    )

    active = []
    original = _PrivateInteractionTransaction.perform

    def observed(source, operation):
        def inside():
            active.append(True)
            try:
                return operation()
            finally:
                active.pop()

        return original(source, inside)

    monkeypatch.setattr(_PrivateInteractionTransaction, "perform", observed)
    with delivery_case(tmp_path, monkeypatch) as (case, delivered):
        _agent, _host, _path, row, body, server, _runtime, _sent = case
        send = FeishuAdapter.send_control_card

        def outside(adapter, *args, **kwargs):
            assert not active
            return send(adapter, *args, **kwargs)

        monkeypatch.setattr(FeishuAdapter, "send_control_card", outside)
        assert seed.post(server, row, body)[1]["ok"] is True and len(delivered) == 1
        assert not active


def test_cold_parent_cancel_during_delivery_wins_and_child_cannot_save(tmp_path, monkeypatch):
    from agent_py_agent.agent.settings.model_profiles import model_profiles_path

    with delivery_case(tmp_path, monkeypatch) as (case, delivered):
        _agent, host, path, row, body, server, _runtime, _sent = case
        before = model_profiles_path(host.home_paths).read_bytes()
        send = FeishuAdapter.send_control_card

        def cancel_during_send(adapter, *args, **kwargs):
            cancel = {**body, "decision": "cancel"}
            status, result = seed.post(server, row, cancel)
            assert status == 200 and result["decision"] == "cancelled"
            return send(adapter, *args, **kwargs)

        monkeypatch.setattr(FeishuAdapter, "send_control_card", cancel_during_send)
        status, result = seed.post(server, row, body)
        assert status == 200 and result["decision"] == "cancelled"
        assert json.loads(path.read_text())["status"] == "cancelled"
        child, reference = _child(host, delivered, server, row["actor_id"])
        save = {
            "reference": reference,
            "decision": "save",
            "message_id": child["message_id"],
            "chat_id": child["chat_id"],
            "fields": {
                "model_backend": "openai_compatible",
                "api_base": "https://model.example.invalid/v1",
                "model_name": "not-saved",
                "model_context_window_tokens": "8192",
                "api_key": "SYNTHETIC_UNUSED",
            },
        }
        assert seed.post(server, child, save)[1]["error_code"] == "IM_INTERACTION_ALREADY_HANDLED"
        assert model_profiles_path(host.home_paths).read_bytes() == before


@pytest.mark.parametrize("same_error_code", [False, True])
def test_cold_unconfirmed_provider_result_keeps_dispatch_id_and_never_resends(
    tmp_path, monkeypatch, same_error_code
):
    with delivery_case(tmp_path, monkeypatch) as (case, _delivered):
        _agent, _host, path, row, body, server, _runtime, _sent = case
        called = []

        def unavailable(*_args, **_kwargs):
            called.append(True)
            error = RuntimeError("synthetic_transport_error")
            if same_error_code:
                error.error_code = "ACTIVE_TURN_OUTCOME_UNCERTAIN"
            raise error

        monkeypatch.setattr(FeishuAdapter, "send_control_card", unavailable)
        status, result = seed.post(server, row, body)
        assert status == 200 and result["error_code"] == "CHANNEL_CONTROL_IO_FAILED"
        assert result["effect_outcome"] == "unknown"
        first = json.loads(path.read_text())
        assert first["status"] == "dispatch_failed" and len(called) == 1
        seed.post(server, row, body)
        assert json.loads(path.read_text()) == first and len(called) == 1


@pytest.mark.parametrize("kind", ["context", "outcome", "bounded", "tool"])
def test_cold_actual_transport_exception_reaches_original_handler_unchanged(
    tmp_path, monkeypatch, kind
):
    from agent_py_agent.agent.backends.bounded_call import BoundedCallExitUnconfirmedError
    from agent_py_agent.agent.common.request_execution_ports import (
        RequestExecutionContextUnavailable,
        RequestExecutionOutcomeUncertainError,
    )
    from agent_py_agent.agent.common.tool_write_context import ToolWriteContextUnavailable
    from agent_py_agent.agent.gateway_parts import private_interaction_transaction as pi

    errors = {
        "context": RequestExecutionContextUnavailable("synthetic"),
        "outcome": RequestExecutionOutcomeUncertainError(
            {
                "id": "synthetic-request",
                "execution_attempt_id": "synthetic-attempt",
                "lease_epoch": 1,
                "lease_owner": "synthetic-worker",
            },
            "provider",
        ),
        "bounded": BoundedCallExitUnconfirmedError("synthetic-call", "worker_exit_unconfirmed"),
        "tool": ToolWriteContextUnavailable("synthetic"),
    }
    error = errors[kind]
    with delivery_case(tmp_path, monkeypatch) as (case, _delivered):
        _agent, _host, path, row, body, server, _runtime, _sent = case
        observed = []
        original = pi.execute_coordinator_private_interaction

        def observe(*args, **kwargs):
            try:
                return original(*args, **kwargs)
            except BaseException as actual:
                observed.append(actual)
                raise

        def unknown(*_args, **_kwargs):
            raise error

        monkeypatch.setattr(pi, "execute_coordinator_private_interaction", observe)
        monkeypatch.setattr(FeishuAdapter, "send_control_card", unknown)
        assert seed.post(server, row, body)[0] == 503
        assert observed == [error] and observed[0] is error
        saved = json.loads(path.read_text())
        assert saved["status"] == "dispatching" and "result" not in saved


def test_cold_stop_after_provider_receipt_does_not_publish_or_finalize_late(tmp_path, monkeypatch):
    with delivery_case(tmp_path, monkeypatch) as (case, delivered):
        _agent, host, path, row, body, server, runtime, _sent = case
        from agent_py_agent.agent.gateway_parts import private_interaction_transaction as pi
        from agent_py_agent.agent.gateway_parts.multiprocess_global_write import GlobalWriteError

        errors, observed = [], []
        original_perform = pi._PrivateInteractionTransaction.perform
        original_execute = pi.execute_coordinator_private_interaction

        def record_refusal(source, operation):
            try:
                return original_perform(source, operation)
            except GlobalWriteError as error:
                errors.append(error)
                raise

        def record_outer(*args, **kwargs):
            try:
                return original_execute(*args, **kwargs)
            except GlobalWriteError as error:
                observed.append(error)
                raise

        monkeypatch.setattr(pi._PrivateInteractionTransaction, "perform", record_refusal)
        monkeypatch.setattr(pi, "execute_coordinator_private_interaction", record_outer)
        send = FeishuAdapter.send_control_card

        def stop_after_send(*args, **kwargs):
            result = send(*args, **kwargs)
            runtime.stop_producers()
            return result

        monkeypatch.setattr(FeishuAdapter, "send_control_card", stop_after_send)
        assert seed.post(server, row, body)[0] == 503
        assert len(delivered) == 1
        assert len(errors) == 1 and observed == [errors[0]] and observed[0] is errors[0]
        parent = json.loads(path.read_text())
        assert parent["status"] == "dispatching" and "result" not in parent
        assert len(list(path.parent.glob("model-*.json"))) == 1


def test_cold_existing_key_lock_does_not_block_readonly_child_signing(tmp_path, monkeypatch):
    from agent_py_agent.agent.common.json_io import locked_json_path
    from agent_py_agent.agent.path_access_policy import PRIVATE_INTERACTIONS_OWNER_PARTS

    with delivery_case(tmp_path, monkeypatch) as (case, delivered):
        _agent, host, _path, row, body, server, _runtime, _sent = case
        key = host.home_paths.owner_home_dir.joinpath(*PRIVATE_INTERACTIONS_OWNER_PARTS, "key.json")
        before = key.read_bytes()
        with locked_json_path(key):
            assert seed.post(server, row, body)[1]["ok"] is True
        assert key.read_bytes() == before and len(delivered) == 1


def test_readonly_signing_missing_key_does_not_create_owner_files(tmp_path):
    from agent_py_agent.agent.user_space.interaction_refs import sign_interaction_reference

    home = tmp_path / "not-created"
    with pytest.raises(ValueError):
        sign_interaction_reference(home, {"kind": "model_configuration"}, create_key=False)
    assert not home.exists()


@pytest.mark.parametrize(
    "error_code", ["CHANNEL_CONTROL_PROVIDER_REJECTED", "CHANNEL_CONTROL_AUTH_UNAVAILABLE"]
)
def test_original_provider_refusal_stays_not_started_with_safe_code(
    tmp_path, monkeypatch, error_code
):
    with delivery_case(tmp_path, monkeypatch) as (case, _delivered):
        _agent, _host, path, row, body, server, _runtime, _sent = case
        called = []

        def refused(*_args, **_kwargs):
            called.append(True)
            return {"error_code": error_code, "provider_code": 99991663}

        monkeypatch.setattr(FeishuAdapter, "send_control_card", refused)
        status, result = seed.post(server, row, body)
        assert status == 200 and result["ok"] is False and result["error_code"] == error_code
        assert result["effect_outcome"] == "not_started" and result["provider_code"] == 99991663
        saved = path.read_bytes()
        assert json.loads(saved)["status"] == "dispatch_failed"
        seed.post(server, row, body)
        assert len(called) == 1 and path.read_bytes() == saved


@pytest.mark.parametrize("known", [False, True])
def test_count0_original_dispatch_preserves_same_delivery_receipt_facts(
    tmp_path, monkeypatch, known
):
    from agent_py_agent.agent.gateway_parts.private_interaction_service import (
        resolve_private_interaction,
    )
    from agent_py_agent.agent.session_lock.service import UnlockService
    from agent_py_agent.agent.session_lock.store import SessionLockStore
    from agent_py_agent.agent.settings.model_profiles import execute_model_profile_operation
    from agent_py_agent.agent.user_space.owner_resolver import owner_identity_from_config
    from agent_py_agent.tests.test_credential_callback_replay import _card

    monkeypatch.setenv("MY_AGENT_HOME", str(tmp_path / "home"))
    agent, _credential_path, credential, _credential_body = _card(tmp_path, "user")
    UnlockService(
        SessionLockStore(agent.home_paths.root / "session_lock/private_chat_locks.db")
    ).set_password(credential["actor_id"], "Synthetic-Private-Password-123")
    owner = owner_identity_from_config(agent.config)
    host = cards.model_control_host(agent, owner)
    thread = credential["scope"]["thread_id"]
    listing = execute_model_profile_operation(host, "list", {}, thread_id=thread)
    delivered = []
    adapter = agent.channel_registry.adapter_for("feishu")

    def sent(_target, card, *, idempotency_key):
        delivered.append(card)
        return {"message_id": "om_count0_model", "chat_id": "oc_count0_model"}

    monkeypatch.setattr(adapter, "send_control_card", sent)
    cards.send_model_control_card(
        agent,
        host,
        owner,
        actor_id=credential["actor_id"],
        thread_id=thread,
        target=credential["actor_id"],
        listing=listing,
    )
    reference = re.search(r'"reference": "([^"]+)"', json.dumps(delivered[0])).group(1)

    def failure(*_args, **_kwargs):
        if known:
            return {"error_code": "CHANNEL_CONTROL_PROVIDER_REJECTED", "provider_code": 99991663}
        raise RuntimeError("synthetic_transport_error")

    monkeypatch.setattr(adapter, "send_control_card", failure)
    body = {
        "reference": reference,
        "decision": "add",
        "message_id": "om_count0_model",
        "chat_id": "oc_count0_model",
        "fields": {"profile_id": ""},
    }
    result = resolve_private_interaction(
        agent, body, channel="feishu", actor_id=credential["actor_id"]
    )
    assert result["ok"] is False
    assert result["effect_outcome"] == ("not_started" if known else "unknown")
    assert result["error_code"] == (
        "CHANNEL_CONTROL_PROVIDER_REJECTED" if known else "CHANNEL_CONTROL_IO_FAILED"
    )
    if known:
        assert result["provider_code"] == 99991663
