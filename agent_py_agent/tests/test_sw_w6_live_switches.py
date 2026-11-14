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
from uuid import uuid4

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


@pytest.mark.parametrize("key", [
    "acp_bridge_enabled", "custom_tools_enabled", "llm_call_records_enabled",
    "notify_delivery_enabled", "openai_compat_gateway_enabled", "ops_remote_update_enabled",
    "side_questions_enabled", "web_terminal_enabled", "technical_cutoff_continuation_enabled",
    "tool_wait_input_reply_enabled",
])
def test_gap_switches_refresh_registered_config_copies_in_both_directions(tmp_path, key):
    path, config = _config(tmp_path, f"{key}: false\n")
    pooled = replace(config)
    live.register_live_config(config)
    live.register_live_config(pooled)
    for enabled in (True, False):
        _write(path, f"{key}: {str(enabled).lower()}\n")
        assert key in live.refresh_live_switches(config, force=True)
        assert getattr(config, key) is enabled
        assert getattr(pooled, key) is enabled


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
    caller = threading.current_thread()

    def stat(*args, **kwargs):
        # 只计此次同步调用的全部文件访问；同进程其它任务可并发读自己的文件。
        if threading.current_thread() is caller:
            calls.append(args[0])
        return real_stat(*args, **kwargs)

    loader = live.load_config

    def forbidden_parse(*args, **kwargs):
        if threading.current_thread() is caller:
            pytest.fail("未变文件不能重新解析")
        return loader(*args, **kwargs)

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


@pytest.mark.parametrize("key", [
    "tool_program_enabled", "command_approval_filter_enabled", "target_scope_policy_enabled",
    "workflow_auto_dispatch_reviewers",
])
def test_capability_switch_and_router_see_file_edit_without_restart(tmp_path, key):
    assert key in live.LIVE_SWITCH_KEYS
    path = tmp_path / "capability.yaml"
    _write(path, f"{key}: true\n")
    router = SimpleNamespace(config=None)
    agent = SimpleNamespace(root=tmp_path, capability_config_path=path, capability_router=router)
    before = capability.capability_config_for_agent(agent)
    assert getattr(before, key) is True
    for enabled in (False, True):
        _write(path, f"{key}: {str(enabled).lower()}\n")
        current = capability.capability_config_for_agent(agent)
        assert getattr(current, key) is enabled
        assert agent._capability_config_runtime_snapshot.config is current
        assert router.config is current


def test_capability_unchanged_snapshot_uses_one_stat_and_no_read(tmp_path, monkeypatch):
    path = tmp_path / "capability.yaml"
    _write(path, "tool_program_enabled: true\n")
    agent = SimpleNamespace(root=tmp_path, capability_config_path=path)
    config = capability.capability_config_for_agent(agent)
    calls = []
    stat = os.stat
    reader = Path.read_bytes
    caller = threading.current_thread()

    def counted(*args, **kwargs):
        if threading.current_thread() is caller:
            calls.append(args[0])
        return stat(*args, **kwargs)

    def forbidden(*args, **kwargs):
        if threading.current_thread() is caller:
            pytest.fail("未变能力快照不能重读正文")
        return reader(*args, **kwargs)

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


# LLM: 真实管理入口只写隔离目录中的离线档案与会话选择，不替换模型解析或配置刷新。
# 函数用途: 为投影回归保存并选中可由正常Agent回合使用的Echo模型。
def _select_live_switch_echo_profile(agent):
    from agent_py_agent.agent.settings.model_profiles import execute_model_profile_operation
    from agent_py_agent.agent.settings.thread_model_selection import execute_local_model_operation

    profile_id = str(uuid4())
    added = execute_model_profile_operation(agent, "add", {"profile_id": profile_id, "profile": {
        "transport": "offline", "model_backend": "echo", "model_name": "live-switch-echo",
        "model_context_window_tokens": 32768,
    }})
    assert any(row["id"] == profile_id for row in added["profiles"])
    selected = execute_local_model_operation(agent, "live-switch-projection", "select", {"profile_id": profile_id})
    assert selected["selected"] == profile_id
    assert agent.conversation_store.threads.require(selected["thread_id"]).model_profile_id == profile_id
    return profile_id, selected["thread_id"]


