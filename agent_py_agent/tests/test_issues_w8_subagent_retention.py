"""W8真实投影、SQLite终态、展示转录TTL及协调器无空提交回归。"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation.agent_transcript import append_agent_transcript_event
from agent_py_agent.agent.gateway_parts.io import locked_file_transition
from agent_py_agent.agent.memory_store.retention_models import MemoryRetentionPolicy
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository
from agent_py_agent.agent.subagents.services.persistence import locator_hygiene as hygiene
from agent_py_agent.agent.user_space.home_layout import home_paths
from agent_py_agent.agent.user_space.storage_lanes import (
    StorageLaneBudget,
    StorageLaneRequest,
    validate_lane_receipt,
)


def _home(tmp_path):
    home = home_paths(tmp_path / "isolated")
    home.owner_home_dir.mkdir(parents=True)
    home.owner_retention_json.write_text(json.dumps(MemoryRetentionPolicy().to_dict()))
    conversation = ConversationStore(home.owner_home_dir / "conversations")
    return home, conversation, time.time()


# 复制原投影测试的结构及时间设置，不导入其它测试模块或触碰真实owner。
def _projection(home, tmp_path, now, *, run_id="child", days=8, status="DONE", runtime=True):
    run = home.owner_home_dir / "agents" / run_id
    run.mkdir(parents=True)
    workspace = tmp_path / f"workspace-{run_id}"
    workspace.mkdir()
    payload = {"run_id": run_id, "parent_id": "parent", "root_id": "root", "task_id": "task",
               "status": status, "created_at": now - 40 * 86400, "updated_at": now - days * 86400,
               "result_summary": "完成摘要", "error_summary": "", "usage_summary": {"tokens": 42},
               "deliverable_paths": [str(workspace / "output.md")]}
    (run / "state.json").write_text(json.dumps(payload))
    (run / "workspace_locator.json").write_text(json.dumps({"workspace_root": str(workspace)}))
    (run / "events.jsonl").write_bytes(b'{"event":1}\n')
    (run / "tmp").mkdir()
    (run / "tmp" / "scratch").write_bytes(b"scratch")
    for path in list(run.rglob("*")) + [run]:
        os.utime(path, (now - days * 86400, now - days * 86400))
    if runtime:
        repo = RuntimeRepository(home.owner_home_dir / "runtime.db")
        created = repo.record_run_creation(owner_id="local/main", goal="test", run_id=run_id)
        if status == "DONE":
            repo.settle_agent_run(agent_run_id=created["agent_run_id"], status="done", attempt_id=created["attempt_id"])
    return run


def _request(home, now, *, cursor=None, commit=None, legal_hold=False):
    return StorageLaneRequest("subagents", home, now, cursor or {}, StorageLaneBudget(None, None, None), legal_hold,
                              commit is not None, None, commit)


def _drain(home, now, **kwargs):
    cursor, reports = {}, []
    for _ in range(64):
        receipt = validate_lane_receipt(hygiene.maintain_subagents_lane(_request(home, now, cursor=cursor, **kwargs)))
        reports.append(receipt)
        if receipt["complete"]:
            return reports
        cursor = receipt["cursor"]
    pytest.fail("子代理维护游标没有完成")


def _transcript(conversation, run_id, now, *, days=31, content=b"{}\n"):
    path = conversation.storage.root / "agent_transcript_events" / f"{run_id}.jsonl"
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(content)
    os.utime(path, (now - days * 86400, now - days * 86400))
    return path


def test_real_lane_slim_archive_and_active_pending_protection(tmp_path):
    home, conversation, now = _home(tmp_path)
    slim = _projection(home, tmp_path, now, run_id="slim", days=8)
    archive = _projection(home, tmp_path, now, run_id="archive", days=31)
    active = _projection(home, tmp_path, now, run_id="active", days=40, status="RUNNING")
    pending = _projection(home, tmp_path, now, run_id="pending", days=40)
    parent = conversation.threads.create(owner_id="local/main", title="父会话")
    conversation.wakes.raise_signal({"thread_id": parent.thread_id, "reason": "subagent_event", "summary": "未消费父唤醒",
                                    "metadata": {"run_id": "pending"}})
    state = (slim / "state.json").read_bytes()
    reports = _drain(home, now)
    assert not (slim / "events.jsonl").exists() and (slim / "subagent_compact.json").exists()
    assert (slim / "state.json").read_bytes() == state
    assert not archive.exists() and active.exists() and pending.exists()
    assert any(row["protected"].get("projection", 0) >= 2 for row in reports)
    assert sum(row["freed_bytes"] for row in reports) > 0
    assert sum(row["deleted_items"] for row in _drain(home, now)) == 0


def test_pinned_projection_and_incomplete_pins_are_kept(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store import retention_scan
    home, _, now = _home(tmp_path)
    run = _projection(home, tmp_path, now, days=40)
    pins = retention_scan._RetentionPins(home.owner_home_dir, ids={"child"})
    monkeypatch.setattr(retention_scan, "current_retention_pins", lambda *a, **kw: (pins, pins.complete))
    reports = _drain(home, now)
    assert run.exists() and any(row["protected"].get("projection") for row in reports)
    pins.complete = False
    reports = _drain(home, now)
    assert run.exists() and reports[-1]["deferred_reason"] == "pins_incomplete"


def test_projection_page_complete_and_bounded_progress(tmp_path):
    home, _, now = _home(tmp_path)
    for index in range(9):
        _projection(home, tmp_path, now, run_id=f"child-{index}", status="RUNNING", runtime=False)
    reports = [hygiene.maintain_owner_agent_projections(home.owner_home_dir, now=now, max_dirs=2, dry_run=True) for _ in range(5)]
    assert [row["scanned_dirs"] for row in reports] == [2, 2, 2, 2, 1]
    assert [row["page_complete"] for row in reports] == [False, False, False, False, True]


def test_terminal_transcript_ttl_active_append_and_unknown_kept(tmp_path):
    home, conversation, now = _home(tmp_path)
    _projection(home, tmp_path, now, run_id="terminal", days=31)
    _projection(home, tmp_path, now, run_id="active", status="RUNNING", days=40)
    terminal = _transcript(conversation, "terminal", now)
    active = _transcript(conversation, "active", now)
    unknown = _transcript(conversation, "unknown", now)
    reports = _drain(home, now)
    assert not terminal.exists() and active.exists() and unknown.exists()
    assert any(row["protected"].get("transcript_unknown") for row in reports)
    agent = SimpleNamespace(conversation_store=conversation)
    assert append_agent_transcript_event(agent, thread_id="child-thread", task_id="active", request_id="bg-agent:active:attempt",
        kind="thinking_started", phase="started", block_id="bg-agent:active:attempt:thinking", payload={}) == 1


def test_transition_busy_is_reported_and_can_retry_without_deleting_sidecar(tmp_path):
    home, conversation, now = _home(tmp_path)
    _projection(home, tmp_path, now)
    path = _transcript(conversation, "child", now)
    transition = path.with_name(f".{path.name}.transition")
    entered, release = threading.Event(), threading.Event()
    def hold():
        with locked_file_transition(transition):
            entered.set()
            assert release.wait(5)
    thread = threading.Thread(target=hold)
    thread.start()
    assert entered.wait(5)
    try:
        receipt = hygiene.maintain_subagents_lane(_request(home, now, cursor={"phase": "transcripts"}))
        assert receipt["skipped"].get("lock_busy") == 1 and path.exists()
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert transition.with_name(transition.name + ".lock").exists()
    _drain(home, now)
    assert not path.exists() and transition.with_name(transition.name + ".lock").exists()


def test_transcript_owner_override_retires_two_day_file(tmp_path):
    home, conversation, now = _home(tmp_path)
    _projection(home, tmp_path, now)
    path = _transcript(conversation, "child", now, days=2)
    home.owner_retention_json.write_text(json.dumps({**json.loads(home.owner_retention_json.read_text()), "agent_transcript_days": 1}))
    _drain(home, now)
    assert not path.exists()


def test_empty_owner_zero_commits_and_legal_hold_keeps_everything(tmp_path):
    home, conversation, now = _home(tmp_path)
    commits = []
    receipt = hygiene.maintain_subagents_lane(_request(home, now, commit=lambda operation: commits.append(operation)))
    assert receipt["complete"] and commits == []
    run = _projection(home, tmp_path, now, days=40)
    path = _transcript(conversation, "child", now)
    reports = _drain(home, now, legal_hold=True, commit=lambda operation: commits.append(operation))
    assert reports[-1]["deferred_reason"] == "legal_hold" and run.exists() and path.exists() and commits == []


def test_zero_byte_transcript_counts_a_real_deletion(tmp_path):
    home, conversation, now = _home(tmp_path)
    _projection(home, tmp_path, now)
    path = _transcript(conversation, "child", now, content=b"")
    receipt = hygiene.maintain_subagents_lane(_request(home, now, cursor={"phase": "transcripts"}))
    assert receipt["deleted_items"] == 1 and receipt["freed_bytes"] == 0 and not path.exists()


def test_no_follow_transcript_directory_and_expired_budget(tmp_path):
    home, conversation, now = _home(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    original = outside / "child.jsonl"
    original.write_text("keep")
    (conversation.storage.root / "agent_transcript_events").symlink_to(outside, target_is_directory=True)
    _drain(home, now)
    assert original.read_text() == "keep" and list(outside.iterdir()) == [original]
    request = replace(_request(home, now), budget=StorageLaneBudget(0, None, None))
    assert hygiene.maintain_subagents_lane(request)["items"] == 0


@pytest.mark.parametrize("kind", ["reactivated", "late_wake", "late_pin"])
def test_retirement_rechecks_protection_after_delayed_commit(tmp_path, monkeypatch, kind):
    from agent_py_agent.agent.memory_store import retention_scan

    home, conversation, now = _home(tmp_path)
    _projection(home, tmp_path, now)
    path = _transcript(conversation, "child", now)
    pins = retention_scan._RetentionPins(home.owner_home_dir)
    monkeypatch.setattr(retention_scan, "current_retention_pins", lambda *a, **kw: (pins, True))
    parent = conversation.threads.create(owner_id="local/main", title="父会话")

    def commit(operation):
        if kind == "reactivated":
            import sqlite3
            with sqlite3.connect(home.owner_home_dir / "runtime.db") as connection:
                connection.execute("UPDATE agent_runs SET status='running' WHERE run_id='child'")
        elif kind == "late_wake":
            conversation.wakes.raise_signal({"thread_id": parent.thread_id, "reason": "subagent_event", "summary": "晚到唤醒",
                                            "metadata": {"run_id": "child"}})
        else:
            pins.ids.add("child")
        return operation()

    receipt = hygiene.maintain_subagents_lane(_request(home, now, cursor={"phase": "transcripts"}, commit=commit))
    assert path.exists() and receipt["deleted_items"] == 0 and receipt["protected"].get("transcript_changed_or_busy") == 1


@pytest.mark.parametrize("reactivate", [False, True])
def test_commit_worker_thread_binds_and_restores_retirement_context(tmp_path, reactivate):
    import sqlite3
    from concurrent.futures import ThreadPoolExecutor

    from agent_py_agent.agent.conversation import agent_transcript

    home, conversation, now = _home(tmp_path)
    _projection(home, tmp_path, now)
    path = _transcript(conversation, "child", now)
    restored = []

    def worker(operation):
        if reactivate:
            with sqlite3.connect(home.owner_home_dir / "runtime.db") as connection:
                connection.execute("UPDATE agent_runs SET status='running' WHERE run_id='child'")
        value = operation()
        restored.append(agent_transcript._RETIRE_OBSERVATION.get())
        return value

    def commit(operation):
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(worker, operation).result(timeout=5)

    receipt = hygiene.maintain_subagents_lane(_request(home, now, cursor={"phase": "transcripts"}, commit=commit))
    assert receipt["deleted_items"] == int(not reactivate) and path.exists() == reactivate
    assert restored == [None] and agent_transcript._RETIRE_OBSERVATION.get() is None
