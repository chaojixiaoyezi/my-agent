# LLM: Subagent orchestration module; keep task workspace, manager facade, and report contracts stable.
# 模块用途: 支撑主代理派发、跟踪、验收、汇总子代理任务。

from __future__ import annotations

"""Payload assembly for subagent runner result files."""

from .policies import RunnerNextActionParams, _runner_next_action
from .result_contexts import OutputPayloadContext


# LLM: _build_structured_output_payload 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 构建structuredoutput载荷所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _build_structured_output_payload(ctx: OutputPayloadContext) -> dict[str, object]:
    return {
        "found": ctx.parsed.found,
        "ok": ctx.parsed.ok,
        "parse_error": ctx.parsed.parse_error,
        "repair_attempted": ctx.structured_repair_attempted,
        "repair_ok": ctx.structured_repair_ok,
        "repair_error": ctx.structured_repair_error,
        "summary": ctx.parsed.summary,
        "status": ctx.parsed.status,
        "blocked_reason": ctx.parsed.blocked_reason,
        "evidence_count": ctx.structured_evidence_count,
        "capability_request_count": ctx.structured_request_count,
        "capability_request_ids": ctx.created_request_ids,
        "artifact_count": len(ctx.artifacts),
        "test_count": len(ctx.tests),
        "patch_count": len(ctx.patches),
        "lesson_count": len(ctx.lessons),
        "actual_tools": ctx.actual_tools or [],
        "ignored_unauthorized_tools": ctx.ignored_tools,
        "ignored_unauthorized_skills": ctx.ignored_skills,
    }


# LLM: _build_output_payload 属于子代理任务管理的函数边界；调整时先确认任务状态、执行器结果、验收和报告展示仍按原契约工作。
# 函数用途: 构建output载荷所需的数据结构或请求参数，供下一阶段流程消费；关键副作用: 主要返回快照或派生值，需避免引入额外写入副作用。
def _build_output_payload(ctx: OutputPayloadContext) -> dict[str, object]:
    task = ctx.task
    return {
        "run_id": task.id,
        "dry_run": ctx.dry_run,
        "ok": ctx.ok,
        "status": task.status,
        "verification_status": task.verification_status,
        "message": ctx.message,
        "backend": ctx.backend,
        "tool_rounds": ctx.tool_rounds,
        "runner_attempts": task.runner_attempts,
        "runner_last_error": task.runner_last_error,
        "response": "",
        "structured_output": _build_structured_output_payload(ctx),
        "used_tools": task.used_tools,
        "used_skills": task.used_skills,
        "artifacts": ctx.artifacts,
        # LLM: output.json 将可追溯声明与旧产物、测试字段并列保存。
        "evidence_packets": ctx.evidence_packets,
        "findings": ctx.findings,
        "tests": ctx.tests,
        "patches": ctx.patches,
        "acceptance": [item.summary for item in task.evidence],
        "lessons": ctx.lessons,
        "blockers": ctx.blockers,
        "next_action": _runner_next_action(
            params=RunnerNextActionParams(
                dry_run=ctx.dry_run,
                ok=ctx.ok,
                status=task.status,
                capability_request_count=ctx.structured_request_count,
                next_actions=ctx.next_actions,
            ),
        ),
        "next_actions": ctx.next_actions,
        "created_at": ctx.now,
    }
