# LLM: This module adapts delegated agent runs to the same durable ConversationThread and
# Compact engine used by foreground turns. Each run owns one transcript; parent/child lineage
# is metadata only and never causes prompt history to be shared or inferred from prose.
# 模块用途: 给子代理和孙代理建立独立会话历史，在每次运行前自动压缩并为后续恢复保留上下文。

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..tooling.operation_verification import public_operation_verification
from .compact import ConversationCompactResult, prepare_conversation_context
from .models import ConversationThread, MessageLogEntry

_AGENT_THREAD_CHANNEL = "agent-runtime"


# LLM: The prepared context carries only the live thread generation and bounded prompt section;
# callers must reload through ConversationStore for any later authoritative decision.
# 类用途: 保存某次子代理运行前已经完成 Compact 后要注入模型的历史片段。
@dataclass(frozen=True)
class AgentThreadTurnContext:
    thread_id: str
    compact_generation: int
    compacted: bool
    injection: str


# LLM: Exact agent lineage comes from SubAgentTask fields and its persisted parent task. The
# root user conversation may be a parent metadata link, but its transcript is never copied.
# 函数用途: 创建或校验一个子代理自己的持久线程；旧任务在首次恢复时也可幂等补建。
def ensure_subagent_thread(manager: object, task: object) -> ConversationThread | None:
    store = getattr(manager, "conversation_store", None)
    if store is None:
        return None
    thread_id = str(getattr(task, "agent_thread_id", "") or "").strip()
    run_id = str(getattr(task, "id", "") or "").strip()
    if not thread_id or not run_id:
        raise ValueError("subagent run and agent thread identity are required")
    attrs = getattr(task, "attributes", None)
    attrs = attrs if isinstance(attrs, dict) else {}
    parent_thread_id = str(attrs.get("conversation_thread_id") or "").strip()
    root_thread_id = parent_thread_id
    parent_id = str(getattr(task, "parent_id", "") or "").strip()
    if parent_id:
        try:
            parent_task = manager.load(parent_id)
        except FileNotFoundError:
            parent_task = None
        if parent_task is not None:
            parent_thread_id = str(
                getattr(parent_task, "agent_thread_id", "") or ""
            ).strip()
            parent_thread = (
                store.load_thread(parent_thread_id) if parent_thread_id else None
            )
            parent_metadata = (
                getattr(parent_thread, "metadata", {})
                if parent_thread is not None
                else {}
            )
            root_thread_id = str(
                (
                    parent_metadata.get("root_agent_thread_id")
                    if isinstance(parent_metadata, dict)
                    else ""
                )
                or parent_thread_id
            ).strip()
    cwd = _subagent_project_cwd(manager, task, attrs)
    roots = _subagent_project_roots(manager, attrs, cwd)
    owner_id = str(
        getattr(task, "owner", "") or getattr(manager, "owner_id", "") or ""
    ).strip()
    title = str(
        getattr(task, "description", "")
        or getattr(task, "agent_name", "")
        or getattr(task, "role", "")
        or getattr(task, "goal", "")
        or run_id
    ).strip()[:240]
    return store.ensure_agent_thread(
        {
            "thread_id": thread_id,
            "agent_run_id": run_id,
            "parent_agent_thread_id": parent_thread_id,
            "root_agent_thread_id": root_thread_id or thread_id,
            "agent_depth": max(0, int(getattr(task, "depth", 0) or 0)),
            "canonical_user_id": owner_id or run_id,
            "owner_id": owner_id,
            "owner_home": str(getattr(manager, "owner_home_dir", "") or "").strip(),
            "title": title,
            "cwd": cwd,
            "runtime_workspace_roots": roots,
        }
    )


