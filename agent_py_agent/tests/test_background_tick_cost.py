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
    off, on = report["results"]
    owner_ticks = report["owners"] * report["ticks"]
    # 每拍两臂均推进一次 scheduler history 索引；另外 off 有两个 runtime 读口，on 只有一次候选探针。
    assert off["sqlite_connections"] == 3 * owner_ticks
    assert on["sqlite_connections"] == 2 * owner_ticks
    # 每个owner每拍完整读一次；同拍后续scheduler读口共用可信投影，不禁用缓存凑旧12次。
    assert off["scheduler_reads"] == report["owners"] * report["ticks"] == 4
    assert on["scheduler_reads"] == 0
    assert off["scheduler_read_bytes"] > 0 and on["scheduler_read_bytes"] == 0
    assert off["process_transactions"] == 4 and on["process_transactions"] == 0
    assert off["facts_sha256"] == on["facts_sha256"]
    assert all(row["wall_seconds"] > 0 and row["cpu_seconds"] >= 0 for row in (off, on))
