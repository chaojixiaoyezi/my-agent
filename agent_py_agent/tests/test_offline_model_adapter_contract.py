from __future__ import annotations


# LLM: Existing tool protocol should normalize legacy calls into stable operation/idempotency fields.
# 函数用途: 验证旧 tool/args 调用不会缺 operation_id 或 idempotency_key。
def test_model_adapter_normalizes_legacy_tool_call_ids() -> None:
    from agent_py_agent.agent.contracts.tool_protocol_v2 import (
        normalize_tool_call,
        validate_tool_call,
    )

    envelope = normalize_tool_call({"tool": "read_file", "args": {"path": "input.txt"}})

    assert envelope.operation_id
    assert envelope.idempotency_key
    assert validate_tool_call(envelope) == []


# LLM: Adapter facts should reject missing tool_call_id when no generated id is recorded.
# 函数用途: 验证模型输出没有 tool_call_id 且系统未补 ID 时会失败。
def test_model_adapter_requires_tool_call_id_or_generated_id() -> None:
    from agent_py_agent.agent.contracts.offline_model_adapter_contract import (
        validate_model_adapter_facts,
    )

    result = validate_model_adapter_facts({"tool_calls": [{"tool_name": "read_file"}]})

    assert result.error_codes == ("TOOL_CALL_ID_MISSING",)


# LLM: Streaming fragments and upstream failures should be bounded and structured.
# 函数用途: 验证半截 JSON、模型重试耗尽、模型切换 schema 不兼容不会卡死或假成功。
def test_model_adapter_rejects_streaming_and_retry_and_schema_failures() -> None:
    from agent_py_agent.agent.contracts.offline_model_adapter_contract import (
        validate_model_adapter_facts,
    )

    result = validate_model_adapter_facts(
        {
            "stream": {"complete": False, "partial_json": True},
            "model_errors": [{"retryable": True}, {"retryable": True}, {"retryable": True}],
            "retry_limit": 2,
            "model_switch": {"from_schema": "tools-v2", "to_schema": "json-text"},
        }
    )

    assert result.error_codes == (
        "STREAMING_JSON_INCOMPLETE",
        "MODEL_RETRY_LIMIT_EXCEEDED",
        "MODEL_SWITCH_SCHEMA_MISMATCH",
    )


# LLM: zero retry limits should disable the adapter retry cap instead of acting as one retry.
# 函数用途: 验证 retry_limit=0 表示模型适配器重试预算不设上限。
def test_model_adapter_zero_retry_limit_is_unlimited() -> None:
    from agent_py_agent.agent.contracts.offline_model_adapter_contract import (
        validate_model_adapter_facts,
    )

    result = validate_model_adapter_facts(
        {
            "model_errors": [{"retryable": True}, {"retryable": True}, {"retryable": True}],
            "retry_limit": 0,
        }
    )

    assert "MODEL_RETRY_LIMIT_EXCEEDED" not in result.error_codes
