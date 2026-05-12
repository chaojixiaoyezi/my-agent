# LLM: Controlled exec gateway bridges parent capability grants to shell gateway requests.
# 模块用途: 把父级授权的命令、路径、网络和输出预算编译成受控 exec 计划，不让子代理自授权。

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path

from .models import CapabilityGrant
from .shell_gateway import ShellGatewayDecision, ShellGatewayRequest, plan_shell_command

_DELETE_COMMANDS = frozenset({"rm", "rmdir", "unlink"})


# LLM: ControlledExecRequest is the stable bundle for grant-backed subagent exec planning.
# 类用途: 保存一次子代理 exec 请求；调用方传父级 grant，不能让模型自己填命令授权。
@dataclass(frozen=True)
class ControlledExecRequest:
    command: str | list[str]
    workspace_root: str | Path
    grant: CapabilityGrant
    cwd: str | Path = ""
    task_dir: str | Path = ""
    artifact_dir: str | Path = ""
    apply: bool = False


# LLM: ControlledExecPlan is refs-only planning data; allowed never means execution already happened.
# 类用途: 返回受控 exec 预检结果，包括 shell 决策、阻断原因和 trash 替代提示。
@dataclass
class ControlledExecPlan:
    allowed: bool
    action: str
    reason: str = ""
    blockers: list[str] = field(default_factory=list)
    shell_decision: ShellGatewayDecision | None = None
    trash_hint: dict[str, str] = field(default_factory=dict)


# LLM: controlled_exec_grant_refs serializes grant-backed execution boundaries for prompts/tools.
# 函数用途: 从父级 CapabilityGrant 生成受控 exec 可见范围；tool 类型 grant 只要显式授权 controlled_exec，也会进入执行边界。
def controlled_exec_grant_refs(grants: list[CapabilityGrant]) -> list[dict[str, object]]:
    return [_controlled_exec_grant_ref(grant) for grant in grants if _is_controlled_exec_grant(grant)]


# LLM: _is_controlled_exec_grant treats controlled_exec tool grants as shell-boundary grants.
# 函数用途: 兼容模型把 capability_type 写成 tool 的真实场景，只要父级 grant 明确包含 controlled_exec 和命令/路径 scope，就允许工具层读取。
def _is_controlled_exec_grant(grant: CapabilityGrant) -> bool:
    if grant.grant_type == "shell":
        return True
    tools = {str(item).strip() for item in grant.tools if str(item).strip()}
    return "controlled_exec" in tools and bool(grant.command_allowlist or grant.path_scope)


# LLM: shell_request_from_controlled_exec exposes the compiled shell request for the execution layer.
# 函数用途: 将受控 exec 请求转换成 shell gateway 请求；仍然只使用父级 grant 的授权边界。
def shell_request_from_controlled_exec(request: ControlledExecRequest) -> ShellGatewayRequest:
    return _shell_request_from_grant(request)


# LLM: plan_controlled_exec compiles a parent grant into shell-gateway policy inputs.
# 函数用途: 规划子代理 exec 能否执行；只相信 CapabilityGrant 的 scope，不执行命令。
def plan_controlled_exec(request: ControlledExecRequest) -> ControlledExecPlan:
    argv, parse_error = _parse_command(request.command)
    if parse_error:
        return _blocked(parse_error)
    if delete := _delete_action_plan(request, argv):
        return delete
    if request.grant.grant_type != "shell":
        return _blocked(f"unsupported_grant_type:{request.grant.grant_type}")
    if not request.grant.path_scope:
        return _blocked("missing_parent_path_scope")
    decision = plan_shell_command(_shell_request_from_grant(request))
    if decision.allowed:
        return ControlledExecPlan(True, "execute_shell", decision.reason, shell_decision=decision)
    return ControlledExecPlan(False, "blocked", decision.reason, decision.blockers, decision)


