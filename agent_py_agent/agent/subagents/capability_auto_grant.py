
from __future__ import annotations

# LLM: 子代理常规能力申请的机制层自动批(编排稳定性攻坚 §5.1 头号靶:真机 3 例
#   A1-u2/B-u1/B-u3——子代理申请 shell/写文件后,主代理模型不调 resolve_capability_requests,
#   子代理卡 BLOCKED 到收口 ok=False)。原则:子代理本就是主代理派的、申请的又只是
#   "在自己任务沙箱内跑命令/写文件"这类常规能力,这种申请不需要模型裁决——机制层按
#   客观围栏直接批,批完走既有 dispatch 续派管道(_blocked_after_capability_grant)续跑。
#   判据全部客观、宁窄勿宽:凡涉及外部能力(mcp/network/skill)、high 风险、纯删除命令、
#   越界路径,一律不自动批,原样走父级裁决(resolve_capability_requests / capability route)。
#   改动时同步检查 agent_core/capability_request_tool.py(接入点)、
#   agent_core/orchestration/dispatch/capability_auto_sweep.py(wake 端兜底)、
#   tests/test_capability_auto_grant.py。
"""Mechanism-level auto-grant for routine subagent capability requests."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..tooling.write_boundary import WRITE_TOOL_NAMES, WRITE_TOOL_ORDER
from .capability_scope import (
    _delete_only_request,
    grant_command_allowlist,
    request_scope_snapshot,
    scoped_constraints,
)
from .model_capabilities import capability_request_counts_as_open
from .services.lifecycle import RecordCapabilityGrantParams

# 常规内置工具:读/写/跑命令,全部只作用于文件系统围栏内,由 owner 墙 + 沙箱兜底。
ROUTINE_GRANT_TOOLS = frozenset(
    {"run_command", *WRITE_TOOL_NAMES, "read_file", "list_files", "search_text", "search"}
)
# LLM: Routine write grants must reuse the canonical ordered mutation tools so
# child snapshots never receive a different editor set from the write boundary.
# 常量用途: 规定自动授权时文件写工具的稳定顺序，并和执行围栏保持同一成员集合。
_WRITE_TOOLS = WRITE_TOOL_ORDER
_SHELL_HINTS = frozenset({"shell", "controlled_exec"})
_NON_ROUTINE_CAPABILITY_TYPES = frozenset({"mcp", "network", "skill"})


@dataclass(frozen=True)
class RoutineGrantAssessment:
    """一条能力申请是否可机制层自动批,以及批下去的生效范围。"""

    eligible: bool
    blockers: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    path_scope: list[str] = field(default_factory=list)
    command_allowlist: list[str] = field(default_factory=list)


# LLM: 判据函数(纯逻辑,无副作用):全部客观事实,绝不解析自然语言。不批的每条原因
#   都进 blockers(可审计/可测);任一 blocker 即整单不批——不做"部分收窄后批"(收窄
#   会让子代理误以为拿到了它要的,还不如留给父级明确裁决)。
# 函数用途: 判定一条申请是不是"常规能力 + 范围全在任务沙箱内",能否机制层直接批。
def assess_routine_capability_request(
    task: Any,
    request: Any,
    *,
    extra_safe_roots: tuple[str, ...] = (),
) -> RoutineGrantAssessment:
    blockers = _routine_request_blockers(task, request, extra_safe_roots)
    if blockers:
        return RoutineGrantAssessment(eligible=False, blockers=blockers)
    requested_paths = _requested_paths(request)
    requested_tools = _clean_list(getattr(request, "requested_tools", []))
    commands = grant_command_allowlist(request)
    tools = list(requested_tools)
    if commands or _shell_capability_requested(request):
        tools = _merge_unique(tools, ["run_command"])
    if requested_paths or any(tool in _WRITE_TOOLS for tool in tools):
        # 与 resolve_capability_requests._mark_grant 同款语义:给了路径围栏就是要在里面
        # 读写,写工具必须并入,否则 path_scope 在运行时不生效(runner_context_service
        # ._granted_filesystem_write_roots 只认带写工具的 grant)。
        tools = _merge_unique(tools, list(_WRITE_TOOLS))
    return RoutineGrantAssessment(
        eligible=True,
        tools=tools,
        # fallback 用 task 上的原始文本(而非 resolve 后的),与 write boundary 里其他根一致。
        path_scope=requested_paths or [_task_root_text(task)],
        command_allowlist=commands,
    )


# 函数用途: 收集一条申请不能自动批的全部客观原因(空列表=常规且在沙箱内)。
def _routine_request_blockers(task: Any, request: Any, extra_safe_roots: tuple[str, ...]) -> list[str]:
    blockers: list[str] = []
    if list(getattr(request, "requested_mcp_tools", []) or []):
        blockers.append("mcp_tools_requested")
    if list(getattr(request, "network_scope", []) or []):
        blockers.append("network_scope_requested")
    if list(getattr(request, "requested_skills", []) or []):
        blockers.append("skills_requested")
    capability_type = str(getattr(request, "capability_type", "") or "").strip().lower()
    if capability_type in _NON_ROUTINE_CAPABILITY_TYPES:
        blockers.append(f"capability_type_not_routine:{capability_type}")
    if str(getattr(request, "risk_level", "") or "").strip().lower() == "high":
        blockers.append("high_risk")
    non_routine = [tool for tool in _clean_list(getattr(request, "requested_tools", [])) if tool not in ROUTINE_GRANT_TOOLS]
    if non_routine:
        blockers.append("non_routine_tools:" + ",".join(non_routine))
    if _delete_only_request(request):
        # 纯删除申请走既有 task_trash/父级裁决通道,自动批出一张空 allowlist 毫无意义。
        blockers.append("delete_only_request")
    if not _task_root_text(task):
        blockers.append("no_task_workspace")
    safe_roots = _task_safe_roots(task, extra_safe_roots)
    out_of_sandbox = [raw for raw in _requested_paths(request) if not _within_any(raw, safe_roots)]
    if out_of_sandbox:
        blockers.append("path_out_of_sandbox:" + ",".join(out_of_sandbox))
    return blockers


# 函数用途: task 自己的工作区根(原始文本,fallback path_scope 与围栏判定共用)。
def _task_root_text(task: Any) -> str:
    return str(getattr(task, "task_workspace_dir", "") or getattr(task, "task_dir", "") or "").strip()


# 函数用途: 申请里显式给出的路径范围(path_scope+cwd_scope,去空去重)。
def _requested_paths(request: Any) -> list[str]:
    return _clean_list(
        [*(getattr(request, "path_scope", None) or []), *(getattr(request, "cwd_scope", None) or [])]
    )


# 函数用途: 申请是否要 shell(带命令 / capability_type 或 needed_capability 声明 shell)。
def _shell_capability_requested(request: Any) -> bool:
    capability_type = str(getattr(request, "capability_type", "") or "").strip().lower()
    needed = str(getattr(request, "needed_capability", "") or "").strip().lower()
    return capability_type in _SHELL_HINTS or needed in _SHELL_HINTS


# LLM: record_capability_grant is the sole atomic resolution boundary: it marks the exact request
# GRANTED, appends one idempotent grant and updates dispatch readiness under the canonical guard.
# Never pre-save a detached task snapshot here; that was the lost-update race with runner result.
# 函数用途: 对一条 OPEN 申请执行机制层自动批;不符合判据或找不到请求返回 None。
def auto_grant_routine_request(
    manager: Any,
    run_id: str,
    request_id: str,
    *,
    extra_safe_roots: tuple[str, ...] = (),
) -> Any | None:
    task = manager.load(run_id)
    request = _open_request(task, request_id)
    if request is None:
        return None
    assessment = assess_routine_capability_request(task, request, extra_safe_roots=extra_safe_roots)
    if not assessment.eligible:
        return None
    grant = manager.lifecycle.record_capability_grant(
        run_id,
        RecordCapabilityGrantParams(
            request_id=request_id,
            grant_type=str(getattr(request, "capability_type", "") or "generic"),
            tools=assessment.tools,
            command_allowlist=assessment.command_allowlist,
            reason="机制层自动批:常规能力(shell/读写)且范围全在任务沙箱内,无需父级裁决。",
            constraints=scoped_constraints(request),
            path_scope=assessment.path_scope,
            output_budget=dict(getattr(request, "output_budget", {}) or {}),
            risk_level=str(getattr(request, "risk_level", "") or ""),
            request_scope={**request_scope_snapshot(request), "resolved_by": "capability_auto_grant"},
        ),
    )
    _append_auto_grant_work_log(manager, run_id, request_id, assessment)
    return grant


# 函数用途: 该 run 所有 OPEN 申请逐条尝试自动批,返回批成的 grant 列表(wake 端兜底用)。
def auto_grant_open_requests(manager: Any, run_id: str, *, extra_safe_roots: tuple[str, ...] = ()) -> list[Any]:
    try:
        task = manager.load(run_id)
    except FileNotFoundError:
        return []
    open_ids = [
        str(getattr(item, "id", "") or "")
        for item in (getattr(task, "capability_requests", None) or [])
        if capability_request_counts_as_open(getattr(item, "status", "OPEN"))
    ]
    grants = []
    for request_id in open_ids:
        if not request_id:
            continue
        grant = auto_grant_routine_request(manager, run_id, request_id, extra_safe_roots=extra_safe_roots)
        if grant is not None:
            grants.append(grant)
    return grants


# 函数用途: 围栏基准=任务自己的工作区目录 + 子代理体系 workspace(调用方可补充);
#   刻意不含主代理更宽的 workspace_roots——自动批宁窄勿宽,越界留给父级裁决。
def _task_safe_roots(task: Any, extra_safe_roots: tuple[str, ...]) -> list[Path]:
    roots: list[Path] = []
    for raw in (
        getattr(task, "task_workspace_dir", ""),
        getattr(task, "task_dir", ""),
        *extra_safe_roots,
    ):
        text = str(raw or "").strip()
        if text:
            roots.append(Path(text).expanduser().resolve(strict=False))
    return roots


def _open_request(task: Any, request_id: str) -> Any | None:
    for item in getattr(task, "capability_requests", None) or []:
        if str(getattr(item, "id", "") or "") != request_id:
            continue
        if capability_request_counts_as_open(getattr(item, "status", "OPEN")):
            return item
        return None
    return None


def _within_any(raw: str, roots: list[Path]) -> bool:
    target = Path(raw).expanduser().resolve(strict=False)
    for root in roots:
        if target == root:
            return True
        try:
            target.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _clean_list(value: Any) -> list[str]:
    items = [str(item or "").strip() for item in (value or [])]
    return list(dict.fromkeys([item for item in items if item]))


def _merge_unique(base: list[str], extra: list[str]) -> list[str]:
    return list(dict.fromkeys([*base, *extra]))


def _append_auto_grant_work_log(manager: Any, run_id: str, request_id: str, assessment: RoutineGrantAssessment) -> None:
    try:
        task = manager.load(run_id)
        manager.actions._append_task_work_log(
            task,
            f"capability_auto_grant: request {request_id} 常规能力机制层自动批,"
            f"tools={','.join(assessment.tools) or 'none'} path_scope={','.join(assessment.path_scope) or 'none'}。",
        )
    except Exception:
        # work log 只是给人看的痕迹,批账(grant)已落盘;记不上不能推翻授权本身。
        import logging

        logging.getLogger(__name__).warning("capability_auto_grant work log failed (run_id=%s)", run_id, exc_info=True)


__all__ = [
    "ROUTINE_GRANT_TOOLS",
    "RoutineGrantAssessment",
    "assess_routine_capability_request",
    "auto_grant_open_requests",
    "auto_grant_routine_request",
]
