"""决策模型复用原配置目录，但不能混入主/子代理生成选择；只做本地合同验收。"""

import asyncio
import json

import pytest

from agent_py_agent.agent.settings.model_profiles import (
    _resolved_profile,
    model_profiles_path,
    read_model_profiles,
    resolve_child_model_profile,
    selected_model_config,
)
from agent_py_agent.agent.settings.model_profiles import (
    execute_model_profile_operation as op,
)
from agent_py_agent.agent.settings.model_provider_schema import ModelProfileError, resolved_model
from agent_py_agent.agent.settings.shared_model_catalog import (
    public_shared_profiles,
    resolve_shared_model,
    set_shared_profile,
)
from agent_py_agent.tests.test_model_profiles import Host, add
from agent_py_agent.tests.test_shared_model_catalog import admin_host


# LLM: 测试数据只含虚构连接；调用原新增入口，不直接伪造当前 schema。
# 函数用途: 在临时 owner 目录建立决策配置，不访问服务商或生成聊天记录。
def decision(host, **fields):
    return add(host, **{"model_backend": "typesafe_decision", "model_name": "jev-test",
                       "capability": "decision", **fields})


def test_decision_roundtrip_secrets_and_generation_separation(tmp_path):
    host = Host(tmp_path)
    key, result = decision(host)
    row = next(row for row in result["profiles"] if row["id"] == key)
    assert row["capability"] == "decision" and not row["available"]
    assert row["available_for"] == ["decision"]
    assert "only-private-secret" not in json.dumps(result)
    data = read_model_profiles(model_profiles_path(host.home_paths))
    assert data["selected"] == "default"
    resolved = _resolved_profile(host, data, key, capability="decision")
    assert resolved["api_key"] == "only-private-secret"
    assert resolved["model_backend"] == "typesafe_decision"
    for action in (lambda: op(host, "set_default", {"profile_id": key}),
                   lambda: selected_model_config(host, profile_id=key),
                   lambda: resolve_child_model_profile(host, key),
                   lambda: resolved_model(data, key, require_enabled=False)):
        with pytest.raises(ModelProfileError, match="用途"):
            action()
    assert selected_model_config(host) is host.config


def test_same_name_decision_does_not_make_child_generation_ambiguous(tmp_path):
    host = Host(tmp_path)
    decision(host, model_name="same")
    chat, _ = add(host, model_name="same")
    assert resolve_child_model_profile(host, "same") == chat


@pytest.mark.parametrize("fields", [
    {"capability": "agentic"}, {"capability": "embedding"}, {"capability": []},
    {"model_backend": "openai_compatible"}, {"model_backend": {}},
    {"temperature": 0.3}, {"top_p": 0.5}, {"model_queue_wait_seconds": 0},
])
def test_decision_invalid_role_or_generation_settings_never_saved(tmp_path, fields):
    host = Host(tmp_path)
    with pytest.raises(ModelProfileError):
        decision(host, **fields)
    assert not model_profiles_path(host.home_paths).exists()


def test_provider_purpose_and_disable_are_rechecked_at_resolution(tmp_path):
    host = Host(tmp_path)
    key, _ = decision(host)
    data = read_model_profiles(model_profiles_path(host.home_paths))
    provider = data["profiles"][key]["provider_id"]
    op(host, "save_provider", {"provider_id": provider, "editing": True,
                               "provider": {"capabilities": ["agentic"]}})
    data = read_model_profiles(model_profiles_path(host.home_paths))
    with pytest.raises(ModelProfileError, match="用途"):
        resolved_model(data, key, capability="decision")
    op(host, "save_provider", {"provider_id": provider, "editing": True,
                               "provider": {"capabilities": ["decision"], "enabled": False}})
    data = read_model_profiles(model_profiles_path(host.home_paths))
    with pytest.raises(ModelProfileError, match="未启用"):
        resolved_model(data, key, capability="decision")
    row = next(row for row in op(host, "list", {})["profiles"] if row["id"] == key)
    assert row["available_for"] == []


def test_v2_migration_is_readonly_until_explicit_save(tmp_path):
    host = Host(tmp_path)
    key, _ = add(host)
    op(host, "set_default", {"profile_id": key})
    path = model_profiles_path(host.home_paths)
    legacy = json.loads(path.read_text())
    legacy["schema"] = "owner_model_profiles.v2"
    legacy.pop("decision_settings")
    path.write_text(json.dumps(legacy))
    before = path.read_bytes()
    current = read_model_profiles(path)
    assert current == {**legacy, "schema": "owner_model_profiles.v4", "decision_settings": {
        "schema": "decision_settings.v1", "revision": 0, "overrides": {},
    }}
    assert selected_model_config(host).api_key == "only-private-secret"
    assert path.read_bytes() == before
    decision(host)
    saved = json.loads(path.read_text())
    assert saved["schema"] == "owner_model_profiles.v4" and saved["selected"] == key
    assert saved["profiles"][key] == legacy["profiles"][key]
    assert all(saved["providers"][k] == v for k, v in legacy["providers"].items())


