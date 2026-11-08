"""真实索引、canonical、只读 Store 与本机 HTTP 验证被动投影的范围和未知状态。"""
import copy
import json
import os
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.agent_core.tool_loop.completion import _delegated_work_facts
from agent_py_agent.agent.common.global_write_commit import global_write_commit_scope
from agent_py_agent.agent.conversation.agent_activity import conversation_agent_activity
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts import control_service
from agent_py_agent.agent.gateway_parts.http_service import (
    GatewayHTTPServer,
    GatewayHTTPServerParams,
)
from agent_py_agent.agent.gateway_parts.paths import gateway_paths_from_root
from agent_py_agent.agent.plugin_host import subagents_v2_handlers as plugin
from agent_py_agent.agent.plugin_host.errors import HostAPIError
from agent_py_agent.agent.runtime_errors import DataCorruptionError
from agent_py_agent.agent.settings.config import AgentConfig
from agent_py_agent.agent.subagents.manager import SubAgentManager
from agent_py_agent.agent.subagents.services.persistence.run_index import SubagentRunIndex
from agent_py_agent.agent.subagents.services.persistence.service import SubAgentPersistenceService
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.owner_resolver import (
    OwnerIdentity,
    ensure_owner_home,
    home_paths_with_owner,
)
from agent_py_agent.agent.user_space.runtime_paths import runtime_paths_for_agent
from agent_py_agent.cli.chat_client_context import GatewayChatClientAgent
from agent_py_agent.tests.test_local_store_coordinator_io import make_store
from agent_py_agent.tests.test_r7_a_w2 import _task
from agent_py_agent.tests.test_runtime_guidance import _tool_loop_params


@pytest.fixture
def case(tmp_path):
    root = tmp_path / "home"
    owner = ensure_owner_home(root, OwnerIdentity.local_main())
    paths = home_paths_with_owner(home_paths(root), owner)
    project = owner.home_dir / "project"
    project.mkdir()
    runtime = runtime_paths_for_agent(SimpleNamespace(), project, paths)
    local, door = make_store(runtime["local_store_path"].parent, enable_fts=False)
    store = ConversationStore(owner.data_dir / "conversations")
    thread = store.threads.get_or_create({"channel": "chat", "channel_conversation_id": "session-live",
        "channel_user_id": "local-agent", "owner_id": owner.owner_id, "owner_home": str(owner.home_dir),
        "workspace_root": str(project), "runtime_workspace_roots": [str(project)], "now": 1.0})
    store.tasks.bind({"thread_id": thread.thread_id, "task_id": "task-live", "goal": "核对当前工作", "now": 2.0})
    manager = SubAgentManager(runtime["subagent_workspace"], local_store=local, conversation_store=store,
        workspace_root=project, owner_id=owner.owner_id, owner_home_dir=str(owner.home_dir),
        execution_mode="LOCAL_UNMANAGED")
    with global_write_commit_scope(door):
        child = manager.create_run(goal="直属工作", parent_id="task-live", root_id="task-live", depth=1,
            owner=owner.owner_id, attributes={"conversation_thread_id": thread.thread_id})
        grandchild = manager.create_run(goal="下级工作", parent_id=child.id, root_id="task-live", depth=2,
            owner=owner.owner_id, attributes={"conversation_thread_id": thread.thread_id})
    config = AgentConfig(my_agent_home=str(root), model_backend="echo", enable_tools=False)
    agent = SimpleNamespace(config=config, root=project, home_paths=paths, conversation_store=store,
        subagents=manager, subagent_run_ids_for_request=lambda key: SubagentRunIndex(
            manager.workspace, read_only=True).request_ids((key,), "", None))
    context = SimpleNamespace(owner_home=owner, identity=owner.identity)
    with closing(sqlite3.connect(local.db_path)) as keeper:
        keeper.execute("SELECT count(*) FROM legacy_agent_runs").fetchone()
        yield SimpleNamespace(owner=owner, paths=paths, local=local, door=door, store=store, thread=thread,
            manager=manager, agent=agent, child=child, grandchild=grandchild, context=context, keeper=keeper)


