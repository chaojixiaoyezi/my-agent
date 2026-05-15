# LLM: Body-read refs define safe metadata artifacts and user-facing blocked guidance.
# 模块用途: 给委托期正文读取 guard 提供稳定的元数据白名单、编排 artifact 识别和阻断提示。

from __future__ import annotations

from pathlib import Path

BODY_READ_TOOLS = {"read_file", "read_artifact", "run_command"}
ORCHESTRATION_ARTIFACT_PREFIXES = (
    "dispatch_subagents-",
    "subagent_board-",
    "subagents_due_check-",
    "subagents_plan_actions-",
    "subagents_apply_actions-",
)
METADATA_FILE_NAMES = {
    "ACCEPTANCE.md",
    "CONTEXT_BUNDLE.md",
    "HANDOFF.md",
    "STATUS.md",
    "TAKEOVER_READINESS.md",
    "acceptance_review.json",
    "checkpoint.json",
    "compaction_ledger.jsonl",
    "context_bundle.json",
    "failing_tests.json",
    "failure_handoff.json",
    "final_report.md",
    "latest_continue_packet.json",
    "latest_metadata.json",
    "latest_summary.md",
    "output.json",
    "parent_acceptance_auto_followup.json",
    "progress.md",
    "runner_result.json",
    "session_compact_ledger.jsonl",
    "status_report.json",
    "subagent_board.json",
    "subagent_dispatch_report.json",
    "summary.md",
    "takeover_readiness.json",
    "task.md",
    "task.json",
    "test_execution.json",
    "timeline.jsonl",
}


# LLM: is_orchestration_artifact_read lets parents read small refs/status artifacts during delegation.
# 函数用途: 识别 dispatch/subagent_board 等编排摘要 artifact，避免父级恢复时只能盲目派工。
def is_orchestration_artifact_read(payload: dict[str, object]) -> bool:
    raw = str(payload.get("artifact_ref") or payload.get("ref") or "").strip()
    if not raw:
        return False
    name = Path(raw).name
    return any(name.startswith(prefix) for prefix in ORCHESTRATION_ARTIFACT_PREFIXES)


# LLM: looks_like_runtime_path keeps runtime metadata readable without importing workspace adapters.
# 函数用途: 用路径片段识别 .my_agent_runtime/subagents 或 tasks/.../agents 元数据区。
def looks_like_runtime_path(path: Path) -> bool:
    parts = tuple(path.parts)
    if ".my_agent_runtime" in parts and "subagents" in parts:
        return True
    if "_runtime" in parts and "subagents" in parts:
        return True
    return "tasks" in parts and "agents" in parts


# LLM: blocked_message teaches the model the recovery route instead of making it ask the user.
# 函数用途: 返回机器可读阻断原因和推荐工具，让父级自己派 QA/验收/修复。
def blocked_message(tool: str, parent: object) -> str:
    quality_tool = quality_role_creation_tool(parent)
    return (
        "delegating_body_read_blocked=true "
        f"tool={tool} parent_run_id={getattr(parent, 'id', '')}。"
        "父级已经派出子代理，验收子代理完成前保持 refs-only："
        "不要主动 read_file/read_artifact/run_command 读取产物正文或大 artifact 正文。"
        "请先使用 subagent_board 或 dispatch_subagents 的 direct_children 摘要查看状态；"
        f"如缺 tester/bug_finder/acceptor，请调用 {quality_tool} 创建质量子代理；"
        "如已有失败 refs，请创建 scoped repair worker；"
        "只有 acceptor 完成验收后，父级才进入最终读正文核查阶段。"
    )


# LLM: quality_role_creation_tool keeps root and nested parents on the right delegation API.
# 函数用途: 顶层 root 用 create_subagents；子代理 runner 内的父级用 schedule_child_subagents。
def quality_role_creation_tool(parent: object) -> str:
    if str(getattr(parent, "id", "") or "") == "top-level-root":
        return "create_subagents"
    return "schedule_child_subagents"
