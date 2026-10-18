"""冷C模型卡仅原真实row/catalog/thread事务；外发只有进入C前的fake Delivery种卡。"""

import json
import re
from contextlib import contextmanager

import pytest

from agent_py_agent.agent.gateway_parts import model_control_cards as cards
from agent_py_agent.agent.settings.model_profiles import (
    execute_model_profile_operation,
    model_profiles_path,
    read_model_profiles,
)
from agent_py_agent.agent.user_space.owner_resolver import owner_identity_from_config
from agent_py_agent.tests import test_coordinator_private_credential_transactions as seed


# LLM: 原Agent/TaskLink/HostActor与模型菜单均在C前沿原生产入口创建；C期间原21 fixture禁止新Agent/foreign pool。
# 函数用途: 提供准确签名thread/卡与真实临时C，不把假current execution或base补属性当许可。
@contextmanager
def model_case(tmp_path, monkeypatch, *, operation="select", kind="user", editing=False):
    original = seed._card
    prepared = {}

    def create_card(*args):
        agent, path, credential, credential_body = original(*args)
        sent = []
        adapter = agent.channel_registry.adapter_for("feishu")

        def deliver(target, card, *, idempotency_key):
            message = f"om_model_{len(sent)}"
            chat = target if target.startswith("oc_") else "oc_private_synthetic"
            sent.append((target, card, message, chat))
            return {"message_id": message, "chat_id": chat}

        monkeypatch.setattr(adapter, "send_control_card", deliver)
        owner = owner_identity_from_config(agent.config)
        host = cards.model_control_host(agent, owner)
        thread_id = credential["scope"]["thread_id"]
        from agent_py_agent.agent.user_space.group_authorization_context import (
            bind_group_interaction_host,
        )

        bind_group_interaction_host(
            agent, host, {**credential, "thread_id": thread_id}, credential["actor_id"]
        )
        profile = {
            "model_backend": "openai_compatible",
            "api_base": "https://model.example.invalid/v1",
            "model_name": "synthetic-model",
            "api_key": "SYNTHETIC_MODEL_OLD_KEY",
            "model_context_window_tokens": 8192,
        }
        execute_model_profile_operation(
            host,
            "add",
            {"profile_id": "b00f0289-a209-40fe-a3b7-a01cdf972abb", "profile": profile},
            thread_id=thread_id,
        )
        listing = execute_model_profile_operation(host, "list", {}, thread_id=thread_id)
        kwargs = {}
        if operation != "save":
            kwargs["listing"] = listing
        elif editing:
            from agent_py_agent.agent.gateway_parts.model_control_dispatch import ModelFormOrigin

            kwargs["form_origin"] = ModelFormOrigin(
                profile_id="b00f0289-a209-40fe-a3b7-a01cdf972abb"
            )
        cards.send_model_control_card(
            agent,
            host,
            owner,
            actor_id=credential["actor_id"],
            thread_id=thread_id,
            target=credential["chat_id"],
            **kwargs,
        )
        _target, card, message, chat = sent[-1]
        reference = re.search(r'"reference": "([^"]+)"', json.dumps(card)).group(1)
        from agent_py_agent.agent.user_space.interaction_refs import verify_interaction_reference

        payload = verify_interaction_reference(
            agent.home_paths.root, reference, channel="feishu", actor_id=credential["actor_id"]
        )
        model_path = cards._record_path(host.home_paths.owner_home_dir, payload["control_id"])
        row = json.loads(model_path.read_text())
        fields = {"profile_id": "b00f0289-a209-40fe-a3b7-a01cdf972abb"}
        if operation == "save":
            fields = {
                **profile,
                "api_key": "" if editing else "SYNTHETIC_MODEL_NEW_KEY",
                "model_context_window_tokens": "8192",
            }
            fields = {key: str(value) for key, value in fields.items()}
        body = {
            "reference": reference,
            "decision": operation,
            "message_id": message,
            "chat_id": chat,
            "fields": fields,
        }
        if operation == "cancel" and kind == "group":
            body["submitter"] = {
                "actor_id": row["actor_id"],
                "chat_id": owner.owner_id,
                "current_group_member": True,
            }
        prepared.update(host=host, path=model_path, row=row, body=body, sent=sent)
        return agent, path, credential, credential_body

    monkeypatch.setattr(seed, "_card", create_card)
    with seed.original_case(tmp_path, monkeypatch, kind=kind) as (
        agent,
        _path,
        _row,
        _body,
        server,
        runtime,
    ):
        yield (
            agent,
            prepared["host"],
            prepared["path"],
            prepared["row"],
            prepared["body"],
            server,
            runtime,
            prepared["sent"],
        )


