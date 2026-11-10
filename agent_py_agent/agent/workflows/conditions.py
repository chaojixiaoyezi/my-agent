# 模块用途: WF-04 工作流步骤条件的唯一求值与校验位置。旧 {step_id,key?,equals,fallback?} 原样走
#   output_value(...) == equals（结果、异常与惰性 fallback 都不变）；带 op 的类型化表达式在保存时严格校验、
#   求值时缺值/类型不符稳定返回 False。正则只接受可证明有界的安全子集，Python re 无法同步限时，故不在运行时赌超时。
from __future__ import annotations

import math
import re

from .models import output_value

try:  # Python 3.11+ 的解析器位置；只读取语法树，不改变匹配行为。
    import re._parser as _sre_parse
    from re._constants import MAXREPEAT as _MAXREPEAT
except ImportError:  # pragma: no cover - 3.10 及更早
    import sre_parse as _sre_parse  # type: ignore[no-redef]
    from sre_constants import MAXREPEAT as _MAXREPEAT  # type: ignore[no-redef]

_COMPARISONS = frozenset({"eq", "neq", "gt", "gte", "lt", "lte", "in", "not_in", "regex"})
_PRESENCE = frozenset({"exists", "not_exists"})
_GROUPS = frozenset({"and", "or"})
# 有界上限：嵌套深度、节点总数、组成员数、列表成员数、正则长度/重复次数/可变重复个数、被匹配文本长度。
_MAX_DEPTH, _MAX_NODES, _MAX_GROUP, _MAX_LIST = 8, 64, 32, 100
_MAX_PATTERN_CHARS, _MAX_REPEAT_COUNT, _MAX_VARIABLE_REPEATS, _MAX_SUBJECT_CHARS = 256, 1000, 2, 4096
_REJECTED_REGEX_NODES = frozenset({"GROUPREF", "GROUPREF_EXISTS", "ASSERT", "ASSERT_NOT", "ATOMIC_GROUP",
                                   "POSSESSIVE_REPEAT"})
_REPEAT_NODES = frozenset({"MAX_REPEAT", "MIN_REPEAT"})


# 类用途: 条件结构不合法时的结构化错误（保存/预览时拒绝），error_code 固定为 WORKFLOW_CONDITION_INVALID。
class WorkflowConditionError(ValueError):
    error_code = "WORKFLOW_CONDITION_INVALID"

    def __init__(self, reason: str):
        super().__init__(f"WORKFLOW_CONDITION_INVALID: {reason}")
        self.code = self.error_code
        self.details = {"reason": reason}


# 函数用途: 只有带 op 的对象才是类型化表达式；其余（含旧 equals 形状与非对象值）一律走原判据。
def _is_legacy(condition: object) -> bool:
    return not (isinstance(condition, dict) and "op" in condition)


# LLM: 只校验类型化表达式；旧形状保持原保存行为（不新增校验，避免改变历史定义的可读性与原异常）。
# 函数用途: 在写入或预览前拒绝非法的类型化条件，校验正则安全子集与全部上限。
def validate_condition(condition: object) -> None:
    if _is_legacy(condition):
        return
    counter = [0]
    _validate_node(condition, depth=1, counter=counter)


def _validate_node(node: object, *, depth: int, counter: list[int]) -> None:
    if depth > _MAX_DEPTH:
        raise WorkflowConditionError("depth_exceeded")
    counter[0] += 1
    if counter[0] > _MAX_NODES:
        raise WorkflowConditionError("too_many_nodes")
    if not isinstance(node, dict) or type(node.get("op")) is not str:
        raise WorkflowConditionError("node_requires_op")
    op = node["op"]
    if op in _GROUPS:
        members = node.get("conditions")
        if set(node) != {"op", "conditions"} or not isinstance(members, list) or not 1 <= len(members) <= _MAX_GROUP:
            raise WorkflowConditionError("group_requires_conditions")
        for member in members:
            _validate_node(member, depth=depth + 1, counter=counter)
        return
    if op not in _COMPARISONS | _PRESENCE:
        raise WorkflowConditionError("unknown_op")
    allowed = {"op", "ref"} if op in _PRESENCE else {"op", "ref", "value"}
    if set(node) != allowed:
        raise WorkflowConditionError("unexpected_fields")
    _validate_ref(node["ref"], depth=0)
    if op in _PRESENCE:
        return
    value = node["value"]
    if op in ("in", "not_in"):
        if not isinstance(value, list) or len(value) > _MAX_LIST or not all(_is_scalar(item) for item in value):
            raise WorkflowConditionError("membership_requires_scalar_list")
    elif op == "regex":
        _validate_pattern(value)
    elif not _is_scalar(value):
        raise WorkflowConditionError("comparison_requires_scalar")


def _validate_ref(ref: object, *, depth: int) -> None:
    if depth > _MAX_DEPTH or not isinstance(ref, dict) or not set(ref) <= {"step_id", "key", "fallback"}:
        raise WorkflowConditionError("invalid_ref")
    if type(ref.get("step_id")) is not str or not ref["step_id"]:
        raise WorkflowConditionError("invalid_ref")
    if "key" in ref and type(ref["key"]) not in (str, int):
        raise WorkflowConditionError("invalid_ref")
    if "fallback" in ref:
        _validate_ref(ref["fallback"], depth=depth + 1)


