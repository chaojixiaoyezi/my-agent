"""创建子代理前，对调用方显式声明的输入路径做子代理可见性预检。"""

# LLM: 预检只读调用方显式给出的结构化输入字段（input_refs / input_files / required_read_paths /
#   context_manifest.required_read_paths；批量 item 在并入 goal 文本扒出的路径之前已记下），绝不解析 goal 正文。
#   可见性裁决复用子代理工具真实使用的同一条链：prepare_run 的准备任务 → runner 写边界 →
#   write_boundary_with_runtime_ledger 按 task_local 合并 → granted_external_work_roots →
#   PathAccessPolicy.check_with_external_roots；相对路径按同一 execution_cwd 解析，不另写路径规则。
#   看不到就整批拒绝（未创建任何 run），与越界 output_files、无效接管的结构化拒绝同一约定；
#   无 owner 墙或结构化事实无法取得时不预检，不能凭猜测拦主链路。
# 模块用途: 在派出子代理之前发现它根本读不到的输入文件，让父代理先把文件放进工作区或把内容写进任务。
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

from ...path_access_policy import (
    PathAccessDecision,
    PathAccessPolicy,
    effective_owner_scope_root,
    granted_external_work_roots,
)
from ...subagents.context_bundle_refs import runtime_task_attributes
from ...tooling.registry_workspace import effective_registry_cwd
from ...user_space.approval_mode import permission_config
from ..runner.ref_fields import explicit_input_refs
from ..tool_runtime_ledger import write_boundary_with_runtime_ledger

INPUT_PATH_NOT_VISIBLE_ERROR_CODE = "SUBAGENT_INPUT_PATH_NOT_VISIBLE"
# 用户指定的大白话提示：父代理看到后应先把文件放进工作区或把内容直接写进任务。
INPUT_PATH_NOT_VISIBLE_HINT = (
    "该路径不在子代理可见范围（只能访问你的工作区和 shared 区），"
    "请先把需要的文件放进工作区，或直接把内容写进任务。"
)
# 回执里单个路径文本的长度上限，以及最多列出的不可见输入条数；超出只计数，不塞进回执。
_REF_TEXT_LIMIT_CHARS = 300
# 读作用域回执最多列出的不可见输入问题条数，超出只计数。
_ISSUE_COUNT = 20
# 预检中结构化事实取不到时视为无法判定，只放行不拦截；这些是准备链路可能抛出的已知类型。
_SCOPE_UNAVAILABLE_ERRORS = (OSError, ValueError, TypeError, KeyError, AttributeError)


# LLM: 冻结的是子代理运行时同源事实：owner 墙根、相对路径起点、墙外已授权根和同一 PathAccessPolicy；不持有可变状态。
# 类用途: 表示一个待创建子代理能读到哪些路径，并对单个输入路径给出结构化裁决。
@dataclass(frozen=True)
class ChildReadScope:
    owner_root: Path
    cwd: Path
    granted_roots: tuple[Path, ...]
    policy: PathAccessPolicy

    # LLM: 解析方式与 FileSystemTool.resolve_path 一致（展开 ~、相对路径接 cwd、resolve），
    #   裁决与 FileSystemTool.check_path_access 一致；不读文件内容、不判断文件是否存在。
    # 函数用途: 判断一个输入路径子代理能否读取，返回解析后的路径和结构化裁决。
    def check(self, ref: str) -> tuple[Path, PathAccessDecision]:
        candidate = Path(ref).expanduser()
        if not candidate.is_absolute():
            candidate = self.cwd / candidate
        try:
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError):
            return candidate, PathAccessDecision(False, "PATH_RESOLUTION_FAILED", "路径解析失败，请检查路径是否有效。")
        return resolved, self.policy.check_with_external_roots(resolved, self.granted_roots)

    # LLM: 只列出本 scope 已冻结的根，供回执告诉父代理可以把文件放到哪里；不是新的授权。
    # 函数用途: 返回子代理可访问的根目录列表（自己的工作区、shared 区和已授权的墙外根）。
    def visible_roots(self) -> list[str]:
        roots = [str(self.owner_root)]
        home = self.policy.agent_home_root
        if home is not None:
            roots.append(str(home / "shared"))
        roots.extend(str(root) for root in self.granted_roots)
        return list(dict.fromkeys(roots))


# LLM: owner 墙只来自 SubAgentManager.owner_scope_root（经唯一权威 effective_owner_scope_root 按 task_local 取值，与运行时相同），
#   非字符串或空值说明没有可判定的墙，返回 None；准备任务、写边界与路径模式全部走子代理运行时的同一函数。
# 函数用途: 按一个待创建子代理的创建参数，算出它运行时真正能看到的读取范围。
def child_read_scope(agent: object, run_params: object) -> ChildReadScope | None:
    manager = getattr(agent, "subagents", None)
    owner_scope = getattr(manager, "owner_scope_root", "")
    if manager is None or not isinstance(owner_scope, str) or not owner_scope.strip():
        return None
    task = manager.base_service.prepare_run(params=run_params).task
    boundary = write_boundary_with_runtime_ledger(agent, _child_tool_call_params(manager, task))
    scope_root = effective_owner_scope_root(agent, context_scope="task_local", write_boundary=boundary)
    config = permission_config(agent.config, agent.home_paths, inherited=True)
    policy = PathAccessPolicy.from_values(
        mode=getattr(config, "path_access_mode", "normal"),
        dangerous_roots=getattr(config, "path_dangerous_roots", None),
        owner_scope_root=scope_root,
    )
    owner_root = Path(scope_root).expanduser().resolve(strict=False)
    return ChildReadScope(
        owner_root=owner_root,
        cwd=effective_registry_cwd(owner_root, boundary),
        granted_roots=granted_external_work_roots(boundary, scope_root),
        policy=policy,
    )


