# LLM: Conveyor M00 / R3-G0：hot_json.dumps 必须与同参 json.dumps 逐字节一致（NaN/Inf、星际平面字符、孤立代理、
#   排序键、紧凑分隔符、ensure_ascii 开关），不可序列化对象的 TypeError 文本与循环引用 ValueError 也一致；
#   同时钉住“8 个模块级编码器、实例属性全部落地”这一免类型查找的机制，并给 G1 锁等待门禁提供压测用例工厂。
# 模块用途: 固定 agent/common/hot_json.py 的输出等价合同（C1.1），不触网、不读 git 历史。
from __future__ import annotations

import json
import random
from collections import OrderedDict
from decimal import Decimal
from enum import IntEnum

import pytest

from agent_py_agent.agent.common import hot_json

# 随机语料的固定种子：失败时可原样复现同一批输入。
RANDOM_CORPUS_SEED = 20261119
# 随机生成的嵌套文档条数；每条在 8 种参数组合下各比一次，本文件需在两种解释器上 20 秒内跑完。
RANDOM_DOCUMENT_COUNT = 400
# 随机文档的最大嵌套层数。
RANDOM_DOCUMENT_DEPTH = 4

_OPTION_GRID = [
    {"ensure_ascii": ensure_ascii, "sort_keys": sort_keys, "compact": compact}
    for ensure_ascii in (True, False)
    for sort_keys in (False, True)
    for compact in (False, True)
]


# 类用途: IntEnum 值走 int 子类编码分支，验证与标准库同一路径。
class _Level(IntEnum):
    LOW = 1
    HIGH = 2


# 函数用途: 用标准库 json.dumps 按合同参数映射求参考结果。
def _reference(obj: object, *, ensure_ascii: bool, sort_keys: bool, compact: bool) -> str:
    return json.dumps(obj, ensure_ascii=ensure_ascii, sort_keys=sort_keys,
                      separators=(",", ":") if compact else None)


# 函数用途: 返回调用结果或异常的可比较形态（类型 + 文本）。
def _outcome(fn, *args, **kwargs) -> tuple[str, object]:
    try:
        return ("ok", fn(*args, **kwargs))
    except Exception as exc:  # noqa: BLE001 - 差分比较需要捕获任意异常类型
        return ("raise", (type(exc), str(exc)))


# 函数用途: 生成带 Unicode、星际平面、控制字符与孤立代理的随机字符串。
def _random_text(rng: random.Random) -> str:
    pools = ("abcXYZ019 _-", "\"\\/\b\f\n\r\t\x00\x1f\x7f", "中文é漢字ü", "😀𝄞🀄\U0010ffff", "𐏿\u2028\u2029")
    return "".join(rng.choice(rng.choice(pools)) for _ in range(rng.randrange(0, 12)))


# 函数用途: 生成随机嵌套 JSON 文档（含大整数、特殊浮点、混合容器）。
def _random_value(rng: random.Random, depth: int) -> object:
    kind = rng.randrange(9 if depth < RANDOM_DOCUMENT_DEPTH else 6)
    if kind == 0:
        return rng.choice((None, True, False))
    if kind == 1:
        return rng.randrange(-(2 ** 70), 2 ** 70)
    if kind == 2:
        return rng.choice((0.0, -0.0, 1.5, 1e300, 5e-324, float("nan"), float("inf"), float("-inf"),
                           rng.uniform(-1e6, 1e6)))
    if kind in (3, 4, 5):
        return _random_text(rng)
    if kind in (6, 7):
        return {_random_text(rng): _random_value(rng, depth + 1) for _ in range(rng.randrange(0, 5))}
    return [_random_value(rng, depth + 1) for _ in range(rng.randrange(0, 5))]


