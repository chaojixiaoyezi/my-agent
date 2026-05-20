from __future__ import annotations


# LLM: Private network targets should be blocked by URL facts before any browser/fetch call.
# 函数用途: 验证 localhost、loopback 和私网 IP 触发 NETWORK_PRIVATE_HOST_BLOCKED。
def test_network_url_policy_blocks_private_hosts_without_allowlist() -> None:
    from agent_py_agent.agent.contracts.offline_security_boundary_contract import (
        validate_security_boundary_events,
    )

    result = validate_security_boundary_events(
        (
            {"type": "network_request", "url": "http://127.0.0.1:8000/admin"},
            {"type": "network_request", "url": "http://10.0.0.5/metadata"},
            {"type": "network_request", "url": "https://example.test"},
        )
    )

    assert result.ok is False
    assert result.error_codes == ("NETWORK_PRIVATE_HOST_BLOCKED",)
    assert [item["host"] for item in result.findings] == ["127.0.0.1", "10.0.0.5"]


# LLM: Prompt-injection content is allowed as data but cannot mutate machine contracts.
# 函数用途: 验证外部文本只有在被错误应用到机器合同时才生成隔离 finding。
def test_prompt_injection_text_cannot_override_state_machine_or_contracts() -> None:
    from agent_py_agent.agent.contracts.offline_security_boundary_contract import (
        validate_security_boundary_events,
    )

    allowed = validate_security_boundary_events(
        ({"type": "external_text", "untrusted_instruction_detected": True, "applied_to_machine_contract": False},)
    )
    blocked = validate_security_boundary_events(
        ({"type": "external_text", "untrusted_instruction_detected": True, "applied_to_machine_contract": True},)
    )

    assert allowed.ok is True
    assert blocked.error_codes == ("PROMPT_INJECTION_IGNORED_AS_DATA",)


# LLM: LLM context must not receive raw secret fields.
# 函数用途: 验证 token/password 等结构字段未脱敏会返回 SECRET_REDACTION_REQUIRED。
def test_secret_fields_are_redacted_before_llm_context() -> None:
    from agent_py_agent.agent.contracts.offline_security_boundary_contract import (
        validate_security_boundary_events,
    )

    result = validate_security_boundary_events(
        ({"type": "llm_context", "payload": {"tool_result": {"token": "raw-token", "password": "[redacted]"}}},)
    )

    assert result.ok is False
    assert result.error_codes == ("SECRET_REDACTION_REQUIRED",)
    assert result.findings[0]["field_path"] == "payload.tool_result.token"


# LLM: Side effects need idempotency keys and cannot reuse one key for different args.
# 函数用途: 验证消息、产物写入等副作用缺少幂等键或同键换参数会被拦截。
def test_replayed_message_or_artifact_write_uses_idempotency_key() -> None:
    from agent_py_agent.agent.contracts.offline_security_boundary_contract import (
        validate_security_boundary_events,
    )

    result = validate_security_boundary_events(
        (
            {"type": "tool_call", "tool": "send_message", "side_effect": True, "args_hash": "a"},
            {
                "type": "tool_call",
                "tool": "write_artifact",
                "side_effect": True,
                "idempotency_key": "artifact-1",
                "args_hash": "a",
            },
            {
                "type": "tool_call",
                "tool": "write_artifact",
                "side_effect": True,
                "idempotency_key": "artifact-1",
                "args_hash": "b",
            },
        )
    )

    assert result.ok is False
    assert result.error_codes == ("IDEMPOTENCY_KEY_REQUIRED", "SIDE_EFFECT_REPLAY_BLOCKED")


# LLM: Path and URL normalization should catch symlink escapes, file URLs, and localhost variants.
# 函数用途: 验证符号链接越界、file://、IPv6 localhost 和数字 localhost 会被安全边界合同拦截。
def test_advanced_path_and_url_boundary_blocks_escape_variants() -> None:
    from agent_py_agent.agent.contracts.offline_security_boundary_contract import (
        validate_security_boundary_events,
    )

    result = validate_security_boundary_events(
        (
            {
                "type": "path_access",
                "path": "/workspace/link",
                "resolved_path": "/etc/passwd",
                "workspace_root": "/workspace",
            },
            {"type": "network_request", "url": "file:///etc/passwd"},
            {"type": "network_request", "url": "http://[::1]/admin"},
            {"type": "network_request", "url": "http://2130706433/admin"},
        )
    )

    assert result.error_codes == (
        "PATH_SYMLINK_ESCAPE_BLOCKED",
        "NETWORK_FILE_URL_BLOCKED",
        "NETWORK_PRIVATE_HOST_BLOCKED",
    )
