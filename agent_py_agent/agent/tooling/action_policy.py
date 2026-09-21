# LLM: 所有副作用前走唯一 ActionPolicy；显式宿主管理可免模型暴露门，用户自主选择只免可选确认，其余硬门保持。
# 模块用途: 从宿主快照裁决模型或管理调用的允许、询问与拒绝，不执行工具或从参数取得权限。
from __future__ import annotations

"""The single pre-effect authorization decision for canonical tool calls."""

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..contracts.gates.command_policy import CommandAnalysis, analyze_command
from ..contracts.gates.path_url_command import (
    PathUrlCommandFacts,
    evaluate_path_url_command_gate,
)
from ..contracts.gates.tool_approval_binding import (
    ApprovalBindingFacts,
    evaluate_approval_binding_gate,
)
from ..contracts.gates.tool_guardrail import (
    ToolGuardrailConfig,
    ToolGuardrailFacts,
    evaluate_tool_guardrail_gate,
    is_guardrail_rejection,
)
from ..contracts.gates.tool_rate_limit import (
    ToolRateLimitFacts,
    evaluate_tool_rate_limit_gate,
)
from ..settings.runtime_guard_config import runtime_guard_bool, runtime_guard_int
from .input_schema import normalize_tool_input, validate_tool_input
from .models import (
    ToolRuntime,
    ToolRuntimeSnapshot,
    sandbox_effect_is_contained,
    tool_effect_for_runtime_policy,
)
from .registry_rate_limit_policy import tool_rate_limit_policy
from .runtime_boundary import exact_read_boundary_error
from .runtime_contracts import ToolCall
from .workspace_scopes import authoritative_workspace_scopes
from .write_boundary import validate_write_boundary

ActionStatus = Literal["allow", "ask", "deny"]
_EFFECT_RANK = {"read_only": 0, "mutating": 1, "dangerous": 2}


@dataclass(frozen=True)
class ActionDecision:
    status: ActionStatus
    reason_codes: tuple[str, ...] = ()
    evidence: dict[str, Any] = field(default_factory=dict)
    approval_request: dict[str, Any] | None = None
    sandbox_plan: dict[str, Any] = field(default_factory=dict)
    resolved_effect: str = "read_only"
    resource_scopes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        status = str(self.status or "").strip().lower()
        effect = str(self.resolved_effect or "").strip().lower()
        if status not in {"allow", "ask", "deny"}:
            raise ValueError(f"invalid action decision status: {status}")
        if effect not in _EFFECT_RANK:
            raise ValueError(f"invalid action effect: {effect}")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "resolved_effect", effect)
        object.__setattr__(
            self,
            "reason_codes",
            tuple(str(item).strip().upper() for item in self.reason_codes if str(item).strip()),
        )
        object.__setattr__(
            self,
            "resource_scopes",
            tuple(str(item).strip() for item in self.resource_scopes if str(item).strip()),
        )

    @property
    def allowed(self) -> bool:
        return self.status == "allow"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason_codes": list(self.reason_codes),
            "evidence": dict(self.evidence),
            "approval_request": dict(self.approval_request or {}),
            "sandbox_plan": dict(self.sandbox_plan),
            "resolved_effect": self.resolved_effect,
            "resource_scopes": list(self.resource_scopes),
        }


# LLM: approval_mode 与 require_model_visibility 只能由宿主组装；显式管理可调用隐藏工具，其他权限不变，修改时同步 ToolExecutorRequest。
# 类用途: 保存本次工具裁决所需的调用、工作目录、安全边界和用户审批选择。
@dataclass(frozen=True)
class ActionPolicyRequest:
    call: ToolCall
    runtime_snapshot: ToolRuntimeSnapshot
    workspace_root: Path
    workspace_roots: tuple[Path, ...] = ()
    path_access_mode: str = "normal"
    path_dangerous_roots: tuple[str, ...] = ()
    owner_scope_root: str = ""
    write_boundary: dict[str, object] | None = None
    runtime_guard_policy: object | None = None
    approval_mode: str = "ask"
    required_action: object | None = None
    now: float = 0.0
    require_model_visibility: bool = True


