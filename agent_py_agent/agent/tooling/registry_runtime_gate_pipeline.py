# LLM: Registry runtime gate pipeline evaluates tool-entry contracts before actual invocation.
# 模块用途: 将工具调用入口的 protocol、manifest、路径/URL/命令、限流和副作用门统一装配。

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from ..contracts.gates import (
    GateContext,
    GateDecision,
    GatePipeline,
    PathUrlCommandFacts,
    ToolGuardrailFacts,
    ToolRateLimitFacts,
    ToolRateLimitPolicy,
    command_name,
    evaluate_path_url_command_gate,
    evaluate_tool_call_gate,
    evaluate_tool_guardrail_gate,
    evaluate_tool_rate_limit_gate,
)
from ..contracts.gates.tool_effects import args_hash_for_call
from ..contracts.tool_protocol_v2 import normalize_tool_call
from ..settings.config_io import load_simple_yaml
from .registry_gate_policy import (
    boundary_bool,
    boundary_list,
    boundary_mapping,
    boundary_path_roots,
    boundary_strings,
    boundary_text,
    tool_gate_policy,
    tool_manifest_decision,
)
from .registry_payload_normalize import tool_name as normalize_tool_name

_DEFAULT_RUNTIME_GUARD_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "runtime_guard_config.yaml"


# LLM: tool_call_gate_decision evaluates the mandatory gate pipeline for one tool payload.
# 函数用途: 在 ToolRegistry 鉴权和工具执行前，返回 allow/block GateDecision 和可持久化证据。
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
    normalized = normalize_tool_call(_payload_for_rate_limit(payload))
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


# LLM: _tool_execution_pipeline registers all mandatory child gates for tool execution.
# 函数用途: 把 gate 装配集中在一个地方，避免主执行函数绕过任何入口合同。
def _tool_execution_pipeline(payload: dict[str, Any], call: object, tool_name: str) -> GatePipeline:
    pipeline = GatePipeline()
    tools = call.tools
    pipeline.register(
        "tool_call",
        lambda _context: evaluate_tool_call_gate(
            payload,
            available_tools=tools.keys(),
            allowed_tools=getattr(call, "allowed_tools", None),
            policy=None,
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
        ),
    )
    return pipeline


# LLM: _path_url_command_decision evaluates filesystem, URL, and shell-command facts for one call.
# 函数用途: 统一读取 workspace/write_boundary 结构字段，给 path_url_command gate 使用。
def _path_url_command_decision(payload: dict[str, Any], call: object) -> GateDecision:
    boundary = getattr(call, "write_boundary", None)
    return evaluate_path_url_command_gate(
        PathUrlCommandFacts(
            payload=payload,
            workspace_root=call.workspace_root,
            workspace_roots=_path_gate_roots(call),
            allowed_private_hosts=boundary_strings(boundary, "allowed_private_hosts"),
            allow_shell_operators=boundary_bool(boundary, "allow_shell_operators"),
            allowed_commands=_controlled_exec_allowed_commands(boundary),
        )
    )


# LLM: _controlled_exec_allowed_commands collects command_allowlist entries from every parent grant.
# 函数用途: 提取所有 controlled_exec_grants 的 command_allowlist，归一化为 basename 小写供 policy 匹配。
def _controlled_exec_allowed_commands(boundary: dict[str, object] | None) -> list[str]:
    grants = boundary_list(boundary, "controlled_exec_grants")
    allowed: list[str] = []
    for grant in grants:
        if not isinstance(grant, dict):
            continue
        _collect_grant_allowlist(grant, allowed)
        _collect_delete_allowlist(grant, allowed)
    return allowed


# LLM: _collect_grant_allowlist extracts command_allowlist entries from a single grant dict.
# 函数用途: 遍历单个 grant 的 command_allowlist 字段，归一化为 command_name 和原始字符串两种形式。
def _collect_grant_allowlist(grant: dict[str, object], allowed: list[str]) -> None:
    for item in grant.get("command_allowlist") or []:
        text = str(item).strip()
        if not text:
            continue
        allowed.append(command_name(text))
        allowed.append(text)


