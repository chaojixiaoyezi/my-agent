"""Three-run self-stability, committed baseline equality and independent invariant checks."""
from __future__ import annotations

import difflib
import json
import os
import time
from pathlib import Path

import pytest

from .normalize import assert_invariants, normalize
from .replayer import replay

HERE = Path(__file__).parent
SCENARIOS = [json.loads(path.read_text()) for path in sorted((HERE / "scenarios").glob("*.json"))]


# LLM: Capabilities use the existing root marker; no local try/skip or expected-failure path masks a golden failure.
# 函数用途: Build one collected test per declared scenario with its exact capability requirements.
def scenario_parameters():
    rows = []
    for scenario in SCENARIOS:
        marks = [pytest.mark.sandbox_capability(*scenario["required_capabilities"])] if scenario["required_capabilities"] else []
        rows.append(pytest.param(scenario, id=scenario["id"], marks=marks))
    return rows


# LLM: 更新只写稳定回放；旧基线也先核原哈希再应用同一幂等归一规则，不重录未解释的产品差异。
# 函数用途: Replay the same Gateway scenario three times and compare it with the committed oracle.
@pytest.mark.parametrize("scenario", scenario_parameters())
def test_golden_replay(tmp_path, golden_parameters, scenario, record_property):
    started = time.monotonic()
    traces = []
    for index in range(scenario["repetitions"]):
        trace = replay(tmp_path / f"run-{index}", scenario, golden_parameters)
        assert_invariants(trace, scenario["expected_effects"], scenario["forbidden_effects"])
        traces.append(trace)
    assert len(traces) == 3, "the golden contract requires exactly three consecutive runs"
    for trace in traces[1:]:
        assert trace == traces[0], _difference(traces[0], trace)
    expected = HERE / "expected" / f"{scenario['id']}.json"
    if os.environ.get("MY_AGENT_GOLDEN_UPDATE") == "1":
        expected.parent.mkdir(parents=True, exist_ok=True)
        expected.write_text(json.dumps(traces[0], ensure_ascii=False, indent=2) + "\n")
    assert expected.is_file(), f"missing committed baseline: {expected.name}; explicit MY_AGENT_GOLDEN_UPDATE=1 is required"
    baseline = normalize(json.loads(expected.read_text()))
    assert_invariants(baseline, scenario["expected_effects"], scenario["forbidden_effects"])
    assert traces[0] == baseline, _difference(baseline, traces[0])
    elapsed = time.monotonic() - started
    record_property("golden_runtime_seconds", elapsed)
    assert elapsed < scenario["deadline_seconds"], f"scenario exceeded its CI deadline: {elapsed}"


# LLM: Diffs compare actual structured traces; no values are stripped to make a mismatch pass.
# 函数用途: Render an inspectable unified diff for a baseline or stability failure.
def _difference(expected, actual):
    before = json.dumps(expected, ensure_ascii=False, indent=2, sort_keys=True).splitlines()
    after = json.dumps(actual, ensure_ascii=False, indent=2, sort_keys=True).splitlines()
    return "\n".join(difflib.unified_diff(before, after, fromfile="expected", tofile="actual"))
