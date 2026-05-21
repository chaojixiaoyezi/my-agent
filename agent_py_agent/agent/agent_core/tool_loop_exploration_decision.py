# LLM: Exploration-fuse response decisions stay out of the central tool-loop decision file.
# 模块用途: 将“只探索不落地”的纠偏/阻断分支独立出来，保持主工具循环决策文件不过度膨胀。

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from .tool_exploration_fuse import (
    exploration_fuse_block_response,
    exploration_fuse_context,
    has_pending_exploration_fuse,
    has_required_exploration_fuse,
)
from .tool_loop_repair_counters import ToolLoopRepairCounters, _inc_exploration_fuse


# LLM: ExplorationFuseDecision mirrors the central decision shape without importing it.
# 类用途: 避免和 tool_loop_response_decision 形成循环导入，同时保持返回字段机器可读。
@dataclass(frozen=True)
class ExplorationFuseDecision:
    __test__: ClassVar[bool] = False

    action: str
    response: object
    calls: list[dict[str, object]]
    counters: ToolLoopRepairCounters


# LLM: ExplorationFuseDecisionRequest bundles loop facts so helper signatures stay small.
# 类用途: 保存 exploration fuse 分支需要的 agent、params、response、counters 和 calls。
@dataclass(frozen=True)
class ExplorationFuseDecisionRequest:
    __test__: ClassVar[bool] = False

    agent: object
    params: object
    response: object
    counters: ToolLoopRepairCounters
    calls: list[dict[str, object]]


# LLM: exploration_fuse_tool_call_decision handles read/fetch loops before tools execute.
# 函数用途: 连续只读/抓取且没有本地进展时，先要求写 checkpoint/草稿/脚本，重复忽略则阻断。
def exploration_fuse_tool_call_decision(
    request: ExplorationFuseDecisionRequest,
) -> ExplorationFuseDecision | None:
    if not has_required_exploration_fuse(request.agent, request.calls):
        return None
    context = exploration_fuse_context(request.agent, request.counters.exploration_fuse_redirects)
    if context:
        request.params.tool_context.append(context)
        return ExplorationFuseDecision("continue", None, [], _inc_exploration_fuse(request.counters))
    return ExplorationFuseDecision("break", exploration_fuse_block_response(request.agent), [], request.counters)


# LLM: exploration_fuse_no_tool_call_decision keeps final prose from bypassing materialization.
# 函数用途: 探索空转达到阈值后，即使模型不再调用工具，也必须先写本地进展或被阻断。
def exploration_fuse_no_tool_call_decision(
    request: ExplorationFuseDecisionRequest,
) -> ExplorationFuseDecision | None:
    if not has_pending_exploration_fuse(request.agent):
        return None
    context = exploration_fuse_context(request.agent, request.counters.exploration_fuse_redirects)
    if context:
        request.params.tool_context.append(context)
        return ExplorationFuseDecision("continue", None, [], _inc_exploration_fuse(request.counters))
    block = exploration_fuse_block_response(request.agent) or request.response
    return ExplorationFuseDecision("break", block, [], request.counters)


__all__ = [
    "ExplorationFuseDecision",
    "ExplorationFuseDecisionRequest",
    "exploration_fuse_no_tool_call_decision",
    "exploration_fuse_tool_call_decision",
]
