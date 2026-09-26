# LLM: 每个 delegated run 使用原 ConversationThread/Compact；谱系不共享历史，模型建议仅由宿主参数初始化新 thread，读取/恢复不采用。
# 模块用途: 为子孙代理建立独立会话、保存正文与建议；普通准备和完整恢复候选共用纯历史投影。

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from ..settings.reasoning_effort import child_reasoning_level
from ..settings.thread_model_selection import PendingSubagentModelAdvice
from ..tooling.operation_verification import public_operation_verification
from ..turn_end import result_turn_end_reason
from .compact_guard import CompactInterruptCheck, raise_if_compact_interrupted
from .compact_projection import ConversationCompactSource, ConversationCompactView
from .compact_scope import THREAD_COMPACT_SCOPE
from .compact_summary_view import AppliedCompactContext, resolve_compact_summary_view
from .history_seed import freeze_history_source
from .models import ConversationHistorySeed, ConversationThread, MessageLogEntry
from .native_history import (
    CANONICAL_NATIVE_MESSAGES_METADATA_KEY,
    canonical_native_messages_envelope,
)
from .tool_context_window import (
    TERMINAL_TOOL_FOLD_METADATA_KEY,
    build_conversation_terminal_tool_fold,
    conversation_message_with_terminal_tool_fold,
)

if TYPE_CHECKING:
    from .compact_provider_surface import ConversationCompactModelSurface

_AGENT_THREAD_CHANNEL = "agent-runtime"


# LLM: One runner attempt may contain several Goal turns; display/history IDs differ while execution authority stays in attempt_id.
# 类用途: 显式区分子代理执行凭证和模型对话轮次，避免下一轮覆盖上一轮或压缩重放当前输入。
@dataclass(frozen=True)
class AgentThreadTurnInput:
    prompt: str
    attempt_id: str
    turn_id: str = ""


# LLM: 历史投影不授予提交权；compact_context固定本轮所用摘要范围，source仅供内部恢复保留原未压缩行。
# 类用途: 保存子代理准备好的历史，以及可选的待完整请求准备后压缩的来源。
@dataclass(frozen=True)
class AgentThreadTurnContext:
    thread_id: str
    compact_generation: int
    compacted: bool
    injection: str
    history_seed: ConversationHistorySeed | None = None
    compact_context: AppliedCompactContext | None = field(default=None, repr=False)
    compact_source: ConversationCompactSource | None = field(default=None, repr=False)


# LLM: 谱系只读 canonical task/父；建议仅接受宿主准备载体的显式参数，绝不从 task attrs 恢复，既有线程不重植 pending。
#   新线程同时写入创建时冻结的智能程度档位（host_reasoning_effort.v1），既有线程不改。
# 函数用途: 物化或校验孩子独立会话，新线程可保存待验证模型建议与档位，读取/恢复不改其有效模型。
def ensure_subagent_thread(manager: object, task: object, *, model_advice: PendingSubagentModelAdvice | None = None) -> ConversationThread | None:
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
                store.threads.load(parent_thread_id) if parent_thread_id else None
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
    return store.threads.ensure_agent(
        {
            "thread_id": thread_id,
            "agent_run_id": run_id,
            "parent_agent_thread_id": parent_thread_id,
            "root_agent_thread_id": root_thread_id or thread_id,
            "agent_depth": max(0, int(getattr(task, "depth", 0) or 0)),
            "canonical_user_id": owner_id or run_id,
            "owner_id": owner_id,
            "owner_home": str(getattr(manager, "owner_home_dir", "") or "").strip(),
            "model_profile_id": str((attrs.get("host_model_profile.v1") or {}).get("profile_id") or "default"),
            "reasoning_effort": child_reasoning_level(attrs),
            "title": title,
            "cwd": cwd,
            "runtime_workspace_roots": roots,
            "model_advice": model_advice,
        }
    )


