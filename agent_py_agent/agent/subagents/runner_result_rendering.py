# LLM: Runner result rendering is split from the larger subagent report renderer.
# 模块用途: 专门渲染 RUNNER_RESULT.md，让执行上下文、通道探测和 runner 结果的展示边界清楚。

from __future__ import annotations

from .models import SubAgentRunnerResult


# LLM: render_runner_result_markdown renders one persisted runner closeout report.
# 函数用途: 把 runner 状态、结构化输出、父级下一步和关键文件 refs 渲染成 Markdown。
def render_runner_result_markdown(result: SubAgentRunnerResult) -> str:
    status = "OK" if result.ok else "FAIL"
    mode = "dry-run" if result.dry_run else "execute"
    lines = _runner_result_header_lines(result, mode, status)
    lines.extend(_runner_structured_output_lines(result))
    lines.extend(_runner_parent_next_action_lines(result))
    lines.extend(_runner_result_file_lines(result))
    return "\n".join(lines) + "\n"


# LLM: _runner_result_header_lines keeps the top metadata stable for humans and parent agents.
# 函数用途: 渲染 RUNNER_RESULT.md 的固定头部字段，便于父级快速判断 run 状态。
def _runner_result_header_lines(
    result: SubAgentRunnerResult,
    mode: str,
    status: str,
) -> list[str]:
    return [
        "# SUBAGENT RUNNER RESULT",
        "",
        f"- run_id: {result.run_id}",
        f"- created_at: {result.created_at}",
        f"- mode: {mode}",
        f"- status: {result.status}",
        f"- verification_status: {result.verification_status}",
        f"- ok: {status}",
        f"- backend: {result.backend or 'none'}",
        f"- tool_rounds: {result.tool_rounds}",
        f"- runner_attempts: {result.runner_attempts}",
        f"- runner_last_error: {result.runner_last_error or 'none'}",
        f"- structured_output_found: {result.structured_output_found}",
        f"- structured_output_ok: {result.structured_output_ok}",
        f"- structured_repair_attempted: {result.structured_repair_attempted}",
        f"- structured_repair_ok: {result.structured_repair_ok}",
        f"- evidence_count: {result.evidence_count}",
        f"- capability_request_count: {result.capability_request_count}",
        f"- artifact_count: {result.artifact_count}",
        f"- test_count: {result.test_count}",
        f"- patch_count: {result.patch_count}",
        f"- lesson_count: {result.lesson_count}",
        "",
        "## Message",
        "",
        result.message or "none",
    ]


# LLM: _runner_structured_output_lines shows parsed output without expanding artifact bodies.
# 函数用途: 渲染 summary、blocked_reason 和 parse/repair error，保留结构化事实源。
def _runner_structured_output_lines(result: SubAgentRunnerResult) -> list[str]:
    if result.structured_summary or result.blocked_reason or result.structured_parse_error:
        lines = ["", "## Structured Output", ""]
        if result.structured_summary:
            lines.append(f"- summary: {result.structured_summary}")
        if result.blocked_reason:
            lines.append(f"- blocked_reason: {result.blocked_reason}")
        if result.structured_parse_error:
            lines.append(f"- parse_error: {result.structured_parse_error}")
        if result.structured_repair_error:
            lines.append(f"- repair_error: {result.structured_repair_error}")
        return lines
    return []


# LLM: _runner_parent_next_action_lines turns machine blockers into clear parent-facing repair steps.
# 函数用途: 子代理因产物结构失败阻塞时，明确告诉父级继续派修复子代理，避免 root 读完报告后亲自改业务文件。
def _runner_parent_next_action_lines(result: SubAgentRunnerResult) -> list[str]:
    blocked = " ".join([result.blocked_reason or "", result.structured_summary or ""]).lower()
    if "artifact_integrity_failed" not in blocked:
        return []
    return [
        "",
        "## Parent Next Action",
        "",
        "- artifact_integrity_failed 表示产物还没过结构检查，不能进入父级验收。",
        "- 父级/root 不要直接改业务产物；请派 repair worker/修复小傻妞读取 output_json 和产物路径继续修。",
        "- 修复后重新 dispatch，并再次检查 artifact integrity 与父级验收。",
    ]


# LLM: _runner_result_file_lines lists refs the parent should read instead of product bodies.
# 函数用途: 渲染执行上下文、prompt/response、runner_result 和 output_json 等小型文件引用。
def _runner_result_file_lines(result: SubAgentRunnerResult) -> list[str]:
    return [
        "",
        "## Files",
        "",
        f"- execution_context_json: {result.execution_context_json}",
        f"- execution_context_file: {result.execution_context_file}",
        f"- prompt_file: {result.prompt_file or 'none'}",
        f"- response_file: {result.response_file or 'none'}",
        f"- result_json: {result.result_json}",
        f"- output_json: {result.output_json}",
    ]


__all__ = ["render_runner_result_markdown"]
