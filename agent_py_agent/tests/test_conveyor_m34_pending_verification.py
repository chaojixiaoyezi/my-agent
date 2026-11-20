from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.conversation.control_commands import parse_conversation_control
from agent_py_agent.agent.gateway_parts.turn_recovery_control import execute_turn_recovery_control
from agent_py_agent.agent.runtime_db import owner_recovery, recovery_guard
from agent_py_agent.agent.runtime_db.repository import RuntimeRepository


@pytest.fixture
def runtime(tmp_path):
    return RuntimeRepository(tmp_path / "runtime.db")


def _run(repo, run_id, *, owner_id="local/main", thread_id="", **metadata):
    return repo.record_run_creation(
        owner_id=owner_id, run_id=run_id, role="main", thread_id=thread_id,
        task_run_metadata=metadata,
    )


def _insert_operations(repo, records):
    with repo.transaction() as conn:
        conn.executemany(
            "INSERT INTO tool_operations(operation_id, agent_run_id, attempt_id, attempt_generation, "
            "tool_operation_generation, operation_type, status, handler_started_at, settled_at, "
            "outcome_json, created_at, updated_at) VALUES(?,?,?,1,1,'fixture',?,1,?,?,1,1)",
            [(op, run["agent_run_id"], run["attempt_id"], status, settled, json.dumps(outcome))
             for op, run, status, settled, outcome in records],
        )


def _admin(repo):
    return SimpleNamespace(
        subagents=SimpleNamespace(runtime_db=repo),
        home_paths=SimpleNamespace(owner_id="local/main", owner_kind="main", owner_provider="local"),
    )


def test_pending_verification_selects_original_isolated_operation(runtime):
    run = _run(runtime, "run-isolated")
    _insert_operations(runtime, [("tool_operation:fixture", run, "UNKNOWN", 0, {})])
    rows, next_cursor = owner_recovery.list_pending_verification(runtime.db_path.parent)
    assert [row["operation_id"] for row in rows] == ["tool_operation:fixture"]
    assert next_cursor == ""


def test_sqlite_operational_error_uses_primary_code():
    error = sqlite3.OperationalError("fixture")
    error.sqlite_errorcode = sqlite3.SQLITE_BUSY | (1 << 8)
    assert recovery_guard.classify_sqlite_operational_error(error) == "bounded_backoff"


def test_recover_lists_isolated_operations_without_a_blocked_attempt(runtime):
    run = _run(runtime, "run-isolated")
    _insert_operations(runtime, [("tool_operation:fixture", run, "UNKNOWN", 0, {})])
    view = execute_turn_recovery_control(_admin(runtime), None, parse_conversation_control("/recover owner"))
    assert view.ok and "tool_operation:fixture" in view.message


@pytest.mark.parametrize(("code", "expected"), [
    (sqlite3.SQLITE_CORRUPT, "integrity_review"),
    (sqlite3.SQLITE_NOTADB, "integrity_review"),
    (sqlite3.SQLITE_LOCKED, "bounded_backoff"),
    (sqlite3.SQLITE_BUSY, "bounded_backoff"),
    (sqlite3.SQLITE_IOERR, "uncertain"),
    (None, "uncertain"),
])
def test_sqlite_classification_and_recovery_report_status_are_independent(code, expected):
    from agent_py_agent.agent.io.sqlite_connection import sqlite_error_details
    from agent_py_agent.agent.runtime_errors import runtime_error_report

    error = sqlite3.OperationalError("fixture")
    if code is not None:
        error.sqlite_errorcode = code
    assert recovery_guard.classify_sqlite_operational_error(error) == expected
    assert runtime_error_report(error)["sqlite_operational_classification"] == expected
    for status in ("deferred", "requires_recovery"):
        report = {"status": status, "error": sqlite_error_details(error)}
        wrapped = recovery_guard.runtime_db_recovery_error(report)
        assert wrapped.status == status
        assert wrapped.runtime_db_recovery is report
        assert recovery_guard.classify_sqlite_operational_error(wrapped) == expected


def test_real_sqlite_contention_preserves_operational_classification(runtime):
    from contextlib import closing

    with runtime.transaction() as writer, closing(sqlite3.connect(runtime.db_path, timeout=0)) as contender:
        writer.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError) as blocked:
            contender.execute("BEGIN IMMEDIATE")
        assert recovery_guard.classify_sqlite_operational_error(blocked.value) == "bounded_backoff"


def _query(repo, **options):
    return owner_recovery.list_pending_verification(repo.db_path.parent, **options)


def _pages(repo, **options):
    cursor = ""
    while True:
        rows, cursor = _query(repo, cursor=cursor, **options)
        yield rows
        if not cursor:
            return