@pytest.mark.parametrize("kind", ["user", "group"])
@pytest.mark.parametrize("operation", ["select", "set_default", "save", "cancel"])
def test_cold_c_model_card_commits_original_catalog_thread_and_row(
    tmp_path, monkeypatch, kind, operation
):
    with model_case(tmp_path, monkeypatch, kind=kind, operation=operation) as (
        _agent,
        host,
        path,
        row,
        body,
        server,
        _runtime,
        sent,
    ):
        before = host.conversation_store.threads.load(row["thread_id"])
        status, result = seed.post(server, row, body)
        assert status == 200 and result["ok"] is True, result
        saved = json.loads(path.read_text())
        assert saved["status"] == ("cancelled" if operation == "cancel" else "completed")
        after = host.conversation_store.threads.load(row["thread_id"])
        if operation == "select":
            assert (
                after.model_profile_id == "b00f0289-a209-40fe-a3b7-a01cdf972abb"
                and after.model_selection_revision == before.model_selection_revision + 1
            )
        elif operation == "set_default":
            assert (
                read_model_profiles(model_profiles_path(host.home_paths))["selected"]
                == "b00f0289-a209-40fe-a3b7-a01cdf972abb"
            )
            assert after.model_profile_id == before.model_profile_id
        elif operation == "save":
            assert len(read_model_profiles(model_profiles_path(host.home_paths))["profiles"]) == 2
        assert len(sent) == 1 and server.agent is None
        frozen = path.read_bytes()
        assert seed.post(server, row, body)[1]["ok"] is True
        assert path.read_bytes() == frozen


@pytest.mark.parametrize("fault", ["credential", "actor", "message_id", "chat_id", "reference"])
def test_cold_model_rejects_foreign_callback_before_original_writes(tmp_path, monkeypatch, fault):
    with model_case(tmp_path, monkeypatch) as (
        _agent,
        host,
        path,
        row,
        body,
        server,
        _runtime,
        _sent,
    ):
        tracked = (
            path,
            model_profiles_path(host.home_paths),
            host.conversation_store.storage.thread_path(row["thread_id"]),
        )
        before = [p.read_bytes() for p in tracked]
        kwargs = {}
        if fault == "credential":
            kwargs["credential"] = False
        elif fault == "actor":
            kwargs["actor"] = "ou_foreign"
        else:
            body[fault] = "invalid"
        assert seed.post(server, row, body, **kwargs)[0] == 403
        assert [p.read_bytes() for p in tracked] == before


@pytest.mark.parametrize("target", ["card", "catalog", "thread", "group"])
def test_cold_model_original_lock_contention_is_unexecuted_and_replayable(
    tmp_path, monkeypatch, target
):
    import time

    from agent_py_agent.agent.common.json_io import locked_json_path

    with model_case(tmp_path, monkeypatch, kind="group" if target == "group" else "user") as (
        _agent,
        host,
        path,
        row,
        body,
        server,
        _runtime,
        _sent,
    ):
        thread_path = host.conversation_store.storage.thread_path(row["thread_id"])
        catalog = model_profiles_path(host.home_paths)
        locked = {
            "card": path,
            "catalog": catalog,
            "thread": thread_path,
            "group": host.home_paths.owner_tool_policy_json,
        }[target]
        tracked = (path, catalog, thread_path)
        before = [p.read_bytes() for p in tracked]
        with locked_json_path(locked):
            started = time.monotonic()
            status, result = seed.post(server, row, body)
            assert status == 503 and result["error_code"] == "IM_INTERACTION_HOST_UNAVAILABLE"
            assert time.monotonic() - started < 1.0
            assert [p.read_bytes() for p in tracked] == before
        assert seed.post(server, row, body)[0] == 200
        assert json.loads(path.read_text())["status"] == "completed"


