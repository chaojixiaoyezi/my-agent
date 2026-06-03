"""测试 runtime_capabilities 模块。

函数/模块用途: 验证运行时能力只从显式 capability/tool 机器标识读取，不再从“安全日志”等自然语言关键词猜业务能力。
"""

from __future__ import annotations

from agent_py_agent.agent.agent_core.runtime.capabilities import (
    SECURITY_RUNTIME_CAPABILITY,
    _contains_keyword,
    _contains_single_keyword,
    _looks_like_security_log_task,
    _normalize_capabilities,
    resolve_runtime_capabilities,
)


def test_explicit_security_marker_adds_capability():
    result = resolve_runtime_capabilities("runtime_capabilities: logs/security")
    assert SECURITY_RUNTIME_CAPABILITY in result


def test_natural_security_log_phrases_do_not_add_capability():
    assert resolve_runtime_capabilities("analyze firewall logs") == []
    assert resolve_runtime_capabilities("查看安全日志和审计日志") == []
    assert _looks_like_security_log_task("分析安全日志") is False


def test_inject_and_granted_capabilities_preserve_explicit_protocol():
    injected = resolve_runtime_capabilities("analyze these events", inject=["logs/security"])
    granted = resolve_runtime_capabilities(
        "plain request",
        granted_capabilities=["custom_skill", "logs/security"],
    )
    assert SECURITY_RUNTIME_CAPABILITY in injected
    assert granted == ["custom_skill", "logs/security"]


def test_granted_security_capability_not_duplicated():
    result = resolve_runtime_capabilities(
        "run security_query",
        granted_capabilities=["logs/security"],
    )
    assert result.count("logs/security") == 1


def test_normalize_capabilities_contract():
    assert _normalize_capabilities(None) == []
    assert _normalize_capabilities([]) == []
    assert _normalize_capabilities(["  skill1  ", " skill2"]) == ["skill1", "skill2"]
    assert _normalize_capabilities(["Skill", "SKILL", "skill"]) == ["Skill"]
    assert _normalize_capabilities([123, "skill"]) == ["123", "skill"]


def test_keyword_helpers_match_machine_tokens_only():
    assert _contains_keyword("", ["security_query"]) is False
    assert _contains_keyword("run security_query", ["security_query"]) is True
    assert _contains_keyword("normal text", ["security_query"]) is False
    assert _contains_single_keyword("security_query", "security") is False
    assert _contains_single_keyword("logs/security", "logs/security") is True
