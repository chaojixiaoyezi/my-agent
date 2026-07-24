
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from ..contracts.gates.adapters import (
    evaluate_tool_call_gate,
    evaluate_tool_call_parameter_gate,
)
from ..contracts.gates.command_policy import command_name
from ..contracts.gates.gate_pipeline import GatePipeline
from ..contracts.gates.models import GateContext, GateDecision
from ..contracts.gates.path_url_command import PathUrlCommandFacts, evaluate_path_url_command_gate
from ..contracts.gates.tool_effects import args_hash_for_call
from ..contracts.gates.tool_guardrail import (
    ToolGuardrailConfig,
    ToolGuardrailFacts,
    evaluate_tool_guardrail_gate,
)
from ..contracts.gates.tool_rate_limit import ToolRateLimitFacts, evaluate_tool_rate_limit_gate
from ..contracts.tool_protocol_v2 import (
    execution_payload_for_tool_protocol,
    normalize_tool_call,
)
from ..settings.runtime_guard_config import (
    runtime_guard_bool,
    runtime_guard_int,
)
from .registry_gate_policy import (
    boundary_bool,
    boundary_list,
    boundary_mapping,
    boundary_path_roots,
    boundary_strings,
    boundary_text,
    tool_call_policy_for_spec,
    tool_gate_policy,
    tool_manifest_decision,
)
from .registry_payload_normalize import tool_name as normalize_tool_name
from .registry_rate_limit_policy import tool_rate_limit_policy


def tool_call_gate_decision(payload: dict[str, Any], call: object) -> GateDecision:
    tool_name = _tool_name_for_gate(payload)
    action = _tool_execution_action(call, tool_name)
    pipeline_decision = _tool_execution_pipeline(payload, call, tool_name).evaluate(
        GateContext(
            phase="tool_execution",
            payload=payload,
            run_id=boundary_text(getattr(call, "write_boundary", None), "run_id"),
            task_id=boundary_text(getattr(call, "write_boundary", None), "task_id"),
            scope={"gate_action": action},
        )
    )
    if not pipeline_decision.allowed:
        return pipeline_decision
    normalized = _normalized_execution_call(payload, call, tool_name)
    return GateDecision.allow(
        "tool_execution",
        evidence={
            **dict(pipeline_decision.evidence),
            "tool_name": tool_name,
            "operation_id": normalized.operation_id,
            "idempotency_key": normalized.idempotency_key,
            "args_hash": args_hash_for_call(normalized.input),
            "tool_execution_action": action,
            "pipeline_gate": pipeline_decision.to_dict(),
        },
    )


def _tool_execution_pipeline(payload: dict[str, Any], call: object, tool_name: str) -> GatePipeline:
    pipeline = GatePipeline()
    tools = call.tools
    spec = getattr(tools.get(tool_name), "spec", None)
    declared_fields = _declared_input_fields(spec)
    pipeline.register(
        "tool_call",
        lambda _context: evaluate_tool_call_gate(
            payload,
            available_tools=tools.keys(),
            allowed_tools=getattr(call, "allowed_tools", None),
            policy=None,
            declared_input_fields=declared_fields,
        ),
    )
    pipeline.register(
        "tool_call",
        lambda _context: evaluate_tool_call_parameter_gate(
            payload,
            tool_call_policy_for_spec(tools.get(tool_name)),
        ),
    )
    pipeline.register("tool_manifest", lambda _context: tool_manifest_decision(tool_name, tools))
    pipeline.register("path_url_command", lambda _context: _path_url_command_decision(payload, call))
    pipeline.register("tool_guardrail", lambda _context: _tool_guardrail_decision(payload, call))
    pipeline.register("tool_rate_limit", lambda _context: _tool_rate_limit_decision(payload, call, tool_name))
    pipeline.register(
        "tool_effect",
        lambda _context: evaluate_tool_call_gate(
            payload,
            available_tools=tools.keys(),
            allowed_tools=getattr(call, "allowed_tools", None),
            policy=tool_gate_policy(getattr(call, "write_boundary", None), tools.get(tool_name)),
            declared_input_fields=declared_fields,
        ),
    )
    return pipeline


def _path_url_command_decision(payload: dict[str, Any], call: object) -> GateDecision:
    boundary = getattr(call, "write_boundary", None)
    return evaluate_path_url_command_gate(
        PathUrlCommandFacts(
            payload=payload,
            workspace_root=call.workspace_root,
            workspace_roots=_path_gate_roots(call),
            path_access_mode=getattr(call, "path_access_mode", "normal"),
            path_dangerous_roots=getattr(call, "path_dangerous_roots", None) or (),
            allowed_private_hosts=boundary_strings(boundary, "allowed_private_hosts"),
            allow_shell_operators=_allow_shell_operators(boundary),
            allowed_commands=_controlled_exec_allowed_commands(boundary),
        )
    )


def _allow_shell_operators(boundary: dict[str, object] | None) -> bool:
    if boundary_bool(boundary, "allow_shell_operators"):
        return True
    mode = boundary_text(boundary, "shell_access_mode").strip().lower().replace("_", "-")
    if not mode:
        return True
    return mode in {"workspace-write", "full-access"}


def _controlled_exec_allowed_commands(boundary: dict[str, object] | None) -> list[str]:
    grants = boundary_list(boundary, "controlled_exec_grants")
    allowed: list[str] = []
    for grant in grants:
        if not isinstance(grant, dict):
            continue
        _collect_grant_allowlist(grant, allowed)
        _collect_delete_allowlist(grant, allowed)
    return allowed


def _collect_grant_allowlist(grant: dict[str, object], allowed: list[str]) -> None:
    for item in grant.get("command_allowlist") or []:
        text = str(item).strip()
        if not text:
            continue
        allowed.append(command_name(text))
        allowed.append(text)


