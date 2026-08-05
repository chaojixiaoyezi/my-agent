from __future__ import annotations

import json

from agent_py_agent.agent.conversation import ConversationStore
from agent_py_agent.agent.conversation.task_runtime_state import task_runtime_state
from agent_py_agent.agent.ingestion.audit_state import (
    audit_task_activation_facts,
    audit_task_source_facts,
    audit_task_summary_facts,
    lane_unjudged_backlog,
    owner_home_has_incomplete_watch,
    task_has_incomplete_watch,
)
from agent_py_agent.agent.ingestion.watch_state import (
    new_state,
    persist_state,
    state_dir,
    watch_id_for,
)

NOW = 100_000.0


class _HomePaths:
    def __init__(self, owner_home):
        self.owner_home_dir = str(owner_home)


class _Agent:
    def __init__(self, owner_home, store):
        self.home_paths = _HomePaths(owner_home)
        self.conversation_store = store


def _store(tmp_path) -> ConversationStore:
    return ConversationStore(tmp_path / "conversations")


def _bind_audit(
    store: ConversationStore,
    task_id: str,
    *,
    duration_seconds: int | None = None,
    run_epoch: int = 0,
    source_bindings: list[dict] | None = None,
):
    thread = store.get_or_create_thread(
        {
            "canonical_user_id": "user-1",
            "channel": "internal",
            "channel_conversation_id": f"conv:{task_id}",
            "channel_user_id": "local-main-agent",
            "now": NOW,
        }
    )
    return_link = store.bind_task(
        {
            "thread_id": thread.thread_id,
            "task_id": task_id,
            "goal": "opaque user objective",
            "status": "active",
            "work_kind": "audit",
            "work_name": task_id,
            "cancellation_scope": "detached",
            "duration_seconds": duration_seconds,
            "run_epoch": run_epoch,
            "effective_source_bindings": source_bindings or [],
            "now": NOW,
        }
    )
    return thread, return_link


def _lane(
    owner_home,
    *,
    task_id: str,
    url: str,
    written: int,
    acknowledged: int,
    window: int = 2700,
    opened_at: float = NOW - 60.0,
    closed: bool = False,
    run_epoch: int = 0,
    source_id: str = "",
):
    from agent_py_agent.agent.common.audit_activation import audit_watch_scope_id

    watch_id = watch_id_for(owner_home, url, audit_watch_scope_id(task_id, run_epoch))
    state = new_state(
        owner_home,
        url,
        {"watch_window_seconds": window},
        watch_id=watch_id,
    )
    state.opened_at = opened_at
    state.closed = closed
    state.audit_guarantee = True
    state.audit_root_task_id = task_id
    state.audit_run_epoch = run_epoch
    state.source_id = source_id
    state.totals["spool_candidates"] = written
    persist_state(state)
    sidecar = state_dir(owner_home) / f"{state.watch_id}.read.json"
    sidecar.write_text(
        json.dumps(
            {
                "read_seq": 0,
                "candidates_consumed": acknowledged,
                "candidates_acked": acknowledged,
                "updated_at": NOW - 10,
            }
        ),
        encoding="utf-8",
    )
    return state


def test_task_runtime_projects_only_exact_audit_sources(tmp_path):
    store = _store(tmp_path)
    owner_home = tmp_path / "owner"
    thread, _ = _bind_audit(store, "audit-a", duration_seconds=3600)
    _bind_audit(store, "audit-b", duration_seconds=3600)
    first = _lane(
        owner_home,
        task_id="audit-a",
        url="http://source/a",
        written=50,
        acknowledged=5,
    )
    _lane(
        owner_home,
        task_id="audit-b",
        url="http://source/b",
        written=70,
        acknowledged=7,
    )
    agent = _Agent(owner_home, store)

    sources = audit_task_source_facts(agent, "audit-a", now=NOW)
    summary = audit_task_summary_facts(agent, "audit-a", now=NOW)
    state = task_runtime_state(
        agent=agent,
        store=store,
        thread_id=thread.thread_id,
        task_id="audit-a",
        load_errors=[],
    )

    assert [item["watch_id"] for item in sources] == [first.watch_id]
    assert sources[0]["audit_receipt"]["enqueued"] == 50
    assert sources[0]["audit_receipt"]["pending"] == 45
    assert state["work_kind"] == "audit"
    assert state["work_name"] == "audit-a"
    assert state["duration_seconds"] == 3600
    assert summary["source_count"] == 1
    assert summary["receipt_totals"]["enqueued"] == 50
    assert summary["receipt_totals"]["pending"] == 45
    assert summary["all_receipts_settled"] is False
    assert state["audit_summary"]["source_urls"] == ["http://source/a"]
    assert [item["watch_id"] for item in state["audit_sources"]] == [first.watch_id]


