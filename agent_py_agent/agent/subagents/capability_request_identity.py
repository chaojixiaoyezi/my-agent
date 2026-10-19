# LLM: 请求去重仅复用同task既有结构化能力；已GRANTED的复用还须同canonical真实grant、有效期和完整范围，描述文字不签权。联查lifecycle/result_structured及授权边界测试。
# 模块用途: 比较能力申请，并在真实授权仍覆盖时避免相同权限因措辞变化再次排队。
from __future__ import annotations

import math
import shlex
import time
from collections.abc import Iterable
from pathlib import Path

from ..contracts.gates.command_policy import command_source_too_large
from .model_capabilities import capability_request_suppresses_duplicate


# LLM: 两写入方必须传当前canonical task；OPEN保原签名协议，GRANTED才核实际grant完整范围，不凭旧文字或无grant记录跳过申请；新命令参数语义另设计。
# 函数用途: 找回已排队的同一申请，或仍被本任务真实授权完整覆盖的旧申请。
def find_equivalent_capability_request(requests: Iterable[object], candidate: object, *, task=None):
    target = capability_request_signature(candidate)
    for request in requests or []:
        status = getattr(request, "status", "")
        if not capability_request_suppresses_duplicate(status):
            continue
        if status == "GRANTED":
            if task is not None and _request_grant_still_covers(task, request, candidate):
                return request
        elif capability_request_signature(request) == target:
            return request
    return None


# LLM: 同一旧申请与新申请的非路径结构字段必须相等，路径另核actual grant子树包含，命令比较原完整文本而非仅可执行名；不把problem/needed/risk等展示声明当授权。真正权限另核grant持久字段。
# 函数用途: 比较已经授权的请求范围，防止换工具、路径、命令、网络或预算时撞中旧许可。
def _structured_request_scope(value):
    lists = ("requested_tools", "requested_skills", "requested_mcp_tools", "requested_commands", "network_scope")
    return (_text_attr(value, "capability_type", default="generic").lower(),
            *(tuple(sorted(set(_list_attr(value, name)))) for name in lists),
            _object_dict_attr(value, "output_budget"), _object_dict_attr(value, "constraints"))


# LLM: grant/request仅来自同canonical task；终态、归属、过期和范围都核真实记录，工具复用原runner纯投影扣除canonical disabled，不把allowed_tools声明当有效工具。不创建/修复权限。
# 函数用途: 判断新申请是否仍被原任务当前有效授权完整覆盖，避免旧批准掩盖撤销或越界。
def _request_grant_still_covers(task, request, candidate):
    from .models import SUBAGENT_ENDED_STATUSES, task_status_in
    from .services.runner_context_service import _runner_allowed_tools

    if task_status_in(getattr(task, "status", ""), SUBAGENT_ENDED_STATUSES):
        return False
    if _structured_request_scope(request) != _structured_request_scope(candidate):
        return False
    target_fields = ("requested_tools", "requested_skills", "requested_mcp_tools", "requested_commands", "path_scope", "cwd_scope", "network_scope")
    if not any(_list_attr(candidate, key) for key in target_fields):
        return False
    run_id = str(getattr(task, "id", "") or "")
    if not run_id or str(getattr(request, "from_run_id", "") or "") != run_id:
        return False
    for grant in getattr(task, "capability_grants", ()) or ():
        if (getattr(grant, "request_id", "") != getattr(request, "id", "")
                or getattr(grant, "grant_to_run_id", "") != run_id):
            continue
        try:
            deadline = float(getattr(grant, "expires_at", 0))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(deadline) or deadline < 0 or (deadline and deadline <= time.time()):
            continue
        tools = set(_list_attr(candidate, "requested_tools") + _list_attr(candidate, "requested_mcp_tools"))
        effective_tools = set(_runner_allowed_tools(task, _list_attr(task, "allowed_tools")))
        if not tools.issubset(set(_list_attr(grant, "tools") + _list_attr(grant, "mcp_tools")) & effective_tools):
            continue
        if not set(_list_attr(candidate, "requested_skills")).issubset(set(_list_attr(grant, "skills")) & set(_list_attr(task, "allowed_skills"))):
            continue
        if not _requested_paths_within_grant(candidate, grant):
            continue
        if not _actual_request_details_cover(grant, candidate):
            continue
        if not set(_list_attr(candidate, "network_scope")).issubset(set(_list_attr(grant, "network_scope"))):
            continue
        if _object_dict_attr(candidate, "output_budget") != _object_dict_attr(grant, "output_budget"):
            continue
        return True
    return False


