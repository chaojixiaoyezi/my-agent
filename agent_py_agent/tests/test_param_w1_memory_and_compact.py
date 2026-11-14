"""PARAM-W1：消费者读取配置、兼容显式覆盖及有界读取的运行时回归。"""
from __future__ import annotations

import json
import os
import runpy
from types import SimpleNamespace

import pytest

from agent_py_agent.agent import memory_push
from agent_py_agent.agent.agent_core import planner_service
from agent_py_agent.agent.agent_core.runner import gate
from agent_py_agent.agent.agent_core.runtime import context_compactor, loop_support
from agent_py_agent.agent.conversation import compact_landmarks
from agent_py_agent.agent.memory_store import jsonl, reference_pins, retention_scan_budget
from agent_py_agent.agent.memory_store.generation_manifest import ContextBudget, MemoryPolicyV2
from agent_py_agent.agent.memory_store.recall import MemoryRecallScope
from agent_py_agent.agent.prompting_parts.memory_context import memory_context_text
from agent_py_agent.agent.settings import defaults


def _record(entry_id, content="正式记忆。", kind="fact"):
    return jsonl.MemoryRecord(role="user", entry_id=entry_id, content=content, kind=kind,
        attributes={"origin": "user_explicit", "scope_type": "personal", "scope_key": "personal",
                    "subject_key": entry_id})


@pytest.fixture
def decision_recall(monkeypatch):
    calls = []
    records = [_record(f"hot-{index}", kind="hot") for index in range(10)]

    def route(agent, query, limit):
        calls.append(limit)
        return SimpleNamespace(receipts=[], findings=[])

    monkeypatch.setattr(memory_push, "_route_formal_lessons", route)
    monkeypatch.setattr(memory_push, "hot_memory_records", lambda *_args, **_kwargs: records)
    monkeypatch.setattr(memory_push, "routed_lesson_records", lambda *_args, **_kwargs: [])
    return calls


@pytest.mark.parametrize("entry", ["direct", "report", "timeout", "failure", "planning", "planner", "runner"])
@pytest.mark.parametrize("configured,expected", [("no-config", 3), ("no-field", 5), (7, 7), (0, 0)])
def test_all_automatic_decision_points_use_memory_top_k(decision_recall, entry, configured, expected):
    agent = SimpleNamespace(memory_hot=object(), memory_lessons=object())
    if configured != "no-config":
        agent.config = SimpleNamespace() if configured == "no-field" else SimpleNamespace(memory_top_k=configured)
    if entry == "direct":
        result = memory_push.push_relevant_memories(agent, "planning", {})
    elif entry == "report":
        result, errors = memory_push.push_relevant_memories_report(agent, "planning", {})
        assert errors == []
    elif entry == "timeout":
        result = memory_push.push_timeout_memories(agent, "task", "目标")
    elif entry == "failure":
        result = memory_push.push_failure_memories(agent, "task", "目标", "timeout")
    elif entry == "planning":
        result = memory_push.push_planning_memories(agent, "task", "目标")
    elif entry == "planner":
        result = planner_service.append_planner_memory_hint(agent, {}, "base")
    else:
        params = gate.RunnerFailureParams(agent, "task", SimpleNamespace(goal="目标"), None, "base")
        result = gate._inject_failure_memories(params, "TIMEOUT")
    assert decision_recall == ([expected] if expected else [])
    if entry in {"planner", "runner"}:
        assert result.count('"entry_id"') == expected
    else:
        assert len(result) == expected


@pytest.mark.parametrize("entry", [memory_push.push_relevant_memories, memory_push.push_relevant_memories_report])
@pytest.mark.parametrize("limit,expected", [(3, 3), (1, 1), (0, 0), (-1, 0), (None, 0)])
def test_explicit_recall_limit_wins_over_config(decision_recall, entry, limit, expected):
    agent = SimpleNamespace(config=SimpleNamespace(memory_top_k=8), memory_hot=object(), memory_lessons=object())
    result = entry(agent, "planning", {}, limit=limit)
    if isinstance(result, tuple):
        result, errors = result
        assert errors == []
    assert len(result) == expected
    assert decision_recall == ([expected] if expected else [])


def test_owner_memory_off_still_short_circuits(decision_recall):
    agent = SimpleNamespace(config=SimpleNamespace(memory_top_k=8),
                            owner_policy=SimpleNamespace(memory_enabled=False))
    assert memory_push.push_relevant_memories(agent, "planning", {}) == []
    assert decision_recall == []


