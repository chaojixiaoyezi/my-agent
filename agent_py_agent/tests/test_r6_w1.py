"""引用保护按路径深度匹配，来源写入后不得沿用旧投影。"""
import json
import os
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_py_agent.agent.memory_store import retention_scan as scan
from agent_py_agent.agent.memory_store.retention_scan_budget import RetentionScanBudget
from agent_py_agent.tests.test_memory_retention_wave5 import (
    NOW,
    _expired_pin_target,
    _freeze_pin_file_stat,
    old_file,
    runtime,
)


@pytest.fixture(autouse=True)
def projection_cache():
    yield
    with scan._PIN_BUILDS_LOCK:
        snapshots = list(scan._PIN_BUILDS.values())
        scan._PIN_BUILDS.clear()
    for snapshot in snapshots:
        snapshot.close()


@pytest.fixture
def cold_clock(monkeypatch):
    settled = time.time_ns() + 3_000_000_000
    monkeypatch.setattr(scan.reference_pins.time, "time_ns", lambda: settled)


def projection_step(home, candidates, cursor=None, *, entries=97, size=4096):
    policy, error = scan._load_policy(home.owner_retention_json)
    assert error is None
    context = scan._RetentionScanContext(home, candidates, policy, scan._conversation_roots(home, ()), NOW,
        scan_budget=RetentionScanBudget(entries, size, float("inf")), scan_cursor=json.loads(json.dumps(cursor or {})))
    result = scan._budgeted_pin_projection(context)
    assert context.scan_budget.entries <= entries and context.scan_budget.size <= size
    assert not context.errors, context.errors
    assert len(json.dumps(context.scan_cursor)) < 256
    return context, result[0]


def finish_projection(home, candidates, cursor=None, **limits):
    for _ in range(3000):
        context, result = projection_step(home, candidates, cursor, **limits)
        if result is not None:
            assert result[1]
            return result[0], context
        assert context.scan_partial
        cursor = context.scan_cursor
    pytest.fail(f"pin projection stalled: {context.scan_cursor}")


@pytest.mark.parametrize("pin,target,protected", [
    ("a/b", "a/b", True), ("a/b", "a", True),
    ("a/b", "a/b/c", True), ("a/b", "a/bb", False),
])
def test_path_boundaries_and_writers(tmp_path, pin, target, protected):
    pins = scan._RetentionPins(tmp_path)
    pins.collect({"path": str(tmp_path / pin)}, tmp_path / "source.json")
    assert pins.protects(tmp_path / target, []) is protected
    pins.paths = {tmp_path / "replacement"}
    assert not pins.protects(tmp_path / "a/b", [])
    pins.paths.clear()
    assert not pins.protects(tmp_path / "replacement", [])
    pins.collect({"path": str(tmp_path / "later")}, tmp_path / "source.json")
    assert pins.protects(tmp_path / "later/child", [])
    recovered = scan._RetentionPins(tmp_path, paths={tmp_path / "recovered"})
    pins.complete, pins.target_rebuild = False, lambda errors: (recovered, True)
    assert pins.complete_for(tmp_path / "recovered", [])
    assert pins.protects(tmp_path / "recovered/child", [])


@pytest.mark.parametrize("count", [100, 1000, 10000])
def test_lookup_never_compares_history_paths(tmp_path, monkeypatch, count):
    pins = scan._RetentionPins(tmp_path, paths={tmp_path / "pins" / str(i) for i in range(count)})
    equal, resolve = Path.__eq__, Path.resolve
    calls = {"equal": 0, "resolve": 0}

    def compared(path, other):
        calls["equal"] += 1
        return equal(path, other)

    def resolved(path, *args, **kwargs):
        calls["resolve"] += 1
        return resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "__eq__", compared)
    monkeypatch.setattr(Path, "resolve", resolved)
    for index in range(20):
        assert not pins.protects(tmp_path / f"missing-{index}", [])
    assert calls == {"equal": 0, "resolve": 20}


