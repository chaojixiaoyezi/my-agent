"""W3 静默展示、typed 读取/选择/孩子加载；不从正文授予工具。"""
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.capability import package_selection_runtime as selection
from agent_py_agent.agent.capability import subagent_package_entries as children
from agent_py_agent.agent.capability.package_selection_context import PackageEntryContext
from agent_py_agent.agent.capability.skill_search_tool import SkillSearchTool
from agent_py_agent.agent.tooling.registry import _render_deferred_notice
from agent_py_agent.agent.tooling.tool_search_state import (
    merge_loop_loaded_tool_names,
    ordered_carried_loaded_tool_names,
    resolve_active_loaded_tool_names,
)
from agent_py_agent.tests.test_capability_package_discovery import package_fixture
from agent_py_agent.tests.test_capability_package_read import _reader_agent
from agent_py_agent.tests.test_cpack_w3_lazy_activation import component_client, pack_registry


def _view(tmp_path, monkeypatch):
    client, _, _, owner, starts, calls = component_client(tmp_path, monkeypatch)
    registry = pack_registry(tmp_path, owner)
    registry._mcp_clients.append(client)
    registry.tools.update((tool.model_spec.name, tool) for tool in client.pack_tools)
    return registry, client, starts, calls


def _params(snapshot, loaded=None):
    return SimpleNamespace(tool_runtime_snapshot=snapshot, loaded_tool_names=[] if loaded is None else loaded,
                           live_archive_state={})


def test_silent_names_truthful_catalog_and_explicit_full_output(tmp_path, monkeypatch):
    registry, client, starts, _ = _view(tmp_path, monkeypatch)
    try:
        spec = client.pack_tools[0].model_spec
        snapshot = registry.runtime_snapshot()
        for shortlist in (None, frozenset({spec.name})):
            notice = _render_deferred_notice([spec], presentation_shortlist_names=shortlist,
                                            declared_names=frozenset({spec.name}))
            assert "1 个工具" in notice and spec.name not in notice and "插件 lazy-pack" not in notice
        assert spec.name not in {s.name for s in registry.model_visible_specs(runtime_snapshot=snapshot)}
        catalog = registry.tools["tool_search"].execute({"action": "catalog"})
        assert catalog.ok and spec.name in catalog.output
        assert spec.name in {s.name for s in registry.model_visible_specs(loaded_tool_names=[spec.name])}
        assert not starts
    finally:
        registry.close_mcp_clients()


def test_pack_get_typed_envelope_loads_next_round_not_text(tmp_path, monkeypatch):
    registry, client, starts, _ = _view(tmp_path, monkeypatch)
    try:
        name = client.pack_tools[0].model_spec.name
        package = replace(package_fixture(), mcp_runtime="on_demand", mcp_tool_names=(name, "outside_snapshot"))
        agent, _ = _reader_agent(package)
        tool = SkillSearchTool(agent)
        get = tool.execute({"action": "get", "package_id": package.package_id})
        assert get.ok and get.result_envelope["capability_package_tools"] == {
            "package_id": package.package_id, "activation_id": package.activation_id,
            "loaded_tool_names": [name, "outside_snapshot"],
        }
        assert "capability_package_tools" not in tool.execute(
            {"action": "search", "package_id": package.package_id}).result_envelope
        assert "capability_package_tools" not in tool.execute(
            {"action": "get", "package_id": package.package_id, "resource_path": "missing"}).result_envelope
        carried = {"tool_round": 1, "tool_result_envelope": get.result_envelope}
        assert ordered_carried_loaded_tool_names([carried]) == (name, "outside_snapshot")
        params = _params(registry.runtime_snapshot())
        merge_loop_loaded_tool_names(params, ordered_carried_loaded_tool_names([carried]))
        assert params.loaded_tool_names == [name]
        assert name in {s.name for s in registry.model_visible_specs(
            runtime_snapshot=params.tool_runtime_snapshot, loaded_tool_names=params.loaded_tool_names)}
        assert not ordered_carried_loaded_tool_names([{"tool_round": 2, "output": str(get.result_envelope)}])
        assert not starts
    finally:
        registry.close_mcp_clients()


@pytest.mark.parametrize("kind", [list, set])
def test_merge_parity_with_original_loop_and_frozen_filter(tmp_path, monkeypatch, kind):
    from agent_py_agent.agent.agent_core._tool_loop_service import _load_discovered_tools
    registry, client, _, _ = _view(tmp_path, monkeypatch)
    try:
        name = client.pack_tools[0].model_spec.name
        snapshot = registry.runtime_snapshot()
        left = _params(snapshot, kind(["read_file"]))
        right = _params(snapshot, kind(["read_file"]))
        left.live_archive_state["loaded_tool_order"] = ("read_file",)
        right.live_archive_state["loaded_tool_order"] = ("read_file",)
        names = [name, "read_file", name]
        _load_discovered_tools(left, {"tool_round": 0, "tool_result_envelope": {"tool_search": {"loaded_tool_names": names}}})
        merge_loop_loaded_tool_names(right, names)
        assert left.loaded_tool_names == right.loaded_tool_names
        assert left.live_archive_state == right.live_archive_state
        merge_loop_loaded_tool_names(right, ["not_authorized", name])
        assert "not_authorized" not in right.loaded_tool_names
        assert right.live_archive_state["loaded_tool_order"] == ("read_file", name)
        assert len(resolve_active_loaded_tool_names([f"t{i}" for i in range(40)])) == 32
    finally:
        registry.close_mcp_clients()


