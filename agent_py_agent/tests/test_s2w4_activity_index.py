"""未知来源、跨kind修订与可重建性的运行合同。"""
from __future__ import annotations

from agent_py_agent.agent.owner_activity_index import OwnerActivityIndex

OWNER = ("feishu", "user", "alice")


def test_deadline_projection_is_persisted(tmp_path):
    index = OwnerActivityIndex(tmp_path / "activity.sqlite3")
    index.note_deadline(OWNER, "runtime_wake", 200, revision=1)
    assert index.row(OWNER) is not None
    assert OwnerActivityIndex(index.path).row(OWNER)["next_wake_at"] == 200


def test_restart_revision_is_seeded_above_persisted_revision(tmp_path, monkeypatch):
    from agent_py_agent.agent import owner_activity_index as module
    index = OwnerActivityIndex(tmp_path / "activity.sqlite3")
    index.note_deadline(OWNER, "runtime_wake", 200, revision=5000)
    monkeypatch.setattr(module, "_REVISION", 1)
    OwnerActivityIndex(index.path)
    assert module.next_hint_revision() > 5000


def test_null_deadlines_are_infinity_and_missing_probes_are_unknown(tmp_path):
    index = OwnerActivityIndex(tmp_path / "activity.sqlite3")
    index.note_facts(OWNER, False, False, revision=1)
    index.note_activity(OWNER, 1)
    report = index.idle_report(OWNER, wake_margin_seconds=5, idle_seconds=10, now=100)
    assert report.earliest_deadline is None
    assert not report.idle
    assert set(report.unknown_sources) == {"inflight", "streams", "processes"}


def test_live_probes_never_touch_last_activity_and_do_not_authorize_unknown(tmp_path):
    index = OwnerActivityIndex(tmp_path / "activity.sqlite3")
    index.note_facts(OWNER, False, False, revision=1)
    index.note_activity(OWNER, 1)
    for source in ("inflight", "streams", "processes"):
        index.register_activity_probe(source, lambda owner: 0)
    report = index.idle_report(OWNER, wake_margin_seconds=5, idle_seconds=10, now=100)
    assert report.idle
    assert index.row(OWNER)["last_activity_at"] == 1
    index.register_activity_probe("streams", lambda owner: None)
    assert not index.idle_report(OWNER, wake_margin_seconds=5, idle_seconds=10, now=100).idle
    index.register_activity_probe("streams", lambda owner: 1)
    assert not index.idle_report(OWNER, wake_margin_seconds=5, idle_seconds=10, now=100).idle
    index.note_facts(OWNER, None, None, revision=2)
    report = index.idle_report(OWNER, wake_margin_seconds=5, idle_seconds=10, now=100)
    assert not report.idle and "hard_facts" in report.unknown_sources


def test_stale_revisions_do_not_restore_cancelled_deadline_or_facts(tmp_path):
    index = OwnerActivityIndex(tmp_path / "activity.sqlite3")
    index.note_deadline(OWNER, "runtime_wake", 200, revision=2)
    index.note_deadline(OWNER, "runtime_wake", None, revision=3)
    index.note_deadline(OWNER, "runtime_wake", 100, revision=2)
    index.note_deadline(OWNER, "scheduler_due", 300, revision=1)
    index.note_facts(OWNER, None, None, revision=4)
    index.note_facts(OWNER, False, False, revision=3)
    row = index.row(OWNER)
    assert row["next_wake_at"] is None and row["next_job_due_at"] == 300
    assert row["hard_facts"] is None and row["facts_revision"] == 4
    assert row["deadline_revisions"]["runtime_wake"] == 3


def test_deadlines_activity_and_hard_facts_all_block_idle(tmp_path):
    index = OwnerActivityIndex(tmp_path / "activity.sqlite3")
    for source in ("inflight", "streams", "processes"):
        index.register_activity_probe(source, lambda owner: 0)
    index.note_facts(OWNER, False, False, revision=1)
    index.note_activity(OWNER, 1)
    for revision, due in enumerate((105, 106), 1):
        index.note_deadline(OWNER, "runtime_wake", due, revision=revision)
        report = index.idle_report(OWNER, wake_margin_seconds=5, idle_seconds=10, now=100)
        assert report.earliest_deadline == due
        assert report.idle is (due > 105)
    index.note_activity(OWNER, 99)
    assert not index.idle_report(OWNER, wake_margin_seconds=5, idle_seconds=10, now=100).idle
    index.note_activity(OWNER, 1)
    assert index.row(OWNER)["last_activity_at"] == 99


def test_rebuild_matches_incremental_updates(tmp_path):
    first = OwnerActivityIndex(tmp_path / "first.sqlite3")
    second = OwnerActivityIndex(tmp_path / "second.sqlite3")
    records = []
    for value in range(21):
        owner = ("feishu", "group", str(value))
        data = {"hard_facts": bool(value % 2), "soft_facts": False, "facts_revision": value + 1,
                "next_wake_at": 200 + value, "last_activity_at": 5}
        records.append((owner, data))
        first.note_facts(owner, data["hard_facts"], False, revision=value + 1)
        first.note_deadline(owner, "runtime_wake", 200 + value, revision=value + 1)
        first.note_activity(owner, 5)
    second.rebuild(iter(records))
    for owner, _data in records:
        left, right = first.row(owner), second.row(owner)
        left.pop("facts_checked_at")
        right.pop("facts_checked_at")
        assert left == right