def _observe(case, monkeypatch):
    scans, reads, copies = [], [], []
    original_load, original_copy = SubAgentPersistenceService.load, copy.deepcopy
    original_report = SubAgentPersistenceService.list_runs_report

    def full_report(service):
        scans.append(True)
        return original_report(service)

    def load(service, run_id):
        reads.append(run_id)
        return original_load(service, run_id)

    def clone(value, memo=None):
        if hasattr(value, "id"):
            copies.append(value.id)
        return original_copy(value, memo)

    monkeypatch.setattr(SubAgentPersistenceService, "list_runs_report", full_report)
    monkeypatch.setattr(SubAgentPersistenceService, "load", load)
    monkeypatch.setattr(copy, "deepcopy", clone)
    return scans, reads, copies


@pytest.mark.parametrize("history", [100, 1000, 10000])
def test_real_notice_and_status_wire_only_read_selected_ids(case, monkeypatch, record_property, history):
    index = case.manager.persistence.run_index
    with index.connect() as db:
        for number in range(history):
            task = _task(case.manager, f"history-{number}", "DONE", root="other-root", thread="other-thread")
            index.upsert(task, connection=db)
    scans, reads, copies = _observe(case, monkeypatch)
    calls = case.door.calls
    server = GatewayHTTPServer(0, gateway_paths_from_root(case.paths.root / "gateway"),
        params=GatewayHTTPServerParams(agent=case.agent))
    server.start()
    try:
        client_config = AgentConfig(my_agent_home=str(case.paths.root), gateway_port=server.server.server_address[1])
        client = GatewayChatClientAgent(None, client_config, case.agent.root, [case.agent.root], case.paths)
        notice = client.request_background_notices("session-live", after=0)
        assert notice["binding_state"] == "confirmed" and notice["snapshot_ok"] is True, notice
        activity = notice["agent_activity"]
        assert activity["subagent_projection_ok"] is True
        assert [row["run_id"] for row in activity["subagents"]] == [case.child.id]
        counts = control_service._subagent_status(case.agent, ["task-live"])
        assert (counts.total, counts.running) == (2, 2)
        # 同一真实客户端发送 /status，保留原控制结果信封及客户端 JSON 解析。
        status, body = client.post_gateway_json("/control", {"command": "/status", "conversation_id": "session-live",
            "metadata": {"message_id": "status-wire"}}, timeout=10)
        assert status == 200 and body["ok"] is True, body
        assert body["task_status"]["subagent_total"] == 2, body
        record_property("healthy_canonical_load_attempts", len(reads))
        record_property("healthy_canonical_copies", len(copies))
        record_property("healthy_history_scans", len(scans))
        assert set(reads) <= {"task-live", case.child.id, case.grandchild.id} and not scans
        assert set(copies) <= {case.child.id, case.grandchild.id}
        assert case.door.calls == calls
        # 单一真实缺库故障；已有连接关闭后删源，不能从历史文件补造完整投影。
        case.keeper.close()
        case.local.db_path.unlink()
        reads.clear()
        broken = client.request_background_notices("session-live", after=0)
        assert broken["binding_state"] == "confirmed" and broken["agent_activity"]["subagent_projection_ok"] is False
        assert "subagent_runs_unavailable" in broken["agent_activity"]["subagent_projection_warnings"]
        assert set(reads) <= {"task-live"} and not scans and case.door.calls == calls
        assert not case.local.db_path.exists()
        record_property("failed_child_load_attempts", len([run_id for run_id in reads if run_id != "task-live"]))
        record_property("python", sys.version)
        record_property("gil_enabled", getattr(sys, "_is_gil_enabled", lambda: True)())
    finally:
        server.stop()