def test_selection_loads_only_after_entry_commit_and_snapshot_intersection(tmp_path, monkeypatch):
    registry, client, _, _ = _view(tmp_path, monkeypatch)
    try:
        name = client.pack_tools[0].model_spec.name
        package = replace(package_fixture(), mcp_runtime="on_demand", mcp_tool_names=(name, "outside_snapshot"))
        agent, skills = _reader_agent(package)
        agent.conversation_store = SimpleNamespace(tasks=SimpleNamespace(finish_capability_selection=lambda **k: object()))
        authority = SimpleNamespace(check=lambda: None, is_current=lambda: True, task_id="task", thread_id="thread")
        claim = SimpleNamespace(claim_id="claim")
        result = SimpleNamespace(outcome="selected", error_code="", warning_codes=(), selected_refs=[package.to_ref()], failure_facts=None)
        scope = SimpleNamespace(skills=skills, config=SimpleNamespace(capability_bundle_max_tokens=1000))
        context = PackageEntryContext("entry", 1, ())
        monkeypatch.setattr(selection, "prepare_package_entry_context", lambda *a, **k: context)
        monkeypatch.setattr(selection, "record_package_entry_usage", lambda *a: None)
        monkeypatch.setattr(selection, "record_runtime_facts_turn_ir", lambda *a, **k: False)
        params = _params(registry.runtime_snapshot())
        selection._commit_selection(agent, params, scope, authority, claim, result, 1000)
        assert not params.loaded_tool_names
        monkeypatch.setattr(selection, "record_runtime_facts_turn_ir", lambda *a, **k: True)
        selection._commit_selection(agent, params, scope, authority, claim, result, 1000)
        assert params.loaded_tool_names == [name]
    finally:
        registry.close_mcp_clients()


def test_child_loading_intersects_explicit_child_tools(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core import tool_ir_history
    from agent_py_agent.agent.agent_core.subagent import model_selection
    from agent_py_agent.agent.agent_core.tool_loop import recovery
    registry, client, _, _ = _view(tmp_path, monkeypatch)
    try:
        name = client.pack_tools[0].model_spec.name
        package = replace(package_fixture(), mcp_runtime="on_demand", mcp_tool_names=(name, "read_file"))
        agent, skills = _reader_agent(package)
        task = SimpleNamespace(agent_thread_id="thread", allowed_tools=[name])
        agent.subagents = SimpleNamespace(creation_guard=nullcontext, load=lambda run: task)
        monkeypatch.setattr(children, "subagent_entries_enabled", lambda agent: True)
        monkeypatch.setattr(children, "_authorized_references", lambda task, snapshot: [package.to_ref()])
        monkeypatch.setattr(children, "SubagentEntryAuthority", lambda *a: SimpleNamespace(check=lambda: None))
        monkeypatch.setattr(children, "_prepare_entries", lambda *a: PackageEntryContext("entry", 1, ()))
        monkeypatch.setattr(children, "record_package_entry_usage", lambda *a: None)
        monkeypatch.setattr(model_selection, "first_request_preparation", lambda *a: SimpleNamespace(package_entries_prepared=False))
        monkeypatch.setattr(recovery, "runtime_run_scope", lambda *a: None)
        monkeypatch.setattr(tool_ir_history, "record_runtime_facts_turn_ir", lambda *a, **k: True)
        params = _params(registry.runtime_snapshot())
        params.run_id = "child"
        children.prepare_subagent_package_entries(agent, params)
        assert params.loaded_tool_names == [name]
        task.allowed_tools = []
        params.loaded_tool_names = []
        children.prepare_subagent_package_entries(agent, params)
        assert params.loaded_tool_names == []
    finally:
        registry.close_mcp_clients()


def test_playbook_does_not_treat_pack_as_vendor_mcp(tmp_path, monkeypatch):
    from agent_py_agent.agent.business_spaces.v13_w7_playbooks import _mcp_rows
    registry, _, _, _ = _view(tmp_path, monkeypatch)
    try:
        call = SimpleNamespace(owner_id=registry.operation_owner_id,
                               context=SimpleNamespace(host=SimpleNamespace(tools=registry)))
        rows, partial = _mcp_rows(call)
        assert rows == [] and partial is False
    finally:
        registry.close_mcp_clients()


def test_refresh_keeps_current_candidates_when_other_plugin_contributes_tools(tmp_path, monkeypatch):
    from agent_py_agent.agent.plugin_runtime import PluginMCPClient
    from agent_py_agent.agent.tooling.plugin_registration import refresh_plugin_client
    client, _, _, _, _, calls = component_client(tmp_path, monkeypatch)
    other = object.__new__(PluginMCPClient)
    other.lazy = False
    other.validated_tools = (SimpleNamespace(
        model_spec=replace(client.pack_tools[0].model_spec, name="plugin__other__check"),
        availability=lambda: SimpleNamespace(available=True)),)
    registry = SimpleNamespace(tools={}, _mcp_clients=[client, other])
    try:
        transport = client.start()
        refresh_plugin_client(registry, client, transport)
        assert tuple(tool.model_spec.name for tool in client.validated_tools) == (
            client.pack_tools[0].model_spec.name,)
        assert not calls
    finally:
        client.stop()
