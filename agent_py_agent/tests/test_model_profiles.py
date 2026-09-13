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
        self.home_paths = SimpleNamespace(root=path.parent, config_dir=path, owner_provider="local", owner_kind="user", owner_id=owner)
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
    result = execute_model_profile_operation(host, "set_default", {"profile_id": key})
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
        execute_model_profile_operation(bob, "set_default", {"profile_id": ids[0]})


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
    execute_model_profile_operation(host, "set_default", {"profile_id": first})
    with selected_model_scope(host):
        backend = host.backend
        assert host.config.model_name == "MiniMax-M2.7"
        assert host.prompts.config is host.config
        observations = []

        def other_thread():
            observations.append(host.config is old_config)
            execute_model_profile_operation(host, "set_default", {"profile_id": second})
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
    execute_model_profile_operation(host, "set_default", {"profile_id": key})
    attrs = {"host_model_profile.v1": {"profile_id": "spoofed"}}
    with selected_model_scope(host):
        inherit_model_profile(attrs, host)
    assert attrs == {"host_model_profile.v1": {"profile_id": key}}
    execute_model_profile_operation(host, "set_default", {"profile_id": "default"})
    recovered = inherited_model_config(host, SimpleNamespace(attributes=attrs))
    assert recovered.model_name == "MiniMax-M2.7" and recovered.model_context_window_tokens == 96000
    child = Host(tmp_path)
    child.config = recovered
    with selected_model_scope(child, inherited=True):
        assert child.config is recovered
    inherit_model_profile(attrs, host)
    assert attrs == {"host_model_profile.v1": {"profile_id": "default"}}


def test_child_can_select_saved_model_without_switching_parent(tmp_path):
    host = Host(tmp_path)
    a, _ = add(host, model_name="model-A")
    b, _ = add(host, model_name="model-B", model_backend="openai_compatible", model_context_window_tokens=262144)
    execute_model_profile_operation(host, "set_default", {"profile_id": a})
    attrs = {"host_model_profile.v1": {"profile_id": "spoofed"}}
    with selected_model_scope(host):
        inherit_model_profile(attrs, host, model="model-B")
        assert host.config.model_name == "model-A"
    assert attrs == {"host_model_profile.v1": {"profile_id": b}}
    assert read_model_profiles(model_profiles_path(host.home_paths))["selected"] == a
    child = Host(tmp_path)
    child.config = inherited_model_config(host, SimpleNamespace(attributes=attrs))
    assert child.config.model_name == "model-B" and child.config.model_context_window_tokens == 262144
    grandchild_attrs = {}
    inherit_model_profile(grandchild_attrs, child)
    assert grandchild_attrs == attrs
    execute_model_profile_operation(host, "set_default", {"profile_id": "default"})
    assert inherited_model_config(host, SimpleNamespace(attributes=grandchild_attrs)).model_name == "model-B"


def test_explicit_child_model_is_owner_scoped_and_unambiguous(tmp_path):
    from agent_py_agent.agent.settings.model_profiles import ModelProfileError

    host = Host(tmp_path, owner="alice")
    first, _ = add(host, model_name="same-name")
    add(host, model_name="same-name", model_context_window_tokens=100000)
    for selection in ("same-name", "missing", "", {"api_key": "not-accepted"}):
        with pytest.raises(ModelProfileError):
            inherit_model_profile({}, host, model=selection)
    attrs = {}
    inherit_model_profile(attrs, host, model=first)
    assert attrs == {"host_model_profile.v1": {"profile_id": first}}
    with pytest.raises(ModelProfileError):
        inherit_model_profile({}, Host(tmp_path, owner="bob"), model=first)


def test_root_batch_model_validation_has_no_partial_creation(tmp_path):
    from agent_py_agent.agent.agent_core.orchestration_tools import CreateSubagentsTool
    from agent_py_agent.tests.test_orchestration_create_subagents_items import _agent

    host = Host(tmp_path)
    profile_id, _ = add(host, model_name="model-B")
    agent = _agent()
    agent.home_paths = host.home_paths
    result = CreateSubagentsTool(agent).execute({"items": [
        {"goal": "检查输入", "model": profile_id}, {"goal": "检查输出", "model": "missing-model"},
    ]})
    assert result.ok is False and result.error_code == "TOOL_INVALID_ARGUMENTS"
    assert result.effect_outcome == "not_started"
    agent.subagents.create_run.assert_not_called()
    assert "model-B" in result.output and "only-private-secret" not in result.output