def test_paging_interleaves_legacy_rows_without_omissions(runtime):
    first = _run(runtime, "first", source_session_id="session-one", scheduler_run_id="scheduled-one")
    second = _run(runtime, "second", source_session_id="session-two", scheduler_run_id="scheduled-two")
    threaded = _run(runtime, "threaded", thread_id="ordinary-thread")
    private = _run(runtime, "scheduled-private", thread_id="scheduler-thread", isolated_execution=True,
                   source_session_id="session-origin", scheduler_run_id="scheduled-private")
    foreign = _run(runtime, "foreign", owner_id="separate/owner")
    inserted, expected = [], []
    for index in range(1100):
        operation = f"op-{index:04d}"
        legacy = index % 11 == 0
        payload = {"result": {"effect_outcome": "unknown"}} if legacy else {}
        inserted.append((operation, first if index % 2 == 0 else second, "FAILED" if legacy else "UNKNOWN",
                         1 if legacy else 0, payload))
        expected.append(operation)
        if index % 25 == 0:
            inserted.append((f"known-{index}", first, "FAILED", 1, {"result": {"effect_outcome": "failed"}}))
    inserted.extend([
        ("settled", first, "UNKNOWN", 1, {}),
        ("private", private, "UNKNOWN", 0, {}),
        ("threaded", threaded, "UNKNOWN", 0, {}),
        ("foreign", foreign, "UNKNOWN", 0, {}),
    ])
    _insert_operations(runtime, inserted)
    with runtime.transaction() as conn:
        conn.execute("UPDATE tool_operations SET outcome_json='malformed' WHERE operation_id='known-0'")

    pages = list(_pages(runtime, limit=50, owner_id="local/main"))
    assert [len(page) for page in pages] == [50] * 22 + [1]
    rows = [row for page in pages for row in page]
    assert [row["operation_id"] for row in rows] == expected + ["private"]
    assert sum(row.get("legacy_normalized", False) for row in rows) == 100
    assert all(row["status"] == "UNKNOWN" for row in rows)
    for field, value, expected_ids in (
        ("source_session", "session-one", expected[::2]),
        ("scheduler_run", "scheduled-two", expected[1::2]),
        ("thread_id", "ordinary-thread", ["threaded"]),
        ("task_id", second["task_id"], expected[1::2]),
        ("operation_id", "op-0011", ["op-0011"]),
        ("source_session", "session-origin", ["private"]),
    ):
        found = [row["operation_id"] for page in _pages(runtime, owner_id="local/main", **{field: value}) for row in page]
        assert found == expected_ids
    _, cursor = _query(runtime, limit=2, owner_id="local/main")
    with pytest.raises(ValueError, match="cursor"):
        _query(runtime, limit=2, owner_id="local/main", source_session="session-one", cursor=cursor)


def _command(runtime, text, *, agent=None):
    parsed = parse_conversation_control(text)
    assert parsed is not None and parsed.valid
    return execute_turn_recovery_control(agent or _admin(runtime), None, parsed)


def test_recover_owner_pages_and_confirms_the_exact_original_operation(runtime):
    from agent_py_agent.agent.runtime_db.managed_operation_store import settle_pending_verification

    run = _run(runtime, "run-isolated")
    op_id = "tool_operation:original"
    _insert_operations(runtime, [(op_id, run, "UNKNOWN", 0, {"args_hash": "original"}),
                                 ("next-op", run, "FAILED", 1, {"result": {"effect_outcome": "unknown"}})])

    first = _command(runtime, "/recover owner --limit 1")
    assert [row["operation_id"] for row in first.details["pending_verification"]] == [op_id]
    cursor = first.details["next_pending_verification_cursor"]
    second = _command(runtime, f"/recover owner --limit 1 --cursor {cursor}")
    assert [row["operation_id"] for row in second.details["pending_verification"]] == ["next-op"]
    assert not second.details["next_pending_verification_cursor"]

    preview = _command(runtime, f"/recover owner operation {op_id} recorded")
    code = preview.details["confirmation_code"]
    revision = preview.details["operation"]["revision"]
    with runtime.transaction() as conn:
        conn.execute("UPDATE tool_operations SET tool_operation_generation=2 WHERE operation_id=?", (op_id,))
    stale = _command(runtime, f"/recover owner operation {op_id} recorded --confirm {code}")
    assert not stale.ok and stale.details["reason"] == "operation_revision_changed"
    outcome = settle_pending_verification(runtime, owner_id="local/main", operation_id=op_id,
                                          disposition="recorded", expected_revision=revision)
    assert outcome == {"settled": False, "reason": "operation_revision_changed"}

    current = _command(runtime, f"/recover owner operation {op_id} recorded")
    confirm = f"/recover owner operation {op_id} recorded --confirm {current.details['confirmation_code']}"
    assert _command(runtime, confirm).ok
    assert not _command(runtime, confirm).ok
    stored = runtime.get_operation(op_id)
    assert stored["status"] == "SUCCEEDED" and stored["tool_operation_generation"] == 3
    assert stored["attempt_id"] == run["attempt_id"]
    assert json.loads(stored["outcome_json"])["args_hash"] == "original"

    legacy = _command(runtime, "/recover owner operation next-op abandoned")
    assert _command(runtime, f"/recover owner operation next-op abandoned --confirm {legacy.details['confirmation_code']}").ok
    abandoned = runtime.get_operation("next-op")
    assert abandoned["status"] == "UNKNOWN" and abandoned["settled_at"] > 0
    assert _query(runtime) == ([], "")


