from __future__ import annotations

"""Durable ConversationStore ingress for the public one-shot ``my-agent run`` command.

``cli_run`` remains a standalone task/workspace execution.  This module only gives its
user/assistant turn the same authoritative transcript and message evidence used by Gateway;
it does not turn the command into an interactive Gateway conversation.
"""

# LLM: 仅由可信 CLI 宿主入口写原 owner 会话；线程、消息、运行参数共用既有学习来源投影，
#   明确禁止与自动化痕迹优先。不能放宽 Memory 晋升、升级旧未知线程或另建来源/会话账本。
# 模块用途: 给一次性运行保存权威用户、回复及异常前的工具记录，同时保持原独立任务和工作区生命周期。

import logging
from dataclasses import replace
from functools import partial

from ..conversation.channels import project_user_reply
from ..conversation.history_index import index_conversation_message
from ..conversation.learning_source import (
    LEARNING_SOURCE_KEY,
    derive_source_projection,
    merge_source_projections,
    unknown_projection,
    user_dialogue_projection,
)
from ..conversation.native_history import (
    CANONICAL_NATIVE_MESSAGES_METADATA_KEY,
    canonical_native_messages_envelope,
)
from ..memory_store.learning_exclusions import curator_learning_exclusions, thread_is_excluded
from ..tooling.operation_verification import public_operation_verification
from ..turn_end import result_turn_end_reason

_LOGGER = logging.getLogger(__name__)
_CLI_RUN_SOURCE = "cli_run"
_CLI_RUN_CHANNEL = "cli"
_CLI_RUN_USER_ID = "local-agent"


# LLM: This typed failure is reserved for the authoritative CLI user-turn write; callers must
# stop before model/tool execution and render a stable user-facing diagnostic.
# 类用途: 表示一次性 CLI 用户原文未能可靠进入会话账本，命令层据此安全停止而不打印 traceback。
class CliRunConversationPersistenceError(RuntimeError):
    """The one-shot CLI input could not enter its authoritative transcript."""

    # 复用既有 taxonomy 同语义 contract(CONVERSATION_PERSISTENCE_UNAVAILABLE,
    # category=state/retryable/「不执行模型或副作用」hint 与 cli_run 行为吻合),
    # 不重复注册带 CLI_RUN_ 前缀的变体 code(error_code 精确匹配不做前缀归一)。
    error_code = "CONVERSATION_PERSISTENCE_UNAVAILABLE"


# LLM: 可信 CLI 输入在模型前绑定原 owner/thread/user 与同一学习来源；续跑沿原 root 来源写 system，
#   重放源/谱系漂移拒绝，原 owner 排除政策只读一次并收紧；既有禁止来源不升级，提交侧仍最终复核。
# 函数用途: 为一次性 CLI 建立权威会话记录并写入用户原文；会写 ConversationStore 和检索索引。
def bind_cli_run_conversation(agent: object, params: object, user_prompt: str):
    """Persist one CLI user turn before model/tool execution and return bound params.

    The request id is the one-shot conversation binding and the append dedupe key.  Supplying
    the same request id with different text therefore fails closed in ConversationStore instead
    of silently reusing evidence from another request.
    """

    if not _is_cli_run(params):
        return params
    request_id = str(getattr(params, "request_id", "") or "").strip()
    task_id = str(
        getattr(params, "task_id", "")
        or getattr(params, "run_id", "")
        or request_id
    ).strip()
    prompt = str(user_prompt or "")
    store = getattr(agent, "conversation_store", None)
    if not request_id or not task_id or store is None:
        raise CliRunConversationPersistenceError(
            "CLI run 无法建立可靠会话记录，已在模型执行前停止。"
        )
    # 2026-08-14 根因3 设计 v2(审查意见1): 续跑轮复用同一 conversation
    # thread(root channel id), 不因 request_id=root#cont-N 建新 thread——
    # channel_conversation_id 用 root request_id。
    continuation_seq = int(getattr(params, "continuation_seq", 0) or 0)
    continuation = continuation_seq > 0
    # 缺口F(双席复核 seq1835): 续跑消息是带结构化元数据的 typed
    # continuation event——role=system(非 user, 不伪装成普通用户请求,
    # memory/curator 按 role=user 消费证据时天然排除), metadata 显式
    # 记 is_continuation/continuation_seq/continuation_reason/
    # continuation_parent_attempt_id; 首轮用户原文 role=user 不受影响,
    # dedupe_key 按轮 request_id 派生互不破坏。
    role = "system" if continuation else "user"
    # channel 身份用首轮 request_id(thread 的 channel_conversation_id),
    # 不是 root_run_id(可能是另一值)。
    root_request_id = str(
        getattr(params, "continuation_root_request_id", "") or request_id
    ).strip()
    try:
        excluded_ids, unreadable = curator_learning_exclusions(agent.home_paths)
        source = _cli_learning_source(params)
        if unreadable or root_request_id in excluded_ids:
            source = unknown_projection()
        thread = store.threads.get_or_create(
            {
                "canonical_user_id": _CLI_RUN_USER_ID,
                "channel": _CLI_RUN_CHANNEL,
                "channel_conversation_id": root_request_id if continuation else request_id,
                "channel_user_id": _CLI_RUN_USER_ID,
                "owner_id": _owner_id(agent),
                "owner_home": _owner_home(agent),
                "title": prompt[:80] or root_request_id,
                LEARNING_SOURCE_KEY: source,
            }
        )
        if continuation and str(getattr(params, "continuation_root_thread_id", "") or "") not in {"", thread.thread_id}:
            raise ValueError("CLI continuation root thread changed")
        attributes = dict(getattr(params, "task_attributes", None) or {})
        attributes["conversation_thread_id"] = str(thread.thread_id)
        source = _cli_learning_source(params, thread.learning_source)
        if unreadable or thread_is_excluded(thread, excluded_ids):
            source = unknown_projection()
        attributes[LEARNING_SOURCE_KEY] = source
        bound = replace(params, task_attributes=attributes)
        metadata = _message_metadata(bound)
        entry = store.messages.append_once(
            {
                "thread_id": thread.thread_id,
                "role": role,
                "content": prompt,
                "channel": _CLI_RUN_CHANNEL,
                "metadata": metadata,
            },
            dedupe_key=_dedupe_key(request_id, role),
        )
        _require_matching_message_metadata(entry, bound)
        _index_message_best_effort(agent, store, entry)
        if not continuation:
            from ..conversation.goal_progress_fuse import reset_goal_progress_fuse

            reset_goal_progress_fuse(store, thread.thread_id)
    except Exception as exc:
        _LOGGER.error(
            "CLI run 用户消息写入失败(thread=%s, request=%s): %s: %s",
            str((thread.thread_id if "thread" in dir() else "") or ""),
            request_id,
            type(exc).__name__,
            exc,
        )
        raise CliRunConversationPersistenceError(
            "CLI run 用户消息无法可靠写入 ConversationStore，已在模型执行前停止。"
        ) from exc
    # Do not predeclare conversation_task_id.  cli_run owns a standalone task/workspace; a
    # ConversationStore task link may be created only by a real task-promoting tool.
    return replace(bound, partial_turn_callback=partial(persist_cli_run_assistant, agent, bound))