# LLM: 先执行客观安全门，再考虑可选确认；返回结构化事实供唯一执行器消费，不在这里运行 handler。
# 类用途: 给一次工具调用做统一授权判断，不靠模型正文决定权限。
class ActionPolicy:
    """Evaluate every host-owned gate once, before a handler can run."""

    # LLM: 权限模式由宿主注入，未知值 fail-closed；auto 不替代参数、身份、路径、命令和禁用工具硬门。
    # 函数用途: 对一次调用做统一安全裁决，只有可选交互确认受用户自主模式影响。
    def decide(self, request: ActionPolicyRequest) -> ActionDecision:
        call = request.call
        if request.approval_mode not in {"ask", "auto"}:
            return _deny("APPROVAL_MODE_UNAVAILABLE")
        snapshot = request.runtime_snapshot
        runtime, early = _runtime_for_call(call, snapshot, require_model_visibility=request.require_model_visibility)
        if early is not None:
            return early
        assert runtime is not None

        schema_decision = _schema_decision(call, runtime)
        if schema_decision is not None:
            return schema_decision

        effect, command = _resolved_effect(call, runtime)
        required_decision = _required_action_decision(request, runtime, effect)
        if required_decision is not None:
            return required_decision

        path_decision = _path_url_command_decision(request, runtime, effect)
        if path_decision is not None:
            return path_decision

        boundary_decision = _task_boundary_decision(request, effect)
        if boundary_decision is not None:
            return boundary_decision

        command_decision = _command_classification_decision(
            call,
            runtime,
            command,
            effect,
            allowed_commands=_unknown_command_allowlist(request.runtime_guard_policy),
        )
        if command_decision is not None:
            return command_decision

        idempotency_decision = _idempotency_decision(call, runtime, effect)
        if idempotency_decision is not None:
            return idempotency_decision

        approval_decision = _approval_decision(request, runtime, effect)
        if approval_decision is not None:
            return approval_decision

        guardrail_decision = _guardrail_decision(request, effect)
        if guardrail_decision is not None:
            return guardrail_decision

        rate_decision = _rate_limit_decision(request, effect)
        if rate_decision is not None:
            return rate_decision

        scopes = authoritative_workspace_scopes(
            workspace_root=request.workspace_root,
            write_boundary=request.write_boundary,
            policy=runtime.runtime_policy,
            arguments=call.arguments,
            additional_scopes=tuple(
                f"command_path:{item}" for item in command.paths
            )
            if command is not None
            else (),
        )
        return ActionDecision(
            "allow",
            evidence={
                "tool_name": call.tool_name,
                "call_id": call.call_id,
                "schema_hash": call.schema_hash,
                "snapshot_hash": snapshot.snapshot_hash,
                "args_hash": call.args_hash,
                "approval_mode": request.approval_mode,
            },
            sandbox_plan=_sandbox_plan(request, runtime, effect),
            resolved_effect=effect,
            resource_scopes=scopes,
        )


# LLM: 模型可见性是单独的暴露门，只有显式宿主调用可免除此门；快照、版本与可用性仍须逐项检查。
# 函数用途: 取得本次允许调用的工具，模型默认不能访问隐藏管理工具。
def _runtime_for_call(
    call: ToolCall,
    snapshot: ToolRuntimeSnapshot,
    *, require_model_visibility: bool = True,
) -> tuple[ToolRuntime | None, ActionDecision | None]:
    if snapshot.run_id and call.run_id != snapshot.run_id:
        return None, _deny("TOOL_RUN_SNAPSHOT_MISMATCH")
    runtime = snapshot.runtime(call.tool_name)
    if runtime is None or call.tool_name not in snapshot.available_tool_names:
        return None, _deny("TOOL_NOT_IN_RUNTIME_SNAPSHOT")
    if require_model_visibility is not False and not runtime.exposure.model_visible:
        return None, _deny("TOOL_NOT_MODEL_VISIBLE")
    if call.schema_hash != runtime.model_spec.schema_hash:
        return None, _deny("TOOL_SCHEMA_HASH_MISMATCH")
    if not runtime.availability.available:
        return None, _deny(
            runtime.availability.error_code or "TOOL_UNAVAILABLE",
            stage="runtime_gate",
        )
    return runtime, None