# LLM: The current child prompt is appended once before preflight and excluded by its typed request
# id. 首次读source时冻结thread view；内部defer只读，普通入口沿同源摘要/提交，停止检查不能因延迟而跳过。
# 函数用途: 按独立对话轮记录输入，保留真实 attempt 身份，以同一摘要范围生成历史种子并可中断压缩。
def prepare_subagent_thread_turn(
    agent: object,
    task: object,
    *,
    turn: AgentThreadTurnInput,
    force: bool = False,
    defer_compact: bool = False,
    progress_callback: Callable[[dict[str, object]], object] | None = None,
    interrupt_check: CompactInterruptCheck | None = None,
    model_surface: ConversationCompactModelSurface | None = None,
) -> AgentThreadTurnContext:
    from .compact import (
        ConversationCompactOptions,
        load_conversation_compact_source,
        prepare_conversation_context,
    )

    prompt, attempt_id = turn.prompt, turn.attempt_id
    selected_attempt = str(turn.turn_id or attempt_id or "").strip()
    if not selected_attempt:
        raise ValueError("subagent attempt_id is required")
    manager = getattr(agent, "subagents", None)
    thread = ensure_subagent_thread(manager, task)
    if thread is None:
        raise RuntimeError("subagent ConversationStore is unavailable")
    store = getattr(agent, "conversation_store", None)
    if store is None or store is not getattr(manager, "conversation_store", None):
        raise RuntimeError("subagent ConversationStore is not the owner store")
    store.messages.append_once(
        {
            "thread_id": thread.thread_id,
            "role": "user",
            "content": str(prompt or ""),
            "channel": _AGENT_THREAD_CHANNEL,
            "metadata": {**_agent_turn_metadata(task, attempt_id), "conversation_request_id": selected_attempt},
        },
        dedupe_key=_agent_message_dedupe_key(task, selected_attempt, "user"),
    )
    thread, load_error = store.threads.load_report(thread.thread_id)
    if load_error is not None or thread is None:
        raise OSError("subagent conversation thread could not be reloaded")
    source = load_conversation_compact_source(
        agent, store, thread, exclude_request_id=selected_attempt, scope=THREAD_COMPACT_SCOPE,
    )
    if defer_compact:
        raise_if_compact_interrupted(interrupt_check)
        return project_agent_thread_context(agent, ConversationCompactView(
            thread.thread_id, thread.compact_generation, source.compact_context.view.summary, source.messages,
            source.compact_context.view.operation_evidence, source.recent_operation_evidence,
            source.policy.trigger_tokens, False,
        ), source=source, compact_context=source.compact_context)
    compact = prepare_conversation_context(
        agent,
        store,
        thread,
        options=ConversationCompactOptions(
            current_prompt=str(prompt or ""),
            exclude_request_id=selected_attempt,
            source=source,
            force=bool(force),
            progress_callback=progress_callback,
            interrupt_check=interrupt_check,
            model_surface=model_surface,
        ),
    )
    applied = source.compact_context
    if compact.compacted:
        applied = AppliedCompactContext(
            compact.thread.thread_id, THREAD_COMPACT_SCOPE,
            resolve_compact_summary_view(agent, compact.thread, THREAD_COMPACT_SCOPE),
        )
    return project_agent_thread_context(agent, ConversationCompactView(
        compact.thread.thread_id, compact.thread.compact_generation, applied.view.summary, compact.messages,
        applied.view.operation_evidence, dict(compact.recent_operation_evidence or {}),
        compact.trigger_tokens, compact.compacted,
    ), compact_context=applied)


# LLM: 仅渲染显式历史视图并原样传递同次摘要范围；seed展示代次取view，宿主CAS代次仍取thread。
# 函数用途: 普通准备和完整恢复候选共用同一子代理历史注入与原生历史种子。
def project_agent_thread_context(agent, view: ConversationCompactView, *, source=None, compact_context=None) -> AgentThreadTurnContext:
    applied_view = (
        replace(view, compact_generation=compact_context.view.generation)
        if compact_context is not None else view
    )
    return AgentThreadTurnContext(
        thread_id=view.thread_id, compact_generation=view.compact_generation, compacted=view.is_candidate,
        injection=_render_agent_thread_context(agent, applied_view, include_transcript=False),
        history_seed=_agent_thread_history_seed(agent, applied_view), compact_context=compact_context, compact_source=source,
    )


