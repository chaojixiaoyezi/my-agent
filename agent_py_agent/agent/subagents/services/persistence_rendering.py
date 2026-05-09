# LLM: Rendering helpers for subagent persistence; keep file writes separate from content assembly.
# 模块用途: 承接子代理持久化时的人类可读 Markdown 渲染，避免 persistence 主流程膨胀。

from __future__ import annotations

from ..models import SubAgentTask


# LLM: render_thought_markdown mirrors the persisted task contract without mutating task state.
# 函数用途: 把子代理思考、计划、能力边界、写入边界、验收条件和证据渲染成 thought.md。
def render_thought_markdown(task: SubAgentTask) -> str:
    return (
        "# Thought\n\n"
        f"{task.thought}\n\n"
        "## Plan\n"
        + "\n".join(f"- {item}" for item in task.plan)
        + "\n\n"
        "## Capability Boundary\n"
        f"- Agent: {task.agent_name}\n"
        f"- Role: {task.role}\n"
        f"- Owner: {task.owner or 'none'}\n"
        f"- Supervisor: {task.supervisor or 'none'}\n"
        f"- Final owner: {task.final_owner or 'none'}\n"
        f"- Parent: {task.parent_id or 'none'}\n"
        f"- Depth: {task.depth}\n"
        f"- Allowed skills: {', '.join(task.allowed_skills) or 'none'}\n"
        f"- Allowed tools: {', '.join(task.allowed_tools) or 'none'}\n\n"
        "## Write Boundary\n"
        f"- Task dir: {task.task_dir}\n"
        f"- Allowed write roots: {', '.join(task.allowed_write_roots) or 'none'}\n"
        f"- Forbidden write roots: {', '.join(task.forbidden_write_roots) or 'none'}\n\n"
        "## Acceptance Checks\n"
        + "\n".join(f"- {item}" for item in task.acceptance_checks or ["未设置"])
        + "\n\n"
        "## Evidence\n"
        + "\n".join(f"- [{item.kind}] {item.summary}" for item in task.evidence or [])
        + ("\n" if task.evidence else "- 暂无\n")
    )
