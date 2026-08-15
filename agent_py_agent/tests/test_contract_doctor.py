from __future__ import annotations


def test_contract_doctor_rejects_schema_type_and_unknown_verifier() -> None:
    from agent_py_agent.agent.contracts.contract_doctor import lint_contract

    report = lint_contract(
        {
            "version": 2,
            "artifact_path": "output.md",
            "max_steps": "twenty",
            "rules": ["magic_verify"],
        },
        known_verifiers=("artifact_acceptance",),
    )

    assert report.ok is False
    assert report.error_codes == (
        "CONTRACT_SCHEMA_INVALID",
        "CONTRACT_FIELD_TYPE_INVALID",
        "UNKNOWN_VERIFIER",
    )


def test_contract_doctor_rejects_conflicts_and_impossible_rules() -> None:
    from agent_py_agent.agent.contracts.contract_doctor import lint_contract

    report = lint_contract(
        {
            "version": 2,
            "artifacts": {
                "required": [
                    {
                        "path": "output.md",
                        "min_size": 100_000_000_001,
                        "required_sections": ["Evidence"],
                        "forbidden_words": ["Evidence"],
                    }
                ]
            },
            "required_tools": ["block_ip"],
            "forbidden_tools": ["block_ip"],
        },
        known_verifiers=("artifact_acceptance",),
    )

    assert report.ok is False
    assert report.error_codes == ("CONTRACT_RULE_CONFLICT", "CONTRACT_IMPOSSIBLE")


def test_contract_doctor_rejects_non_current_versions() -> None:
    from agent_py_agent.agent.contracts.contract_doctor import lint_contract

    v1 = lint_contract({"version": 1, "artifact_path": "output.md"})
    unsupported = lint_contract({"version": 99, "artifacts": {"required": []}})

    assert v1.error_codes == ("CONTRACT_VERSION_UNSUPPORTED",)
    assert unsupported.error_codes == ("CONTRACT_VERSION_UNSUPPORTED",)
