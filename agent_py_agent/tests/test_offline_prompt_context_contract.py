from __future__ import annotations


# LLM: Context assembly must keep machine contract fields even when history is truncated.
# 函数用途: 验证 contract_hash、allowed_tools、required_artifacts 等机器字段不能被截断丢失。
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


# LLM: Allowed tools must have visible tool schema.
# 函数用途: 验证 allowed_tools 与 tool_schemas 不一致时构建上下文失败。
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


# LLM: Untrusted user/tool/memory text cannot override machine contracts.
# 函数用途: 验证 prompt 注入和污染记忆只作为数据，不可改写合同事实。
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
