# LLM: Top-level subagent dispatch closeout helpers avoid extra final model calls after all work is verified.
# 模块用途: 当顶层主代理调度的子代理全部 DONE/VERIFIED 时，生成本地收尾回答，避免完成后再请求模型卡住。

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from ..backends import ModelResponse
from ._runtime_params import ToolLoopExecuteParams


# LLM: DispatchCompletionRequest bundles the deterministic closeout inputs for bundle-interface rules.
# 类用途: 集中保存顶层 dispatch 收口判断所需上下文；调用方只传一个参数包，避免扩散散参。
@dataclass(frozen=True)
class DispatchCompletionRequest:
    __test__: ClassVar[bool] = False

    agent: object
    params: ToolLoopExecuteParams
    before_executed_count: int
    backend: str


# LLM: subagent_dispatch_completion_response closes top-level dispatch without another model call.
# 函数用途: 顶层主代理刚完成 dispatch_subagents 且全部子代理 DONE/VERIFIED 时，直接生成确定性收尾说明。
def subagent_dispatch_completion_response(request: DispatchCompletionRequest) -> ModelResponse | None:
    if _inside_subagent_runner(request.agent) or not _round_executed_dispatch(request):
        return None
    tasks = _subagent_tasks(request.agent)
    if not tasks or not _all_tasks_done_verified(tasks):
        return None
    return ModelResponse(text=_dispatch_completion_text(tasks), backend=request.backend)


# LLM: _inside_subagent_runner keeps runner closeout controlled by output.json only.
# 函数用途: 判断当前是否处在某个子代理 runner 内；runner 内不能用顶层 dispatch 收口替代 output.json 契约。
def _inside_subagent_runner(agent) -> bool:
    return bool(str(getattr(agent, "_current_subagent_run_id", "") or ""))


# LLM: _round_executed_dispatch looks only at tools executed in the just-finished round.
# 函数用途: 判断刚结束的工具轮是否真实执行过 dispatch_subagents，避免普通看板查询被误当成收尾信号。
def _round_executed_dispatch(request: DispatchCompletionRequest) -> bool:
    return "dispatch_subagents" in [
        str(item or "") for item in request.params.executed_tools[request.before_executed_count:]
    ]


# LLM: _subagent_tasks reads the manager list defensively for top-level closeout.
# 函数用途: 安全读取当前 workspace 的子代理任务列表；manager 不可用时保守返回空，不影响常规模型流程。
def _subagent_tasks(agent) -> list[object]:
    try:
        return list(agent.subagents.list_runs())
    except Exception:
        return []


# LLM: _all_tasks_done_verified gates deterministic closeout on the persisted task state.
# 函数用途: 只有所有任务状态都是 DONE 且 verification_status 是 VERIFIED 时才允许跳过额外模型请求。
def _all_tasks_done_verified(tasks: list[object]) -> bool:
    for task in tasks:
        if str(getattr(task, "status", "") or "") != "DONE":
            return False
        if str(getattr(task, "verification_status", "") or "") != "VERIFIED":
            return False
    return True


# LLM: _dispatch_completion_text keeps final top-level output refs-first and compact.
# 函数用途: 从已验收任务生成用户可读收尾说明，列出 root、任务数和 output.json 引用，不复述长日志。
def _dispatch_completion_text(tasks: list[object]) -> str:
    refs = _output_refs(tasks)
    lines = _dispatch_completion_header(tasks)
    if refs:
        lines.append("- output_json_refs:")
        lines.extend(f"  - {ref}" for ref in refs[:12])
    return "\n".join(lines)


# LLM: _dispatch_completion_header renders stable counters without touching output bodies.
# 函数用途: 生成本地收尾回答的固定头部，帮助用户快速定位总数和 root 节点。
def _dispatch_completion_header(tasks: list[object]) -> list[str]:
    roots = _root_task_ids(tasks)
    return [
        "子代理调度已完成，系统根据本地任务状态直接收口，未再发起额外模型请求。",
        "",
        f"- total_runs: {len(tasks)}",
        f"- done_verified: {len(tasks)}",
        f"- root_run_ids: {', '.join(roots) if roots else '(none)'}",
    ]


# LLM: _root_task_ids extracts top-level subagent ids for deterministic final summaries.
# 函数用途: 找出 parent_id 为空或等于自身的 root 节点 id，方便用户定位主链路。
def _root_task_ids(tasks: list[object]) -> list[str]:
    roots: list[str] = []
    for task in tasks:
        task_id = str(getattr(task, "id", "") or "")
        parent_id = str(getattr(task, "parent_id", "") or "")
        if task_id and (not parent_id or parent_id == task_id):
            roots.append(task_id)
    return roots


# LLM: _output_refs keeps deterministic summaries traceable without reading big outputs.
# 函数用途: 收集每个任务 output.json 路径作为验收追踪入口；只列路径，不读取正文。
def _output_refs(tasks: list[object]) -> list[str]:
    refs: list[str] = []
    for task in tasks:
        ref = str(getattr(task, "output_json", "") or "")
        if ref and ref not in refs:
            refs.append(ref)
    return refs
