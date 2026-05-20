from __future__ import annotations


# LLM: Contract doctor should reject malformed schemas, wrong field types, and unknown verifiers before task start.
# 函数用途: 验证合同自身错误不会被静默忽略。
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


# LLM: Contract doctor should catch conflicting and impossible requirements from structured fields.
# 函数用途: 验证 required/forbidden 工具冲突和不可能完成的产物规则会预检失败。
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


# LLM: Contract doctor should migrate supported old contracts and reject unsupported versions explicitly.
# 函数用途: 验证 version=1 的 artifact_path 可迁移，未知版本返回 CONTRACT_VERSION_UNSUPPORTED。
def test_contract_doctor_migrates_v1_and_rejects_unknown_version() -> None:
    from agent_py_agent.agent.contracts.contract_doctor import lint_contract, migrate_contract

    migrated = migrate_contract({"version": 1, "artifact_path": "output.md"})
    unsupported = lint_contract({"version": 99, "artifacts": {"required": []}})

    assert migrated["version"] == 2
    assert migrated["artifacts"]["required"][0]["path"] == "output.md"
    assert unsupported.error_codes == ("CONTRACT_VERSION_UNSUPPORTED",)
