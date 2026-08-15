from __future__ import annotations

"""LLM: 只在统一工具入口补入明示安全默认值或可信运行事实，不猜字段、不读取自然语言。

模块用途: 为缺失工具参数提供一个可审计的结构化补全点，并记录不含参数值的 source/source_ref。
"""

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from .models import ToolRuntime, TrustedParameterBinding


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
    error_code: str = ""
    conflict_fields: tuple[str, ...] = ()


def complete_tool_arguments(
    arguments: dict[str, Any],
    runtime: ToolRuntime,
    context: ToolInputCompletionContext | None = None,
    *,
    include_trusted_bindings: bool = True,
) -> ToolInputCompletion:
    """Complete canonical arguments from ToolRuntimePolicy, never from prose."""

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
    policy = runtime.runtime_policy.input_policy
    for name, value in policy.safe_parameter_defaults:
        if name in completed:
            continue
        completed[name] = deepcopy(value)
        sources.append(
            ToolInputSource(
                path=_field_path(name),
                source="safe_default",
                source_ref=(
                    f"tool_runtime:{runtime.model_spec.name}"
                    f"#/input_policy/safe_parameter_defaults/{_pointer_part(name)}"
                ),
            )
        )
    if not include_trusted_bindings:
        return ToolInputCompletion(completed, tuple(sources))
    trusted_context = (
        completion_context.trusted_context
        if isinstance(completion_context.trusted_context, dict)
        else {}
    )
    conflicts: list[str] = []
    for name, binding in policy.trusted_parameter_bindings:
        if not _binding_matches(binding, completed):
            continue
        resolved = _first_trusted_value(binding.source_refs, trusted_context)
        if resolved is None:
            continue
        source_ref, value = resolved
        if name in completed:
            if binding.authority == "fill_missing":
                continue
            if binding.authority == "must_match" and completed[name] != value:
                conflicts.append(str(name))
                sources.append(
                    ToolInputSource(
                        path=_field_path(name),
                        source="trusted_context_conflict",
                        source_ref=source_ref,
                    )
                )
                continue
            if binding.authority == "must_match":
                sources = [item for item in sources if item.path != _field_path(name)]
        completed[name] = deepcopy(value)
        if binding.authority == "host_authoritative":
            sources = [item for item in sources if item.path != _field_path(name)]
        sources.append(
            ToolInputSource(
                path=_field_path(name),
                source=(
                    "trusted_context_verified"
                    if binding.authority == "must_match"
                    else "trusted_context"
                ),
                source_ref=source_ref,
            )
        )
    return ToolInputCompletion(
        completed,
        tuple(sources),
        "TOOL_TRUSTED_PARAMETER_CONFLICT" if conflicts else "",
        tuple(conflicts),
    )


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
    "complete_tool_arguments",
]
