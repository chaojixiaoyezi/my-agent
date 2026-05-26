# LLM: Deterministic subagent closeout rendering stays refs-first and avoids product body reads.
# 模块用途: 根据持久化 task 状态生成顶层收口说明、未完成提示和缺质量角色提示。

from __future__ import annotations

from .subagent_dispatch_closeout_resolution import blocking_task_ids, done_verified_count


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


# LLM: dispatch_incomplete_notice renders persisted blockers as a status summary.
# 函数用途: 有真实失败/阻塞任务时展示 task 状态摘要，避免口头完成掩盖已落盘问题。
def dispatch_incomplete_notice(tasks: list[object], blockers: list[str]) -> str:
    lines = [
        "---",
        "",
        "## 子代理状态摘要",
        "",
        "结论修正：子代理链路尚未完整通过，本轮不能按完成汇报。",
        "",
        f"- total_runs: {len(tasks)}",
        f"- done_verified: {done_verified_count(tasks)}",
        f"- blocking_run_ids: {', '.join(blockers) if blockers else '(none)'}",
        "",
        "Persisted task state:",
        *_task_status_rows(tasks)[:12],
        "",
        "Latest child summaries and refs:",
        *_task_summary_rows(tasks)[:12],
        "",
        "建议下一步：父级读取 blocking_run_ids 对应的 refs，决定重试、接管、重派或把阻塞原因上报给用户。",
    ]
    return "\n".join(lines)


# LLM: dispatch_missing_quality_roles_notice reports missing explicitly requested quality roles.
# 函数用途: 用户明确要求质量角色但没有运行时，展示缺口，让父级决定继续派工或说明限制。
def dispatch_missing_quality_roles_notice(tasks: list[object], missing_roles: list[str]) -> str:
    lines = [
        "---",
        "",
        "## 子代理状态摘要",
        "",
        "结论修正：用户要求的质量链路还没跑完，本轮不能按完成汇报。",
        f"缺少质量角色：{', '.join(missing_roles)}",
        f"缺少质量角色组合：{'/'.join(sorted(missing_roles))}",
        *_quality_role_alias_lines(missing_roles),
        "",
        f"- total_runs: {len(tasks)}",
        f"- done_verified: {done_verified_count(tasks)}",
        f"- missing_quality_roles: {', '.join(missing_roles)}",
        "",
        "Persisted task state:",
        *_task_status_rows(tasks)[:12],
        "",
        "Latest child summaries and refs:",
        *_task_summary_rows(tasks)[:12],
        "",
        "建议下一步：父级根据缺失角色决定是否调用 create_subagents 继续派工、调整验收范围，或把无法继续的原因上报给用户。",
    ]
    return "\n".join(lines)


def _quality_role_alias_lines(missing_roles: list[str]) -> list[str]:
    normalized = {str(role or "").strip().lower() for role in missing_roles}
    if {"tester", "acceptor"}.issubset(normalized):
        return ["缺少质量角色组合：tester/acceptor"]
    return []


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


# LLM: _task_summary_rows makes rework actionable without reading artifact bodies.
# 函数用途: 未完成收口时暴露每个子代理的最新摘要和 refs，避免父级只看到 run_id/status 后漏掉已产出的关键发现。
def _task_summary_rows(tasks: list[object]) -> list[str]:
    rows: list[str] = []
    for task in sorted(tasks, key=_task_sort_key):
        task_id = str(getattr(task, "id", "") or "")
        name = str(getattr(task, "agent_name", "") or "")
        summary = _bounded_inline(str(getattr(task, "latest_summary", "") or ""))
        refs = _short_refs([
            str(getattr(task, "output_json", "") or ""),
            *(getattr(task, "artifact_refs", []) or []),
            *(getattr(task, "evidence_refs", []) or []),
        ])
        rows.append(
            f"- `{task_id}` name={name or 'unnamed'} summary={summary or '(empty)'} refs={refs or '(none)'}"
        )
    return rows


def _bounded_inline(value: str, *, limit: int = 220) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _short_refs(values: list[object], *, limit: int = 3) -> str:
    refs: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in refs:
            refs.append(text)
        if len(refs) >= limit:
            break
    return ", ".join(refs)


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


# LLM: _output_refs keeps deterministic summaries traceable without reading big outputs.
# 函数用途: 收集每个任务 output.json 路径作为验收追踪入口；只列路径，不读取正文。
def _output_refs(tasks: list[object]) -> list[str]:
    refs: list[str] = []
    for task in tasks:
        ref = str(getattr(task, "output_json", "") or "")
        if ref and ref not in refs:
            refs.append(ref)
    return refs