# LLM: 旧request文字不能补回grant丢失字段；非空constraints须原grant保存，命令同时核实际完整request_scope和exe门。缺字段拒复用，不能把exe-only当精确命令授权。
# 函数用途: 检查实际授权是否保留申请中的限制与完整命令，不增建状态或推断参数是否扩权。
def _actual_request_details_cover(grant, candidate):
    constraints = _object_dict_attr(candidate, "constraints")
    actual = _object_dict_attr(grant, "constraints")
    if any(key not in actual or actual[key] != value for key, value in constraints.items()):
        return False
    commands = _list_attr(candidate, "requested_commands")
    if not commands:
        return True
    names = [_command_name(item) for item in commands]
    if any(not name for name in names) or not set(names).issubset(set(_list_attr(grant, "command_allowlist"))):
        return False
    scope = _object_dict_attr(grant, "request_scope")
    recorded = scope.get("requested_commands")
    return isinstance(recorded, list) and sorted(set(commands)) == sorted(set(recorded))


# LLM: 持久grant根必须仍等于原canonical绝对路径；根/祖先被换成符号链接即失去覆盖，不能重新resolve替旧许可改绑。旧相对grant不猜cwd，子link逃逸也拒复用。
# 函数用途: 判断新申请的文件/工作目录是否都位于原实际授权目录内，不读取文件内容。
def _requested_paths_within_grant(request, grant):
    requested = _list_attr(request, "path_scope") + _list_attr(request, "cwd_scope")
    if not requested:
        return True
    try:
        roots = [Path(raw).expanduser() for raw in _list_attr(grant, "path_scope")]
        targets = [Path(raw).expanduser() for raw in requested]
        if not roots or any(not path.is_absolute() for path in (*roots, *targets)):
            return False
        if any(path.resolve(strict=False) != path for path in roots):
            return False
        return all(any(target.resolve(strict=False).is_relative_to(root) for root in roots) for target in targets)
    except (OSError, RuntimeError, ValueError):
        return False


def capability_request_signature(value: object) -> tuple:
    return (
        _text_attr(value, "capability_type", default="generic").lower(),
        _text_attr(value, "needed_capability").lower(),
        _normalized_list_attr(value, "requested_tools"),
        _normalized_list_attr(value, "requested_skills"),
        _normalized_list_attr(value, "requested_mcp_tools"),
        _normalized_command_attr(value, "requested_commands"),
        _normalized_list_attr(value, "cwd_scope"),
        _normalized_list_attr(value, "path_scope"),
        _normalized_list_attr(value, "network_scope"),
        tuple(sorted(_object_dict_attr(value, "output_budget").items())),
        _text_attr(value, "risk_level").lower(),
    )


def _normalized_command_attr(value: object, name: str) -> tuple[str, ...]:
    commands = [_command_name(item).lower() for item in _list_attr(value, name)]
    return tuple(sorted(item for item in dict.fromkeys(commands) if item))


# LLM: 能力申请里的 command 字段是模型文本、可能超长；超长时不做 shlex（慢解析会卡线程），
#   按"无命令名"处理——fail-closed：签名不会匹配等价申请，删除类判定更保守。
# 函数用途: 从一条命令文本里提取可比较的命令名；解析失败或超长时返回空串。
def _command_name(value: object) -> str:
    text = str(value or "").strip()
    if not text or command_source_too_large(text):
        return ""
    try:
        parts = shlex.split(text)
    except ValueError:
        parts = text.split()
    return str(parts[0]).strip() if parts else ""


def _normalized_list_attr(value: object, name: str) -> tuple[str, ...]:
    items = [str(item).strip() for item in _list_attr(value, name)]
    return tuple(sorted(item for item in dict.fromkeys(items) if item))


def _list_attr(value: object, name: str) -> list:
    raw = getattr(value, name, None)
    if raw is None:
        return []
    return list(raw) if isinstance(raw, (list, tuple, set)) else [raw]


def _text_attr(value: object, name: str, *, default: str = "") -> str:
    raw = getattr(value, name, default)
    if raw is None:
        return default
    return str(raw or default).strip()


def _object_dict_attr(value: object, name: str) -> dict[str, object]:
    raw = getattr(value, name, None)
    if not isinstance(raw, dict):
        return {}
    return {str(key): item for key, item in raw.items()}