def _collect_delete_allowlist(grant: dict[str, object], allowed: list[str]) -> None:
    constraints = grant.get("constraints")
    if not isinstance(constraints, dict) or not constraints.get("delete_policy"):
        return
    for cmd in ("rm", "rmdir", "unlink"):
        if cmd not in allowed:
            allowed.append(cmd)


def _tool_guardrail_decision(payload: dict[str, Any], call: object) -> GateDecision:
    tool_name = _tool_name_for_gate(payload)
    normalized = _normalized_execution_call(payload, call, tool_name)
    boundary = getattr(call, "write_boundary", None)
    is_readonly = _tool_effect_for_action(call, tool_name) == "read_only"
    records = tuple(boundary_list(boundary, "tool_guardrail_records"))
    return evaluate_tool_guardrail_gate(
        ToolGuardrailFacts(
            tool_name=tool_name or normalized.tool_name,
            args_hash=args_hash_for_call(normalized.input),
            is_readonly=is_readonly,
            result_hash=_latest_guardrail_result_hash(records, tool_name or normalized.tool_name, args_hash_for_call(normalized.input)),
        ),
        config=_tool_guardrail_config(boundary, getattr(call, "runtime_guard_policy", None)),
        records=records,
    )


def _tool_guardrail_config(boundary: dict[str, object] | None, policy: object = None) -> ToolGuardrailConfig:
    boundary_policy = boundary_mapping(boundary, "tool_guardrail_policy") or {}
    return ToolGuardrailConfig(
        repeat_fail_threshold=_int_value(
            boundary_policy.get("repeat_fail_threshold"),
            runtime_guard_int("repeat_fail_threshold", 10, policy=policy),
        ),
        readonly_no_progress_threshold=_int_value(
            boundary_policy.get("readonly_no_progress_threshold"),
            runtime_guard_int("readonly_no_progress_threshold", 3, policy=policy),
        ),
        terminal_block_enabled=_bool_value(
            boundary_policy.get("terminal_block_enabled"),
            runtime_guard_bool("terminal_block_enabled", False, policy=policy),
        ),
    )


def _latest_guardrail_result_hash(records: tuple[object, ...], tool_name: str, args_hash: str) -> str:
    for item in reversed(records):
        if not isinstance(item, dict):
            continue
        if item.get("failed") is True:
            return ""
        if str(item.get("tool_name") or "") != tool_name:
            continue
        if str(item.get("args_hash") or "") != args_hash:
            continue
        return str(item.get("result_hash") or "")
    return ""


def _tool_rate_limit_decision(payload: dict[str, Any], call: object, tool_name: str) -> GateDecision:
    normalized = _normalized_execution_call(payload, call, tool_name)
    boundary = getattr(call, "write_boundary", None)
    return evaluate_tool_rate_limit_gate(
        ToolRateLimitFacts(
            tool_name=tool_name or normalized.tool_name,
            args_hash=args_hash_for_call(normalized.input),
            now=_float_value(boundary_text(boundary, "now"), time.time()),
            operation_id=normalized.operation_id,
        ),
        policy=tool_rate_limit_policy(boundary, getattr(call, "runtime_guard_policy", None)),
        records=tuple(boundary_list(boundary, "tool_rate_limit_records")),
    )


# LLM: 保护、限流和最终允许证据必须哈希同一份 Schema 感知参数，不能各自丢弃同名字段。
# 函数用途: 把当前工具的扁平执行 payload 转成统一规范调用，供全部运行门复用。
def _normalized_execution_call(
    payload: dict[str, Any],
    call: object,
    tool_name: str,
):
    spec = getattr(call.tools.get(tool_name), "spec", None)
    return normalize_tool_call(
        execution_payload_for_tool_protocol(
            payload,
            declared_input_fields=_declared_input_fields(spec),
        )
    )


def _declared_input_fields(spec: object) -> tuple[str, ...]:
    from .tool_spec_schema import tool_spec_runtime_input_schema

    if spec is None:
        return ()
    schema = tool_spec_runtime_input_schema(spec)
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return ()
    return tuple(str(key) for key in properties)


def _tool_execution_action(call: object, tool_name: str) -> str:
    effect = _tool_effect_for_action(call, tool_name)
    return effect if effect in {"read_only", "mutating", "dangerous"} else "read_only"


def _tool_effect_for_action(call: object, tool_name: str) -> str:
    tools = call.tools
    policy = tool_gate_policy(getattr(call, "write_boundary", None), tools.get(tool_name))
    if policy is not None:
        value = str(policy.tool_effects.get(tool_name) or "").strip().lower()
        if value:
            return value
    spec = getattr(tools.get(tool_name), "spec", None)
    return str(getattr(spec, "effect", "") or "").strip().lower()


def _path_gate_roots(call: object) -> list[Path]:
    roots = list(getattr(call, "workspace_roots", None) or [])
    roots.extend(Path(item) for item in boundary_path_roots(getattr(call, "write_boundary", None)))
    return roots


def _tool_name_for_gate(payload: dict[str, Any]) -> str:
    try:
        return normalize_tool_name(payload.get("tool"))
    except ValueError:
        return str(payload.get("tool") or "").strip()


def _int_value(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _float_value(value: object, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _bool_value(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"1", "true"}:
        return True
    if text in {"0", "false"}:
        return False
    return default


def _float_tuple(value: object, default: tuple[float, ...]) -> tuple[float, ...]:
    if not isinstance(value, (list, tuple)):
        return default
    result: list[float] = []
    for item in value:
        try:
            result.append(float(item))
        except (TypeError, ValueError):
            continue
    return tuple(result) or default


__all__ = ["tool_call_gate_decision"]