# LLM: 用冻结 schema 校验原生调用，只归一化无歧义类型；拒绝时返回公开参数名而不泄露参数值或宿主字段。
# 函数用途: 执行前检查参数并给出修正线索，不静默丢弃模型填错的字段，也不把错误参数送进 handler。
def _schema_decision(call: ToolCall, runtime: ToolRuntime) -> ActionDecision | None:
    # Host-only values are injected after the provider call and are deliberately
    # absent from the model-visible schema.  Validate the exact provider-owned
    # subset here; the executor separately rejects any model attempt to supply an
    # internal field before trusted completion happens.
    internal = set(runtime.runtime_policy.input_policy.internal_parameters)
    model_arguments = {
        key: value for key, value in call.arguments.items() if key not in internal
    }
    # S-C1: MiniMax-M2.7 会把数组/对象参数序列化成 JSON 字符串（items/run_ids/
    # target 等全部中招），validate 直接拒绝导致 task_progress/create_subagents
    # 批量功能不可用。先做 schema 引导的类型纠正（normalize），无歧义字符串转
    # 回原生类型后再校验；纠正结果写回 call.arguments，让后续 effect/path/
    # 审批/幂等/执行统一使用同一份参数（args_hash 是动态 property，纠正是
    # 确定性的，同一输入永远得到同一 hash）。
    normalization = normalize_tool_input(model_arguments, runtime.model_spec.input_schema)
    validation = validate_tool_input(normalization.value, runtime.model_spec.input_schema)
    if validation.ok:
        if normalization.coercions:
            # ToolCall 是 frozen dataclass；用 object.__setattr__（与 __post_init__
            # 同款）在授权门内做参数纠正，后续 effect/path/审批/幂等/执行门
            # 统一读取同一份参数。call 是单次执行的私有对象，无共享风险。
            merged = {**call.arguments, **normalization.value}
            object.__setattr__(call, "arguments", merged)
        return None
    return _deny(
        validation.primary_error_code or "TOOL_INVALID_ARGUMENTS",
        stage="validation",
        evidence={
            "tool_name": call.tool_name,
            "issues": [item.to_dict() for item in validation.issues],
            "allowed_parameters": sorted(runtime.model_spec.input_schema.get("properties", {})),
        },
    )


# LLM: ActionPolicy 必须既保留 command analysis 证据，又使用全局唯一 effect resolver。
# 函数用途: 计算工具真实副作用，并在 shell 工具上额外返回命令分类结果。
def _resolved_effect(
    call: ToolCall,
    runtime: ToolRuntime,
) -> tuple[str, CommandAnalysis | None]:
    resolver = runtime.runtime_policy.effect_resolver
    if resolver.strategy != "command":
        return tool_effect_for_runtime_policy(runtime.runtime_policy, call.arguments), None
    command = analyze_command(call.arguments.get(resolver.command_parameter))
    return tool_effect_for_runtime_policy(runtime.runtime_policy, call.arguments), command


def _command_classification_decision(
    call: ToolCall,
    runtime: ToolRuntime,
    command: CommandAnalysis | None,
    effect: str,
    *,
    allowed_commands: frozenset[str] = frozenset(),
) -> ActionDecision | None:
    if command is None:
        return None
    evidence = {
        "tool_name": call.tool_name,
        "classification": command.classification,
        "executables": [segment.executable for segment in command.segments],
        "reason_codes": list(command.reason_codes),
    }
    if command.classification == "unknown":
        executables = tuple(segment.executable for segment in command.segments)
        if executables and allowed_commands and all(item in allowed_commands for item in executables):
            # 部署者显式白名单：命令全部段落在 unknown_command_allowlist 中，视为
            # 已声明的受信命令（按 mutating 语义继续走沙箱/边界/灾难保护）。
            return None
        # unknown 命令不进入人工审批（approval_request 无消费端，ask 只会卡死任务），
        # 额度制在更前置的 runtime guard 层（agent_budget_stage，有 agent 身份）按
        # 代理实例分桶管理；这里放行，沙箱/边界/灾难命令保护仍然兜底。
        return None
    if command.classification == "dangerous" and runtime.runtime_policy.approval_policy.mode == "never":
        return _deny("COMMAND_DANGEROUS_DENIED", effect=effect, evidence=evidence)
    return None


