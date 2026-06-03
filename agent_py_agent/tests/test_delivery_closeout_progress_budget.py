from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.delivery_closeout.config import DeliveryCloseoutConfig
from agent_py_agent.agent.agent_core.delivery_closeout.progress import (
    _no_progress_block_threshold,
)


def test_missing_artifacts_use_configured_missing_retry_limit(tmp_path: Path):
    report = {
        "artifacts": [
            _artifact(False, "ARTIFACT_MISSING", "outputs/a.txt"),
            _artifact(False, "ARTIFACT_MISSING", "outputs/b.txt"),
        ]
    }
    agent = SimpleNamespace(_delivery_closeout_config=DeliveryCloseoutConfig(missing_artifacts_retry_limit=3))

    threshold = _no_progress_block_threshold(report, agent=agent)

    assert threshold == 3


def test_invalid_existing_artifacts_use_configured_invalid_retry_limit(tmp_path: Path):
    bad = tmp_path / "out.txt"
    bad.write_text("", encoding="utf-8")
    report = {"artifacts": [_artifact(False, "ARTIFACT_EMPTY", str(bad))]}
    agent = SimpleNamespace(_delivery_closeout_config=DeliveryCloseoutConfig(invalid_artifacts_retry_limit=3))

    threshold = _no_progress_block_threshold(report, agent=agent)

    assert threshold == 3


def test_closeout_retry_zero_limit_is_unlimited(tmp_path: Path):
    report = {"artifacts": [_artifact(False, "ARTIFACT_MISSING", "outputs/a.txt")]}
    agent = SimpleNamespace(_delivery_closeout_config=DeliveryCloseoutConfig(missing_artifacts_retry_limit=0))

    assert _no_progress_block_threshold(report, agent=agent) == 0


def _artifact(ok: bool, code: str, path: str) -> dict[str, object]:
    return {
        "artifact_id": "out",
        "kind": "txt",
        "path": path,
        "ok": ok,
        "acceptance_report": {
            "ok": ok,
            "findings": [
                {
                    "code": code,
                    "severity": "hard",
                    "message": code,
                    "location": path,
                }
            ],
        },
    }
