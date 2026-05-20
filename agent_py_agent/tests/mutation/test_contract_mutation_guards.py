from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from agent_py_agent.tests.support.contract_fixture_runner import (
    FixtureRunFacts,
    verify_contract_fixture,
)


def test_contract_mutation_guards_reject_missing_evidence_source(tmp_path: Path):
    contract = _evidence_contract()
    _write_json_report(tmp_path, evidence_source="trace:query-1")
    assert _verify_ok(tmp_path, contract) is True

    _write_json_report(tmp_path, evidence_source="")
    result = verify_contract_fixture(tmp_path, contract, _success_facts())

    assert result.ok is False
    assert "EVIDENCE_SOURCE_MISSING" in result.error_codes
    assert "FINAL_STATUS_REJECTED" in result.error_codes


def test_contract_mutation_guards_reject_dry_run_replacing_real_action(tmp_path: Path):
    contract = _real_action_contract()
    (tmp_path / "report.md").write_text("Summary\nAction completed\n", encoding="utf-8")
    assert _verify_ok(tmp_path, contract, facts=_real_action_facts()) is True

    mutated = FixtureRunFacts(
        tool_trace=(
            {"tool": "write_file", "result": {"ok": True}},
            {"tool": "block_ip", "result": {"ok": True, "mode": "dry_run"}},
        ),
        final_status="SUCCEEDED",
    )
    result = verify_contract_fixture(tmp_path, contract, mutated)

    assert result.ok is False
    assert "REQUIRED_REAL_TOOL_CALL_MISSING" in result.error_codes


def test_contract_mutation_guards_reject_path_escape(tmp_path: Path):
    contract = _real_action_contract()
    mutated = deepcopy(contract)
    mutated["artifacts"]["required"][0]["path"] = "../report.md"
    (tmp_path.parent / "report.md").write_text("Summary\nOutside\n", encoding="utf-8")

    result = verify_contract_fixture(tmp_path, mutated, _real_action_facts())

    assert result.ok is False
    assert "ARTIFACT_PATH_OUTSIDE_RUN_DIR" in result.error_codes


def test_contract_mutation_guards_reject_required_tool_failure(tmp_path: Path):
    contract = _real_action_contract()
    (tmp_path / "report.md").write_text("Summary\nAction attempted\n", encoding="utf-8")
    facts = FixtureRunFacts(
        tool_trace=(
            {"tool": "write_file", "result": {"ok": True}},
            {"tool": "block_ip", "result": {"ok": False, "error_code": "APPROVAL_REQUIRED"}},
        ),
        final_status="SUCCEEDED",
    )

    result = verify_contract_fixture(tmp_path, contract, facts)

    assert result.ok is False
    assert "REQUIRED_SUCCESSFUL_TOOL_CALL_MISSING" in result.error_codes
    assert "FINAL_STATUS_REJECTED" in result.error_codes


def _verify_ok(
    run_dir: Path,
    contract: dict[str, object],
    *,
    facts: object | None = None,
) -> bool:
    return verify_contract_fixture(run_dir, contract, facts or _success_facts()).ok


def _evidence_contract() -> dict[str, object]:
    return {
        "artifacts": {
            "required": [
                {
                    "path": "report.json",
                    "min_size": 20,
                    "json_requirements": {"required_non_empty_paths": ["evidence"]},
                    "evidence": {"json_path": "evidence", "require_source": True},
                }
            ]
        },
        "tools": {"required_calls": ["query_logs"], "required_successful_calls": ["query_logs"]},
        "final_status": {
            "allow_succeeded_only_if": [
                "artifact_exists",
                "artifact_non_empty",
                "json_requirements_present",
                "evidence_source_present",
                "required_tool_calls_present",
                "required_successful_tool_calls_present",
            ]
        },
    }


def _real_action_contract() -> dict[str, object]:
    return {
        "artifacts": {"required": [{"path": "report.md", "min_size": 10}]},
        "tools": {
            "required_calls": ["write_file", "block_ip"],
            "required_successful_calls": ["write_file", "block_ip"],
            "required_real_successful_calls": ["block_ip"],
        },
        "final_status": {
            "allow_succeeded_only_if": [
                "artifact_exists",
                "artifact_non_empty",
                "required_tool_calls_present",
                "required_successful_tool_calls_present",
                "required_real_tool_calls_present",
            ]
        },
    }


def _write_json_report(run_dir: Path, *, evidence_source: str) -> None:
    source = f'"source_ref":"{evidence_source}"' if evidence_source else '"summary":"missing source"'
    (run_dir / "report.json").write_text(f'{{"evidence":[{{{source}}}]}}', encoding="utf-8")


def _success_facts():
    return FixtureRunFacts(
        tool_trace=({"tool": "query_logs", "result": {"ok": True}},),
        final_status="SUCCEEDED",
    )


def _real_action_facts():
    return FixtureRunFacts(
        tool_trace=(
            {"tool": "write_file", "result": {"ok": True}},
            {"tool": "block_ip", "result": {"ok": True}},
        ),
        final_status="SUCCEEDED",
    )
