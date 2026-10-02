"""P12：固定档案不拼接聊天凭据，失效不回退，只有管理员用户命令能改引用。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_py_agent.agent import core
from agent_py_agent.agent.backends.errors import ModelNotConfiguredError
from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.gateway_parts import settings_control_service as settings
from agent_py_agent.agent.gateway_parts.model_profile_service import render_model_choices
from agent_py_agent.agent.memory_store.curator_models import MemoryCuratorConfig
from agent_py_agent.agent.settings.config import load_config
from agent_py_agent.agent.settings.memory import normalize_memory_settings
from agent_py_agent.agent.settings.model_profiles import (
    execute_model_profile_operation,
    model_profiles_path,
)
from agent_py_agent.agent.settings.parameter_changes import (
    ChangeOrigin,
    reset_parameter,
    revert_change,
    set_parameter,
)
from agent_py_agent.agent.settings.parameter_registry import parameter_registry
from agent_py_agent.cli.chat_parts import tui_model_menu, tui_provider_menu
from agent_py_agent.tests.test_curator_input_budget import _conversation, _service

KEY = "memory_curator_model_profile"


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    home = SimpleNamespace(root=tmp_path, config_dir=tmp_path / "config", owner_provider="local",
                           owner_kind="main", owner_id="main")
    path = tmp_path / "desktop.yaml"
    path.write_text('agent_name: "curator-test"\n', encoding="utf-8")
    config = load_config(path)
    config.model_backend, config.model_name = "echo", "deployment-test"
    return SimpleNamespace(home_paths=home, config=config)


def _add(host, **values):
    profile_id = str(uuid4())
    profile = {"model_name": "curator-test", "model_backend": "openai_compatible",
               "api_base": "https://curator.example.test/v1", "api_key": "fake-curator-credential",
               "model_context_window_tokens": 64000, **values}
    listing = execute_model_profile_operation(host, "add", {"profile_id": profile_id, "profile": profile})
    return profile_id, listing


def _bind(host, profile_id):
    setattr(host.config, KEY, profile_id)
    return MemoryCuratorConfig.from_agent_config(host.config)


def _command(host, monkeypatch, text):
    monkeypatch.setattr(settings, "_scoped_home", lambda _agent, _scope: host.home_paths)
    command = parse_conversation_control(text, reject_unknown_slash=True)
    assert command is not None and command.valid
    return settings.execute_settings_control(host, command, None)


def test_config_snapshot_keeps_the_profile_id_and_removes_old_overrides(host):
    profile_id, _ = _add(host)
    config = _bind(host, profile_id)
    assert getattr(config, "model_profile", None) == profile_id
    assert not hasattr(config, "provider") and not hasattr(config, "model")
    assert config.revision() != MemoryCuratorConfig().revision()


def test_fixed_profile_uses_its_whole_connection_not_the_selected_chat_model(host, monkeypatch):
    chat_id, _ = _add(host, model_name="chat-test", api_base="https://chat.example.test/v1", api_key="fake-chat")
    execute_model_profile_operation(host, "set_default", {"profile_id": chat_id})
    fixed_id, _ = _add(host, temperature=0.2, top_p=0.8, model_queue_wait_seconds=17,
                       model_custom_headers={"X-Tenant": "curator-test"}, model_session_header="X-Session")
    seen = []
    monkeypatch.setattr(core, "get_backend", lambda provider, config: seen.append(config) or SimpleNamespace(name=provider))
    _, provider, model = core._build_memory_curator_backend(host, host.config, _bind(host, fixed_id))
    assert (provider, model) == ("openai_compatible", "curator-test")
    config = seen[0]
    assert (config.api_base, config.api_key, config.api_key_env) == ("https://curator.example.test/v1", "fake-curator-credential", "")
    assert config.model_custom_headers == {"X-Tenant": "curator-test"} and config.model_session_header == "X-Session"
    assert (config.temperature, config.top_p, config.model_queue_wait_seconds, config.stream_enabled) == ("0.2", 0.8, 17, False)


def test_empty_reference_follows_the_owner_selection(host, monkeypatch):
    selected, _ = _add(host, model_name="owner-selected")
    execute_model_profile_operation(host, "set_default", {"profile_id": selected})
    seen = []
    monkeypatch.setattr(core, "get_backend", lambda provider, config: seen.append(config) or SimpleNamespace(name=provider))
    _, _, model = core._build_memory_curator_backend(host, host.config, _bind(host, ""))
    assert model == "owner-selected" and seen[0].api_key == "fake-curator-credential"


@pytest.mark.parametrize("case,reason", [
    ("missing", "profile_not_found"), ("default", "profile_not_found"),
    ("capability", "capability_mismatch"), ("disabled", "profile_disabled"),
    ("provider_disabled", "provider_disabled"), ("provider_capability", "capability_mismatch"),
    ("credential", "credential_missing"), ("catalog", "catalog_invalid"),
])
def test_unusable_reference_is_a_typed_failure_with_id_and_never_falls_back(host, tmp_path, case, reason):
    profile_id, _ = _add(host, capability="embedding" if case == "capability" else "agentic",
                         enabled=case != "disabled")
    path = model_profiles_path(host.home_paths)
    data = json.loads(path.read_text(encoding="utf-8"))
    provider = data["providers"][data["profiles"][profile_id]["provider_id"]]
    if case in {"provider_disabled", "provider_capability", "credential"}:
        provider.update(enabled=case != "provider_disabled", capabilities=["embedding"] if case == "provider_capability" else ["agentic"],
                        api_key="" if case == "credential" else provider["api_key"])
        path.write_text(json.dumps(data), encoding="utf-8")
    if case == "catalog":
        path.write_text("broken", encoding="utf-8")
    reference = str(uuid4()) if case == "missing" else "default" if case == "default" else profile_id
    config = _bind(host, reference)
    backend, _, _ = core._build_memory_curator_backend(host, host.config, config)
    assert backend.name == "unconfigured"
    with pytest.raises(ModelNotConfiguredError) as caught:
        backend.generate_structured("synthetic input", response_schema={})
    assert getattr(caught.value, "profile_id", None) == reference
    assert getattr(caught.value, "profile_reason", None) == reason
    store, _, _ = _conversation(tmp_path / "curator", 2)
    service = _service(tmp_path / "curator", backend, store, config)
    service.request("session_close")
    failed = service.run_if_due()
    assert failed.failure_code == "CURATOR_MODEL_NOT_CONFIGURED"
    [record] = service.run_log.list()
    [warning] = [item for item in record.warnings if item.startswith("failure_diagnostic=")]
    assert len(warning) <= 300
    diagnostic = json.loads(warning.split("=", 1)[1])
    assert diagnostic["profile_id"] == reference and diagnostic["profile_reason"] == reason
    assert "fake-curator" not in json.dumps(diagnostic) and not service.state_store.load().per_thread_cursors


def test_old_keys_warn_without_alias_or_conversion(host):
    path = host.config.config_path
    Path(path).write_text('memory_curator_provider: "echo"\nmemory_curator_model: "old-model"\n', encoding="utf-8")
    config = load_config(path)
    assert getattr(config, KEY, None) == ""
    assert not hasattr(config, "memory_curator_provider") and not hasattr(config, "memory_curator_model")
    assert all(any(f"unknown config key: '{key}'" in warning for warning in config.config_warnings)
               for key in ("memory_curator_provider", "memory_curator_model"))
    memory, warnings = normalize_memory_settings({KEY: " profile-id "})
    assert getattr(memory, KEY, None) == "profile-id" and not warnings


def test_admin_settings_writes_and_resets_but_model_actions_and_spoofed_actor_cannot(host, monkeypatch):
    profile_id, _ = _add(host)
    result = _command(host, monkeypatch, f"/settings set {KEY} {profile_id}")
    assert result.ok and "/restart" in result.message
    assert getattr(load_config(host.config.config_path), KEY) == profile_id
    assert parameter_registry()[KEY].writable is False
    for actor in ("model", "chat"):
        assert set_parameter(KEY, "", user_path=host.config.config_path, origin=ChangeOrigin(actor)).get("code") == "PARAMETER_BOUNDARY"
        assert reset_parameter(KEY, user_path=host.config.config_path, origin=ChangeOrigin(actor)).get("code") == "PARAMETER_BOUNDARY"
    history = _command(host, monkeypatch, f"/settings history {KEY}")
    change_id = history.message.splitlines()[1].split()[1]
    assert revert_change(change_id, user_path=host.config.config_path, origin=ChangeOrigin("model")).get("code") == "PARAMETER_BOUNDARY"
    assert _command(host, monkeypatch, f"/settings revert {change_id}").ok
    assert getattr(load_config(host.config.config_path), KEY) == ""
    assert _command(host, monkeypatch, f"/settings set {KEY} {profile_id}").ok
    assert _command(host, monkeypatch, f"/settings reset {KEY}").ok
    assert not _command(host, monkeypatch, "/settings set api_base https://evil.example.test").ok


def test_non_admin_cannot_change_the_reference(host, monkeypatch):
    host.home_paths.owner_provider, host.home_paths.owner_kind, host.home_paths.owner_id = "feishu", "users", "test-user"
    result = _command(host, monkeypatch, f"/settings set {KEY} unknown")
    assert not result.ok and "管理员" in result.message
    assert getattr(load_config(host.config.config_path), KEY, "") == ""


def test_settings_show_reports_id_and_model_for_running_and_saved_references(host, monkeypatch):
    current_id, _ = _add(host, model_name="running-curator")
    saved_id, _ = _add(host, model_name="saved-curator")
    _bind(host, current_id)
    assert _command(host, monkeypatch, f"/settings set {KEY} {saved_id}").ok
    result = _command(host, monkeypatch, f"/settings show {KEY}")
    assert result.ok and current_id in result.message and saved_id in result.message
    assert "running-curator" in result.message and "saved-curator" in result.message and "仅用户" in result.message
    _bind(host, "missing-id")
    result = _command(host, monkeypatch, f"/settings show {KEY}")
    assert result.ok and "profile_not_found" in result.message and "未回退" in result.message


def test_model_text_and_tui_lists_display_the_stable_profile_id(host, monkeypatch):
    profile_id, listing = _add(host)
    assert profile_id in render_model_choices(listing)
    labels = []

    async def request(*_args, **_kwargs):
        return listing

    async def dialog(_app, _title, choices, _actions, **_kwargs):
        labels.extend(label for _, label in choices.values)
        return None

    async def choose(_app, _title, rows):
        labels.extend(label for _, label in rows)
        return None

    monkeypatch.setattr(tui_model_menu, "_request", request)
    monkeypatch.setattr(tui_model_menu, "_dialog", dialog)
    monkeypatch.setattr(tui_model_menu, "_publish_selection", lambda *_: None)
    asyncio.run(tui_model_menu._select_model(None, host, "test", None))
    assert any(profile_id in label for label in labels)
    labels.clear()
    monkeypatch.setattr(tui_provider_menu, "_request", request)
    monkeypatch.setattr(tui_provider_menu, "_choose", choose)
    provider_id = listing["profiles"][-1]["provider_id"]
    asyncio.run(tui_provider_menu.manage_provider_models(None, host, "test", provider_id))
    assert any(profile_id in label for label in labels)
