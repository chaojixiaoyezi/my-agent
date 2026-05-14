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
    if _prompt_requires_uncreated_quality_roles(request.params.user_prompt, tasks):
        return None
    return ModelResponse(text=_dispatch_completion_text(tasks), backend=request.backend)


# LLM: subagent_dispatch_limit_response prevents final user reports from inventing subagent status.
# 函数用途: 顶层工具轮数耗尽时，直接按 task.json 生成事实状态报告，不再让模型自由总结失败链路。
def subagent_dispatch_limit_response(agent, *, backend: str, reason: str = "tool_limit") -> ModelResponse | None:
    if _inside_subagent_runner(agent):
        return None
    tasks = _subagent_tasks(agent)
    if not tasks:
        return None
    return ModelResponse(text=_dispatch_limit_text(tasks, reason=reason), backend=backend)


# LLM: subagent_dispatch_final_response_guard makes the final user answer reflect persisted task state.
# 函数用途: 顶层主代理用过子代理编排工具后，如果 task.json 仍有阻塞，就用事实报告替换模型草稿，防止先报喜再纠正。
def subagent_dispatch_final_response_guard(
    agent,
    response: ModelResponse | None,
    *,
    executed_tools: list[object],
) -> ModelResponse | None:
    if response is None or _inside_subagent_runner(agent):
        return response
    if not _executed_orchestration(executed_tools):
        return response
    tasks = _subagent_tasks(agent)
    blockers = _blocking_task_ids(tasks)
    if not blockers:
        return response
    notice = _dispatch_incomplete_notice(tasks, blockers)
    text = str(getattr(response, "text", "") or "")
    if text.strip() == notice.strip():
        return response
    return ModelResponse(text=notice, backend=response.backend)


# LLM: _executed_orchestration mirrors the top-level tool-loop check without importing the service.
# 函数用途: 判断本轮是否执行过 dispatch；单纯 create/board 只是中间状态，不覆盖诚实的等待调度回答。
def _executed_orchestration(executed_tools: list[object]) -> bool:
    return "dispatch_subagents" in [str(item or "") for item in executed_tools or []]


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


# LLM: _prompt_requires_uncreated_quality_roles preserves explicit tester/acceptor workflow contracts.
# 函数用途: 用户要求 worker 后继续创建测试/验收角色时，顶层不能因现有任务全绿而提前本地收口。
def _prompt_requires_uncreated_quality_roles(prompt: str, tasks: list[object]) -> bool:
    required = _required_quality_roles(prompt)
    if not required:
        return False
    present = _present_role_tokens(tasks)
    return any(role not in present for role in required)


# LLM: _required_quality_roles reads only explicit role-style requirements, not generic quality prose.
# 函数用途: 从当前用户 prompt 判断是否明确要求 tester/acceptor 角色，避免普通“测试一下”误伤本地收口。
def _required_quality_roles(prompt: str) -> set[str]:
    text = " ".join(str(prompt or "").lower().split())
    if not text:
        return set()
    required: set[str] = set()
    if "tester" in text or "测试子代理" in text or "测试代理" in text:
        required.add("tester")
    if "acceptor" in text or "验收子代理" in text or "验收代理" in text:
        required.add("acceptor")
    return required


# LLM: _present_role_tokens normalizes role/name fields enough for deterministic closeout gating.
# 函数用途: 汇总已有子代理的 role 和名字关键词，判断 tester/acceptor 是否已经真正创建过。
def _present_role_tokens(tasks: list[object]) -> set[str]:
    present: set[str] = set()
    for task in tasks:
        text = " ".join(
            [
                str(getattr(task, "role", "") or ""),
                str(getattr(task, "agent_name", "") or ""),
            ]
        ).lower()
        if "tester" in text or "test" in text or "测试" in text:
            present.add("tester")
        if "acceptor" in text or "accept" in text or "验收" in text:
            present.add("acceptor")
    return present


# LLM: _dispatch_completion_text keeps final top-level output refs-first and compact.
# 函数用途: 从已验收任务生成用户可读收尾说明，列出 root、任务数和 output.json 引用，不复述长日志。
def _dispatch_completion_text(tasks: list[object]) -> str:
    refs = _output_refs(tasks)
    lines = _dispatch_completion_header(tasks)
    if refs:
        lines.append("- output_json_refs:")
        lines.extend(f"  - {ref}" for ref in refs[:12])
    return "\n".join(lines)


# LLM: _dispatch_limit_text is a refs-first factual report for incomplete or failed subagent trees.
# 函数用途: 工具轮数到顶时输出真实状态、阻塞 run_id 和引用路径，避免模型把 TIMEOUT/BLOCKED 说成完成。
def _dispatch_limit_text(tasks: list[object], *, reason: str = "tool_limit") -> str:
    rows = _task_status_rows(tasks)
    blockers = _blocking_task_ids(tasks)
    lines = [
        _dispatch_fallback_reason_text(reason),
        "",
        "结论：子代理链路尚未完整通过，不能按完成汇报。" if blockers else "结论：未发现阻塞状态，但本轮是工具上限收口，请按下方真实状态复核。",
        "",
        f"- total_runs: {len(tasks)}",
        f"- done_verified: {_done_verified_count(tasks)}",
        f"- blocking_run_ids: {', '.join(blockers) if blockers else '(none)'}",
        "",
        "## Persisted Task State",
        "",
    ]
    lines.extend(rows[:24])
    refs = _output_refs(tasks)
    if refs:
        lines.extend(["", "## Output Refs", ""])
        lines.extend(f"- {ref}" for ref in refs[:12])
    if blockers:
        lines.extend(
            [
                "",
                "## Required Next Action",
                "",
                "- 先读取 blocking_run_ids 的 runner_result、failure_handoff、acceptance_review，再由父级接管、重试或重派。",
                "- 不要把本轮说成完成；页面产物存在不等于子代理层级、角色覆盖和验收链路已经通过。",
            ]
        )
    return "\n".join(lines)


