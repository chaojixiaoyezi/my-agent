from __future__ import annotations


def test_prompt_context_requires_core_contract_fields_after_truncation() -> None:
    from agent_py_agent.agent.contracts.offline_prompt_context_contract import (
        validate_prompt_context,
    )

    result = validate_prompt_context(
        {
            "required_contract_fields": ["contract_hash", "allowed_tools", "required_artifacts"],
            "assembled_context": {"contract_hash": "sha256:a", "allowed_tools": ["read_file"]},
            "truncated_fields": ["required_artifacts"],
        }
    )

    assert result.error_codes == ("CONTEXT_CONTRACT_FIELD_MISSING", "CONTEXT_CONTRACT_TRUNCATED")


def test_prompt_context_requires_visible_tool_schema() -> None:
    from agent_py_agent.agent.contracts.offline_prompt_context_contract import (
        validate_prompt_context,
    )

    result = validate_prompt_context(
        {
            "allowed_tools": ["query_logs", "write_file"],
            "tool_schemas": {"write_file": {"input_schema": {}}},
        }
    )

    assert result.error_codes == ("CONTEXT_TOOL_SCHEMA_MISSING",)


def test_prompt_context_rejects_untrusted_and_memory_contract_override() -> None:
    from agent_py_agent.agent.contracts.offline_prompt_context_contract import (
        validate_prompt_context,
    )

    result = validate_prompt_context(
        {
            "untrusted_inputs": [{"applied_to_contract": True}],
            "memory_entries": [{"attempted_contract_override": True}],
        }
    )

    assert result.error_codes == ("UNTRUSTED_INPUT_OVERRIDES_CONTRACT", "MEMORY_OVERRIDES_CONTRACT")
