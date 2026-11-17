"""S7: model-call records carry stages_ms (assembly, request build, record start, GC pause); the GC meter measures pauses."""
from __future__ import annotations

import gc
import time

from agent_py_agent.agent.agent_core import tool_model_generation as generation
from agent_py_agent.agent.agent_core.model import call_runtime
from agent_py_agent.agent.observability import gc_pause_meter
from agent_py_agent.tests.test_gap_w10_context_usage_identity import (  # noqa: F401
    _call_case,
    isolated_usage_home,
)


def test_gc_meter_measures_a_collection_inside_a_window():
    gc_pause_meter.install()
    gc_pause_meter.install()
    assert gc.callbacks.count(gc_pause_meter._on_gc) == 1
    before = gc_pause_meter.snapshot()["count"]
    started = time.monotonic()
    gc.collect()
    finished = time.monotonic()
    after = gc_pause_meter.snapshot()
    assert after["count"] == before + 1 and sum(after["buckets"].values()) >= 1
    assert 0.0 < gc_pause_meter.pause_ms_between(started, finished) <= (finished - started) * 1000.0
    assert gc_pause_meter.pause_ms_between(finished + 10.0, finished + 20.0) == 0.0
    assert gc_pause_meter.pause_ms_between(finished, started) is None


def test_gc_meter_never_consumes_a_patched_clock(monkeypatch):
    gc_pause_meter.install()
    observed = iter((100.0, 101.0))
    monkeypatch.setattr(time, "monotonic", lambda: next(observed))
    gc.collect()
    assert next(observed) == 100.0


def test_record_start_writes_stage_breakdown(tmp_path, monkeypatch):
    agent, _main, _child, _stream, params = _call_case(tmp_path)
    prepared = time.monotonic()
    real_snapshot = call_runtime.model_visible_context_snapshot

    def snapshot_with_collection(*args, **kwargs):
        gc.collect()
        return real_snapshot(*args, **kwargs)

    monkeypatch.setattr(call_runtime, "model_visible_context_snapshot", snapshot_with_collection)
    request = generation.ModelGenerateParams(
        agent, params, "主输入", 0, context_assembly_started_at=prepared, request_prepare_started_at=prepared,
        pre_call_housekeeping_ms=1.5, supervision_sweep_ms=0.25, assembly_ms=12.0,
        generate_started_at=time.monotonic())
    ledger, call_id, _estimate = call_runtime.start_model_call_record(request)
    stages = next(record for record in ledger.records() if record.call_id == call_id).metadata["stages_ms"]
    assert {"assembly_ms": 12.0, "supervision_sweep_ms": 0.25, "pre_call_housekeeping_ms": 1.5}.items() <= stages.items()
    assert stages["request_build_ms"] >= 0.0 and stages["start_record_ms"] > 0.0
    assert stages["gc_pause_ms"] > 0.0


def test_unknown_stages_are_left_out_not_zeroed(tmp_path):
    agent, _main, _child, _stream, params = _call_case(tmp_path)
    ledger, call_id, _estimate = call_runtime.start_model_call_record(
        generation.ModelGenerateParams(agent, params, "主输入", 0))
    stages = next(record for record in ledger.records() if record.call_id == call_id).metadata["stages_ms"]
    assert set(stages) == {"start_record_ms"}
