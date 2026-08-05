from __future__ import annotations

"""LLM: 提供工具输入的有限 JSON Schema 归一化与校验，不承担权限、审批或业务事实判断。

模块用途: 把模型工具参数安全地转换成 Schema 声明的 Python 原生类型，并在任何工具副作用
发生前检查必填、类型、枚举、范围、嵌套结构和额外字段。
"""

import json
import math
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

_MAX_SCHEMA_DEPTH = 32
_MAX_ISSUES = 32
_MAX_COERCIONS = 64
_MAX_SCALAR_COERCION_CHARS = 1_024
_MAX_CONTAINER_COERCION_CHARS = 1_000_000
_INTEGER_TEXT_RE = re.compile(r"-?(?:0|[1-9][0-9]*)\Z")
_NUMBER_TEXT_RE = re.compile(
    r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?\Z"
)
_NO_COERCION = object()


# LLM: issue 只保存结构化路径和约束事实；展示文案由上层 gate 根据这些字段生成。
# 类用途: 表示一个参数违反了哪条 Schema 规则，便于模型精确修正而不是重猜整次调用。
@dataclass(frozen=True)
class ToolInputIssue:
    keyword: str
    path: str
    expected: Any = None
    actual_type: str = ""

    # LLM: 输出必须保持 JSON 可序列化，不能塞入原始参数值或敏感正文。
    # 函数用途: 把校验问题转换成可写入 gate evidence 的安全字典。
    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "keyword": self.keyword,
            "path": self.path,
        }
        if self.expected is not None:
            payload["expected"] = self.expected
        if self.actual_type:
            payload["actual_type"] = self.actual_type
        return payload


# LLM: coercion 只记录类型变化，不记录输入值，防止参数正文或凭据进入审计投影。
# 类用途: 记录一次确定性的类型纠正，例如字符串整数转成真正整数。
@dataclass(frozen=True)
class ToolInputCoercion:
    path: str
    source_type: str
    target_type: str

    # LLM: 该投影会进入工具结果 envelope；字段名属于稳定机器合同。
    # 函数用途: 输出一条可审计但不泄露具体参数值的类型纠正记录。
    def to_dict(self) -> dict[str, str]:
        return {
            "path": self.path,
            "source_type": self.source_type,
            "target_type": self.target_type,
        }


# LLM: normalization result 保留新值和全部有界纠正记录；调用方不得原地修改原始 payload。
# 类用途: 返回安全类型纠正后的参数，以及本轮做过哪些类型变化。
@dataclass(frozen=True)
class ToolInputNormalization:
    value: Any
    coercions: tuple[ToolInputCoercion, ...] = ()


# LLM: validation result 只表达结构合法性；业务存在性、owner 权限和副作用仍由后续硬门负责。
# 类用途: 汇总有限 JSON Schema 校验结果，并提供稳定的上层错误分类。
@dataclass(frozen=True)
class ToolInputValidation:
    issues: tuple[ToolInputIssue, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.issues

    @property
    def primary_error_code(self) -> str:
        keywords = {item.keyword for item in self.issues}
        if "required" in keywords:
            return "TOOL_PARAMETER_REQUIRED"
        if "type" in keywords:
            return "TOOL_PARAMETER_TYPE_INVALID"
        return "TOOL_INVALID_ARGUMENTS" if self.issues else ""

    @property
    def fields(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.path for item in self.issues))


# LLM: 只做 Schema 明确要求且可逆/无歧义的类型纠正；不猜字段、不补默认值、不把标量包成数组。
# 函数用途: 把字符串形式的整数、布尔、JSON 数组/对象等安全地转成声明类型。
def normalize_tool_input(value: Any, schema: dict[str, Any]) -> ToolInputNormalization:
    coercions: list[ToolInputCoercion] = []
    normalized = _normalize_node(
        value,
        schema,
        root_schema=schema,
        path="$",
        depth=0,
        coercions=coercions,
    )
    return ToolInputNormalization(normalized, tuple(coercions[:_MAX_COERCIONS]))