@pytest.mark.parametrize("changed", ["catalog", "thread", "shared"])
def test_cold_model_rechecks_original_generation_and_selection_cas(tmp_path, monkeypatch, changed):
    from agent_py_agent.agent.settings.shared_model_catalog import shared_catalog_path
    from agent_py_agent.agent.settings.thread_model_selection import thread_model_profile_id

    with model_case(tmp_path, monkeypatch) as (
        _agent,
        host,
        path,
        row,
        body,
        server,
        _runtime,
        _sent,
    ):
        if changed == "thread":
            thread_model_profile_id(host, row["thread_id"], select="default")
        else:
            source = (
                model_profiles_path(host.home_paths)
                if changed == "catalog"
                else shared_catalog_path(host.home_paths)
            )
            source.parent.mkdir(parents=True, exist_ok=True)
            data = json.loads(source.read_text()) if source.exists() else {}
            data["catalog_generation"] = "changed-generation"
            source.write_text(json.dumps(data))
        before = path.read_bytes()
        assert seed.post(server, row, body)[0] == 403
        assert path.read_bytes() == before


def test_cold_model_shutdown_before_commit_keeps_original_bytes(tmp_path, monkeypatch):
    with model_case(tmp_path, monkeypatch) as (
        _agent,
        host,
        path,
        row,
        body,
        server,
        runtime,
        _sent,
    ):
        tracked = (
            path,
            model_profiles_path(host.home_paths),
            host.conversation_store.storage.thread_path(row["thread_id"]),
        )
        before = [p.read_bytes() for p in tracked]
        from agent_py_agent.agent.gateway_parts.private_interaction_transaction import (
            _PrivateInteractionTransaction,
        )

        original = _PrivateInteractionTransaction.perform

        def stop_before_actual_gate(source, operation):
            runtime.stop_producers()
            return original(source, operation)

        monkeypatch.setattr(_PrivateInteractionTransaction, "perform", stop_before_actual_gate)
        assert seed.post(server, row, body)[0] == 503
        assert [p.read_bytes() for p in tracked] == before


@pytest.mark.parametrize("decision", ["add", "edit"])
def test_cold_model_unavailable_adapter_keeps_failed_dispatch_without_success(
    tmp_path, monkeypatch, decision
):
    with model_case(tmp_path, monkeypatch) as (
        _agent,
        _host,
        path,
        row,
        body,
        server,
        _runtime,
        sent,
    ):
        body["decision"] = decision
        status, result = seed.post(server, row, body)
        assert status == 200 and result["ok"] is False
        assert result["error_code"] == "CHANNEL_ADAPTER_UNAVAILABLE"
        assert result["effect_outcome"] == "not_started"
        saved = json.loads(path.read_text())
        assert saved["status"] == "dispatch_failed" and saved["dispatch_id"]
        assert cards._model_control_payload(saved) == cards._model_control_payload(row)
        assert all(saved[key] == row[key] for key in ("reference", "message_id", "chat_id"))
        assert len(list(path.parent.glob("model-*.json"))) == 1 and len(sent) == 1
        frozen = path.read_bytes()
        seed.post(server, row, body)
        assert path.read_bytes() == frozen and len(sent) == 1


@pytest.mark.parametrize("changed", ["fields", "decision"])
def test_cold_model_completed_card_only_replays_same_logical_input(tmp_path, monkeypatch, changed):
    with model_case(tmp_path, monkeypatch) as (
        _agent,
        _host,
        path,
        row,
        body,
        server,
        _runtime,
        _sent,
    ):
        assert seed.post(server, row, body)[0] == 200
        before = path.read_bytes()
        if changed == "fields":
            body["fields"]["profile_id"] = "default"
        else:
            body["decision"] = "set_default"
        assert seed.post(server, row, body)[0] == 403
        assert path.read_bytes() == before


