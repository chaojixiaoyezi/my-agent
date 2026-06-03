from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_check_replay_contracts_script_outputs_green_json():
    repo_root = Path(__file__).resolve().parents[2]
    script = repo_root / "scripts" / "check_replay_contracts.py"

    completed = subprocess.run(
        [sys.executable, str(script), "--repo-root", str(repo_root), "--json"],
        check=True,
        capture_output=True,
        text=True,
    )

    payload = json.loads(completed.stdout)
    assert payload["ok"] is True
    assert payload["count"] >= 1
    assert all(item["ok"] for item in payload["results"])