_FIXED_CORPUS: list[object] = [
    None, True, False, 0, -1, 2 ** 63, -(2 ** 70), 1.5, -0.0, 1e300, 5e-324,
    float("nan"), float("inf"), float("-inf"),
    "", "plain", "quote\" backslash\\ slash/ ctrl\x00\x1f\x7f", "中文", "é", "\u2028\u2029",
    "😀𝄞", "\U0010ffff", "\ud800", "\udfff",
    [], {}, [1, [2, [3, []]]], (1, "tuple"),
    {"b": 1, "a": [1.0, {"z": None, "y": "😀"}]},
    OrderedDict([("z", 1), ("a", 2), ("中", 3)]),
    {1: "int key", 2.5: "float key", None: "none key"},
    {True: "bool key", False: "false key"},
    {"level": _Level.HIGH, "levels": [_Level.LOW]},
    {"nested": {"deep": [{"k": index, "v": [index] * index} for index in range(5)]}},
    # 下列对象在某些组合下抛错：差分同时比较异常类型与文本。
    {1: "int", "a": "str"},
    object(), {1, 2}, b"bytes", Decimal("1.1"), complex(1, 2),
    {"k": object()}, [1, {"x": {2, 3}}], {(1, 2): "tuple key"},
]


@pytest.mark.parametrize("options", _OPTION_GRID, ids=lambda o: "-".join(f"{k}={int(v)}" for k, v in o.items()))
def test_fixed_corpus_is_byte_identical_including_errors(options):
    for obj in _FIXED_CORPUS:
        assert _outcome(hot_json.dumps, obj, **options) == _outcome(_reference, obj, **options), repr(obj)


def test_random_corpus_is_byte_identical():
    rng = random.Random(RANDOM_CORPUS_SEED)
    for _ in range(RANDOM_DOCUMENT_COUNT):
        doc = _random_value(rng, 0)
        for options in _OPTION_GRID:
            assert _outcome(hot_json.dumps, doc, **options) == _outcome(_reference, doc, **options), repr(doc)


def test_defaults_equal_plain_json_dumps_and_type_error_text():
    sample = {"b": [1, 2.5, "中😀"], "a": None}
    assert hot_json.dumps(sample) == json.dumps(sample)
    assert hot_json.dumps(sample, compact=True) == json.dumps(sample, separators=(",", ":"))
    with pytest.raises(TypeError) as ours:
        hot_json.dumps({"when": object()})
    with pytest.raises(TypeError) as theirs:
        json.dumps({"when": object()})
    assert str(ours.value) == str(theirs.value) == "Object of type object is not JSON serializable"


def test_circular_reference_error_matches():
    loop: list[object] = []
    loop.append(loop)
    assert _outcome(hot_json.dumps, loop) == _outcome(json.dumps, loop)
    assert _outcome(hot_json.dumps, loop)[1][0] is ValueError


def test_eight_module_level_encoders_with_every_attribute_set_on_the_instance():
    encoders = hot_json._ENCODERS
    assert len(encoders) == 8
    assert sorted(encoders) == sorted((a, s, c) for a in (False, True) for s in (False, True) for c in (False, True))
    for (ensure_ascii, sort_keys, compact), encoder in encoders.items():
        assert type(encoder) is json.JSONEncoder
        # 默认 json.dumps 的编码器把 default/分隔符留在类上，每次编码都要走类型查找；这里必须全部是实例属性。
        attributes = vars(encoder)
        assert {"default", "item_separator", "key_separator"} <= set(attributes)
        assert attributes["item_separator"] == ("," if compact else ", ")
        assert attributes["key_separator"] == (":" if compact else ": ")
        assert encoder.ensure_ascii is ensure_ascii and encoder.sort_keys is sort_keys
        assert encoder.check_circular is True and encoder.allow_nan is True and encoder.indent is None


# 函数用途: G1 锁等待门禁用例工厂：每个线程一份负载，返回的零参可调用编码一次即一个操作。
def g1_case_hot_json():
    payload = {
        "type": "status",
        "items": [{"id": index, "name": f"item-{index}", "ok": True, "score": index / 3} for index in range(8)],
        "text": "中文😀",
    }
    dumps = hot_json.dumps

    def operation() -> str:
        return dumps(payload)

    return operation
