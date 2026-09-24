# LLM: Gateway 正文投影、canonical 追加和延迟补交共用本模块；保留 request/assistant_part 去重、原生信封与原提交顺序。
# 模块用途: 保存或原样补交本轮会话记录，索引只是投影；错误与停止不伪造用户答复，改动需联测上下文读取。
from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..conversation.channels import (
    LOCAL_CHAT_CHANNEL,
    LOCAL_CHAT_SOURCE,
    project_host_paths_for_channel,
    project_user_reply,
    redact_structured_identifiers,
    thread_channel,
)
from ..conversation.history_index import (
    ensure_thread_history_indexed,
    index_conversation_message,
)
from ..conversation.native_history import (
    CANONICAL_NATIVE_MESSAGES_METADATA_KEY,
    canonical_native_messages_envelope,
)
from ..conversation.tool_context_window import (
    TERMINAL_TOOL_FOLD_METADATA_KEY,
    build_conversation_terminal_tool_fold,
)
from ..runtime_errors import runtime_error_report
from ..tooling.operation_verification import public_operation_verification
from ..turn_end import normalize_turn_end_reason, result_turn_end_reason
from . import request_binding
from .io import (
    read_json_file_report,
)
from .request_errors import (
    UserReplyUnavailableError,
    gateway_model_response_error_projection,
)
from .response_renderer import is_silent_user_stop
from .stream_writer import BufferedChunkStreamWriter

if TYPE_CHECKING:
    from ...core import SimpleAgent
    from . import request_context

logger = logging.getLogger(__name__)


# LLM: 这是同一结果的不可变传输包，不是第二份会话事实源；正常提交和 repair 必须使用相同投影。
# 类用途: 将原生消息、结束原因与独立显示快照一起交给 final/repair，展示数据不进入模型历史。
@dataclass(frozen=True)
class GatewayAssistantTurn:
    native_messages: object = None
    end_reason: str = ""
    display_snapshot: dict | None = None

    # LLM: 仅构建新的 metadata 字典；不改原生内容、不写文件、不推断缺失 reason。
    # 函数用途: 为正常落账与延迟补交生成同一份元数据；显示快照复制后传出，避免调用方污染原包。
    def metadata(self) -> dict[str, object]:
        metadata: dict[str, object] = {}
        if envelope := canonical_native_messages_envelope(self.native_messages):
            metadata[CANONICAL_NATIVE_MESSAGES_METADATA_KEY] = envelope
        if reason := normalize_turn_end_reason(self.end_reason):
            metadata["turn_end_reason"] = reason
        if self.display_snapshot:
            metadata["background_transcript_request_id"] = self.display_snapshot["request_id"]
            metadata["background_display_turn"] = copy.deepcopy(self.display_snapshot)
        return metadata