def test_recall_default_signature_remains_three():
    import inspect

    for entry in (memory_push.push_relevant_memories, memory_push.push_relevant_memories_report):
        value = inspect.signature(entry).parameters["limit"].default
        assert isinstance(value, int) and value == 3 and repr(value) == "3"


@pytest.mark.parametrize("recall_chars,expected", [(0, []), (12000, ["fact-0", "fact-1", "fact-2"])])
def test_runtime_final_selection_uses_canonical_policy(tmp_path, recall_chars, expected):
    owner = tmp_path / "owner"
    memory_path = owner / "memory" / "long_term" / "memory.jsonl"
    memory_path.parent.mkdir(parents=True)
    policy = {"schema_version": "memory-policy.v2", "budget": {"recall_max_chars": recall_chars}}
    (owner / "memory_policy.json").write_text(json.dumps(policy), encoding="utf-8")
    memory = jsonl.JsonlMemory(memory_path)
    records = [_record(f"fact-{index}", "事实" * 1400) for index in range(3)]
    for record in records:
        memory.add_record(record)
    agent = SimpleNamespace(memory=memory, memory_hot=SimpleNamespace(list=lambda: []),
        memory_lessons=SimpleNamespace(list=lambda: []), config=SimpleNamespace(memory_top_k=5))
    routed = SimpleNamespace(receipts=[], findings=[], injected_sections=[])
    result = loop_support._formal_memories_for_request(agent, SimpleNamespace(), routed,
        recall_scope=MemoryRecallScope.from_runtime(), long_term_memories=records, skip_formal_recall=False)
    assert [record.entry_id for record in result] == expected
    assert set(memory._pending_access) == set(expected)
    assert not result or len(memory_context_text(result)) <= recall_chars


def test_invalid_policy_does_not_silently_restore_default_recall():
    def invalid_policy():
        return MemoryPolicyV2.from_payload({"budget": {"recall_max_chars": -1}})

    agent = SimpleNamespace(memory=SimpleNamespace(memory_policy=invalid_policy))
    with pytest.raises(ValueError, match="MEMORY_POLICY_INVALID"):
        loop_support._memory_context_budget(agent)


def test_context_budget_changes_hot_and_working_agreement_pools():
    records = [_record("hot-a", "热" * 300, "hot"), _record("hot-b", "热" * 300, "hot"),
               _record("agreement-a", "约" * 2000, "working_agreement"),
               _record("agreement-b", "约" * 2000, "working_agreement"), _record("fact")]
    budget = ContextBudget(12000, 300)
    selected = loop_support._budgeted_formal_memories(records, budget=budget)
    assert [row.entry_id for row in selected] == ["hot-a", "agreement-a", "agreement-b", "fact"]
    assert selected.omitted_count == 1
    agreements = [row for row in selected if row.kind == "working_agreement"]
    assert len(memory_context_text(agreements)) <= budget.working_agreement_chars
    assert loop_support._budgeted_formal_memories(records, budget=ContextBudget(0, 0)) == []
    canonical = ContextBudget.from_policy(MemoryPolicyV2())
    assert loop_support._budgeted_formal_memories(records) == loop_support._budgeted_formal_memories(
        records, budget=canonical)


def test_refresh_keeps_the_same_context_budget():
    records = [_record(f"fact-{index}", "事实" * 1400) for index in range(3)]
    by_id = {row.entry_id: row for row in records}
    agent = SimpleNamespace(
        memory=SimpleNamespace(
            all=lambda: records,
            read_ids=lambda ids: [by_id[eid] for eid in ids if eid in by_id],
        ),
        memory_hot=SimpleNamespace(list=lambda: []),
        memory_lessons=SimpleNamespace(list=lambda: []),
        config=SimpleNamespace(),
    )
    selected = loop_support._refresh_recall_candidates(agent, records, [], MemoryRecallScope.from_runtime(),
                                                       budget=ContextBudget(12000, 0))
    assert [row.entry_id for row in selected] == [row.entry_id for row in records]


