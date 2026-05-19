from __future__ import annotations

import subprocess
import sys


def test_live_lab_module_entrypoint_runs_help():
    """`python -m scripts.live_lab.cli` should execute the CLI, not silently import."""

    result = subprocess.run(
        [sys.executable, "-m", "scripts.live_lab.cli", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )

    assert result.returncode == 0
    assert "usage: live_agent_lab.py" in result.stdout