def _required_action_decision(
    request: ActionPolicyRequest,
    runtime: ToolRuntime,
    effect: str,
) -> ActionDecision | None:
    action = request.required_action
    if action is None:
        return None
    # 只有 open(待销账)的 required action 才施加工具级执行约束。已 blocked/settled 的
    # 动作(如评估器未给出任何可用证据工具而自动封死)约束已失效,继续拦截只会把
    # "继续干活"类任务卡死:模型误以为要用户确认,而 approval 无消费端(真机实证:
    # click 复刻任务 pwd/ls 只读命令因 2>&1 解析成 mutating 后撞 read_only ceiling,
    # 模型放弃 run_command 改用 read_file,任务停滞)。
    if str(getattr(action, "status", "") or "").strip().lower() != "open":
        return None
    allowed_tools = tuple(getattr(action, "allowed_tools", ()) or ())
    # 只读调用不受 action 工具名单约束:读文件/查目录是任务推进的正常前置,评估模型
    # 生成的 allowed_tools 常只列写/执行工具,漏只读工具会把"先读后改"卡死(真机铁证
    # 2026-08-08: 修 main.go 的 action allowed_tools=[edit_file],模型 read_file 被
    # REQUIRED_ACTION_TOOL_NOT_ALLOWED/TOOL_CHOICE_VIOLATION 连拦 3 轮 break)。
    # 只读调用零副作用,effect_ceiling 已覆盖其效果上限,名单约束只施加于 mutating 以上。
    if not (_EFFECT_RANK.get(effect, 0) <= _EFFECT_RANK["read_only"]):
        if allowed_tools and runtime.model_spec.name not in allowed_tools:
            return _deny("REQUIRED_ACTION_TOOL_NOT_ALLOWED", effect=effect)
    ceiling = str(getattr(action, "effect_ceiling", "") or "").strip().lower()
    if ceiling in _EFFECT_RANK and _EFFECT_RANK[effect] > _EFFECT_RANK[ceiling]:
        return _deny("REQUIRED_ACTION_EFFECT_CEILING_EXCEEDED", effect=effect)
    return None


def _path_url_command_decision(
    request: ActionPolicyRequest,
    runtime: ToolRuntime,
    effect: str,
) -> ActionDecision | None:
    boundary = request.write_boundary if isinstance(request.write_boundary, dict) else {}
    payload = {"tool": request.call.tool_name, **request.call.arguments}
    decision = evaluate_path_url_command_gate(
        PathUrlCommandFacts(
            payload=payload,
            workspace_root=request.workspace_root,
            workspace_roots=list(request.workspace_roots) or [request.workspace_root],
            path_access_mode=request.path_access_mode,
            path_dangerous_roots=request.path_dangerous_roots,
            owner_scope_root=request.owner_scope_root,
            allowed_private_hosts=_string_values(boundary.get("allowed_private_hosts")),
            local_file_url_fields=runtime.runtime_policy.input_policy.local_file_url_parameters,
            allow_shell_operators=_shell_operators_allowed(boundary),
            allowed_commands=_controlled_exec_allowed_commands(boundary),
        )
    )
    if decision.allowed:
        return None
    return _deny(
        decision.finding_codes or ("PATH_URL_COMMAND_DENIED",),
        effect=effect,
        evidence={"gate": decision.to_dict()},
    )


def _idempotency_decision(
    call: ToolCall,
    runtime: ToolRuntime,
    effect: str,
) -> ActionDecision | None:
    if effect == "read_only":
        return None
    scope = runtime.runtime_policy.idempotency_policy.scope
    if scope not in {"operation", "business"}:
        return _deny("TOOL_MANIFEST_IDEMPOTENCY_POLICY_MISSING", effect=effect)
    if not call.operation_id or not call.idempotency_key:
        return _deny("TOOL_IDEMPOTENCY_KEY_MISSING", effect=effect)
    return None


def _task_boundary_decision(
    request: ActionPolicyRequest,
    effect: str,
) -> ActionDecision | None:
    boundary = request.write_boundary
    write_error = validate_write_boundary(
        request.call.tool_name,
        request.call.arguments,
        workspace_root=request.workspace_root,
        workspace_roots=list(request.workspace_roots) or [request.workspace_root],
        path_access_mode=request.path_access_mode,
        path_dangerous_roots=list(request.path_dangerous_roots),
        write_boundary=boundary,
    )
    if write_error:
        return _deny(
            "WRITE_FORBIDDEN",
            effect=effect,
            evidence={"boundary_error": write_error},
        )
    read_error = exact_read_boundary_error(
        request.call.tool_name,
        request.call.arguments,
        workspace_root=request.workspace_root,
        write_boundary=boundary,
    )
    if read_error:
        return _deny(
            "TOOL_PERMISSION_DENIED",
            effect=effect,
            evidence={"boundary_error": read_error},
        )
    return None


