from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent_py_agent.agent import owner_wake_discovery as wake
from agent_py_agent.agent.conversation.business_observability import ObservationSourcePage
from agent_py_agent.agent.gateway_parts import compact_operations as compact
from agent_py_agent.agent.gateway_parts.business_api_v13_readers import native_approval_readers
from agent_py_agent.agent.gateway_parts.paths import gateway_paths
from agent_py_agent.agent.gateway_parts.queue_service import ensure_gateway_folders
from agent_py_agent.agent.local_storage.owner_read_index import OwnerReadIndex
from agent_py_agent.tests.test_fw41_w4_native_approvals_wiring import (
    _make_agent,
    _sample_approval_request,
)
from agent_py_agent.tests.test_gateway_agent_control_service import _bound_agent_tree


def test_idle_wake_discovery_stops_reading_history(tmp_path, monkeypatch):
    home = tmp_path / "owner"
    root = home / "agents"
    root.mkdir(parents=True)
    for number in range(10_000):
        run = root / f"run-{number:05d}"
        run.mkdir()
        (run / "state.json").write_text('{"status":"DONE"}', encoding="utf-8")

    for _ in range(10):
        if not wake._has_unfinished_subagent_run(home):
            break
    else:
        raise AssertionError("repair did not complete")

    probes = []
    original = wake._subagent_run_has_wake_fact

    def probe(owner_home, run_dir):
        probes.append(run_dir.name)
        return original(owner_home, run_dir)

    monkeypatch.setattr(wake, "_subagent_run_has_wake_fact", probe)
    assert not wake._has_unfinished_subagent_run(home)
    assert probes == []


def _foreground_approvals(tmp_path, monkeypatch, size):
    agent = _make_agent(tmp_path, monkeypatch, tag="m50")
    paths = gateway_paths(agent)
    ensure_gateway_folders(paths)
    store = agent.conversation_store
    store.threads.get_or_create({
        "channel": "chat", "channel_conversation_id": "conv-m50",
        "channel_user_id": "local-user", "canonical_user_id": "local-user",
        "owner_id": agent.home_paths.owner_id,
        "owner_home": str(agent.home_paths.owner_home_dir), "title": "pending test",
    })
    expected = set()
    now = time.time()
    for number in range(size):
        key = f"request-{number:05d}"
        approval = _sample_approval_request(key, f"run-{number:05d}", "Check release status")
        expected.add(approval.permission_id)
        document = {
            "id": key, "kind": "ask", "status": "processing", "turn_phase": "open",
            "lease_started_at": now - 5, "user_id": "local-user",
            "conversation": {"channel": "chat", "channel_conversation_id": "conv-m50",
                             "channel_user_id": "local-user", "canonical_user_id": "local-user"},
            "host_request_owner.v1": {"provider": "local", "owner_kind": "main", "owner_id": "main"},
        }
        (paths.processing / f"{key}.json").write_text(json.dumps(document), encoding="utf-8")
        event = {"kind": "permission_requested", "t": now, "permission": approval.to_dict()}
        (paths.processing / f"{key}.chunks.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")
    return agent, paths, store, expected


def test_approval_list_pages_past_processing_scan_cap(tmp_path, monkeypatch):
    agent, paths, store, expected = _foreground_approvals(tmp_path, monkeypatch, 5_000)
    reader = native_approval_readers(agent, paths, agent.home_paths.owner_id, store)["approvals"]
    position = None
    seen = set()
    for _ in range(150):
        if getattr(reader, "observation_source_paging_version", None) == 1:
            page = reader({}, source_page=ObservationSourcePage(position, 100))
        else:
            page = reader({})
        position = page.next_position
        for record in page.records:
            approval_id = record.value["approval_id"]
            assert approval_id not in seen
            seen.add(approval_id)
        if position is None:
            break
    assert page.coverage == "complete"
    assert seen == expected


def _child_process(*args):
    subprocess.run(
        [sys.executable, "-X", "gil=0", "-c", *args],
        check=True, capture_output=True, text=True, timeout=30,
    )


