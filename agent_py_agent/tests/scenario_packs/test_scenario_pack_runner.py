from __future__ import annotations

from pathlib import Path


def test_scenario_pack_runs_contract_fake_llm_and_replay_cases(tmp_path: Path):
    from agent_py_agent.tests.support.scenario_pack_runner import run_scenario_pack

    manifest = Path(__file__).parent / "fake_done_regression_pack.json"

    result = run_scenario_pack(manifest, tmp_path)

    assert result.ok is True
    assert result.summary == {"failed": 0, "passed": 5, "total": 5}
    assert {case["case_id"] for case in result.cases} == {
        "missing_artifact_contract",
        "fake_llm_empty_artifact",
        "fake_llm_staged_json_no_rows",
        "replay_builder_not_called",
        "replay_repeated_tool",
    }