# LLM: 原幂等键及native envelope不变；已提交commentary按结构化part序号保留，空final不伪造正文，联测reload与中断。
# 函数用途: 保存CLI模型说明、最终回复及工具历史，失败明确标记降级。
def persist_cli_run_assistant(agent: object, params: object, result: object) -> bool:
    """Append the final public CLI reply exactly once; degrade only this trailing write."""

    if not _is_cli_run(params):
        return True
    attributes = getattr(params, "task_attributes", None)
    attributes = attributes if isinstance(attributes, dict) else {}
    thread_id = str(attributes.get("conversation_thread_id") or "").strip()
    request_id = str(getattr(params, "request_id", "") or "").strip()
    store = getattr(agent, "conversation_store", None)
    projection = project_user_reply(str(getattr(result, "response", "") or ""))
    native = canonical_native_messages_envelope(getattr(result, "canonical_native_messages", None))
    if projection.internal_signal or (not projection.content and not native):
        return True
    if not thread_id or not request_id or store is None:
        return _mark_assistant_persist_degraded(
            result,
            "CLI run assistant transcript lacks its bound ConversationStore identity",
        )
    metadata = _message_metadata(params)
    metadata["turn_end_reason"] = result_turn_end_reason(result)
    if native:
        metadata[CANONICAL_NATIVE_MESSAGES_METADATA_KEY] = native
    metadata["operation_verification"] = public_operation_verification(
        getattr(result, "operation_verification", None)
    )
    try:
        for index, text in enumerate(getattr(result, "assistant_commentary_messages", ()) or (), start=1):
            content = project_user_reply(str(text or "")).content
            if content:
                store.messages.append_once(
                    {"thread_id": thread_id, "role": "assistant", "content": content, "channel": _CLI_RUN_CHANNEL,
                     "metadata": {**_message_metadata(params), "assistant_part_id": f"commentary:{index}"}},
                    dedupe_key=f"{_dedupe_key(request_id, 'assistant')}:commentary:{index}",
                )
        entry = store.messages.append_once(
            {
                "thread_id": thread_id,
                "role": "assistant",
                "content": projection.content,
                "channel": _CLI_RUN_CHANNEL,
                "metadata": metadata,
            },
            dedupe_key=_dedupe_key(request_id, "assistant"),
        )
        _require_matching_message_metadata(entry, params)
        _index_message_best_effort(agent, store, entry)
        return True
    except Exception as exc:
        _LOGGER.error(
            "CLI run assistant transcript append failed(thread=%s): %s: %s",
            thread_id,
            type(exc).__name__,
            exc,
        )
        return _mark_assistant_persist_degraded(
            result,
            "CLI run assistant transcript append failed",
        )