# LLM: 先真实load再改文件，随后投影；三次刷新均须认回加载基线，不把旧值当局部覆盖。
# 函数用途: 验证两处模型投影、无Home分支及直接配置在双向切换下保留独立来源和行为。
@pytest.mark.parametrize("projection", ["direct", "selected", "unconfigured", "unconfigured_no_home"])
@pytest.mark.parametrize("initial", [True, False])
def test_model_projection_adopts_file_edit_across_three_refreshes(tmp_path, projection, initial):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings.model_profiles import selected_model_config_for_resources

    path, config = _config(tmp_path, f"enable_tools: {str(initial).lower()}\nmemory_curator_enabled: false\n")
    original_sources = config.config_sources
    baseline = (original_sources.path, original_sources.applied, original_sources.guard_applied)
    original_model_source = config.config_source_for("model_name")
    _write(path, f"enable_tools: {str(not initial).lower()}\nmemory_curator_enabled: false\n")
    if projection == "direct":
        projected = config
    else:
        agent = SimpleAgent(config, tmp_path / "workspace")
        profile_id = _select_live_switch_echo_profile(agent)[0] if projection == "selected" else "default"
        home = None if projection == "unconfigured_no_home" else agent.home_paths
        projected = selected_model_config_for_resources(config, home, profile_id=profile_id)
    projected_sources = projected.config_sources
    for _ in range(3):
        live.refresh_live_switches(projected, force=True)
        assert projected.enable_tools is not initial
    assert config.config_source_for("model_name") == original_model_source
    if projection != "direct":
        assert projected_sources is not original_sources
        assert (projected_sources.path, projected_sources.applied, projected_sources.guard_applied) == baseline
        assert projected.config_sources is not original_sources
        assert (original_sources.path, original_sources.applied, original_sources.guard_applied) == baseline
        assert projected.config_sources.path == path.resolve()
        assert dict(projected.config_sources.applied)["enable_tools"] is not initial
        assert isinstance(projected.config_sources.guard_applied, tuple)
        expected = {"source": "owner_model_profile", "priority": 90, "profile_id": profile_id} if projection == "selected" else {
            "source": "model_unconfigured", "priority": 90,
        }
        assert projected.config_source_for("model_name") == expected
        if projection != "selected":
            assert (projected.model_backend, projected.model_name, projected.api_key) == ("", "", "")


# LLM: 正常run经过真实选模、依赖作用域及工具准备；仅观察原prepare调用，不替换运行或刷新链。
# 函数用途: 验证连续两回合采用新文件开关，并保留与原文件同值的明确owner覆盖。
@pytest.mark.parametrize("owner_override", [False, True])
@pytest.mark.parametrize("initial", [True, False])
def test_model_projected_normal_turns_refresh_before_tool_preparation(tmp_path, monkeypatch, initial, owner_override):
    from agent_py_agent.agent.core import SimpleAgent
    from agent_py_agent.agent.settings.services.runtime_config_env import (
        apply_runtime_config_environment,
    )

    path, config = _config(tmp_path, f"enable_tools: {str(initial).lower()}\nmemory_curator_enabled: false\n")
    if owner_override:
        overlay = tmp_path / "owner.yaml"
        _write(overlay, f"enable_tools: {str(initial).lower()}\n")
        config = apply_runtime_config_environment(config, env={
            "MY_AGENT_RUNTIME_CONFIG": str(overlay), "MY_AGENT_RUNTIME_CONFIG_SCOPE": "owner",
        })
        assert config.config_source_for("enable_tools") == {"source": str(overlay), "priority": 30}
    agent = SimpleAgent(config, tmp_path / "workspace")
    profile_id, thread_id = _select_live_switch_echo_profile(agent)
    prepared = []
    registry_type = type(agent.tools)
    original_prepare = registry_type.prepare_for_run

    def observed_prepare(registry, *args, **kwargs):
        prepared.append((agent.config.enable_tools, agent.config.config_source_for("model_name")))
        return original_prepare(registry, *args, **kwargs)

    monkeypatch.setattr(registry_type, "prepare_for_run", observed_prepare)
    _write(path, f"enable_tools: {str(not initial).lower()}\nmemory_curator_enabled: false\n")
    enabled = initial if owner_override else not initial
    for turn in range(2):
        result = agent.run(f"核对本轮工具开关 {turn}", save=False, task_attributes={"conversation_thread_id": thread_id})
        assert result.runtime_status == "ok"
        assert len(prepared) == (turn + 1 if enabled else 0)
    assert all(value is enabled and source == {"source": "owner_model_profile", "priority": 90, "profile_id": profile_id}
               for value, source in prepared)
    if owner_override:
        assert agent.config.enable_tools is initial
        assert agent.config.config_source_for("enable_tools") == {"source": str(overlay), "priority": 30}


@pytest.mark.parametrize("holders", [1, 17, 100])
def test_settled_generation_skips_switch_projection_but_updates_every_copy(tmp_path, monkeypatch, holders):
    from concurrent.futures import ThreadPoolExecutor

    path, config = _config(tmp_path)
    copies = [config, *(replace(config) for _ in range(holders - 1))]
    for candidate in copies:
        live.register_live_config(candidate)
    live.refresh_live_switches(config)
    applied = []
    original = live._apply_switches

    def tracked(*args):
        applied.append(id(args[0]))
        return original(*args)

    monkeypatch.setattr(live, "_apply_switches", tracked)
    with ThreadPoolExecutor(max_workers=17) as pool:
        assert list(pool.map(lambda index: live.refresh_live_switches(copies[index % holders]), range(34))) == [frozenset()] * 34
    assert applied == []
    _write(path, "enable_tools: false\n")
    assert "enable_tools" in live.refresh_live_switches(config)
    assert set(applied) == {id(candidate) for candidate in copies}
    assert all(candidate.enable_tools is False for candidate in copies)


