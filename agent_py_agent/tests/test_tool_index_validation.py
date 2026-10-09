"""toolrefs-2：标准C逐行解码保持晋升旧拒绝口径，不积累无关对象。"""
from __future__ import annotations

import io
import json
import random
import tracemalloc

import pytest

from agent_py_agent.agent.common.tool_index_stream import (
    _check_index_resource_errors,
    _decoded_index_value,
    _universal_index_lines,
    iter_prescreened_tool_index_lines,
    tool_index_prescreen_pattern,
)


def valid_tool_index_object(text: str) -> bool:
    return isinstance(_decoded_index_value(text), dict)

EXAMPLES = [
    '{}', ' {"a": 1} ', '{"a":[1, true, false, null, "x", {}, []]}',
    '{"a":NaN,"b":Infinity,"c":-Infinity}', '{"a":1e+2,"b":-0.12E-3}',
    '{"a":"\\u0000\\u0085\\u2028\\ud800\\/\\\\\\\""}', '{"x":"正文\u0085\u2028\u2029"}',
    '{"a":1,"a":2}', '{"a":{ "nested":[{},[[]]]}}',
    '{"a":}', '{"a":1,}', '{"a":[1,]}', '{"a":[1}}', '{"a":01}',
    '{"a":1.}', '{"a":1e}', '{"a":+1}', '{"a":.1}', '{"a":truex}',
    '{"a":"\\u0XXX"}', '{"a":"\\x41"}', '{"a":"\x00"}',
    '{"a" 1}', '{1:2}', '{"a":1}{"b":2}', '{"a":1}\u0085', '[]', 'null', '1', '"a"', '',
]


def old_valid(text: str) -> bool:
    try:
        return isinstance(json.loads(text), dict)
    except json.JSONDecodeError:
        return False


@pytest.mark.parametrize("text", EXAMPLES)
def test_validator_equals_json_loads_acceptance(text: str) -> None:
    assert valid_tool_index_object(text) == old_valid(text)


def test_generated_valid_and_mutated_records_match_decoder() -> None:
    rng = random.Random(711)
    values = [None, True, False, 0, -1, 1e20, "运行/a", "\\u00x", [1, {"x": [None]}], {"a": "x"}]
    texts = [json.dumps({str(n): rng.choice(values), "padding": "\u0085\u2028"}, ensure_ascii=n % 2 == 0) for n in range(250)]
    for text in texts:
        variants = [text, text[:-1], text + "}", text.replace(':', '', 1), text.replace('}', ',}', 1)]
        assert all(valid_tool_index_object(item) == old_valid(item) for item in variants)


def test_ordinary_validation_uses_c_decoder_once_per_line(monkeypatch) -> None:
    real, calls = json.loads, []

    def decode(text, *args, **kwargs):
        calls.append(text)
        return real(text, *args, **kwargs)

    monkeypatch.setattr(json, "loads", decode)
    assert valid_tool_index_object('{"noise":[true,1,"abc",{}]}')
    assert not valid_tool_index_object('{"noise":[true,1,]}')
    assert len(calls) == 2


def test_deep_valid_input_uses_standard_fallback_without_truncation() -> None:
    text = '{"deep":' + '[' * 140 + '0' + ']' * 140 + '}'
    assert valid_tool_index_object(text) == old_valid(text)


def test_oversized_integer_preserves_standard_value_error() -> None:
    text = '{"integer":' + '1' * 5000 + '}'
    with pytest.raises(ValueError):
        json.loads(text)
    with pytest.raises(ValueError):
        valid_tool_index_object(text)


def test_multi_megabyte_string_validator_memory_is_bounded() -> None:
    text = '{"padding":"' + 'x' * (2 * 1024 * 1024) + '"}'
    tracemalloc.start()
    try:
        assert valid_tool_index_object(text)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    # 07明确允许单行C对象分配，禁止全集积累；两MiB单行按新的300万量级上界核对。
    assert peak < 3 * 1024 * 1024


@pytest.mark.parametrize("text", ["last\r", "\r", "a" * 65535 + "中\r\n尾\u0085\u2028\u2029", "a" * 65535 + "\r\nnext\r尾"])
def test_shared_block_reader_matches_standard_newlines(text: str) -> None:
    payload = text.encode("utf-8")
    with io.TextIOWrapper(io.BytesIO(payload), encoding="utf-8", newline=None) as expected:
        lines = [line.removesuffix("\n") for line in expected]
    assert list(_universal_index_lines(io.BytesIO(payload))) == lines


def test_python310_without_integer_limit_accessor_is_supported(monkeypatch) -> None:
    import sys
    monkeypatch.delattr(sys, "get_int_max_str_digits", raising=False)
    _check_index_resource_errors('{"run_id":"other"}', True)


def test_nonmatching_depth_under_old_threshold_still_preserves_deep_stack_resource_error(tmp_path) -> None:
    import sys
    text = '[' * 100 + '0' + ']' * 100
    path = tmp_path / "index.jsonl"
    path.write_text(text + "\n", encoding="utf-8")
    frame, frames = sys._getframe(), 0
    while frame is not None:
        frames += 1
        frame = frame.f_back
    depth = sys.getrecursionlimit() - frames - 60

    def with_stack(n, call):
        if n:
            return with_stack(n - 1, call)
        return call()

    # CPython3.12的C递归预算独立，不能预设所有版本都在这里抛错；比较各自真实结果。
    def capture(call):
        try:
            with_stack(depth, call)
        except RecursionError:
            return "recursion"
        return "ok"

    assert capture(lambda: json.loads(text)) == capture(lambda: list(iter_prescreened_tool_index_lines(
        path, tool_index_prescreen_pattern(("hit",)), universal_newlines=True)))


def test_original_full_decoder_contract_does_not_depend_on_resource_heuristics(monkeypatch) -> None:
    real, calls = json.loads, []
    text = '[' * 100 + '0' + ']' * 100

    def decode(text, *args, **kwargs):
        calls.append(text)
        return real(text, *args, **kwargs)

    monkeypatch.setattr(json, "loads", decode)
    _check_index_resource_errors(text, True)
    assert calls == [text]
