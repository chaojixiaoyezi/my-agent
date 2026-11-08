"""候选只读真实 SQLite 前沿：计量 VM、实际取行、排序及完整发现兜底。"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing

import pytest

from agent_py_agent.agent.owner_activity_index import OwnerActivityIndex

NOW = 1000
FACTS_BEFORE = 900
KINDS = ("runtime_wake", "scheduler_due", "maintenance")
COLUMNS = ("next_wake_at", "next_job_due_at", "next_maintenance_at")
CASES = ("fresh", "future", "unknown", "stale", *KINDS, "mixed")


def _state(case, number):
    states = {
        "fresh": (0, 0, 2000, None, None, None),
        "future": (0, 0, 2000, 2001, 2002, 2003),
        "unknown": (
            (None, 0, 2000, None, None, None),
            (0, None, 2000, None, None, None),
            (0, 0, None, None, None, None),
        )[number % 3],
        "stale": (0, 0, FACTS_BEFORE, None, None, None),
        "runtime_wake": (0, 0, 2000, NOW, None, None),
        "scheduler_due": (0, 0, 2000, None, NOW, None),
        "maintenance": (0, 0, 2000, None, None, NOW),
    }
    if case == "mixed":
        # 第一类同时命中五个前沿，其余四类只命中自己的前沿。
        return (
            (None, None, 800, 800, 800, 800),
            states["stale"], states["runtime_wake"],
            states["scheduler_due"], states["maintenance"],
        )[number % 5]
    return states[case]


def _populate(index, count, case):
    with closing(sqlite3.connect(index.path)) as conn, conn:
        conn.executemany(
            "INSERT INTO owner_activity(provider,owner_kind,owner_id,hard_facts,soft_facts,"
            "facts_checked_at,next_wake_at,next_job_due_at,next_maintenance_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (("feishu", "user", f"u{number:07}", *_state(case, number)) for number in range(count)),
        )


def _measure(index, monkeypatch, *, limit=4):
    observations, steps = [], [0]
    original = index._connect

    def connect():
        conn = original()

        def trace(sql):
            observations.append({"sql": sql, "rows": []})

        def row_factory(cursor, row):
            observations[-1]["rows"].append(tuple(row))
            return sqlite3.Row(cursor, row)

        def progress():
            steps[0] += 1
            return 0

        conn.set_trace_callback(trace)
        conn.row_factory = row_factory
        conn.set_progress_handler(progress, 1)
        return conn

    with monkeypatch.context() as patch:
        patch.setattr(index, "_connect", connect)
        result = index.priority_owners(now=NOW, facts_before=FACTS_BEFORE, limit=limit)
    # EXPLAIN 和夹具构建不计入查询指令数；SQL 来自实际执行的 trace。
    with closing(original()) as conn:
        for observation in observations:
            observation["plan"] = [row[3] for row in conn.execute(
                "EXPLAIN QUERY PLAN " + observation["sql"]
            )]
    return result, observations, steps[0]


def _assert_bounded(result, observations, steps, *, limit=4):
    assert steps < 2000, f"查询执行了 {steps} VM steps，limit 没有约束底层工作"
    assert 1 <= len(observations) <= 5
    assert len(result) <= limit and len(result) == len(set(result))
    for observation in observations:
        assert observation["sql"].lstrip().upper().startswith("SELECT ")
        assert len(observation["rows"]) <= limit
        assert any("INDEX" in detail for detail in observation["plan"])
        assert not any("USE TEMP B-TREE" in detail for detail in observation["plan"])
        assert "SCAN owner_activity" not in observation["plan"]


@pytest.mark.parametrize("count", [10_000, 500_000])
@pytest.mark.parametrize("case", CASES)
def test_priority_frontiers_bound_vm_steps_and_materialized_rows(
    tmp_path, monkeypatch, record_testsuite_property, count, case,
):
    index = OwnerActivityIndex(tmp_path / "activity.db")
    _populate(index, count, case)
    result, observations, steps = _measure(index, monkeypatch)
    record_testsuite_property(f"W2_{case}_{count}", json.dumps({
        "vm_steps": steps, "branch_rows": [len(item["rows"]) for item in observations],
        "plans": [item["plan"] for item in observations], "result": result,
    }))
    _assert_bounded(result, observations, steps)
    if case in {"fresh", "future"}:
        assert result == []
    else:
        assert len(result) == 4
        for provider, kind, identifier in result:
            assert (provider, kind) == ("feishu", "user")
            hard, soft, checked, *deadlines = _state(case, int(identifier[1:]))
            assert (hard is None or soft is None or checked is None or checked <= FACTS_BEFORE
                    or any(due is not None and due <= NOW for due in deadlines))


def test_existing_database_acquires_unknown_partial_index_without_changing_rows(tmp_path, monkeypatch):
    index = OwnerActivityIndex(tmp_path / "existing.db")
    _populate(index, 10_000, "unknown")
    with closing(sqlite3.connect(index.path)) as conn, conn:
        # 模拟原版本持久库：只有原来的事实时间和三个期限索引。
        partial = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND sql LIKE '%WHERE%'"
        ).fetchall()
        for (name,) in partial:
            conn.execute('DROP INDEX "' + name.replace('"', '""') + '"')
        before = list(conn.execute("SELECT * FROM owner_activity ORDER BY owner_id"))
    reopened = OwnerActivityIndex(index.path)
    result, observations, steps = _measure(reopened, monkeypatch)
    _assert_bounded(result, observations, steps)
    assert len(result) == 4
    with closing(sqlite3.connect(index.path)) as conn:
        assert list(conn.execute("SELECT * FROM owner_activity ORDER BY owner_id")) == before
        assert {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )} == {"owner_activity", "deadline_revisions"}


@pytest.mark.parametrize("limit", [0, -4, 1, 4, "2"])
def test_limit_normalization_and_duplicate_frontiers(tmp_path, monkeypatch, limit):
    index = OwnerActivityIndex(tmp_path / "activity.db")
    _populate(index, 19, "mixed")
    expected_limit = max(1, int(limit))
    result, observations, steps = _measure(index, monkeypatch, limit=limit)
    _assert_bounded(result, observations, steps, limit=expected_limit)
    assert len(result) == expected_limit
    # 同一身份确实出现在多个实际前沿，不以空集合充当去重证明。
    flattened = [key for item in observations for key in item["rows"]]
    assert len(flattened) > len(set(flattened))
    assert index.priority_owners(now=NOW, facts_before=FACTS_BEFORE, limit=limit) == result


@pytest.mark.parametrize("case", ["unknown", "stale"])
def test_checked_facts_advance_to_every_remaining_candidate(tmp_path, case):
    index = OwnerActivityIndex(tmp_path / "activity.db")
    _populate(index, 19, case)
    seen = set()
    for revision in range(1, 21):
        page = index.priority_owners(now=NOW, facts_before=FACTS_BEFORE, limit=4)
        if not page:
            break
        assert len(page) <= 4 and seen.isdisjoint(page)
        seen.update(page)
        for key in page:
            index.note_facts(key, False, False, revision=revision)
    assert seen == {("feishu", "user", f"u{number:07}") for number in range(19)}
    assert index.priority_owners(now=NOW, facts_before=FACTS_BEFORE, limit=4) == []


@pytest.mark.parametrize("kind,column", zip(KINDS, COLUMNS))
def test_cancelled_deadline_and_unknown_facts_keep_revision_and_idle_guards(tmp_path, kind, column):
    index = OwnerActivityIndex(tmp_path / "activity.db")
    owner = ("feishu", "user", "alice")
    index.note_facts(owner, False, False, revision=8)
    index.note_activity(owner, 1)
    for source in ("inflight", "streams", "processes"):
        index.register_activity_probe(source, lambda key: 0)
    index.note_deadline(owner, kind, NOW, revision=7)
    assert index.priority_owners(now=NOW, facts_before=FACTS_BEFORE, limit=1) == [owner]
    index.note_deadline(owner, kind, None, revision=8)
    index.note_deadline(owner, kind, NOW - 1, revision=7)
    assert index.priority_owners(now=NOW, facts_before=FACTS_BEFORE, limit=1) == []
    row = index.row(owner)
    assert row[column] is None and row["deadline_revisions"][kind] == 8
    assert index.idle_report(owner, wake_margin_seconds=5, idle_seconds=10, now=NOW).idle
    # 另一 kind 的独立较低修订仍可发表，但不能恢复已取消的 kind。
    other = next(value for value in KINDS if value != kind)
    index.note_deadline(owner, other, NOW, revision=1)
    assert index.priority_owners(now=NOW, facts_before=FACTS_BEFORE, limit=1) == [owner]
    index.note_deadline(owner, other, None, revision=2)
    index.note_facts(owner, None, None, revision=9)
    index.note_facts(owner, False, False, revision=8)
    assert index.priority_owners(now=NOW, facts_before=FACTS_BEFORE, limit=1) == [owner]
    report = index.idle_report(owner, wake_margin_seconds=5, idle_seconds=10, now=NOW)
    assert not report.idle and {"hard_facts", "soft_facts"} <= set(report.unknown_sources)
    assert index.row(owner)["facts_revision"] == 9


def test_periodic_discovery_advances_despite_a_permanently_unknown_hint(tmp_path, monkeypatch):
    from agent_py_agent.agent import owner_wake_discovery as discovery

    owners = tmp_path / "owners"
    identifiers = {"a-stuck", "b-fresh", "c-unindexed", "d-unindexed", "e-unindexed"}
    for identifier in identifiers:
        (owners / "providers/feishu/users" / identifier).mkdir(parents=True)
    bad = owners / "providers/feishu/users/a-stuck/memory/curator"
    bad.mkdir(parents=True)
    (bad / "state.json").write_text("[]")
    monkeypatch.setattr(discovery, "_owner_fact_kind", lambda home: "hard")
    index = OwnerActivityIndex(tmp_path / "activity.db")
    stuck = ("feishu", "user", "a-stuck")
    index.note_facts(stuck, None, None, revision=1)
    index.note_facts(("feishu", "user", "b-fresh"), False, False, revision=1)
    binding = discovery.bind_owner_activity_index(owners, index, sweep_seconds=1800, freshness_seconds=60)
    try:
        binding.next_sweep_at = float("inf")
        hint_page = discovery.discover_wake_pending_owner_page(owners, limit=1)
        assert {owner.owner_id for owner in hint_page.owners} == {"a-stuck"}
        assert index.row(stuck)["hard_facts"] is None
        binding.next_sweep_at = 0
        seen, cursor = set(), None
        for _ in range(10):
            page = discovery.discover_wake_pending_owner_page(owners, limit=1, after_cursor=cursor)
            assert page.scanned <= 2
            seen.update(owner.owner_id for owner in page.owners)
            cursor = page.next_cursor
            if cursor is None:
                break
        assert cursor is None and seen == identifiers
        assert index.row(stuck)["hard_facts"] is None
        assert all(index.row(("feishu", "user", identifier)) is not None for identifier in identifiers)
    finally:
        binding.close()


def test_empty_projection_is_bounded_and_sql_errors_remain_visible(tmp_path, monkeypatch):
    index = OwnerActivityIndex(tmp_path / "activity.db")
    result, observations, steps = _measure(index, monkeypatch)
    _assert_bounded(result, observations, steps)
    assert result == []
    with closing(sqlite3.connect(index.path)) as conn, conn:
        conn.execute("DROP TABLE owner_activity")
    with pytest.raises(sqlite3.OperationalError):
        index.priority_owners(now=NOW, facts_before=FACTS_BEFORE, limit=4)
