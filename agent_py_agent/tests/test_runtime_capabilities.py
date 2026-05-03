"""测试 runtime_capabilities 模块

测试运行时能力推断功能：
- resolve_runtime_capabilities: 判断用户提示是否包含安全日志意图
- _normalize_capabilities: 能力列表规范化
- _looks_like_security_log_task: 检测安全日志任务
- _contains_keyword / _contains_single_keyword: 关键词匹配
"""
from __future__ import annotations

import pytest

from agent_py_agent.agent.agent_core.runtime_capabilities import (
    SECURITY_RUNTIME_CAPABILITY,
    resolve_runtime_capabilities,
    _looks_like_security_log_task,
    _normalize_capabilities,
    _contains_keyword,
    _contains_single_keyword,
)


# ============================================================
# 测试用例：SECURITY_RUNTIME_CAPABILITY 常量
# ============================================================

class TestSecurityCapabilityConstant:
    """测试安全运行时能力常量"""

    def test_security_runtime_capability_value(self):
        """测试常量值为 'logs/security'"""
        assert SECURITY_RUNTIME_CAPABILITY == "logs/security"

    def test_security_runtime_capability_not_empty(self):
        """测试常量非空"""
        assert len(SECURITY_RUNTIME_CAPABILITY) > 0


# ============================================================
# 测试用例：resolve_runtime_capabilities
# ============================================================

class TestResolveRuntimeCapabilities:
    """测试 resolve_runtime_capabilities 函数"""

    def test_empty_prompt_returns_normalized_empty(self):
        """测试空提示返回空列表"""
        result = resolve_runtime_capabilities("")
        assert result == []

    def test_whitespace_only_returns_empty(self):
        """测试只包含空白字符返回空列表"""
        result = resolve_runtime_capabilities("   \n\t  ")
        assert result == []

    def test_explicit_security_marker_adds_capability(self):
        """测试显式安全标记添加安全能力"""
        result = resolve_runtime_capabilities("run security_query for me")
        assert SECURITY_RUNTIME_CAPABILITY in result

    def test_security_log_phrase_adds_capability(self):
        """测试安全日志短语添加安全能力"""
        result = resolve_runtime_capabilities("analyze firewall logs")
        assert SECURITY_RUNTIME_CAPABILITY in result

    def test_cjk_security_phrase_adds_capability(self):
        """测试中文安全日志短语添加安全能力"""
        result = resolve_runtime_capabilities("查看安全日志和审计日志")
        assert SECURITY_RUNTIME_CAPABILITY in result

    def test_inject_adds_capability(self):
        """测试 inject 参数提供安全意图"""
        result = resolve_runtime_capabilities(
            "analyze these events",
            inject=["security event analysis"]
        )
        assert SECURITY_RUNTIME_CAPABILITY in result

    def test_granted_capabilities_preserved(self):
        """测试已授权的能力被保留"""
        result = resolve_runtime_capabilities(
            "analyze firewall logs",
            granted_capabilities=["custom_skill", "logs/security"]
        )
        assert "custom_skill" in result
        assert "logs/security" in result

    def test_granted_security_capability_not_duplicated(self):
        """测试已授权安全能力不重复添加"""
        result = resolve_runtime_capabilities(
            "run some security_query",
            granted_capabilities=["logs/security"]
        )
        # 不应该有重复
        assert result.count("logs/security") == 1

    def test_log_token_alone_not_enough(self):
        """测试只有 log token 不足以触发安全能力"""
        result = resolve_runtime_capabilities("show me the log file")
        assert SECURITY_RUNTIME_CAPABILITY not in result

    def test_case_insensitive_matching(self):
        """测试大小写不敏感匹配"""
        result = resolve_runtime_capabilities("SECURITY LOGS for Audit")
        assert SECURITY_RUNTIME_CAPABILITY in result

    def test_normalize_removes_duplicates(self):
        """测试规范化去除重复"""
        result = resolve_runtime_capabilities(
            "analyze logs",
            granted_capabilities=["skill", "SKILL", "Skill"]
        )
        # 应该只有一个
        lower_keys = [c.lower() for c in result]
        assert len(lower_keys) == len(set(lower_keys))


# ============================================================
# 测试用例：_normalize_capabilities
# ============================================================

class TestNormalizeCapabilities:
    """测试能力规范化函数"""

    def test_none_input_returns_empty_list(self):
        """测试 None 输入返回空列表"""
        result = _normalize_capabilities(None)
        assert result == []

    def test_empty_iterable_returns_empty_list(self):
        """测试空可迭代对象返回空列表"""
        result = _normalize_capabilities([])
        assert result == []

    def test_string_items_preserved(self):
        """测试字符串项被保留"""
        result = _normalize_capabilities(["skill1", "skill2"])
        assert "skill1" in result
        assert "skill2" in result

    def test_whitespace_stripped(self):
        """测试空白被去除"""
        result = _normalize_capabilities(["  skill1  ", " skill2"])
        assert "skill1" in result
        assert "skill2" in result

    def test_case_insensitive_dedup(self):
        """测试大小写不敏感去重"""
        result = _normalize_capabilities(["Skill", "SKILL", "skill"])
        assert len(result) == 1

    def test_preserves_original_case(self):
        """测试保留原始大小写（第一个出现的形式）"""
        result = _normalize_capabilities(["Skill", "SKILL", "skill"])
        assert result[0] == "Skill"

    def test_numeric_items_converted_to_string(self):
        """测试数字项被转换为字符串"""
        result = _normalize_capabilities([123, "skill"])
        assert "123" in result
        assert "skill" in result