def test_root_and_nested_creation_bind_the_same_explicit_model(tmp_path):
    from agent_py_agent.agent.agent_core.hierarchy_tools import _hierarchy_child_specs
    from agent_py_agent.agent.agent_core.orchestration.create_payload import (
        create_items_from_params,
    )
    from agent_py_agent.agent.agent_core.orchestration.create_policy import create_task_attributes
    from agent_py_agent.agent.agent_core.orchestration.tool_specs import (
        build_create_subagents_model_spec,
    )
    from agent_py_agent.tests.test_orchestration_create_subagents_items import _agent

    host = Host(tmp_path)
    profile_id, _ = add(host, model_name="model-B")
    agent = _agent()
    agent.home_paths = host.home_paths
    raw = {"goal": "检查输入", "model": "model-B"}
    expected = {"profile_id": profile_id}
    assert create_task_attributes(raw, agent)["host_model_profile.v1"] == expected
    specs = _hierarchy_child_specs(agent, {"children": [raw]}, tool_name="create_subagents")
    assert specs[0].attributes["host_model_profile.v1"] == expected
    invalid = _hierarchy_child_specs(agent, {"children": [raw, {"goal": "检查输出", "model": "missing"}]}, tool_name="create_subagents")
    assert invalid.ok is False and invalid.effect_outcome == "not_started"
    items = create_items_from_params({"model": "model-B", "items": [{"goal": "A"}, {"goal": "B", "model": profile_id}]})
    assert [item.params["model"] for item in items] == ["model-B", profile_id]
    props = build_create_subagents_model_spec().input_schema["properties"]
    assert props["model"]["type"] == props["items"]["items"]["properties"]["model"]["type"] == "string"


def test_different_child_models_do_not_share_creation_guard_identity():
    from agent_py_agent.agent.agent_core.parameters import subagent_intent_identity

    payload = {"goal": "检查同一份材料"}
    original = subagent_intent_identity({}, payload)
    assert "model" not in json.loads(original)
    first = subagent_intent_identity({"model": "model-A"}, payload)
    second = subagent_intent_identity({"model": "model-B"}, payload)
    assert original != first != second
    assert subagent_intent_identity({"model": "model-A"}, {**payload, "model": "model-B"}) == second


def test_task_overlay_preserves_model_reference_and_full_selected_profile(tmp_path):
    host = Host(tmp_path)
    key, _ = add(host)
    execute_model_profile_operation(host, "set_default", {"profile_id": key})
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
    execute_model_profile_operation(host, "set_default", {"profile_id": "default"})
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
    execute_model_profile_operation(host, "set_default", {"profile_id": key})
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


# LLM: 策展是后台消费者，必须走 owner 选中的 profile，而不是部署占位默认（echo/gpt-4o-mini）；
# 这条回归防止再次把占位模型当成真实模型、让抽取永远过不了 schema。
# 函数用途: 验证策展后端按 owner 选择解析 provider/model，profile 缺失时回落基础 config。
def test_curator_backend_follows_owner_selected_profile(tmp_path):
    from agent_py_agent.agent.core import _build_memory_curator_backend
    from agent_py_agent.agent.memory_store.curator_models import MemoryCuratorConfig

    host = Host(tmp_path)
    placeholder = AgentConfig(model_backend="echo", model_name="gpt-4o-mini")
    curator_config = MemoryCuratorConfig.from_agent_config(placeholder)

    # 未选择 profile 时按 owner 自己的 config（部署默认）解析，不静默换模型
    fallback_backend, provider, model = _build_memory_curator_backend(host, placeholder, curator_config)
    assert (provider, model) == ("echo", "deployment-model")
    assert getattr(fallback_backend, "name", "") == "echo"

    key, _result = add(host)
    # 不依赖具体 operation 名（不同分支的模型菜单操作集不同）：直接把 store 的 selected 指到该 profile。
    store_path = model_profiles_path(host.home_paths)
    store_data = json.loads(store_path.read_text(encoding="utf-8"))
    store_data["selected"] = key
    store_path.write_text(json.dumps(store_data, ensure_ascii=False), encoding="utf-8")
    backend, provider, model = _build_memory_curator_backend(host, placeholder, curator_config)
    assert (provider, model) == ("anthropic_compatible", "MiniMax-M2.7")
    assert getattr(backend, "name", "") == "anthropic_compatible"
    assert getattr(backend, "api_key", "") == "only-private-secret"
