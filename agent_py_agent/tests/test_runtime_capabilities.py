"""测试 runtime_capabilities 模块。

函数/模块用途: 验证运行时能力只从显式 capability/tool 机器标识读取，不再从“安全日志”等自然语言关键词猜业务能力。
"""

from __future__ import annotations

from agent_py_agent.agent.agent_core.runtime_capabilities import (
    SECURITY_RUNTIME_CAPABILITY,
    _contains_keyword,
    _contains_single_keyword,
    _looks_like_security_log_task,
    _normalize_capabilities,
    resolve_runtime_capabilities,
)


# LLM: explicit capability markers are the only runtime security auto-grant source.
# 函数用途: 用户或模型写出 logs/security、log_analysis 或 security_query 等机器标识时才自动打开安全日志能力。
def test_explicit_security_marker_adds_capability():
    result = resolve_runtime_capabilities("runtime_capabilities: logs/security")
    assert SECURITY_RUNTIME_CAPABILITY in result


# LLM: natural-language security phrases must not grant runtime capabilities.
# 函数用途: 普通“分析安全日志/查看防火墙日志”不能让 Python 代码层靠词表猜出 logs/security。
def test_natural_security_log_phrases_do_not_add_capability():
    assert resolve_runtime_capabilities("analyze firewall logs") == []
    assert resolve_runtime_capabilities("查看安全日志和审计日志") == []
    assert _looks_like_security_log_task("分析安全日志") is False


# LLM: injected protocol markers are accepted because callers already made an explicit routing decision.
# 函数用途: inject/granted_capabilities 里的机器能力标识继续生效，方便配置、外部网关和 LLM 结构化路由使用。
def test_inject_and_granted_capabilities_preserve_explicit_protocol():
    injected = resolve_runtime_capabilities("analyze these events", inject=["logs/security"])
    granted = resolve_runtime_capabilities(
        "plain request",
        granted_capabilities=["custom_skill", "logs/security"],
    )
    assert SECURITY_RUNTIME_CAPABILITY in injected
    assert granted == ["custom_skill", "logs/security"]


# LLM: duplicate granted capabilities remain normalized without changing caller order.
# 函数用途: 已授权能力大小写去重，避免工具表里重复出现同一 capability。
def test_granted_security_capability_not_duplicated():
    result = resolve_runtime_capabilities(
        "run security_query",
        granted_capabilities=["logs/security"],
    )
    assert result.count("logs/security") == 1


# LLM: capability normalization keeps existing public helper behavior.
# 函数用途: None、空列表、空白和大小写重复项仍按旧契约规整。
def test_normalize_capabilities_contract():
    assert _normalize_capabilities(None) == []
    assert _normalize_capabilities([]) == []
    assert _normalize_capabilities(["  skill1  ", " skill2"]) == ["skill1", "skill2"]
    assert _normalize_capabilities(["Skill", "SKILL", "skill"]) == ["Skill"]
    assert _normalize_capabilities([123, "skill"]) == ["123", "skill"]


# LLM: keyword helpers remain syntax-level utilities for exact machine markers.
# 函数用途: 边界匹配函数仍可复用，但产品业务能力不再喂自然语言词表给它。
def test_keyword_helpers_match_machine_tokens_only():
    assert _contains_keyword("", ["security_query"]) is False
    assert _contains_keyword("run security_query", ["security_query"]) is True
    assert _contains_keyword("normal text", ["security_query"]) is False
    assert _contains_single_keyword("security_query", "security") is False
    assert _contains_single_keyword("logs/security", "logs/security") is True
