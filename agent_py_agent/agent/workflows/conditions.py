# 模块用途: WF-04 工作流步骤条件的唯一求值与校验位置。旧 {step_id,key?,equals,fallback?} 原样走
#   output_value(...) == equals（结果、异常与惰性 fallback 都不变）；带 op 的类型化表达式在保存时严格校验、
#   求值时缺值/类型不符稳定返回 False。正则只借用 re 的解析器校验语法，匹配由本模块的 Thompson NFA 子集模拟完成，
#   工作量上界为 被匹配文本长度(≤4096) × 程序状态数(≤256)，不使用回溯引擎，因此与模式结构无关。
from __future__ import annotations

import functools
import math
import re

from .models import output_value

try:  # Python 3.11+ 的解析器/编译器位置；只读语法树，并把单个字符原子编译成逐字符判定（与 re 语义一致）。
    import re._compiler as _sre_compile
    import re._parser as _sre_parse
    from re._constants import MAXREPEAT as _MAXREPEAT
except ImportError:  # pragma: no cover - 3.10 及更早
    import sre_compile as _sre_compile  # type: ignore[no-redef]
    import sre_parse as _sre_parse  # type: ignore[no-redef]
    from sre_constants import MAXREPEAT as _MAXREPEAT  # type: ignore[no-redef]

_COMPARISONS = frozenset({"eq", "neq", "gt", "gte", "lt", "lte", "in", "not_in", "regex"})
_PRESENCE = frozenset({"exists", "not_exists"})
_GROUPS = frozenset({"and", "or"})
# 有界上限：嵌套深度、节点总数、组成员数、列表成员数、正则长度/重复次数/程序状态数、被匹配文本长度、整数位数。
_MAX_DEPTH, _MAX_NODES, _MAX_GROUP, _MAX_LIST = 8, 64, 32, 100
_MAX_PATTERN_CHARS, _MAX_REPEAT_COUNT, _MAX_PROGRAM_STATES, _MAX_SUBJECT_CHARS = 256, 1000, 256, 4096
# 类型化整数的支持范围：绝对值 < 10**1000。超出者在保存时拒绝；运行时读到的超范围整数按类型不符处理。
_MAX_INT_DIGIT_COUNT = 1000
_INT_LIMIT = 10 ** _MAX_INT_DIGIT_COUNT
# 正则只接受正则语言子集；以下结构不是有限自动机可表达的或语义依赖位置以外的上下文，一律拒绝。
_CHAR_ATOMS = frozenset({"LITERAL", "NOT_LITERAL", "ANY", "IN"})
_REPEAT_NODES = frozenset({"MAX_REPEAT", "MIN_REPEAT"})
_ANCHORS = {"AT_BEGINNING": 1, "AT_BEGINNING_STRING": 1, "AT_END": 2, "AT_END_STRING": 4}


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
        if not isinstance(value, list) or len(value) > _MAX_LIST:
            raise WorkflowConditionError("membership_requires_scalar_list")
        _reject_out_of_range(value)
        if not all(_is_scalar(item) for item in value):
            raise WorkflowConditionError("membership_requires_scalar_list")
    elif op == "regex":
        _validate_pattern(value)
    else:
        _reject_out_of_range([value])
        if not _is_scalar(value):
            raise WorkflowConditionError("comparison_requires_scalar")


def _reject_out_of_range(values: list) -> None:
    if any(type(item) is int and not _int_in_range(item) for item in values):
        raise WorkflowConditionError("integer_out_of_range")


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
    return (value is None or type(value) in (str, bool) or (type(value) is int and _int_in_range(value))
            or (type(value) is float and math.isfinite(value)))


def _int_in_range(value: int) -> bool:
    return -_INT_LIMIT < value < _INT_LIMIT


# LLM: re 只负责语法与解析树；不在求值时调用回溯引擎。程序在状态预算内构造，超出即结构化拒绝。
# 函数用途: 校验并编译类型化条件的正则模式。
def _validate_pattern(pattern: object) -> None:
    if type(pattern) is not str or not pattern or len(pattern) > _MAX_PATTERN_CHARS:
        raise WorkflowConditionError("regex_pattern_invalid")
    _program(pattern)