# LLM: auto 只免去当前权限内的可选交互确认；此前路径/命令/身份硬门及随后护栏继续执行，always 本人确认不可绕过。
# 函数用途: 结合用户审批选择、工具策略和精确批准记录决定是否需要弹窗，不把自主模式变成 Full Access。
def _approval_decision(
    request: ActionPolicyRequest,
    runtime: ToolRuntime,
    effect: str,
) -> ActionDecision | None:
    mode = runtime.runtime_policy.approval_policy.mode
    sandbox_mode = runtime.runtime_policy.sandbox_policy.mode
    effect_contained = sandbox_effect_is_contained(
        runtime.runtime_policy.sandbox_policy,
        request.call.arguments,
    )
    if (
        mode != "always"
        and sandbox_mode == "required"
        and effect == "dangerous"
        and effect_contained
    ):
        return None
    required = (
        mode == "always"
        or (mode == "mutating" and effect in {"mutating", "dangerous"})
        or (mode == "dangerous" and effect == "dangerous")
    )
    if not required:
        return None
    if request.approval_mode == "auto" and mode != "always":
        return None
    boundary = request.write_boundary if isinstance(request.write_boundary, dict) else {}
    approved = boundary.get("approved_actions")
    approved_actions = tuple(approved) if isinstance(approved, (list, tuple)) else ()
    decision = evaluate_approval_binding_gate(
        ApprovalBindingFacts(
            tool_name=request.call.tool_name,
            run_id=request.call.run_id,
            operation_id=request.call.operation_id,
            idempotency_key=request.call.idempotency_key,
            args_hash=request.call.args_hash,
            approved_actions=approved_actions,
        )
    )
    if decision.allowed:
        return None
    if "APPROVAL_BINDING_MISMATCH" in decision.finding_codes:
        return _deny(
            "APPROVAL_BINDING_MISMATCH",
            effect=effect,
            evidence={"gate": decision.to_dict()},
        )
    return _ask(
        request.call,
        "APPROVAL_REQUIRED",
        effect=effect,
        evidence={"gate": decision.to_dict()},
        approval_kind="tool_action",
    )


def _guardrail_decision(
    request: ActionPolicyRequest,
    effect: str,
) -> ActionDecision | None:
    boundary = request.write_boundary if isinstance(request.write_boundary, dict) else {}
    records = boundary.get("tool_guardrail_records")
    policy = boundary.get("tool_guardrail_policy")
    policy = policy if isinstance(policy, dict) else {}
    record_items = tuple(records) if isinstance(records, (list, tuple)) else ()
    decision = evaluate_tool_guardrail_gate(
        ToolGuardrailFacts(
            tool_name=request.call.tool_name,
            args_hash=request.call.args_hash,
            is_readonly=effect == "read_only",
            result_hash=_latest_guardrail_result_hash(
                record_items,
                request.call.tool_name,
                request.call.args_hash,
            ),
        ),
        config=_tool_guardrail_config(policy, request.runtime_guard_policy),
        records=record_items,
    )
    if decision.allowed:
        return None
    return _deny(
        decision.finding_codes or ("TOOL_GUARDRAIL_DENIED",),
        effect=effect,
        evidence={"gate": decision.to_dict()},
    )


# LLM: 只找精确 tool/args 的最近真实结果；自身拒绝及其它调用的失败不清空其结果哈希，真实同调用失败仍清空。
# 函数用途: 给执行前重复门提供上次结果，避免刚拦截一次就再次放行。
def _latest_guardrail_result_hash(
    records: tuple[object, ...],
    tool_name: str,
    args_hash: str,
) -> str:
    for item in reversed(records):
        if not isinstance(item, dict) or is_guardrail_rejection(item):
            continue
        if str(item.get("tool_name") or "") != tool_name:
            continue
        if str(item.get("args_hash") or "") != args_hash:
            continue
        if item.get("failed") is True:
            return ""
        return str(item.get("result_hash") or "")
    return ""


def _tool_guardrail_config(
    boundary_policy: dict[str, object] | None,
    runtime_policy: object = None,
) -> ToolGuardrailConfig:
    policy = boundary_policy if isinstance(boundary_policy, dict) else {}
    return ToolGuardrailConfig(
        repeat_fail_threshold=_int_value(
            policy.get("repeat_fail_threshold"),
            runtime_guard_int("repeat_fail_threshold", 10, policy=runtime_policy),
        ),
        readonly_no_progress_threshold=_int_value(
            policy.get("readonly_no_progress_threshold"),
            runtime_guard_int(
                "readonly_no_progress_threshold",
                3,
                policy=runtime_policy,
            ),
        ),
        terminal_block_enabled=(
            policy.get("terminal_block_enabled")
            if isinstance(policy.get("terminal_block_enabled"), bool)
            else runtime_guard_bool(
                "terminal_block_enabled",
                False,
                policy=runtime_policy,
            )
        ),
    )


