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
from ..path_access_policy import data_root_from_boundary
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
from .write_boundary import validate_write_boundary, write_boundary_error_code

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
# 类用途: 保存本次工具裁决所需的调用、工作目录、安全边界与宿主当次管理员全盘投影；模型不能提供授权。
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
    admin_full_access: bool = False
    owner_id: str = ""
    # 只能由原宿主执行链传入；不从ToolCall.arguments或角色/目录投影恢复。
    business_actor: object | None = field(default=None, kw_only=True, repr=False, compare=False)
    business_context: object | None = field(default=None, kw_only=True, repr=False, compare=False)


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

        business_decision = _business_scope_decision(request, runtime)
        if business_decision is not None:
            return business_decision

        effect, command = _resolved_effect(call, runtime)
        required_decision = _required_action_decision(request, runtime, effect)
        if required_decision is not None:
            return required_decision

        boundary_decision = _task_boundary_decision(request, effect)
        if boundary_decision is not None:
            return boundary_decision

        path_decision = _path_url_command_decision(request, runtime, effect)
        if path_decision is not None:
            return path_decision

        scope_decision = _configured_policy_decision(request, runtime, effect, command, kind="scope")
        if scope_decision is not None:
            return scope_decision

        command_decision = _command_classification_decision(
            call,
            runtime,
            command,
            effect,
            allowed_commands=_unknown_command_allowlist(request.runtime_guard_policy),
        )
        if command_decision is not None:
            return command_decision

        filter_decision = _configured_policy_decision(request, runtime, effect, command, kind="command")
        if filter_decision is not None:
            return filter_decision

        idempotency_decision = _idempotency_decision(call, runtime, effect)
        if idempotency_decision is not None:
            return idempotency_decision

        approval_decision, approval_applied = _approval_decision(request, runtime, effect)
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
                **({"approval_applied": True} if approval_applied else {}),
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
    issue_rows = [item.to_dict() for item in validation.issues]
    evidence: dict[str, Any] = {
        "tool_name": call.tool_name,
        "issues": issue_rows,
        "allowed_parameters": sorted(runtime.model_spec.input_schema.get("properties", {})),
    }
    conflict = next(
        (
            row.get("details")
            for row in issue_rows
            if row.get("keyword") == "exclusiveArguments"
            and isinstance(row.get("details"), dict)
        ),
        None,
    )
    if conflict is not None:
        evidence["details"] = dict(conflict)
    return _deny(
        validation.primary_error_code or "TOOL_INVALID_ARGUMENTS",
        stage="validation",
        evidence=evidence,
    )


# LLM: 业务门只收窄原授权；旧无上下文调用无新读取/导入。校验在effect/claim/handler前，不能替代后续原路径、审批和工具门。
# 函数用途: 从真实native handler或宿主typed context重新核验当次业务范围，不用展示标签授予执行权。
def _business_scope_decision(request: ActionPolicyRequest, runtime: ToolRuntime) -> ActionDecision | None:
    native = runtime.model_spec.name == 'business_spaces'
    if not native and request.business_context is None and request.business_actor is None:
        return None
    from ..conversation.business_access import BusinessError
    from ..conversation.business_context import revalidate_business_context

    try:
        handler = runtime.handler
        if native:
            from .business_spaces_tool import BusinessSpacesTool

            if type(handler) is not BusinessSpacesTool:
                raise BusinessError('ACTOR_REQUIRED')
            handler.authorize_invocation(request.call.arguments)
        if request.business_context is not None or request.business_actor is not None:
            actor = request.business_actor if request.business_actor is not None else handler.actor if native else None
            revalidate_business_context(actor=actor, context=request.business_context)
            if native:
                sid = request.call.arguments.get('params', {}).get('space_id')
                if sid is not None and sid != request.business_context.space_id:
                    raise BusinessError('SPACE_SCOPE_MISMATCH')
    except BusinessError as error:
        return _deny(error.code, stage='authorization', evidence={'business_scope_checked': True})
    except (OSError, ValueError, TypeError):
        return _deny('HOST_NOT_READY', stage='authorization', evidence={'business_scope_checked': False})
    return None


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


