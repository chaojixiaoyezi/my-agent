from __future__ import annotations

"""LLM: 只在统一工具入口补入明示安全默认值或可信运行事实，不猜字段、不读取自然语言。

模块用途: 为缺失工具参数提供一个可审计的结构化补全点，并记录不含参数值的 source/source_ref。
"""

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from ..contracts.tool_input_schema import validate_tool_input
from .models import ToolSpec, TrustedParameterBinding

_TRUSTED_CONTEXT_ROOTS = frozenset({"registry", "run_scope", "write_boundary"})


# LLM: 来源记录只允许暴露字段路径、来源类别和结构化引用，绝不能复制命令、密钥或参数正文。
# 类用途: 描述一个最终工具参数由模型、工具安全默认值还是宿主可信上下文提供。
@dataclass(frozen=True)
class ToolInputSource:
    path: str
    source: str
    source_ref: str

    def to_dict(self) -> dict[str, str]:
        return {
            "path": self.path,
            "source": self.source,
            "source_ref": self.source_ref,
        }


# LLM: completion context 只能由 Registry 从 typed envelope、write boundary 和 workspace 构造。
# 类用途: 携带本次调用身份与可信事实树，防止模型在普通参数中伪造参数来源。
@dataclass(frozen=True)
class ToolInputCompletionContext:
    call_id: str = ""
    call_source: str = ""
    trusted_context: dict[str, Any] | None = None


# LLM: 补全结果同时返回有效输入与来源账目，调用方必须在 Schema 校验前使用 value。
# 类用途: 将补全后的参数和不含原值的来源记录作为一个不可变结果交回 Registry。
@dataclass(frozen=True)
class ToolInputCompletion:
    value: dict[str, Any]
    sources: tuple[ToolInputSource, ...] = ()


# LLM: 该函数只补“缺失”的顶层字段；显式空值、错误值和未知字段仍交给同一 Schema 门处理。
# 函数用途: 依次应用工具作者声明的安全默认值与可信上下文绑定，并记录每个已有/补入字段的来源。
def complete_tool_input(
    arguments: dict[str, Any],
    spec: ToolSpec,
    context: ToolInputCompletionContext | None = None,
) -> ToolInputCompletion:
    completion_context = context or ToolInputCompletionContext()
    completed = deepcopy(arguments)
    sources = [
        ToolInputSource(
            path=_field_path(name),
            source=_supplied_source(completion_context),
            source_ref=_supplied_source_ref(name, completion_context),
        )
        for name in completed
    ]
    for name, value in (
        getattr(spec, "safe_parameter_defaults", None) or {}
    ).items():
        if name in completed:
            continue
        completed[name] = deepcopy(value)
        sources.append(
            ToolInputSource(
                path=_field_path(name),
                source="safe_default",
                source_ref=f"tool_spec:{spec.name}#/safe_parameter_defaults/{_pointer_part(name)}",
            )
        )
    trusted_context = (
        completion_context.trusted_context
        if isinstance(completion_context.trusted_context, dict)
        else {}
    )
    for name, binding in (
        getattr(spec, "trusted_parameter_bindings", None) or {}
    ).items():
        if name in completed or not _binding_matches(binding, completed):
            continue
        resolved = _first_trusted_value(binding.source_refs, trusted_context)
        if resolved is None:
            continue
        source_ref, value = resolved
        completed[name] = deepcopy(value)
        sources.append(
            ToolInputSource(
                path=_field_path(name),
                source="trusted_context",
                source_ref=source_ref,
            )
        )
    return ToolInputCompletion(completed, tuple(sources))