# LLM: _dispatch_incomplete_notice replaces over-optimistic final model text.
# 函数用途: 用真实 task 状态替换最终汇报，确保有阻塞时用户先看到未完成事实而不是模型自述成功。
def _dispatch_incomplete_notice(tasks: list[object], blockers: list[str]) -> str:
    lines = [
        "---",
        "",
        "## Subagent State Notice",
        "",
        "结论修正：子代理链路尚未完整通过，不能按完成汇报。",
        "",
        f"- total_runs: {len(tasks)}",
        f"- done_verified: {_done_verified_count(tasks)}",
        f"- blocking_run_ids: {', '.join(blockers) if blockers else '(none)'}",
        "",
        "Persisted task state:",
    ]
    lines.extend(_task_status_rows(tasks)[:12])
    lines.extend(
        [
            "",
            "建议下一步：继续让父级基于 blocking_run_ids 做 retry、takeover、repair 或验收复核；"
            "不要只因为产物文件存在就认为整条子代理恢复链路已通过。",
        ]
    )
    return "\n".join(lines)


# LLM: _dispatch_fallback_reason_text keeps deterministic subagent status reports honest.
# 函数用途: 区分工具轮数耗尽和最终模型空响应两类收口原因，避免 E2E 报告误导用户。
def _dispatch_fallback_reason_text(reason: str) -> str:
    if reason == "empty_model_response":
        return "模型接口最终总结返回空文本，系统根据本地 subagent task.json 直接生成状态报告，未让本轮崩溃。"
    return "已达到最大工具轮数限制，系统根据本地 subagent task.json 直接生成状态报告，未让模型继续自由总结。"


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


# LLM: _task_status_rows renders short task facts from persisted fields only.
# 函数用途: 汇总每个子代理真实 id、role、name、depth、status 和 task_dir，不读取大日志正文。
def _task_status_rows(tasks: list[object]) -> list[str]:
    lines: list[str] = []
    for task in sorted(tasks, key=_task_sort_key):
        task_id = str(getattr(task, "id", "") or "")
        status = str(getattr(task, "status", "") or "UNKNOWN")
        verification = str(getattr(task, "verification_status", "") or "UNKNOWN")
        role = str(getattr(task, "role", "") or "")
        name = str(getattr(task, "agent_name", "") or "")
        depth = str(getattr(task, "depth", "") or 0)
        parent = str(getattr(task, "parent_id", "") or "")
        child_count = len(getattr(task, "child_ids", []) or [])
        task_dir = str(getattr(task, "task_dir", "") or "")
        lines.append(
            f"- `{task_id}` depth={depth} role={role or 'unknown'} name={name or 'unnamed'} "
            f"status={status}/{verification} parent={parent or '(root)'} children={child_count} task_dir={task_dir}"
        )
    return lines


# LLM: _task_sort_key keeps factual reports stable across filesystem ordering.
# 函数用途: 按 depth、创建时间和 id 排序，便于对比 E2E 日志。
def _task_sort_key(task: object) -> tuple[int, float, str]:
    try:
        depth = int(getattr(task, "depth", 0) or 0)
    except (TypeError, ValueError):
        depth = 0
    try:
        created = float(getattr(task, "created_at", 0.0) or 0.0)
    except (TypeError, ValueError):
        created = 0.0
    return depth, created, str(getattr(task, "id", "") or "")


# LLM: _done_verified_count counts only persisted DONE + VERIFIED rows.
# 函数用途: 给确定性报告提供严格完成数，不能把 AWAITING_ACCEPTANCE 或模型自述算完成。
def _done_verified_count(tasks: list[object]) -> int:
    return sum(
        1
        for task in tasks
        if str(getattr(task, "status", "") or "") == "DONE"
        and str(getattr(task, "verification_status", "") or "") == "VERIFIED"
    )


# LLM: _blocking_task_ids identifies run ids that make the whole hierarchy not complete.
# 函数用途: 找出所有尚未 DONE/VERIFIED 的节点，供父级恢复、重试、验收或接管使用。
def _blocking_task_ids(tasks: list[object]) -> list[str]:
    blockers: list[str] = []
    for task in tasks:
        status = str(getattr(task, "status", "") or "").upper()
        verification = str(getattr(task, "verification_status", "") or "").upper()
        task_id = str(getattr(task, "id", "") or "")
        if status != "DONE" or verification != "VERIFIED":
            blockers.append(task_id)
    return [item for item in blockers if item]


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