# ============================================================
# 测试用例：_looks_like_security_log_task
# ============================================================

class TestLooksLikeSecurityLogTask:
    """测试安全日志任务检测"""

    def test_empty_string_returns_false(self):
        """测试空字符串返回 False"""
        assert _looks_like_security_log_task("") is False

    def test_whitespace_only_returns_false(self):
        """测试仅空白字符返回 False"""
        assert _looks_like_security_log_task("   \n\t") is False

    def test_plain_log_without_security_returns_false(self):
        """测试普通 log 无 security 返回 False"""
        assert _looks_like_security_log_task("show me the application log") is False

    def test_security_log_phrase_returns_true(self):
        """测试安全日志短语返回 True"""
        assert _looks_like_security_log_task("analyze security logs") is True

    def test_firewall_logs_returns_true(self):
        """测试防火墙日志返回 True"""
        assert _looks_like_security_log_task("check firewall logs") is True

    def test_audit_logs_returns_true(self):
        """测试审计日志返回 True"""
        assert _looks_like_security_log_task("review audit logs") is True

    def test_waf_logs_returns_true(self):
        """测试 WAF 日志返回 True"""
        assert _looks_like_security_log_task("parse waf logs") is True

    def test_cjk_security_log_returns_true(self):
        """测试中文安全日志短语返回 True"""
        assert _looks_like_security_log_task("分析安全日志") is True

    def test_cjk_audit_log_returns_true(self):
        """测试中文审计日志短语返回 True"""
        assert _looks_like_security_log_task("查看审计日志") is True

    def test_explicit_tool_name_returns_true(self):
        """测试显式工具名返回 True"""
        assert _looks_like_security_log_task("run security_query") is True

    def test_mixed_english_chinese_returns_true(self):
        """测试中英文混合返回 True"""
        assert _looks_like_security_log_task("check 安全日志 and firewall logs") is True


# ============================================================
# 测试用例：_contains_keyword / _contains_single_keyword
# ============================================================

class TestKeywordMatching:
    """测试关键词匹配函数"""

    def test_contains_keyword_empty_text(self):
        """测试空文本返回 False"""
        assert _contains_keyword("", ["security"]) is False

    def test_contains_keyword_empty_keywords(self):
        """测试空关键词列表返回 False"""
        assert _contains_keyword("some text", []) is False

    def test_contains_keyword_found(self):
        """测试关键词被找到"""
        assert _contains_keyword("security event", ["security"]) is True

    def test_contains_keyword_not_found(self):
        """测试关键词未找到"""
        assert _contains_keyword("normal text", ["security"]) is False

    def test_contains_keyword_partial_match(self):
        """测试部分匹配不触发（单词边界精确匹配）"""
        assert _contains_keyword("multiple attacks detected", ["attacks"]) is True
        assert _contains_keyword("multiple attacks detected", ["attack"]) is False  # attack 是 attacks 的一部分

    def test_single_keyword_word_boundary(self):
        """测试单词边界匹配"""
        assert _contains_single_keyword("a security attack happened", "attack") is True
        assert _contains_single_keyword("attacker IP", "attack") is False

    def test_single_keyword_regex_chars(self):
        """测试包含正则字符的关键词"""
        # 包含特殊正则字符如 .
        assert _contains_single_keyword("file.txt content", ".") is True

    def test_single_keyword_case_sensitive(self):
        """测试单关键词大小写敏感"""
        assert _contains_single_keyword("SECURITY", "security") is False

    def test_contains_keyword_multiple_matches(self):
        """测试多关键词 OR 关系"""
        text = "found suspicious activity"
        assert _contains_keyword(text, ["malicious", "suspicious"]) is True

    def test_contains_keyword_allows_word_boundary_chars(self):
        """测试单词边界字符"""
        # 下划线分隔不匹配（_ 是单词字符，无边界）
        assert _contains_single_keyword("security_logs", "security") is False
        # 空格分隔匹配
        assert _contains_single_keyword("security logs", "security") is True


# ============================================================
# 测试用例：边界场景
# ============================================================

class TestBoundaryCases:
    """测试边界场景"""

    def test_very_long_prompt(self):
        """测试超长提示"""
        long_text = "analyze " * 1000 + "security logs"
        result = resolve_runtime_capabilities(long_text)
        assert isinstance(result, list)

    def test_unicode_characters(self):
        """测试 Unicode 字符"""
        result = resolve_runtime_capabilities("分析日志 🔒 安全事件")
        assert isinstance(result, list)

    def test_special_characters_in_prompt(self):
        """测试提示中的特殊字符"""
        result = resolve_runtime_capabilities("check <script> injection logs")
        assert isinstance(result, list)

    def test_newlines_and_tabs(self):
        """测试换行和制表符"""
        result = resolve_runtime_capabilities("analyze\n\tsecurity\n\tlogs")
        assert SECURITY_RUNTIME_CAPABILITY in result

    def test_none_in_granted_capabilities(self):
        """测试 granted_capabilities 包含 None"""
        result = resolve_runtime_capabilities(
            "check firewall logs",
            granted_capabilities=[None, "skill", None]
        )
        assert "skill" in result