# LLM: 预检与 handler 共用配置主机与本次授权的并集；总开关显式值优先环境，角色不改变网络事实。
# 函数用途: 在执行前检验工具路径、URL、命令，保留文件与命令硬门并接通已生效的私网设置。
def _path_url_command_decision(
    request: ActionPolicyRequest,
    runtime: ToolRuntime,
    effect: str,
) -> ActionDecision | None:
    boundary = request.write_boundary if isinstance(request.write_boundary, dict) else {}
    from .web import _effective_allow_private_resolution

    private_setting = boundary.get("allow_private_resolution")
    agent_config = getattr(getattr(runtime.handler, "agent", None), "config", None)
    effective_private_hosts = (
        *_string_values(getattr(runtime.handler, "allowed_private_hosts", None)),
        *_string_values(getattr(agent_config, "allowed_private_hosts", None)),
        *_string_values(boundary.get("allowed_private_hosts")),
    )
    payload = {"tool": request.call.tool_name, **request.call.arguments}
    decision = evaluate_path_url_command_gate(
        PathUrlCommandFacts(
            payload=payload,
            workspace_root=request.workspace_root,
            workspace_roots=list(request.workspace_roots) or [request.workspace_root],
            path_access_mode=request.path_access_mode,
            path_dangerous_roots=request.path_dangerous_roots,
            owner_scope_root=request.owner_scope_root,
            allowed_private_hosts=effective_private_hosts,
            allow_private_resolution=_effective_allow_private_resolution(
                private_setting if type(private_setting) is bool else None),
            network_enabled=boundary.get("network_access_mode") != "disabled",
            local_file_url_fields=runtime.runtime_policy.input_policy.local_file_url_parameters,
            metadata_url_fields=runtime.runtime_policy.input_policy.metadata_url_parameters,
            allow_shell_operators=_shell_operators_allowed(boundary),
            allowed_commands=_controlled_exec_allowed_commands(boundary),
            data_root=str(data_root_from_boundary(boundary) or ""),
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
            write_boundary_error_code(write_error),
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
    prohibited_error = _prohibited_action_error(
        boundary, request.call.tool_name, request.call.arguments
    )
    if prohibited_error:
        return _deny(
            "TOOL_PERMISSION_DENIED",
            effect=effect,
            evidence={"boundary_error": prohibited_error},
        )
    return None


def _prohibited_action_error(
    boundary: dict[str, object] | None,
    tool_name: str,
    arguments: dict[str, Any],
) -> str:
    if not isinstance(boundary, dict):
        return ""
    raw_prohibited = boundary.get("prohibited_actions")
    if not raw_prohibited or not isinstance(raw_prohibited, (list, tuple, set, frozenset)):
        return ""
    prohibited = set(raw_prohibited)
    normalized_tool = str(tool_name or "").strip().lower()

    target_action = normalized_tool
    if normalized_tool == "apply_patch":
        target_action = str(arguments.get("patch") or "").strip().lower()
    elif normalized_tool in {"run_command", "process_session", "terminal_session"}:
        target_action = str(arguments.get("command") or arguments.get("cmd") or "").strip().lower()

    if normalized_tool in prohibited or target_action in prohibited:
        matched = normalized_tool if normalized_tool in prohibited else target_action
        return f"Action '{matched}' is prohibited by delegation constraints."

    if "git_push" in prohibited and (
        target_action == "git_push" or "git push" in target_action or normalized_tool == "git_push"
    ):
        return "Action 'git_push' is prohibited by delegation constraints."

    if "git_reset_hard" in prohibited and (
        target_action == "git_reset_hard"
        or "git reset --hard" in target_action
        or "git reset -hard" in target_action
        or normalized_tool == "git_reset_hard"
    ):
        return "Action 'git_reset_hard' is prohibited by delegation constraints."

    if "spawn_children" in prohibited and (
        normalized_tool in {"create_subagents", "spawn_children"}
        or target_action in {"create_subagents", "spawn_children"}
    ):
        return "Action 'spawn_children' is prohibited by delegation constraints."

    if "network_access" in prohibited and (
        normalized_tool in {"web_search", "web_fetch", "network_access", "network_fetch"}
        or target_action in {"web_search", "web_fetch", "network_access", "network_fetch"}
        or "curl " in target_action
        or target_action.startswith("curl")
        or "wget " in target_action
        or target_action.startswith("wget")
        or "http://" in target_action
        or "https://" in target_action
    ):
        return "Action 'network_access' is prohibited by delegation constraints."

    if "network_fetch" in prohibited and (
        normalized_tool in {"web_fetch", "network_fetch"}
        or target_action in {"web_fetch", "network_fetch"}
        or "curl " in target_action
        or target_action.startswith("curl")
        or "wget " in target_action
        or target_action.startswith("wget")
        or "http://" in target_action
        or "https://" in target_action
    ):
        return "Action 'network_fetch' is prohibited by delegation constraints."

    if "shell_execution" in prohibited and (
        normalized_tool in {"run_command", "process_session", "terminal_session"}
        or target_action in {"run_command", "process_session", "terminal_session", "shell_execution"}
    ):
        return "Action 'shell_execution' is prohibited by delegation constraints."

    if "file_delete" in prohibited:
        if normalized_tool == "file_delete" or target_action == "file_delete":
            return "Action 'file_delete' is prohibited by delegation constraints."
        import re

        has_rm = bool(re.search(r"(?:^|[;&|\s])rm(?:\s|$)", target_action))
        has_unlink = bool(re.search(r"(?:^|[;&|\s])unlink(?:\s|$)", target_action))
        if (
            has_rm
            or has_unlink
            or "shutil.rmtree" in target_action
            or "os.remove" in target_action
            or "os.unlink" in target_action
            or "*** delete file:" in target_action
        ):
            return "Action 'file_delete' is prohibited by delegation constraints."

    return ""


# LLM: auto 只免去当前权限内的可选确认；宿主当次已核管理员 Full Access 仅免普通 MCP 的逐工具 always，插件及其他 always 仍本人确认。身份、路径、网络和插件 deny 不在此免除。
# 第二个返回值只在精确批准 binding 匹配时为 True（用户确实批准过这次调用），沙箱内自动执行、策略不要求、auto 模式都为 False；执行器据此决定批准后是否再复核。
# 函数用途: 结合用户审批选择、工具策略和精确批准记录决定是否需要弹窗，不把自主模式变成 Full Access。
def _approval_decision(
    request: ActionPolicyRequest,
    runtime: ToolRuntime,
    effect: str,
) -> tuple[ActionDecision | None, bool]:
    mode = runtime.runtime_policy.approval_policy.mode
    human_required = runtime.runtime_policy.approval_policy.require_human
    always_effects = runtime.runtime_policy.approval_policy.always_effects
    sandbox_mode = runtime.runtime_policy.sandbox_policy.mode
    effect_contained = sandbox_effect_is_contained(
        runtime.runtime_policy.sandbox_policy,
        request.call.arguments,
    )
    if (
        not human_required
        and mode != "always"
        and sandbox_mode == "required"
        and effect == "dangerous"
        and effect_contained
    ):
        return None, False
    required = human_required or (
        (mode == "always" and (not always_effects or effect in always_effects))
        or (mode == "mutating" and effect in {"mutating", "dangerous"})
        or (mode == "dangerous" and effect == "dangerous")
    )
    if not required:
        return None, False
    if not human_required and request.approval_mode == "auto" and (mode != "always" or _admin_full_access_mcp(request, runtime)):
        return None, False
    boundary = request.write_boundary if isinstance(request.write_boundary, dict) else {}
    approved = boundary.get("approved_actions")
    approved_actions = tuple(approved) if isinstance(approved, (list, tuple)) else ()
    if human_required:
        import time

        from ..contracts.gates.tool_approval_binding import has_current_human_approval
        facts = ApprovalBindingFacts(tool_name=request.call.tool_name, run_id=request.call.run_id,
            operation_id=request.call.operation_id, idempotency_key=request.call.idempotency_key,
            args_hash=request.call.args_hash, approved_actions=approved_actions,
            owner_id=request.owner_id)
        if not has_current_human_approval(facts, time.time()):
            approved_actions = ()
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
        return None, True
    if "APPROVAL_BINDING_MISMATCH" in decision.finding_codes:
        return _deny(
            "APPROVAL_BINDING_MISMATCH",
            effect=effect,
            evidence={"gate": decision.to_dict()},
        ), False
    return _ask(
        request.call,
        "APPROVAL_REQUIRED",
        effect=effect,
        evidence={"gate": decision.to_dict(), "human_confirmation_required": human_required},
        approval_kind="tool_action",
    ), False


# LLM: 仅承认宿主 bool 的准确 True 与原普通 MCP 实现；插件继承类、名字前缀和描述均不能签权。前置硬门及后续 PluginToolGate 仍执行。
# 函数用途: 判断本次是否可免普通 MCP 的逐次确认，不扩展任何路径或工具可见范围。
def _admin_full_access_mcp(request: ActionPolicyRequest, runtime: ToolRuntime) -> bool:
    from .mcp_registration import MCPProxyTool

    return request.admin_full_access is True and type(runtime.handler) is MCPProxyTool


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


def _policy_host_config(request, runtime):
    """只取本次宿主已加载的配置快照；关闭时不探测磁盘或加载 owner 状态。"""
    boundary = request.write_boundary if isinstance(request.write_boundary, dict) else {}
    policy = getattr(request.runtime_guard_policy, "values", None)
    switches = policy if isinstance(policy, dict) else {}
    host = None
    # Shell 本身没有 Agent，冻结目录中其它原生 handler 仍持有同一真实宿主。
    for selected in (runtime, *request.runtime_snapshot.runtimes):
        handler = selected.handler
        candidate = getattr(handler, "agent", None)
        if candidate is None:
            candidate = getattr(getattr(handler, "registry", None), "remote_push_agent", None)
        if candidate is None:
            continue
        home = getattr(getattr(candidate, "home_paths", None), "owner_home_dir", None)
        if request.owner_scope_root and home is not None and str(Path(home)) != str(Path(request.owner_scope_root)):
            continue
        host = candidate
        break
    snapshot = getattr(host, "_capability_config_runtime_snapshot", None)
    config = getattr(snapshot, "config", None)
    # write_boundary 与 runtime_guard_policy 均是宿主参数，不读取 ToolCall.arguments 中的同名键。
    values = {name: boundary.get(name, switches.get(name, getattr(config, name, False))) is True
              for name in ("command_approval_filter_enabled", "target_scope_policy_enabled")}
    values["tool_approval_timeout_seconds"] = getattr(config, "tool_approval_timeout_seconds", 1800)
    return host, values


def _policy_command_values(command):
    """补齐常见文件命令的相对操作数；字面 shell 程序复用原分析器，不执行展开。"""
    from ..contracts.gates.command_policy import _NESTED_SHELL_DEPTH_LIMIT_COUNT
    from ..contracts.gates.shell_source import shell_command_source

    paths, urls, commands = [], [], []
    pending = [(command, 0)] if command is not None else []
    file_commands = {"cat", "cp", "mv", "tee", "stat", "file", "touch", "mkdir", "ln",
                     "wc", "head", "tail", "ls", "du", "find", "sort", "uniq", "diff", "cmp"}
    value_flags = {"-n", "-c", "--lines", "--bytes", "--max-depth", "-t", "--target-directory"}
    while pending:
        analysis, depth = pending.pop()
        paths.extend(analysis.paths)
        for segment in analysis.segments:
            commands.append(segment.argv)
            args = segment.argv[1:]
            urls.extend(value for value in args if value.startswith(("http://", "https://")))
            if segment.executable in {"sh", "bash", "zsh", "dash", "ksh"}:
                source = shell_command_source(args)
                if source is not None and depth < _NESTED_SHELL_DEPTH_LIMIT_COUNT:
                    pending.append((analyze_command(source), depth + 1))
                continue
            if segment.executable not in file_commands:
                continue
            operands, skip, options = [], False, True
            for value in args:
                if skip:
                    skip = False
                    continue
                if value == "--" and options:
                    options = False
                    continue
                if options and value.startswith("-"):
                    skip = value in value_flags
                    # target-directory 的值本身是文件目标，不当数字选项丢掉。
                    if value in {"-t", "--target-directory"}:
                        skip = False
                    continue
                if value and value not in {"|", ";", "&&", "||", "&", ">", ">>", "<"}:
                    operands.append(value)
            paths.extend(operands or (["."] if segment.executable in {"ls", "du", "find"} else []))
    return paths, urls, commands


def _policy_call_facts(request, command):
    """与原路径/URL 门共用字段匹配器；路径先 resolve，规则评估器无需任何 I/O。"""
    from urllib.parse import urlparse

    from ..contracts.gates.path_url_command import _matching_values
    arguments = request.call.arguments
    root = request.workspace_root
    paths = []
    for _key, value in _matching_values(arguments,
            {"path", "file_path", "target_path", "output_path", "working_dir", "cwd", "directory"}):
        if isinstance(value, str) and value:
            candidate = Path(value)
            paths.append(str((candidate if candidate.is_absolute() else root / candidate).resolve(strict=False)))
    cwd = Path(str(arguments.get("working_dir") or arguments.get("cwd") or root))
    if not cwd.is_absolute():
        cwd = root / cwd
    command_paths, command_urls, commands = _policy_command_values(command)
    if command is not None:
        for value in command_paths:
            candidate = Path(value)
            paths.append(str((candidate if candidate.is_absolute() else cwd / candidate).resolve(strict=False)))
    domains = []
    for _key, value in _matching_values(arguments, {"url", "urls", "endpoint", "webhook_url"}):
        parsed = urlparse(str(value))
        if parsed.hostname and parsed.scheme.lower() != "file":
            domains.append(parsed.hostname)
    for value in command_urls:
        parsed = urlparse(value)
        if parsed.hostname:
            domains.append(parsed.hostname)
    boundary = request.write_boundary if isinstance(request.write_boundary, dict) else {}
    task_id = str(getattr(request.business_context, "task_id", "") or
                  boundary.get("canonical_task_id") or boundary.get("task_id") or request.call.run_id)
    allowed = request.runtime_snapshot.allowed_tools
    return {"tool": request.call.tool_name, "task_id": task_id,
            "argv": command.argv if command is not None else (),
            "commands": tuple(commands),
            "paths": tuple(dict.fromkeys(paths)), "domains": tuple(dict.fromkeys(domains)),
            "workspace_root": str(root.resolve(strict=False)),
            "allowed_tools": allowed if allowed is not None else request.runtime_snapshot.available_tool_names}


def _record_policy_decision(request, decision, facts, *, approval_id="", scope=False):
    """日志失败仅诊断，不把可选索引或历史变成新的工具硬门。"""
    import logging

    from ..business_spaces.gap158_w2_policy_history import append_record
    try:
        append_record(request.owner_scope_root, {
            "at": request.now or time.time(), "tool": request.call.tool_name,
            "task_id": facts["task_id"], "run_id": request.call.run_id,
            "execution_id": request.call.operation_id or request.call.call_id,
            "decision": "judge_pending" if decision.decision == "judge" else decision.decision,
            "rule_id": decision.rule_id, "rule_source": decision.rule_source or "default",
            "scope_rule_id": decision.rule_id if scope else "", "approval_id": approval_id})
    except (OSError, ValueError, RuntimeError) as error:
        logging.getLogger(__name__).warning("W2 政策历史未写入：%s", type(error).__name__)


def _configured_policy_decision(request, runtime, effect, command, *, kind):
    """两个开关仅加收窄门；批准后凭原 binding 复入，不从待决索引签发执行许可。"""
    host, config = _policy_host_config(request, runtime)
    enabled = config["target_scope_policy_enabled" if kind == "scope" else "command_approval_filter_enabled"]
    if not enabled or not request.owner_scope_root:
        return None
    from ..business_spaces.gap158_w2_policy_store import get_snapshot
    from .policy_rules import PolicyDecision, evaluate_command_rules, evaluate_scope_rules
    facts = _policy_call_facts(request, command)
    snapshot = get_snapshot(request.owner_scope_root, kind, facts["task_id"], request.workspace_root)
    decision = (evaluate_scope_rules if kind == "scope" else evaluate_command_rules)(snapshot, facts)
    if kind == "command" and not decision.rule_id:
        tool_config = next((row for row in snapshot.get("tool_configs", ())
                            if row["tool"] == request.call.tool_name), {})
        default = tool_config.get("default_decision", "inherit")
        if default != "inherit":
            decision = PolicyDecision(default, "", "tool_config", ("工具默认审批策略",))
    evidence = {"policy_rule": {"decision": decision.decision, "rule_id": decision.rule_id,
                "rule_source": decision.rule_source, "reasons": list(decision.reasons)}}
    if decision.decision == "deny":
        _record_policy_decision(request, decision, facts, scope=kind == "scope")
        return _deny("TARGET_SCOPE_DENIED" if kind == "scope" else "POLICY_RULE_DENIED",
                     effect=effect, evidence=evidence)
    if kind == "scope" or decision.decision == "allow":
        if decision.rule_id:
            _record_policy_decision(request, decision, facts, scope=kind == "scope")
        return None
    boundary = request.write_boundary if isinstance(request.write_boundary, dict) else {}
    approved = boundary.get("approved_actions", ())
    binding = evaluate_approval_binding_gate(ApprovalBindingFacts(
        tool_name=request.call.tool_name, run_id=request.call.run_id,
        operation_id=request.call.operation_id, idempotency_key=request.call.idempotency_key,
        args_hash=request.call.args_hash, approved_actions=tuple(approved) if isinstance(approved, (list, tuple)) else ()))
    if binding.allowed:
        return None
    if "APPROVAL_BINDING_MISMATCH" in binding.finding_codes:
        return _deny("APPROVAL_BINDING_MISMATCH", effect=effect, evidence={"gate": binding.to_dict()})
    approval_id = ""
    try:
        from ..business_spaces.gap158_w2_judge import add_pending, schedule_for_request
        entry = add_pending(request, runtime, decision, task_id=facts["task_id"],
            now=request.now or time.time(), timeout_seconds=config["tool_approval_timeout_seconds"])
        approval_id = entry["approval_id"]
        if decision.decision == "judge" and host is not None:
            schedule_for_request(request, runtime, entry, snapshot.get("judge_config", {}), host)
    except (OSError, ValueError, RuntimeError) as error:
        import logging
        logging.getLogger(__name__).warning("W2 待决索引未就绪：%s", type(error).__name__)
    _record_policy_decision(request, decision, facts, approval_id=approval_id)
    return _ask(request.call, "APPROVAL_REQUIRED", effect=effect,
                evidence=evidence, approval_kind="command_approval_filter")


__all__ = [
    "ActionDecision",
    "ActionPolicy",
    "ActionPolicyRequest",
    "ActionStatus",
]