# LLM: 正常回复和 typed 停止都保留 native IR；停止只禁止正文投递，不禁止已有事实进入同一 canonical final。
# 函数用途: 保存回复或中断前的执行历史；空正文不伪装成模型答复，写失败沿原 repair 补交。
def persist_gateway_assistant_result(
    context: request_context.GatewayAskRunContext,
    conversation: request_context.GatewayConversationContext,
    result: object,
):
    if is_silent_user_stop(result):
        persist_gateway_partial_result(context, conversation, result)
        result.channel_delivery = silent_user_stop_delivery()
        return result
    delivery_projection = project_user_reply(str(getattr(result, "response", "") or ""))
    if delivery_projection.internal_signal:
        raise UserReplyUnavailableError("任务只返回了运行时内部信号，没有生成可交付给用户的回复")
    if not delivery_projection.content and not gateway_model_response_error_projection(result):
        raise UserReplyUnavailableError("模型没有生成可安全交付的自然回复")
    channel_delivery = delivery_projection.to_dict()
    public_content = redact_structured_identifiers(
        project_host_paths_for_channel(
            delivery_projection.content,
            gateway_conversation_channel(context, conversation),
        ),
        gateway_identifier_redactions(context, conversation, result=result),
        channel=gateway_conversation_channel(context, conversation),
    )
    channel_delivery["content"] = public_content
    channel_delivery["artifacts"] = _metadata_artifact_refs(
        getattr(result, "delivery_artifacts", None)
    )
    operation_verification = public_operation_verification(
        getattr(result, "operation_verification", None)
    )
    terminal_tool_fold = build_conversation_terminal_tool_fold(
        context.agent,
        getattr(result, "archive_tool_calls", None),
    )
    channel_delivery["operation_verification"] = operation_verification
    result.channel_delivery = channel_delivery
    commentaries_persisted = _persist_gateway_assistant_commentaries(
        context,
        conversation,
        result,
    )
    if not commentaries_persisted:
        result.conversation_persist_degraded = True
        result.conversation_persist_error = "assistant commentary append deferred for repair"
    assistant_turn = GatewayAssistantTurn(
        native_messages=getattr(result, "canonical_native_messages", None),
        end_reason=result_turn_end_reason(result),
        display_snapshot=context.on_chunk.prepare_display_history() if isinstance(context.on_chunk, BufferedChunkStreamWriter) else None,
    )
    if not append_gateway_conversation_message(
        context.agent,
        context.request,
        conversation,
        request_id=context.request_id,
        role="assistant",
        content=public_content,
        delivery_artifacts=channel_delivery["artifacts"],
        operation_verification=channel_delivery.get("operation_verification"),
        terminal_tool_fold=terminal_tool_fold,
        assistant_turn=assistant_turn,
        assistant_part_id="final",
    ):
        _queue_gateway_conversation_repair(
            context.agent,
            context.request,
            conversation,
            request_id=context.request_id,
            role="assistant",
            content=public_content,
            delivery_artifacts=channel_delivery["artifacts"],
            operation_verification=channel_delivery.get("operation_verification"),
            terminal_tool_fold=terminal_tool_fold,
            assistant_turn=assistant_turn,
            assistant_part_id="final",
        )
        result.conversation_persist_degraded = True
        result.conversation_persist_error = "assistant transcript append deferred for repair"
    return result


# LLM: 回调在异常栈退出前使用宿主冻结的 owner/thread/request；不重新取当前会话，不发频道消息，也不调模型。
# 函数用途: 将中断或异常前的原生工具历史写进原会话；沿用正常 final 的幂等和 repair，停止提示不写正文。
def persist_gateway_partial_result(context, conversation, result) -> None:
    if not getattr(result, "canonical_native_messages", None):
        return
    turn = GatewayAssistantTurn(
        native_messages=result.canonical_native_messages,
        end_reason=result_turn_end_reason(result),
        display_snapshot=context.on_chunk.prepare_display_history()
        if isinstance(context.on_chunk, BufferedChunkStreamWriter) else None,
    )
    fields = dict(
        request_id=context.request_id, role="assistant", content="", assistant_turn=turn,
    )
    if not append_gateway_conversation_message(context.agent, context.request, conversation, **fields):
        _queue_gateway_conversation_repair(context.agent, context.request, conversation, **fields)
        result.conversation_persist_degraded = True
        result.conversation_persist_error = "partial native transcript append deferred for repair"


# LLM: Only typed tool-boundary commentary captured by the run sink may become additional
# assistant parts. Each part has its own structured identity; content length/order is not guessed.
# 函数用途: 在最终回复前按原顺序持久化模型已确认的过程回复，让后续连续任务保留完整上下文。
def _persist_gateway_assistant_commentaries(
    context: request_context.GatewayAskRunContext,
    conversation: request_context.GatewayConversationContext,
    result: object,
) -> bool:
    raw_messages = getattr(result, "assistant_commentary_messages", None)
    if not isinstance(raw_messages, (list, tuple)):
        return True
    identifiers = gateway_identifier_redactions(context, conversation, result=result)
    channel = gateway_conversation_channel(context, conversation)
    all_persisted = True
    for index, raw in enumerate(raw_messages, start=1):
        projection = project_user_reply(str(raw or ""))
        content = redact_structured_identifiers(
            project_host_paths_for_channel(projection.content, channel),
            identifiers,
            channel=channel,
        ).strip()
        if not content:
            continue
        part_id = f"commentary:{index}"
        if append_gateway_conversation_message(
            context.agent,
            context.request,
            conversation,
            request_id=context.request_id,
            role="assistant",
            content=content,
            assistant_part_id=part_id,
        ):
            continue
        _queue_gateway_conversation_repair(
            context.agent,
            context.request,
            conversation,
            request_id=context.request_id,
            role="assistant",
            content=content,
            assistant_part_id=part_id,
        )
        all_persisted = False
    return all_persisted