# LLM: 校验器实现项目实际使用的有限 JSON Schema 子集；未知 annotation 不获得机器权威。
# 函数用途: 检查工具参数的结构、必填、类型、枚举、范围、长度、数组和组合规则。
def validate_tool_input(value: Any, schema: dict[str, Any]) -> ToolInputValidation:
    issues: list[ToolInputIssue] = []
    _validate_node(
        value,
        schema,
        root_schema=schema,
        path="$",
        depth=0,
        issues=issues,
    )
    return ToolInputValidation(tuple(issues[:_MAX_ISSUES]))


# LLM: normalization 先解析本地 $ref/组合分支，再按明确类型递归；达到深度上限就保持原值。
# 函数用途: 实现 normalize_tool_input 的递归工作，同时收集不含原值的纠正记录。
def _normalize_node(
    value: Any,
    schema: Any,
    *,
    root_schema: dict[str, Any],
    path: str,
    depth: int,
    coercions: list[ToolInputCoercion],
) -> Any:
    if depth > _MAX_SCHEMA_DEPTH or not isinstance(schema, dict):
        return value
    resolved = _resolved_schema(schema, root_schema)
    value = _normalize_composed_value(
        value,
        resolved,
        root_schema=root_schema,
        path=path,
        depth=depth,
        coercions=coercions,
    )
    expected_types = _schema_types(resolved)
    value = _coerce_scalar(value, expected_types, resolved, path, coercions)

    if isinstance(value, list):
        items = resolved.get("items")
        if isinstance(items, dict):
            return [
                _normalize_node(
                    item,
                    items,
                    root_schema=root_schema,
                    path=f"{path}[{index}]",
                    depth=depth + 1,
                    coercions=coercions,
                )
                for index, item in enumerate(value)
            ]
    if isinstance(value, dict):
        properties = resolved.get("properties")
        if isinstance(properties, dict):
            normalized = dict(value)
            for key, child_schema in properties.items():
                if key not in value or not isinstance(child_schema, dict):
                    continue
                normalized[key] = _normalize_node(
                    value[key],
                    child_schema,
                    root_schema=root_schema,
                    path=_child_path(path, str(key)),
                    depth=depth + 1,
                    coercions=coercions,
                )
            return normalized
    return value


# LLM: anyOf/oneOf 只选择经完整校验可成立的分支；allOf 按顺序叠加，不凭顺序猜失败分支。
# 函数用途: 在联合 Schema 中找到能安全解释当前值的结构，并递归纠正其内部字段。
def _normalize_composed_value(
    value: Any,
    schema: dict[str, Any],
    *,
    root_schema: dict[str, Any],
    path: str,
    depth: int,
    coercions: list[ToolInputCoercion],
) -> Any:
    current = value
    all_of = schema.get("allOf")
    if isinstance(all_of, list):
        for branch in all_of:
            current = _normalize_node(
                current,
                branch,
                root_schema=root_schema,
                path=path,
                depth=depth + 1,
                coercions=coercions,
            )
    for keyword in ("oneOf", "anyOf"):
        branches = schema.get(keyword)
        if not isinstance(branches, list):
            continue
        for branch in branches:
            local_coercions: list[ToolInputCoercion] = []
            candidate = _normalize_node(
                current,
                branch,
                root_schema=root_schema,
                path=path,
                depth=depth + 1,
                coercions=local_coercions,
            )
            if validate_tool_input(candidate, _schema_with_root_defs(branch, root_schema)).ok:
                coercions.extend(local_coercions)
                return candidate
    return current


# LLM: 只允许 exact integer/number、true/false、显式 null、合法 JSON container 的确定性转换。
# 函数用途: 对一个字符串值尝试无歧义的 Schema 引导类型转换。
def _coerce_scalar(
    value: Any,
    expected_types: tuple[str, ...],
    schema: dict[str, Any],
    path: str,
    coercions: list[ToolInputCoercion],
) -> Any:
    if not isinstance(value, str) or "string" in expected_types:
        return value
    text = value.strip()
    target = _coercion_target(text, expected_types)
    if target is _NO_COERCION:
        return value
    if not _matches_declared_type(target, schema):
        return value
    if len(coercions) < _MAX_COERCIONS:
        coercions.append(
            ToolInputCoercion(
                path=path,
                source_type="string",
                target_type=_json_type(target),
            )
        )
    return target


