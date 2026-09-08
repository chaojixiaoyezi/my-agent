"""用户模型配置的秘密、隔离、原子写入和执行快照定向验证。"""

import json
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_py_agent.agent.agent_core.model.context_window import resolve_model_context_window_tokens
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.settings.model_profiles import (
    execute_model_profile_operation,
    inherit_model_profile,
    inherited_model_config,
    model_profiles_path,
    read_model_profiles,
    selected_model_config,
    validate_model_profile,
)
from agent_py_agent.agent.settings.model_scope import ModelScopedAttribute, selected_model_scope
from agent_py_agent.agent.settings.services.runtime_config_task import (
    apply_task_runtime_config_overlay,
)


class Host:
    config = ModelScopedAttribute("config")
    backend = ModelScopedAttribute("backend")
    prompts = ModelScopedAttribute("prompts")

    def __init__(self, path, owner="alice"):
        self.home_paths = SimpleNamespace(config_dir=path, owner_provider="local", owner_kind="user", owner_id=owner)
        self.config = AgentConfig(model_name="deployment-model", api_key="deployment-secret")
        self.backend = SimpleNamespace(name="deployment")
        self.prompts = SimpleNamespace(config=self.config)


def profile(**values):
    return {"model_backend": "anthropic_compatible", "model_name": "MiniMax-M2.7",
            "api_base": "https://example.test/anthropic", "api_key": "only-private-secret",
            "model_context_window_tokens": 96000, **values}


def add(host, **values):
    profile_id = str(uuid4())
    result = execute_model_profile_operation(host, "add", {"profile_id": profile_id, "profile": profile(**values)})
    return profile_id, result


def test_private_profile_roundtrip_and_default(tmp_path):
    host = Host(tmp_path)
    key, result = add(host)
    assert "secret" not in json.dumps(result)
    assert result["selected"] == "default"
    assert selected_model_config(host) is host.config
    result = execute_model_profile_operation(host, "select", {"profile_id": key})
    assert result["selected"] == key
    cfg = selected_model_config(host)
    assert cfg.model_name == "MiniMax-M2.7" and cfg.api_key_env == ""
    assert cfg.api_key == "only-private-secret" and cfg.model_context_window_explicit
    assert cfg.model_context_window_tokens == 96000
    assert stat.S_IMODE(model_profiles_path(host.home_paths).stat().st_mode) == 0o600
    assert stat.S_IMODE(model_profiles_path(host.home_paths).parent.stat().st_mode) == 0o700
    assert not list(model_profiles_path(host.home_paths).parent.glob(".models-*"))


@pytest.mark.parametrize("values", [
    {"model_backend": "auth"}, {"model_name": ""}, {"api_key": ""},
    {"api_base": "file:///etc/passwd"}, {"api_base": "https://name:secret@example.test"},
    {"api_base": "https://example.test?key=secret"}, {"api_base": "https://example.test:bad"},
    {"api_key": "secret\ninjection"}, {"model_context_window_tokens": True},
    {"model_context_window_tokens": "-1"}, {"model_context_window_tokens": "12.5"},
    {"model_context_window_tokens": "一万"}, {"model_context_window_tokens": "2048"},
])
def test_invalid_profiles_rejected_without_secret(values):
    with pytest.raises(ValueError) as caught:
        validate_model_profile(profile(**values))
    assert "secret" not in str(caught.value)


def test_preserve_model_case_and_normalize_window():
    row = validate_model_profile(profile(model_context_window_tokens="200000", api_base="http://localhost:4000/v1/"))
    assert row["model_name"] == "MiniMax-M2.7"
    assert row["model_context_window_tokens"] == 200000
    assert row["api_base"] == "http://localhost:4000/v1"


def test_saving_same_id_is_idempotent_but_never_overwrites(tmp_path):
    host = Host(tmp_path)
    key, _ = add(host)
    execute_model_profile_operation(host, "add", {"profile_id": key, "profile": profile()})
    with pytest.raises(ValueError):
        execute_model_profile_operation(host, "add", {"profile_id": key, "profile": profile(model_name="different")})
    assert len(read_model_profiles(model_profiles_path(host.home_paths))["profiles"]) == 1


def test_concurrent_saves_and_owner_isolation(tmp_path):
    alice, bob = Host(tmp_path), Host(tmp_path, "bob")
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda number: add(alice, model_name=f"model-{number}")[0], range(12)))
    assert len(read_model_profiles(model_profiles_path(alice.home_paths))["profiles"]) == 12
    assert read_model_profiles(model_profiles_path(bob.home_paths))["profiles"] == {}
    with pytest.raises(ValueError):
        execute_model_profile_operation(bob, "select", {"profile_id": ids[0]})


def test_corrupt_file_not_overwritten(tmp_path):
    host = Host(tmp_path)
    add(host)
    path = model_profiles_path(host.home_paths)
    path.write_text("broken", encoding="utf-8")
    with pytest.raises(ValueError):
        add(host)
    assert path.read_text() == "broken"


@pytest.mark.parametrize("change", [{"schema": "other"}, {"selected": "missing"}, {"profiles": []}])
def test_invalid_persisted_structure_is_rejected(tmp_path, change):
    host = Host(tmp_path)
    add(host)
    path = model_profiles_path(host.home_paths)
    data = json.loads(path.read_text())
    data.update(change)
    path.write_text(json.dumps(data))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        add(host)
    assert path.read_bytes() == before