def test_unsettled_override_is_reconsidered_without_another_file_change(tmp_path):
    path, config = _config(tmp_path)
    child = replace(config, enable_tools=False)
    live.register_live_config(config)
    live.register_live_config(child)
    _write(path, "enable_tools: false\n")
    live.refresh_live_switches(config)
    child.enable_tools = True
    assert "enable_tools" in live.refresh_live_switches(config)
    assert child.enable_tools is False


def test_settled_switches_still_detect_replaced_guard_source(tmp_path):
    _path, config = _config(tmp_path)
    first = tmp_path / "first-host"
    second = tmp_path / "second-host"
    config.runtime_guard_policy = runtime_guard_policy(home_root=first)
    live.register_live_config(config)
    live.refresh_live_switches(config)
    config.runtime_guard_policy = runtime_guard_policy(home_root=second)
    _write(second / "config" / "deployment_policy.json", json.dumps({
        "schema_version": "deployment-policy.v1", "revision": 1,
        "runtime_guards": {"terminal_block_enabled": True},
    }))
    assert "terminal_block_enabled" in live.refresh_live_switches(config)
    assert config.runtime_guard_policy.terminal_block_enabled is True


def test_settled_generation_republishes_externally_replaced_provenance(tmp_path):
    _path, config = _config(tmp_path)
    live.register_live_config(config)
    live.refresh_live_switches(config)
    config.config_sources = dict(config.config_sources)
    live.refresh_live_switches(config)
    assert isinstance(config.config_sources, live._ConfigSources)
    child = replace(config)
    live.register_live_config(child)
    assert child._live_switch_binding.applied == config._live_switch_binding.applied


def test_failed_guard_refresh_does_not_retain_old_file_config_generation(tmp_path, monkeypatch):
    path, config = _config(tmp_path)
    copies = [config, replace(config), replace(config)]
    for candidate in copies:
        live.register_live_config(candidate)
    live.refresh_live_switches(config)
    source = config._live_switch_binding.source
    previous = weakref.ref(source.fresh)
    previous_revision = source.revision
    _write(path, "enable_tools: false\n")
    original = live._refresh_guard
    calls = []

    def fail_first(*args, **kwargs):
        calls.append(args[0])
        raise OSError("injected guard read failure")

    monkeypatch.setattr(live, "_refresh_guard", fail_first)
    with pytest.raises(OSError, match="injected guard read failure"):
        live.refresh_live_switches(config)
    assert len(calls) == 1
    assert source.revision > previous_revision
    assert any(candidate._live_switch_binding.settled_revision == previous_revision for candidate in copies)
    assert previous() is None, "延迟绑定只能保留轻量代次，不能钉住旧配置"
    monkeypatch.setattr(live, "_refresh_guard", original)
    live.refresh_live_switches(config)
    assert all(candidate.enable_tools is False for candidate in copies)


def test_guard_cache_retires_unbound_paths_only_after_complete_refresh(tmp_path, monkeypatch):
    path, base = _config(tmp_path)
    live.register_live_config(base)
    source = base._live_switch_binding.source
    for index in range(12):
        home = tmp_path / f'guard-{index}'
        _write(home / 'config' / 'deployment_policy.json', json.dumps({
            'schema_version': 'deployment-policy.v1', 'revision': 1,
            'runtime_guards': {'terminal_block_enabled': False},
        }))
        child = replace(base, runtime_guard_policy=runtime_guard_policy(home_root=home))
        live.register_live_config(child)
        live.refresh_live_switches(base)
        ref = weakref.ref(child)
        del child
        assert ref() is None
    live.refresh_live_switches(base)
    assert len(source.policies) == 0

    home = tmp_path / 'active-guard'
    policy_path = home / 'config' / 'deployment_policy.json'
    _write(policy_path, json.dumps({'schema_version': 'deployment-policy.v1', 'revision': 1,
                                  'runtime_guards': {'terminal_block_enabled': False}}))
    active = replace(base, runtime_guard_policy=runtime_guard_policy(home_root=home))
    live.register_live_config(active)
    live.refresh_live_switches(base)
    assert len(source.policies) == 1
    _write(policy_path, json.dumps({'schema_version': 'deployment-policy.v1', 'revision': 2,
                                  'runtime_guards': {'terminal_block_enabled': True}}))
    assert 'terminal_block_enabled' in live.refresh_live_switches(base)
    assert active.runtime_guard_policy.terminal_block_enabled is True

    # 异常不能把未访问的活路径或旧诊断状态当作已完整核验后删除。
    stale = tmp_path / 'unvisited.json'
    source.policies[stale] = ('evidence', object())
    original = live._refresh_guard
    def fail(*args, **kwargs):
        raise PermissionError('unreadable guard')
    monkeypatch.setattr(live, '_refresh_guard', fail)
    with pytest.raises(PermissionError):
        live.refresh_live_switches(base)
    assert stale in source.policies and policy_path in source.policies
    monkeypatch.setattr(live, '_refresh_guard', original)
    live.refresh_live_switches(base)
    assert set(source.policies) == {policy_path}
