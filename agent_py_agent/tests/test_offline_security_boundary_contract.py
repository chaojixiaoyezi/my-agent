from __future__ import annotations

import pytest


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


# LLM: 离线合同与在线门共用永久禁止项；白名单不能覆盖云凭据端点。
# 函数用途: 覆盖多个厂商元数据地址及域名的不可授权性。
@pytest.mark.parametrize("host", ["169.254.169.254", "metadata.google.internal", "metadata.goog", "100.100.100.200", "[fd00:ec2::254]", "[::ffff:169.254.169.254]"])
def test_offline_metadata_never_allowlisted(host):
    from agent_py_agent.agent.contracts.offline_security_boundary_contract import (
        validate_security_boundary_events,
    )

    result = validate_security_boundary_events(
        ({"type": "network_request", "url": f"http://{host}/"},),
        allowed_private_hosts=(host.strip("[]"), "169.254.0.0/16", "100.64.0.0/10", "fd00::/8"),
    )
    assert not result.ok
    assert result.error_codes[0].startswith("NETWORK_ALWAYS_BLOCKED_")


# LLM: 离线授权输入同样防裸字符串，CIDR/localhost 用共享匹配而非字符串集合。
# 函数用途: 覆盖离线网段授权及不合法的授权容器。
@pytest.mark.parametrize(("host", "entries", "allowed"), [
    ("localhost", ("127.0.0.0/8",), True),
    ("192.168.0.50", ("192.168.0.0/24",), True),
    ("[::ffff:192.168.0.50]", ("192.168.0.0/24",), True),
    ("localhost", "localhost", False),
])
def test_offline_private_allowlist_formats(host, entries, allowed):
    from agent_py_agent.agent.contracts.offline_security_boundary_contract import (
        validate_security_boundary_events,
    )

    result = validate_security_boundary_events(
        ({"type": "network_request", "url": f"http://{host}:8000/"},), allowed_private_hosts=entries,
    )
    assert result.ok is allowed