def test_external_wake_is_seen_by_cursor_repair(tmp_path, monkeypatch):
    home = tmp_path / "owner"
    root = home / "agents"
    root.mkdir(parents=True)
    for name in (*[f"other-{number:02d}" for number in range(16)], "last"):
        run = root / name
        run.mkdir()
        (run / "state.json").write_text('{"status":"DONE"}', encoding="utf-8")

    monkeypatch.setattr(wake, "_SUBAGENT_SCAN_MAX_DIRS", 8)
    for _ in range(5):
        if not wake._has_unfinished_subagent_run(home):
            break
    else:
        pytest.fail("initial repair never completed")
    index = wake._owner_pending_index(home)
    receipt, pending, _ = index.pending_wake_summary()
    assert receipt["coverage"] == "complete"
    assert not pending

    target = root / "last" / "state.json"
    outer_stamp = wake._agents_stamp(home)
    _child_process(
        'import sys; from pathlib import Path; from agent_py_agent.agent.common.json_io import write_json_file_atomic; '
        'write_json_file_atomic(Path(sys.argv[1]), {"status":"DONE","runtime_closeout_pending":True,"thread_id":"t"})',
        str(target),
    )
    assert wake._agents_stamp(home) == outer_stamp
    assert not wake.pending_projection_changes(home, "wake")
    real_time = time.time
    monkeypatch.setattr(wake, "time", SimpleNamespace(
        time=lambda: real_time() + wake._HISTORY_REVERIFY_SECONDS + 1, monotonic=time.monotonic,
    ))
    assert wake._has_unfinished_subagent_run(home)
    partially_repaired, _, _ = index.pending_wake_summary()
    assert partially_repaired["coverage"] == "partial"
    assert partially_repaired["cursor"] and partially_repaired["backlog"] == 1
    for _ in range(5):
        assert wake._has_unfinished_subagent_run(home)
        recovered, pending, _ = index.pending_wake_summary()
        if recovered["complete"]:
            break
    assert recovered["complete"] and pending


def test_foreign_approval_breaks_complete_compaction_proof(tmp_path):
    agent, _, child = _bound_agent_tree(tmp_path)
    store = agent.conversation_store
    paths = gateway_paths(agent)
    ensure_gateway_folders(paths)
    parent_thread = store.tasks.load(child.root_id).thread_id
    store.threads.update_atomic(parent_thread, lambda current: replace(current, owner_id=agent.home_paths.owner_id))
    root = store.storage.root / "subagent_tool_approvals" / child.root_id
    root.mkdir(parents=True, exist_ok=True)
    thread = SimpleNamespace(thread_id=child.agent_thread_id, task_ids=(child.root_id,), active_task_ids=())
    confirmations = agent.home_paths.owner_home_dir / "data" / "private_interactions"
    assert compact._idle_pending_approval_state(agent, store, thread) == ""
    complete, _, verdict, _ = compact._idle_approval_projection(agent, thread, root.parent, confirmations)
    assert complete[2]["complete"] and verdict == ""
    previous_parent = compact._approval_dir_stamp(root.parent)

    _child_process(
        """
import sys
from pathlib import Path
from agent_py_agent.agent.conversation.agent_tool_approval import publish_agent_tool_approval
from agent_py_agent.agent.core import SimpleAgent
from agent_py_agent.agent.settings import AgentConfig
from agent_py_agent.tests.test_gateway_agent_control_service import _child_approval_request

directory = Path(sys.argv[1])
agent = SimpleAgent(AgentConfig(model_backend="echo", my_agent_home=str(directory / "home"),
                                gateway_per_user_owner_scoping=False), directory / "workspace")
child = agent.subagents.load(sys.argv[2])
publish_agent_tool_approval(agent, run_id=child.id, thread_id=child.agent_thread_id,
                           request_value=_child_approval_request(child.id))
""",
        str(tmp_path), child.id,
    )
    assert compact._approval_dir_stamp(root.parent) == previous_parent
    assert not wake.pending_projection_changes(agent.home_paths.owner_home_dir, "approvals")
    _, _, verdict, _ = compact._idle_approval_projection(agent, thread, root.parent, confirmations)
    assert verdict == "partial"
    assert compact._idle_pending_approval_state(agent, store, thread) in {"pending_approval", "activity_unknown"}

    reader = native_approval_readers(agent, paths, agent.home_paths.owner_id, store)["approvals"]
    position = None
    for _ in range(5):
        page = reader({}, source_page=ObservationSourcePage(position, 25))
        position = page.next_position
        if position is None:
            break
    assert page.coverage == "complete"
    assert [row.run_id for row in page.records] == [child.id]
    index = OwnerReadIndex(agent.home_paths.owner_home_dir, owner_id=agent.home_paths.owner_id)
    _, projected = index.pending_approval_page("approval_list_coverage")
    assert projected[0]["root_id"] == child.root_id