def test_25_passive_readers_use_real_tree_and_release_descriptors(case, monkeypatch, record_property):
    scans, reads, _copies = _observe(case, monkeypatch)
    ready, release = threading.Event(), threading.Event()
    entered, lock = [], threading.Lock()
    selected = case.manager.list_runs_by_ids_report

    def wait_for_release(run_ids):
        with lock:
            entered.append(tuple(run_ids))
            if len(entered) == 25:
                ready.set()
        assert release.wait(10)
        return selected(run_ids)

    monkeypatch.setattr(case.manager, "list_runs_by_ids_report", wait_for_release)
    directory = "/proc/self/fd" if os.path.isdir("/proc/self/fd") else "/dev/fd"
    # SQLite 会在仍有 keeper 时暂存并发读者的主库 FD；测量边界须包含 keeper 的实际关闭。
    case.keeper.close()
    before = len(os.listdir(directory)) if os.path.isdir(directory) else None
    calls = case.door.calls
    with closing(sqlite3.connect(case.local.db_path)) as keeper, ThreadPoolExecutor(max_workers=25) as workers:
        keeper.execute("SELECT count(*) FROM legacy_agent_runs").fetchone()
        futures = [workers.submit(conversation_agent_activity, case.agent, case.store, case.thread.thread_id) for _ in range(25)]
        try:
            assert ready.wait(10)
            assert entered == [(case.child.id,)] * 25
        finally:
            release.set()
        results = [future.result(timeout=10) for future in futures]
    assert all(result.subagent_projection_ok and result.subagents[0]["run_id"] == case.child.id for result in results)
    assert not scans and set(reads) <= {"task-live", case.child.id} and case.door.calls == calls
    if before is not None:
        assert len(os.listdir(directory)) - before == 0
    record_property("simultaneous_readers", len(results))
    record_property("selected_ids_per_reader", 1)
    record_property("fd_delta", 0 if before is not None else "unavailable")


@pytest.mark.parametrize("failure", ["pending", "corrupt", "missing-canonical"])
def test_status_and_closeout_keep_index_failure_unknown(case, monkeypatch, failure):
    scans, _reads, _copies = _observe(case, monkeypatch)
    index = case.manager.persistence.run_index
    if failure == "pending":
        with index.connect() as db:
            db.execute("UPDATE runs SET pending=1 WHERE run_id=?", (case.child.id,))
    elif failure == "corrupt":
        index.path.write_bytes(b"unavailable index")
    else:
        Path(case.child.agent_run_workspace_dir, "canonical_state.json").unlink()
    with pytest.raises(DataCorruptionError):
        control_service._subagent_status(case.agent, ["task-live"])
    params = _tool_loop_params(root_user_prompt="汇总当前工作", executed_tools=["create_subagents", "dispatch_subagents"])
    params_task_agent = SimpleNamespace(subagents=case.manager,
        subagent_run_ids_for_request=lambda _key: case.agent.subagent_run_ids_for_request("task-live"))
    assert _delegated_work_facts(params_task_agent, params) == {}
    assert not scans
    server = GatewayHTTPServer(0, gateway_paths_from_root(case.paths.root / "gateway"),
        params=GatewayHTTPServerParams(agent=case.agent))
    server.start()
    try:
        config = AgentConfig(my_agent_home=str(case.paths.root), gateway_port=server.server.server_address[1])
        client = GatewayChatClientAgent(None, config, case.agent.root, [case.agent.root], case.paths)
        status, body = client.post_gateway_json("/control", {"command": "/status", "conversation_id": "session-live",
            "metadata": {"message_id": "unavailable-status-wire"}}, timeout=10)
        assert status == 202 and body["control_state"] == "terminal_unknown", body
        assert body["ok"] is False and body["delivery_status"] == "unknown"
        assert body["error_code"] == "CONTROL_OPERATION_DELIVERY_UNKNOWN"
        assert "task_status" not in body and not scans
    finally:
        server.stop()