@pytest.mark.parametrize("kind", ["link", "unreadable"])
def test_links_and_unreadable_sources_keep_protection(tmp_path, monkeypatch, kind):
    home, _candidates, _service = runtime(tmp_path)
    source = old_file(home.owner_memory_curator_runs_dir / "pins.jsonl", '{"source_refs": ["evidence/a"]}\n')
    if kind == "link":
        target = tmp_path / "target"
        target.touch()
        link = tmp_path / "link"
        link.symlink_to(target)
        pins = scan._RetentionPins(tmp_path, paths={target})
        assert pins.protects(link, [])
    else:
        original = scan._fingerprint_source

        def unreadable(path):
            if path == source:
                raise PermissionError("single unreadable pin source")
            return original(path)

        monkeypatch.setattr(scan, "_fingerprint_source", unreadable)
        errors = []
        pins, complete = scan.current_retention_pins(home, (), errors, now=NOW)
        assert not complete and not pins.complete and errors


def test_shared_ancestor_counts_survive_remove_and_same_size_update(tmp_path):
    pins = scan._RetentionPins(tmp_path, paths={tmp_path / "a/x", tmp_path / "a/y"})
    pins.paths.discard(tmp_path / "a/x")
    assert pins.protects(tmp_path / "a", [])
    pins.paths -= {tmp_path / "a/y"}
    pins.paths |= {tmp_path / "b/x"}
    assert not pins.protects(tmp_path / "a", []) and pins.protects(tmp_path / "b", [])
    pins.paths.pop()
    assert not pins.protects(tmp_path / "b", [])


@pytest.mark.parametrize("count", [100, 1000, 10000])
def test_cold_source_resumes_without_rereading_prefix(tmp_path, monkeypatch, cold_clock, count):
    home, candidates, _service = runtime(tmp_path)
    source = old_file(home.owner_memory_curator_runs_dir / "cold.jsonl", "".join(
        json.dumps({"source_refs": [f"evidence/{index}"]}) + "\n" for index in range(count)))
    opened, sizes = [], []
    original = scan._fingerprint_source

    class Meter:
        def __init__(self, raw):
            self.raw = raw

        def __getattr__(self, name):
            return getattr(self.raw, name)

        def read1(self, size):
            data = self.raw.read1(size)
            sizes.append(len(data))
            return data

    def measured(path):
        raw = original(path)
        if path == source:
            opened.append(path)
            return Meter(raw)
        return raw

    monkeypatch.setattr(scan, "_fingerprint_source", measured)
    context, result = projection_step(home, candidates)
    assert result is None and context.scan_partial
    pins, _context = finish_projection(home, candidates, context.scan_cursor)
    assert pins.protects(home.owner_home_dir / f"evidence/{count - 1}", [])
    assert not pins.protects(home.owner_home_dir / "evidence/missing", [])
    assert len(opened) == 2 and sum(sizes) == 2 * source.stat().st_size
    assert max(sizes) <= 4096


def test_partial_plan_never_deletes_and_complete_plan_keeps_references(tmp_path, cold_clock):
    home, _candidates, service = runtime(tmp_path)
    target, control = (_expired_pin_target(home, name) for name in ("pinned.jsonl", "control.jsonl"))
    old_file(home.owner_memory_curator_runs_dir / "cold.jsonl", json.dumps(
        {"source_refs": [str(target)] + [f"evidence/{i}" for i in range(100)]}) + "\n")
    cursor, partials = {}, 0
    for _ in range(300):
        budget = RetentionScanBudget(17, 128, float("inf"))
        plan = service.plan(now=NOW, scan_budget=budget, scan_cursor=cursor)
        assert not plan.errors
        assert budget.entries <= 17 and budget.size <= 128
        if plan.scan_cursor.get(scan._PIN_CURSOR_KEY, {}).get("phase") in {"build", "verify"}:
            assert plan.scan_partial and not plan.actions
            partials += 1
        assert target.exists() and control.exists()
        cursor = json.loads(json.dumps(plan.scan_cursor))
        if not plan.scan_partial:
            break
    else:
        pytest.fail(f"planner stalled: {cursor}")
    assert partials > 1
    assert any(action.path == control for action in plan.actions)
    assert not any(action.path == target for action in plan.actions)


