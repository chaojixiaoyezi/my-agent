"""当前义务、精确 Audit 请求和启动终态窗口的真实索引行为回归。"""
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent import startup_recovery as startup
from agent_py_agent.agent.agent_core.orchestration.dispatch.params import DispatchParams
from agent_py_agent.agent.agent_core.services import watch_service as watch
from agent_py_agent.agent.capability.skill_learning_protection import learning_protection_for_agent
from agent_py_agent.agent.common.audit_activation import (
    AUDIT_SOURCE_ID_ATTR,
    AUDIT_SOURCE_OWNER_HOME_ATTR,
    AUDIT_SOURCE_WATCH_ID_ATTR,
    AUDIT_SOURCE_WORKER_ATTR,
    AUDIT_SOURCE_WORKER_KEY_ATTR,
    audit_source_worker_key,
)
from agent_py_agent.agent.conversation.authority import CONVERSATION_REQUEST_ID_ATTR
from agent_py_agent.agent.conversation.background_team_lifecycle import identity_outstanding
from agent_py_agent.agent.conversation.store import ConversationStore
from agent_py_agent.agent.gateway_parts import compact_operations as idle
from agent_py_agent.agent.ingestion import source_worker as source
from agent_py_agent.agent.memory_store.retention_scan_budget import RETENTION_SCAN_SLICE_ENTRIES
from agent_py_agent.tests.test_r7_a_w2 import _task
from agent_py_agent.tests.test_subagent_listruns_cache import _selected_manager


def _run(manager, run_id, status="RUNNING", *, thread="thread", root="root", capability="", **fields):
    task = _task(manager, run_id, status, thread=thread, root=root, capability=capability)
    if fields:
        path = manager.workspace / run_id / "task.json"
        row = json.loads(path.read_text())
        row.update(fields)
        path.write_text(json.dumps(row))
        task = manager.load(run_id)
    manager.persistence.run_index.upsert(task)
    return task


def _host(tmp_path, manager):
    store = ConversationStore(tmp_path / "conversations")
    thread = replace(store.threads.create(canonical_user_id="user"), task_ids=("root",))
    return SimpleNamespace(subagents=manager, conversation_store=store, scheduler_repository=None,
                           home_paths=SimpleNamespace(owner_home_dir=tmp_path)), thread


@pytest.mark.parametrize("history", [100, 1000, 10000])
def test_hot_read_counts_depend_on_current_obligations_not_history(tmp_path, monkeypatch, record_property, history):
    manager = _selected_manager(tmp_path)
    host, thread = _host(tmp_path, manager)
    with manager.persistence.run_index.connect() as db:
        for number in range(history):
            task = _task(manager, f"old-{number}", "DONE", root="elsewhere", thread="elsewhere")
            manager.persistence.run_index.upsert(task, connection=db)
    _run(manager, "live", thread=thread.thread_id, attributes={
        "conversation_thread_id": thread.thread_id, "allowed_skills": ["owner:live"]})
    reads, original = [], manager.persistence._cached_run_ref
    original_glob = Path.glob

    def observed(run_id, path):
        reads.append(run_id)
        return original(run_id, path)

    def observed_glob(path, pattern, *args, **kwargs):
        assert path != manager.workspace or pattern != "*/task.json", "hot read enumerated historical runs"
        return original_glob(path, pattern, *args, **kwargs)

    # 库存准备均已完成；此作用域只观察热读，不在glob接缝替换后调用rglob。
    monkeypatch.setattr(Path, "glob", observed_glob)
    monkeypatch.setattr(manager.persistence, "_cached_run_ref", observed)
    monkeypatch.setattr(idle, "list_background_processes", lambda *a, **k: {"projection_ok": True, "items": []})
    assert idle.idle_compact_busy_state(host, host.conversation_store, thread) == "subagent_active"
    record_property("idle_canonical_reads", len(reads))
    record_property("idle_historical_reads", sum(run_id.startswith("old-") for run_id in reads))
    assert set(reads) == {"live"}
    assert "owner:live" in learning_protection_for_agent(host)["protected_ids"]
    assert watch._watch_dispatch_input_state(host).has_inputs
    assert watch._watch_scoped_dispatch_params(host, DispatchParams(limit=1)).include_run_ids == ["live"]
    assert [task.id for task in source._worker_runs_snapshot(host, audit_ids=("root",))] == ["live"]
    assert source.audit_task_has_unreported_findings(host, "root") is False
    startup._detect_active_tasks(host, startup.ActiveWorkSummary())
    startup._detect_crashed_running_tasks(host, startup.ActiveWorkSummary())
    assert set(reads) == {"live"} and len(reads) <= 12
    record_property("canonical_reads", len(reads))
    record_property("historical_reads", sum(run_id.startswith("old-") for run_id in reads))


@pytest.mark.parametrize("status,capability,bound,expected", [
    ("BLOCKED", "", True, "subagent_blocked"),
    ("DONE", "OPEN", True, "pending_approval"),
    ("DONE", "OPEN", False, "pending_approval"),
    ("BLOCKED", "", False, "subagent_blocked"),
])
def test_idle_preserves_terminal_and_unbound_obligations(tmp_path, status, capability, bound, expected):
    manager = _selected_manager(tmp_path)
    host, thread = _host(tmp_path, manager)
    _run(manager, "obligation", status, root="unrelated-root",
         thread=thread.thread_id if bound else "", capability=capability)
    assert idle.idle_compact_busy_state(host, host.conversation_store, thread) == expected