_IDENTIFIER_PUBLIC_LABELS = {
    "user_id": "当前用户",
    "canonical_user_id": "当前用户",
    "channel_user_id": "当前用户",
    "conversation_id": "当前会话",
    "channel_conversation_id": "当前会话",
    "channel_chat_id": "当前会话",
    "message_id": "当前消息",
    "request_id": "当前请求",
    "thread_id": "当前会话",
    "task_id": "当前任务",
    "goal_id": "当前目标",
    "audit_id": "当前监控",
    "owner_id": "当前空间",
    "reply_to": "当前消息",
    "progress_handle": "当前进度",
}


# LLM: 用户出口遮蔽只枚举可信 request/conversation/task 结构中的稳定字段；不得解析模型正文猜 ID。
# 函数用途: 为最终回复和流式 commentary 生成同一份精确标识替换表，内部 transcript 保留原始正文。
def gateway_identifier_redactions(
    context: request_context.GatewayAskRunContext,
    conversation: request_context.GatewayConversationContext,
    *,
    result: object | None = None,
) -> tuple[tuple[object, str], ...]:
    pairs: list[tuple[object, str]] = [(context.request_id, "当前请求")]
    for source in (
        context.request,
        context.request.get("metadata"),
        context.request.get("conversation"),
        context.request.get("conversation_runtime"),
        context.request.get("conversation_audit_scope"),
    ):
        if not isinstance(source, dict):
            continue
        pairs.extend(
            (source.get(key), label)
            for key, label in _IDENTIFIER_PUBLIC_LABELS.items()
            if source.get(key)
        )
    pairs.append((conversation.thread_id, "当前会话"))
    if conversation.scope is not None:
        pairs.extend(
            (
                (conversation.scope.owner_id, "当前空间"),
                (conversation.scope.canonical_user_id, "当前用户"),
                (conversation.scope.channel_conversation_id, "当前会话"),
                (conversation.scope.channel_user_id, "当前用户"),
            )
        )
    if isinstance(conversation.thread_goal, dict):
        pairs.extend(
            (
                (conversation.thread_goal.get("goal_id"), "当前目标"),
                (conversation.thread_goal.get("task_id"), "当前任务"),
            )
        )
    if conversation.workspace_task is not None:
        pairs.append((conversation.workspace_task.task_id, "当前任务"))
    store = getattr(context.agent, "conversation_store", None)
    if store is not None and conversation.thread_id:
        links, _load_errors = store.tasks.list_report(conversation.thread_id)
        for link in links:
            task_id = str(getattr(link, "task_id", "") or "").strip()
            if not task_id:
                continue
            work_kind = str(getattr(link, "work_kind", "") or "").strip().lower()
            label = {
                "audit": "当前监控",
                "goal": "当前目标",
            }.get(work_kind, "当前任务")
            pairs.append((task_id, label))
    pairs.extend(_result_identifier_redactions(result))
    return tuple(pairs)


# LLM: 本轮工具新生成的运行标识只能从可信工具结果外壳读取；不得扫描模型正文猜测 ID。
# 函数用途: 让新建 watch 等本轮才出现的内部标识在最终正文与会话落账前统一被遮蔽。
def _result_identifier_redactions(result: object | None) -> tuple[tuple[object, str], ...]:
    pairs: list[tuple[object, str]] = []
    records = getattr(result, "archive_tool_calls", None) if result is not None else None
    for record in records or ():
        if not isinstance(record, dict):
            continue
        tool = str(record.get("tool") or "").strip()
        parameters = record.get("parameters")
        if tool == "watch_stream" and isinstance(parameters, dict):
            pairs.append((parameters.get("watch_id"), "当前来源读取"))
        payload = _complete_runtime_tool_output(record)
        if tool == "watch_stream":
            pairs.append((payload.get("watch_id"), "当前来源读取"))
            source_binding = payload.get("source_binding")
            if isinstance(source_binding, dict):
                pairs.append((source_binding.get("watch_id"), "当前来源读取"))
        elif tool == "publish_audit_update":
            pairs.append((payload.get("audit_id"), "当前监控"))
    return tuple(pairs)


