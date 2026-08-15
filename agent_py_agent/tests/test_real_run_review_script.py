from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_real_run_review_script_writes_jsonl_and_markdown(tmp_path: Path) -> None:
    run_root = tmp_path / "runs"
    out_dir = tmp_path / "reports"
    report_path = (
        run_root
        / "main-agent-stage5-research-pdf-20260521"
        / "main_agent_task_execution"
        / "tasks"
        / "research"
        / "acceptance_report.json"
    )
    report_path.parent.mkdir(parents=True)
    report_path.write_text(
        json.dumps({"ok": False, "findings": [{"code": "ARTIFACT_MISSING"}]}),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            sys.executable,
            "scripts/review_real_runs.py",
            "--runs-root",
            str(run_root),
            "--glob",
            "*20260521*",
            "--out-dir",
            str(out_dir),
            "--json",
        ],
        cwd=Path(__file__).resolve().parents[2],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["summary"] == {"failed": 1, "passed": 0, "total": 1, "unknown": 0}
    assert (out_dir / "real-run-review.jsonl").is_file()
    assert (out_dir / "real-run-review.md").is_file()
    assert "ARTIFACT_MISSING" in (out_dir / "real-run-review.md").read_text(encoding="utf-8")
