"""诊断只使用临时显式路径；验证真实计数和事实一致性，不以耗时作为通过门槛。"""

import json
import subprocess
import sys
from pathlib import Path

from agent_py_agent.tests.support.ci_release_layout import (
    assert_business_origins,
    child_origin_capture,
)


def test_storage_diagnostic_measures_actual_calls_and_preserves_facts(tmp_path):
    script = Path(__file__).resolve().parents[2] / "scripts/diag/background_tick_cost.py"
    output = tmp_path / "measurement.json"
    origins_file = tmp_path / "diagnostic-origins.json"
    code = "import runpy, sys\n"
    code += f"sys.argv = {[str(script), '--owners', '2', '--ticks', '2', '--ledger-runs', '25', '--out', str(output)]!r}\n"
    code += f"try:\n    runpy.run_path({str(script)!r}, run_name='__main__')\nexcept SystemExit as exc:\n    if exc.code not in (None, 0): raise\n"
    code += child_origin_capture(f"Path({str(origins_file)!r}).write_text(json.dumps(facts))")
    result = subprocess.run([sys.executable, "-I", "-c", code],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(output.read_text())
    facts = json.loads(origins_file.read_text())
    assert_business_origins(facts)
    assert json.loads(result.stdout) == report
    assert report["scope"] == "storage_phases_only" and report["fixture_removed"]
    assert report["comparison"] == "runtime_wake_attention_probe"
    assert report["scheduler_process_cache"] == "always_on"
    off, on = report["results"]
    owner_ticks = report["owners"] * report["ticks"]
    # 每拍两臂均推进一次 scheduler history 索引；另外 off 有两个 runtime 读口，on 只有一次候选探针。
    assert off["sqlite_connections"] == 3 * owner_ticks
    assert on["sqlite_connections"] == 2 * owner_ticks
    assert off["sql_statements"] == 6 * owner_ticks
    assert on["sql_statements"] == 3 * owner_ticks
    # SW-W1已退役scheduler/process慢路；两臂预热后都必须复用可信投影，不清缓存凑旧次数。
    assert off["scheduler_reads"] == on["scheduler_reads"] == 0
    assert off["scheduler_read_bytes"] == on["scheduler_read_bytes"] == 0
    assert off["process_transactions"] == on["process_transactions"] == 0
    assert off["facts_sha256"] == on["facts_sha256"]
    assert all(row["wall_seconds"] > 0 and row["cpu_seconds"] >= 0 for row in (off, on))


def test_storage_diagnostic_observer_counts_real_cold_reads(tmp_path):
    """零热读不能来自坏计数器：同一观察器必须看到真实冷读，并恢复原入口。"""
    import runpy
    from types import SimpleNamespace

    script = Path(__file__).resolve().parents[2] / 'scripts/diag/background_tick_cost.py'
    diagnostic = SimpleNamespace(**runpy.run_path(str(script)))
    stores = diagnostic._build_stores(tmp_path / 'synthetic', owners=1, ledger_runs=25)
    scheduler = diagnostic.scheduler_module
    process = diagnostic.ProcessSessionStore
    runtime = diagnostic.RuntimeSchemaMixin
    originals = (scheduler._read_store_bytes, process.transaction, runtime._runtime_connect)
    cold = diagnostic._Counters()
    with diagnostic._observe_reads(cold):
        diagnostic._storage_round(stores, False)
    assert cold.scheduler_reads == 1 and cold.scheduler_read_bytes > 0
    assert cold.process_transactions == 1
    before = diagnostic._facts(stores)
    warm = diagnostic._Counters()
    with diagnostic._observe_reads(warm):
        diagnostic._storage_round(stores, False)
        diagnostic._storage_round(stores, True)
    assert warm.scheduler_reads == warm.scheduler_read_bytes == warm.process_transactions == 0
    assert diagnostic._facts(stores) == before
    assert originals == (scheduler._read_store_bytes, process.transaction, runtime._runtime_connect)