# LLM: 类型联合里排除 null 后只有一个明确目标才转标量；数组/对象还必须有对应 JSON 起始符。
# 函数用途: 为字符串计算无歧义目标值，无法安全决定时返回内部 sentinel。
def _coercion_target(text: str, expected_types: tuple[str, ...]) -> Any:
    concrete = set(expected_types) - {"null"}
    # LLM: 容器有独立的大参数上限，必须先于普通标量的 1K 上限判断；否则合法但较长的
    # 原生工具 JSON 数组/对象会永远到不了 _json_container，和公开 Schema 产生假冲突。
    if concrete == {"array"} and text.startswith("["):
        return _json_container(text, list, _NO_COERCION)
    if concrete == {"object"} and text.startswith("{"):
        return _json_container(text, dict, _NO_COERCION)
    if len(text) > _MAX_SCALAR_COERCION_CHARS:
        return _NO_COERCION
    lowered = text.lower()
    if "null" in expected_types and lowered == "null":
        return None
    if concrete == {"integer"} and _INTEGER_TEXT_RE.fullmatch(text):
        try:
            return int(text)
        except (OverflowError, ValueError):
            return _NO_COERCION
    if concrete == {"number"} and _NUMBER_TEXT_RE.fullmatch(text):
        parsed = float(text)
        if math.isfinite(parsed):
            return int(parsed) if parsed.is_integer() else parsed
    if concrete == {"boolean"} and lowered in {"true", "false"}:
        return lowered == "true"
    return _NO_COERCION


# LLM: JSON container 转换必须同时满足语法和目标 Python 类型，失败时原样返回。
# 函数用途: 安全解析模型错误包成字符串的数组或对象。
def _json_container(text: str, expected: type, fallback: Any) -> Any:
    if len(text) > _MAX_CONTAINER_COERCION_CHARS:
        return fallback
    try:
        parsed = json.loads(text)
    except (RecursionError, TypeError, ValueError):
        return fallback
    return parsed if isinstance(parsed, expected) else fallback


# LLM: validation 有界收集问题，不因恶意深层 payload 或外部 Schema 产生无限递归。
# 函数用途: 递归执行有限 JSON Schema 规则并写入结构化问题列表。
def _validate_node(
    value: Any,
    schema: Any,
    *,
    root_schema: dict[str, Any],
    path: str,
    depth: int,
    issues: list[ToolInputIssue],
) -> None:
    if len(issues) >= _MAX_ISSUES:
        return
    if depth > _MAX_SCHEMA_DEPTH:
        issues.append(ToolInputIssue("maxDepth", path, _MAX_SCHEMA_DEPTH, _json_type(value)))
        return
    if not isinstance(schema, dict):
        return
    resolved = _resolved_schema(schema, root_schema)
    if not _validate_compositions(
        value,
        resolved,
        root_schema=root_schema,
        path=path,
        depth=depth,
        issues=issues,
    ):
        return
    expected_types = _schema_types(resolved)
    if expected_types and not _value_matches_types(value, expected_types):
        issues.append(
            ToolInputIssue(
                "type",
                path,
                list(expected_types) if len(expected_types) > 1 else expected_types[0],
                _json_type(value),
            )
        )
        return
    if "enum" in resolved and not any(_json_equal(value, item) for item in resolved.get("enum") or []):
        issues.append(ToolInputIssue("enum", path, resolved.get("enum"), _json_type(value)))
    if "const" in resolved and not _json_equal(value, resolved.get("const")):
        issues.append(ToolInputIssue("const", path, resolved.get("const"), _json_type(value)))
    if isinstance(value, dict):
        _validate_object(value, resolved, root_schema, path, depth, issues)
    elif isinstance(value, list):
        _validate_array(value, resolved, root_schema, path, depth, issues)
    elif isinstance(value, str):
        _validate_string(value, resolved, path, issues)
    elif _is_number(value):
        _validate_number(value, resolved, path, issues)


