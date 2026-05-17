# LLM: scheduled hierarchy role inference is isolated from scheduler orchestration.
# 模块用途: 根据结构化 role/tool/write-root 信号推断下一层真实 role，避免从自然语言 goal 猜职责。

from __future__ import annotations

from typing import Any

from ..models import SubAgentTask
from .hierarchy_role_identity import role_from_child_spec_identity
from .hierarchy_tool_policy import LeafWriteIntentRequest, should_infer_leaf_coding_tools


# LLM: scheduled_child_role repairs structured model slips while preserving concrete product-write intent.
# 函数用途: 带层级调度工具的 child 按协调任务推断；写入根明确时才归一为 leaf_worker，不解析 goal 关键词。
def scheduled_child_role(
    parent: SubAgentTask,
    spec: Any,
    extra_write_roots: list[str] | None = None,
    *,
    goal: str | None = None,
) -> str:
    role = role_from_child_spec_identity(spec)
    if role not in {"worker", "general", "child"}:
        return role
    if should_infer_leaf_coding_tools(
        LeafWriteIntentRequest(spec=spec, extra_write_roots=extra_write_roots or [], goal=goal)
    ):
        return "leaf_worker"
    tools = set(getattr(spec, "allowed_tools", []) or [])
    if "schedule_child_subagents" not in tools and "dispatch_subagents" not in tools:
        return role
    return _coordinator_role_for_depth(parent)


# LLM: _coordinator_role_for_depth maps hierarchy depth to the existing role naming contract.
# 函数用途: 将“继续创建下级”的任务按层级命名成 child/grandchild coordinator。
def _coordinator_role_for_depth(parent: SubAgentTask) -> str:
    depth = int(parent.depth or 0) + 1
    if depth == 1:
        return "child_coordinator"
    if depth == 2:
        return "grandchild_coordinator"
    return "coordinator"
