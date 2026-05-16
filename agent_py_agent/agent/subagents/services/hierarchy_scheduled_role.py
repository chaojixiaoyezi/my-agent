# LLM: scheduled hierarchy role inference is isolated from scheduler orchestration.
# 模块用途: 根据 child spec、父级深度和写入意图推断下一层真实 role，避免 scheduler 主流程膨胀。

from __future__ import annotations

from typing import Any

from ..models import SubAgentTask
from .hierarchy_role_identity import role_from_child_spec_identity
from .hierarchy_tool_policy import LeafWriteIntentRequest, should_infer_leaf_coding_tools


# LLM: scheduled_child_role repairs common model slips while preserving concrete product-write intent.
# 函数用途: 带层级调度权限的 child 即使有报告写入工具，也按协调任务推断；只有明确写业务产物才归一为 leaf_worker。
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
    if _looks_like_child_creation_goal(goal or getattr(spec, "goal", "")):
        return _coordinator_role_for_depth(parent)
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


# LLM: _looks_like_child_creation_goal catches coordinator intent before file-extension leaf inference.
# 函数用途: goal 说创建/调度 worker 或下一层时，即使提到 evidence.json 也应是协调节点。
def _looks_like_child_creation_goal(goal: object) -> bool:
    text = str(goal or "").lower()
    return ("创建" in text or "create" in text or "spawn" in text) and (
        "worker" in text or "leaf" in text or "子代理" in text or "下级" in text or "下一层" in text
    )
