"""Consolidated tests for shard c3:
- test_cpack_w3_crash_restart.py: W3 单激活退避/目录漂移/原错误透传；内存传输用例不替代真实 stdio。
- test_cpack_w3_presentation_loading.py: W3 静默展示、typed 读取/选择/孩子加载；不从正文授予工具。
- test_cpack_w3_revoke_fencing.py: W3 固定代发送门和迟到发布：真实 CAS，Event 控制交错，内存传输不证明 OS 清理。
- test_r6_w2.py: 用真实运行索引和规范文件约束恢复集合的读取量及错误边界。
- test_r6_w3.py
- test_r6_w4.py: 隔离 owner 的真实追加、迁移、故障恢复与 no-follow 回归，不启动模型或 live 服务。
- test_r6_w5.py: 当前来源窗口不随历史目录增长，恢复仍保留原身份与不完整事实。
"""

import json
import os
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent_py_agent.agent import plugin_runtime
from agent_py_agent.agent.agent_core.model.call_runtime import model_call_ledger
from agent_py_agent.agent.agent_core.orchestration.dispatch import capability_auto_sweep as sweep
from agent_py_agent.agent.agent_core.runtime.loop_models import RuntimeContextRequest
from agent_py_agent.agent.agent_core.runtime.loop_support import build_runtime_main_context_bundle
from agent_py_agent.agent.capability import package_selection_runtime as selection
from agent_py_agent.agent.capability import subagent_package_entries as children
from agent_py_agent.agent.capability.package_selection_context import PackageEntryContext
from agent_py_agent.agent.capability.skill_search_tool import SkillSearchTool
from agent_py_agent.agent.common import tool_output_paths
from agent_py_agent.agent.contracts.model_call_ledger import (
    ModelCallFinishParams,
    ModelCallFirstTokenParams,
    ModelCallLedger,
    ModelCallLedgerContext,
    ModelCallLedgerOptions,
)
from agent_py_agent.agent.conversation.background_context import (
    BackgroundContextLoad,
    _task_context_ids,
)
from agent_py_agent.agent.memory_archive import compact_tool_output_refs as refs
from agent_py_agent.agent.observability import task_call_inspector as inspector_module
from agent_py_agent.agent.observability import task_rounds as storage
from agent_py_agent.agent.observability.telemetry_ledger import ToolTelemetryLedger, _encode
from agent_py_agent.agent.plugin_activation import PluginActivationRequest
from agent_py_agent.agent.subagents import manager as managers
from agent_py_agent.agent.subagents.services import runtime_closeout
from agent_py_agent.agent.tooling.mcp_client import MCPError, MCPToolInfo
from agent_py_agent.agent.tooling.models import ToolRuntimeSnapshot
from agent_py_agent.agent.tooling.registry import _render_deferred_notice
from agent_py_agent.agent.tooling.tool_search_state import (
    merge_loop_loaded_tool_names,
    ordered_carried_loaded_tool_names,
    resolve_active_loaded_tool_names,
)
from agent_py_agent.agent.user_space import context_bundle as bundles
from agent_py_agent.agent.user_space.context_bundle_artifacts import (
    MainContextBundleArtifactUpdateRequest,
    _tool_index_window,
    update_main_context_bundle_artifacts,
)
from agent_py_agent.tests.test_audit_source_worker import _agent, _bind_audit_workspace, _watch
from agent_py_agent.tests.test_capability_package_discovery import package_fixture
from agent_py_agent.tests.test_capability_package_read import _reader_agent
from agent_py_agent.tests.test_closeout_recovery_dependencies import _pending_task
from agent_py_agent.tests.test_cpack_w3_hot_path_bounds import _aged
from agent_py_agent.tests.test_cpack_w3_lazy_activation import component_client, pack_registry
from agent_py_agent.tests.test_model_call_ledger_partitions import (
    _event,
    _other_scoped_calls,
    _start,
)
from agent_py_agent.tests.test_provider_transient_auto_resume import (
    AgentConfig,
    SimpleAgent,
    _assert_single_logical_turn,
    _TransientThenOkBackend,
    _UsageStreamIncompleteThenOkBackend,
    provider_transient_auto_resume,
)
from agent_py_agent.tests.test_r5db_closeout_tool_index_window import (
    SCOPE,
    _externalize,
    _history,
    _mark,
    _update,
    ensure_my_agent_home,
)
from agent_py_agent.tests.test_subagent_listruns_cache import _selected_manager, _selected_task_file
from agent_py_agent.tests.test_telemetry_ledger_and_inspector import (
    _end_model_call,
    _model_request,
    call,
    call_runtime,
)

# ============================================================================
# Source: agent_py_agent/tests/test_cpack_w3_crash_restart.py
# ============================================================================