def test_owner_verification_refuses_foreign_and_unprivileged_access(runtime):
    foreign = _run(runtime, "foreign", owner_id="foreign/owner")
    _insert_operations(runtime, [("op-foreign", foreign, "UNKNOWN", 0, {})])
    assert _command(runtime, "/recover owner --limit 1").details["pending_verification"] == []
    denied = _command(runtime, "/recover owner operation op-foreign recorded")
    assert not denied.ok and denied.details["reason"] == "operation_not_pending"
    ordinary = _admin(runtime)
    ordinary.home_paths.owner_kind = "user"
    blocked = _command(runtime, "/recover owner --limit 1", agent=ordinary)
    assert not blocked.ok and blocked.details["reason"] == "admin_required"


def test_c128_pending_query(runtime):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from time import monotonic

    from agent_py_agent.agent.runtime_db.managed_operation_store import settle_pending_verification

    run = _run(runtime, "concurrency")
    _insert_operations(runtime, [(f"op-{index:03d}", run, "UNKNOWN", 0, {}) for index in range(115)])
    snapshot, _ = _query(runtime, limit=8)
    readers = 128
    start = Barrier(readers + 1, timeout=45)

    def read_pages():
        start.wait()
        ids = [row["operation_id"] for page in _pages(runtime, limit=50) for row in page]
        assert len(ids) == len(set(ids))
        assert ids == sorted(ids)
        return ids

    with runtime._runtime_connection() as conn:
        busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0] / 1000
    with ThreadPoolExecutor(max_workers=readers) as pool:
        futures = [pool.submit(read_pages) for _ in range(readers)]
        start.wait()
        for record in snapshot:
            started = monotonic()
            result = settle_pending_verification(
                runtime, owner_id="local/main", operation_id=record["operation_id"],
                disposition="recorded", expected_revision=record["revision"],
            )
            assert result["settled"] and monotonic() - started < busy_timeout
        assert all(len(future.result(timeout=45)) >= 115 - len(snapshot) for future in futures)
    assert len([row for page in _pages(runtime, limit=50) for row in page]) == 115 - len(snapshot)


def test_owner_command_roundtrip_preserves_paging_and_confirmation():
    from agent_py_agent.cli.chat_parts.control_runtime import _recover_command_text

    for command in (
        "/recover owner --limit 12 --cursor aaaaaaaa-42",
        "/recover owner operation tool_operation:fixture recorded",
        "/recover owner operation tool_operation:fixture confirmed_noop --confirm abcdef012345",
    ):
        parsed = parse_conversation_control(command)
        assert parsed is not None and parsed.valid
        assert parse_conversation_control(_recover_command_text(parsed)) == parsed


def test_source_metadata_comes_from_the_claimed_scheduler_run():
    from agent_py_agent.agent.agent_core.runtime_mixin import _pending_operation_source_metadata

    active = {"run_id": "srun-fixture", "thread_id": "private-thread", "origin_thread_id": "origin",
              "status": "running", "claim_id": "claim", "owner": {"id": "local/main"},
              "execution": {"mode": "isolated"}}
    agent = SimpleNamespace(scheduler_repository=SimpleNamespace(get_active_run=lambda _id: active))
    params = SimpleNamespace(source="background_main_agent", task_id=active["run_id"],
                             task_attributes={"scheduler_run_id": active["run_id"], "origin_thread_id": "untrusted"})
    assert _pending_operation_source_metadata(agent, params, "local/main", "private-thread") == {
        "scheduler_run_id": "srun-fixture", "source_session_id": "origin", "isolated_execution": True,
    }
    assert _pending_operation_source_metadata(agent, params, "other/owner", "private-thread") == {}
    assert _pending_operation_source_metadata(agent, params, "local/main", "different-thread") == {}


