# LLM: Deterministic subagent closeout rendering stays refs-first and avoids product body reads.
# 模块用途: 根据持久化 task 状态生成顶层收口说明、未完成提示和缺质量角色提示。

from __future__ import annotations

from .subagent_dispatch_closeout_resolution import blocking_task_ids, done_verified_count


# LLM: dispatch_completion_text keeps final top-level output refs-first and compact.
# 函数用途: 从已验收任务生成用户可读收尾说明，列出 root、任务数和 output.json 引用。
def dispatch_completion_text(tasks: list[object]) -> str:
    refs = _output_refs(tasks)
    artifact_refs = _artifact_refs(tasks)
    lines = _dispatch_completion_header(tasks)
    if artifact_refs:
        lines.append("- artifact_refs:")
        lines.extend(f"  - {ref}" for ref in artifact_refs[:12])
    if refs:
        lines.append("- output_json_refs:")
        lines.extend(f"  - {ref}" for ref in refs[:12])
    return "\n".join(lines)


# LLM: dispatch_limit_text is a factual report for incomplete or failed subagent trees.
# 函数用途: 工具轮数到顶时输出真实状态、阻塞 run_id 和引用路径。
def dispatch_limit_text(tasks: list[object], *, reason: str = "tool_limit") -> str:
    rows = _task_status_rows(tasks)
    blockers = blocking_task_ids(tasks)
    lines = [
        _dispatch_fallback_reason_text(reason),
        "",
        "结论：子代理链路尚未完整通过，不能按完成汇报。" if blockers else "结论：未发现阻塞状态，但本轮是工具上限收口，请按下方真实状态复核。",
        "",
        f"- total_runs: {len(tasks)}",
        f"- done_verified: {done_verified_count(tasks)}",
        f"- blocking_run_ids: {', '.join(blockers) if blockers else '(none)'}",
        "",
        "## Persisted Task State",
        "",
        *rows[:24],
    ]
    _append_output_refs(lines, tasks)
    if blockers:
        lines.extend(_required_next_action_lines())
    return "\n".join(lines)


# LLM: dispatch_incomplete_notice replaces over-optimistic final model text.
# 函数用途: 用真实 task 状态替换最终汇报，确保有阻塞时用户先看到未完成事实。
def dispatch_incomplete_notice(tasks: list[object], blockers: list[str]) -> str:
    lines = [
        "---",
        "",
        "## Subagent State Notice",
        "",
        "结论修正：子代理链路尚未完整通过，不能按完成汇报。",
        "",
        f"- total_runs: {len(tasks)}",
        f"- done_verified: {done_verified_count(tasks)}",
        f"- blocking_run_ids: {', '.join(blockers) if blockers else '(none)'}",
        "",
        "Persisted task state:",
        *_task_status_rows(tasks)[:12],
        "",
        "建议下一步：继续让父级基于 blocking_run_ids 做 retry、takeover、repair 或验收复核；不要只因为产物文件存在就认为整条子代理恢复链路已通过。",
    ]
    return "\n".join(lines)


# LLM: dispatch_missing_quality_roles_notice blocks false completion when requested roles never ran.
# 函数用途: 当前任务都绿但缺用户要求的测试/验收角色时，告诉 root 继续派质量子代理。
def dispatch_missing_quality_roles_notice(tasks: list[object], missing_roles: list[str]) -> str:
    lines = [
        "---",
        "",
        "## Subagent State Notice",
        "",
        "结论修正：用户要求的质量链路还没跑完，不能按完成汇报。",
        "",
        f"- total_runs: {len(tasks)}",
        f"- done_verified: {done_verified_count(tasks)}",
        f"- missing_quality_roles: {', '.join(missing_roles)}",
        "",
        "Persisted task state:",
        *_task_status_rows(tasks)[:12],
        "",
        "建议下一步：顶层 root 请继续调用 create_subagents 创建缺少的 tester/acceptor，然后再调用 dispatch_subagents 调度这些质量子代理；不要使用 schedule_child_subagents，因为它只给已经处在子代理 runner 内的父级使用。",
    ]
    return "\n".join(lines)


# LLM: _append_output_refs keeps summaries traceable without reading big outputs.
# 函数用途: 给状态报告追加 output.json 路径引用，方便后续人工或 agent 核验。
def _append_output_refs(lines: list[str], tasks: list[object]) -> None:
    refs = _output_refs(tasks)
    if refs:
        lines.extend(["", "## Output Refs", ""])
        lines.extend(f"- {ref}" for ref in refs[:12])


# LLM: _required_next_action_lines gives a stable remediation block for blocked trees.
# 函数用途: 提醒父级使用 refs 和接管链继续恢复，不要口头报喜。
def _required_next_action_lines() -> list[str]:
    return [
        "",
        "## Required Next Action",
        "",
        "- 先读取 blocking_run_ids 的 runner_result、failure_handoff、acceptance_review，再由父级接管、重试或重派。",
        "- 不要把本轮说成完成；页面产物存在不等于子代理层级、角色覆盖和验收链路已经通过。",
    ]


# LLM: _dispatch_fallback_reason_text keeps deterministic status reports honest.
# 函数用途: 区分工具轮数耗尽和最终模型空响应两类收口原因。
def _dispatch_fallback_reason_text(reason: str) -> str:
    if reason == "empty_model_response":
        return "模型接口最终总结返回空文本，系统根据本地 subagent task.json 直接生成状态报告，未让本轮崩溃。"
    return "已达到最大工具轮数限制，系统根据本地 subagent task.json 直接生成状态报告，未让模型继续自由总结。"


# LLM: _dispatch_completion_header renders stable counters without touching output bodies.
# 函数用途: 生成本地收尾回答固定头部，帮助用户快速定位总数和 root 节点。
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
# 函数用途: 汇总每个子代理真实 id、role、name、depth、status 和 task_dir。
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


# LLM: _root_task_ids extracts top-level subagent ids for deterministic final summaries.
# 函数用途: 找出 parent_id 为空或等于自身的 root 节点 id。
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


# LLM: _artifact_refs exposes deliverable refs in deterministic closeout without reading bodies.
# 函数用途: 收集已完成子代理登记的业务产物路径，让 root 本地收口也能交付可读报告 refs。
def _artifact_refs(tasks: list[object]) -> list[str]:
    refs: list[str] = []
    for task in sorted(tasks, key=_task_sort_key):
        _append_user_artifact_refs(refs, getattr(task, "artifact_refs", []) or [])
    return refs


# LLM: _append_user_artifact_refs keeps filtering separate from sorted task traversal.
# 函数用途: 只追加可交付产物 ref，过滤 output/checkpoint/report 等内部状态文件。
def _append_user_artifact_refs(target: list[str], refs: list[object]) -> None:
    for ref in refs:
        text = str(ref or "").strip()
        if text and _looks_like_user_artifact(text) and text not in target:
            target.append(text)


# LLM: _looks_like_user_artifact filters internal state refs from closeout deliverable refs.
# 函数用途: 避免把 output.json、checkpoint、运行审计报告当作用户最终产物展示。
def _looks_like_user_artifact(ref: str) -> bool:
    text = str(ref or "").strip().replace("\\", "/")
    if not text:
        return False
    lower = text.lower()
    if lower.endswith("/output.json") or lower == "output.json":
        return False
    blocked_parts = (
        "/reports/",
        "/memory_archive/",
        "/agent_run/",
        "/compactions/",
        "/checkpoint",
        "/takeover_readiness",
    )
    return not any(part in lower for part in blocked_parts)
