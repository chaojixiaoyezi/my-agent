# LLM: 仅使用临时假目录与凭据；v6 小窗口合法，溢出仍拒绝，不能以降低校验换取通过。
# 模块用途: 验证模型秘密、owner 隔离、原子写入与执行快照。
"""用户模型配置的秘密、隔离、原子写入和执行快照定向验证。"""

import asyncio
import json
import stat
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from agent_py_agent.agent.agent_core.model.context_window import resolve_model_context_window_tokens
from agent_py_agent.agent.backends import get_backend
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts import model_control_cards as cards
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.settings.config_io import parse_simple_yaml_text
from agent_py_agent.agent.settings.model_credentials import (
    import_legacy_owner_configuration,
    provider_api_key,
)
from agent_py_agent.agent.settings.model_profiles import (
    _save_profiles,
    execute_model_profile_operation,
    inherit_model_profile,
    inherited_model_config,
    model_profiles_path,
    public_model_profiles,
    read_model_profiles,
    selected_model_config,
    selected_model_config_for_resources,
    validate_model_profile,
)
from agent_py_agent.agent.settings.model_provider_schema import (
    ModelProfileError,
    resolved_model,
    validate_model,
    validate_provider,
)
from agent_py_agent.agent.settings.model_scope import ModelScopedAttribute, selected_model_scope
from agent_py_agent.agent.settings.services.runtime_config_task import (
    apply_task_runtime_config_overlay,
)
from agent_py_agent.agent.settings.thread_model_selection import (
    execute_local_model_operation,
    thread_model_config,
)
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.cli.chat_parts import tui_provider_menu as tui


class Host:
    config = ModelScopedAttribute("config")
    backend = ModelScopedAttribute("backend")
    prompts = ModelScopedAttribute("prompts")

    def __init__(self, path, owner="alice"):
        self.home_paths = SimpleNamespace(root=path.parent, config_dir=path, owner_provider="local", owner_kind="user", owner_id=owner)
        self.config = AgentConfig(model_backend="echo", model_name="deployment-model", api_key="deployment-secret")
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
    unconfigured = selected_model_config(host)
    assert (unconfigured.model_backend, unconfigured.model_name, unconfigured.api_key) == ("", "", "")
    assert host.config.model_name == "deployment-model"
    result = execute_model_profile_operation(host, "set_default", {"profile_id": key})
    assert result["selected"] == key
    cfg = selected_model_config(host)
    assert cfg.model_name == "MiniMax-M2.7" and cfg.api_key_env == ""
    assert cfg.api_key == "only-private-secret"
    assert cfg.model_context_window_tokens == 96000
    assert stat.S_IMODE(model_profiles_path(host.home_paths).stat().st_mode) == 0o600
    assert stat.S_IMODE(model_profiles_path(host.home_paths).parent.stat().st_mode) == 0o700
    assert not list(model_profiles_path(host.home_paths).parent.glob(".models-*"))


@pytest.mark.parametrize("injected", [False, True])
def test_empty_catalog_never_falls_back_but_preserves_explicit_injected_backend(tmp_path, injected):
    from agent_py_agent.agent.backends import get_backend
    from agent_py_agent.agent.backends.errors import ModelNotConfiguredError
    from agent_py_agent.agent.core import SimpleAgent

    agent = SimpleAgent(AgentConfig(my_agent_home=str(tmp_path / "home"), model_backend="echo"), tmp_path)
    backend = get_backend("echo", agent.config)
    if injected:
        agent.backend = backend
    with selected_model_scope(agent):
        assert agent.config.model_backend == "" and agent.config.model_name == ""
        if injected:
            assert agent.backend is backend and agent.backend.generate("offline").text
        else:
            with pytest.raises(ModelNotConfiguredError):
                agent.backend.generate("must not fall back")
    if injected:
        key, _ = add(agent)
        execute_model_profile_operation(agent, "set_default", {"profile_id": key})
        with selected_model_scope(agent):
            assert agent.config.model_name == "MiniMax-M2.7" and agent.backend is not backend