def test_compact_fallbacks_follow_the_canonical_defaults(monkeypatch):
    original = defaults.default_config_int
    changed = {"memory_compact_auto_trigger_max_tokens": 123456,
               "memory_compact_recovery_target_percent": 57, "compact_landmark_max_tokens": 9876}
    monkeypatch.setattr(defaults, "default_config_int", lambda key, **kwargs: changed.get(key, original(key, **kwargs)))
    compactor = runpy.run_path(context_compactor.__file__, run_name=context_compactor.__name__)
    landmarks = runpy.run_path(compact_landmarks.__file__, run_name=compact_landmarks.__name__)
    assert compactor["DEFAULT_COMPACT_TRIGGER_MAX_TOKENS"] == 123456
    assert compactor["DEFAULT_COMPACT_RECOVERY_TARGET_PERCENT"] == 57
    assert compactor["compact_trigger_max_tokens"](None) == 123456
    assert compactor["compact_trigger_max_tokens"](0) == 0
    assert compactor["compact_recovery_target_percent"](None) == 57
    assert compactor["DEFAULT_COMPACT_TRIGGER_PERCENT"] == 90
    assert landmarks["_DEFAULT_MAX_TOKENS"] == 9876
    assert landmarks["landmark_options"](SimpleNamespace(config=SimpleNamespace(
        model_context_window_tokens=1000000))).landmark_budget_tokens == 9876
    assert landmarks["landmark_options"](SimpleNamespace(config=SimpleNamespace(
        compact_landmark_max_tokens=0))).landmark_budget_tokens == 0


def test_legacy_library_cap_is_only_a_canonical_default_alias(monkeypatch):
    from agent_py_agent.agent.memory_store import generation_manifest

    monkeypatch.setattr(generation_manifest, "MemoryPolicyV2", lambda: SimpleNamespace(active_library_max_chars=43210))
    module = runpy.run_path(jsonl.__file__, run_name=jsonl.__name__)
    assert module["MEMORY_LIBRARY_WRITE_CAP_CHARS"] == 43210


