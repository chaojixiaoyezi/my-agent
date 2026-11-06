"""在未加载 pytest/conftest 的新解释器里验证导入不重新启用 GIL。"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

PROBE_TIMEOUT_SECONDS = 45
PROBE_PATH = Path(__file__).with_name("_import_probe.py")
REPO_ROOT = Path(__file__).resolve().parents[3]


def _probe(stage):
    result = subprocess.run(
        [sys.executable, str(PROBE_PATH), stage], cwd=REPO_ROOT,
        capture_output=True, text=True, timeout=PROBE_TIMEOUT_SECONDS,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    observed = json.loads(result.stdout.splitlines()[-1])
    assert observed["gil"] == observed["before"]
    return observed


def test_every_available_production_module_preserves_gil_state():
    observed = _probe("all")
    assert len(observed["imported"]) > 0
    for skipped in observed["skipped"]:
        assert skipped["module"].startswith("agent_py_agent.")
        assert skipped["missing_extra"]


@pytest.mark.parametrize("stage", ["crypto", "image", "sqlalchemy", "psycopg", "tui", "yaml", "lark"])
def test_first_lazy_extension_call_preserves_gil_state(stage):
    observed = _probe(stage)
    if observed["missing_extra"] is not None:
        pytest.skip(observed["missing_extra"])
    assert observed["stage"] == stage


@pytest.mark.skipif(getattr(sys, "_is_gil_enabled", lambda: True)(), reason="仅无 GIL 解释器断言")
def test_free_threaded_interpreter_still_has_gil_disabled_after_lazy_calls():
    for stage in ("crypto", "image", "sqlalchemy", "psycopg", "tui", "yaml", "lark"):
        observed = _probe(stage)
        assert observed["before"] is False
        assert observed["gil"] is False