@pytest.mark.parametrize("injected", [False, True])
def test_nested_unconfigured_scope_uses_raw_backend_not_active_profile(tmp_path, injected):
    from agent_py_agent.agent.backends import get_backend
    from agent_py_agent.agent.backends.base import UnconfiguredBackend
    from agent_py_agent.agent.backends.errors import ModelNotConfiguredError
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings.model_scope import (
        model_dependencies_scope,
        prepare_model_dependencies,
    )

    agent = SimpleAgent(AgentConfig(my_agent_home=str(tmp_path / "home")), tmp_path)
    original = agent.backend
    fake_backend = get_backend("echo", agent.config)
    first, _ = add(agent, model_name="model-A")
    second, _ = add(agent, model_name="model-B")
    execute_model_profile_operation(agent, "set_default", {"profile_id": first})
    with selected_model_scope(agent):
        first_backend = agent.backend
        assert first_backend is not original
        second_dependencies = prepare_model_dependencies(agent, selected_model_config(agent, profile_id=second))
        with model_dependencies_scope(agent, second_dependencies):
            second_backend = agent.backend
            assert second_backend is not first_backend and agent.config.model_name == "model-B"
            if injected:
                # 赋值只改变实例依赖，活动 B 作用域仍投影 B；不能把 B 当成这次注入。
                agent.backend = fake_backend
            assert agent.backend is second_backend
            assert agent.__dict__["backend"] is (fake_backend if injected else original)
            config = selected_model_config(agent, profile_id="default")
            assert config.model_backend == ""
            dependencies = prepare_model_dependencies(agent, config)
            with model_dependencies_scope(agent, dependencies):
                if injected:
                    assert agent.backend is fake_backend and agent.backend.generate("offline").text
                else:
                    assert isinstance(agent.backend, UnconfiguredBackend)
                    with pytest.raises(ModelNotConfiguredError):
                        agent.backend.generate("must not reuse model B")
            assert agent.backend is second_backend
        assert agent.backend is first_backend
    assert agent.backend is (fake_backend if injected else original)