# LLM: 只读取可信工具记录的完整内存预览；外置或非对象结果不能被当成标识来源，不扫描正文猜 ID。
# 函数用途: 为公开标识投影解析可用的工具外壳，缺少完整结果时返回空事实。
def _complete_runtime_tool_output(record: dict[str, object]) -> dict[str, object]:
    """Parse only a complete, in-memory runtime tool envelope preview."""
    if record.get("output_externalized") is True:
        return {}
    raw = record.get("output_preview")
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


# LLM: 搜索索引仅是 canonical 历史的投影；重建失败只撤缓存并记诊断，不修改历史或阻断模型入口。
# 函数用途: 在读取上下文前同步当前线程的索引，失败时留下可重试线索。
def ensure_gateway_conversation_index(
    agent: SimpleAgent,
    store: object,
    thread_id: str,
) -> None:
    """Best-effort rebuild of the owner-local search projection; raw transcript stays authoritative."""
    try:
        ensure_thread_history_indexed(agent, store, thread_id)
    except Exception as exc:
        logger.warning(
            "会话搜索索引更新失败(thread=%s): %s: %s",
            thread_id,
            type(exc).__name__,
            exc,
        )
        indexed = getattr(agent, "_conversation_indexed_threads", set())
        indexed.discard(thread_id)
        local_store = getattr(agent, "local_store", None)
        if local_store is not None:
            local_store.record_event(
                "conversation_history_index_failed",
                payload={
                    "thread_id": thread_id,
                    "error": f"{type(exc).__name__}: {exc}",
                },
            )


# LLM: 近期产物只从当前 thread 的 assistant metadata 或旧版完整完成协议恢复；普通对话文字不获此权威。
# 函数用途: 为“把上一个文件发我”提供已登记产物引用，避免模型重新搜索、复制或生成。
def gateway_recent_artifacts(
    agent: SimpleAgent,
    thread_id: str,
    current_request_id: str,
    load_errors: list[dict],
) -> tuple[dict[str, object], ...]:
    store = getattr(agent, "conversation_store", None)
    try:
        # 与原 recent_report(limit=80) 同窗同错，只保留每行的产物引用（新到旧），不驻留正文。
        refs_by_row, errors = store.messages.recent_projection_report(
            thread_id, limit=80, project=lambda row: _conversation_row_artifacts(row, current_request_id),
        )
    except Exception as exc:
        load_errors.append(runtime_error_report(exc, context="gateway.conversation.recent_artifacts"))
        return ()
    load_errors.extend(error for error in errors if isinstance(error, dict))
    selected: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    candidates = [ref for refs in refs_by_row for ref in reversed(refs)]
    for ref in candidates:
        key = (str(ref.get("artifact_id") or ""), str(ref.get("path") or ""))
        if key in seen:
            continue
        seen.add(key)
        selected.append(ref)
        if len(selected) >= 20:
            return tuple(reversed(selected))
    return tuple(reversed(selected))


# LLM: 只有非当前请求的 assistant 行能贡献产物引用；权威来源只有结构化 metadata。
# 函数用途: 从一条会话消息提取可信的最小产物引用。
def _conversation_row_artifacts(row: object, current_request_id: str) -> list[dict[str, object]]:
    if str(getattr(row, "role", "") or "").strip().lower() != "assistant":
        return []
    metadata = getattr(row, "metadata", None)
    metadata = metadata if isinstance(metadata, dict) else {}
    if metadata.get("gateway_request_id") == current_request_id:
        return []
    return _metadata_artifact_refs(metadata.get("delivery_artifacts"))


# LLM: delivery_artifacts 写入会话 metadata 前只保留最小稳定字段；不得复制验收协议或任意嵌套对象。
# 函数用途: 清洗能跨轮复用的产物引用。
def _metadata_artifact_refs(value: object) -> list[dict[str, object]]:
    if not isinstance(value, (list, tuple)):
        return []
    refs: list[dict[str, object]] = []
    seen: set[tuple[str, str]] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        artifact_id = str(item.get("artifact_id") or "").strip()
        path = str(item.get("path") or "").strip()
        if not artifact_id and not path:
            continue
        key = (artifact_id, path)
        if key in seen:
            continue
        seen.add(key)
        refs.append(
            {
                "artifact_id": artifact_id,
                "path": path,
                "name": str(item.get("name") or Path(path).name or artifact_id),
                "kind": str(item.get("kind") or "file"),
                "ok": item.get("ok") is True,
            }
        )
    return refs