def test_unknown_private_fields_never_reach_public_list(tmp_path):
    host = Host(tmp_path)
    key, _ = add(host)
    path = model_profiles_path(host.home_paths)
    data = json.loads(path.read_text())
    data["profiles"][key]["extra_secret"] = "must-stay-private"
    path.write_text(json.dumps(data))
    before = path.read_bytes()
    result = execute_model_profile_operation(host, "list", {})
    assert "must-stay-private" not in json.dumps(result)
    assert path.read_bytes() == before


def test_model_scope_freezes_and_restores_across_concurrent_selection(tmp_path):
    host = Host(tmp_path)
    old_config, old_backend = host.config, host.backend
    first, _ = add(host)
    second, _ = add(host, model_name="another-model", model_context_window_tokens=128000)
    execute_model_profile_operation(host, "select", {"profile_id": first})
    with selected_model_scope(host):
        backend = host.backend
        assert host.config.model_name == "MiniMax-M2.7"
        assert host.prompts.config is host.config
        observations = []

        def other_thread():
            observations.append(host.config is old_config)
            execute_model_profile_operation(host, "select", {"profile_id": second})
            with selected_model_scope(host):
                observations.append(host.config.model_name == "another-model")

        thread = threading.Thread(target=other_thread)
        thread.start()
        thread.join()
        assert observations == [True, True]
        assert host.config.model_name == "MiniMax-M2.7" and host.backend is backend
        with selected_model_scope(host):
            assert host.backend is backend
    assert host.config is old_config and host.backend is old_backend
    with pytest.raises(RuntimeError), selected_model_scope(host):
        assert host.config.model_name == "another-model"
        raise RuntimeError("cancelled")
    assert host.config is old_config


def test_child_inherits_exact_profile_after_selection_changes(tmp_path):
    host = Host(tmp_path)
    key, _ = add(host)
    execute_model_profile_operation(host, "select", {"profile_id": key})
    attrs = {"host_model_profile.v1": {"profile_id": "spoofed"}}
    with selected_model_scope(host):
        inherit_model_profile(attrs, host)
    assert attrs == {"host_model_profile.v1": {"profile_id": key}}
    execute_model_profile_operation(host, "select", {"profile_id": "default"})
    recovered = inherited_model_config(host, SimpleNamespace(attributes=attrs))
    assert recovered.model_name == "MiniMax-M2.7" and recovered.model_context_window_tokens == 96000
    child = Host(tmp_path)
    child.config = recovered
    with selected_model_scope(child, inherited=True):
        assert child.config is recovered
    inherit_model_profile(attrs, host)
    assert attrs == {}


def test_task_overlay_preserves_model_reference_and_full_selected_profile(tmp_path):
    host = Host(tmp_path)
    key, _ = add(host)
    execute_model_profile_operation(host, "select", {"profile_id": key})
    config = selected_model_config(host)
    overlay = tmp_path / "task-runtime.yaml"
    overlay.write_text("tool_timeout_seconds: 31\nmodel_context_window_tokens: 128000\napi_base: https://stale.test\n")
    task = SimpleNamespace(runtime_identity=SimpleNamespace(config_overlay_ref=str(overlay), config_scope="task"))
    child = Host(tmp_path)
    child.config = apply_task_runtime_config_overlay(config, task, workspace_root=tmp_path)
    assert child.config.model_context_window_tokens == 96000
    assert child.config.api_base == "https://example.test/anthropic"
    attrs = {}
    inherit_model_profile(attrs, child)
    assert attrs == {"host_model_profile.v1": {"profile_id": key}}
    execute_model_profile_operation(host, "select", {"profile_id": "default"})
    grandchild = inherited_model_config(host, SimpleNamespace(attributes=attrs))
    assert grandchild.model_context_window_tokens == 96000


def test_explicit_window_does_not_call_metadata():
    def forbidden():
        raise AssertionError("explicit window must not probe")

    host = SimpleNamespace(config=AgentConfig(model_context_window_tokens=96000, model_context_window_explicit=True),
                           backend=SimpleNamespace(provider_context_window_tokens=forbidden))
    assert resolve_model_context_window_tokens(host) == 96000


@pytest.mark.parametrize("backend_fails", [False, True])
def test_selected_model_survives_real_transport_guard_thread(tmp_path, monkeypatch, backend_fails):
    from agent_py_agent.agent.agent_core import tool_model_generation as generation
    from agent_py_agent.agent.settings import model_scope

    host = Host(tmp_path)
    original = host.config
    key, _ = add(host, model_name="chosen-model")
    execute_model_profile_operation(host, "select", {"profile_id": key})
    monkeypatch.setattr(model_scope, "_profile_backend", lambda agent, config: SimpleNamespace(name=config.model_name))
    monkeypatch.setattr(generation, "_effective_model_request_timeout_seconds", lambda *args: 1.0)
    observed = []

    def generate(backend, prompt, state):
        observed.append((backend.name, host.config.model_name, host.prompts.config.model_name, threading.get_ident()))
        if backend_fails:
            raise RuntimeError("provider-test-failure")
        return SimpleNamespace(text="done")

    monkeypatch.setattr(generation, "_invoke_backend_generate", generate)
    request = SimpleNamespace(agent=host, prompt="ordinary task")
    with selected_model_scope(host):
        if backend_fails:
            with pytest.raises(RuntimeError, match="provider-test-failure"):
                generation._generate_with_wall_timeout(request, SimpleNamespace())
        else:
            assert generation._generate_with_wall_timeout(request, SimpleNamespace()).text == "done"
        assert host.config.model_name == "chosen-model"
    assert len(observed) == 1 and observed[0][:3] == ("chosen-model",) * 3
    assert observed[0][3] != threading.get_ident()
    assert host.config is original
