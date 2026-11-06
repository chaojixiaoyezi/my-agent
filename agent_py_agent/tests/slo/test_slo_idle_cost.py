"""Idle subprocess resource observations and executable stage-gate self-checks."""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


# LLM: Counters come from the actual subprocess; unavailable process observations remain unmeasured.
# 函数用途: Read CPU seconds and context-switch counters without adding psutil.
def process_counters(pid):
    proc = Path("/proc") / str(pid)
    if (proc / "stat").exists():
        fields = (proc / "stat").read_text().rsplit(")", 1)[1].split()
        ticks = os.sysconf("SC_CLK_TCK")
        cpu = (int(fields[11]) + int(fields[12])) / ticks
        status = dict(line.split(":", 1) for line in (proc / "status").read_text().splitlines() if ":" in line)
        switches = int(status["voluntary_ctxt_switches"]) + int(status["nonvoluntary_ctxt_switches"])
        return {"cpu_seconds": cpu, "context_switches": switches, "source": "procfs"}
    try:
        result = subprocess.run(["ps", "-o", "time=", "-p", str(pid)], capture_output=True, text=True, check=False)
    except OSError as error:
        return {"cpu_seconds": None, "context_switches": "unmeasured", "source": "ps", "error": str(error)}
    if result.returncode or not result.stdout.strip():
        return {"cpu_seconds": None, "context_switches": "unmeasured", "source": "ps", "error": "PS_UNAVAILABLE"}
    parts = result.stdout.strip().split(":")
    try:
        cpu = sum(float(value) * 60 ** index for index, value in enumerate(reversed(parts)))
    except ValueError:
        cpu = math.nan
    if not math.isfinite(cpu) or cpu < 0:
        return {"cpu_seconds": None, "context_switches": "unmeasured", "source": "ps", "error": "PS_INVALID_COUNTER"}
    return {"cpu_seconds": cpu, "context_switches": "unmeasured", "source": "ps"}


# LLM: Denied, empty or malformed process observations must never become measured zero CPU.
# 函数用途: Exercise the observation-unavailable path without requiring host process-inspection permission.
@pytest.mark.slo
@pytest.mark.parametrize("output,denied", [("", True), ("", False), ("invalid", False), ("nan", False)])
def test_process_counter_unavailable(output, denied):
    result = SimpleNamespace(returncode=0, stdout=output)
    with patch.object(Path, "exists", return_value=False), patch.object(subprocess, "run", return_value=result,
            side_effect=PermissionError("fixture process observation denied") if denied else None):
        counters = process_counters(os.getpid())
    assert counters["cpu_seconds"] is None and counters["context_switches"] == "unmeasured"
    assert counters["error"] and counters["source"] == "ps"


# LLM: Stage 1 publishes observations only; Stage 2 owns the numerical idle-cost gate.
# 函数用途: Measure an idle Gateway over the JSON-configured window and persist its report.
@pytest.mark.slo
def test_idle_cost(isolated_gateway, slo_data, slo_report):
    _base, _home, proc = isolated_gateway
    scenario = next(row for row in slo_data["scenarios"] if row["id"] == "idle_cost")
    before = process_counters(proc.pid)
    start = time.monotonic()
    threading.Event().wait(scenario["load_profile"]["window_seconds"])
    elapsed = time.monotonic() - start
    after = process_counters(proc.pid)
    cpu = None if before["cpu_seconds"] is None or after["cpu_seconds"] is None else after["cpu_seconds"] - before["cpu_seconds"]
    switches = (after["context_switches"] - before["context_switches"]
                if isinstance(before["context_switches"], int) and isinstance(after["context_switches"], int) else "unmeasured")
    report = slo_report("idle_cost", scenario["load_profile"], [], idle={"window_seconds": elapsed,
        "before": before, "after": after, "cpu_seconds": cpu, "cpu_cores": cpu / elapsed if cpu is not None else None,
        "context_switches": switches, "context_switches_per_second": switches / elapsed if isinstance(switches, int) else "unmeasured"},
        assertion_stage=scenario["targets"]["assert_stage"])
    assert Path(report["report_path"]).is_file()
    assert proc.poll() is None, "Gateway exited during the observation window"


