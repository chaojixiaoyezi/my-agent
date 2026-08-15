from __future__ import annotations


def test_tool_adapter_readiness_accepts_read_only_and_dry_run_adapters() -> None:
    from agent_py_agent.agent.contracts.tool_adapter_readiness_contract import (
        validate_tool_adapter_readiness,
    )

    result = validate_tool_adapter_readiness(
        (
            {
                "tool": "query_logs",
                "effect": "read_only",
                "modes": ["read_only"],
                "result_schema_ref": "schema://tools/query_logs/result",
                "contract_tests": [
                    "success",
                    "timeout",
                    "auth_failure",
                    "empty_result",
                    "large_output",
                    "secret_redaction",
                ],
            },
            {
                "tool": "block_ip",
                "effect": "dangerous",
                "modes": ["dry_run"],
                "dry_run_only": True,
                "mode_field": "mode",
                "real_execution_enabled": False,
                "requires_approval": True,
                "idempotency_key_required": True,
                "result_schema_ref": "schema://tools/block_ip/result",
                "contract_tests": ["dry_run_success", "approval_required", "args_hash_bound"],
            },
        )
    )

    assert result.ok is True
    assert result.error_codes == ()


def test_tool_adapter_readiness_rejects_uncovered_and_unsafe_adapters() -> None:
    from agent_py_agent.agent.contracts.tool_adapter_readiness_contract import (
        validate_tool_adapter_readiness,
    )

    result = validate_tool_adapter_readiness(
        (
            {
                "tool": "query_logs",
                "effect": "read_only",
                "modes": ["read_only"],
                "result_schema_ref": "",
                "contract_tests": ["success"],
            },
            {
                "tool": "block_ip",
                "effect": "dangerous",
                "modes": ["dry_run", "real_run"],
                "dry_run_only": False,
                "mode_field": "",
                "real_execution_enabled": True,
                "requires_approval": False,
                "idempotency_key_required": False,
                "contract_tests": [],
            },
        )
    )

    assert result.error_codes == (
        "ADAPTER_RESULT_SCHEMA_MISSING",
        "ADAPTER_READ_ONLY_TEST_COVERAGE_MISSING",
        "ADAPTER_DRY_RUN_MODE_FIELD_MISSING",
        "ADAPTER_REAL_RUN_NOT_APPROVED",
        "ADAPTER_SIDE_EFFECT_IDEMPOTENCY_MISSING",
        "ADAPTER_DRY_RUN_ONLY_VIOLATED",
    )