# LLM: _collect_delete_allowlist adds rm/rmdir/unlink to allowed commands when grant has delete_policy.
# 函数用途: 带 delete_policy 的 grant 会由 controlled_exec 工具拦截删除命令并走 task_trash，所以 gate 层不应阻断。
def _collect_delete_allowlist(grant: dict[str, object], allowed: list[str]) -> None:
    constraints = grant.get("constraints")
    if not isinstance(constraints, dict) or not constraints.get("delete_policy"):
        return
    for cmd in ("rm", "rmdir", "unlink"):
        if cmd not in allowed:
            allowed.append(cmd)


# LLM: _tool_guardrail_decision checks tool loop patterns (exact failure, same-tool failure, no-progress).
# 函数用途: 从 boundary 读取 tool_guardrail_records，在工具执行前阻断重复失败/无进展的调用。
def _tool_guardrail_decision(payload: dict[str, Any], call: object) -> GateDecision:
    tool_name = _tool_name_for_gate(payload)
    normalized = normalize_tool_call(_payload_for_rate_limit(payload))
    boundary = getattr(call, "write_boundary", None)
    is_readonly = _tool_effect_for_action(call, tool_name) == "read_only"
    return evaluate_tool_guardrail_gate(
        ToolGuardrailFacts(
            tool_name=tool_name or normalized.tool_name,
            args_hash=args_hash_for_call(normalized.input),
            is_readonly=is_readonly,
        ),
        records=tuple(boundary_list(boundary, "tool_guardrail_records")),
    )


# LLM: _tool_rate_limit_decision checks repeated identical tool calls before invocation.
# 函数用途: 根据 tool/args_hash/window/backoff 结构字段阻断超速或打开熔断的调用。
def _tool_rate_limit_decision(payload: dict[str, Any], call: object, tool_name: str) -> GateDecision:
    normalized = normalize_tool_call(_payload_for_rate_limit(payload))
    boundary = getattr(call, "write_boundary", None)
    return evaluate_tool_rate_limit_gate(
        ToolRateLimitFacts(
            tool_name=tool_name or normalized.tool_name,
            args_hash=args_hash_for_call(normalized.input),
            now=_float_value(boundary_text(boundary, "now"), time.time()),
            operation_id=normalized.operation_id,
        ),
        policy=_tool_rate_limit_policy(boundary),
        records=tuple(boundary_list(boundary, "tool_rate_limit_records")),
    )


# LLM: _tool_rate_limit_policy converts shared defaults plus write_boundary overrides into a typed policy object.
# 函数用途: 先读取统一运行门配置，再让特殊工具/长期监控通过 write_boundary 显式覆盖机器字段。
def _tool_rate_limit_policy(boundary: dict[str, object] | None) -> ToolRateLimitPolicy:
    default = _default_tool_rate_limit_policy()
    value = boundary_mapping(boundary, "tool_rate_limit_policy")
    if not value:
        return default
    return ToolRateLimitPolicy(
        max_calls=_int_value(value.get("max_calls"), default.max_calls),
        window_seconds=_float_value(value.get("window_seconds"), default.window_seconds),
        failure_threshold=_int_value(value.get("failure_threshold"), default.failure_threshold),
        backoff_schedule_seconds=_float_tuple(
            value.get("backoff_schedule_seconds"),
            default.backoff_schedule_seconds,
        ),
        max_records=_int_value(value.get("max_records"), default.max_records),
    )