# LLM: A standalone subprocess loads a copied real plugin, not a reimplementation of the gate.
# 函数用途: Prove baseline xfail passes while strict stage mode rejects the same failure and every skip path.
@pytest.mark.slo
def test_stage_gate_self_check(tmp_path, slo_data):
    root, here = tmp_path / "gate-selfcheck", Path(__file__).parent
    suite = root / "slo"
    suite.mkdir(parents=True)
    for name in ("__init__.py", "conftest.py", "load.py", "fake_provider.py", "report.py"):
        shutil.copyfile(here / name, suite / name)
    targets = {"schema": 1, "scenarios": [{"id": "fixture", "load_profile": {}, "targets": {}}]}
    target_path, ledger_path = suite / "slo_targets.json", suite / "expected_failures.json"
    target_path.write_text(json.dumps(targets))
    ledger = {"schema": 1, "entries": [{"nodeid": "slo/test_case.py::test_case", "reason": "fixture target failure",
        "clears_when": "S1-INT", "added": "2026-11-05"}]}
    ledger_path.write_text(json.dumps(ledger))
    test_path = suite / "test_case.py"
    test_path.write_text("import pytest\nfrom .report import SLOTargetFailure\n@pytest.mark.slo\ndef test_case():\n    raise SLOTargetFailure('fixture target failure')\n")
    env = {**os.environ, "HOME": str(root), "MY_AGENT_HOME": str(root), "PYTHONDONTWRITEBYTECODE": "1"}
    for key in ("MY_AGENT_SLO_EXPECTED_FAILURES", "MY_AGENT_SLO_TARGETS", "MY_AGENT_SLO_STAGE_GATE"):
        env.pop(key, None)
    command = [sys.executable, "-m", "pytest", str(suite), "--rootdir", str(root), "--confcutdir", str(root),
               "-q", "-o", "addopts=", "-p", "no:cacheprovider"]
    # LLM: Each invocation is bounded and preserves its real return code and combined output.
    # 函数用途: Run one isolated baseline or stage-mode subprocess.
    def run(stage):
        return subprocess.run(command, cwd=root, env={**env, "MY_AGENT_SLO_STAGE_GATE": str(int(stage))},
            capture_output=True, text=True, timeout=slo_data["fixture"]["selftest_seconds"])
    baseline = run(False)
    assert baseline.returncode == 0 and "xfailed" in baseline.stdout, baseline.stdout + baseline.stderr
    strict = run(True)
    assert strict.returncode == 1 and "failed" in strict.stdout, strict.stdout + strict.stderr
    test_path.write_text("import pytest\n@pytest.fixture\ndef broken():\n    assert False, 'broken fixture'\n@pytest.mark.slo\ndef test_case(broken):\n    pass\n")
    broken = run(False)
    assert broken.returncode == 1 and "broken fixture" in broken.stdout, broken.stdout + broken.stderr
    ledger_path.write_text(json.dumps({"schema": 1, "entries": []}))
    for body in ("pytest.skip('fixture')", "pytest.xfail('fixture')", "assert True"):
        test_path.write_text("import pytest\n@pytest.mark.slo\ndef test_case():\n    " + body + "\n")
        result = run(True)
        assert result.returncode == 1 and "missing_reports" in result.stdout, result.stdout + result.stderr
    target_path.write_text(json.dumps({"schema": 1, "scenarios": []}))
    test_path.write_text("import pytest\npytest.skip('collection fixture', allow_module_level=True)\n")
    collection_skip = run(True)
    assert collection_skip.returncode == 1 and "bad_outcomes" in collection_skip.stdout, collection_skip.stdout + collection_skip.stderr
    test_path.write_text("import pytest\n@pytest.mark.slo\ndef test_case():\n    pass\n")
    ledger["entries"][0]["nodeid"] = "slo/test_case.py::test_missing"
    ledger_path.write_text(json.dumps(ledger))
    stale = run(False)
    assert stale.returncode != 0 and "stale SLO expected failure" in stale.stdout + stale.stderr