# LLM: items_params 与 task_params 一一对应且同序；只检查显式输入，复用同一 child_read_scope，不创建任何 run。
# 函数用途: 汇总一批待创建子代理里读不到的输入路径；全部可见时返回 None，否则返回结构化整批拒绝内容。
def input_read_scope_failure(
    agent: object,
    items_params: list[dict[str, object]],
    task_params: list[object],
) -> dict[str, object] | None:
    children: list[dict[str, object]] = []
    for index, (params, run_params) in enumerate(zip(items_params, task_params, strict=True)):
        refs = explicit_input_refs(params) if isinstance(params, dict) else []
        if refs and (child := _invisible_child_inputs(agent, index, refs, run_params)):
            children.append(child)
    if not children:
        return None
    return _failure_payload(children)


# LLM: 只把已知的准备链路异常当作"无法判定"；判定出的不可见路径按输入顺序截断到上限。
# 函数用途: 预检单个子代理的显式输入；结构化事实取不到时返回空（不拦截）。
def _invisible_child_inputs(
    agent: object,
    index: int,
    refs: list[str],
    run_params: object,
) -> dict[str, object]:
    try:
        scope = child_read_scope(agent, run_params)
    except _SCOPE_UNAVAILABLE_ERRORS:
        return {}
    if scope is None:
        return {}
    invisible = [issue for ref in refs if (issue := _ref_issue(scope, ref))]
    if not invisible:
        return {}
    return {
        "index": index,
        "invisible_refs": invisible[:_ISSUE_COUNT],
        "omitted_ref_count": max(0, len(invisible) - _ISSUE_COUNT),
        "visible_roots": scope.visible_roots(),
    }


# LLM: 原因码直接取 PathAccessDecision.code（PATH_OWNER_SCOPE_BLOCKED / PATH_CROSS_OWNER_BLOCKED 等），不改写、不合并。
# 函数用途: 把一个读不到的输入路径整理成回执条目，可读时返回空。
def _ref_issue(scope: ChildReadScope, ref: str) -> dict[str, str]:
    resolved, decision = scope.check(ref)
    if decision.allowed:
        return {}
    return {
        "ref": ref[:_REF_TEXT_LIMIT_CHARS],
        "resolved_path": str(resolved)[:_REF_TEXT_LIMIT_CHARS],
        "reason_code": decision.code or "PATH_ACCESS_DENIED",
    }


# LLM: 与 SUBAGENT_PLANNED_DELEGATION_INVALID 同形：ok=False、明确"本批没有创建任何子代理"、给出修复选项与重试工具。
# 函数用途: 组装整批拒绝回执，用大白话告诉父代理这些路径子代理看不到、该怎么修。
def _failure_payload(children: list[dict[str, object]]) -> dict[str, object]:
    return {
        "ok": False,
        "error_code": INPUT_PATH_NOT_VISIBLE_ERROR_CODE,
        "error": INPUT_PATH_NOT_VISIBLE_HINT + "本批没有创建任何子代理。",
        "invisible_inputs": children,
        "next_action": {
            "action": "repair_input_refs_and_retry",
            "retry_tool": "create_subagents",
            "options": [
                "copy_needed_files_into_workspace",
                "inline_needed_content_in_goal",
                "drop_invisible_input_refs",
            ],
            "preserve_user_constraints": True,
        },
    }


# LLM: 字段与子代理 runner 调用工具时交给 write_boundary_with_runtime_ledger 的参数一致（task_local、runner 写边界、
#   runtime_task_attributes）；只做内存投影，prepare_execution_context 不落盘。
# 函数用途: 为预检构造一次"子代理工具调用"的最小参数，让写边界合并走运行时同一函数。
def _child_tool_call_params(manager: object, task: object) -> SimpleNamespace:
    context = manager.runner_context.prepare_execution_context(task)
    return SimpleNamespace(
        context_scope="task_local",
        write_boundary=dict(context.write_boundary),
        task_attributes=runtime_task_attributes(context.task),
        run_id=str(getattr(task, "id", "") or ""),
        source="subagent_runner",
        delivery_contract=None,
        runtime_approved_actions=[],
    )


__all__ = [
    "INPUT_PATH_NOT_VISIBLE_ERROR_CODE",
    "INPUT_PATH_NOT_VISIBLE_HINT",
    "ChildReadScope",
    "child_read_scope",
    "input_read_scope_failure",
]