# LLM: oneOf 要求恰好一支、anyOf 至少一支、allOf 每支都通过；只返回组合级问题避免分支噪声。
# 函数用途: 校验联合或叠加结构，并告诉递归主流程是否应继续检查当前节点。
def _validate_compositions(
    value: Any,
    schema: dict[str, Any],
    *,
    root_schema: dict[str, Any],
    path: str,
    depth: int,
    issues: list[ToolInputIssue],
) -> bool:
    for keyword in ("anyOf", "oneOf"):
        branches = schema.get(keyword)
        if not isinstance(branches, list) or not branches:
            continue
        matches = sum(
            1
            for branch in branches
            if _branch_is_valid(value, branch, root_schema, path, depth + 1)
        )
        valid = matches >= 1 if keyword == "anyOf" else matches == 1
        if not valid:
            issues.append(ToolInputIssue(keyword, path, keyword, _json_type(value)))
            return False
    all_of = schema.get("allOf")
    if isinstance(all_of, list):
        for branch in all_of:
            _validate_node(
                value,
                branch,
                root_schema=root_schema,
                path=path,
                depth=depth + 1,
                issues=issues,
            )
    return True


# LLM: 分支探测使用独立有界问题列表，不能把未选分支的错误泄露到主结果。
# 函数用途: 判断一个联合 Schema 分支能否完整接受当前值。
def _branch_is_valid(
    value: Any,
    branch: Any,
    root_schema: dict[str, Any],
    path: str,
    depth: int,
) -> bool:
    branch_issues: list[ToolInputIssue] = []
    _validate_node(
        value,
        branch,
        root_schema=root_schema,
        path=path,
        depth=depth,
        issues=branch_issues,
    )
    return not branch_issues


# LLM: 对象未知字段遵守 additionalProperties；false 为封闭，Schema 值则递归校验扩展字段。
# 函数用途: 检查对象必填项、已声明字段和额外字段规则。
def _validate_object(
    value: dict[Any, Any],
    schema: dict[str, Any],
    root_schema: dict[str, Any],
    path: str,
    depth: int,
    issues: list[ToolInputIssue],
) -> None:
    properties = schema.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    required = schema.get("required")
    if isinstance(required, list):
        for raw_name in required:
            name = str(raw_name)
            if name not in value:
                issues.append(ToolInputIssue("required", _child_path(path, name), True, "missing"))
    additional = schema.get("additionalProperties", True)
    for raw_key, child in value.items():
        key = str(raw_key)
        child_schema = properties.get(key)
        child_path = _child_path(path, key)
        if isinstance(child_schema, dict):
            _validate_node(
                child,
                child_schema,
                root_schema=root_schema,
                path=child_path,
                depth=depth + 1,
                issues=issues,
            )
        elif additional is False:
            issues.append(ToolInputIssue("additionalProperties", child_path, False, _json_type(child)))
        elif isinstance(additional, dict):
            _validate_node(
                child,
                additional,
                root_schema=root_schema,
                path=child_path,
                depth=depth + 1,
                issues=issues,
            )
    _length_issue(len(value), schema, path, issues, "minProperties", "maxProperties", "object")


# LLM: 数组规则递归检查每个 items，并独立检查条目数量；不自动丢弃或截断元素。
# 函数用途: 检查数组长度和每一项的嵌套类型/约束。
def _validate_array(
    value: list[Any],
    schema: dict[str, Any],
    root_schema: dict[str, Any],
    path: str,
    depth: int,
    issues: list[ToolInputIssue],
) -> None:
    _length_issue(len(value), schema, path, issues, "minItems", "maxItems", "array")
    items = schema.get("items")
    if not isinstance(items, dict):
        return
    for index, child in enumerate(value):
        _validate_node(
            child,
            items,
            root_schema=root_schema,
            path=f"{path}[{index}]",
            depth=depth + 1,
            issues=issues,
        )


