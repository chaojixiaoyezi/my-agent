from __future__ import annotations

"""Durable ConversationStore ingress for the public one-shot ``my-agent run`` command.

``cli_run`` remains a standalone task/workspace execution.  This module only gives its
user/assistant turn the same authoritative transcript and message evidence used by Gateway;
it does not turn the command into an interactive Gateway conversation.
"""

# LLM: This module adapts only cli_run into the existing owner ConversationStore; it must not
# create a second transcript, relax Memory evidence, or reinterpret a one-shot run as Gateway.
# 模块用途: 给一次性 my-agent run 补齐权威用户/回复记录，同时保持原独立任务和工作区生命周期。

import logging
from dataclasses import replace

from ..conversation.channels import project_user_reply
from ..conversation.history_index import index_conversation_message
from ..tooling.operation_verification import public_operation_verification

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


# LLM: Only cli_run enters here; it writes one owner-scoped user message before execution and
# returns immutable run params carrying the real thread id, never a fabricated task-link id.
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
    try:
        thread = store.get_or_create_thread(
            {
                "canonical_user_id": _CLI_RUN_USER_ID,
                "channel": _CLI_RUN_CHANNEL,
                "channel_conversation_id": request_id,
                "channel_user_id": _CLI_RUN_USER_ID,
                "owner_id": _owner_id(agent),
                "owner_home": _owner_home(agent),
                "title": prompt[:80] or request_id,
            }
        )
        entry = store.append_message_once(
            {
                "thread_id": thread.thread_id,
                "role": "user",
                "content": prompt,
                "channel": _CLI_RUN_CHANNEL,
                "metadata": _message_metadata(params),
            },
            dedupe_key=_dedupe_key(request_id, "user"),
        )
        _require_matching_message_metadata(entry, params)
        _index_message_best_effort(agent, store, entry)
    except Exception as exc:
        raise CliRunConversationPersistenceError(
            "CLI run 用户消息无法可靠写入 ConversationStore，已在模型执行前停止。"
        ) from exc
    attributes = dict(getattr(params, "task_attributes", None) or {})
    attributes["conversation_thread_id"] = str(thread.thread_id)
    # Do not predeclare conversation_task_id.  cli_run owns a standalone task/workspace; a
    # ConversationStore task link may be created only by a real task-promoting tool.
    return replace(params, task_attributes=attributes)


# LLM: The final public projection is appended idempotently after execution; failure must only
# mark typed delivery degradation because the user input and completed operation facts remain.
# 函数用途: 把 CLI 最终公开回复写入同一会话；失败时只更新结果的降级字段并记录错误日志。
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
    if projection.internal_signal or not projection.content:
        return True
    if not thread_id or not request_id or store is None:
        return _mark_assistant_persist_degraded(
            result,
            "CLI run assistant transcript lacks its bound ConversationStore identity",
        )
    metadata = _message_metadata(params)
    metadata["operation_verification"] = public_operation_verification(
        getattr(result, "operation_verification", None)
    )
    try:
        entry = store.append_message_once(
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


# LLM: Message lineage comes solely from typed run parameters and is shared by evidence lookup,
# retries, Curator ingestion, and corruption checks.
# 函数用途: 生成会话消息的请求、运行和任务标识元数据，不读取模型正文。
def _message_metadata(params: object) -> dict[str, object]:
    request_id = str(getattr(params, "request_id", "") or "").strip()
    return {
        "gateway_request_id": request_id,
        "request_id": request_id,
        "run_id": str(getattr(params, "run_id", "") or "").strip(),
        "task_id": str(getattr(params, "task_id", "") or "").strip(),
    }


# LLM: Keep this adapter source-exact so programmatic runs and Gateway remain unchanged.
# 函数用途: 判断当前运行是不是公开的一次性 CLI run。
def _is_cli_run(params: object) -> bool:
    return str(getattr(params, "source", "") or "").strip().lower() == _CLI_RUN_SOURCE


# LLM: One request and role own one transcript append identity across process retries.
# 函数用途: 生成 CLI 用户或 assistant 消息的稳定幂等键。
def _dedupe_key(request_id: str, role: str) -> str:
    return f"cli_run:{request_id}:{role}"


# LLM: Content equality alone cannot authorize reuse when run/task lineage differs; fail closed
# before a stale message can become Memory evidence for another execution.
# 函数用途: 重放命中旧消息时核对结构化运行身份，发现同请求串线就拒绝继续。
def _require_matching_message_metadata(entry: object, params: object) -> None:
    """Reject a reused request identity whose typed run lineage has drifted."""

    actual = getattr(entry, "metadata", None)
    actual = actual if isinstance(actual, dict) else {}
    expected = _message_metadata(params)
    if any(str(actual.get(key) or "") != str(value or "") for key, value in expected.items()):
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
