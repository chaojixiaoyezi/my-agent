from __future__ import annotations


# LLM: Shared tool manifest contracts must expose recovery facts, not only tool names.
# 函数用途: 验证共享工具清单会输出 visible/executable tools 和失败恢复合同。
def test_tool_manifest_payload_includes_failure_contracts():
    from agent_py_agent.agent.contracts.tool_manifest_contract import tool_manifest_payload

    payload = tool_manifest_payload(
        [
            {
                "name": "read_file",
                "category": "filesystem",
                "description": "Read a file",
                "parameters": {"path": "file path"},
                "parameter_details": {"path": "绝对路径"},
                "examples": ['{"tool":"read_file"}'],
            },
            {
                "name": "write_file",
                "category": "filesystem",
                "description": "Write a file",
                "parameters": {"path": "file path", "content": "text"},
                "parameter_details": {"path": "绝对路径", "content": "内容"},
                "examples": ['{"tool":"write_file"}'],
            },
        ],
        allowed_tools=["read_file", "write_file"],
        granted_capabilities=["filesystem"],
    )

    assert payload["visible_tools"] == ["read_file", "write_file"]
    assert payload["executable_tools"] == ["read_file", "write_file"]
    assert payload["permission_mode"] == "same_as_root_agent"
    assert payload["failure_taxonomy"][0] == "PATH_INVALID"
    failure_contracts = {item["code"]: item for item in payload["failure_contracts"]}
    assert failure_contracts["TOOL_UNAVAILABLE"]["recommended_action"] == "request_capability_or_choose_available_tool"
    assert failure_contracts["APPROVAL_REQUIRED"]["retryable"] is True
    assert payload["tools"][0]["visible_in_context"] is True


# LLM: Subagent-style owners must surface owner-scoped permission mode in the same manifest contract.
# 函数用途: 验证 owner_type 变化时权限模式字段同步变化，不需要各处单独拼字符串。
def test_tool_manifest_payload_uses_owner_scoped_permission_mode_for_non_root_owner():
    from agent_py_agent.agent.contracts.tool_manifest_contract import tool_manifest_payload

    payload = tool_manifest_payload(
        [{"name": "read_file", "category": "filesystem", "parameters": {}}],
        owner_type="subagent",
    )

    assert payload["permission_mode"] == "owner_scoped"
    assert payload["tools"][0]["permission_mode"] == "owner_scoped"
