# LLM: Background prompt context renderer keeps live wake prompts refs-first.
# 模块用途: 将长期会话、wake signal、任务树压缩成后台主代理 prompt 上下文。

from __future__ import annotations

from typing import Any

from ..agent_core.agent_tree_status import agent_tree_status_payload
from ..artifacts.registry import latest_artifact_records
from .context_budget import (
    BackgroundContextPayloadRequest,
    background_context_budget_from_config,
    bounded_background_context_payload,
)
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
        ("Recovery Snapshot", bounded["recovery_snapshot"]),
        ("Agent Tree Snapshot", bounded["agent_tree"]),
    ]
    lines = _context_header(request, thread)
    for title, payload in sections:
        lines.extend(["", f"## {title}", json_block(payload)])
    lines.extend(["", "## Available Control Actions", *CONTROL_ACTION_LINES, "[/background-main-agent-context]"])
    return "\n".join(lines)


def _bounded_context(agent: object, store: ConversationStore, thread_id: str) -> dict[str, Any]:
    config = getattr(agent, "config", None)
    return bounded_background_context_payload(
        BackgroundContextPayloadRequest(
            bundle=store.context_bundle(thread_id, recent_limit=_config_int(config, "conversation_context_recent_limit")),
            pending_wake_signals=pending_wake_payload(
                store,
                thread_id,
                limit=_config_int(config, "background_pending_wake_prompt_limit"),
            ),
            agent_tree=agent_tree_status_payload(agent, {}),
            recovery_snapshot=_recovery_snapshot(agent, store, thread_id),
            budget=background_context_budget_from_config(config),
        )
    )


# LLM: _config_int resolves background-context limits from AgentConfig for prompt rendering.
# 函数用途: 读取长期会话 prompt 预算；非法值只回退到配置 schema 默认。
def _config_int(config: object | None, key: str) -> int:
    if config is None:
        from ..settings.config import AgentConfig

        config = AgentConfig()
    try:
        return max(0, int(getattr(config, key)))
    except (TypeError, ValueError):
        from ..settings.config import AgentConfig

        return max(0, int(getattr(AgentConfig(), key)))


def _context_header(request, thread: ConversationThread) -> list[str]:
    return [
        "[background-main-agent-context]",
        f"reason: {request.reason}",
        f"thread_id: {thread.thread_id}",
        f"task_id: {request.task_id or ''}",
    ]


# LLM: _recovery_snapshot is a non-blocking handoff summary for background takeover.
# 函数用途: 对 claim、任务树和产物登记做轻量对账，只生成提示事实，不阻断后台运行。
def _recovery_snapshot(agent: object, store: ConversationStore, thread_id: str) -> dict[str, Any]:
    claim = store.load_background_run_claim(thread_id)
    previous = claim.get("previous_claim") if isinstance(claim.get("previous_claim"), dict) else {}
    tree = agent_tree_status_payload(agent, {})
    records = latest_artifact_records(getattr(agent, "root", "."))
    return {
        "schema_version": "background_recovery_snapshot.v1",
        "effect": "read_only",
        "does_not_block": True,
        "current_claim_status": str(claim.get("status") or ""),
        "current_claim_id": str(claim.get("claim_id") or ""),
        "current_claim_reason": str(claim.get("reason") or ""),
        "previous_claim_status": str(previous.get("status") or ""),
        "previous_claim_error": previous.get("last_error") if isinstance(previous.get("last_error"), dict) else {},
        "takeover": claim.get("takeover") if isinstance(claim.get("takeover"), dict) else {},
        "tree_status_buckets": tree.get("status_buckets") if isinstance(tree.get("status_buckets"), dict) else {},
        "artifact_registry_count": len(records),
        "artifact_registry_status_counts": _artifact_status_counts(records),
        "takeover_advice": "接手前先核对 claim、任务树和产物登记；不要把模型文本里的完成声明当成事实。",
    }


def _artifact_status_counts(records: dict[str, object]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records.values():
        status = str(getattr(record, "status", "") or "unknown")
        counts[status] = counts.get(status, 0) + 1
    return counts