# LLM: v6 允许 1..4095 真窗口，非法值测试改覆盖上溢，不删除原边界防线。
# 函数用途: 检查非法档案被拒绝且错误不回显秘密。
@pytest.mark.parametrize("values", [
    {"model_backend": "auth"}, {"model_name": ""}, {"api_key": ""},
    {"api_base": "file:///etc/passwd"}, {"api_base": "https://name:secret@example.test"},
    {"api_base": "https://example.test?key=secret"}, {"api_base": "https://example.test:bad"},
    {"api_key": "secret\ninjection"}, {"model_context_window_tokens": True},
    {"model_context_window_tokens": "-1"}, {"model_context_window_tokens": "12.5"},
    {"model_context_window_tokens": "一万"}, {"model_context_window_tokens": "2147483648"},
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


@pytest.mark.parametrize("value", [-1, "nan", "inf", True, 86401])
def test_invalid_queue_budget_rejected(value):
    with pytest.raises(ValueError, match="排队预算"):
        validate_model_profile(profile(model_queue_wait_seconds=value))


def test_queue_budget_follows_selected_profile(tmp_path):
    host = Host(tmp_path)
    key, _ = add(host, model_queue_wait_seconds=3600)
    cfg = selected_model_config(host, profile_id=key)
    assert cfg.model_queue_wait_seconds == 3600 and host.config.model_queue_wait_seconds == 0


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
    assert isinstance(create_items_from_params({"model": "model-B", "items": [{"goal": "A"}]}), str)
    items = create_items_from_params({"items": [{"goal": "A", "model": "model-B"}, {"goal": "B", "model": profile_id}]})
    assert isinstance(items, list)
    assert [item.params["model"] for item in items] == ["model-B", profile_id]
    props = build_create_subagents_model_spec().input_schema["properties"]
    assert "model" not in props
    assert props["items"]["items"]["properties"]["model"]["type"] == "string"


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


@pytest.mark.parametrize("with_overlay", [False, True])
def test_task_config_projection_matches_worker_without_log_or_file_side_effects(tmp_path, monkeypatch, with_overlay):
    from copy import deepcopy
    from dataclasses import asdict

    from agent_py_agent.agent.settings.services import runtime_config_task

    host = Host(tmp_path)
    key, _ = add(host)
    config = selected_model_config(host, profile_id=key)
    overlay = tmp_path / "task-runtime.yaml"
    overlay.write_text("log_level: debug\nrunner_timeout_seconds: 31\nmax_tokens: 1234\n", encoding="utf-8")
    task = SimpleNamespace(runtime_identity=SimpleNamespace(
        config_overlay_ref=str(overlay) if with_overlay else "", config_scope="task"))
    original = deepcopy(asdict(config))
    before = {str(path.relative_to(tmp_path)): path.read_bytes() if path.is_file() else None for path in tmp_path.rglob("*")}
    applied = []
    monkeypatch.setattr(runtime_config_task, "apply_log_level", applied.append)

    projected = runtime_config_task.project_task_runtime_config_overlay(config, task, workspace_root=tmp_path)
    assert not applied
    actual = runtime_config_task.apply_task_runtime_config_overlay(config, task, workspace_root=tmp_path)

    assert asdict(projected) == asdict(actual)
    assert asdict(config) == original
    assert len(applied) == int(with_overlay)
    assert before == {str(path.relative_to(tmp_path)): path.read_bytes() if path.is_file() else None for path in tmp_path.rglob("*")}
    if with_overlay:
        assert projected is not config
        assert projected.runner_timeout_seconds == "31"
        assert projected.max_tokens == config.max_tokens  # 原模型配置优先级仍高于 task overlay。
        assert applied[0] is actual
    else:
        assert projected is actual is config


def test_explicit_window_does_not_call_metadata():
    def forbidden():
        raise AssertionError("explicit window must not probe")

    host = SimpleNamespace(config=AgentConfig(model_context_window_tokens=96000),
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
    from agent_py_agent.agent.contracts.model_call_ledger import (
        ModelCallLedger,
        ModelCallStartedParams,
    )

    ledger = ModelCallLedger()
    ledger.started(ModelCallStartedParams(call_id="selected-model-guard", backend="fixture", model="chosen-model",
        input_tokens=0, output_tokens_estimate=0))
    state = SimpleNamespace(ledger=ledger, call_id="selected-model-guard")
    with selected_model_scope(host):
        if backend_fails:
            with pytest.raises(RuntimeError, match="provider-test-failure"):
                generation._generate_with_wall_timeout(request, state)
        else:
            assert generation._generate_with_wall_timeout(request, state).text == "done"
        assert host.config.model_name == "chosen-model"
    assert len(observed) == 1 and observed[0][:3] == ("chosen-model",) * 3
    assert observed[0][3] != threading.get_ident()
    assert host.config is original


# LLM: 策展是后台消费者，必须走 owner 选中的 profile，而不是部署占位默认（echo/gpt-4o-mini）；
# 这条回归防止再次把占位模型当成真实模型、让抽取永远过不了 schema。
# 函数用途: 验证策展后端按 owner 选择解析 provider/model，未选择时不得回落部署连接。
def test_curator_backend_follows_owner_selected_profile(tmp_path):
    from agent_py_agent.agent.core import _build_memory_curator_backend
    from agent_py_agent.agent.memory_store.curator_models import MemoryCuratorConfig

    host = Host(tmp_path)
    default_off = AgentConfig(model_backend="echo", model_name="gpt-4o-mini")
    off_backend, off_provider, off_model = _build_memory_curator_backend(
        host, default_off, MemoryCuratorConfig.from_agent_config(default_off)
    )
    assert (off_provider, off_model) == ("", "")

    placeholder = AgentConfig(
        model_backend="echo",
        model_name="gpt-4o-mini",
        memory_curator_model_profile="selected",
    )
    curator_config = MemoryCuratorConfig.from_agent_config(placeholder)

    # 未选目录模型就是未配置，部署 echo 也不能充当正式目录旁路。
    fallback_backend, provider, model = _build_memory_curator_backend(host, placeholder, curator_config)
    assert (provider, model) == ("", "")
    from agent_py_agent.agent.backends.errors import ModelNotConfiguredError

    with pytest.raises(ModelNotConfiguredError):
        fallback_backend.generate("must not use deployment fallback")

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


# LLM: 不初始化真实宿主或读取用户环境，只构造本次临时目录的完整管理员身份。
# 函数用途: 为正式保存、导入与会话选择提供规范资源。
def _host(tmp_path):
    home = replace(home_paths(tmp_path / "home"), owner_provider="local", owner_kind="main", owner_id="main")
    return SimpleNamespace(home_paths=home, config=AgentConfig(),
                           conversation_store=ConversationStore(home.owner_home_dir))


# LLM: 直接建立合法的测试目录输入，不经被测编辑入口；所有后续操作仍读取原目录。
# 函数用途: 保存一个保留关闭开关和未知窗口的模型及可信服务商。
def _seed(host, *, env=False):
    path = model_profiles_path(host.home_paths)
    data = read_model_profiles(path)
    key = str(uuid4())
    provider = validate_provider({"display_name": "原连接", "api_base": "https://example.invalid/v1",
                                  "api_key": "" if env else "inline-fixture",
                                  **({"api_key_ref": "env:E905_R1_KEY"} if env else {})})
    data["providers"]["p"] = provider
    data["profiles"][key] = validate_model({"provider_id": "p", "model_backend": "openai_compatible",
        "model_name": "fixture", "model_context_window_tokens": None, "reasoning_update_items": "off"})
    path.parent.mkdir(parents=True, exist_ok=True)
    _save_profiles(path, data)
    return key


# LLM: 只返回公开表单字段，不把环境引用或内联秘密回送客户端。
# 函数用途: 构造旧五字段飞书表单的一次普通改名提交。
def _form():
    return {"model_backend": "openai_compatible", "api_base": "https://example.invalid/v1",
            "model_name": "renamed", "model_context_window_tokens": "", "api_key": ""}


# LLM: 空串表示用户未声明容量；列表仍属非法正式输入，不能为迁移放宽在线校验。
# 函数用途: 验证正式模型窗口空值往返与非法结构拒绝。
def test_nullable_window_form_value():
    row = {"provider_id": "p", "model_backend": "openai_compatible", "model_name": "m"}
    assert validate_model({**row, "model_context_window_tokens": ""})["model_context_window_tokens"] is None
    with pytest.raises(ModelProfileError):
        validate_model({**row, "model_context_window_tokens": []})


# LLM: 未出现的字段保留，明确提交自动则清除关闭；不能把缺键当作重置请求。
# 函数用途: 验证服务端模型编辑的存在性感知合并。
def test_save_model_preserves_missing_v6_fields(tmp_path):
    host = _host(tmp_path)
    key = _seed(host)
    execute_model_profile_operation(host, "save_model", {"profile_id": key, "editing": True,
                                                       "profile": {"model_name": "new-name"}})
    model = read_model_profiles(model_profiles_path(host.home_paths))["profiles"][key]
    assert model["reasoning_update_items"] == "off" and model["model_context_window_tokens"] is None
    execute_model_profile_operation(host, "save_model", {"profile_id": key, "editing": True,
                                                       "profile": {"reasoning_update_items": "auto"}})
    assert "reasoning_update_items" not in read_model_profiles(model_profiles_path(host.home_paths))["profiles"][key]


# LLM: 对话替身驱动真实编辑函数，保存仍走原服务端操作；分别核对预填和提交，防止只修一端。
# 函数用途: 验证终端编辑未知窗口和关闭开关后原目录不漂移。
def test_tui_edit_model_nullable_and_off(tmp_path, monkeypatch):
    host = _host(tmp_path)
    key = _seed(host)
    row = {"id": key, **read_model_profiles(model_profiles_path(host.home_paths))["profiles"][key]}
    fields = []
    original_field = tui._field

    # LLM: 保留真实控件的构造与渲染，只登记控件供交互替身读取。
    # 函数用途: 收集真实编辑框以检查窗口预填值。
    def field(*args, **kwargs):
        control = original_field(*args, **kwargs)
        fields.append(control)
        return control

    # LLM: 只替换用户按保存的交互，读取真实控件值，不替换模型字段生成。
    # 函数用途: 检查窗口预填为空并提交保存动作。
    async def dialog(app, title, body, actions, **kwargs):
        assert fields[1].text == ""
        return True

    # LLM: 复用原目录事务，不把被测载荷伪造成成功回执。
    # 函数用途: 核对关闭开关已提交并保存真实表单请求。
    async def request(app, agent, session, operation, payload):
        assert payload["profile"]["reasoning_update_items"] == "off"
        return execute_model_profile_operation(agent, operation, payload)

    monkeypatch.setattr(tui, "_field", field)
    monkeypatch.setattr(tui, "_dialog", dialog)
    monkeypatch.setattr(tui, "_request", request)
    assert asyncio.run(tui._edit_model(None, host, "s", "p", row))
    model = read_model_profiles(model_profiles_path(host.home_paths))["profiles"][key]
    assert model["reasoning_update_items"] == "off" and model["model_context_window_tokens"] is None


# LLM: 密钥不预填，未知窗口不能格式化为字符串空值标记。
# 函数用途: 检查飞书表单窗口的可见默认值。
def test_feishu_nullable_prefill():
    card = cards.model_configuration_card("fixture-ref", "测试范围", {"model_context_window_tokens": None})
    form = next(item for item in card["elements"] if item["tag"] == "form")
    window = next(item for item in form["elements"] if item.get("name") == "model_context_window_tokens")
    assert window["default_value"] == ""


# LLM: 父卡生命周期不是本回归目标，替换该前置检查；目录代次、向量保护、保存及凭据解析不替换。
# 函数用途: 验证飞书旧字段表单改名保留可信环境来源和关闭开关。
def test_feishu_edit_env_preserves_credential_and_v6(tmp_path, monkeypatch):
    from agent_py_agent.agent.gateway_parts import model_control_dispatch

    host = _host(tmp_path)
    key = _seed(host, env=True)
    data = read_model_profiles(model_profiles_path(host.home_paths))
    row = {"profile_id": key, "thread_id": "", "catalog_revisions": [data["catalog_generation"]]}
    monkeypatch.setattr(model_control_dispatch, "model_form_parent_error", lambda *args, **kwargs: None)
    result = cards._save_model_form(host, row, _form())
    assert result["ok"] is True
    saved = read_model_profiles(model_profiles_path(host.home_paths))
    assert saved["profiles"][key]["reasoning_update_items"] == "off"
    assert saved["profiles"][key]["model_context_window_tokens"] is None
    assert saved["providers"]["p"]["api_key_ref"] == "env:E905_R1_KEY"
    monkeypatch.setenv("E905_R1_KEY", "late-fixture")
    assert resolved_model(saved, key)["api_key"] == "late-fixture"


# LLM: 任何普通表单均不能自行创建环境引用；只有服务端锁内既有记录可参与保留。
# 函数用途: 检查快捷模型新增和编辑拒绝客户端注入环境名。
@pytest.mark.parametrize("operation", ["add", "save_profile", "save_provider"])
def test_forms_reject_new_env_reference(tmp_path, operation):
    host = _host(tmp_path)
    key = _seed(host)
    value = {**_form(), "api_key": "", "api_key_ref": "env:UNTRUSTED_FIXTURE"}
    payload = {"profile_id": key, "provider_id": "p", "editing": True,
               "profile": value, "provider": value}
    before = model_profiles_path(host.home_paths).read_bytes()
    with pytest.raises(ModelProfileError):
        execute_model_profile_operation(host, operation, payload)
    assert model_profiles_path(host.home_paths).read_bytes() == before


# LLM: 端点包含地址和传输，变更后旧引用不得跟随；失败必须保留整个原目录。
# 函数用途: 验证服务商改地址或传输时不能留空继承凭据。
@pytest.mark.parametrize("change", [{"api_base": "https://other.invalid/v1"}, {"transport": "offline"}])
def test_provider_endpoint_change_does_not_retain_env(tmp_path, change):
    host = _host(tmp_path)
    _seed(host, env=True)
    before = model_profiles_path(host.home_paths).read_bytes()
    with pytest.raises(ModelProfileError):
        execute_model_profile_operation(host, "save_provider", {"provider_id": "p", "editing": True,
                                                               "provider": {"api_key": "", **change}})
    assert model_profiles_path(host.home_paths).read_bytes() == before


# LLM: 同端点留空只保留服务端原来源，环境暂未就绪不阻止名称编辑。
# 函数用途: 验证服务商改名保留环境引用及新密钥明确替换旧来源。
def test_provider_edit_env_empty_key_then_replace(tmp_path):
    host = _host(tmp_path)
    _seed(host, env=True)
    execute_model_profile_operation(host, "save_provider", {"provider_id": "p", "editing": True,
        "provider": {"display_name": "新名称", "api_key": ""}})
    provider = read_model_profiles(model_profiles_path(host.home_paths))["providers"]["p"]
    assert provider["api_key_ref"] == "env:E905_R1_KEY" and provider["display_name"] == "新名称"
    execute_model_profile_operation(host, "save_provider", {"provider_id": "p", "editing": True,
        "provider": {"api_key": "replacement-fixture"}})
    provider = read_model_profiles(model_profiles_path(host.home_paths))["providers"]["p"]
    assert provider["api_key"] == "replacement-fixture" and "api_key_ref" not in provider


# LLM: 真实解析器的裸字段会生成空列表，只允许唯一离线解码器归一；数值零不得误判为空。
# 函数用途: 验证旧简化配置经正式导入后空值与零温度语义正确。
@pytest.mark.parametrize("temperature", [None, [], "", 0])
@pytest.mark.parametrize("key", [None, [], ""])
def test_legacy_empty_fields_import(tmp_path, monkeypatch, temperature, key):
    host = _host(tmp_path)
    raw = parse_simple_yaml_text("model_backend: openai_compatible\napi_base: https://example.invalid/v1\n"
        "model_name: imported\napi_key:\napi_key_env: E905_R1_KEY\ntemperature:\nmodel_context_window_tokens:\n")
    assert raw["api_key"] == [] and raw["temperature"] == []
    raw.update(api_key=key, temperature=temperature)
    monkeypatch.setenv("E905_R1_KEY", "import-fixture")
    report = import_legacy_owner_configuration(host.home_paths, agent_values=raw, capability_values={},
        environ={"E905_R1_KEY": "import-fixture"}, expected_generation=None)
    config = selected_model_config_for_resources(host.config, host.home_paths, profile_id=report["profile_id"])
    assert config.api_key == "import-fixture" and config.model_context_window_tokens is None
    assert config.temperature == ("0.0" if temperature == 0 else None)


# LLM: 暂空环境不应擦掉合法来源；注值后不用重迁移即可解析，历史静态值仅为后备。
# 函数用途: 验证环境迟到及历史内联备份的优先级。
@pytest.mark.parametrize("inline", ["", "backup-fixture"])
def test_empty_env_migration_later_resolves(tmp_path, monkeypatch, inline):
    host = _host(tmp_path)
    monkeypatch.delenv("E905_R1_KEY", raising=False)
    report = import_legacy_owner_configuration(host.home_paths, agent_values={"model_backend": "openai_compatible",
        "api_base": "https://example.invalid/v1", "model_name": "imported", "api_key": inline,
        "api_key_env": "E905_R1_KEY"}, capability_values={}, environ={}, expected_generation=None)
    data = read_model_profiles(model_profiles_path(host.home_paths))
    provider = next(iter(data["providers"].values()))
    assert provider["api_key_ref"] == "env:E905_R1_KEY"
    assert provider_api_key(provider, environ={}) == inline
    if not inline:
        with pytest.raises(ModelProfileError) as error:
            resolved_model(data, report["profile_id"])
        assert error.value.reason == "credential_missing"
    before = model_profiles_path(host.home_paths).read_bytes()
    monkeypatch.setenv("E905_R1_KEY", "late-fixture")
    assert resolved_model(data, report["profile_id"])["api_key"] == "late-fixture"
    assert model_profiles_path(host.home_paths).read_bytes() == before


# LLM: 离线协议不要求凭据，但停用与协议错配仍须关闭；不以模型名或环境猜测。
# 函数用途: 覆盖导入、公开列表、真实会话选择、解析和后端生成的完整离线链。
def test_echo_import_list_select_resolve(tmp_path):
    host = _host(tmp_path)
    report = import_legacy_owner_configuration(host.home_paths, agent_values={"model_backend": "echo"},
        capability_values={}, environ={}, expected_generation=None)
    key = report["profile_id"]
    catalog = read_model_profiles(model_profiles_path(host.home_paths))
    assert next(row for row in public_model_profiles(catalog, host.config)["profiles"] if row["id"] == key)["available"]
    listing = execute_local_model_operation(host, "echo-session", "list", {})
    assert listing["selection_available"] is True
    selected = execute_local_model_operation(host, "echo-session", "select", {"profile_id": key})
    assert selected["selected"] == key and selected["selection_available"] is True
    config = thread_model_config(host, selected["thread_id"])
    assert config.model_backend == "echo"
    assert get_backend(config.model_backend, config).generate("offline-fixture").text
    provider_id = catalog["profiles"][key]["provider_id"]
    catalog["providers"][provider_id]["enabled"] = False
    assert not public_model_profiles(catalog, host.config)["profiles"][0]["available"]
    with pytest.raises(ModelProfileError):
        resolved_model(catalog, key)


# LLM: 有规范目录的正式入口不得把内存 echo 当未选模型的旁路；无目录显式测试桩单列。
# 函数用途: 检查纯内存兼容只存在于明确无目录的测试调用。
def test_echo_memory_stub_is_not_formal_catalog_bypass(tmp_path):
    host = _host(tmp_path)
    config = AgentConfig(model_backend="echo")
    assert selected_model_config_for_resources(config, None) is config
    formal = selected_model_config_for_resources(config, host.home_paths)
    assert formal.model_backend != "echo"
    assert not any(row.get("available") for row in execute_model_profile_operation(
        SimpleNamespace(home_paths=host.home_paths, config=config), "list", {})["profiles"])


# LLM: 快捷编辑必须从锁内原记录补未出现的字段，而不能依赖客户端回传整个旧模型。
# 函数用途: 检查快捷改名保留关闭开关、未知窗口、环境来源和原请求头。
def test_flat_edit_presence_and_endpoint_replacement(tmp_path):
    host = _host(tmp_path)
    key = _seed(host, env=True)
    execute_model_profile_operation(host, "save_provider", {"provider_id": "p", "editing": True,
        "provider": {"custom_headers": {"X-Fixture": "private-header"}, "session_header": "x-session"}})
    execute_model_profile_operation(host, "save_profile", {"profile_id": key, "editing": True,
        "profile": {"model_name": "new-name", "api_key": ""}})
    data = read_model_profiles(model_profiles_path(host.home_paths))
    assert data["profiles"][key]["reasoning_update_items"] == "off"
    assert data["profiles"][key]["model_context_window_tokens"] is None
    assert data["providers"]["p"]["api_key_ref"] == "env:E905_R1_KEY"
    assert data["providers"]["p"]["custom_headers"] == {"X-Fixture": "private-header"}
    before = model_profiles_path(host.home_paths).read_bytes()
    with pytest.raises(ModelProfileError):
        execute_model_profile_operation(host, "save_profile", {"profile_id": key, "editing": True,
            "profile": {"api_base": "https://other.invalid/v1", "api_key": ""}})
    assert model_profiles_path(host.home_paths).read_bytes() == before
    execute_model_profile_operation(host, "save_profile", {"profile_id": key, "editing": True,
        "profile": {"api_base": "https://other.invalid/v1", "api_key": "new-fixture"}})
    provider = read_model_profiles(model_profiles_path(host.home_paths))["providers"]["p"]
    assert "api_key_ref" not in provider and provider["api_key"] == "new-fixture"
    assert provider["custom_headers"] == {} and provider["session_header"] == ""


# LLM: 明确清除凭据与留空保留是两种不同动作；历史后备不应让清除失效。
# 函数用途: 检查引用和静态后备同时存在时，普通编辑保留而明确清除全部移除。
def test_env_inline_backup_roundtrip_and_clear(tmp_path):
    host = _host(tmp_path)
    key = _seed(host, env=True)
    path = model_profiles_path(host.home_paths)
    data = read_model_profiles(path)
    data["providers"]["p"]["api_key"] = "backup-fixture"
    _save_profiles(path, data)
    execute_model_profile_operation(host, "save_provider", {"provider_id": "p", "editing": True,
        "provider": {"api_key": "", "display_name": "renamed"}})
    data = read_model_profiles(path)
    assert data["providers"]["p"]["api_key_ref"] == "env:E905_R1_KEY"
    assert provider_api_key(data["providers"]["p"], environ={}) == "backup-fixture"
    assert provider_api_key(data["providers"]["p"], environ={"E905_R1_KEY": "env-fixture"}) == "env-fixture"
    execute_model_profile_operation(host, "save_provider", {"provider_id": "p", "editing": True,
        "clear_key": True, "provider": {}})
    data = read_model_profiles(path)
    assert "api_key_ref" not in data["providers"]["p"] and data["providers"]["p"]["api_key"] == ""
    with pytest.raises(ModelProfileError) as error:
        resolved_model(data, key)
    assert error.value.reason == "credential_missing"


# LLM: 正式协议合法性必须与解析器一致，离线服务商不能伪装成网络模型的可用来源。
# 函数用途: 检查公开目录和实际解析同时拒绝传输与模型协议错配。
def test_echo_protocol_mismatch_unavailable(tmp_path):
    host = _host(tmp_path)
    key = _seed(host)
    data = read_model_profiles(model_profiles_path(host.home_paths))
    data["profiles"][key]["model_backend"] = "echo"
    assert not public_model_profiles(data, host.config)["profiles"][0]["available"]
    with pytest.raises(ModelProfileError) as error:
        resolved_model(data, key)
    assert error.value.reason == "capability_mismatch"
