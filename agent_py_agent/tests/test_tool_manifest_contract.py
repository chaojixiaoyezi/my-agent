from __future__ import annotations


def test_tool_manifest_payload_includes_failure_contracts():
    payload = _tool_manifest_payload()

    assert payload["visible_tools"] == ["read_file", "write_file"]
    assert payload["executable_tools"] == ["read_file", "write_file"]
    assert payload["permission_mode"] == "same_as_root_agent"
    assert payload["failure_taxonomy"] == sorted(payload["failure_taxonomy"])
    assert "PATH_INVALID" in payload["failure_taxonomy"]
    failure_contracts = {item["code"]: item for item in payload["failure_contracts"]}
    assert failure_contracts["TOOL_UNAVAILABLE"]["recommended_action"] == "request_capability"
    assert failure_contracts["APPROVAL_REQUIRED"]["retryable"] is True
    assert payload["tools"][0]["visible_in_context"] is True


def test_tool_manifest_payload_includes_side_effect_policy_fields():
    payload = _tool_manifest_payload()
    write_manifest = payload["tools"][1]

    assert write_manifest["effect"] == "mutating"
    assert write_manifest["default_mode"] == "real"
    assert write_manifest["idempotency_scope"] == "operation"
    assert write_manifest["requires_approval"] is False
    assert write_manifest["timeout_seconds"] == 20
    assert write_manifest["output_refs"] == ["file"]


def test_tool_manifest_payload_uses_owner_scoped_permission_mode_for_non_root_owner():
    from agent_py_agent.agent.contracts.tool_manifest_contract import tool_manifest_payload

    payload = tool_manifest_payload(
        [{"name": "read_file", "category": "filesystem", "parameters": {}}],
        owner_type="subagent",
    )

    assert payload["permission_mode"] == "owner_scoped"
    assert payload["tools"][0]["permission_mode"] == "owner_scoped"


def _tool_manifest_payload() -> dict[str, object]:
    from agent_py_agent.agent.contracts.tool_manifest_contract import tool_manifest_payload

    return tool_manifest_payload(
        [
            {
                "name": "read_file",
                "category": "filesystem",
                "description": "Read a file",
                "effect": "read_only",
                "parameters": {"path": "file path"},
                "parameter_details": {"path": "绝对路径"},
                "examples": ['{"tool":"read_file"}'],
            },
            {
                "name": "write_file",
                "category": "filesystem",
                "description": "Write a file",
                "effect": "mutating",
                "default_mode": "real",
                "idempotency_scope": "operation",
                "requires_approval": False,
                "timeout_seconds": 20,
                "output_refs": ["file"],
                "parameters": {"path": "file path", "content": "text"},
                "parameter_details": {"path": "绝对路径", "content": "内容"},
                "examples": ['{"tool":"write_file"}'],
            },
        ],
        allowed_tools=["read_file", "write_file"],
    )