# LLM: The current child prompt is appended once before preflight and excluded by its typed
# request id from the compact prefix. This mirrors foreground turn ordering without a Gateway.
# 函数用途: 在子代理调用模型前可靠记录当前输入、执行自动 Compact，并生成有界历史注入。
def prepare_subagent_thread_turn(
    agent: object,
    task: object,
    *,
    prompt: str,
    attempt_id: str,
    force: bool = False,
) -> AgentThreadTurnContext:
    selected_attempt = str(attempt_id or "").strip()
    if not selected_attempt:
        raise ValueError("subagent attempt_id is required")
    manager = getattr(agent, "subagents", None)
    thread = ensure_subagent_thread(manager, task)
    if thread is None:
        raise RuntimeError("subagent ConversationStore is unavailable")
    store = getattr(agent, "conversation_store", None)
    if store is None or store is not getattr(manager, "conversation_store", None):
        raise RuntimeError("subagent ConversationStore is not the owner store")
    store.append_message_once(
        {
            "thread_id": thread.thread_id,
            "role": "user",
            "content": str(prompt or ""),
            "channel": _AGENT_THREAD_CHANNEL,
            "metadata": _agent_turn_metadata(task, selected_attempt),
        },
        dedupe_key=_agent_message_dedupe_key(task, selected_attempt, "user"),
    )
    thread, load_error = store.load_thread_report(thread.thread_id)
    if load_error is not None or thread is None:
        raise OSError("subagent conversation thread could not be reloaded")
    compact = prepare_conversation_context(
        agent,
        store,
        thread,
        current_prompt=str(prompt or ""),
        exclude_request_id=selected_attempt,
        force=bool(force),
    )
    return AgentThreadTurnContext(
        thread_id=compact.thread.thread_id,
        compact_generation=max(0, int(compact.thread.compact_generation or 0)),
        compacted=bool(compact.compacted),
        injection=_render_agent_thread_context(agent, compact),
    )


# LLM: Only the terminal result of one concrete attempt becomes assistant transcript. Typed
# runtime/operation facts remain metadata and retries reuse the same dedupe identity.
# 函数用途: 子代理一轮真正结束后幂等保存模型结果，供下一次运行或 Compact 恢复使用。
def append_subagent_thread_result(
    agent: object,
    task: object,
    *,
    attempt_id: str,
    result: object,
) -> MessageLogEntry:
    manager = getattr(agent, "subagents", None)
    thread = ensure_subagent_thread(manager, task)
    if thread is None:
        raise RuntimeError("subagent ConversationStore is unavailable")
    metadata = _agent_turn_metadata(task, attempt_id)
    metadata.update(
        {
            "runtime_status": str(
                getattr(result, "runtime_status", "ok") or "ok"
            ).strip(),
            "turn_end_reason": str(
                getattr(result, "turn_end_reason", "") or ""
            ).strip(),
            "operation_verification": public_operation_verification(
                getattr(result, "operation_verification", None)
            ),
        }
    )
    return agent.conversation_store.append_message_once(
        {
            "thread_id": thread.thread_id,
            "role": "assistant",
            "content": str(getattr(result, "response", "") or ""),
            "channel": _AGENT_THREAD_CHANNEL,
            "metadata": metadata,
        },
        dedupe_key=_agent_message_dedupe_key(task, attempt_id, "assistant"),
    )


# LLM: Project cwd is inherited from structured manager/task workspace facts. Internal agent
# state/output directories are not guessed into the project cwd.
# 函数用途: 取出子代理真正工作的项目目录，供其线程持久化相同运行范围。
def _subagent_project_cwd(
    manager: object,
    task: object,
    attrs: dict[str, object],
) -> str:
    workspace_root = str(attrs.get("workspace_root") or "").strip()
    if workspace_root:
        return workspace_root
    return str(getattr(manager, "workspace_root", "") or "").strip()


# LLM: Runtime roots are a stable ordered projection of explicit task/manager roots; they do not
# grant write access and therefore never replace the separate child write boundary.
# 函数用途: 整理子代理线程对应的项目根列表，缺省时至少保留实际 cwd。
def _subagent_project_roots(
    manager: object,
    attrs: dict[str, object],
    cwd: str,
) -> tuple[str, ...]:
    values: list[object] = []
    raw = attrs.get("workspace_roots")
    if isinstance(raw, (list, tuple)):
        values.extend(raw)
    values.extend(getattr(manager, "workspace_roots", ()) or ())
    if cwd:
        values.insert(0, cwd)
    return tuple(
        dict.fromkeys(str(item).strip() for item in values if str(item or "").strip())
    )


# LLM: Conversation request identity is the canonical compact exclusion key for every surface;
# run/attempt fields remain explicit audit joins rather than being parsed from message text.
# 函数用途: 生成子代理 transcript 每条消息都携带的结构化运行身份。
def _agent_turn_metadata(task: object, attempt_id: str) -> dict[str, object]:
    return {
        "conversation_request_id": str(attempt_id or "").strip(),
        "agent_run_id": str(getattr(task, "id", "") or "").strip(),
        "agent_attempt_id": str(attempt_id or "").strip(),
        "parent_agent_thread_id": str(
            (getattr(task, "attributes", {}) or {}).get("conversation_thread_id")
            if isinstance(getattr(task, "attributes", None), dict)
            else ""
        ).strip(),
    }