def test_v2_cannot_smuggle_future_decision_schema(tmp_path):
    host = Host(tmp_path)
    decision(host)
    path = model_profiles_path(host.home_paths)
    legacy = json.loads(path.read_text())
    legacy["schema"] = "owner_model_profiles.v2"
    path.write_text(json.dumps(legacy))
    before = path.read_bytes()
    with pytest.raises(ModelProfileError, match="损坏"):
        decision(host)
    assert path.read_bytes() == before


def test_shared_decision_retains_purpose_owner_and_revocation(tmp_path):
    admin, alice = admin_host(tmp_path), Host(tmp_path)
    key, _ = decision(admin)
    with pytest.raises(ModelProfileError):
        _resolved_profile(alice, read_model_profiles(model_profiles_path(alice.home_paths)), key, capability="decision")
    set_shared_profile(admin, key, True)
    shared = "shared:" + key
    row = public_shared_profiles(alice.home_paths)[0]
    assert not row["available"] and row["available_for"] == ["decision"]
    assert resolve_shared_model(alice.home_paths, shared, capability="decision")["model_name"] == "jev-test"
    with pytest.raises(ModelProfileError, match="用途"):
        resolve_shared_model(alice.home_paths, shared)
    with pytest.raises(ModelProfileError, match="用途"):
        resolve_child_model_profile(alice, shared)
    set_shared_profile(admin, key, False)
    with pytest.raises(ModelProfileError, match="撤销"):
        resolve_shared_model(alice.home_paths, shared, capability="decision")


def test_model_edit_form_preserves_decision_protocol_and_identity(tmp_path, monkeypatch):
    from agent_py_agent.cli.chat_parts import tui_model_menu, tui_provider_menu

    host = Host(tmp_path)
    key, result = decision(host)
    row = next(row for row in result["profiles"] if row["id"] == key)
    sent = []

    async def dialog(app, title, body, buttons, **kwargs):
        if title == "新增模型 · 选择接口":
            assert body.current_value == "typesafe_decision"
            return body.current_value
        return True

    async def request(app, agent, session, operation, payload):
        sent.append((operation, payload))
        return op(agent, operation, payload)

    monkeypatch.setattr(tui_model_menu, "_dialog", dialog)
    monkeypatch.setattr(tui_provider_menu, "_dialog", dialog)
    monkeypatch.setattr(tui_provider_menu, "_request", request)
    message = asyncio.run(tui_provider_menu._edit_model(None, host, "test", row["provider_id"], row))
    assert "尚未启用" in message
    assert [action for action, _ in sent] == ["save_model"]
    saved = read_model_profiles(model_profiles_path(host.home_paths))["profiles"][key]
    assert saved["capability"] == "decision" and saved["model_backend"] == "typesafe_decision"
    assert not set(saved) & {"temperature", "top_p", "model_queue_wait_seconds"}


@pytest.mark.parametrize("capabilities", [["decision"], ["agentic", "decision", "embedding"]])
def test_provider_edit_form_keeps_decision_capability(tmp_path, monkeypatch, capabilities):
    from agent_py_agent.cli.chat_parts import tui_provider_menu

    host = Host(tmp_path)
    key, result = decision(host)
    provider = result["providers"][0]
    result = op(host, "save_provider", {"provider_id": provider["id"], "editing": True,
                                       "provider": {"capabilities": capabilities}})
    sent = []

    async def dialog(*args, **kwargs):
        return True

    async def request(app, agent, session, operation, payload):
        sent.append(operation)
        return op(agent, operation, payload)

    monkeypatch.setattr(tui_provider_menu, "_dialog", dialog)
    monkeypatch.setattr(tui_provider_menu, "_request", request)
    asyncio.run(tui_provider_menu._edit_provider(None, host, "test", result["providers"][0]))
    data = read_model_profiles(model_profiles_path(host.home_paths))
    assert data["providers"][provider["id"]]["capabilities"] == sorted(capabilities)
    assert resolved_model(data, key, capability="decision")["api_key"] == "only-private-secret"
    assert sent == ["save_provider"]