def test_owner_discovery_and_backlog_use_acknowledged_not_delivered(tmp_path):
    owner_home = tmp_path / "owner"
    state = _lane(
        owner_home,
        task_id="audit-a",
        url="http://source/a",
        written=12,
        acknowledged=4,
        window=0,
    )

    assert lane_unjudged_backlog(owner_home, state.__dict__) == 8
    assert owner_home_has_incomplete_watch(owner_home, now=NOW)


def test_exact_task_incomplete_watch_does_not_leak_across_named_audits(tmp_path):
    store = _store(tmp_path)
    owner_home = tmp_path / "owner"
    _bind_audit(store, "audit-a")
    _bind_audit(store, "audit-b")
    _lane(
        owner_home,
        task_id="audit-a",
        url="http://source/a",
        written=12,
        acknowledged=4,
        window=0,
    )
    agent = _Agent(owner_home, store)

    assert task_has_incomplete_watch(agent, "audit-a", now=NOW)
    assert not task_has_incomplete_watch(agent, "audit-b", now=NOW)


def test_closed_source_stays_incomplete_until_all_records_are_acknowledged(tmp_path):
    store = _store(tmp_path)
    owner_home = tmp_path / "owner"
    _bind_audit(store, "audit-a")
    state = _lane(
        owner_home,
        task_id="audit-a",
        url="http://source/a",
        written=10,
        acknowledged=4,
        closed=True,
    )
    agent = _Agent(owner_home, store)

    assert task_has_incomplete_watch(agent, "audit-a", now=NOW)

    sidecar = state_dir(owner_home) / f"{state.watch_id}.read.json"
    sidecar.write_text(
        json.dumps(
            {
                "read_seq": 0,
                "candidates_consumed": 10,
                "candidates_acked": 10,
                "updated_at": NOW,
            }
        ),
        encoding="utf-8",
    )
    assert not task_has_incomplete_watch(agent, "audit-a", now=NOW)


def test_current_audit_epoch_reuses_source_identity_and_requires_exact_source_coverage(
    tmp_path,
):
    store = _store(tmp_path)
    owner_home = tmp_path / "owner"
    _, link = _bind_audit(
        store,
        "audit-rerun",
        run_epoch=2,
        source_bindings=[
            {"source_id": "source-a", "url": "http://source/a"},
            {"source_id": "source-b", "url": "http://source/b"},
        ],
    )
    old = _lane(
        owner_home,
        task_id="audit-rerun",
        url="http://source/a",
        written=100,
        acknowledged=100,
        run_epoch=1,
    )
    current_a = _lane(
        owner_home,
        task_id="audit-rerun",
        url="http://source/a",
        written=10,
        acknowledged=10,
        run_epoch=2,
        source_id="source-a",
    )
    current_b = _lane(
        owner_home,
        task_id="audit-rerun",
        url="http://source/b",
        written=20,
        acknowledged=20,
        run_epoch=2,
        source_id="source-b",
    )
    agent = _Agent(owner_home, store)

    facts = audit_task_source_facts(agent, link.task_id, now=NOW)
    activation = audit_task_activation_facts(agent, link, now=NOW)

    assert old.watch_id == current_a.watch_id
    assert {row["watch_id"] for row in facts} == {current_a.watch_id, current_b.watch_id}
    assert {row["run_epoch"] for row in facts} == {2}
    assert activation["ready"] is True
    assert activation["run_epoch"] == 2
    assert activation["expected_source_count"] == 2
    assert activation["observed_source_count"] == 2


def test_prior_run_watch_cannot_satisfy_current_audit_activation(tmp_path):
    store = _store(tmp_path)
    owner_home = tmp_path / "owner"
    _, link = _bind_audit(
        store,
        "audit-rerun-missing",
        run_epoch=2,
        source_bindings=[{"source_id": "source-a", "url": "http://source/a"}],
    )
    _lane(
        owner_home,
        task_id=link.task_id,
        url="http://source/a",
        written=10,
        acknowledged=10,
        run_epoch=1,
    )
    activation = audit_task_activation_facts(_Agent(owner_home, store), link, now=NOW)

    assert activation["ready"] is False
    assert activation["reason"] == "source_coverage_incomplete"
    assert activation["missing_source_ids"] == ["source-a"]
    assert activation["observed_source_count"] == 0