@pytest.mark.parametrize("populated", [False, True])
def test_pending_tables_use_c21_keys_and_one_commit(tmp_path, monkeypatch, populated):
    home = tmp_path / "owner"
    state = home / "agents" / "one" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text('{"status":"DONE","runtime_closeout_pending":true,"thread_id":"test"}', encoding="utf-8")
    for number in range(16):
        other = home / "agents" / f"settled-{number}"
        other.mkdir()
        (other / "state.json").write_text('{"status":"DONE"}', encoding="utf-8")
    index = wake._owner_pending_index(home)
    db = home / "cache" / "owner_read_index.sqlite3"
    if populated:
        with index.upload_registry_transaction() as conn:
            conn.execute("INSERT INTO ori_upload_registry(artifact_id,byte_offset,length,row_sha,generation) "
                         "VALUES ('existing',0,1,'sha','gen')")
            conn.execute("DROP TABLE ori_wake_pending")
            conn.execute("DROP TABLE ori_pending_approvals")

    original = sqlite3.connect
    commits = []

    def connect(*args, **kwargs):
        connection = original(*args, **kwargs)
        connection.set_trace_callback(lambda sql: commits.append(sql) if sql == "COMMIT" else None)
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    monkeypatch.setattr(wake, "_FACT_MIN_CACHED_SECONDS", 0.0)
    assert wake._owner_fact_kind(home) == "hard"
    assert commits == ["COMMIT"]
    assert wake._owner_fact_kind(home) == "hard"
    assert commits == ["COMMIT"]

    with closing(original(db)) as conn:
        for table, expected in (
            ("ori_wake_pending", [("owner_id", 1), ("thread_id", 2), ("kind", 3),
                                  ("next_due_at", 0), ("source_ref", 4), ("generation", 0)]),
            ("ori_pending_approvals", [("owner_id", 1), ("thread_id", 0), ("root_id", 0),
                                       ("approval_id", 2), ("revision", 0), ("created_at", 0), ("generation", 0)]),
        ):
            assert [(col[1], col[5]) for col in conn.execute(f"PRAGMA table_info({table})")] == expected
        assert conn.execute(
            "SELECT thread_id FROM ori_wake_pending WHERE owner_id=? AND source_ref=?", ("owner", "one"),
        ).fetchone() == ("test",)
        if populated:
            assert conn.execute("SELECT artifact_id FROM ori_upload_registry").fetchone() == ("existing",)


def test_wake_schedule_projects_deadline_and_canonical_publish(tmp_path, monkeypatch):
    agent = _make_agent(tmp_path, monkeypatch, tag="schedule")
    store = agent.conversation_store
    home, owner_id = agent.home_paths.owner_home_dir, agent.home_paths.owner_id
    thread = store.threads.get_or_create({"channel": "chat", "channel_conversation_id": "wake-schedule",
                                          "owner_id": owner_id})
    root = store.storage.root

    def refresh():
        return wake.owner_wake_schedule(home, owner_id, root)

    assert refresh() == (True, False, None)
    policy = store.progress.create({"thread_id": thread.thread_id, "interval_seconds": 100, "now": 200})
    assert refresh() == (True, False, 300)
    updated = store.progress.expedite(policy.policy_id, due_at=250, now=210)
    assert updated.next_due_at == 250
    assert refresh() == (True, False, 250)
    signal = store.wakes.raise_signal({"thread_id": thread.thread_id, "reason": "agent_event"})
    assert refresh() == (True, True, 250)
    store.wakes.mark_handled(signal.wake_signal_id)
    assert refresh() == (True, False, 250)
