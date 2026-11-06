"""SW-W6：真实临时文件、配置副本及运行入口的同步回归。"""
from __future__ import annotations

import gc
import json
import os
import threading
import time
import weakref
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.audit.logger import AuditLogger, LogParams
from agent_py_agent.agent.auth.manager import AuthManager
from agent_py_agent.agent.capability import runtime_config_reload as capability
from agent_py_agent.agent.common.cache_freshness import CACHE_TRUST_AGE_SECONDS
from agent_py_agent.agent.settings import live_switches as live
from agent_py_agent.agent.settings.config import AgentConfig, load_config
from agent_py_agent.agent.settings.runtime_guard_config import runtime_guard_policy


@pytest.fixture(autouse=True)
def isolated_host(tmp_path, monkeypatch):
    home = tmp_path / "host"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MY_AGENT_HOME", str(home))
    monkeypatch.setattr(live, "_AUTH_PIPELINE_PRESENT", threading.Event())


def _write(path, text, *, recent=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if not recent:
        timestamp = time.time() - CACHE_TRUST_AGE_SECONDS - 1
        os.utime(path, (timestamp, timestamp))


def _config(tmp_path, text="enable_tools: true\n"):
    path = tmp_path / "agent.yaml"
    _write(path, text)
    return path, load_config(path)


def test_refresh_updates_all_registered_copies_but_not_host_numeric_or_secret_fields(tmp_path):
    path, config = _config(tmp_path)
    pooled = replace(config)
    live.register_live_config(config)
    live.register_live_config(pooled)
    before = (config.gateway_port, config.my_agent_home, config.api_key, config.memory_top_k)
    _write(path, "enable_tools: false\ngateway_port: 1234\nmy_agent_home: elsewhere\napi_key: ignored\nmemory_top_k: 42\n")
    assert "enable_tools" in live.refresh_live_switches(config)
    assert config.enable_tools is False and pooled.enable_tools is False
    assert (config.gateway_port, config.my_agent_home, config.api_key, config.memory_top_k) == before


def test_replace_override_and_unregistered_managed_owner_stay_isolated(tmp_path):
    path, base = _config(tmp_path)
    owner = replace(base, enable_tools=False)
    managed = replace(base, config_path="", enable_tools=True)
    for config in (base, owner, managed):
        live.register_live_config(config)
    for enabled in (False, True, False, True):
        _write(path, f"enable_tools: {str(enabled).lower()}\n")
        live.refresh_live_switches(path=path, force=True)
        assert base.enable_tools is enabled
        assert owner.enable_tools is False
        assert managed.enable_tools is True


def test_auth_validation_keeps_last_good_trio_and_deduplicates_warning(tmp_path):
    path, config = _config(tmp_path)
    live.note_auth_pipeline_present()
    live.register_live_config(config)
    _write(path, "auth_enabled: false\ngateway_per_user_owner_scoping: true\nadmin_channel_identity_enabled: false\nenable_tools: false\n")
    changed = live.refresh_live_switches(config)
    assert config.auth_enabled is True
    assert config.gateway_per_user_owner_scoping is True
    assert config.admin_channel_identity_enabled is True
    assert config.enable_tools is False and "auth_enabled" not in changed
    assert len(config.config_warnings) == 1
    live.refresh_live_switches(config, force=True)
    assert len(config.config_warnings) == 1


def test_enabling_auth_without_pipeline_is_rejected_then_retried_after_pipeline_arrives(tmp_path):
    path, config = _config(tmp_path, "auth_enabled: false\ngateway_per_user_owner_scoping: false\n")
    live.register_live_config(config)
    _write(path, "auth_enabled: true\ngateway_per_user_owner_scoping: true\nadmin_channel_identity_enabled: false\n")
    live.refresh_live_switches(config)
    assert config.auth_enabled is False and config.gateway_per_user_owner_scoping is False
    assert config.admin_channel_identity_enabled is True
    assert len(config.config_warnings) == 1
    live.note_auth_pipeline_present()
    assert "auth_enabled" in live.refresh_live_switches(config)
    assert config.auth_enabled is True and config.gateway_per_user_owner_scoping is True
    assert config.admin_channel_identity_enabled is False


def test_memory_mirror_normalizes_without_changing_local_numeric_overrides(tmp_path, monkeypatch):
    path, config = _config(tmp_path)
    live.register_live_config(config)
    config.memory_curator_interval_seconds = 731
    calls = []
    normalize = live.normalize_agent_memory_config

    def tracked(candidate):
        calls.append(candidate)
        return normalize(candidate)

    monkeypatch.setattr(live, "normalize_agent_memory_config", tracked)
    _write(path, "memory_hook_enabled: false\nmemory_curator_interval_seconds: 509\n")
    live.refresh_live_switches(config)
    assert config.memory_hook_enabled is False and calls
    assert config.memory_curator_interval_seconds == 731


def test_unchanged_file_costs_one_stat_and_no_parse(tmp_path, monkeypatch):
    path, config = _config(tmp_path)
    live.refresh_live_switches(config)
    calls = []
    real_stat = os.stat

    def stat(*args, **kwargs):
        calls.append(args[0])
        return real_stat(*args, **kwargs)

    def forbidden_parse(*args, **kwargs):
        pytest.fail("未变文件不能重新解析")

    monkeypatch.setattr(os, "stat", stat)
    monkeypatch.setattr(live, "load_config", forbidden_parse)
    assert live.refresh_live_switches(config) == frozenset()
    assert calls == [path]


def test_recent_file_is_applied_but_not_cached(tmp_path, monkeypatch):
    path, config = _config(tmp_path)
    live.register_live_config(config)
    _write(path, "enable_tools: false\n", recent=True)
    loads = []
    loader = live.load_config

    def tracked(*args, **kwargs):
        loads.append(args[0])
        return loader(*args, **kwargs)

    monkeypatch.setattr(live, "load_config", tracked)
    live.refresh_live_switches(config)
    live.refresh_live_switches(config)
    assert config.enable_tools is False
    assert len(loads) == 2


def test_runtime_guard_policy_edit_updates_registered_config_copies(tmp_path):
    path, config = _config(tmp_path)
    home = Path(os.environ["MY_AGENT_HOME"])
    config.my_agent_home = str(home)
    config.runtime_guard_policy = runtime_guard_policy(home_root=home)
    pooled = replace(config)
    live.register_live_config(config)
    live.register_live_config(pooled)
    live.refresh_live_switches(config)
    policy_path = home / "config" / "deployment_policy.json"
    _write(policy_path, json.dumps({"schema_version": "deployment-policy.v1", "revision": 1,
                                 "runtime_guards": {"terminal_block_enabled": True, "hard_failure_halt_enabled": True}}))
    changed = live.refresh_live_switches(path=path)
    for candidate in (config, pooled):
        assert candidate.runtime_guard_policy.terminal_block_enabled is True
        assert candidate.runtime_guard_policy.hard_failure_halt_enabled is True
    assert {"terminal_block_enabled", "hard_failure_halt_enabled"} <= changed


# LLM: 使用真实 Echo Agent 和隔离宿主文件；执行片入口必须在工具循环前同步配置与两个执行侧策略引用。
# 函数用途: 验证部署守卫双向切换后，Agent 和工具注册表都消费本轮的新策略对象。
@pytest.mark.parametrize("enabled", [False, True])
def test_run_refreshes_guard_policy_on_agent_and_tools(tmp_path, enabled):
    from agent_py_agent.agent.agent_core import runtime_mixin as runtime
    from agent_py_agent.agent.core import SimpleAgent

    _path, config = _config(tmp_path, "memory_curator_enabled: false\n")
    home = Path(os.environ["MY_AGENT_HOME"])
    config = replace(config, model_backend="echo", my_agent_home=str(home))
    policy_path = home / "config" / "deployment_policy.json"
    _write(policy_path, json.dumps({"schema_version": "deployment-policy.v1", "revision": 1,
                                  "runtime_guards": {"terminal_block_enabled": not enabled}}))
    agent = SimpleAgent(config, tmp_path)
    live.register_live_config(agent.config)
    live.refresh_live_switches(agent.config)
    previous = agent.config.runtime_guard_policy
    assert agent.runtime_guard_policy is previous
    assert agent.tools.runtime_guard_policy is previous
    assert previous.terminal_block_enabled is not enabled

    _write(policy_path, json.dumps({"schema_version": "deployment-policy.v1", "revision": 2,
                                  "runtime_guards": {"terminal_block_enabled": enabled}}))
    result = runtime._run_once_with_params(agent, "核对本轮部署守卫", runtime.RunParams(save=False))

    assert result.runtime_status == "ok"
    assert agent.config.runtime_guard_policy is not previous
    assert agent.runtime_guard_policy is agent.config.runtime_guard_policy
    assert agent.tools.runtime_guard_policy is agent.config.runtime_guard_policy
    assert agent.runtime_guard_policy.terminal_block_enabled is enabled
    assert agent.tools.runtime_guard_policy.terminal_block_enabled is enabled


def test_registration_does_not_retain_dead_config(tmp_path):
    path, config = _config(tmp_path)
    live.register_live_config(config)
    reference = weakref.ref(config)
    del config
    gc.collect()
    assert reference() is None
    assert live.refresh_live_switches(path=path) == frozenset()


def test_capability_switch_and_router_see_file_edit_without_restart(tmp_path):
    path = tmp_path / "capability.yaml"
    _write(path, "tool_program_enabled: true\n")
    router = SimpleNamespace(config=None)
    agent = SimpleNamespace(root=tmp_path, capability_config_path=path, capability_router=router)
    before = capability.capability_config_for_agent(agent)
    assert before.tool_program_enabled is True
    _write(path, "tool_program_enabled: false\n")
    current = capability.capability_config_for_agent(agent)
    assert current.tool_program_enabled is False
    assert agent._capability_config_runtime_snapshot.config is current
    assert router.config is current


def test_capability_unchanged_snapshot_uses_one_stat_and_no_read(tmp_path, monkeypatch):
    path = tmp_path / "capability.yaml"
    _write(path, "tool_program_enabled: true\n")
    agent = SimpleNamespace(root=tmp_path, capability_config_path=path)
    config = capability.capability_config_for_agent(agent)
    calls = []
    stat = os.stat

    def counted(*args, **kwargs):
        calls.append(args[0])
        return stat(*args, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("未变能力快照不能重读正文")

    monkeypatch.setattr(os, "stat", counted)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    assert capability.capability_config_for_agent(agent) is config
    assert calls == [path]


def test_auth_manager_bound_config_is_live_and_legacy_setter_still_works(tmp_path):
    path, config = _config(tmp_path)
    manager = AuthManager(config=config)
    live.register_live_config(config)
    _write(path, "auth_enabled: false\ngateway_per_user_owner_scoping: false\n")
    assert manager.authenticate("chat", "other").can_access_all_users is True
    assert manager.auth_enabled is False
    _write(path, "auth_enabled: true\ngateway_per_user_owner_scoping: true\n")
    assert manager.authorize(SimpleNamespace(can=lambda action: False), "READ") is False
    assert manager.auth_enabled is True
    legacy = AuthManager(auth_enabled=False)
    legacy.auth_enabled = True
    assert legacy.auth_enabled is True


def test_audit_enabled_is_read_only_and_reads_config_at_use_time(tmp_path):
    config = AgentConfig(audit_enabled=True, audit_log_path="audit")
    logger = AuditLogger(config, root=tmp_path)
    config.audit_enabled = False
    assert logger.enabled is False
    logger.log(LogParams(action="QUERY", user_id="reader", channel="chat", target_type="task", target_id="check"))
    assert not (tmp_path / "audit" / "audit.jsonl").exists()
    with pytest.raises(AttributeError):
        logger.enabled = True
    config.audit_enabled = True
    logger.log(LogParams(action="QUERY", user_id="reader", channel="chat", target_type="task", target_id="check"))
    assert (tmp_path / "audit" / "audit.jsonl").is_file()


@pytest.mark.parametrize("context_scope", ["main", "task_local"])
def test_run_start_refreshes_before_tool_preparation(tmp_path, monkeypatch, context_scope):
    from agent_py_agent.agent.agent_core import runtime_mixin as runtime
    from agent_py_agent.agent.agent_core.subagent import model_selection
    from agent_py_agent.agent.settings import model_scope

    path, config = _config(tmp_path)
    live.register_live_config(config)
    _write(path, "enable_tools: false\n")
    seen = []
    result = SimpleNamespace(runtime_status="ok")
    agent = SimpleNamespace(
        config=config, tools=SimpleNamespace(prepare_for_run=lambda: seen.append("tools")),
        _build_finalize_context=lambda *args: None,
        _get_finalization_service=lambda: SimpleNamespace(finalize=lambda ctx: result),
    )
    monkeypatch.setattr(model_selection, "canonical_subagent_model_scope", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(model_scope, "model_dependency_lifetime", lambda *args: nullcontext())
    monkeypatch.setattr(runtime, "current_prompt_scope", lambda *args: nullcontext())
    monkeypatch.setattr(runtime, "_prepare_runtime_context", lambda *args: seen.append(config.enable_tools))
    monkeypatch.setattr(runtime, "_runtime_loop_params", lambda *args: None)
    monkeypatch.setattr(runtime, "_execute_runtime_loop", lambda *args: SimpleNamespace(native_compact_carry=None))
    assert runtime._run_once_with_params(agent, "hello", runtime.RunParams(context_scope=context_scope)) is result
    assert seen == [False]


@pytest.mark.parametrize("explicit_apply", [None, True])
def test_daemon_entry_refreshes_options_and_cli_flags_win(tmp_path, monkeypatch, explicit_apply):
    from agent_py_agent.cli import daemon

    path, config = _config(tmp_path)
    live.register_live_config(config)
    _write(path, "daemon_mutate_state: false\ndaemon_start_runners: false\n")
    capability_path = tmp_path / "capability.yaml"
    _write(capability_path, "tool_program_enabled: true\n")
    observed = []

    def watch(*args, params):
        observed.append(params)
        return SimpleNamespace(summary={})

    agent = SimpleNamespace(config=config, watch_subagents=watch,
                            subagents=SimpleNamespace(workspace=tmp_path))
    args = SimpleNamespace(capability_config=capability_path, skill_dir=[], apply=explicit_apply,
                           start_runners=explicit_apply, interval=0, max_cycles=1)
    monkeypatch.setattr(daemon, "make_agent", lambda args: agent)
    monkeypatch.setattr(daemon, "make_capability_router", lambda *args: SimpleNamespace())
    assert daemon.cmd_daemon(args) == 0
    assert len(observed) == 1
    assert observed[0].mutate_state is (explicit_apply is True)
    assert observed[0].should_start_runners is (explicit_apply is True)


def test_capability_replacement_during_parse_never_publishes_mixed_snapshot(tmp_path, monkeypatch):
    path = tmp_path / "capability.yaml"
    _write(path, "tool_program_enabled: true\n")
    loader = capability.load_capability_config

    def replace_after_parse(candidate):
        parsed = loader(candidate)
        _write(path, "tool_program_enabled: invalid\n")
        return parsed

    monkeypatch.setattr(capability, "load_capability_config", replace_after_parse)
    agent = SimpleNamespace(root=tmp_path, capability_config_path=path)
    assert capability.capability_config_for_agent(agent) is None
    assert capability.capability_config_for_agent(agent) is None
    assert getattr(agent, "_capability_config_runtime_snapshot", None) is None


def test_unchanged_refresh_never_reassigns_auth_or_other_switches(tmp_path):
    class ObservedConfig(AgentConfig):
        def __setattr__(self, name, value):
            observer = vars(self).get("observe_writes")
            if observer is not None and name in live.LIVE_SWITCH_KEYS:
                observer.append((name, value))
            super().__setattr__(name, value)

    path, _unused = _config(tmp_path, "auth_enabled: false\ngateway_per_user_owner_scoping: false\n")
    config = ObservedConfig(config_path=str(path), auth_enabled=False, gateway_per_user_owner_scoping=False)
    live.register_live_config(config)
    config.observe_writes = []
    live.refresh_live_switches(config)
    assert config.observe_writes == []


def test_nested_replace_inherits_last_applied_baseline_after_file_roundtrip(tmp_path):
    path, base = _config(tmp_path, "enable_tools: false\n")
    live.register_live_config(base)
    owner = replace(base, enable_tools=True)
    live.register_live_config(owner)
    _write(path, "enable_tools: true\n")
    live.refresh_live_switches(base)
    derived = replace(owner)
    live.register_live_config(derived)
    _write(path, "enable_tools: false\n")
    live.refresh_live_switches(base)
    assert base.enable_tools is False
    assert owner.enable_tools is True
    assert derived.enable_tools is True


def test_guard_replace_override_preserves_original_file_baseline(tmp_path):
    path, base = _config(tmp_path)
    home = Path(os.environ["MY_AGENT_HOME"])
    base.my_agent_home = str(home)
    base.runtime_guard_policy = runtime_guard_policy(home_root=home)
    live.register_live_config(base)
    overridden = replace(base.runtime_guard_policy, terminal_block_enabled=True)
    object.__setattr__(overridden, "_source_path", base.runtime_guard_policy.source_path)
    owner = replace(base, runtime_guard_policy=overridden)
    live.register_live_config(owner)
    live.refresh_live_switches(path=path)
    assert base.runtime_guard_policy.terminal_block_enabled is False
    assert owner.runtime_guard_policy.terminal_block_enabled is True


@pytest.mark.parametrize("guard", [False, True])
def test_delayed_registration_keeps_clone_time_baseline(tmp_path, guard):
    path, base = _config(tmp_path, "enable_tools: false\n")
    if guard:
        home = Path(os.environ["MY_AGENT_HOME"])
        base.my_agent_home = str(home)
        base.runtime_guard_policy = runtime_guard_policy(home_root=home)
        live.register_live_config(base)
        overridden = replace(base.runtime_guard_policy, terminal_block_enabled=True)
        object.__setattr__(overridden, "_source_path", base.runtime_guard_policy.source_path)
        child = replace(base, runtime_guard_policy=overridden)
        source = home / "config" / "deployment_policy.json"
    else:
        live.register_live_config(base)
        child = replace(base, enable_tools=True)
        source = path
    for enabled in (True, False):
        text = (json.dumps({"schema_version": "deployment-policy.v1", "revision": 1,
                            "runtime_guards": {"terminal_block_enabled": enabled}}) if guard
                else f"enable_tools: {str(enabled).lower()}\n")
        _write(source, text)
        if enabled:
            live.refresh_live_switches(base)
    live.register_live_config(child)
    live.refresh_live_switches(child)
    if guard:
        assert base.runtime_guard_policy.terminal_block_enabled is False
        assert child.runtime_guard_policy.terminal_block_enabled is True
    else:
        assert base.enable_tools is False
        assert child.enable_tools is True


def test_applied_yaml_baseline_is_published_even_when_policy_reload_fails(tmp_path):
    path, config = _config(tmp_path, "enable_tools: false\n")
    home = Path(os.environ["MY_AGENT_HOME"])
    config.my_agent_home = str(home)
    config.runtime_guard_policy = runtime_guard_policy(home_root=home)
    live.register_live_config(config)
    _write(path, "enable_tools: true\n")
    _write(home / "config" / "deployment_policy.json", "{invalid")
    with pytest.raises(ValueError):
        live.refresh_live_switches(config)
    assert config.enable_tools is True
    assert dict(config.config_sources.applied)["enable_tools"] is True


@pytest.mark.parametrize("scope", ["owner", "task", "run"])
@pytest.mark.parametrize("initial", [False, True])
def test_live_refresh_preserves_equal_valued_scoped_source(tmp_path, scope, initial):
    from agent_py_agent.agent.settings.runtime_scope_config import runtime_scope_priority
    from agent_py_agent.agent.settings.services.runtime_config_env import (
        apply_runtime_config_environment,
    )

    text = f"enable_tools: {str(initial).lower()}\n"
    path, host = _config(tmp_path, text)
    overlay = tmp_path / f"{scope}.yaml"
    _write(overlay, text)
    scoped = apply_runtime_config_environment(host, env={
        "MY_AGENT_RUNTIME_CONFIG": str(overlay), "MY_AGENT_RUNTIME_CONFIG_SCOPE": scope,
    })
    expected_source = {"source": str(overlay), "priority": runtime_scope_priority(scope)}
    assert scoped.config_source_for("enable_tools") == expected_source
    live.register_live_config(host)
    live.register_live_config(scoped)
    for enabled in (not initial, initial, not initial):
        _write(path, f"enable_tools: {str(enabled).lower()}\n")
        live.refresh_live_switches(path=path, force=True)
        assert host.enable_tools is enabled
        assert scoped.enable_tools is initial
        assert scoped.config_source_for("enable_tools") == expected_source


@pytest.mark.parametrize("initial", [False, True])
def test_first_refresh_adopts_edit_after_load_before_registration(tmp_path, initial):
    path, config = _config(tmp_path, f"enable_tools: {str(initial).lower()}\n")
    assert getattr(config, "_live_switch_binding", None) is None
    assert path.resolve() not in live._FILES
    _write(path, f"enable_tools: {str(not initial).lower()}\n")
    for _ in range(3):
        live.refresh_live_switches(config, force=True)
        assert config.enable_tools is not initial


@pytest.mark.parametrize("initial", [False, True])
def test_delayed_runtime_copy_keeps_programmatic_override_baseline(tmp_path, initial):
    from agent_py_agent.agent.settings.services.runtime_config_env import (
        apply_runtime_config_environment,
    )

    path, host = _config(tmp_path, f"enable_tools: {str(initial).lower()}\n")
    live.register_live_config(host)
    overlay = tmp_path / "run.yaml"
    _write(overlay, "tool_read_max_chars: 2000\n")
    child = apply_runtime_config_environment(replace(host, enable_tools=not initial), env={
        "MY_AGENT_RUNTIME_CONFIG": str(overlay), "MY_AGENT_RUNTIME_CONFIG_SCOPE": "run",
    })
    _write(path, f"enable_tools: {str(not initial).lower()}\n")
    live.refresh_live_switches(host)
    _write(path, f"enable_tools: {str(initial).lower()}\n")
    live.refresh_live_switches(child, force=True)
    assert host.enable_tools is initial
    assert child.enable_tools is not initial
    assert child.tool_read_max_chars == 2000
    assert child.config_source_for("tool_read_max_chars")["source"] == str(overlay)


def test_live_refresh_uses_source_ownership_not_priority_threshold(tmp_path):
    from agent_py_agent.agent.settings.runtime_scope_config import (
        RuntimeConfigLayer,
        merge_runtime_config_layers,
    )

    path, host = _config(tmp_path, "")
    effective = merge_runtime_config_layers(host, [
        RuntimeConfigLayer("owner", "explicit-owner-policy", {"enable_tools": True}, priority=5),
    ])
    scoped = replace(host, **effective.values)
    scoped.config_sources = effective.sources
    scoped.config_layers = list(effective.layers)
    expected_source = scoped.config_source_for("enable_tools")
    live.register_live_config(host)
    live.register_live_config(scoped)
    _write(path, "enable_tools: false\n")
    live.refresh_live_switches(host, force=True)
    assert host.enable_tools is False
    assert scoped.enable_tools is True
    assert scoped.config_source_for("enable_tools") == expected_source


def test_live_refresh_adopts_file_and_schema_default_roundtrip(tmp_path):
    path, config = _config(tmp_path, "")
    assert config.enable_tools is True
    live.register_live_config(config)
    _write(path, "enable_tools: false\n")
    live.refresh_live_switches(config)
    assert config.enable_tools is False
    _write(path, "")
    live.refresh_live_switches(config)
    assert config.enable_tools is True


@pytest.mark.parametrize("context_scope", ["main", "task_local"])
def test_run_start_does_not_prepare_tools_for_scoped_restriction(tmp_path, monkeypatch, context_scope):
    from agent_py_agent.agent.agent_core import runtime_mixin as runtime
    from agent_py_agent.agent.agent_core.subagent import model_selection
    from agent_py_agent.agent.settings import model_scope
    from agent_py_agent.agent.settings.services.runtime_config_env import (
        apply_runtime_config_environment,
    )

    path, host = _config(tmp_path, "enable_tools: false\n")
    overlay = tmp_path / "task.yaml"
    _write(overlay, "enable_tools: false\n")
    config = apply_runtime_config_environment(host, env={
        "MY_AGENT_RUNTIME_CONFIG": str(overlay), "MY_AGENT_RUNTIME_CONFIG_SCOPE": "task",
    })
    live.register_live_config(config)
    _write(path, "enable_tools: true\n")
    seen = []
    result = SimpleNamespace(runtime_status="ok")
    agent = SimpleNamespace(
        config=config, tools=SimpleNamespace(prepare_for_run=lambda: seen.append("tools")),
        _build_finalize_context=lambda *args: None,
        _get_finalization_service=lambda: SimpleNamespace(finalize=lambda ctx: result),
    )
    monkeypatch.setattr(model_selection, "canonical_subagent_model_scope", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(model_scope, "model_dependency_lifetime", lambda *args: nullcontext())
    monkeypatch.setattr(runtime, "current_prompt_scope", lambda *args: nullcontext())
    monkeypatch.setattr(runtime, "_prepare_runtime_context", lambda *args: seen.append(config.enable_tools))
    monkeypatch.setattr(runtime, "_runtime_loop_params", lambda *args: None)
    monkeypatch.setattr(runtime, "_execute_runtime_loop", lambda *args: SimpleNamespace(native_compact_carry=None))
    assert runtime._run_once_with_params(agent, "核对工具限制", runtime.RunParams(context_scope=context_scope)) is result
    assert seen == [False]
    assert config.config_source_for("enable_tools")["source"] == str(overlay)