def test_missing_database_does_not_create_state(tmp_path):
    owner_home = tmp_path / "missing"
    assert owner_recovery.list_pending_verification(owner_home) == ([], "")
    assert not owner_home.exists()


def test_keyset_uses_existing_index_without_offset(runtime):
    from contextlib import closing

    recorded = []

    class ReadProbe:
        def __init__(self, conn):
            self.conn = conn

        def execute(self, sql, params):
            assert "OFFSET" not in sql.upper()
            with closing(self.conn.execute("EXPLAIN QUERY PLAN " + sql, params)) as plan:
                recorded.extend(row[3] for row in plan.fetchall())
            return self.conn.execute(sql, params)

    with runtime._runtime_connection() as conn:
        owner_recovery._pending_operation_page(ReadProbe(conn), ("local/main", "", "", "", "", ""), 50, 51)
    assert sum("idx_tool_operations_status" in item and "rowid>?" in item for item in recorded) == 2


def _settle(runtime, operation_id, disposition):
    from agent_py_agent.agent.runtime_db.managed_operation_store import settle_pending_verification

    rows, _ = _query(runtime, owner_id="local/main", operation_id=operation_id, limit=1)
    assert [row["operation_id"] for row in rows] == [operation_id]
    return settle_pending_verification(runtime, owner_id="local/main", operation_id=operation_id,
                                       disposition=disposition, expected_revision=rows[0]["revision"])


def _replay(runtime, operation_id):
    from agent_py_agent.agent.runtime_db.managed_operation_store import _operation_record
    from agent_py_agent.agent.tooling.tool_operation_coordinator import (
        replay_completed_tool_operation,
    )

    record = _operation_record(runtime, operation_id, owner_id="local/main", run_id="", task_id="")
    return record, replay_completed_tool_operation(record)


# 读口把成功结果的 committed 投影为空 effect_outcome（与 _result_from_record 一致），原 handler 事实在 original_tool_execution。
@pytest.mark.parametrize(("disposition", "status", "ok", "effect", "replayed_effect"), [
    ("recorded", "succeeded", True, "committed", ""),
    ("confirmed_noop", "failed", False, "not_started", "not_started"),
])
def test_manual_verification_result_replays_as_the_confirmed_outcome(
    runtime, disposition, status, ok, effect, replayed_effect,
):
    run = _run(runtime, "run-isolated")
    op_id = f"tool_operation:{disposition}"
    _insert_operations(runtime, [(op_id, run, "UNKNOWN", 0, {"args_hash": "original"})])

    outcome = _settle(runtime, op_id, disposition)
    assert outcome["settled"], outcome
    record, replayed = _replay(runtime, op_id)
    assert replayed.error_code != "TOOL_OPERATION_OUTCOME_UNKNOWN", replayed
    assert record.status == status
    assert record.result.get("handler_executed") is ok and record.result.get("effect_outcome") == effect
    assert replayed.ok is ok and replayed.effect_outcome == replayed_effect
    operation = replayed.result_envelope["tool_operation"]
    assert operation["replayed"] is True and operation["original_tool_execution"]["handler_executed"] is ok
    assert _query(runtime) == ([], "")


def test_abandoned_manual_verification_still_replays_unknown(runtime):
    run = _run(runtime, "run-isolated")
    _insert_operations(runtime, [("tool_operation:abandoned", run, "UNKNOWN", 0, {})])
    assert _settle(runtime, "tool_operation:abandoned", "abandoned")["settled"]
    record, replayed = _replay(runtime, "tool_operation:abandoned")
    assert record.status == "unknown"
    assert replayed.error_code == "TOOL_OPERATION_OUTCOME_UNKNOWN"


def test_unreplayable_manual_result_fails_closed_without_state_change(runtime, monkeypatch):
    from agent_py_agent.agent.tooling import tool_operation_coordinator

    run = _run(runtime, "run-isolated")
    op_id = "tool_operation:guarded"
    _insert_operations(runtime, [(op_id, run, "UNKNOWN", 0, {"args_hash": "original"})])
    before = dict(runtime.get_operation(op_id))
    # 确定性错误用例：模拟读口合同与写出结果漂移。
    monkeypatch.setattr(tool_operation_coordinator, "_complete_terminal_result", lambda _record: False)

    outcome = _settle(runtime, op_id, "recorded")
    assert outcome == {"settled": False, "reason": "operation_result_not_replayable"}
    assert dict(runtime.get_operation(op_id)) == before
    assert [row["operation_id"] for row in _query(runtime)[0]] == [op_id]