# LLM: _controlled_exec_grant_ref keeps prompt-visible grant data bounded and non-secret.
# 函数用途: 复制 shell grant 的命令、路径、网络和输出预算，供 runner context/tool wrapper 按 id 查找。
def _controlled_exec_grant_ref(grant: CapabilityGrant) -> dict[str, object]:
    return {
        "grant_id": grant.id,
        "request_id": grant.request_id,
        "run_id": grant.grant_to_run_id,
        "command_allowlist": list(grant.command_allowlist or []),
        "path_scope": list(grant.path_scope or []),
        "network_scope": list(grant.network_scope or []),
        "output_budget": dict(grant.output_budget or {}),
        "risk_level": grant.risk_level,
        "constraints": dict(grant.constraints or {}),
        "delete_policy": {
            "mode": "task_trash",
            "commands": sorted(_DELETE_COMMANDS),
            "requires_apply": True,
            "command_allowlist_required": False,
            "completion_requires": ["moved=true", "trash_manifest_ref"],
        },
    }


# LLM: _shell_request_from_grant is the only place parent grant fields become shell gateway inputs.
# 函数用途: 用父级 grant 的 command/path/network/output scope 构造 ShellGatewayRequest。
def _shell_request_from_grant(request: ControlledExecRequest) -> ShellGatewayRequest:
    grant = request.grant
    return ShellGatewayRequest(
        command=request.command,
        workspace_root=request.workspace_root,
        cwd=request.cwd,
        allowed_roots=grant.path_scope,
        command_allowlist=grant.command_allowlist,
        network_allowlist=grant.network_scope,
        output_budget=grant.output_budget,
        request_id=grant.request_id,
        run_id=grant.grant_to_run_id,
        dry_run=not request.apply,
        artifact_dir=request.artifact_dir,
    )


# LLM: _delete_action_plan redirects deletion-like commands to task-local trash contracts.
# 函数用途: 识别 rm/rmdir/unlink，返回 move_to_task_trash 所需提示，不进入 shell 执行。
def _delete_action_plan(request: ControlledExecRequest, argv: list[str]) -> ControlledExecPlan | None:
    executable = Path(argv[0]).name.lower() if argv else ""
    if executable not in _DELETE_COMMANDS:
        return None
    if not str(request.task_dir or "").strip():
        return _blocked(f"delete_requires_task_trash:{executable}", action="use_task_trash")
    hint = {
        "task_dir": str(Path(request.task_dir).expanduser().resolve()),
        "source_path": _delete_source_path(request, argv),
        "replacement": "move_to_task_trash",
    }
    return ControlledExecPlan(
        False,
        "use_task_trash",
        f"delete_requires_task_trash:{executable}",
        [f"delete_requires_task_trash:{executable}"],
        trash_hint=hint,
    )


# LLM: _delete_source_arg extracts the first non-option target for human/tool guidance only.
# 函数用途: 从删除命令里提取建议移入 trash 的源路径；无法确定时返回空字符串。
def _delete_source_arg(argv: list[str]) -> str:
    for item in argv[1:]:
        if not item.startswith("-"):
            return item
    return ""


# LLM: _delete_source_path resolves relative delete targets against cwd before task trash moves them.
# 函数用途: 删除命令提前绕过 shell gateway 时，仍按命令 cwd 解析相对路径，避免误找 task_dir 下的同名文件。
def _delete_source_path(request: ControlledExecRequest, argv: list[str]) -> str:
    raw = _delete_source_arg(argv)
    if not raw:
        return ""
    source = Path(raw).expanduser()
    if source.is_absolute():
        return str(source)
    base = Path(request.cwd or request.task_dir or request.workspace_root).expanduser()
    return str((base / source).resolve())


# LLM: _parse_command mirrors shell-gateway argv parsing enough to detect delete commands early.
# 函数用途: 解析字符串或 argv 列表；解析失败返回阻断原因。
def _parse_command(command: str | list[str]) -> tuple[list[str], str]:
    if isinstance(command, list):
        argv = [str(item) for item in command if str(item).strip()]
        return argv, "" if argv else "empty_command"
    try:
        argv = shlex.split(str(command))
    except ValueError as exc:
        return [], f"command_parse_error:{exc}"
    return argv, "" if argv else "empty_command"


# LLM: _blocked keeps blocked plans uniform for tests, reports, and future tool wrappers.
# 函数用途: 构造统一阻断计划，默认 action 为 blocked。
def _blocked(reason: str, *, action: str = "blocked") -> ControlledExecPlan:
    return ControlledExecPlan(False, action, reason, [reason])