@pytest.mark.parametrize("expired", [False, True])
def test_deadline_starts_after_fixed_discovery_prelude(tmp_path, monkeypatch, expired):
    home, _candidates, service = runtime(tmp_path)
    clock = [0.0]
    original = scan._conversation_roots

    def slow_prelude(*args):
        roots = original(*args)
        clock[0] += 1.0
        return roots

    monkeypatch.setattr(scan.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(scan, "_conversation_roots", slow_prelude)
    budget = RetentionScanBudget(1, 4096, -1 if expired else .025)
    plan = service.plan(now=NOW, scan_budget=budget)
    assert plan.scan_partial and not plan.actions
    assert budget.entries == (0 if expired else 1)
    assert budget.deadline == pytest.approx(1.0 if expired else 1.025)


def test_actual_scan_io_exhausts_time_before_unbounded_source_build(tmp_path, monkeypatch):
    home, _candidates, service = runtime(tmp_path)
    old_file(home.owner_memory_curator_runs_dir / "cold.jsonl", '{"source_refs":["evidence/large"]}\n' * 1000)
    clock, reads = [0.0], []
    original = scan._fingerprint_source

    class SlowSource:
        def __init__(self, raw):
            self.raw = raw

        def __getattr__(self, key):
            return getattr(self.raw, key)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.raw.__exit__(*args)

        def read1(self, size):
            raw = self.raw.read1(size)
            reads.append(len(raw))
            clock[0] += .026
            return raw

    monkeypatch.setattr(scan, "_fingerprint_source", lambda path: SlowSource(original(path)))
    monkeypatch.setattr(scan.retention_scan_budget, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    budget = RetentionScanBudget(1000, 1024 * 1024, .025)
    report = service.plan(now=NOW, scan_budget=budget)
    assert report.scan_partial and not report.actions
    assert len(reads) == 1 and budget.size == sum(reads)


def test_generation_change_during_pending_verification_restarts_closed(tmp_path, monkeypatch, cold_clock):
    home, candidates, _service = runtime(tmp_path)
    source = old_file(home.owner_memory_curator_runs_dir / "cold.jsonl", '{"source_refs":["evidence/old"]}\n')
    cursor = {}
    for _ in range(100):
        context, result = projection_step(home, candidates, cursor, entries=1)
        assert result is None
        cursor = context.scan_cursor
        if cursor[scan._PIN_CURSOR_KEY]["phase"] == "verify":
            break
    else:
        pytest.fail("never entered pending verification")
    snapshot = scan._PIN_BUILDS[Path(home.owner_home_dir)]
    assert snapshot.pending is not None and snapshot.value is None
    source.write_text('{"source_refs":["evidence/new"]}\n')
    monkeypatch.setattr(scan, "current_retention_pins", lambda *a, **k: pytest.fail("unbudgeted restart"))
    pins, context = finish_projection(home, candidates, cursor, entries=7)
    assert pins.protects(home.owner_home_dir / "evidence/new", [])
    assert not pins.protects(home.owner_home_dir / "evidence/old", [])
    assert context.scan_cursor[scan._PIN_CURSOR_KEY]["token"] != snapshot.token


def test_lost_in_memory_continuation_restarts_instead_of_trusting_cursor(tmp_path, cold_clock):
    home, candidates, _service = runtime(tmp_path)
    context, result = projection_step(home, candidates, entries=1)
    assert result is None
    old = scan._PIN_BUILDS.pop(Path(home.owner_home_dir))
    old.close()
    resumed, result = projection_step(home, candidates, context.scan_cursor, entries=1)
    assert result is None and resumed.scan_partial
    assert resumed.scan_cursor[scan._PIN_CURSOR_KEY]["token"] != old.token
    finish_projection(home, candidates, resumed.scan_cursor)


def test_budgeted_discovery_without_scandir_descriptor_support(tmp_path, monkeypatch, cold_clock):
    home, candidates, _service = runtime(tmp_path)
    old_file(home.owner_memory_curator_runs_dir / "portable.jsonl", '{"source_refs":["evidence/portable"]}\n')
    monkeypatch.setattr(os, "supports_fd", os.supports_fd - {os.scandir})
    pins, _context = finish_projection(home, candidates)
    assert pins.protects(home.owner_home_dir / "evidence/portable", [])


@pytest.mark.parametrize("quota", [100 * 1024**2, 1024**3, 100 * 1024**3])
def test_cold_projection_preserves_existing_storage_quota(tmp_path, cold_clock, quota):
    from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope
    from agent_py_agent.agent.user_space.owner_quota import OwnerQuotaEnforcer

    home, candidates, _service = runtime(tmp_path)
    enforcer = OwnerQuotaEnforcer(home.owner_home_dir, max_bytes=quota)
    candidates.quota_enforcer = enforcer
    target = home.owner_home_dir / "evidence/quota"
    candidates.observe(CandidateObservation(candidate_type="lesson", content="配额内的证据",
        subject_key="r6.quota", scope=MemoryScope("personal", "personal"), origin="model_inferred",
        promotion_target="lesson", evidence_refs=[{"path": str(target)}]))
    before = candidates.path.read_bytes()
    pins, _context = finish_projection(home, candidates, entries=11)
    assert pins.protects(target, [])
    assert enforcer.max_bytes == quota and candidates.path.read_bytes() == before


@pytest.mark.parametrize("generation", [False, True])
@pytest.mark.parametrize("removed", [False, True])
def test_incremental_formal_sources_keep_existing_tombstone_semantics(tmp_path, cold_clock, generation, removed):
    from agent_py_agent.agent.memory_store.jsonl import JsonlMemory

    home, candidates, _service = runtime(tmp_path)
    target = home.owner_home_dir / "evidence/formal"
    memory = JsonlMemory(home.owner_memory_long_term_jsonl)
    record = memory.add("user", "formal fact", attributes={"source_refs": [str(target)]})
    if generation:
        memory.migrate_tiers()
    if removed:
        memory.remove(record.entry_id)
    pins, _context = finish_projection(home, candidates, entries=11, size=4096)
    expected, complete = scan.current_retention_pins(home, (), [], now=NOW)
    assert complete and pins.protects(target, []) is expected.protects(target, []) is not removed


def test_apply_persists_cold_continuation_and_reaches_original_delete_guard(tmp_path, monkeypatch, cold_clock):
    home, _candidates, service = runtime(tmp_path)
    target, control = (_expired_pin_target(home, name) for name in ("pinned.jsonl", "control.jsonl"))
    old_file(home.owner_memory_curator_runs_dir / "cold.jsonl", json.dumps({"source_refs": [str(target)]}) + "\n")
    monkeypatch.setattr(RetentionScanBudget, "default",
        classmethod(lambda cls, config=None: cls(11, 512, float("inf"))))
    pending = 0
    for _ in range(200):
        report = service.apply(now=NOW)
        assert not report.errors and target.exists()
        if report.scan_cursor.get(scan._PIN_CURSOR_KEY, {}).get("phase") in {"build", "verify"}:
            assert not report.actions and control.exists()
            pending += 1
        assert json.loads((home.owner_data_dir / "retention_scan_cursor.json").read_text()) == report.scan_cursor
        if not report.scan_partial:
            break
    else:
        pytest.fail(f"apply stalled: {report.scan_cursor}")
    assert pending > 1 and not control.exists()


def test_unchanged_metadata_still_requires_parsed_byte_generation(tmp_path, monkeypatch, cold_clock):
    home, candidates, _service = runtime(tmp_path)
    source = old_file(home.owner_memory_curator_runs_dir / "cold.jsonl", '{"source_refs":["evidence/old"]}\n')
    _freeze_pin_file_stat(monkeypatch, source)
    cursor = {}
    for _ in range(200):
        context, result = projection_step(home, candidates, cursor, entries=1)
        cursor = context.scan_cursor
        if cursor[scan._PIN_CURSOR_KEY]["phase"] == "verify":
            break
    else:
        pytest.fail("never reached post verification")
    assert result is None
    token = cursor[scan._PIN_CURSOR_KEY]["token"]
    source.write_text('{"source_refs":["evidence/new"]}\n')
    pins, context = finish_projection(home, candidates, cursor, entries=7)
    assert context.scan_cursor[scan._PIN_CURSOR_KEY]["token"] != token
    assert pins.protects(home.owner_home_dir / "evidence/new", [])
    assert not pins.protects(home.owner_home_dir / "evidence/old", [])


def test_hot_post_group_cannot_publish_previous_pass_after_cold_continuation(tmp_path, monkeypatch):
    from agent_py_agent.agent.memory_store.jsonl import JsonlMemory

    home, candidates, _service = runtime(tmp_path)
    memory = JsonlMemory(home.owner_memory_long_term_jsonl)
    memory.add("user", "formal fact", attributes={"source_refs": ["evidence/old"]})
    original = memory.path.read_text()
    _freeze_pin_file_stat(monkeypatch, memory.path)
    old_file(home.owner_memory_curator_runs_dir / "large.jsonl", json.dumps({"note": "x" * 32768}) + "\n")
    clock = [time.time_ns()]
    monkeypatch.setattr(scan.reference_pins.time, "time_ns", lambda: clock[0])
    cursor, witnessed = {}, False
    for _ in range(100):
        context, result = projection_step(home, candidates, cursor, size=4096)
        assert result is None and context.scan_partial
        cursor = context.scan_cursor
        snapshot = scan._PIN_BUILDS[Path(home.owner_home_dir)]
        if memory.path in snapshot.post_scopes:
            witnessed = True
            break
    assert witnessed and snapshot.pending is not None and snapshot.value is None
    memory.path.write_text(original.replace("evidence/old", "evidence/new"))
    clock[0] += 3_000_000_000
    pins, _context = finish_projection(home, candidates, cursor)
    assert pins.protects(home.owner_home_dir / "evidence/new", [])
    assert not pins.protects(home.owner_home_dir / "evidence/old", [])


@pytest.mark.parametrize("damage", [None, "corrupt", "link"])
def test_cold_candidate_month_uses_budgeted_hydration_and_original_validator(tmp_path, cold_clock, damage):
    from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope

    home, candidates, _service = runtime(tmp_path)
    target = home.owner_home_dir / "evidence/cold"
    row = candidates.observe(CandidateObservation(candidate_type="lesson", content="归档证据",
        subject_key="r6.cold", scope=MemoryScope("personal", "personal"), origin="model_inferred",
        promotion_target="lesson", evidence_refs=[{"path": str(target)}]))
    candidates.transition(row.candidate_id, "approved", reviewer="test")
    row = candidates.mark_promoted(row.candidate_id, reviewer="test", promotion_ref="memory/lessons/r6")
    candidates.path.write_text(json.dumps(replace(row, updated_at="2020-01-01T00:00:00+00:00").to_record()) + "\n")
    assert candidates.compact_terminal_candidates(now=NOW) == 1
    archive = next((candidates.path.parent / "candidates_archive").glob("*.jsonl"))
    if damage == "corrupt":
        archive.write_text('{"record": "bad"}\n')
    elif damage == "link":
        saved = archive.with_suffix(".saved")
        archive.rename(saved)
        archive.symlink_to(saved)
    cursor = {}
    for _ in range(300):
        policy, _ = scan._load_policy(home.owner_retention_json)
        context = scan._RetentionScanContext(home, candidates, policy, (), NOW,
            scan_budget=RetentionScanBudget(11, 512, float("inf")), scan_cursor=cursor)
        result, _continuation = scan._budgeted_pin_projection(context)
        assert context.scan_budget.size <= 512 and context.scan_budget.entries <= 11
        if result is not None:
            break
        assert context.scan_partial
        cursor = context.scan_cursor
    else:
        pytest.fail("cold candidate stalled")
    pins, complete = result
    assert complete is (damage is None)
    if complete:
        assert not context.errors and pins.protects(target, []) and pins.protects(archive, [])
    else:
        assert context.errors and not pins.complete


@pytest.mark.parametrize("damage", ["version", "action", "expiry", "missing-id"])
def test_legacy_invalid_events_cannot_be_normalized_before_validation(tmp_path, cold_clock, damage):
    from agent_py_agent.agent.memory_store.jsonl import JsonlMemory

    home, candidates, _service = runtime(tmp_path)
    memory = JsonlMemory(home.owner_memory_long_term_jsonl)
    memory.add("user", "formal fact", attributes={"source_refs": ["evidence/formal"]})
    row = json.loads(memory.path.read_text().splitlines()[0])
    if damage == "version":
        row["version"] = 0
    elif damage == "action":
        row["action"] = "invalid"
    elif damage == "expiry":
        row["expires_at"] = -1
    elif damage == "missing-id":
        row.update(entry_id="", action="replace")
    memory.path.write_text(json.dumps(row) + "\n")
    cursor = {}
    for _ in range(100):
        policy, _ = scan._load_policy(home.owner_retention_json)
        context = scan._RetentionScanContext(home, candidates, policy, (), NOW,
            scan_budget=RetentionScanBudget(11, 512, float("inf")), scan_cursor=cursor)
        result, _continuation = scan._budgeted_pin_projection(context)
        if result is not None:
            break
        cursor = context.scan_cursor
    else:
        pytest.fail("invalid event never surfaced")
    assert not result[1] and not result[0].complete and context.errors