def test_cold_plugin_scope_is_indexed_existing_only_and_exact_get_never_discovers(case, monkeypatch):
    scans, reads, _copies = _observe(case, monkeypatch)

    def snapshot_files():
        snapshot = {}
        for directory, _subdirectories, filenames in os.walk(case.owner.home_dir):
            for filename in filenames:
                path = Path(directory) / filename
                if path.is_file():
                    info = path.stat()
                    snapshot[str(path.relative_to(case.owner.home_dir))] = (info.st_size, info.st_mtime_ns)
        return snapshot

    before = snapshot_files()
    calls = case.door.calls

    def forbidden(*args, **kwargs):
        pytest.fail("cold scoped read started a writer, maintenance thread, or workspace discovery")

    monkeypatch.setattr(threading.Thread, "start", forbidden)
    monkeypatch.setattr(SubAgentPersistenceService, "_start_run_index_maintenance", forbidden)
    monkeypatch.setattr(Path, "glob", forbidden)
    payload = {"thread_id": case.thread.thread_id, "root_task_id": "task-live"}
    rows = plugin.list_subagents(case.context, payload, store=case.store)["items"]
    assert {row["run_id"] for row in rows} == {case.child.id, case.grandchild.id}
    detail = plugin.get_subagent(case.context, {**payload, "subagent_id": case.child.id}, store=case.store)
    assert detail["revision"] == case.manager.load(case.child.id).state_revision
    assert detail["thread_id"] == case.thread.thread_id
    for extra in ({"thread_id": "other-thread"}, {"root_task_id": "other-root"}):
        with pytest.raises(HostAPIError) as error:
            plugin.get_subagent(case.context, {**payload, **extra, "subagent_id": case.child.id})
        assert error.value.code == "SUBAGENT_NOT_FOUND"
    assert not scans and set(reads) <= {case.child.id, case.grandchild.id} and case.door.calls == calls
    after = snapshot_files()
    assert before == after


def test_plugin_terminal_roster_and_cross_owner_stay_canonical(case):
    with global_write_commit_scope(case.door):
        case.manager.mutate(case.child.id, lambda task: setattr(task, "status", "DONE"))
    payload = {"thread_id": case.thread.thread_id, "root_task_id": "task-live"}
    full = plugin.list_subagents(case.context, payload, store=case.store)["items"]
    active = plugin.list_subagents(case.context, {**payload, "include_finished": False}, store=case.store)["items"]
    assert {row["run_id"] for row in full} == {case.child.id, case.grandchild.id}
    assert [row["run_id"] for row in active] == [case.grandchild.id]
    other = ensure_owner_home(case.paths.root, OwnerIdentity.local_user("other"))
    context = SimpleNamespace(owner_home=other, identity=other.identity)
    with pytest.raises(HostAPIError) as error:
        plugin.get_subagent(context, {"subagent_id": case.child.id})
    assert error.value.code == "SUBAGENT_NOT_FOUND"


@pytest.mark.parametrize("damage", ["missing", "partial", "pending", "missing-workspace"])
def test_cold_plugin_missing_coverage_is_unknown_without_writes(case, monkeypatch, damage):
    index = case.manager.persistence.run_index
    if damage == "missing":
        index.path.unlink()
    elif damage == "missing-workspace":
        case.manager.workspace.rename(case.manager.workspace.with_name("unavailable-subagents"))
    else:
        with index.connect() as db:
            db.execute("UPDATE meta SET value=0 WHERE key='complete'" if damage == "partial" else "UPDATE runs SET pending=1")
    folder = case.manager.workspace if case.manager.workspace.exists() else case.manager.workspace.parent
    before = {path.name: (path.stat().st_size, path.stat().st_mtime_ns) for path in folder.iterdir()}

    def forbidden(*args, **kwargs):
        pytest.fail("passive query tried to create or repair an index")

    monkeypatch.setattr(SubAgentPersistenceService, "_start_run_index_maintenance", forbidden)
    monkeypatch.setattr(threading.Thread, "start", forbidden)
    with pytest.raises(HostAPIError) as error:
        plugin.list_subagents(case.context, {"thread_id": case.thread.thread_id}, store=case.store)
    assert error.value.code == "STORE_UNAVAILABLE" and error.value.status == 503
    after = {path.name: (path.stat().st_size, path.stat().st_mtime_ns) for path in folder.iterdir()}
    assert before == after