def _is_scalar(value: object) -> bool:
    return value is None or type(value) in (str, bool, int) or (type(value) is float and math.isfinite(value))


# LLM: 只接受可证明有界的正则：无反向引用/环视/原子组/占有量词，重复体内不能再含可变重复或分支，
#   可变重复至多两个且计数有上限；解析异常（含溢出的重复计数）统一转成结构化错误。
# 函数用途: 校验并编译类型化条件的正则模式。
def _validate_pattern(pattern: object) -> None:
    if type(pattern) is not str or not pattern or len(pattern) > _MAX_PATTERN_CHARS:
        raise WorkflowConditionError("regex_pattern_invalid")
    try:
        parsed = _sre_parse.parse(pattern)
        re.compile(pattern)
    except (re.error, OverflowError, RecursionError, ValueError, TypeError):
        raise WorkflowConditionError("regex_pattern_invalid") from None
    variable = [0]
    _check_regex_items(parsed, inside_repeat=False, variable=variable)


def _check_regex_items(items, *, inside_repeat: bool, variable: list[int]) -> None:
    for op, argument in items:
        name = str(op)
        if name in _REJECTED_REGEX_NODES:
            raise WorkflowConditionError("regex_construct_unsupported")
        if name in _REPEAT_NODES:
            low, high, body = argument
            if low > _MAX_REPEAT_COUNT or (high != _MAXREPEAT and high > _MAX_REPEAT_COUNT):
                raise WorkflowConditionError("regex_repeat_too_large")
            if high == _MAXREPEAT or high > low:
                if inside_repeat:
                    raise WorkflowConditionError("regex_nested_repeat")
                variable[0] += 1
                if variable[0] > _MAX_VARIABLE_REPEATS:
                    raise WorkflowConditionError("regex_too_many_repeats")
                _check_regex_items(body, inside_repeat=True, variable=variable)
            else:
                _check_regex_items(body, inside_repeat=inside_repeat, variable=variable)
        elif name == "BRANCH":
            if inside_repeat:
                raise WorkflowConditionError("regex_branch_in_repeat")
            for branch in argument[1]:
                _check_regex_items(branch, inside_repeat=inside_repeat, variable=variable)
        elif name == "SUBPATTERN":
            _check_regex_items(argument[-1], inside_repeat=inside_repeat, variable=variable)


# LLM: 旧形状逐字保留原判据与原异常；类型化表达式先校验再求值，求值本身不因缺值或类型不符抛错。
# 函数用途: 判断一个步骤条件当前是否成立。
def evaluate_condition(condition: object, outputs: dict, inputs: dict | None = None) -> bool:
    if _is_legacy(condition):
        return output_value(condition, outputs, inputs) == condition["equals"]
    validate_condition(condition)
    return _evaluate(condition, outputs, inputs)


def _evaluate(node: dict, outputs: dict, inputs: dict | None) -> bool:
    op = node["op"]
    if op == "and":
        return all(_evaluate(member, outputs, inputs) for member in node["conditions"])
    if op == "or":
        return any(_evaluate(member, outputs, inputs) for member in node["conditions"])
    present, value = _resolve(node["ref"], outputs, inputs)
    if op in _PRESENCE:
        return present if op == "exists" else not present
    if not present:
        # 缺值不满足任何比较（含 neq/not_in）；判断缺失请用 not_exists。
        return False
    expected = node["value"]
    if op == "eq":
        return _same(value, expected)
    if op == "neq":
        return not _same(value, expected)
    if op == "in":
        return any(_same(value, item) for item in expected)
    if op == "not_in":
        return not any(_same(value, item) for item in expected)
    if op == "regex":
        return type(value) is str and re.search(expected, value[:_MAX_SUBJECT_CHARS]) is not None
    return _ordered(op, value, expected)


# 函数用途: 按引用取值；缺键、越界或容器类型不符都视为缺失，null 也视为缺失。
def _resolve(ref: dict, outputs: dict, inputs: dict | None) -> tuple[bool, object]:
    try:
        value = output_value(ref, outputs, inputs)
    except (KeyError, IndexError, TypeError, ValueError):
        return False, None
    return value is not None, value


def _number(value: object) -> bool:
    # bool 不是数字；只对 float 判有限，避免把超大 int 转成 float 时溢出。
    return type(value) is int or (type(value) is float and math.isfinite(value))


def _same(left: object, right: object) -> bool:
    if type(left) is bool or type(right) is bool:
        return type(left) is type(right) and left == right
    if _number(left) and _number(right):
        return left == right
    return type(left) is type(right) and left == right


def _ordered(op: str, left: object, right: object) -> bool:
    if not ((_number(left) and _number(right)) or (type(left) is str and type(right) is str)):
        return False
    return {"gt": left > right, "gte": left >= right, "lt": left < right, "lte": left <= right}[op]


__all__ = ["WorkflowConditionError", "evaluate_condition", "validate_condition"]
