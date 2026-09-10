from __future__ import annotations

"""Machine-readable projection of one immutable ``ToolRuntimeSnapshot``.

The manifest is a view, never a second tool definition.  Model schema,
runtime policy, availability and execution all originate from the exact same
run snapshot passed to this module.
"""

from typing import Any

from .error_taxonomy import error_contract, tool_failure_taxonomy


# LLM: manifest 只能投影同一 run 的 ToolRuntimeSnapshot；禁止接收 dict/spec 并猜测已删除字段。
# 函数用途: 将不可变工具运行快照转成目录、归档和诊断共用的机器清单。
def tool_manifest_payload(snapshot: object) -> dict[str, object]:
    from ..tooling.models import ToolRuntimeSnapshot

    if not isinstance(snapshot, ToolRuntimeSnapshot):
        raise TypeError("tool manifest requires ToolRuntimeSnapshot")
    # snapshot.runtimes 已经是本 run 的 owner、allowlist 与 availability 交集。
    # manifest 只能投影这份事实；再次拿 user/group 去匹配 agent role 会让远程
    # TUI 的真实工具清单错误变成空集，而 provider 仍收到同一快照中的 Schema。
    visible_runtimes = tuple(
        runtime for runtime in snapshot.runtimes if runtime.exposure.model_visible
    )
    visible = [runtime.model_spec.name for runtime in visible_runtimes]
    executable = [
        runtime.model_spec.name
        for runtime in visible_runtimes
        if runtime.availability.available
        and runtime.model_spec.name in snapshot.available_tool_names
    ]
    permission_mode = _permission_mode(snapshot.owner_type)
    return {
        "schema_name": "tool_runtime_manifest",
        "schema_version": 2,
        "run_id": snapshot.run_id,
        "snapshot_hash": snapshot.snapshot_hash,
        "owner_type": snapshot.owner_type,
        "permission_mode": permission_mode,
        "allowed_tools": (
            sorted(snapshot.allowed_tools) if snapshot.allowed_tools is not None else None
        ),
        "visible_tools": visible,
        "executable_tools": executable,
        "unavailable_tools": [
            {"name": name, "error_code": code, "reason": reason}
            for name, code, reason in snapshot.unavailable_tools
        ],
        "failure_taxonomy": tool_failure_taxonomy(),
        "failure_contracts": _failure_contracts(),
        "tools": [
            _runtime_item(
                runtime,
                permission_mode=permission_mode,
                executable=runtime.model_spec.name in executable,
            )
            for runtime in visible_runtimes
        ],
    }


def _runtime_item(
    runtime: object,
    *,
    permission_mode: str,
    executable: bool,
) -> dict[str, object]:
    spec = runtime.model_spec
    spec.assert_schema_hash()
    policy = runtime.runtime_policy
    return {
        "name": spec.name,
        "category": spec.hints.category,
        "description": spec.description,
        "schema_hash": spec.schema_hash,
        "input_schema": spec.input_schema,
        "input_fields": _input_field_items(spec.input_schema),
        "model_hints": {
            "use_cases": list(spec.hints.use_cases),
            "avoid_when": list(spec.hints.avoid_when),
            "keywords": list(spec.hints.keywords),
            "examples": list(spec.hints.examples[:2]),
        },
        "runtime_policy": _runtime_policy_item(policy),
        "availability": {
            "available": runtime.availability.available,
            "error_code": runtime.availability.error_code,
            "reason": runtime.availability.reason,
        },
        "exposure": {
            "model_visible": runtime.exposure.model_visible,
        },
        "visible_in_context": True,
        "executable_in_context": executable,
        "permission_mode": permission_mode,
    }


def _input_field_items(input_schema: dict[str, object]) -> list[dict[str, object]]:
    properties = input_schema.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    required = input_schema.get("required")
    required_names = (
        {str(item) for item in required if str(item).strip()}
        if isinstance(required, list)
        else set()
    )
    return [
        {
            "name": str(name),
            "required": str(name) in required_names,
            "description": (str(value.get("description") or "") if isinstance(value, dict) else ""),
        }
        for name, value in properties.items()
    ]


# LLM: manifest 只能展开 canonical ToolRuntimePolicy，effect/sandbox 参数变体不得在此重新推断。
# 函数用途: 把一个工具的运行时策略投影成可诊断、可归档的机器清单。
def _runtime_policy_item(policy: object) -> dict[str, object]:
    resolver = policy.effect_resolver
    return {
        "effect_resolver": {
            "default_effect": resolver.default_effect,
            "strategy": resolver.strategy,
            "command_parameter": resolver.command_parameter,
            "parameter_overrides": [
                {
                    "field": field_name,
                    "values": [{"value": value, "effect": effect} for value, effect in variants],
                }
                for field_name, variants in resolver.by_parameter
            ],
            "combination_overrides": [
                {
                    "conditions": [
                        {"field": field_name, "value": value}
                        for field_name, value in conditions
                    ],
                    "effect": effect,
                }
                for conditions, effect in resolver.by_parameter_combinations
            ],
        },
        "approval_policy": {"mode": policy.approval_policy.mode},
        "sandbox_policy": {
            "mode": policy.sandbox_policy.mode,
            "contained_by_parameter": [
                {"field": field_name, "values": list(values)}
                for field_name, values in policy.sandbox_policy.contained_by_parameter
            ],
        },
        "idempotency_policy": {"scope": policy.idempotency_policy.scope},
        "timeout_policy": {"seconds": policy.timeout_policy.seconds},
        "concurrency_policy": {"mode": policy.concurrency_policy.mode},
        "resource_scopes": {
            "mode": policy.resource_scopes.mode,
            "parameter_names": list(policy.resource_scopes.parameter_names),
            "static_scopes": list(policy.resource_scopes.static_scopes),
        },
        "output_policy": {
            "refs": list(policy.output_policy.refs),
            "trust": policy.output_policy.trust,
            "redaction": policy.output_policy.redaction,
        },
        "availability_policy": {"mode": policy.availability_policy.mode},
        "input_policy": _input_policy_item(policy.input_policy),
        "promotes_task": policy.promotes_task,
    }


def _input_policy_item(input_policy: object) -> dict[str, object]:
    return {
        "internal_parameters": list(input_policy.internal_parameters),
        "safe_default_parameters": [
            str(name) for name, _value in input_policy.safe_parameter_defaults
        ],
        "trusted_parameter_bindings": [
            {
                "name": str(name),
                "source_refs": list(binding.source_refs),
                "when_fields": [str(field) for field, _value in binding.when],
                "authority": binding.authority,
            }
            for name, binding in input_policy.trusted_parameter_bindings
        ],
        "local_file_url_parameters": list(input_policy.local_file_url_parameters),
    }


def _permission_mode(owner_type: str) -> str:
    return "same_as_root_agent" if owner_type == "main_agent" else "owner_scoped"


def _failure_contracts() -> list[dict[str, Any]]:
    return [
        {
            "code": code,
            "category": contract.category,
            "retryable": contract.retryable,
            "recommended_action": contract.recommended_action,
            "recovery_hint": contract.recovery_hint,
        }
        for code in tool_failure_taxonomy()
        for contract in [error_contract(code)]
    ]


__all__ = ["tool_manifest_payload"]