def test_initialize_failure_one_attempt_and_local_backoff(tmp_path, monkeypatch):
    client, _, _, _, starts, _ = component_client(tmp_path, monkeypatch)
    clock = [100.0]
    monkeypatch.setattr(plugin_runtime, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    original = client.start
    attempts = []
    def failed():
        attempts.append(1)
        raise MCPError("fixture initialize failure", code="MCP_TIMEOUT")
    monkeypatch.setattr(client, "start", failed)
    tool = client.pack_tools[0]
    try:
        first = tool.execute({})
        assert not first.ok and first.reported_error_code == "MCP_PACK_SERVER_UNAVAILABLE"
        assert first.effect_outcome == "not_started" and attempts == [1]
        assert client.retry_at == 101.0 and client.attempts == 1
        assert tool.precheck_availability().error_code == "MCP_PACK_SERVER_UNAVAILABLE"
        assert not tool.execute({}).ok and attempts == [1]
        clock[0] = 101.0
        monkeypatch.setattr(client, "start", original)
        assert tool.execute({}).ok and len(starts) == 1
        assert client.attempts == 0 and client.retry_at == 0 and client._in_flight == 0
    finally:
        client.stop()


def test_crash_waits_backoff_and_prepare_never_reconnects(tmp_path, monkeypatch):
    client, _, _, _, starts, _ = component_client(tmp_path, monkeypatch)
    clock = [100.0]
    monkeypatch.setattr(plugin_runtime, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    registry = pack_registry(tmp_path, None)
    registry._mcp_clients.append(client)
    try:
        tool = client.pack_tools[0]
        assert tool.execute({}).ok
        starts[0].revoke()
        registry.prepare_for_run()
        assert len(starts) == 1
        result = tool.execute({})
        assert result.reported_error_code == "MCP_PACK_SERVER_UNAVAILABLE"
        assert result.effect_outcome == "not_started" and len(starts) == 1
        clock[0] = client.retry_at
        assert tool.execute({}).ok and len(starts) == 2
        assert client.pack_tools[0].transport is None
    finally:
        registry.close_mcp_clients()


@pytest.mark.parametrize("pinned", [False, True])
def test_catalog_drift_is_permanent_and_process_is_retired(tmp_path, monkeypatch, pinned):
    client, _, _, _, starts, calls = component_client(tmp_path, monkeypatch, pinned=pinned)
    monkeypatch.setattr(client, "list_tools", lambda **kwargs: [MCPToolInfo("different", "不同工具", {})])
    try:
        result = client.pack_tools[0].execute({})
        assert not result.ok and result.reported_error_code == "MCP_PACK_CATALOG_DRIFT"
        assert result.effect_outcome == "not_started" and not calls
        assert client.drift and client.is_closed() and starts[0].stops >= 1
        assert not client.pack_tools[0].execute({}).ok and len(starts) == 1
        assert not client.pack_tools[0].availability().available
    finally:
        client.stop()


def test_pinned_comparison_uses_same_normalizer_not_raw_description(tmp_path, monkeypatch):
    client, _, _, _, _, _ = component_client(tmp_path, monkeypatch, pinned=True)
    original = client.list_tools
    monkeypatch.setattr(client, "list_tools", lambda **kwargs: [
        MCPToolInfo(tool.name, "  " + tool.description + "\n", tool.input_schema) for tool in original(**kwargs)])
    try:
        assert client.pack_tools[0].execute({}).ok and not client.drift
    finally:
        client.stop()


def test_business_error_and_sent_unknown_are_not_relabelled_not_started(tmp_path, monkeypatch):
    client, _, _, _, _, _ = component_client(tmp_path, monkeypatch)
    try:
        monkeypatch.setattr(client, "call_tool", lambda *args, **kwargs: {"content": "business rejected", "isError": True})
        failed = client.pack_tools[0].execute({})
        assert (
            not failed.ok
            and failed.error_code == "TOOL_RUNTIME_ERROR"
            and failed.reported_error_code == "MCP_REMOTE_ERROR"
            and failed.effect_outcome == "failed"
        )
        def unknown(*args, **kwargs):
            raise MCPError("sent timeout", code="MCP_TIMEOUT", effect_outcome="unknown")
        monkeypatch.setattr(client, "call_tool", unknown)
        uncertain = client.pack_tools[0].execute({})
        assert not uncertain.ok and uncertain.error_code == "TOOL_TIMEOUT" and uncertain.effect_outcome == "unknown"
        assert client._in_flight == 0
    finally:
        client.stop()


def test_unknown_cleanup_never_starts_replacement(tmp_path, monkeypatch):
    client, _, _, _, starts, _ = component_client(tmp_path, monkeypatch)
    clock = [100.0]
    monkeypatch.setattr(plugin_runtime, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    try:
        assert client.pack_tools[0].execute({}).ok
        starts[0].cleanup_confirmed = False
        starts[0].revoke()
        assert not client.pack_tools[0].execute({}).ok
        clock[0] = client.retry_at
        assert not client.pack_tools[0].execute({}).ok
        assert len(starts) == 1 and client._in_flight == 0
    finally:
        client.stop()


# ============================================================================
# Source: agent_py_agent/tests/test_cpack_w3_presentation_loading.py
# ============================================================================

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


# ============================================================================
# Source: agent_py_agent/tests/test_cpack_w3_revoke_fencing.py
# ============================================================================

def _revoke(store, entry):
    return store.change_activation(PluginActivationRequest(
        "disable", entry.revision, replace(entry.activation, phase="revoked"))).installation


def test_revoke_between_facade_fence_and_ensure_refuses_before_start(tmp_path, monkeypatch):
    client, store, entry, _, starts, calls = component_client(tmp_path, monkeypatch)
    tool = client.pack_tools[0]
    original = tool._activation_revoked
    def fence():
        revoked = original()
        _revoke(store, entry)
        return revoked
    monkeypatch.setattr(tool, "_activation_revoked", fence)
    try:
        result = tool.execute({})
        assert not result.ok and result.error_code == "TOOL_UNAVAILABLE"
        assert result.reported_error_code == "PLUGIN_ACTIVATION_UNAVAILABLE"
        assert result.effect_outcome == "not_started"
        assert not starts and not calls and client._in_flight == 0
    finally:
        client.stop()


def test_late_publication_after_revoke_never_resurrects_tools(tmp_path, monkeypatch):
    client, store, entry, _, starts, calls = component_client(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    original = client.discover_tools
    def discovered(transport):
        tools = original(transport)
        entered.set()
        assert release.wait(5)
        return tools
    monkeypatch.setattr(client, "discover_tools", discovered)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(client.pack_tools[0].execute, {})
            assert entered.wait(3)
            _revoke(store, entry)
            release.set()
            outcome = result.result(timeout=5)
        assert not outcome.ok and outcome.reported_error_code == "PLUGIN_ACTIVATION_UNAVAILABLE"
        assert outcome.effect_outcome == "not_started" and not calls
        assert not client.validated_tools and client._in_flight == 0 and starts[0].stops >= 1
    finally:
        release.set()
        client.stop()


def test_warm_display_cache_cannot_authorize_send_and_boundary_retires_old_client(tmp_path, monkeypatch):
    client, store, entry, owner, starts, calls = component_client(tmp_path, monkeypatch)
    registry = pack_registry(tmp_path, owner)
    registry._mcp_clients.append(client)
    tool = client.pack_tools[0]
    registry.tools[tool.model_spec.name] = tool
    try:
        assert tool.execute({}).ok
        _aged(owner)
        assert tool.availability().available and tool.precheck_availability().available
        _revoke(store, entry)
        sent = len(calls)
        outcome = tool.execute({})
        assert not outcome.ok and outcome.reported_error_code == "PLUGIN_ACTIVATION_UNAVAILABLE"
        assert outcome.effect_outcome == "not_started" and len(calls) == sent
        assert starts[0].stops == 0
        registry.prepare_for_run()
        assert client.is_closed() and starts[0].stops >= 1
        assert tool.model_spec.name not in registry.tools
        assert len(starts) == 1
    finally:
        registry.close_mcp_clients()


# ============================================================================
# Source: agent_py_agent/tests/test_r6_w2.py
# ============================================================================

@pytest.mark.parametrize("history", [100, 1000, 10000])
def test_current_reads_ignore_terminal_history(tmp_path, monkeypatch, record_property, history):
    manager = _selected_manager(tmp_path)
    index = manager.persistence.run_index
    with index.connect() as db:
        for number in range(history + 7):
            status = "RUNNING" if number < 7 else ("DONE", "FAILED", "BLOCKED", "TAKEN_OVER")[number % 4]
            _selected_task_file(manager, str(number), status)
            index.upsert(manager.load(str(number)), connection=db)
    calls, original = [], manager.persistence._cached_run_ref

    def observed(run_id, path):
        calls.append(run_id)
        return original(run_id, path)

    stats, original_stat = [], Path.stat

    def observed_stat(path, *args, **kwargs):
        if path.name in {"task.json", "canonical_state.json"}:
            stats.append(path)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(manager.persistence, "_cached_run_ref", observed)
    monkeypatch.setattr(Path, "stat", observed_stat)
    reader = getattr(manager, "list_recovery_runs_report", lambda: manager.list_runs_selected_report(lambda row: row.status == "RUNNING"))
    report = reader()
    assert not report.load_errors and {row.id for row in report.runs} == {str(i) for i in range(7)}
    assert len(calls) == 7
    assert len(manager.persistence._run_cache) <= manager.persistence._MAX_RUN_CACHE_ENTRIES
    assert len(stats) <= 7 * 6 and {path.parent.name for path in stats} == set(calls)
    record_property("canonical_stats", len(stats))
    calls.clear()
    agent = SimpleNamespace(subagents=manager)
    assert sweep.auto_start_stalled_orphans(agent)["started"] == 0
    assert sweep._reconcile_direct_parent_waits(agent)["direct_parent_waits_checked"] == 0
    assert len(calls) == 7
    ids, after = [], ""
    while True:
        page, after = index.recovery_ids(after=after, limit=3)
        assert len(page) <= 3
        ids.extend(page)
        if not after:
            break
    assert len(ids) == len(set(ids)) == 7


@pytest.mark.parametrize("status", ["DONE", "FAILED", "BLOCKED", "TAKEN_OVER"])
def test_terminal_delivery_exits_candidates_only_after_ack(tmp_path, status):
    manager = _selected_manager(tmp_path)
    task = manager.create_run(goal="真实未结交付")
    pending, _, _, _ = _pending_task(tmp_path, task.id)
    task.status, task.attributes = status, pending.attributes
    manager.save(task)
    assert [row.id for row in managers.select_recovery_runs(manager)] == [task.id]
    report = runtime_closeout.recover_pending_closeouts(
        None, load_task=manager.load, save_task=manager.save,
        list_tasks=lambda: managers.select_recovery_runs(manager), notify_result=lambda *a, **k: "delivered",
    )
    assert report["runtime_closeouts_recovered"] == 1 and managers.select_recovery_runs(manager) == []
    assert manager.load(task.id).status == status


@pytest.mark.parametrize("damage", ["missing", "corrupt", "generation", "canonical"])
def test_index_unknown_never_becomes_healthy_empty(tmp_path, monkeypatch, damage):
    manager = _selected_manager(tmp_path)
    task = manager.create_run(goal="索引未知保持可见")
    service, index = manager.persistence, manager.persistence.run_index
    monkeypatch.setattr(service, "_start_run_index_maintenance", lambda: None)
    if damage == "missing":
        index.path.unlink()
    elif damage == "corrupt":
        index.path.write_bytes(b"not a sqlite database")
    elif damage == "generation":
        with index.connect() as db:
            db.execute("UPDATE meta SET value=2 WHERE key='schema'")
    else:
        (manager.workspace / task.id / "task.json").write_text("{broken")
    assert service.list_recovery_runs_report().load_errors
    with pytest.raises(managers.SubagentRunIndexUnavailable):
        managers.select_recovery_runs(manager)
    if damage in {"missing", "generation"}:
        assert service.backfill_run_index(batch_size=1)["complete"]
        assert [row.id for row in managers.select_recovery_runs(manager)] == [task.id]


def test_background_lineage_uses_exact_scope_and_reports_bad_canonical(tmp_path, monkeypatch):
    manager = _selected_manager(tmp_path)
    root = manager.create_run(goal="root")
    child = manager.create_run(goal="child", root_id=root.id, parent_id=root.id)
    manager.create_run(goal="other")
    state = BackgroundContextLoad(agent=SimpleNamespace(subagents=manager), store=None, thread=None,
                                  task_id=child.id, config=None, policy_request=None, load_errors=[])
    monkeypatch.setattr(manager, "list_run_summaries", lambda: pytest.fail("historical scan"))
    assert _task_context_ids(state) == {root.id, child.id} and not state.load_errors
    (manager.workspace / root.id / "task.json").write_text("{broken")
    # 同一次准备冻结范围；下一次准备才读取后来发生的canonical损坏。
    state = BackgroundContextLoad(agent=state.agent, store=None, thread=None, task_id=child.id,
                                  config=None, policy_request=None, load_errors=[])
    assert _task_context_ids(state) == {root.id, child.id} and state.load_errors


def test_old_generation_backfill_restores_terminal_closeout_membership(tmp_path, monkeypatch):
    manager = _selected_manager(tmp_path)
    task = manager.create_run(goal="旧投影重建终态待交付")
    pending, _, _, _ = _pending_task(tmp_path, task.id)
    task.status, task.attributes = "TAKEN_OVER", pending.attributes
    manager.save(task)
    service, index = manager.persistence, manager.persistence.run_index
    monkeypatch.setattr(service, "_start_run_index_maintenance", lambda: None)
    with index.connect() as db:
        db.execute("DELETE FROM membership WHERE kind='closeout'")
        db.execute("INSERT INTO inventory(run_id,done) VALUES (?,1)", (task.id,))
        db.execute("UPDATE meta SET value=2 WHERE key='schema'")
    assert service.list_recovery_runs_report().load_errors
    assert service.backfill_run_index(batch_size=1)["complete"]
    assert [row.id for row in managers.select_recovery_runs(manager)] == [task.id]


def test_pending_canonical_commit_uses_existing_exact_repair(tmp_path):
    manager = _selected_manager(tmp_path)
    task = manager.create_run(goal="中断的索引提交")
    index = manager.persistence.run_index
    index.mark_pending(task.id, task=task)
    with pytest.raises(managers.SubagentRunIndexUnavailable):
        index.recovery_ids(limit=1)
    assert [row.id for row in managers.select_recovery_runs(manager)] == [task.id]
    assert index.coverage()["pending"] == 0


def test_ended_parent_wait_clears_proof_without_reopening_parent(tmp_path):
    from agent_py_agent.agent.subagents.direct_parent_lifecycle import (
        mark_parent_waiting_for_direct_children,
        parent_wait_blocks_dispatch,
    )

    manager = _selected_manager(tmp_path)
    parent = manager.create_run(goal="parent")
    child = manager.create_run(goal="child", parent_id=parent.id, root_id=parent.id)
    assert mark_parent_waiting_for_direct_children(manager, parent.id, [child.id]) == (child.id,)
    parent = manager.load(parent.id)
    parent.status = "DONE"
    manager.save(parent)
    report = sweep._reconcile_direct_parent_waits(SimpleNamespace(subagents=manager))
    assert report["direct_parent_waits_checked"] == 1 and report["direct_parent_waits_released"] == 0
    assert manager.load(parent.id).status == "DONE" and not parent_wait_blocks_dispatch(manager.load(parent.id))
    assert [row.id for row in managers.select_recovery_runs(manager)] == [child.id]


def test_terminal_source_link_is_acknowledged_before_candidate_retirement(tmp_path, monkeypatch):
    from agent_py_agent.agent.agent_core.orchestration import lifecycle
    from agent_py_agent.agent.ingestion import source_worker
    from agent_py_agent.agent.ingestion.watch_state import persist_state

    agent, owner_home = _agent(tmp_path)
    _bind_audit_workspace(agent, owner_home, "audit-source-test", tmp_path)
    watch = _watch(owner_home, "audit-source-test", 1)
    monkeypatch.setattr(lifecycle, "auto_start_tasks", lambda *a, **k: {"status": "not_needed", "run_ids": []})
    worker_id = source_worker.ensure_audit_source_worker(agent, watch)["run_id"]
    task = agent.subagents.load(worker_id)
    task.status, task.verification_status = "DONE", "VERIFIED"
    agent.subagents.save(task)
    parent = agent.conversation_store.tasks.load_report("audit-source-test")[0]
    agent.conversation_store.tasks.bind({"task_id": task.id, "thread_id": parent.thread_id, "status": "active"})
    watch.opened_at, watch.watch_window_seconds, watch.window_finalized_at = 1.0, 1, 2.0
    persist_state(watch)
    assert [row.id for row in managers.select_recovery_runs(agent.subagents)] == [task.id]
    assert sweep._reconcile_conversation_parent_lifecycle(agent)["source_workers_completed"] == 1
    assert agent.conversation_store.tasks.load_report(task.id)[0].status == "completed"
    sweep._reconcile_conversation_parent_lifecycle(agent)
    assert managers.select_recovery_runs(agent.subagents) == []
    assert agent.subagents.load(task.id).status == "DONE"


# ============================================================================
# Source: agent_py_agent/tests/test_r6_w3.py
# ============================================================================

def test_ten_thousand_closed_groups_match_untrimmed_reference():
    now = [100.0]
    context = ModelCallLedgerContext(now=lambda: now[0])
    ledger = ModelCallLedger(context=context)
    reference = ModelCallLedger(ModelCallLedgerOptions(max_records=30000), context)
    for target in (ledger, reference):
        _start(target, "old-attempt", logical_call_id="still-retryable")
        target.finished(ModelCallFinishParams("old-attempt", output_tokens=1))
    for turn in range(10000):
        purpose = ("main", "auxiliary", "decision", "probe:tool_capability")[turn % 4]
        identity = dict(request_id="request", run_id="run", purpose=purpose, logical_call_id=f"turn-{turn}")
        for attempt in range(1 + turn % 3):
            call_id = f"{turn}:{attempt}"
            for target in (ledger, reference):
                _start(target, call_id, purpose=purpose, auxiliary=purpose == "auxiliary", logical_call_id=identity["logical_call_id"])
            now[0] += 1
            for target in (ledger, reference):
                target.first_token(ModelCallFirstTokenParams(call_id, 1))
            now[0] += 1
            for target in (ledger, reference):
                target.finished(ModelCallFinishParams(call_id, input_tokens=77, output_tokens=attempt, cached_input_tokens=5))
        ledger.close_logical_call(**identity)
        ledger.close_logical_call(**identity)
    for aggregate in ledger._scope_aggregates.values():
        assert aggregate.logical_call_counts == {("main", "still-retryable"): 1}
    for target in (ledger, reference):
        _start(target, "late-retry", logical_call_id="still-retryable")
        target.finished(ModelCallFinishParams("late-retry", output_tokens=1))
    ledger.close_logical_call(request_id="request", run_id="run", purpose="main", logical_call_id="still-retryable")
    for scope in ({"request_id": "request"}, {"run_id": "run"}):
        actual, expected = ledger.cumulative_summary(**scope), reference.cumulative_summary(**scope)
        actual.pop("usage_scope_id"), expected.pop("usage_scope_id")
        assert actual == expected
    assert len(ledger.records()) == 128
    assert all(not bucket.logical_call_counts for aggregate in ledger._scope_aggregates.values()
               for bucket in (aggregate, *aggregate.purpose_totals.values()))


@pytest.mark.parametrize("late", [("first_token", "finished", "timed_out"), ("timed_out", "first_token", "finished")])
def test_close_keeps_late_worker_residency_and_terminal_accounting(late):
    ledger = ModelCallLedger(ModelCallLedgerOptions(max_records=1))
    _start(ledger, logical_call_id="group")
    token = ledger.retain_call("call")
    ready, report, observed, exit_worker = (Event() for _ in range(4))
    errors = []

    def worker():
        try:
            with token:
                ready.set()
                assert report.wait(5)
                for event in late:
                    _event(ledger, event)
                observed.set()
                assert exit_worker.wait(5)
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=worker)
    thread.start()
    try:
        assert ready.wait(5)
        ledger.close_logical_call(request_id="request", run_id="run", purpose="main", logical_call_id="group")
        report.set()
        assert observed.wait(5)
        _other_scoped_calls(ledger)
        assert "call" in ledger._retained_calls and thread.is_alive()
        summary = ledger.cumulative_summary(run_id="run")
        assert summary["logical_model_turn_count"] == summary["physical_model_attempt_count"] == 1
        assert summary["status_counts"]["finished" if late[0] == "first_token" else "timed_out"] == 1
        assert not ledger._scope_aggregates[("run", "run")].logical_call_counts
    finally:
        report.set()
        exit_worker.set()
        thread.join(5)
    assert not thread.is_alive() and not errors and not ledger._retained_calls
    assert [record.call_id for record in ledger.records()] == ["other-3"]


@pytest.mark.parametrize("incomplete", [False, True])
def test_real_outer_retry_closes_only_after_complete_response(tmp_path, monkeypatch, incomplete):
    monkeypatch.setattr(provider_transient_auto_resume, "wait_interruptibly", lambda _: None)
    agent = SimpleAgent(AgentConfig(enable_tools=False, memory_path="memory.jsonl"), tmp_path)
    agent.backend = _UsageStreamIncompleteThenOkBackend(with_usage=True) if incomplete else _TransientThenOkBackend(failures=1)
    agent.run("完成一次调用", save=False)
    _assert_single_logical_turn(agent, backend_name=agent.backend.name, expect_calibration=incomplete)
    ledger = model_call_ledger(agent)
    assert all(not bucket.logical_call_counts for aggregate in ledger._scope_aggregates.values()
               for bucket in (aggregate, *aggregate.purpose_totals.values()))
    assert all(not aggregate.retry_logical_call_ids for aggregate in ledger._scope_aggregates.values())
    identity = dict(request_id="request", run_id="run", purpose="main", identity="same-input")
    first = ledger.open_logical_call(**identity)
    assert ledger.open_logical_call(**identity) == first
    ledger.close_logical_call(request_id="request", run_id="run", purpose="main", logical_call_id=first)
    assert ledger.open_logical_call(**identity) != first


# ============================================================================
# Source: agent_py_agent/tests/test_r6_w4.py
# ============================================================================

@pytest.fixture
def task(tmp_path):
    inspector = inspector_module.TaskCallInspector(tmp_path, owner_id="one")
    inspector.upsert_task_call(dict(task_id="t", started_at=1))
    return inspector


def _row(identity):
    return dict(round_id=identity, run_id="run", prompt_tokens=1, completion_tokens=2)


@pytest.mark.parametrize("count", [100, 1000, 10000])
def test_append_io_is_independent_of_round_history(task, monkeypatch, count):
    task.upsert_task_call(dict(task_id="t", rounds=[_row(str(i)) for i in range(count)]))
    counters = dict(read=0, written=0)
    read, pread, write = os.read, os.pread, os.write

    def counted_read(*args):
        data = read(*args)
        counters["read"] += len(data)
        return data

    def counted_pread(*args):
        data = pread(*args)
        counters["read"] += len(data)
        return data

    def counted_write(fd, data):
        size = write(fd, data)
        counters["written"] += size
        return size

    with monkeypatch.context() as patch:
        patch.setattr(os, "read", counted_read)
        patch.setattr(os, "pread", counted_pread)
        patch.setattr(os, "write", counted_write)
        patch.setattr(storage, "read_task_rounds", lambda *a: pytest.fail("append hydrated history"))
        patch.setattr(storage, "OwnerReadIndex", lambda *a: pytest.fail("append opened read projection"))
        assert task.append_round("t", _row("last"), include_rounds=False) is None
    assert counters["read"] < 4096 and counters["written"] < 4096, counters
    metadata = json.loads((task._home / "data/telemetry/task_calls/t.json").read_text())
    assert metadata["rounds"] == [] and metadata["token_usage"]["total_tokens"] == (count + 1) * 3
    detail = task.get_task_call_detail("t")
    assert len(detail["rounds"]) == count + 1 and detail["rounds_coverage"]["status"] == "complete"


def test_full_detail_follows_real_prefix_verification_progress(task, monkeypatch):
    from agent_py_agent.agent.local_storage import owner_read_index as index_module

    task.upsert_task_call(dict(task_id="t", rounds=[_row(str(i)) for i in range(100)]))
    task.append_round("t", _row("delta"), include_rounds=False)
    original = index_module._Budget

    def small_budget(*args, **kwargs):
        return original(max_bytes=1024)

    monkeypatch.setattr(index_module, "_Budget", small_budget)
    detail = task.get_task_call_detail("t")
    assert detail["rounds_coverage"]["status"] == "complete", detail["rounds_coverage"]
    assert len(detail["rounds"]) == 101


def test_metadata_failure_is_partial_and_replay_is_exact(task, monkeypatch):
    task.append_round("t", _row("seed"), include_rounds=False)
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            raise OSError("single metadata commit interruption")
        patch.setattr(storage, "_write_metadata", fail)
        with pytest.raises(OSError):
            task.append_round("t", _row("pending"), include_rounds=False)
    detail = task.get_task_call_detail("t")
    assert detail["rounds_coverage"]["status"] == "partial" and len(detail["rounds"]) == 2
    assert detail["token_usage"]["total_tokens"] == 6
    task.append_round("t", _row("pending"), include_rounds=False)
    detail = task.get_task_call_detail("t")
    assert len(detail["rounds"]) == 2 and detail["rounds_coverage"]["status"] == "complete"
    assert detail["token_usage"]["total_tokens"] == 6


def test_first_metadata_commit_failure_is_visible_and_retryable(tmp_path, monkeypatch):
    task = inspector_module.TaskCallInspector(tmp_path, owner_id="one")
    payload = dict(task_id="t", rounds=[_row("old")])
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            raise OSError("single first metadata interruption")
        patch.setattr(inspector_module, "write_text_atomic_beneath", fail)
        with pytest.raises(OSError):
            task.upsert_task_call(payload)
    detail = task.get_task_call_detail("t")
    assert detail["rounds_coverage"]["reason"] == "task_metadata_missing" and detail["status"] is None
    task.upsert_task_call(payload)
    assert [row["round_id"] for row in storage.read_task_rounds(tmp_path, "t")] == ["old"]
    assert task.get_task_call_detail("t")["rounds_coverage"]["status"] == "complete"


@pytest.mark.parametrize("complete", [False, True])
def test_one_torn_append_recovers_without_duplicate(task, complete):
    task.append_round("t", _row("seed"), include_rounds=False)
    path = task._home.joinpath(*storage._parts("t"))
    generation, _ = storage._snapshot(task._home, "t")
    tail = (_encode(storage._frame(generation, 2, _row("pending"))).encode().rstrip(b"\n")
            if complete else b'{"row":"cut\xe4')
    with path.open("ab") as stream:
        stream.write(tail)
    task.append_round("t", _row("pending"), include_rounds=False)
    rows = storage.read_task_rounds(task._home, "t")
    assert [row["round_id"] for row in rows] == ["seed", "pending"]
    sidecars = list(path.parent.glob(path.name + ".torn-*"))
    assert len(sidecars) == (0 if complete else 1)
    if sidecars:
        assert sidecars[0].read_bytes() == tail and sidecars[0].stat().st_mode & 0o777 == 0o600


def test_interrupted_legacy_migration_reuses_generation(task, monkeypatch):
    legacy = inspector_module.TaskCallRecord("t", owner_id="one", rounds=[_row("old")]).to_dict()
    metadata = task._home / "data/telemetry/task_calls/t.json"
    metadata.write_text(_encode(legacy))
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            raise OSError("single migration marker interruption")
        patch.setattr(inspector_module, "write_text_atomic_beneath", fail)
        with pytest.raises(OSError):
            task.append_round("t", _row("new"), include_rounds=False)
    generation, before = storage._snapshot(task._home, "t")
    assert task.get_task_call_detail("t")["rounds_coverage"]["status"] == "partial"
    task.append_round("t", _row("new"), include_rounds=False)
    for _ in range(2):
        task.upsert_task_call(dict(task_id="t", title="metadata only"))
    assert storage._snapshot(task._home, "t")[0] == generation
    assert storage._snapshot(task._home, "t")[1][:2] == before[:2]
    assert [row["round_id"] for row in storage.read_task_rounds(task._home, "t")] == ["old", "new"]


def test_public_roundtrip_redaction_scope_and_terminal_state(task):
    storage.append_task_round(task._home, "t", {**_row("direct"), "note": "password=dummy-private"})
    result = task.append_round("t", _row("default"))
    assert [row["round_id"] for row in result["rounds"]] == ["direct", "default"]
    assert len(result["runs"]) == 1
    task.complete_task_call("t", "cancelled", ended_at=2)
    task.append_round("t", _row("after"), include_rounds=False)
    assert task.get_task_call_detail("t")["status"] == "cancelled"
    assert "dummy-private" not in task._home.joinpath(*storage._parts("t")).read_text()
    with pytest.raises(ValueError, match="owner mismatch"):
        inspector_module.TaskCallInspector(task._home, owner_id="other").append_round("t", _row("wrong"))


def test_round_source_symlink_is_rejected(task, tmp_path):
    task.append_round("t", _row("seed"), include_rounds=False)
    path = task._home.joinpath(*storage._parts("t"))
    outside = tmp_path / "outside"
    outside.write_text("unchanged")
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(OSError):
        task.append_round("t", _row("rejected"), include_rounds=False)
    with pytest.raises(OSError):
        storage.read_task_rounds(task._home, "t")
    assert outside.read_text() == "unchanged"


def test_public_append_reuses_owner_lock_for_concurrent_writers(task):
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda identity: storage.append_task_round(task._home, "t", _row(str(identity))), range(12)))
    detail = task.get_task_call_detail("t")
    assert len({row["round_id"] for row in detail["rounds"]}) == 12
    assert detail["token_usage"]["total_tokens"] == 36


@pytest.mark.parametrize("change", [dict(prompt_tokens=-1), dict(duration_ms=float("nan")), dict(prompt_tokens=2**63)])
def test_invalid_round_is_rejected_before_metadata_or_migration(task, change):
    metadata = task._home / "data/telemetry/task_calls/t.json"
    before = metadata.read_bytes()
    for task_id in ("t", "new"):
        with pytest.raises(ValueError):
            task.append_round(task_id, change, create_if_missing=True, include_rounds=False)
    assert metadata.read_bytes() == before
    assert not task._home.joinpath(*storage._parts("t")).exists()
    assert not (metadata.parent / "new.json").exists()


def test_model_callback_alone_disables_round_hydration(tmp_path, monkeypatch):
    request, _ = _model_request(tmp_path)
    options = []
    original = inspector_module.TaskCallInspector.append_round

    def observed(self, *args, **kwargs):
        options.append(kwargs)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(inspector_module.TaskCallInspector, "append_round", observed)
    ledger, call_id, _ = call_runtime.start_model_call_record(
        request, context_snapshot=SimpleNamespace(current_tokens=17, to_public_dict=lambda: {}),
    )
    _end_model_call(ledger, call_id, "finished")
    assert options == [dict(create_if_missing=True, include_rounds=False)]
    assert not getattr(request.agent.config, "llm_call_records_enabled", False)
    assert len(inspector_module.TaskCallInspector(tmp_path, owner_id="one").get_task_call_detail("model-task")["rounds"]) == 1


def test_first_background_round_never_rebuilds_historical_task_index(task, monkeypatch):
    (task._home / "data/telemetry/task_calls_index.jsonl").unlink()

    def forbid_scan(*args, **kwargs):
        pytest.fail("background creation traversed task history")

    with monkeypatch.context() as patch:
        patch.setattr(inspector_module, "list_names_beneath", forbid_scan)
        patch.setattr(inspector_module.TaskCallInspector, "_write_index", forbid_scan)
        assert task.append_round("new", _row("new"), create_if_missing=True, include_rounds=False) is None
    assert len(task.get_task_call_detail("new")["rounds"]) == 1


def test_short_recovery_read_never_truncates_committed_round(task, monkeypatch):
    task.append_round("t", _row("seed"), include_rounds=False)
    path = task._home.joinpath(*storage._parts("t"))
    generation, _ = storage._snapshot(task._home, "t")
    with path.open("ab") as stream:
        stream.write(_encode(storage._frame(generation, 2, _row("pending"))).encode())
    before, pread = path.read_bytes(), os.pread

    def short_read(fd, count, offset):
        data = pread(fd, count, offset)
        return data[:-1] if offset else data

    with monkeypatch.context() as patch:
        patch.setattr(os, "pread", short_read)
        with pytest.raises(OSError, match="short task round recovery read"):
            task.append_round("t", _row("pending"), include_rounds=False)
    assert path.read_bytes() == before and list(path.parent.glob(path.name + ".torn-*")) == []


def test_preexisting_owner_index_gets_only_missing_domain(task):
    ledger = ToolTelemetryLedger(task._home)
    ledger.record_tool_call(call())
    assert len(ledger.query_tool_calls()) == 1
    with sqlite3.connect(task._home / "cache/owner_read_index.sqlite3") as connection:
        connection.execute("DROP TABLE ori_task_rounds")  # 临时 fixture 还原升级前投影布局。
    task.append_round("t", _row("first"), include_rounds=False)
    assert len(task.get_task_call_detail("t")["rounds"]) == 1
    assert len(ledger.query_tool_calls()) == 1


@pytest.mark.parametrize("quota_bytes", [100 * 1024**2, 1024**3, 100 * 1024**3])
def test_existing_runtime_quota_policy_covers_new_sources(task, quota_bytes):
    from agent_py_agent.agent.user_space import owner_quota as quota

    enforcer = quota.OwnerQuotaEnforcer(task._home, max_bytes=quota_bytes)
    payload = task._home / "payload"
    with enforcer.reserve([quota.OwnerQuotaChange(payload, 1)]):
        payload.write_bytes(b"x")
    task.append_round("t", _row("first"), include_rounds=False)
    assert len(task.get_task_call_detail("t")["rounds"]) == 1
    # 原策略将 telemetry 子树和 owner_read_index 缓存列为宿主运行时；配额范围不随本修复改变。
    assert quota.owner_logical_usage_bytes(task._home, exact=True) == 1


# ============================================================================
# Source: agent_py_agent/tests/test_r6_w5.py
# ============================================================================

def test_current_two_indexes_have_constant_filesystem_work(tmp_path, record_property):
    owner, task = tmp_path / "owner", tmp_path / "owner/runs/live/current/work"
    paths = (_history(owner, 1), _history(task, 1))
    marks = tool_output_paths.tool_output_index_marks(owner, index_paths=paths)
    for root in (owner, task):
        _externalize(root, SCOPE, "now")
    counts = []
    for history in (0, 100, 1000, 10000):
        for number in range(history):
            (owner / "runs/history" / str(number) / "work/blobs/tool_outputs").mkdir(parents=True, exist_ok=True)
        with (patch.object(Path, "glob", side_effect=AssertionError("historical discovery")),
              patch.object(Path, "resolve", autospec=True, side_effect=Path.resolve) as resolved,
              patch.object(os, "stat", wraps=os.stat) as stated,
              patch.object(os, "scandir", wraps=os.scandir) as enumerated):
            assert len(refs.tool_output_source_refs(owner, SCOPE, marks=marks, index_paths=paths)) == 2
        counts.append((resolved.call_count, stated.call_count, enumerated.call_count))
    assert counts == [counts[0]] * len(counts)
    assert counts[0][2] == 0
    record_property("current_index_filesystem_counts", json.dumps(dict(zip((0, 100, 1000, 10000), counts))))


@pytest.mark.parametrize("mutation", ["append", "replace", "truncate", "unaligned"])
def test_explicit_sources_keep_verified_offsets_and_attempt_identity(tmp_path, mutation):
    index = _history(tmp_path, 10)
    marks = _mark(index, size_delta=-1 if mutation == "unaligned" else 0)
    if mutation == "replace":
        replacement = index.with_suffix(".replacement")
        replacement.write_bytes(index.read_bytes())
        replacement.replace(index)
    if mutation == "truncate":
        index.write_bytes(b"")
    for attempt in ("first", "second"):
        _externalize(tmp_path, {**SCOPE, "attempt_id": attempt, "turn_id": "turn"}, "same-call")
    assert len(refs.tool_output_source_refs(tmp_path, SCOPE, marks=marks, index_paths=(index,))) == 2
    carried = refs.carried_tool_call_records(tmp_path, SCOPE, index_paths=(index,))
    assert [row["attempt_id"] for row in carried] == ["first", "second"]
    with pytest.raises(FileNotFoundError):
        refs.tool_source_refs(tmp_path, SCOPE, index_paths=(index.with_name("missing.jsonl"),))


def test_runtime_bundle_keeps_task_source_and_first_window_through_compactions(tmp_path):
    home = ensure_my_agent_home(tmp_path / "home")
    owner, task_root = home.owner_home_dir, home.owner_home_dir / "runs/arbitrary/physical-location"
    task = task_root / "work"
    _history(task, 2)
    agent = SimpleNamespace(root=owner, home_paths=home, config=SimpleNamespace(auto_save_memory=True),
                            _current_run_task_workspace=str(task_root))
    request = RuntimeContextRequest("继续", None, None, **SCOPE, task_attributes={})
    snapshot = ToolRuntimeSnapshot(run_id=SCOPE["run_id"], runtimes=(), available_tool_names=frozenset(),
                                   unavailable_tools=(), allowed_tools=None)
    options = dict(memories=[], runtime_injections=[], routed_context=SimpleNamespace(),
                   resume_context_injected=False, task_local=False, tool_runtime_snapshot=snapshot)
    first = build_runtime_main_context_bundle(agent, request, **options)
    marks = first.bundle["tool_index_marks"]
    assert set(marks) == {str(tool_output_paths.tool_output_root(root) / "index.jsonl") for root in (owner, task)}
    for attempt in ("first", "second"):
        _externalize(task, {**SCOPE, "attempt_id": attempt, "turn_id": "turn"}, "same-call")
    _externalize(task, {**SCOPE, "request_id": "foreign"}, "other-scope")
    assert _update(owner, Path(first.json_path))["artifact_count"] == 2
    second = build_runtime_main_context_bundle(agent, request, **options)
    assert second.bundle["tool_index_marks"] == marks
    payload = json.loads(Path(second.json_path).read_text())
    with patch.object(bundles, "read_json_object_report", return_value=SimpleNamespace(payload=payload)):
        for _ in range(10000):
            carried = bundles._carried_tool_index_window(second.bundle, Path(second.json_path))
            assert carried["tool_index_marks"] == marks and carried["tool_index_sources_complete"] is True
    assert len(_tool_index_window(Path(second.json_path), carried)["index_paths"]) == 2
    Path(second.json_path).write_text("{")
    third = build_runtime_main_context_bundle(agent, request, **options)
    with patch.object(Path, "glob", side_effect=AssertionError("historical fallback")):
        assert _update(owner, Path(third.json_path))["status"] == "incomplete_tool_index_sources"


def test_absent_owner_candidate_keeps_task_records_without_incomplete_history(tmp_path):
    task = tmp_path / "runs/arbitrary/task/work"
    _externalize(task, SCOPE, "task-only")
    sources = (refs.CarriedIndexSource(tmp_path, "owner_index"), refs.CarriedIndexSource(task, "task_index"))
    read = refs.carried_tool_call_records_for_requests(sources, [SCOPE["request_id"]])
    assert [row["call_id"] for row in read.records] == ["task-only"]
    assert read.unreadable_sources == ()


def test_closeout_keeps_host_bound_task_promotion_in_the_same_turn(tmp_path):
    _history(tmp_path, 1)
    bundle = bundles.build_main_context_bundle(bundles.MainContextBundleRequest(
        root=tmp_path, home_paths=ensure_my_agent_home(tmp_path / "home"), user_prompt="继续", **SCOPE,
        tool_index_sources=(refs.CarriedIndexSource(tmp_path, "owner_index"),),
    ))
    promoted_scope = {**SCOPE, "task_id": "promoted-task"}
    _externalize(tmp_path, promoted_scope, "promoted-call")
    result = update_main_context_bundle_artifacts(MainContextBundleArtifactUpdateRequest(
        context_bundle_path=bundle.json_path, workspace_root=tmp_path, **promoted_scope,
    ))
    assert result["ok"] is True and result["artifact_count"] == 1