# LLM: Tool-boundary-confirmed assistant parts and the terminal result are persisted in exact
# order with distinct typed dedupe identities. Runtime facts/tool fold stay only on the final.
# 函数用途: 子代理一轮结束后保存过程和最终正文，终态沿用主代理同一 typed 协议供恢复显示，不解析回复判断。
def append_subagent_thread_result(
    agent: object,
    task: object,
    *,
    attempt_id: str,
    result: object,
    turn_id: str = "",
) -> MessageLogEntry:
    manager = getattr(agent, "subagents", None)
    thread = ensure_subagent_thread(manager, task)
    if thread is None:
        raise RuntimeError("subagent ConversationStore is unavailable")
    selected_turn = str(turn_id or attempt_id)
    _append_subagent_thread_commentaries(
        agent,
        task,
        thread_id=thread.thread_id,
        attempt_id=attempt_id,
        turn_id=selected_turn,
        values=getattr(result, "assistant_commentary_messages", None),
    )
    metadata = _agent_turn_metadata(task, attempt_id)
    metadata["conversation_request_id"] = selected_turn
    metadata.update(
        {
            "assistant_part_id": "final",
            "runtime_status": str(
                getattr(result, "runtime_status", "ok") or "ok"
            ).strip(),
            "turn_end_reason": result_turn_end_reason(result),
            "operation_verification": public_operation_verification(
                getattr(result, "operation_verification", None)
            ),
        }
    )
    terminal_tool_fold = build_conversation_terminal_tool_fold(
        agent,
        getattr(result, "archive_tool_calls", None),
    )
    if terminal_tool_fold:
        metadata[TERMINAL_TOOL_FOLD_METADATA_KEY] = terminal_tool_fold
    native_envelope = canonical_native_messages_envelope(
        getattr(result, "canonical_native_messages", None)
    )
    if native_envelope:
        metadata[CANONICAL_NATIVE_MESSAGES_METADATA_KEY] = native_envelope
    return agent.conversation_store.messages.append_once(
        {
            "thread_id": thread.thread_id,
            "role": "assistant",
            "content": str(getattr(result, "response", "") or ""),
            "channel": _AGENT_THREAD_CHANNEL,
            "metadata": metadata,
        },
        dedupe_key=_agent_message_dedupe_key(task, selected_turn, "assistant"),
    )


