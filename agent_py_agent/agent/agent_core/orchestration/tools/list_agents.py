"""当前代理树的只读模型工具，返回状态与真实产物引用，不暴露界面恢复细节。"""

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
from ...agent_tree.model_view import (
    agent_tree_model_payload,
    agent_tree_model_preview,
    agent_tree_progress_observation,
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

    # LLM: 身份裁决仍由 canonical tree 完成；原始结果保留，稳定进展元数据只接既有软观察，不判死或取消。
    # 函数用途: 读取状态与产物引用，并排除仅耗时变化的假进展；不改等待、权限或生命周期状态。
    def execute(self, params: dict[str, object]) -> ToolHandlerOutcome:
        run_id = str(params.get("run_id") or "").strip()
        query = {"run_id": run_id, "scope": "own_subtree"} if run_id else {}
        snapshot = agent_tree_status_payload(self.agent, query)
        payload = agent_tree_model_payload(snapshot)
        return ToolHandlerOutcome(
            self.model_spec.name,
            True,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            result_envelope={
                "tool_output_policy": {"live_prompt_output": agent_tree_model_preview(payload)},
                "progress_observation": agent_tree_progress_observation(snapshot, payload),
            },
        )


__all__ = ["ListAgentsTool"]