# LLM: Assistant prose, artifacts, operation facts, and one immutable terminal tool fold are
# persisted in separate fields. The public body must stay unchanged while later model turns may
# consume the bounded fold from metadata.
# 本地队列来源决定展示通道，owner provider 仍只负责身份；实时和重放不得使用不同的路径策略。
# 函数用途: 幂等追加 Gateway 消息；空正文停止、截断或错误也落 final 元数据，保留工具历史但不生成替代正文。
def append_gateway_conversation_message(
    agent: SimpleAgent,
    request: dict,
    conversation: request_context.GatewayConversationContext,
    *,
    request_id: str,
    role: str,
    content: str,
    delivery_artifacts: object = (),
    operation_verification: object = None,
    terminal_tool_fold: object = None,
    assistant_turn: GatewayAssistantTurn | None = None,
    assistant_part_id: str = "final",
) -> bool:
    empty_terminal = (
        role == "assistant" and assistant_part_id == "final"
        and assistant_turn is not None and assistant_turn.end_reason in {"max-tokens", "error", "aborted", "interrupted"}
    )
    if not conversation.thread_id or (not content and not empty_terminal):
        return not conversation.thread_id
    store = getattr(agent, "conversation_store", None)
    metadata = request.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    channel_message_id = str(metadata.get("message_id") or "") if role == "user" else ""
    try:
        # 与原 recent_report(limit=100) 同窗同错，只保留去重所需字段，不驻留正文；判定顺序与原实现相同。
        facts, errors = store.messages.recent_projection_report(conversation.thread_id, limit=100, project=_dedupe_facts)
        if errors:
            raise OSError("conversation message ledger could not be read reliably")
        normalized_part_id = (
            str(assistant_part_id or "final").strip() or "final" if role == "assistant" else ""
        )
        if any(
            _gateway_message_matches_part(
                fact,
                role=role,
                request_id=request_id,
                channel_message_id=channel_message_id,
                assistant_part_id=normalized_part_id,
            )
            for fact in facts
        ):
            return True
        entry_metadata: dict[str, object] = {
            "conversation_request_id": request_id,
            "gateway_request_id": request_id,
            "delivery_artifacts": _metadata_artifact_refs(delivery_artifacts),
        }
        entry_metadata.update(request_binding.gateway_message_work_scope(request))
        if role == "assistant":
            entry_metadata["assistant_part_id"] = normalized_part_id
        if role == "assistant" and operation_verification is not None:
            entry_metadata["operation_verification"] = public_operation_verification(
                operation_verification
            )
        if role == "assistant" and isinstance(terminal_tool_fold, dict):
            fold = dict(terminal_tool_fold)
            if fold:
                entry_metadata[TERMINAL_TOOL_FOLD_METADATA_KEY] = fold
        if role == "assistant" and normalized_part_id == "final" and assistant_turn is not None:
            entry_metadata.update(assistant_turn.metadata())
        entry = store.messages.append(
            {
                "thread_id": conversation.thread_id,
                "role": role,
                "content": content,
                "channel": gateway_request_channel(request) or thread_channel(store, conversation.thread_id),
                "channel_message_id": channel_message_id,
                "metadata": entry_metadata,
            }
        )
        try:
            index_conversation_message(agent, store, entry)
        except Exception as exc:
            logger.warning(
                "会话消息索引失败(thread=%s, message=%s): %s: %s",
                conversation.thread_id,
                entry.message_id,
                type(exc).__name__,
                exc,
            )
            indexed = getattr(agent, "_conversation_indexed_threads", set())
            indexed.discard(conversation.thread_id)
        return True
    except Exception as exc:
        logger.error(
            "会话消息持久化失败(thread=%s, role=%s): %s: %s",
            conversation.thread_id,
            role,
            type(exc).__name__,
            exc,
        )
        return False


