"""tokens 模块测试。

测试 token 统计、预算控制、账本管理核心函数。
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


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
        # 合成路径逐元素物化；记录必须在任何断言前，否则断言失败会被合成层回退捕获吞掉。
        calls.append(payload)
        return original(payload, **kwargs)

    monkeypatch.setattr(json, "dumps", bounded_dumps)
    assert [tokens.estimate_tokens(payload) for payload in (small, large)] == expected
    assert calls, "小来源仍应走有界直接编码"
    for call in calls:
        assert call is not large, "大来源不能恢复整份JSON副本"
        assert tokens._bounded_json_size(call, tokens._SMALL_JSON_MAX_BYTES) is not None, (
            "不能物化未证明有界的来源"
        )


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
    calls = []

    def reject_direct_encoding(*args, **_kwargs):
        # 记录必须在抛出前：合成层会把异常吞成回退，末尾的 calls 断言才是可靠守卫。
        calls.append(args[0])
        raise TypeError("未证明有界的结构必须保留流式编码")

    monkeypatch.setattr(json, "dumps", reject_direct_encoding)
    assert tokens.estimate_tokens(payload) == expected
    assert calls == [], "未证明有界的结构不能被合成路径物化编码"


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


# LLM: estcache 契约：合成缓存必须与逐段编码逐位一致（冷/热都相等）、命中后不再编码、内容变化
#   必须失效、上限按 LRU 淘汰；随机结构用固定种子保证可复现。清缓存保证用例间确定性。
class TestComposedLengthCache:
    """缓存版估算与原口径的等价性与缓存行为。"""

    @pytest.fixture(autouse=True)
    def _isolate_length_cache(self):
        from agent_py_agent.agent.memory_archive import tokens

        tokens._ITEM_LENGTH_CACHE.clear()
        yield
        tokens._ITEM_LENGTH_CACHE.clear()

    @staticmethod
    def _legacy(payload):
        from agent_py_agent.agent.memory_archive import tokens

        return tokens._tokens_from_lengths(*tokens._payload_lengths(payload), payload)

    def _assert_matches_legacy(self, payload):
        from agent_py_agent.agent.memory_archive.tokens import estimate_tokens

        assert estimate_tokens(payload) == self._legacy(payload)

    def test_mixed_message_shapes_match_legacy_cold_and_warm(self):
        body = "工具结果内容与中文说明" * 60
        messages = [
            {"role": "user", "content": "第一问"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "read_file", "arguments": '{"path": "a.py"}'}},
            ]},
            {"role": "tool", "tool_call_id": "call_1", "content": body},
            {"role": "assistant", "content": "🪴非BMP🧪" * 40},
            {"role": "user", "content": [
                {"type": "text", "text": "看这张图"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ]},
            {"role": "tool", "tool_call_id": "call_9", "content": "x" * 600_000},
        ]
        payload = {
            "system_instruction": "系统提示" * 100,
            "prompt_adjunct": "提示附件🪴" * 50,
            "messages": messages,
            "tools": [{"type": "function", "function": {"name": "read_file"}}],
        }
        for shape in (messages, payload, [], {}, (1, "a", None)):
            self._assert_matches_legacy(shape)  # 冷
            self._assert_matches_legacy(shape)  # 热（命中缓存）

    def test_random_structures_match_legacy(self):
        import random

        rng = random.Random(20261005)

        def rand_value(depth):
            if depth > 3:
                return rng.choice([rng.randint(-10**6, 10**6), rng.random(), "字" * rng.randint(0, 30),
                                   True, False, None, "🪴"])
            kind = rng.randint(0, 7)
            if kind <= 2:
                return rng.choice([rng.randint(-10**6, 10**6), rng.random(), "文" * rng.randint(0, 50),
                                   True, None, float(rng.randint(-5, 5)), "🪴🌱"])
            if kind == 3:
                return [rand_value(depth + 1) for _ in range(rng.randint(0, 5))]
            if kind <= 5:
                return {f"k{rng.randint(0, 9)}": rand_value(depth + 1) for _ in range(rng.randint(0, 5))}
            return (rand_value(depth + 1), rand_value(depth + 1))

        for _ in range(200):
            self._assert_matches_legacy(rand_value(0))

    def test_repeated_estimate_hits_cache_without_reencoding(self, monkeypatch):
        from agent_py_agent.agent.memory_archive import tokens

        messages = [
            {"role": "tool", "tool_call_id": f"call-{index}", "content": "结果" * 60}
            for index in range(50)
        ]
        calls = []
        original = tokens._encode_element_lengths

        def counting_encode(item):
            calls.append(1)
            return original(item)

        monkeypatch.setattr(tokens, "_encode_element_lengths", counting_encode)
        first = tokens.estimate_tokens(messages)
        assert calls, "冷缓存必须实际编码"
        calls.clear()
        assert tokens.estimate_tokens(messages) == first
        assert calls == [], "同对象重复估算必须全部命中、零编码"
        fresh = [dict(message) for message in messages]
        calls.clear()
        assert tokens.estimate_tokens(fresh) == first
        assert calls == [], "内容相同的新对象也必须命中（指纹按内容）"

    def test_mutated_message_invalidates_cached_lengths(self):
        from agent_py_agent.agent.memory_archive.tokens import estimate_tokens

        message = {"role": "tool", "content": "原始内容" * 20}
        messages = [message]
        first = estimate_tokens(messages)
        message["content"] = "修改后的内容明显更长" * 20
        second = estimate_tokens(messages)
        assert second != first
        assert second == self._legacy(messages)

    def test_cache_eviction_is_bounded_and_lru(self, monkeypatch):
        from agent_py_agent.agent.memory_archive import tokens

        monkeypatch.setattr(tokens, "_ITEM_LENGTH_CACHE_MAX_ENTRY_COUNT", 2)
        first = {"v": "A" * 50}
        second = {"v": "B" * 50}
        third = {"v": "C" * 50}
        tokens.estimate_tokens([first, second])
        tokens.estimate_tokens([first])  # 访问 A，刷新 LRU
        tokens.estimate_tokens([third])  # 插入 C，应淘汰最久未用的 B
        assert len(tokens._ITEM_LENGTH_CACHE) <= 2
        key_first = tokens._item_length_fingerprint(first)
        key_second = tokens._item_length_fingerprint(second)
        assert key_first in tokens._ITEM_LENGTH_CACHE
        assert key_second not in tokens._ITEM_LENGTH_CACHE
        # 淘汰后重算仍与旧口径一致
        assert tokens.estimate_tokens([first, second, third]) == self._legacy([first, second, third])