@pytest.mark.parametrize("status,capability", [("BLOCKED", ""), ("TIMEOUT", ""), ("DONE", "DENIED")])
def test_identity_keeps_original_status_and_parent_resolution_predicates(tmp_path, status, capability):
    manager = _selected_manager(tmp_path)
    host, thread = _host(tmp_path, manager)
    _run(manager, "obligation", status, thread=thread.thread_id, capability=capability)
    refs, _ = identity_outstanding(host, thread)
    assert ("request-obligation" if capability else "obligation") in refs


@pytest.mark.parametrize("reader", ["idle", "watch", "identity", "startup", "findings"])
def test_single_partial_index_preserves_unknown_before_normal_convergence(tmp_path, reader):
    manager = _selected_manager(tmp_path)
    host, thread = _host(tmp_path, manager)
    _run(manager, "live", thread=thread.thread_id)
    with manager.persistence.run_index.connect() as db:
        db.execute("UPDATE meta SET value=0 WHERE key='complete'")
    if reader == "idle":
        assert idle.idle_compact_busy_state(host, host.conversation_store, thread) == "activity_unknown"
    elif reader == "watch":
        state = watch._watch_dispatch_input_state(host)
        assert state.load_error and not state.has_inputs
    elif reader == "identity":
        assert "subagents:unreadable" in identity_outstanding(host, thread)[0]
    elif reader == "startup":
        summary = startup.ActiveWorkSummary()
        startup._detect_crashed_running_tasks(host, summary)
        assert summary.detection_errors
    else:
        assert source.audit_task_has_unreported_findings(host, "root") is True


def test_healthy_idle_does_not_hydrate_foreign_terminal_rows(tmp_path, monkeypatch):
    manager = _selected_manager(tmp_path)
    host, thread = _host(tmp_path, manager)
    _run(manager, "foreign", "DONE", root="foreign", thread="foreign")
    reads, original = [], manager.persistence._cached_run_ref

    def observed(run_id, path):
        reads.append(run_id)
        return original(run_id, path)

    monkeypatch.setattr(manager.persistence, "_cached_run_ref", observed)
    monkeypatch.setattr(idle, "list_background_processes", lambda *a, **k: {"projection_ok": True, "items": []})
    assert idle.idle_compact_busy_state(host, host.conversation_store, thread) == ""
    assert not watch._watch_dispatch_input_state(host).has_inputs
    assert reads == []


def test_terminal_audit_findings_and_quota_workers_are_exact_candidates(tmp_path):
    manager = _selected_manager(tmp_path)
    attrs = {AUDIT_SOURCE_WORKER_ATTR: True, CONVERSATION_REQUEST_ID_ATTR: "audit",
             AUDIT_SOURCE_ID_ATTR: "source", AUDIT_SOURCE_WATCH_ID_ATTR: "watch",
             AUDIT_SOURCE_OWNER_HOME_ATTR: str(tmp_path),
             AUDIT_SOURCE_WORKER_KEY_ATTR: audit_source_worker_key("audit", "watch")}
    ledger = tmp_path / "findings.jsonl"
    ledger.write_text('{"audit_finding": true, "requires_llm_report": true}\n')
    _run(manager, "terminal-finding", "DONE", attributes=attrs, agent_run_findings_jsonl=str(ledger))
    _run(manager, "quota", "BLOCKED", attributes=attrs, failure_type="provider_quota_exhausted")
    _run(manager, "foreign", "DONE", root="foreign", thread="foreign")
    host = SimpleNamespace(subagents=manager)
    assert source.audit_task_has_unreported_findings(host, "audit") is True
    assert {task.id for task in source._worker_runs_snapshot(host, audit_ids=("audit",))} == {"terminal-finding", "quota"}
    assert source.resume_quota_blocked_source_workers(host, "audit") == 1
    assert manager.load("quota").status == "PENDING"
    assert manager.load("terminal-finding").status == "DONE"


def test_startup_terminal_process_proof_uses_bounded_cursor_and_never_reopens(tmp_path, monkeypatch, record_property):
    from agent_py_agent.agent.subagents import process_control

    manager = _selected_manager(tmp_path)
    for number in range(RETENTION_SCAN_SLICE_ENTRIES + 1):
        _run(manager, f"old-{number:04}", "DONE", root="foreign", thread="foreign")
    terminal = _run(manager, "zz-orphan", "DONE", attributes={
        "background_start": {"pid": 424242, "launch_id": "launch", "attempt_id": "attempt", "status": "running"}})
    path = manager.workspace / terminal.id / "task.json"
    before = path.read_bytes()
    monkeypatch.setattr(process_control, "is_pid_alive", lambda pid: pid == 424242)
    monkeypatch.setattr(startup, "_process_cmdline", lambda pid: "python subagents-dispatch")
    reads, original = [], manager.persistence._cached_run_ref

    def observed(run_id, path):
        reads.append(run_id)
        return original(run_id, path)

    monkeypatch.setattr(manager.persistence, "_cached_run_ref", observed)
    host = SimpleNamespace(subagents=manager)
    first = startup.ActiveWorkSummary()
    startup._detect_orphan_processes(host, first)
    assert getattr(first, "orphan_scan_partial", False) and len(reads) <= RETENTION_SCAN_SLICE_ENTRIES
    first_page_reads = len(reads)
    second = startup.ActiveWorkSummary()
    startup._detect_orphan_processes(host, second)
    assert second.orphan_scan_partial
    assert second.orphan_processes == [{"pid": 424242, "launch_id": "launch", "run_id": terminal.id,
        "task_status": "DONE", "cmdline_preview": "python subagents-dispatch"}]
    assert manager.load(terminal.id).status == "DONE" and path.read_bytes() == before
    record_property("first_page_reads", first_page_reads)
