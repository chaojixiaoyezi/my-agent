"""tokens 模块测试。

测试 token 统计、预算控制、账本管理核心函数。
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestTokenBudgetResult:
    """测试 TokenBudgetResult 数据类。"""

    def test_token_budget_result_fields(self):
        """测试返回结果包含所有必要字段。"""
        from agent_py_agent.agent.memory_archive.tokens import TokenBudgetResult

        result = TokenBudgetResult(
            status="ok",
            current_tokens=100,
            max_tokens=1000,
            ratio=0.1,
            archive_level=3,
            message="正常",
        )

        assert result.status == "ok"
        assert result.current_tokens == 100
        assert result.max_tokens == 1000
        assert result.ratio == 0.1
        assert result.archive_level == 3
        assert result.message == "正常"


class TestCheckTokenBudgetLevels:
    """测试不同 archive_level 的预算检查行为。"""

    def test_level_0_thresholds(self):
        """测试 level 0 的阈值（warning 60%, block 85%）。"""
        from agent_py_agent.agent.memory_archive.tokens import check_token_budget

        result = check_token_budget(current_tokens=600, max_tokens=1000, archive_level=0)
        assert result.status == "warning"

        result = check_token_budget(current_tokens=860, max_tokens=1000, archive_level=0)
        assert result.status == "block"

    def test_level_1_thresholds(self):
        """测试 level 1 的阈值（warning 70%, block 90%）。"""
        from agent_py_agent.agent.memory_archive.tokens import check_token_budget

        result = check_token_budget(current_tokens=700, max_tokens=1000, archive_level=1)
        assert result.status == "warning"

        result = check_token_budget(current_tokens=910, max_tokens=1000, archive_level=1)
        assert result.status == "block"

    def test_level_2_thresholds(self):
        """测试 level 2 的阈值（warning 75%, block 95%）。"""
        from agent_py_agent.agent.memory_archive.tokens import check_token_budget

        result = check_token_budget(current_tokens=760, max_tokens=1000, archive_level=2)
        assert result.status == "warning"

        result = check_token_budget(current_tokens=960, max_tokens=1000, archive_level=2)
        assert result.status == "block"

    def test_level_3_thresholds(self):
        """测试 level 3 的阈值（warning 80%, block 100%）。"""
        from agent_py_agent.agent.memory_archive.tokens import check_token_budget

        result = check_token_budget(current_tokens=810, max_tokens=1000, archive_level=3)
        assert result.status == "warning"

        result = check_token_budget(current_tokens=1000, max_tokens=1000, archive_level=3)
        assert result.status == "block"


class TestEstimateTokensEdge:
    """测试 token 估算的边界情况。"""

    def test_estimate_none(self):
        """测试 None 输入。"""
        from agent_py_agent.agent.memory_archive.tokens import estimate_tokens

        result = estimate_tokens(None)
        assert result >= 1

    def test_estimate_mixed_content(self):
        """测试中英文混合内容。"""
        from agent_py_agent.agent.memory_archive.tokens import estimate_tokens

        result = estimate_tokens("Hello 你好 World 世界")
        assert result >= 1

    def test_estimate_numeric(self):
        """测试数字输入。"""
        from agent_py_agent.agent.memory_archive.tokens import estimate_tokens

        result = estimate_tokens(12345)
        assert result >= 1

    def test_estimate_deeply_nested(self):
        """测试深层嵌套结构。"""
        from agent_py_agent.agent.memory_archive.tokens import estimate_tokens

        data = {"a": {"b": {"c": {"d": "value"}}}}
        result = estimate_tokens(data)
        assert result >= 1


class TestAppendSessionTokenUsage:
    """测试会话 token 用量追加。"""

    def test_new_session(self, tmp_path: Path):
        """测试新会话首次记录。"""
        from agent_py_agent.agent.memory_archive.tokens import (
            TurnTokenUsage,
            append_session_token_usage,
        )

        result = append_session_token_usage(
            root=tmp_path,
            usage=TurnTokenUsage(
                session_id="new_session",
                turn_id="turn_1",
                input_tokens=150,
                output_tokens=300,
                tool_tokens=50,
                created_at="2026-05-03T10:00:00Z",
            ),
        )

        assert result["turn_total"] == 500
        assert result["cumulative_tokens"] == 500
        assert result["turn_count"] == 1

    def test_existing_session(self, tmp_path: Path):
        """测试追加到已有会话。"""
        from agent_py_agent.agent.memory_archive.tokens import (
            TurnTokenUsage,
            append_session_token_usage,
        )

        # 第一次
        append_session_token_usage(
            root=tmp_path,
            usage=TurnTokenUsage(
                session_id="existing",
                turn_id="turn_1",
                input_tokens=100,
                output_tokens=100,
                tool_tokens=0,
                created_at="2026-05-03T10:00:00Z",
            ),
        )

        # 第二次
        result = append_session_token_usage(
            root=tmp_path,
            usage=TurnTokenUsage(
                session_id="existing",
                turn_id="turn_2",
                input_tokens=200,
                output_tokens=200,
                tool_tokens=0,
                created_at="2026-05-03T10:01:00Z",
            ),
        )

        assert result["turn_count"] == 2
        assert result["cumulative_tokens"] == 600

    def test_zero_tokens(self, tmp_path: Path):
        """测试零 token 用量。"""
        from agent_py_agent.agent.memory_archive.tokens import (
            TurnTokenUsage,
            append_session_token_usage,
        )

        result = append_session_token_usage(
            root=tmp_path,
            usage=TurnTokenUsage(
                session_id="zero_test",
                turn_id="turn_1",
                input_tokens=0,
                output_tokens=0,
                tool_tokens=0,
                created_at="2026-05-03T10:00:00Z",
            ),
        )

        assert result["turn_total"] == 0
        assert result["cumulative_tokens"] == 0

    def test_large_token_values(self, tmp_path: Path):
        """测试大数值 token 处理。"""
        from agent_py_agent.agent.memory_archive.tokens import (
            TurnTokenUsage,
            append_session_token_usage,
        )

        result = append_session_token_usage(
            root=tmp_path,
            usage=TurnTokenUsage(
                session_id="large",
                turn_id="turn_1",
                input_tokens=1000000,
                output_tokens=2000000,
                tool_tokens=500000,
                created_at="2026-05-03T10:00:00Z",
            ),
        )

        assert result["turn_total"] == 3500000


class TestPayloadEstimation:
    """通过 estimate_tokens 验证不同输入类型，不绑定内部编码方式。"""

    def test_string_payload(self):
        """测试字符串输入直接返回。"""
        from agent_py_agent.agent.memory_archive.tokens import estimate_tokens

        text = "hello world"
        result = estimate_tokens(text)
        assert result >= 1

    def test_dict_payload(self):
        """测试字典转 JSON。"""
        from agent_py_agent.agent.memory_archive.tokens import estimate_tokens

        data = {"key": "value"}
        result = estimate_tokens(data)
        assert result >= 1

    def test_list_payload(self):
        """测试列表转 JSON。"""
        from agent_py_agent.agent.memory_archive.tokens import estimate_tokens

        data = [1, 2, 3]
        result = estimate_tokens(data)
        assert result >= 1


class TestStructuredOverhead:
    """测试结构化数据开销计算。"""

    def test_dict_overhead(self):
        """测试字典开销（字段数/2）。"""
        from agent_py_agent.agent.memory_archive.tokens import estimate_tokens

        data = {"a": 1, "b": 2, "c": 3, "d": 4}
        result = estimate_tokens(data)
        # 应该有额外的结构化开销
        assert result >= 1

    def test_list_overhead(self):
        """测试列表开销（元素数/4）。"""
        from agent_py_agent.agent.memory_archive.tokens import estimate_tokens

        data = [1, 2, 3, 4, 5, 6, 7, 8]
        result = estimate_tokens(data)
        assert result >= 1

    def test_tuple_overhead(self):
        """测试元组开销。"""
        from agent_py_agent.agent.memory_archive.tokens import estimate_tokens

        data = (1, 2, 3, 4)
        result = estimate_tokens(data)
        assert result >= 1

    def test_set_overhead(self):
        """测试集合开销。"""
        from agent_py_agent.agent.memory_archive.tokens import estimate_tokens

        data = {"a", "b", "c"}
        result = estimate_tokens(data)
        assert result >= 1

    def test_string_no_overhead(self):
        """测试字符串无结构化开销。"""
        from agent_py_agent.agent.memory_archive.tokens import estimate_tokens

        text = "plain text"
        result = estimate_tokens(text)
        assert result >= 1


# LLM: 保留原完整JSON及UTF8顺序作为独立数值基准，不调用产品内部长度或结构上界帮助函数。
# 函数用途: 对照编码优化前的预算口径及异常优先级，只在隔离测试内计算，不写账或发请求。
def _legacy_token_estimate(payload: object) -> int:
    try:
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        text = str(payload)
    overhead = max(1, len(payload) // 2) if isinstance(payload, dict) else max(1, len(payload) // 4) if isinstance(payload, (list, tuple, set)) else 0
    return max(1, math.ceil(len(text.encode("utf-8")) / 3), math.ceil(len(text) / 3)) + overhead if text else 1


@pytest.mark.parametrize("payload", [
    "", "中文🪴\\\"\n", None, True, 123, float("nan"), float("inf"),
    {"z": [1, "甲", {"b": "🪴"}], "a": False}, ("a", "b"), {1, 2},
    {"mixed": 1, 2: "key"}, {None: "null key"}, Path("example/file.txt"),
    {"a": "\ud800", "b": {1: "x", "str": "y"}},
])
def test_estimator_preserves_original_numeric_contract(payload):
    from agent_py_agent.agent.memory_archive.tokens import estimate_tokens

    assert estimate_tokens(payload) == _legacy_token_estimate(payload)


def test_streamed_estimator_circular_fallback_and_utf8_error_stay_distinct():
    from agent_py_agent.agent.memory_archive.tokens import estimate_tokens

    circular = []
    circular.append(circular)
    assert estimate_tokens(circular) == 4  # 旧str回退"[[...]]"：7字节/3向上取整+1结构开销。
    for payload in ("\ud800", {"value": "\ud800"}, ["\ud800"]):
        with pytest.raises(UnicodeEncodeError):
            estimate_tokens(payload)


def test_small_json_uses_direct_encoding_and_large_payload_stays_streamed(monkeypatch):
    from agent_py_agent.agent.memory_archive import tokens

    small = {"history": ["正文🪴\n" * 100], "counts": (None, True, -1000, 1.25)}
    large = {"history": ["x" * (tokens._SMALL_JSON_MAX_BYTES + 1)]}
    expected = [_legacy_token_estimate(payload) for payload in (small, large)]
    original = json.dumps
    calls = []

    def bounded_dumps(payload, **kwargs):
        assert payload is small, "大来源不能恢复整份JSON副本"
        calls.append(payload)
        return original(payload, **kwargs)

    monkeypatch.setattr(json, "dumps", bounded_dumps)
    assert [tokens.estimate_tokens(payload) for payload in (small, large)] == expected
    assert calls == [small]


@pytest.mark.parametrize("payload", [
    {"value": "\x00\x01\t\n\r\\\"中🪴"},
    {1: -10**70, 2: [None, False, -5e-324, 1.7976931348623157e308]},
    {None: (float("nan"), float("inf"))},
    {False: "布尔键"},
    {-1.25: "浮点键"},
])
def test_small_json_upper_bound_covers_encoded_utf8(payload):
    from agent_py_agent.agent.memory_archive import tokens

    size = tokens._bounded_json_size(payload, tokens._SMALL_JSON_MAX_BYTES)
    assert size is not None
    assert len(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")) <= size
    assert tokens._bounded_json_size(payload, size) == size
    assert tokens._bounded_json_size(payload, size - 1) is None


@pytest.mark.parametrize("shape", ["circular", "deep", "unknown", "subclass"])
def test_unproven_shapes_keep_original_streaming_semantics(monkeypatch, shape):
    from agent_py_agent.agent.memory_archive import tokens

    class CustomList(list):
        pass

    if shape == "circular":
        payload = []
        payload.append(payload)
    elif shape == "deep":
        payload = "leaf"
        for _ in range(tokens._SMALL_JSON_MAX_DEPTH + 1):
            payload = [payload]
    else:
        payload = Path("example/file.txt") if shape == "unknown" else CustomList([1, "中文"])
    expected = _legacy_token_estimate(payload)

    def reject_direct_encoding(*_args, **_kwargs):
        pytest.fail("未证明有界的结构必须保留流式编码")

    monkeypatch.setattr(json, "dumps", reject_direct_encoding)
    assert tokens.estimate_tokens(payload) == expected


def test_size_probe_does_not_invoke_custom_conversion_or_type_comparison():
    from agent_py_agent.agent.memory_archive import tokens

    calls = []

    class CustomType(type):
        def __eq__(cls, other):
            pytest.fail("上界检查只能按类型身份判断，不能执行自定义比较")

    class CustomValue(metaclass=CustomType):
        def __str__(self):
            calls.append(True)
            return "完整内容"

    payload = {"value": CustomValue()}
    assert tokens._bounded_json_size(payload, tokens._SMALL_JSON_MAX_BYTES) is None
    assert not calls
    actual = tokens.estimate_tokens(payload)
    assert calls == [True]
    assert actual == _legacy_token_estimate(payload)
    assert calls == [True, True]


@pytest.mark.parametrize("large", [False, True])
@pytest.mark.parametrize("json_failure", [False, True])
def test_json_failure_still_precedes_utf8_failure_in_both_encoding_paths(large, json_failure):
    from agent_py_agent.agent.memory_archive import tokens

    payload = {"a": "\ud800"}
    if json_failure:
        payload["b"] = {1: "integer", "key": "string"}
    if large:
        payload["padding"] = "x" * (tokens._SMALL_JSON_MAX_BYTES + 1)
    if json_failure:
        assert tokens.estimate_tokens(payload) == _legacy_token_estimate(payload)
    else:
        with pytest.raises(UnicodeEncodeError):
            tokens.estimate_tokens(payload)