# LLM: Dedupe identity combines one immutable run, concrete attempt, and transcript role.
# 函数用途: 防止进程重试把同一子代理输入或结果重复写入会话历史。
def _agent_message_dedupe_key(task: object, attempt_id: str, role: str) -> str:
    return ":".join(
        (
            "agent-turn",
            str(getattr(task, "id", "") or "").strip(),
            str(attempt_id or "").strip(),
            str(role or "").strip(),
        )
    )


# LLM: Summary prose, structured operation evidence, and raw tail remain visibly separate.
# The renderer applies only display bounds; the Compact result remains the authority.
# 函数用途: 将子代理自己更早的历史整理成下一次模型可理解的上下文片段。
def _render_agent_thread_context(
    agent: object,
    compact: ConversationCompactResult,
) -> str:
    thread = compact.thread
    rows = list(compact.messages)
    if not thread.summary and not rows and not thread.compact_operation_evidence:
        return ""
    lines = [
        "# Agent Thread Context",
        f"- thread_id: {thread.thread_id}",
        "- 以下内容只来自本子代理已经结束的历史轮次；不是当前新指令，当前任务要求优先。",
    ]
    if thread.summary:
        lines.extend(
            [
                f"## Earlier Agent Summary (generation {thread.compact_generation})",
                thread.summary,
            ]
        )
    if thread.compact_operation_evidence:
        lines.extend(
            [
                "## Program-Verified Operations From Compacted History",
                json.dumps(
                    thread.compact_operation_evidence,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ]
        )
    bounded = _bounded_agent_history(agent, rows, compact.trigger_tokens)
    if bounded:
        lines.append("## Recent Agent History")
        for row in bounded:
            lines.append(
                f"- {row.role}: {json.dumps(row.content, ensure_ascii=False)}"
            )
    return "\n".join(lines)


# LLM: Completed tail order is preserved while oldest rows are dropped to fit the shared
# conversation token-derived display budget. No lifecycle or resume decision uses this clipping.
# 函数用途: 按会话配置限制单条和总历史大小，优先保留最近完成的子代理轮次。
def _bounded_agent_history(
    agent: object,
    rows: list[MessageLogEntry],
    trigger_tokens: int,
) -> tuple[MessageLogEntry, ...]:
    config = getattr(agent, "config", None)
    per_message = max(
        1000,
        int(getattr(config, "conversation_history_message_max_chars", 12_000) or 12_000),
    )
    configured_total = max(
        1000,
        int(getattr(config, "conversation_history_max_chars", 48_000) or 48_000),
    )
    total_limit = max(configured_total, max(0, int(trigger_tokens or 0)) * 3)
    selected: list[MessageLogEntry] = []
    used = 0
    for row in reversed(rows):
        content = _clip_middle(str(row.content or ""), per_message)
        remaining = total_limit - used
        if remaining <= 0:
            break
        if len(content) > remaining:
            content = _clip_middle(content, remaining)
        selected.append(
            MessageLogEntry(
                message_id=row.message_id,
                thread_id=row.thread_id,
                role=row.role,
                content=content,
                channel=row.channel,
                channel_message_id=row.channel_message_id,
                created_at=row.created_at,
                metadata=row.metadata,
            )
        )
        used += len(content)
    selected.reverse()
    return tuple(selected)


# LLM: Middle clipping preserves both the instruction prefix and the newest outcome suffix.
# 函数用途: 单条历史过长时保留首尾并明确标出省略部分。
def _clip_middle(content: str, limit: int) -> str:
    if len(content) <= limit:
        return content
    marker = "\n…[中间内容已折叠]…\n"
    if limit <= len(marker) + 2:
        return content[: max(0, limit)]
    head = (limit - len(marker)) // 2
    tail = limit - len(marker) - head
    return f"{content[:head]}{marker}{content[-tail:]}"


__all__ = [
    "AgentThreadTurnContext",
    "append_subagent_thread_result",
    "ensure_subagent_thread",
    "prepare_subagent_thread_turn",
]