# LLM: Delayed repair preserves the same canonical request identity, sanitized body, artifacts,
# operation facts, terminal native messages and turn-end reason as normal append; no repair may lose history.
# 展示通道与正常提交共用 resolver，不把 owner provider 当作本地 TUI 的外部投递通道。
# 函数用途: 写失败时保存原消息与结束原因，包括空正文中断、截断或错误；补交不再次执行任务。
def _queue_gateway_conversation_repair(
    agent: SimpleAgent,
    request: dict,
    conversation: request_context.GatewayConversationContext,
    *,
    request_id: str,
    role: str,
    content: str,
    delivery_artifacts: object = (),
    operation_verification: object = None,
    terminal_tool_fold: object = None,
    assistant_turn: GatewayAssistantTurn | None = None,
    assistant_part_id: str = "final",
) -> None:
    store = getattr(agent, "conversation_store", None)
    root = getattr(getattr(store, "storage", None), "root", None)
    empty_terminal = (
        role == "assistant" and assistant_part_id == "final"
        and assistant_turn is not None and assistant_turn.end_reason in {"max-tokens", "error", "aborted", "interrupted"}
    )
    if not root or not conversation.thread_id or (not content and not empty_terminal):
        return
    repair_metadata: dict[str, object] = {
        "conversation_request_id": request_id,
        "gateway_request_id": request_id,
        "repair": True,
        "delivery_artifacts": _metadata_artifact_refs(delivery_artifacts),
    }
    repair_metadata.update(request_binding.gateway_message_work_scope(request))
    normalized_part_id = str(assistant_part_id or "final").strip() or "final"
    if role == "assistant":
        repair_metadata["assistant_part_id"] = normalized_part_id
    if role == "assistant" and operation_verification is not None:
        repair_metadata["operation_verification"] = public_operation_verification(
            operation_verification
        )
    if role == "assistant" and isinstance(terminal_tool_fold, dict):
        fold = dict(terminal_tool_fold)
        if fold:
            repair_metadata[TERMINAL_TOOL_FOLD_METADATA_KEY] = fold
    if role == "assistant" and normalized_part_id == "final" and assistant_turn is not None:
        repair_metadata.update(assistant_turn.metadata())
    payload = {
        "thread_id": conversation.thread_id,
        "role": role,
        "content": content,
        "channel": gateway_request_channel(request) or thread_channel(store, conversation.thread_id),
        "channel_message_id": "",
        "metadata": repair_metadata,
    }
    # Keep the legacy final filename so an upgrade can consume already queued repairs.
    # Additional commentary parts receive their own non-colliding typed suffix.
    if role == "assistant" and normalized_part_id != "final":
        filename = f"{request_id}-{role}-{normalized_part_id.replace(':', '-')}.json"
    else:
        filename = f"{request_id}-{role}.json"
    path = Path(root) / "message_repairs" / filename
    try:
        from .io import write_json_file

        write_json_file(path, payload)
        indexed = getattr(agent, "_conversation_indexed_threads", set())
        indexed.discard(conversation.thread_id)
    except Exception as exc:
        logger.error("会话修复记录写入失败(%s): %s: %s", path, type(exc).__name__, exc)


# LLM: 补交只读原 repair 包，按 thread/request/part 幂等追加成功后才删除文件；不重做工具或重生成正文。
# 函数用途: 在加载历史前补交该线程曾写失败的消息，读取与追加失败沿原错误列表报告。
def repair_gateway_conversation_messages(
    store: object,
    thread_id: str,
    load_errors: list[dict],
) -> None:
    root = getattr(getattr(store, "storage", None), "root", None)
    repair_dir = Path(root) / "message_repairs" if root else None
    if repair_dir is None or not repair_dir.exists():
        return
    for path in sorted(repair_dir.glob("*.json"))[:100]:
        report = read_json_file_report(path, context="gateway.conversation.repair.read")
        if report.load_error is not None:
            load_errors.append(report.load_error)
            continue
        payload = report.payload
        if str(payload.get("thread_id") or "") != thread_id:
            continue
        try:
            # 与原 recent_report(limit=200) 同窗同错，只保留去重所需字段，不驻留正文；判定顺序与原实现相同。
            facts, errors = store.messages.recent_projection_report(thread_id, limit=200, project=_dedupe_facts)
            if errors:
                load_errors.extend(errors)
                continue
            request_id = str((payload.get("metadata") or {}).get("gateway_request_id") or "")
            role = str(payload.get("role") or "")
            metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
            part_id = str(metadata.get("assistant_part_id") or "final").strip() or "final"
            already_written = any(
                _gateway_message_matches_part(
                    fact,
                    role=role,
                    request_id=request_id,
                    channel_message_id=str(payload.get("channel_message_id") or ""),
                    assistant_part_id=part_id,
                )
                for fact in facts
            )
            if not already_written:
                store.messages.append(payload)
            path.unlink()
        except Exception as exc:
            load_errors.append(runtime_error_report(exc, context="gateway.conversation.repair.append"))


