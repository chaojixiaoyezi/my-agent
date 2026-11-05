"""W3 展示缓存和有界热路径使用真实安装表；发送的 require 始终绕过缓存。"""
import os
import time
from dataclasses import replace

import pytest

from agent_py_agent.agent import plugin_activation_ref as refs
from agent_py_agent.agent import plugin_install_store
from agent_py_agent.agent.plugin_activation import PluginActivationRequest
from agent_py_agent.agent.plugin_activation_ref import PluginActivationRef
from agent_py_agent.tests.test_plugin_activation_ref import plugin_reference


def _aged(owner):
    path = owner.plugins_dir / "installations.json"
    old = time.time_ns() - 10_000_000_000
    os.utime(path, ns=(old, old))
    return path


def _count(monkeypatch):
    calls = []
    original = plugin_install_store.decode_installation_state
    def decode(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(plugin_install_store, "decode_installation_state", decode)
    return calls


def test_aged_positive_memo_and_fresh_send_fence(tmp_path, monkeypatch):
    store, entry, ref, owner = plugin_reference(tmp_path, phase="active")
    _aged(owner)
    calls = _count(monkeypatch)
    assert ref.available()
    assert ref.available()
    assert len(calls) == 1
    assert ref.require() == entry
    assert len(calls) == 2
    store.change_activation(PluginActivationRequest("revoke", entry.revision, replace(entry.activation, phase="revoked")))
    calls.clear()
    assert not ref.available()
    assert len(calls) == 1
    with pytest.raises(ValueError):
        ref.require()
    assert len(calls) == 2


def test_fresh_window_never_reads_or_writes_memo(tmp_path, monkeypatch):
    _, _, ref, owner = plugin_reference(tmp_path, phase="active")
    calls = _count(monkeypatch)
    from types import SimpleNamespace

    from agent_py_agent.agent.common import cache_freshness
    now = (owner.plugins_dir / "installations.json").stat().st_mtime_ns / 1e9
    monkeypatch.setattr(cache_freshness, "time", SimpleNamespace(time=lambda: now))
    assert ref.available() and ref.available()
    assert len(calls) == 2
    assert not any(key[-1] == ref.scope.activation_id for key in refs._ACTIVATION_AVAILABILITY_MEMO)


def test_replace_preserved_mtime_invalidates_and_missing_is_not_cached(tmp_path, monkeypatch):
    store, entry, ref, owner = plugin_reference(tmp_path, phase="active")
    path = _aged(owner)
    calls = _count(monkeypatch)
    assert ref.available()
    before = path.stat()
    store.change_activation(PluginActivationRequest("revoke", entry.revision, replace(entry.activation, phase="revoked")))
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    calls.clear()  # 只计可用性消费，排除真实 CAS 自身必须解码的成本。
    assert not ref.available()
    assert len(calls) == 1
    absent = replace(ref, scope=replace(ref.scope, activation_id="f" * 64))
    assert not absent.available() and not absent.available()
    assert len(calls) == 3


def test_bounded_fifo_and_owner_checks(tmp_path, monkeypatch):
    _, _, ref, owner = plugin_reference(tmp_path, phase="active")
    _aged(owner)
    monkeypatch.setattr(refs, "_ACTIVATION_AVAILABILITY_MEMO", refs.OrderedDict())
    monkeypatch.setattr(refs, "ACTIVATION_AVAILABILITY_MEMO_MAX_ENTRIES", 1)
    assert ref.available()
    # 不同有效引用占用不同槽；用第二个真实安装而不伪造 require。
    _, _, second, second_owner = plugin_reference(tmp_path / "second", phase="active")
    _aged(second_owner)
    assert second.available()
    assert len(refs._ACTIVATION_AVAILABILITY_MEMO) == 1
    calls = _count(monkeypatch)
    assert ref.available() and len(calls) == 1
    from agent_py_agent.agent.user_space.owner_resolver import OwnerIdentity, resolve_owner_home
    other = resolve_owner_home(tmp_path / "absent", OwnerIdentity.provider_user("test", "reader"))
    denied = PluginActivationRef.from_owner(other, "sample-peek", "a" * 64)
    assert not denied.available()
    assert not other.home_dir.exists()


@pytest.mark.parametrize("pack_count", [20, 100])
def test_unused_packs_have_one_decode_zero_launches_and_silent_schemas(tmp_path, monkeypatch, pack_count, record_property):
    from agent_py_agent.agent import plugin_runtime
    from agent_py_agent.agent.tooling import plugin_registration
    from agent_py_agent.agent.user_space.owner_resolver import resolve_owner_home
    from agent_py_agent.tests.test_cpack_w3_lazy_activation import pack_registry, staged_pack
    owner = resolve_owner_home(tmp_path / "home")
    for index in range(pack_count):
        staged_pack(tmp_path, name=f"pack-{index}", tools=10, owner=owner)
    _aged(owner)
    refs._ACTIVATION_AVAILABILITY_MEMO.clear()
    plugin_registration._INSTALLATION_SNAPSHOTS.clear()
    from agent_py_agent.agent import plugin_sandbox
    monkeypatch.setattr(plugin_sandbox, "plugin_sandbox_problem", lambda *args, **kwargs: "")
    monkeypatch.setattr(plugin_runtime, "plugin_sandbox_problem", lambda *args, **kwargs: "")
    monkeypatch.setattr(plugin_runtime, "sandboxed_plugin_argv", lambda argv, spec: list(argv))
    starts = []
    def forbidden_start(*args, **kwargs):
        starts.append(1)
        pytest.fail("unused pack started")
    monkeypatch.setattr(plugin_runtime.PluginMCPClient, "start", forbidden_start)
    registry = pack_registry(tmp_path, owner)
    registry.catalog_default_deferral_enabled = True
    calls = _count(monkeypatch)
    try:
        registry.prepare_for_run()
        snapshot = registry.runtime_snapshot()
        tools = [tool for tool in registry.tools.values() if isinstance(tool, plugin_runtime.PackMCPProxyTool)]
        assert len(tools) == pack_count * 10
        assert all(tool.precheck_availability().available for tool in tools)
        visible = registry.model_visible_specs(runtime_snapshot=snapshot)
        names = {tool.model_spec.name for tool in tools}
        section = registry.render_catalog_section(runtime_snapshot=snapshot)
        assert not names.intersection(spec.name for spec in visible)
        assert not any(name in section for name in names)
        assert len(calls) == 1 and not starts
        registry.prepare_for_run()
        registry.runtime_snapshot()
        assert len(calls) == 1 and not starts
        for key, value in {"packs": pack_count, "tools_per_pack": 10, "spawns": len(starts),
                           "decodes": len(calls), "pack_schemas": len(names.intersection(spec.name for spec in visible)),
                           "notice_pack_names": sum(name in section for name in names)}.items():
            record_property(key, value)
    finally:
        registry.close_mcp_clients()


def test_disabled_switch_does_not_construct_pack_clients(tmp_path, monkeypatch):
    from agent_py_agent.agent import plugin_runtime
    from agent_py_agent.tests.test_cpack_w3_lazy_activation import pack_registry, staged_pack
    _, _, owner = staged_pack(tmp_path)
    monkeypatch.setattr(plugin_runtime.PluginMCPClient, "__init__", lambda *a, **k: pytest.fail("disabled constructed client"))
    registry = pack_registry(tmp_path, owner, capability_pack_mcp_enabled=False)
    try:
        registry.prepare_for_run()
        assert not registry._mcp_clients
    finally:
        registry.close_mcp_clients()
