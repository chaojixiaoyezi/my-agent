# LLM: Background prompt context renderer keeps live wake prompts refs-first.
# 模块用途: 将长期会话、wake signal、任务树压缩成后台主代理 prompt 上下文。

from __future__ import annotations

from typing import Any

from ..agent_core.agent_tree_status import agent_tree_status_payload
from .context_budget import bounded_background_context_payload
from .models import ConversationThread
from .runtime_utils import json_block, pending_wake_payload
from .store import ConversationStore

CONTROL_ACTION_LINES = [
    "- inspect_agent_tree: 只读查看主/子/孙代理状态树。",
    "- case_status: 只读查看协作 case 的请求、证据、参与者和决策。",
    "- list_collaboration_requests: 查看点名给某个代理的待处理协作请求。",
    "- raise_collaboration_event: 发现需要其他代理/数据源协作时，一步打开 case 并发出 request。",
    "- update_collaboration_request: 更新协作请求状态，例如完成、阻塞、需要换策略。",
    "- reroute_collaboration_request: 请求阻塞且有替代目标时，把请求结构化改派给新目标。",
    "- update_case_status: 主代理研判后推进协作 case 生命周期。",
    "- dispatch_subagents: 只有需要推进、恢复或调度时才调用。",
    "- subagent_board: 查看任务看板摘要。",
]


def context_markdown(*, agent: object, store: ConversationStore, thread: ConversationThread, request) -> str:
    bounded = _bounded_context(agent, store, thread.thread_id)
    sections = [
        ("Active Wake Signal", request.wake_signal or {}),
        ("Conversation Thread", bounded["thread"]),
        ("Recent Messages", bounded["messages"]),
        ("Bound Tasks", bounded["tasks"]),
        ("Channel Bindings", bounded["channel_bindings"]),
        ("Recent Observations", bounded["observations"]),
        ("Pending Wake Signals", bounded["pending_wake_signals"]),
        ("Agent Tree Snapshot", bounded["agent_tree"]),
    ]
    lines = _context_header(request, thread)
    for title, payload in sections:
        lines.extend(["", f"## {title}", json_block(payload)])
    lines.extend(["", "## Available Control Actions", *CONTROL_ACTION_LINES, "[/background-main-agent-context]"])
    return "\n".join(lines)


def _bounded_context(agent: object, store: ConversationStore, thread_id: str) -> dict[str, Any]:
    return bounded_background_context_payload(
        bundle=store.context_bundle(thread_id),
        pending_wake_signals=pending_wake_payload(store, thread_id),
        agent_tree=agent_tree_status_payload(agent, {}),
    )


def _context_header(request, thread: ConversationThread) -> list[str]:
    return [
        "[background-main-agent-context]",
        f"reason: {request.reason}",
        f"thread_id: {thread.thread_id}",
        f"task_id: {request.task_id or ''}",
    ]