# LLM: _default_tool_rate_limit_policy reads the shared runtime guard file without requiring task-specific prompts.
# 函数用途: 将工具限流/circuit 默认值集中到 runtime_guard_config.yaml，缺失或坏配置时回落安全默认。
def _default_tool_rate_limit_policy() -> ToolRateLimitPolicy:
    data = _runtime_guard_data()
    return ToolRateLimitPolicy(
        max_calls=_int_value(data.get("tool_rate_max_calls"), 60),
        window_seconds=_float_value(data.get("tool_rate_window_seconds"), 60.0),
        failure_threshold=_int_value(data.get("tool_circuit_failure_threshold"), 3),
        backoff_schedule_seconds=_float_tuple(
            data.get("tool_circuit_backoff_seconds"),
            (1.0, 2.0, 4.0, 8.0, 16.0, 30.0),
        ),
        max_records=_int_value(data.get("tool_rate_max_records"), 256),
    )


# LLM: _runtime_guard_data keeps bad or missing config from disabling the gate pipeline.
# 函数用途: 读取共享运行门配置；配置文件不存在或解析失败时返回空 dict 触发默认值。
def _runtime_guard_data() -> dict[str, object]:
    try:
        data = load_simple_yaml(_DEFAULT_RUNTIME_GUARD_CONFIG_PATH)
    except OSError:
        return {}
    return data if isinstance(data, dict) else {}


# LLM: _payload_for_rate_limit provides a stable args object for legacy flat payloads.
# 函数用途: 兼容 {tool, path} 这类旧形态，把非工具名字段包进 args 再计算 hash。
def _payload_for_rate_limit(payload: dict[str, Any]) -> dict[str, Any]:
    if "tool" not in payload or any(key in payload for key in ("args", "arguments", "input")):
        return payload
    args = {key: value for key, value in payload.items() if key not in {"tool", "kind"}}
    return {**payload, "args": args}


# LLM: _tool_execution_action maps a tool to read_only/mutating/dangerous for pipeline selection.
# 函数用途: 优先读取工具策略，其次读取 ToolSpec.effect，未知时按 read_only 处理。
def _tool_execution_action(call: object, tool_name: str) -> str:
    effect = _tool_effect_for_action(call, tool_name)
    return effect if effect in {"read_only", "mutating", "dangerous"} else "read_only"


# LLM: _tool_effect_for_action reads structured tool effect declarations.
# 函数用途: 从 write_boundary policy 或 tool.spec.effect 获取副作用级别，不解析工具描述文本。
def _tool_effect_for_action(call: object, tool_name: str) -> str:
    tools = call.tools
    policy = tool_gate_policy(getattr(call, "write_boundary", None), tools.get(tool_name))
    if policy is not None:
        value = str(policy.tool_effects.get(tool_name) or "").strip().lower()
        if value:
            return value
    spec = getattr(tools.get(tool_name), "spec", None)
    return str(getattr(spec, "effect", "") or "").strip().lower()


# LLM: _path_gate_roots combines configured workspace roots with parent-granted path roots.
# 函数用途: path gate 在工具执行前同时尊重 workspace_roots 和 write_boundary 的结构化 root grant。
def _path_gate_roots(call: object) -> list[Path]:
    roots = list(getattr(call, "workspace_roots", None) or [])
    roots.extend(Path(item) for item in boundary_path_roots(getattr(call, "write_boundary", None)))
    return roots


# LLM: _tool_name_for_gate keeps malformed tool names inspectable by the gate layer.
# 函数用途: 能规范化就用 registry 名，不能规范化时保留原结构字段给 tool_call gate 产出错误。
def _tool_name_for_gate(payload: dict[str, Any]) -> str:
    try:
        return normalize_tool_name(payload.get("tool"))
    except ValueError:
        return str(payload.get("tool") or "").strip()


# LLM: _int_value converts optional numeric policy fields.
# 函数用途: 类型错误时使用默认值，避免坏配置绕过整个 gate。
def _int_value(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# LLM: _float_value converts optional floating-point policy fields.
# 函数用途: 类型错误时使用默认值，保持 rate-limit gate 可运行。
def _float_value(value: object, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


# LLM: _float_tuple normalizes backoff schedule values.
# 函数用途: 过滤坏项，空 schedule 回落到传入默认退避序列。
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