def _rate_limit_decision(
    request: ActionPolicyRequest,
    effect: str,
) -> ActionDecision | None:
    boundary = request.write_boundary if isinstance(request.write_boundary, dict) else {}
    records = boundary.get("tool_rate_limit_records")
    decision = evaluate_tool_rate_limit_gate(
        ToolRateLimitFacts(
            tool_name=request.call.tool_name,
            args_hash=request.call.args_hash,
            now=request.now or time.time(),
            operation_id=request.call.operation_id,
        ),
        policy=tool_rate_limit_policy(boundary, request.runtime_guard_policy),
        records=tuple(records) if isinstance(records, (list, tuple)) else (),
    )
    if decision.allowed:
        return None
    return _deny(
        decision.finding_codes or ("TOOL_RATE_LIMIT_DENIED",),
        effect=effect,
        evidence={"gate": decision.to_dict()},
    )


def _sandbox_plan(
    request: ActionPolicyRequest,
    runtime: ToolRuntime,
    effect: str,
) -> dict[str, Any]:
    boundary = request.write_boundary if isinstance(request.write_boundary, dict) else {}
    return {
        "mode": runtime.runtime_policy.sandbox_policy.mode,
        "effect": effect,
        "read_roots": list(_string_values(boundary.get("allowed_read_roots"))),
        "write_roots": list(_string_values(boundary.get("allowed_write_roots"))),
        "network": str(boundary.get("network_access_mode") or "inherit"),
    }


def _shell_operators_allowed(boundary: dict[str, object]) -> bool:
    if boundary.get("allow_shell_operators") is True:
        return True
    mode = str(boundary.get("shell_access_mode") or "").strip().lower().replace("_", "-")
    return not mode or mode in {"workspace-write", "full-access"}


def _controlled_exec_allowed_commands(boundary: dict[str, object]) -> tuple[str, ...]:
    grants = boundary.get("controlled_exec_grants")
    allowed: list[str] = []
    for grant in grants if isinstance(grants, list) else []:
        if not isinstance(grant, dict):
            continue
        for item in grant.get("command_allowlist") or []:
            text = str(item or "").strip()
            if text:
                allowed.append(text)
    return tuple(dict.fromkeys(allowed))


def _unknown_command_allowlist(policy: object | None) -> frozenset[str]:
    """Deployment-declared trusted executables for the unknown-command gate.

    Read live from runtime_guard_config.yaml (key: unknown_command_allowlist).
    Empty by default: unknown commands fall through to the per-agent rolling
    window budget in agent_budget_stage instead of a hard deny, so ordinary
    users need no configuration at all.
    """
    values = getattr(policy, "values", None)
    if not isinstance(values, dict):
        return frozenset()
    raw = values.get("unknown_command_allowlist")
    items = raw if isinstance(raw, (list, tuple)) else ()
    return frozenset(
        str(item).strip() for item in items if str(item).strip()
    )


def _string_values(value: object) -> tuple[str, ...]:
    values = value if isinstance(value, (list, tuple)) else ()
    return tuple(str(item).strip() for item in values if str(item).strip())


def _int_value(value: object, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _deny(
    codes: str | tuple[str, ...],
    effect: str = "read_only",
    stage: str = "authorization",
    evidence: dict[str, Any] | None = None,
) -> ActionDecision:
    reason_codes = (codes,) if isinstance(codes, str) else tuple(codes)
    return ActionDecision(
        "deny",
        reason_codes,
        {"failure_stage": stage, **dict(evidence or {})},
        resolved_effect=effect,
    )


def _ask(
    call: ToolCall,
    code: str,
    *,
    effect: str,
    evidence: dict[str, Any],
    approval_kind: str,
) -> ActionDecision:
    return ActionDecision(
        "ask",
        (code,),
        evidence,
        approval_request={
            "kind": approval_kind,
            "tool_name": call.tool_name,
            "run_id": call.run_id,
            "operation_id": call.operation_id,
            "idempotency_key": call.idempotency_key,
            "args_hash": call.args_hash,
        },
        resolved_effect=effect,
    )


__all__ = [
    "ActionDecision",
    "ActionPolicy",
    "ActionPolicyRequest",
    "ActionStatus",
]