@functools.lru_cache(maxsize=128)
def _program(pattern: str) -> _Program:
    try:
        parsed = _sre_parse.parse(pattern)
        re.compile(pattern)
    except (re.error, OverflowError, RecursionError, ValueError, TypeError):
        raise WorkflowConditionError("regex_pattern_invalid") from None
    if parsed.state.flags != re.UNICODE:
        # 行内标志（(?i)/(?m)/(?s)/(?x)/(?a) 等）改变匹配语义，不在支持子集内。
        raise WorkflowConditionError("regex_construct_unsupported")
    builder = _Program(parsed.state)
    fragment = builder.sequence(parsed.data)
    builder.patch(fragment[1], builder.add("match"))
    builder.start = fragment[0]
    return builder


# 类用途: 正则子集的 Thompson NFA 程序；状态 i 的种类为 char/split/jump/assert/match。
class _Program:
    def __init__(self, state):
        self.state = state
        self.kinds: list[str] = []
        self.out1: list[int] = []
        self.out2: list[int] = []
        self.atom: list[int] = []
        self.anchor: list[int] = []
        self.atoms: list[tuple] = []
        self.atom_keys: dict[str, int] = {}
        self.atom_masks: list[int] = []
        self.matchers: list = []
        self.start = 0
        self.match_bit = 0
        self._closures: dict[tuple[int, int], int] = {}

    def add(self, kind: str, *, atom: int = -1, anchor: int = 0) -> int:
        if len(self.kinds) >= _MAX_PROGRAM_STATES:
            raise WorkflowConditionError("regex_program_too_large")
        index = len(self.kinds)
        self.kinds.append(kind)
        self.out1.append(-1)
        self.out2.append(-1)
        self.atom.append(atom)
        self.anchor.append(anchor)
        if kind == "match":
            self.match_bit = 1 << index
        elif kind == "char":
            self.atom_masks[atom] |= 1 << index
        return index

    def patch(self, holes, target: int) -> None:
        for index, slot in holes:
            (self.out1 if slot == 1 else self.out2)[index] = target

    def sequence(self, items):
        fragment = None
        for op, argument in items:
            piece = self.node(str(op), op, argument)
            if fragment is None:
                fragment = piece
            else:
                self.patch(fragment[1], piece[0])
                fragment = (fragment[0], piece[1])
        if fragment is None:
            index = self.add("jump")
            fragment = (index, [(index, 1)])
        return fragment

    def node(self, name, op, argument):
        if name in _CHAR_ATOMS:
            key = repr((name, argument))
            if key not in self.atom_keys:
                self.atom_keys[key] = len(self.atoms)
                self.atoms.append((op, argument))
                self.atom_masks.append(0)
                self.matchers.append(None)
            index = self.add("char", atom=self.atom_keys[key])
            return index, [(index, 1)]
        if name == "AT" and str(argument) in _ANCHORS:
            index = self.add("assert", anchor=_ANCHORS[str(argument)])
            return index, [(index, 1)]
        if name == "SUBPATTERN":
            _group, add_flags, del_flags, body = argument
            if add_flags or del_flags:
                raise WorkflowConditionError("regex_construct_unsupported")
            return self.sequence(body)
        if name == "BRANCH":
            branches = [self.sequence(branch) for branch in argument[1]]
            start, holes = branches[-1][0], list(branches[-1][1])
            for branch in reversed(branches[:-1]):
                split = self.add("split")
                self.out1[split], self.out2[split] = branch[0], start
                start = split
                holes.extend(branch[1])
            return start, holes
        if name in _REPEAT_NODES:
            low, high, body = argument
            if low > _MAX_REPEAT_COUNT or (high != _MAXREPEAT and high > _MAX_REPEAT_COUNT):
                raise WorkflowConditionError("regex_repeat_too_large")
            return self.repeat(low, high, body)
        raise WorkflowConditionError("regex_construct_unsupported")

    def repeat(self, low, high, body):
        fragment = None
        pieces = []
        for _ in range(low):
            pieces.append(self.sequence(body))
        if high == _MAXREPEAT:
            if pieces:
                # x{n,} = x{n-1} 后接 x+：复用最后一份主体回环，不额外复制。
                last = pieces[-1]
                loop = self.add("split")
                self.patch(last[1], loop)
                self.out1[loop] = last[0]
                pieces[-1] = (last[0], [(loop, 2)])
            else:
                inner = self.sequence(body)
                loop = self.add("split")
                self.out1[loop] = inner[0]
                self.patch(inner[1], loop)
                pieces.append((loop, [(loop, 2)]))
        else:
            for _ in range(high - low):
                inner = self.sequence(body)
                optional = self.add("split")
                self.out1[optional] = inner[0]
                pieces.append((optional, inner[1] + [(optional, 2)]))
        for piece in pieces:
            if fragment is None:
                fragment = piece
            else:
                self.patch(fragment[1], piece[0])
                fragment = (fragment[0], piece[1])
        if fragment is None:
            index = self.add("jump")
            fragment = (index, [(index, 1)])
        return fragment

    # 函数用途: 某状态在给定位置上下文（开头/$/文本末）下的 ε 闭包，结果只含 char 与 match 状态。
    def closure(self, index: int, context: int) -> int:
        key = (index, context)
        cached = self._closures.get(key)
        if cached is not None:
            return cached
        mask, seen, stack = 0, set(), [index]
        while stack:
            current = stack.pop()
            if current < 0 or current in seen:
                continue
            seen.add(current)
            kind = self.kinds[current]
            if kind in ("char", "match"):
                mask |= 1 << current
            elif kind == "split":
                stack.extend((self.out1[current], self.out2[current]))
            elif kind == "jump" or (kind == "assert" and context & self.anchor[current]):
                stack.append(self.out1[current])
        self._closures[key] = mask
        return mask

    def accepting(self, character: str) -> int:
        mask = 0
        for atom, states in enumerate(self.atom_masks):
            matcher = self.matchers[atom]
            if matcher is None:
                # 单个原子用 re 自身编译成逐字符判定，Unicode 类别与 . 的换行语义与 re 完全一致。
                matcher = _sre_compile.compile(_sre_parse.SubPattern(self.state, [self.atoms[atom]]), 0).fullmatch
                self.matchers[atom] = matcher
            if matcher(character) is not None:
                mask |= states
        return mask

    # LLM: 与 re.search(...) is not None 同义（只判是否存在匹配，贪婪/惰性不影响结果）；
    #   每个字符只做一次集合推进，工作量 ≤ 文本长度 × 状态数。
    # 函数用途: 在已截断的文本中判断模式是否出现。
    def search(self, subject: str) -> bool:
        size = len(subject)

        def context(position: int) -> int:
            flags = 1 if position == 0 else 0
            if position == size or (position == size - 1 and subject[position] == "\n"):
                flags |= 2
            if position == size:
                flags |= 4
            return flags

        current = self.closure(self.start, context(0))
        accepting: dict[str, int] = {}
        for position, character in enumerate(subject):
            if current & self.match_bit:
                return True
            if character not in accepting:
                accepting[character] = self.accepting(character)
            moving = current & accepting[character]
            following = context(position + 1)
            current = self.closure(self.start, following)
            while moving:
                lowest = moving & -moving
                current |= self.closure(self.out1[lowest.bit_length() - 1], following)
                moving ^= lowest
        return bool(current & self.match_bit)


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
        return type(value) is str and _program(expected).search(value[:_MAX_SUBJECT_CHARS])
    return _ordered(op, value, expected)


# 函数用途: 按引用取值；缺键、越界或容器类型不符都视为缺失，null 也视为缺失。
def _resolve(ref: dict, outputs: dict, inputs: dict | None) -> tuple[bool, object]:
    try:
        value = output_value(ref, outputs, inputs)
    except (KeyError, IndexError, TypeError, ValueError):
        return False, None
    return value is not None, value


def _number(value: object) -> bool:
    # bool 不是数字；int 须在支持范围内，float 须有限（避免把超大 int 转成 float 时溢出）。
    return (type(value) is int and _int_in_range(value)) or (type(value) is float and math.isfinite(value))


def _same(left: object, right: object) -> bool:
    if any(type(side) is int and not _int_in_range(side) for side in (left, right)):
        # 超出支持范围的整数按类型不符处理：不等于任何值。
        return False
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