# LLM: 工具补全声明与参数 Schema 必须在展示和执行前共同验证；错误声明不能降级继续运行。
# 函数用途: 校验补全目标、可信引用、条件字段和默认值类型，并把安全默认值投影为 Schema 注解。
def schema_with_tool_input_completion(
    spec: ToolSpec,
    schema: dict[str, Any],
) -> dict[str, Any]:
    result = deepcopy(schema)
    properties = result.get("properties")
    if not isinstance(properties, dict):
        properties = {}
        result["properties"] = properties
    defaults = getattr(spec, "safe_parameter_defaults", None) or {}
    bindings = getattr(spec, "trusted_parameter_bindings", None) or {}
    if not isinstance(defaults, dict):
        raise ValueError("safe_parameter_defaults 必须是对象")
    if not isinstance(bindings, dict):
        raise ValueError("trusted_parameter_bindings 必须是对象")
    overlap = sorted(set(defaults) & set(bindings), key=str)
    if overlap:
        raise ValueError(
            "同一参数不能同时声明安全默认值和可信上下文绑定: "
            + ", ".join(str(item) for item in overlap)
        )
    for name, value in defaults.items():
        field_name = _require_declared_parameter(
            name,
            properties,
            contract="safe_parameter_defaults",
        )
        _validate_safe_default(field_name, value, result)
        declared_default = properties[field_name].get("default")
        if "default" in properties[field_name] and declared_default != value:
            raise ValueError(
                f"{field_name} 的 Schema default 与 safe_parameter_defaults 冲突"
            )
        properties[field_name]["default"] = deepcopy(value)
    for name, binding in bindings.items():
        field_name = _require_declared_parameter(
            name,
            properties,
            contract="trusted_parameter_bindings",
        )
        if not isinstance(binding, TrustedParameterBinding):
            raise ValueError(f"{field_name} 的 trusted parameter binding 类型无效")
        if not binding.source_refs:
            raise ValueError(
                f"{field_name} 的 trusted parameter binding 缺少 source_refs"
            )
        for source_ref in binding.source_refs:
            _validate_source_ref(source_ref)
        for condition_name, _ in binding.when:
            _require_declared_parameter(
                condition_name,
                properties,
                contract=f"{field_name}.when",
            )
    return result


def _validate_safe_default(
    name: str,
    value: Any,
    schema: dict[str, Any],
) -> None:
    probe_schema: dict[str, Any] = {
        "type": "object",
        "properties": deepcopy(schema.get("properties") or {}),
        "required": [name],
        "additionalProperties": False,
    }
    for definitions_key in ("$defs", "definitions"):
        definitions = schema.get(definitions_key)
        if isinstance(definitions, dict):
            probe_schema[definitions_key] = deepcopy(definitions)
    validation = validate_tool_input({name: value}, probe_schema)
    if validation.ok:
        return
    keywords = ",".join(
        f"{issue.path}:{issue.keyword}" for issue in validation.issues[:4]
    )
    raise ValueError(f"{name} 的安全默认值不符合参数 Schema: {keywords}")


def _require_declared_parameter(
    name: object,
    properties: dict[str, Any],
    *,
    contract: str,
) -> str:
    text = str(name or "").strip()
    if not text or text not in properties:
        raise ValueError(f"{contract} 引用了未声明参数: {text or '<empty>'}")
    if not isinstance(properties[text], dict):
        raise ValueError(f"{contract} 的参数 Schema 无效: {text}")
    return text


def _validate_source_ref(source_ref: object) -> None:
    text = str(source_ref or "").strip()
    parts = text.split(".")
    if (
        len(parts) < 2
        or parts[0] not in _TRUSTED_CONTEXT_ROOTS
        or any(not part or not part.replace("_", "").isalnum() for part in parts)
    ):
        raise ValueError(f"不允许的 trusted source_ref: {text or '<empty>'}")


def _binding_matches(
    binding: TrustedParameterBinding,
    arguments: dict[str, Any],
) -> bool:
    return all(
        condition_name in arguments and arguments[condition_name] == expected
        for condition_name, expected in binding.when
    )


def _first_trusted_value(
    source_refs: tuple[str, ...],
    trusted_context: dict[str, Any],
) -> tuple[str, Any] | None:
    for source_ref in source_refs:
        current: Any = trusted_context
        found = True
        for part in source_ref.split("."):
            if not isinstance(current, dict) or part not in current:
                found = False
                break
            current = current[part]
        if found and _usable_trusted_value(current):
            return source_ref, current
    return None


def _usable_trusted_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _supplied_source(context: ToolInputCompletionContext) -> str:
    return "model_proposed" if context.call_id or context.call_source else "caller_supplied"


def _supplied_source_ref(
    name: str,
    context: ToolInputCompletionContext,
) -> str:
    field = _pointer_part(name)
    if context.call_id:
        return f"tool_call:{context.call_id}#/input/{field}"
    if context.call_source:
        return f"tool_source:{context.call_source}#/input/{field}"
    return f"registry_input#/input/{field}"


def _field_path(name: object) -> str:
    text = str(name)
    if text.replace("_", "").isalnum() and not text[:1].isdigit():
        return f"$.{text}"
    return f"$['{text.replace(chr(39), chr(92) + chr(39))}']"


def _pointer_part(value: object) -> str:
    return str(value).replace("~", "~0").replace("/", "~1")


__all__ = [
    "ToolInputCompletion",
    "ToolInputCompletionContext",
    "ToolInputSource",
    "complete_tool_input",
    "schema_with_tool_input_completion",
]
