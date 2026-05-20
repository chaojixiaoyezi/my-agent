from __future__ import annotations

import json
from pathlib import Path


def test_fake_llm_claims_success_without_artifact_is_rejected(tmp_path: Path):
    from agent_py_agent.tests.support.fake_llm_runner import FakeLLMRunner

    runner = FakeLLMRunner.from_fixture(_fixture("claim_success_without_artifact.json"))

    result = runner.run(tmp_path)

    assert result.final_status == "SUCCEEDED"
    assert result.contract_result.ok is False
    assert "ARTIFACT_MISSING" in result.contract_result.error_codes
    assert "FINAL_STATUS_REJECTED" in result.contract_result.error_codes


def test_fake_llm_writes_empty_artifact_is_rejected(tmp_path: Path):
    from agent_py_agent.tests.support.fake_llm_runner import FakeLLMRunner

    runner = FakeLLMRunner.from_fixture(_fixture("write_empty_artifact.json"))

    result = runner.run(tmp_path)

    assert (tmp_path / "output.md").exists()
    assert result.contract_result.ok is False
    assert "ARTIFACT_TOO_SMALL" in result.contract_result.error_codes


def _fixture(name: str) -> dict[str, object]:
    path = Path(__file__).parent / name
    return json.loads(path.read_text(encoding="utf-8"))