# LLM: pattern 使用 JSON Schema 的搜索语义；Schema 自身正则错误按 pattern 约束失败处理。
# 函数用途: 检查字符串长度和声明的正则格式。
def _validate_string(
    value: str,
    schema: dict[str, Any],
    path: str,
    issues: list[ToolInputIssue],
) -> None:
    _length_issue(len(value), schema, path, issues, "minLength", "maxLength", "string")
    pattern = schema.get("pattern")
    if not isinstance(pattern, str):
        return
    try:
        matched = re.search(pattern, value) is not None
    except re.error:
        matched = False
    if not matched:
        issues.append(ToolInputIssue("pattern", path, pattern, "string"))


# LLM: Python bool 不得作为 number；NaN/Infinity 不属于合法 JSON 数字。
# 函数用途: 检查数字上下界、排他上下界和倍数约束。
def _validate_number(
    value: int | float,
    schema: dict[str, Any],
    path: str,
    issues: list[ToolInputIssue],
) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        issues.append(ToolInputIssue("type", path, "finite number", _json_type(value)))
        return
    comparisons = (
        ("minimum", lambda actual, limit: actual >= limit),
        ("maximum", lambda actual, limit: actual <= limit),
        ("exclusiveMinimum", lambda actual, limit: actual > limit),
        ("exclusiveMaximum", lambda actual, limit: actual < limit),
    )
    for keyword, check in comparisons:
        limit = schema.get(keyword)
        if _is_number(limit) and not check(value, limit):
            issues.append(ToolInputIssue(keyword, path, limit, _json_type(value)))
    multiple = schema.get("multipleOf")
    if _is_number(multiple) and multiple > 0 and not _is_multiple_of(value, multiple):
        issues.append(ToolInputIssue("multipleOf", path, multiple, _json_type(value)))


# LLM: multipleOf 用十进制定点文本计算，避免大整数转 float 溢出或 0.3/0.1 浮点误判。
# 函数用途: 安全判断一个有限 JSON 数字是否是声明步长的整数倍。
def _is_multiple_of(value: int | float, multiple: int | float) -> bool:
    if isinstance(value, int) and isinstance(multiple, int):
        return value % multiple == 0
    try:
        return Decimal(str(value)) % Decimal(str(multiple)) == 0
    except (InvalidOperation, OverflowError, ValueError):
        return False


# LLM: 长度边界只接受非布尔整数；非法 Schema annotation 不改变运行时输入。
# 函数用途: 复用字符串、数组和对象的最小/最大数量检查。
def _length_issue(
    length: int,
    schema: dict[str, Any],
    path: str,
    issues: list[ToolInputIssue],
    minimum_key: str,
    maximum_key: str,
    actual_type: str,
) -> None:
    minimum = schema.get(minimum_key)
    maximum = schema.get(maximum_key)
    if isinstance(minimum, int) and not isinstance(minimum, bool) and length < minimum:
        issues.append(ToolInputIssue(minimum_key, path, minimum, actual_type))
    if isinstance(maximum, int) and not isinstance(maximum, bool) and length > maximum:
        issues.append(ToolInputIssue(maximum_key, path, maximum, actual_type))


# LLM: 只解析当前根内的 JSON Pointer $ref，外部 URL ref 不会触发网络或文件读取。
# 函数用途: 展开本地 $defs/definitions 引用，同时保留引用节点上的额外约束。
def _resolved_schema(schema: dict[str, Any], root_schema: dict[str, Any]) -> dict[str, Any]:
    current = schema
    sibling_layers: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _ in range(_MAX_SCHEMA_DEPTH + 1):
        raw_ref = current.get("$ref")
        if not isinstance(raw_ref, str):
            resolved = dict(current)
            for siblings in reversed(sibling_layers):
                resolved.update(siblings)
            return resolved
        if raw_ref in seen:
            return {"type": "__invalid_schema_reference__"}
        seen.add(raw_ref)
        target = _local_reference_target(root_schema, raw_ref)
        if target is None:
            return {"type": "__invalid_schema_reference__"}
        sibling_layers.append(
            {key: item for key, item in current.items() if key != "$ref"}
        )
        current = target
    return {"type": "__invalid_schema_reference__"}