# LLM: Dedupe uses request plus a host-authored assistant part id. Legacy assistant rows without
# the additive field are treated as the final part; ordinary user message identity is unchanged.
# 函数用途: 判断一条已有消息是否与准备追加的用户消息或助手分段是同一条。
# LLM: 只取 _gateway_message_matches_part 会读的字段（role、channel_message_id、metadata 的请求号与部分编号），不带正文；
# 判定仍由原函数完成，新增判定字段时须同步这里。
# 类用途: 倒序流式扫描时每行保留的去重事实。
@dataclass(frozen=True)
class _DedupeFacts:
    role: object
    channel_message_id: object
    metadata: dict


# 函数用途: 把一行消息投影成去重事实，供倒序流式扫描逐条保留。
def _dedupe_facts(row: object) -> _DedupeFacts:
    metadata = getattr(row, "metadata", None)
    metadata = metadata if isinstance(metadata, dict) else {}
    return _DedupeFacts(
        getattr(row, "role", ""), getattr(row, "channel_message_id", ""),
        {key: metadata[key] for key in ("gateway_request_id", "assistant_part_id") if key in metadata},
    )


def _gateway_message_matches_part(
    row: object,
    *,
    role: str,
    request_id: str,
    channel_message_id: str,
    assistant_part_id: str,
) -> bool:
    if str(getattr(row, "role", "") or "") != role:
        return False
    metadata = getattr(row, "metadata", None)
    metadata = metadata if isinstance(metadata, dict) else {}
    same_request = str(metadata.get("gateway_request_id") or "") == request_id
    if role == "assistant":
        row_part = str(metadata.get("assistant_part_id") or "final").strip() or "final"
        return same_request and row_part == assistant_part_id
    return bool(
        same_request
        or (
            channel_message_id
            and str(getattr(row, "channel_message_id", "") or "") == channel_message_id
        )
    )


# LLM: 本地队列的 source 由 submit_gateway_ask 写入，metadata.channel 是 owner provider，二者不能混用。
# 只把两个精确本地来源映射为私有展示；HTTP/IM/未知来源保留其通道策略，不改变鉴权、owner 或投递路由。
# 函数用途: 决定前台文字的路径展示策略，让多用户本地 TUI 和默认本地 TUI 一样保留绝对路径。
def gateway_request_channel(request: object) -> str:
    if not isinstance(request, dict):
        return ""
    source = str(request.get("source") or "").strip().lower()
    if source == LOCAL_CHAT_SOURCE:
        return LOCAL_CHAT_CHANNEL
    if source == "cli_gateway":
        return "gateway-cli"
    metadata = request.get("metadata")
    declared = metadata.get("channel") if isinstance(metadata, dict) else None
    return str(declared or request.get("source") or request.get("channel") or "").strip().lower()


# LLM: 展示与落账共用请求的通道 resolver；缺请求事实时回到会话绑定，不能从模型内容或 rich 开关猜私有性。
# 函数用途: 取得本次交付/落账所用会话的通道身份。
def gateway_conversation_channel(context: object, conversation: object) -> str:
    request = getattr(context, "request", None)
    channel = gateway_request_channel(request)
    if channel:
        return channel
    return thread_channel(
        getattr(getattr(context, "agent", None), "conversation_store", None),
        getattr(conversation, "thread_id", ""),
    )


# LLM: 这是显式用户停止的通道控制投影，不是模型答复或任务完成；原生历史由独立提交入口保存。
# 函数用途: 将已停止回合标为不投递正文，供执行器与历史保存共同使用。
def silent_user_stop_delivery() -> dict[str, object]:
    """Return the channel projection for a non-message user interruption."""
    return {
        "content": "",
        "artifact_names": [],
        "internal_signal": True,
        "projection_status": "suppressed_user_stop",
    }