# LLM: Commentary append uses the same child thread and exact attempt but separate host-authored
# part identities. It never attaches terminal operation/tool-fold metadata to process prose.
# 函数用途: 按顺序幂等保存子代理工具边界前的完整过程回复。
def _append_subagent_thread_commentaries(
    agent: object,
    task: object,
    *,
    thread_id: str,
    attempt_id: str,
    values: object,
    turn_id: str = "",
) -> None:
    if not isinstance(values, (list, tuple)):
        return
    for index, content in enumerate(values, start=1):
        text = str(content or "").strip()
        if not text:
            continue
        commentary_metadata = _agent_turn_metadata(task, attempt_id)
        commentary_metadata["conversation_request_id"] = turn_id or attempt_id
        commentary_metadata.update(
            {
                "assistant_part_id": f"commentary:{index}",
                "process": True,
            }
        )
        agent.conversation_store.messages.append_once(
            {
                "thread_id": thread_id,
                "role": "assistant",
                "content": text,
                "channel": _AGENT_THREAD_CHANNEL,
                "metadata": commentary_metadata,
            },
            dedupe_key=_agent_message_dedupe_key(
                task,
                turn_id or attempt_id,
                f"assistant-commentary-{index}",
            ),
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


# LLM: 摘要、证据与原文从同次view分离投影；未展示正文时不得读取来源，预计候选不授予提交权。
# 函数用途: 生成子代理历史说明及核验信息；原生模式的正文另由history seed交付，不提前物化。
def _render_agent_thread_context(
    agent: object,
    view: ConversationCompactView,
    *,
    include_transcript: bool = True,
) -> str:
    if not view.summary and not view.messages and not view.operation_evidence:
        return ""
    lines = [
        "# Agent Thread Context",
        f"- thread_id: {view.thread_id}",
        "- 以下内容只来自本子代理已经结束的历史轮次；不是当前新指令，当前任务要求优先。",
    ]
    if include_transcript and view.summary:
        lines.extend(
            [
                f"## Earlier Agent Summary (generation {view.compact_generation})",
                view.summary,
            ]
        )
    if view.operation_evidence:
        lines.extend(
            [
                "## Program-Verified Operations From Compacted History",
                json.dumps(
                    view.operation_evidence,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ]
        )
    bounded = _bounded_agent_history(agent, list(view.messages), view.history_token_budget) if include_transcript else ()
    if bounded:
        lines.append("## Recent Agent History")
        for row in bounded:
            content = (
                conversation_message_with_terminal_tool_fold(
                    row.content,
                    row.metadata,
                )
                if row.role == "assistant"
                else row.content
            )
            lines.append(
                f"- {row.role}: {json.dumps(content, ensure_ascii=False)}"
            )
    return "\n".join(lines)


# LLM: 普通Compact来源与候选完整保留于seed，不能再按展示字符窗少算容量；终态工具折叠仍沿原规则，不重读Store。
# 历史以只读来源冻结（地址视图或原内存行加子代理单行投影），只在发送边界物化，准备阶段不持有具体副本。
# 函数用途: 把子代理已经结束的历史轮次整理成下一次运行可复用的强类型会话种子。
def _agent_thread_history_seed(
    agent: object,
    view: ConversationCompactView,
) -> ConversationHistorySeed:
    return ConversationHistorySeed(
        compact_summary=str(view.summary or ""),
        compact_generation=max(0, int(view.compact_generation or 0)),
        source=freeze_history_source(view.messages, project_row=project_agent_history_row),
    )


# LLM: 普通渲染可按旧字符窗删整行；Compact来源/候选传preserve_complete保留全部原序及metadata，容量由请求门负责。
# 函数用途: 共用终态工具折叠投影，普通展示应用窗口，模型已选历史完整保留。
def _bounded_agent_history(
    agent: object,
    rows: list[MessageLogEntry],
    trigger_tokens: int,
    *,
    preserve_complete: bool = False,
) -> tuple[MessageLogEntry, ...]:
    config = getattr(agent, "config", None)
    configured_total = max(
        1000,
        int(getattr(config, "conversation_history_max_chars", 48_000) or 48_000),
    )
    total_limit = max(configured_total, max(0, int(trigger_tokens or 0)) * 3)
    selected: list[MessageLogEntry] = []
    used = 0
    for row in reversed(rows):
        projected = project_agent_history_row(row)
        if not preserve_complete and selected and used + len(projected.content) > total_limit:
            break
        selected.append(projected)
        used += len(projected.content)
    selected.reverse()
    return tuple(selected)


# LLM: 子代理历史唯一的单行投影：assistant 追加终态工具折叠，metadata 去掉折叠键，其余字段原样；
# 具体投影与只读来源种子重放共用它，不读 Store、不按正文判断范围。current_epoch 同 project_history_row。
# 函数用途: 把一条子代理线程原始记录变成模型历史行。
def project_agent_history_row(row: MessageLogEntry, *, current_epoch: float | None = None) -> MessageLogEntry:
    content = (
        conversation_message_with_terminal_tool_fold(row.content, row.metadata, current_epoch=current_epoch)
        if row.role == "assistant"
        else str(row.content or "")
    )
    return MessageLogEntry(
        message_id=row.message_id,
        thread_id=row.thread_id,
        role=row.role,
        content=content,
        channel=row.channel,
        channel_message_id=row.channel_message_id,
        created_at=row.created_at,
        metadata={
            key: value
            for key, value in row.metadata.items()
            if key != TERMINAL_TOOL_FOLD_METADATA_KEY
        },
    )


__all__ = [
    "AgentThreadTurnContext",
    "append_subagent_thread_result",
    "ensure_subagent_thread",
    "prepare_subagent_thread_turn",
]
