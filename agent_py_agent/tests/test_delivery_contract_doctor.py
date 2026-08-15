from __future__ import annotations

from pathlib import Path

from agent_py_agent.tests.support.delivery_contract_suite import run_delivery_contract_suite


def test_delivery_contract_doctor_runs_standard_contract_suite(tmp_path: Path) -> None:
    from agent_py_agent.agent.contracts.delivery_contract_doctor import validate_delivery_contract

    run_delivery_contract_suite(validate_delivery_contract, tmp_path)


def test_delivery_contract_doctor_accepts_unknown_kind_with_explicit_extension(tmp_path: Path) -> None:
    from agent_py_agent.agent.contracts.delivery_contract_doctor import validate_delivery_contract

    report = validate_delivery_contract(
        {
            "schema_version": "delivery_contract.v1",
            "artifacts": [
                {
                    "artifact_id": "model",
                    "kind": "gguf",
                    "allowed_output_roots": ["outputs"],
                    "validation_contract": {"file_extension": "gguf"},
                }
            ],
        },
        workspace_root=tmp_path,
    )

    assert report.ok is True
    assert report.to_dict()["normalized_contract"]["artifacts"][0]["kind"] == "gguf"


def test_delivery_contract_doctor_normalizes_structured_required_columns(tmp_path: Path) -> None:
    from agent_py_agent.agent.contracts.delivery_contract_doctor import validate_delivery_contract

    report = validate_delivery_contract(
        {
            "schema_version": "delivery_contract.v1",
            "artifacts": [
                {
                    "artifact_id": "summary",
                    "kind": "markdown",
                    "preferred_path": "outputs/report.md",
                    "validation_contract": {
                        "required_columns": [
                            {"column_name": "来源文件", "description": "文件路径"},
                            {"column_name": "行号", "description": "行号"},
                            {"name": "编号"},
                        ],
                    },
                }
            ],
        },
        workspace_root=tmp_path,
    )

    assert report.ok is True
    validation = report.to_dict()["normalized_contract"]["artifacts"][0]["validation_contract"]
    assert validation["required_columns"] == ["来源文件", "行号", "编号"]


def test_delivery_contract_doctor_normalizes_text_validation_lists(tmp_path: Path) -> None:
    from agent_py_agent.agent.contracts.delivery_contract_doctor import validate_delivery_contract

    report = validate_delivery_contract(
        {
            "schema_version": "delivery_contract.v1",
            "artifacts": [
                {
                    "artifact_id": "summary",
                    "kind": "markdown",
                    "preferred_path": "outputs/report.md",
                    "validation_contract": {
                        "required_strings": ["CP-001"],
                        "forbidden_strings": ["见原文"],
                        "required_regex": [r"CP-\d+"],
                        "forbidden_regex": [r"TODO"],
                    },
                }
            ],
        },
        workspace_root=tmp_path,
    )

    assert report.ok is True
    validation = report.to_dict()["normalized_contract"]["artifacts"][0]["validation_contract"]
    assert validation["required_strings"] == ["CP-001"]
    assert validation["forbidden_strings"] == ["见原文"]
    assert validation["required_regex"] == [r"CP-\d+"]
    assert validation["forbidden_regex"] == [r"TODO"]


def test_delivery_contract_doctor_returns_repair_action_for_missing_target(tmp_path: Path) -> None:
    from agent_py_agent.agent.contracts.delivery_contract_doctor import validate_delivery_contract

    report = validate_delivery_contract(
        {"schema_version": "delivery_contract.v1", "artifacts": [{"artifact_id": "report"}]},
        workspace_root=tmp_path,
    )

    payload = report.to_dict()
    assert report.ok is False
    assert payload["should_rematerialize"] is True
    assert payload["repair_actions"][0]["recommended_action"] == "repair_effective_contract"


def test_recovery_action_schema_rejects_missing_machine_fields() -> None:
    from agent_py_agent.agent.contracts.delivery_contract_doctor import validate_recovery_action

    report = validate_recovery_action({"message": "please fix it"})

    assert report.ok is False
    codes = {item["code"] for item in report.to_dict()["findings"]}
    assert "RECOVERY_ACTION_CODE_REQUIRED" in codes
    assert "RECOVERY_ACTION_RECOMMENDED_ACTION_REQUIRED" in codes
