from __future__ import annotations


def test_tool_call_policy_rejects_unknown_not_allowed_and_denied_tools() -> None:
    from agent_py_agent.agent.contracts.tool_call_policy import (
        ToolCallPolicy,
        validate_tool_call_policy,
    )

    policy = ToolCallPolicy(
        available_tools=("read_file", "write_file", "block_ip", "read_artifact"),
        allowed_tools=("read_file", "write_file"),
        denied_tools=("block_ip",),
    )

    missing_tool = validate_tool_call_policy({"tool": "missing_tool", "args": {}}, policy)
    assert missing_tool.error_code == "TOOL_NOT_FOUND"
    assert missing_tool.to_dict()["recovery"]["status"] == "repair_required"
    assert validate_tool_call_policy({"tool": "block_ip", "args": {"ip": "1.1.1.1"}}, policy).error_code == "TOOL_DENIED"
    assert validate_tool_call_policy({"tool": "write_file", "args": {"path": "out.md"}}, policy).ok is True
    assert "recovery" not in validate_tool_call_policy({"tool": "write_file", "args": {"path": "out.md"}}, policy).to_dict()
    not_allowed = validate_tool_call_policy({"tool": "read_artifact", "args": {}}, policy)
    assert not_allowed.error_code == "TOOL_NOT_ALLOWED"
    assert not_allowed.to_dict()["recovery"]["actions"][0]["message_zh"]


def test_tool_call_policy_validates_required_parameters_and_types() -> None:
    from agent_py_agent.agent.contracts.tool_call_policy import (
        ToolCallPolicy,
        validate_tool_call_policy,
    )

    policy = ToolCallPolicy(
        available_tools=("query_logs",),
        required_parameters={"query_logs": ("src_ip", "start_time", "limit")},
        parameter_types={"query_logs": {"src_ip": "string", "start_time": "string", "limit": "integer"}},
    )

    missing = validate_tool_call_policy({"tool": "query_logs", "args": {"src_ip": "1.1.1.1"}}, policy)
    wrong_type = validate_tool_call_policy(
        {"tool": "query_logs", "args": {"src_ip": ["1.1.1.1"], "start_time": "now", "limit": "10"}},
        policy,
    )
    ok = validate_tool_call_policy(
        {"tool": "query_logs", "args": {"src_ip": "1.1.1.1", "start_time": "now", "limit": 10}},
        policy,
    )

    assert missing.error_code == "TOOL_PARAMETER_REQUIRED"
    assert "start_time" in missing.findings
    assert wrong_type.error_code == "TOOL_PARAMETER_TYPE_INVALID"
    assert "src_ip:string" in wrong_type.findings
    assert ok.ok is True


def test_tool_call_policy_blocks_configured_argument_patterns() -> None:
    from agent_py_agent.agent.contracts.tool_call_policy import (
        ToolCallPolicy,
        validate_tool_call_policy,
    )

    policy = ToolCallPolicy(
        available_tools=("query_logs",),
        blocked_argument_patterns=(r";\s*rm\s+-rf\b",),
    )

    result = validate_tool_call_policy(
        {"tool": "query_logs", "args": {"src_ip": "1.1.1.1; rm -rf /"}},
        policy,
    )

    assert result.ok is False
    assert result.error_code == "TOOL_PARAMETER_BLOCKED"
