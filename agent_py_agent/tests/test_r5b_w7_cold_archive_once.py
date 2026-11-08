"""R5B-W7：保留引用投影内每个冷归档月只读一次，引用保护与损坏失败语义不变。"""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agent_py_agent.agent.memory_store import candidates as candidates_module
from agent_py_agent.agent.memory_store import retention_scan as scan
from agent_py_agent.agent.memory_store.candidate_models import CandidateObservation, MemoryScope
from agent_py_agent.tests.test_memory_retention_wave5 import NOW, old_file, runtime


def _cold_candidates(tmp_path, count):
    home, candidates, _service = runtime(tmp_path)
    evidence = []
    rows = []
    for index in range(count):
        path = old_file(home.owner_memory_daily_dir / f"cold-evidence-{index}.jsonl")
        evidence.append(path)
        row = candidates.observe(CandidateObservation(candidate_type="lesson", content=f"已晋升的归档事实 {index}",
            subject_key=f"r5b.cold.{index}", scope=MemoryScope("personal", "personal"), origin="model_inferred",
            promotion_target="lesson", evidence_refs=[{"path": str(path)}]))
        candidates.transition(row.candidate_id, "approved", reviewer="test")
        rows.append(candidates.mark_promoted(row.candidate_id, reviewer="test", promotion_ref=f"memory/lessons/r5b-{index}"))
    candidates.path.write_text("".join(
        json.dumps(replace(row, updated_at="2020-01-01T00:00:00+00:00").to_record()) + "\n" for row in rows))
    assert candidates.compact_terminal_candidates(now=NOW) == count
    hot = candidates.readonly_rows()
    assert len(hot) == count and all(row.cold_detail_ref for row in hot)
    assert len(list((candidates.path.parent / "candidates_archive").glob("*.jsonl"))) == 1
    return home, candidates, evidence


def _count_cold_reads(monkeypatch):
    calls = []
    original = candidates_module._read_cold_records

    def counted(path, name, **kwargs):
        calls.append(name)
        return original(path, name, **kwargs)

    monkeypatch.setattr(candidates_module, "_read_cold_records", counted)
    return calls


def test_retention_pins_read_each_cold_month_once(tmp_path, monkeypatch):
    home, _candidates, evidence = _cold_candidates(tmp_path, 6)
    calls = _count_cold_reads(monkeypatch)
    errors = []
    pins, complete = scan.current_retention_pins(home, (), errors, now=NOW)
    assert complete and not errors
    assert all(pins.protects(path, errors) for path in evidence)
    assert len(calls) == len(set(calls)) == 1


@pytest.mark.parametrize("damage", ["corrupt", "missing_version"])
def test_shared_cold_month_damage_still_fails_closed(tmp_path, damage):
    home, candidates, _evidence = _cold_candidates(tmp_path, 3)
    archive = next((candidates.path.parent / "candidates_archive").glob("*.jsonl"))
    if damage == "corrupt":
        archive.write_text("{corrupt\n")
    else:
        lines = archive.read_text().splitlines(keepends=True)
        archive.write_text("".join(lines[:-1]))
    errors = []
    _pins, complete = scan.current_retention_pins(home, (), errors, now=NOW)
    assert not complete and errors
    assert errors[0].error_code == "MEMORY_RETENTION_PIN_SOURCE_UNREADABLE"