def test_retention_slice_reads_each_configured_dimension(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(retention_scan_budget.time, "monotonic", lambda: clock[0])
    config = SimpleNamespace(retention_scan_slice_entries=2, retention_scan_slice_bytes=9,
                             retention_scan_slice_ms=12.5)
    budget = retention_scan_budget.RetentionScanBudget.default(config)
    assert (budget.max_entries, budget.max_bytes, budget.deadline) == (2, 9, 10.0125)
    budget.charge(entries=1, size=8)
    assert not budget.exhausted()
    budget.charge(size=1)
    assert budget.exhausted()
    budget = retention_scan_budget.RetentionScanBudget.default(config)
    budget.charge(entries=2)
    assert budget.exhausted()
    budget = retention_scan_budget.RetentionScanBudget.default(config)
    clock[0] = 10.0125
    assert budget.exhausted()
    fallback = retention_scan_budget.RetentionScanBudget.default(SimpleNamespace())
    assert (fallback.max_entries, fallback.max_bytes) == (128, 8 * 1024 * 1024)
    assert fallback.deadline == clock[0] + 0.025


@pytest.mark.parametrize("key,value", [("retention_scan_slice_entries", 0), ("retention_scan_slice_entries", True),
    ("retention_scan_slice_bytes", -1), ("retention_scan_slice_bytes", "8"),
    ("retention_scan_slice_ms", 0), ("retention_scan_slice_ms", float("inf")),
    ("retention_scan_slice_ms", float("nan")), ("retention_scan_slice_ms", False)])
def test_invalid_slice_config_cannot_disable_bounded_scanning(key, value):
    with pytest.raises(ValueError, match="invalid_retention_scan_budget"):
        retention_scan_budget.RetentionScanBudget.default(SimpleNamespace(**{key: value}))


@pytest.mark.parametrize("chunk_bytes,body_bytes", [(17, 900), (4096, 300000), (None, 900)])
def test_reference_pin_snapshot_uses_scoped_configured_read_chunks(tmp_path, monkeypatch, chunk_bytes, body_bytes):
    home = SimpleNamespace(owner_home_dir=tmp_path, owner_retention_json=tmp_path / "retention.json")
    source = tmp_path / "source.jsonl"
    source.write_bytes(b"x" * body_bytes)
    reads = []
    original_read = os.read
    inode = source.stat().st_ino

    def read(descriptor, count):
        if os.fstat(descriptor).st_ino == inode:
            reads.append(count)
        return original_read(descriptor, count)

    def builder(_home, _roots, _errors, **_kwargs):
        reference_pins.watch_pin_source(source)
        binding = reference_pins.ParsedPinBytes(source)
        binding.update(source.read_bytes())
        assert binding.matches()
        reference_pins.recheck_pin_sources([source])
        return set(), True

    monkeypatch.setattr(reference_pins.os, "read", read)
    config = SimpleNamespace(memory_reference_pin_chunk_bytes=chunk_bytes) if chunk_bytes else None
    snapshot = reference_pins.ReferencePinSnapshot(home, (), None, builder, config=config)
    assert snapshot.current([]) == (set(), True)
    assert reads and max(reads) <= (chunk_bytes or 65536)
    assert reference_pins._SOURCE_CONTENTS.get() is None
    assert reference_pins._PIN_SOURCE_CHUNK_BYTES == 65536
    assert reference_pins._MAX_PIN_SOURCE_DIGEST_BYTES == 262144
    reads.clear()
    independent = reference_pins.ReferencePinSnapshot(home, (), None, builder)
    assert independent.current([]) == (set(), True)
    assert reads and max(reads) > 17


@pytest.mark.parametrize("value", [0, -1, True, "64", 1.5])
def test_reference_pin_invalid_chunk_size_is_rejected_without_io(value, tmp_path):
    with pytest.raises(ValueError, match="invalid_memory_reference_pin_chunk_bytes"):
        reference_pins.ReferencePinSnapshot(object(), (), None, None,
            config=SimpleNamespace(memory_reference_pin_chunk_bytes=value))
    assert list(tmp_path.iterdir()) == []


def test_reference_pin_chunk_change_preserves_source_mutation_rejection(tmp_path):
    home = SimpleNamespace(owner_home_dir=tmp_path, owner_retention_json=tmp_path / "retention.json")
    source = tmp_path / "source.jsonl"
    source.write_bytes(b"a" * 800)

    def builder(_home, _roots, _errors, **_kwargs):
        reference_pins.watch_pin_source(source)
        source.write_bytes(b"b" * 800)
        return set(), True

    snapshot = reference_pins.ReferencePinSnapshot(home, (), None, builder,
        config=SimpleNamespace(memory_reference_pin_chunk_bytes=31))
    with pytest.raises(OSError, match="MEMORY_RETENTION_PIN_SOURCE_UNREADABLE"):
        snapshot.current([])
    assert snapshot.value is None
    assert reference_pins._SOURCE_CONTENTS.get() is None


@pytest.mark.parametrize("initial_hot_chars,omitted", [(5000, 0), (100, 2)])
def test_hot_pool_does_not_consume_supplemental_recall_room(tmp_path, monkeypatch, initial_hot_chars, omitted):
    from agent_py_agent.agent.conversation import decision_service
    from agent_py_agent.agent.memory_store import decision_recall
    from agent_py_agent.agent.prompting_parts.memory_context import BudgetedMemoryRecords

    owner = tmp_path / "owner"
    memory = jsonl.JsonlMemory(owner / "memory" / "long_term" / "memory.jsonl")
    (owner / "memory_policy.json").write_text(json.dumps({
        "budget": {"recall_max_chars": 4000, "hot_injection_max_chars": 8000},
    }), encoding="utf-8")
    fact = memory.add_record(_record("supplemental-fact", "补充召回的新事实。"))
    agent = SimpleNamespace(memory=memory, memory_hot=object(), memory_lessons=object(),
                            config=SimpleNamespace(memory_top_k=5))
    initial_hot = _record("hot-pool", "a" * initial_hot_chars, "hot")
    refreshed_hot = _record("hot-pool", "a" * 5000, "hot")
    monkeypatch.setattr(loop_support, "hot_memory_records", lambda *_a, **_kw: [initial_hot])
    monkeypatch.setattr(loop_support, "routed_lesson_records", lambda *_a, **_kw: [])
    monkeypatch.setattr(decision_recall, "_operation_id", lambda _request: "operation")
    monkeypatch.setattr(decision_service, "begin_decision_stage", lambda *_a, **_kw: object())
    monkeypatch.setattr(decision_recall, "rerank_recalled_memories", lambda *_a, **_kw: (
        BudgetedMemoryRecords([refreshed_hot], omitted_count=omitted), ""))
    rooms = []

    def supplement(_agent, _request, records, *, remaining_chars, **kwargs):
        rooms.append(remaining_chars)
        assert kwargs["defer_access"] is True
        return BudgetedMemoryRecords([*records, fact], omitted_count=omitted), ""

    monkeypatch.setattr(decision_recall, "supplement_recalled_memories", supplement)
    routed = SimpleNamespace(receipts=[], findings=[], injected_sections=[], supplement_entry_ids=[])
    result = loop_support._formal_memories_for_request(
        agent, SimpleNamespace(user_prompt="查找补充召回的事实信息；核对非HOT池的剩余预算。"), routed,
        recall_scope=MemoryRecallScope.from_runtime(), long_term_memories=[], skip_formal_recall=False)
    regular_envelope = memory_context_text(BudgetedMemoryRecords([], omitted_count=omitted))
    assert rooms == [4000 - len(regular_envelope)]
    assert [record.entry_id for record in result] == ["hot-pool", fact.entry_id]
    assert set(memory._pending_access) == {fact.entry_id}
    assert routed.supplement_entry_ids == [fact.entry_id]
