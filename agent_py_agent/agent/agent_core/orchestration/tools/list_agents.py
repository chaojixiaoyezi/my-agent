"""Read-only model tool for the current agent tree.

The tool exposes the same bounded canonical projection already used by the TUI
and background runtime. It never starts, wakes, retries, or cancels an agent.
"""

# LLM: This module is the only model-visible read adapter for the canonical
# agent-tree projection. Keep lifecycle mutations in create/guidance/cancel and
# do not add a second status store or polling loop here.
# 模块用途: 让主代理或子代理只读查看自己的代理树状态，不承担推进、唤醒或取消。

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ....tooling.models import (
    BaseTool,
    ConcurrencyPolicy,
    EffectResolverPolicy,
    ResourceScopePolicy,
    ToolHandlerOutcome,
    ToolRuntimePolicy,
)
from ...agent_tree.status import agent_tree_status_payload
from ..tool_specs import build_list_agents_model_spec

if TYPE_CHECKING:
    from ....core import SimpleAgent


# LLM: list_agents delegates every identity/scope decision to
# agent_tree_status_payload. The optional run_id narrows an owner-scoped query;
# descendants still cannot escape their own subtree because the canonical
# projection resolves current_subagent_run_id before model parameters.
# 类用途: 返回当前任务代理树或一个已知 run 的子树快照，供模型回答“谁还在跑”。
class ListAgentsTool(BaseTool):
    model_spec = build_list_agents_model_spec()
    runtime_policy = ToolRuntimePolicy(
        effect_resolver=EffectResolverPolicy("read_only"),
        concurrency_policy=ConcurrencyPolicy("parallel_safe"),
        resource_scopes=ResourceScopePolicy(
            mode="declared",
            static_scopes=("current_agent_tree",),
        ),
        promotes_task=False,
        mutates_workspace=False,
    )

    # LLM: Keep only the owner-bound agent reference; callers cannot inject a
    # manager, owner root, or filesystem path through model parameters.
    # 函数用途: 绑定当前代理的 owner 范围和规范子代理管理器。
    def __init__(self, agent: SimpleAgent) -> None:
        self.agent = agent

    # LLM: The result is a bounded runtime projection with explicit read_only
    # policy facts. Do not turn empty nodes into completion or mutate wait state.
    # 函数用途: 读取当前代理树；可按明确 run_id 缩窄，但不会改变任何运行状态。
    def execute(self, params: dict[str, object]) -> ToolHandlerOutcome:
        run_id = str(params.get("run_id") or "").strip()
        query = {"run_id": run_id, "scope": "own_subtree"} if run_id else {}
        payload = agent_tree_status_payload(self.agent, query)
        return ToolHandlerOutcome(
            self.model_spec.name,
            True,
            json.dumps(payload, ensure_ascii=False, indent=2),
            result_envelope={"agent_tree_status": payload},
        )


__all__ = ["ListAgentsTool"]