def test_index_first_discovery_matches_full_sweep_and_preserves_unknown(tmp_path, monkeypatch):
    import json
    import random

    from agent_py_agent.agent import owner_wake_discovery as discovery

    assert callable(getattr(discovery, "bind_owner_activity_index", None))
    owners = tmp_path / "owners"
    rng = random.Random(71)
    for value in range(37):
        home = owners / "providers/feishu/users" / f"u{value:03}"
        home.mkdir(parents=True)
        (home / "fixture-hard").write_text(str(rng.randrange(2)))
    monkeypatch.setattr(discovery, "_owner_fact_kind", lambda home: "hard" if (home / "fixture-hard").read_text() == "1" else "none")
    expected, cursor = set(), None
    while True:
        page = discovery.discover_wake_pending_owner_page(owners, limit=5, after_cursor=cursor)
        expected.update(owner.owner_id for owner in page.owners)
        cursor = page.next_cursor
        if cursor is None:
            break
    index = OwnerActivityIndex(tmp_path / "activity.sqlite3")
    binding = discovery.bind_owner_activity_index(owners, index, sweep_seconds=1800, freshness_seconds=60)
    actual, cursor = set(), None
    try:
        while True:
            page = discovery.discover_wake_pending_owner_page(owners, limit=5, after_cursor=cursor)
            assert page.scanned <= 10
            actual.update(owner.owner_id for owner in page.owners)
            cursor = page.next_cursor
            if cursor is None:
                break
        assert actual == expected
        key = ("feishu", "user", "u000")
        (owners / "providers/feishu/users/u000/data/scheduler").mkdir(parents=True)
        (owners / "providers/feishu/users/u000/data/scheduler/store.json").write_text(json.dumps([]))
        from agent_py_agent.agent.owner_activity_index import next_hint_revision
        index.note_facts(key, None, None, revision=next_hint_revision())
        discovery.discover_wake_pending_owner_page(owners, limit=5)
        row = index.row(key)
        assert row["hard_facts"] is None and row["soft_facts"] is None
        assert not index.idle_report(key, wake_margin_seconds=5, idle_seconds=10, now=100).idle
    finally:
        binding.close()


# LLM: 观察实际目录流入口；分页只续读原流，不重复枚举整个bucket。
# 函数用途: 验证相邻页只枚举一次bucket，结构变化仍立即重建。
def test_bound_discovery_reuses_topology_within_a_page_budget(tmp_path, monkeypatch):
    import os

    from agent_py_agent.agent import owner_wake_discovery as discovery
    owners = tmp_path / "owners"
    bucket = owners / "providers/feishu/users"
    for value in range(40):
        (bucket / str(value)).mkdir(parents=True)
    index = OwnerActivityIndex(tmp_path / "activity.sqlite3")
    binding = discovery.bind_owner_activity_index(owners, index, sweep_seconds=1800, freshness_seconds=60)
    calls = []
    original = os.scandir

    def scandir(path):
        if path == bucket:
            calls.append(path)
        return original(path)

    monkeypatch.setattr(os, "scandir", scandir)
    try:
        cursor, seen = None, set()
        while True:
            page = discovery.discover_owner_home_page(owners, limit=4, after_cursor=cursor)
            page_ids = {target.identity.owner_id for target in page.targets}
            assert page.scanned <= 4 and len(page.targets) <= 4
            assert seen.isdisjoint(page_ids)
            seen.update(page_ids)
            cursor = page.next_cursor
            if cursor is None:
                break
        assert seen == {str(value) for value in range(40)}
        assert len(calls) == 1
        (bucket / "new").mkdir()
        page = discovery.discover_owner_home_page(owners, limit=64)
        assert {target.identity.owner_id for target in page.targets} == seen | {"new"}
        assert len(calls) == 2
    finally:
        binding.close()


def test_hint_validation_failure_keeps_hard_owner_and_index_failure_keeps_scan(tmp_path, monkeypatch):
    from agent_py_agent.agent import owner_wake_discovery as discovery
    owners = tmp_path / "owners"
    home = owners / "providers/feishu/users/alice"
    (home / "memory/curator").mkdir(parents=True)
    (home / "memory/curator/state.json").write_text("[]")
    monkeypatch.setattr(discovery, "_owner_fact_kind", lambda home: "hard")
    index = OwnerActivityIndex(tmp_path / "activity.sqlite3")
    binding = discovery.bind_owner_activity_index(owners, index, sweep_seconds=1800, freshness_seconds=60)
    try:
        assert [owner.owner_id for owner in discovery.discover_wake_pending_owner_page(owners).owners] == ["alice"]
        assert index.row(OWNER)["hard_facts"] is None

        def unavailable(**kwargs):
            raise OSError("index unavailable")

        monkeypatch.setattr(index, "priority_owners", unavailable)
        assert [owner.owner_id for owner in discovery.discover_wake_pending_owner_page(owners).owners] == ["alice"]
    finally:
        binding.close()


def test_deleted_owner_projection_is_pruned_without_starving_next_owner(tmp_path, monkeypatch):
    from agent_py_agent.agent import owner_wake_discovery as discovery
    owners = tmp_path / "owners"
    home = owners / "providers/feishu/users/z-real"
    home.mkdir(parents=True)
    monkeypatch.setattr(discovery, "_owner_fact_kind", lambda home: "hard")
    index = OwnerActivityIndex(tmp_path / "activity.sqlite3")
    index.rebuild([("feishu", "user", key) for key in ("a-gone", "b-gone", "z-real")])
    binding = discovery.bind_owner_activity_index(owners, index, sweep_seconds=1800, freshness_seconds=60)
    binding.next_sweep_at = float("inf")
    try:
        discovery.discover_wake_pending_owner_page(owners, limit=2)
        assert index.row(("feishu", "user", "a-gone")) is None
        page = discovery.discover_wake_pending_owner_page(owners, limit=2)
        assert [owner.owner_id for owner in page.owners] == ["z-real"]
    finally:
        binding.close()