# LLM: 来源仅在可信 CLI 输入边界构造，已有线程/运行字段中的 false 与坏投影优先；自动化痕迹复用
#   既有 derive 合同，不能升级旧未知 root，也不从续跑 system 正文或模型 origin 生成用户证据。
# 函数用途: 合并原宿主学习来源；新用户输入可声明对话来源，续跑仅继承真实 root 的结构化来源。
def _cli_learning_source(params: object, thread_source: object = None) -> dict[str, object]:
    attrs = dict(getattr(params, "task_attributes", None) or {})
    continuation = int(getattr(params, "continuation_seq", 0) or 0) > 0
    sources = [thread_source] if thread_source else []
    if continuation and not thread_source:
        sources.append(unknown_projection())
    if LEARNING_SOURCE_KEY in attrs:
        sources.append(attrs[LEARNING_SOURCE_KEY])
    derived = derive_source_projection(attrs)
    if derived is not None:
        sources.append(derived)
    if not continuation:
        sources.append(user_dialogue_projection())
    return merge_source_projections(sources)


# LLM: 消息谱系只来自宿主 RunParams，与运行学习来源同投影；续跑原字段保持 typed event，不制造 user 行。
# 函数用途: 生成原请求、运行、任务与学习来源元数据，供持久化、重放核对及证据读取。
def _message_metadata(params: object) -> dict[str, object]:
    request_id = str(getattr(params, "request_id", "") or "").strip()
    attrs = dict(getattr(params, "task_attributes", None) or {})
    metadata = {
        "gateway_request_id": request_id,
        "request_id": request_id,
        "run_id": str(getattr(params, "run_id", "") or "").strip(),
        "task_id": str(getattr(params, "task_id", "") or "").strip(),
        LEARNING_SOURCE_KEY: attrs.get(LEARNING_SOURCE_KEY, unknown_projection()),
    }
    continuation_seq = int(getattr(params, "continuation_seq", 0) or 0)
    if continuation_seq > 0:
        metadata.update({
            "is_continuation": True,
            "continuation_seq": continuation_seq,
            "continuation_reason": str(getattr(params, "continuation_reason", "") or ""),
            "continuation_parent_attempt_id": str(getattr(params, "continuation_parent_attempt_id", "") or ""),
        })
    return metadata


# LLM: Keep this adapter source-exact so programmatic runs and Gateway remain unchanged.
# 函数用途: 判断当前运行是不是公开的一次性 CLI run。
def _is_cli_run(params: object) -> bool:
    return str(getattr(params, "source", "") or "").strip().lower() == _CLI_RUN_SOURCE


# LLM: One request and role own one transcript append identity across process retries.
# 函数用途: 生成 CLI 用户或 assistant 消息的稳定幂等键。
def _dedupe_key(request_id: str, role: str) -> str:
    return f"cli_run:{request_id}:{role}"


# LLM: 原文相等也不能复用不同来源、run/task 或续跑父谱系；在模型前精确核对旧消息，不重写旧来源。
# 函数用途: 重放命中旧消息时核对结构化运行身份，发现同请求串线就拒绝继续。
def _require_matching_message_metadata(entry: object, params: object) -> None:
    """Reject a reused request identity whose typed run lineage has drifted."""

    actual = getattr(entry, "metadata", None)
    actual = actual if isinstance(actual, dict) else {}
    expected = _message_metadata(params)
    if any(actual.get(key) != value for key, value in expected.items()):
        raise ValueError("CLI run conversation identity reused with different run metadata")


# LLM: Thread ownership is projected from the already-resolved agent home, never from CLI text.
# 函数用途: 读取当前 Agent 已解析的 owner 标识。
def _owner_id(agent: object) -> str:
    return str(getattr(getattr(agent, "home_paths", None), "owner_id", "") or "")


# LLM: Persist only the canonical resolved owner home on the thread binding.
# 函数用途: 读取当前 Agent 已解析的 owner home 路径。
def _owner_home(agent: object) -> str:
    return str(
        getattr(getattr(agent, "home_paths", None), "owner_home_dir", "") or ""
    )


# LLM: ConversationStore remains authoritative; LocalStore indexing may degrade and must invalidate
# the cached indexed-thread marker instead of failing the transcript commit.
# 函数用途: 最佳努力更新会话检索索引；失败只记日志并让后续重建，不回滚原文账本。
def _index_message_best_effort(agent: object, store: object, entry: object) -> None:
    try:
        index_conversation_message(agent, store, entry)
    except Exception as exc:
        _LOGGER.warning(
            "CLI run conversation index failed(message=%s): %s: %s",
            getattr(entry, "message_id", ""),
            type(exc).__name__,
            exc,
        )
        indexed = getattr(agent, "_conversation_indexed_threads", set())
        indexed.discard(str(getattr(entry, "thread_id", "") or ""))


# LLM: Use AgentRunResult typed fields as the sole CLI assistant-write observability contract.
# 函数用途: 标记最终回复已返回但 assistant 会话落账降级，并返回 False 供调用方判断。
def _mark_assistant_persist_degraded(result: object, message: str) -> bool:
    result.conversation_persist_degraded = True
    result.conversation_persist_error = str(message)
    return False


__all__ = [
    "CliRunConversationPersistenceError",
    "bind_cli_run_conversation",
    "persist_cli_run_assistant",
]
