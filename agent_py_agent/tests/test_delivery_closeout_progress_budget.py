from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from agent_py_agent.agent.agent_core.delivery_closeout_config import DeliveryCloseoutConfig
from agent_py_agent.agent.agent_core.main_agent_delivery_closeout_progress import (
    _no_progress_block_threshold,
)


# LLM: missing one artifact or all artifacts is the same retry class: the delivery is incomplete.
# 函数用途: 验证缺产物场景统一使用 missing_artifacts_retry_limit，不再拆成 2/5/6 等专项阈值。
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


# LLM: complete-but-invalid artifacts should use the invalid-artifact retry class.
# 函数用途: 验证产物齐全但内容/格式/字段不合格时默认给三次验收返工机会。
def test_invalid_existing_artifacts_use_configured_invalid_retry_limit(tmp_path: Path):
    bad = tmp_path / "out.txt"
    bad.write_text("", encoding="utf-8")
    report = {"artifacts": [_artifact(False, "ARTIFACT_EMPTY", str(bad))]}
    agent = SimpleNamespace(_delivery_closeout_config=DeliveryCloseoutConfig(invalid_artifacts_retry_limit=3))

    threshold = _no_progress_block_threshold(report, agent=agent)

    assert threshold == 3


# LLM: zero closeout retry limits mean the model can keep reworking without a count-only block.
# 函数用途: 验证交付验收次数门 0 语义和探索/本地进展门保持一致。
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
