"""摄取引擎单元:字段画像分类、滑窗计数、稀有度分诊、账目守恒、快照恢复。"""

from __future__ import annotations

from agent.ingestion.config import IngestTuning, tuning_from_params
from agent.ingestion.engine import StreamDigestEngine
from agent.ingestion.field_profile import FieldProfile, ProfileTable
from agent.ingestion.flatten import flatten_event
from agent.ingestion.signature import classed_pairs, signature_of
from agent.ingestion.window_counter import SlidingWindowCounter


def _tuning(**overrides) -> IngestTuning:
    base = {"window_seconds": 300, "bucket_seconds": 30, "rare_threshold": 3, "max_candidates_per_pull": 8}
    base.update(overrides)
    return IngestTuning(**base)


def test_flatten_nested_and_arrays():
    pairs = dict(flatten_event({"a": {"b": True}, "items": [{"x": 1}, {"x": 2}], "s": "hi"}))
    assert pairs["a.b"] is True
    assert pairs["items.__len__"] == 2
    assert pairs["s"] == "hi"


def test_profile_bool_and_low_cardinality_literal():
    table = ProfileTable(8)
    assert table.observe_and_token("flag", True) == "b:T"
    assert table.observe_and_token("kind", "charge") == "s:charge"


def test_profile_high_cardinality_string_collapses():
    table = ProfileTable(4)
    for index in range(6):
        token = table.observe_and_token("trace", f"id-{index}")
    assert token == "s:*"


def test_profile_monotone_int_collapses_to_mono():
    profile = FieldProfile()
    for value in range(40):
        profile.observe(value, 8)
    assert profile.token(41) == "n:mono"


def test_profile_nonmonotone_int_becomes_magnitude_bucket():
    profile = FieldProfile()
    values = [5, 3, 9, 480, 12, 77, 260, 8, 33, 410]
    for value in values * 4:
        profile.observe(value, 4)
    assert profile.token(7) == "n:e0"
    assert profile.token(450) == "n:e2"


def test_window_counter_evicts_old_buckets():
    counter = SlidingWindowCounter(window_seconds=60, bucket_seconds=10)
    assert counter.observe("sig", now=1000.0) == 1
    assert counter.observe("sig", now=1005.0) == 2
    assert counter.window_count("sig", now=1100.0) == 0


def test_engine_rare_signature_escalates_and_common_suppresses():
    engine = StreamDigestEngine(_tuning())
    warmup = [(i, {"kind": "beat", "flag": False, "seq": i}) for i in range(200)]
    engine.process(warmup, now=2000.0)
    steady = [(200 + i, {"kind": "beat", "flag": False, "seq": 200 + i}) for i in range(200)]
    steady.insert(100, (999, {"kind": "beat", "flag": True, "seq": 300}))
    digest = engine.process(steady, now=2003.0)
    flagged = [c for c in digest.candidates if c.event.get("flag") is True]
    assert len(flagged) == 1
    assert digest.suppressed_total > 150
    assert digest.groups and digest.groups[0].call_count > 100


def test_engine_accounting_balances():
    engine = StreamDigestEngine(_tuning(max_candidates_per_pull=2, rare_threshold=1))
    events = [(i, {"v": f"unique-{i}", "n": i}) for i in range(30)]
    digest = engine.process(events, now=3000.0)
    assert digest.seen == 30
    assert len(digest.candidates) + len(digest.overflow) + digest.suppressed_total == 30


def test_engine_snapshot_restore_prewarms_first_seen():
    engine = StreamDigestEngine(_tuning())
    events = [(i, {"kind": "beat", "seq": i}) for i in range(120)]
    engine.process(events, now=4000.0)
    snap = engine.snapshot(now=4000.0)

    fresh = StreamDigestEngine(_tuning())
    fresh.restore(snap, now=5000.0)
    digest = fresh.process([(200, {"kind": "beat", "seq": 200})], now=5000.0)
    assert all(not candidate.first_seen for candidate in digest.candidates)
    assert fresh.totals["events_seen"] == 121


def test_tuning_from_params_clamps_and_ignores_bad_values():
    tuning = tuning_from_params({"rare_threshold": "999", "page_limit": "abc", "window_seconds": 5})
    assert tuning.rare_threshold == 100
    assert tuning.page_limit == 400
    assert tuning.window_seconds == 30


def test_signature_stable_across_key_order():
    table = ProfileTable(24)
    sig_a = signature_of(classed_pairs({"a": 1, "b": "x"}, table))
    sig_b = signature_of(classed_pairs({"b": "x", "a": 1}, table))
    assert sig_a == sig_b