# LLM: 运行时只展开当前根内的 JSON Pointer，非法或非对象目标由调用者按无效引用 fail-closed。
# 函数用途: 解析一个 #/... 引用目标，不访问任何外部资源。
def _local_reference_target(
    root_schema: dict[str, Any],
    raw_ref: str,
) -> dict[str, Any] | None:
    if not raw_ref.startswith("#/"):
        return None
    current: Any = root_schema
    for raw_part in raw_ref[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current if isinstance(current, dict) else None


# LLM: 分支独立验证仍需访问原根 definitions；复制引用表不会改变分支业务约束。
# 函数用途: 为单独的组合分支补上根 Schema 的定义表。
def _schema_with_root_defs(branch: Any, root_schema: dict[str, Any]) -> dict[str, Any]:
    result = dict(branch) if isinstance(branch, dict) else {}
    for key in ("$defs", "definitions"):
        if key in root_schema and key not in result:
            result[key] = root_schema[key]
    return result


# LLM: nullable 是 provider 兼容 hint；内部权威类型仍归一为明确的 null 联合。
# 函数用途: 返回 Schema 允许的 JSON 原生类型集合。
def _schema_types(schema: dict[str, Any]) -> tuple[str, ...]:
    raw = schema.get("type")
    if isinstance(raw, str):
        types = [raw]
    elif isinstance(raw, list):
        types = [str(item) for item in raw if isinstance(item, str)]
    else:
        types = []
    if schema.get("nullable") is True and "null" not in types:
        types.append("null")
    return tuple(dict.fromkeys(types))


# LLM: 类型匹配只查看声明类型，供纠正后复核；没有 type 的 Schema 保持开放。
# 函数用途: 确认一个纠正结果确实符合目标 Schema 的类型声明。
def _matches_declared_type(value: Any, schema: dict[str, Any]) -> bool:
    expected = _schema_types(schema)
    return not expected or _value_matches_types(value, expected)


# LLM: JSON Schema 的 integer 是 number 子集；bool 仍必须与两者严格分离。
# 函数用途: 判断 Python 值是否符合一个或多个 JSON Schema 类型。
def _value_matches_types(value: Any, expected: tuple[str, ...]) -> bool:
    actual = _json_type(value)
    return actual in expected or (actual == "integer" and "number" in expected)


# LLM: JSON 类型判定必须把 bool 与 integer 分开，保持 JSON Schema 语义。
# 函数用途: 把 Python 原生值映射到稳定 JSON 类型名称。
def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


# LLM: JSON enum 比较要区分 true 与 1，不能沿用 Python 的宽松相等语义。
# 函数用途: 按 JSON 类型和值同时比较枚举或 const。
def _json_equal(left: Any, right: Any) -> bool:
    if _json_type(left) != _json_type(right):
        if {_json_type(left), _json_type(right)} <= {"integer", "number"}:
            return left == right
        return False
    return left == right


# LLM: number helper 排除 bool，防止 true 通过 minimum/maximum 等数值规则。
# 函数用途: 判断一个值是不是有限规则可处理的 JSON 数字。
def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


# LLM: 路径只包含字段位置，不包含字段值；非标识符 key 使用 JSON 字符串下标。
# 函数用途: 为嵌套问题生成稳定且可读的 JSON 路径。
def _child_path(parent: str, key: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        return f"{parent}.{key}"
    return f"{parent}[{json.dumps(key, ensure_ascii=False)}]"


__all__ = [
    "ToolInputCoercion",
    "ToolInputIssue",
    "ToolInputNormalization",
    "ToolInputValidation",
    "normalize_tool_input",
    "validate_tool_input",
]