@pytest.mark.parametrize("change_endpoint", [False, True])
def test_cold_model_edit_empty_key_retains_only_original_endpoint(
    tmp_path, monkeypatch, change_endpoint
):
    with model_case(tmp_path, monkeypatch, operation="save", editing=True) as (
        _agent,
        host,
        path,
        row,
        body,
        server,
        _runtime,
        _sent,
    ):
        if change_endpoint:
            body["fields"]["api_base"] = "https://another.example.invalid/v1"
        before = path.read_bytes()
        status, result = seed.post(server, row, body)
        assert status == (403 if change_endpoint else 200)
        data = read_model_profiles(model_profiles_path(host.home_paths))
        provider = data["providers"][data["profiles"][row["profile_id"]]["provider_id"]]
        assert provider["api_key"] == "SYNTHETIC_MODEL_OLD_KEY"
        if change_endpoint:
            assert path.read_bytes() == before
        else:
            assert result["ok"] is True
            assert provider["api_base"] == "https://model.example.invalid/v1"
        assert "SYNTHETIC_MODEL_OLD_KEY" not in path.read_text()


def test_cold_model_selection_keeps_f6_invalidation_and_revision(tmp_path, monkeypatch):
    from dataclasses import replace

    with model_case(tmp_path, monkeypatch) as (
        _agent,
        host,
        _path,
        row,
        body,
        server,
        _runtime,
        _sent,
    ):
        host.conversation_store.threads.update_atomic(
            row["thread_id"],
            lambda t: replace(
                t, provider_context_observation={"old": True}, model_context_usage={"old": True}
            ),
        )
        assert seed.post(server, row, body)[0] == 200
        thread = host.conversation_store.threads.require(row["thread_id"])
        assert thread.model_selection_revision == row["selection_revision"] + 1
        assert thread.provider_context_observation == {} and thread.model_context_usage == {}


def test_cold_model_group_cancel_keeps_first_actual_member_and_audit(tmp_path, monkeypatch):
    with model_case(tmp_path, monkeypatch, operation="cancel", kind="group") as (
        _agent,
        _host,
        path,
        row,
        body,
        server,
        _runtime,
        _sent,
    ):
        actor = "ou_other_group_member"
        body["submitter"]["actor_id"] = actor
        status, result = seed.post(server, row, body, actor=actor)
        assert status == 200 and result["cancelled_by"]["actor_id"] == actor
        saved = path.read_bytes()
        body["submitter"]["actor_id"] = row["actor_id"]
        status, result = seed.post(server, row, body)
        assert status == 200 and result["cancelled_by"]["actor_id"] == actor
        assert path.read_bytes() == saved


@pytest.mark.parametrize("decision", ["select", "cancel"])
def test_cold_model_capability_off_still_allows_original_cancel_only(
    tmp_path, monkeypatch, decision
):
    from agent_py_agent.agent.capability import runtime_config_reload
    from agent_py_agent.agent.capability.config import CapabilityConfig

    with model_case(tmp_path, monkeypatch, operation=decision) as (
        _agent,
        _host,
        path,
        row,
        body,
        server,
        _runtime,
        _sent,
    ):
        monkeypatch.setattr(
            runtime_config_reload,
            "capability_config_for_paths",
            lambda **_kwargs: CapabilityConfig(im_model_cards_enabled=False),
        )
        before = path.read_bytes()
        status, result = seed.post(server, row, body)
        assert status == (200 if decision == "cancel" else 403)
        if decision == "cancel":
            assert result["ok"] is True
        else:
            assert path.read_bytes() == before


def test_cold_model_source_copy_and_closed_source_never_write_late(tmp_path, monkeypatch):
    import copy

    from agent_py_agent.agent.gateway_parts import private_interaction_transaction as pi
    from agent_py_agent.agent.gateway_parts.multiprocess_global_write import GlobalWriteError

    original = pi._PrivateInteractionTransaction.close
    observed = []

    def close(source):
        with pytest.raises(GlobalWriteError):
            copy.copy(source).perform(lambda: pytest.fail("复制来源不能提交"))
        original(source)
        with pytest.raises(GlobalWriteError):
            source.perform(lambda: pytest.fail("退出来源不能迟到提交"))
        observed.append(True)

    monkeypatch.setattr(pi._PrivateInteractionTransaction, "close", close)
    with model_case(tmp_path, monkeypatch) as (
        _agent,
        _host,
        _path,
        row,
        body,
        server,
        _runtime,
        _sent,
    ):
        assert seed.post(server, row, body)[0] == 200
        assert observed == [True]
