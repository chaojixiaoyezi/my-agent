# LLM: 前后台共用完整历史行选择与作用域过滤，窗口必须同时服务正文和原生回放；只读原 ConversationStore，不依赖 Gateway 执行器。
# 模块用途: 从既有会话行生成有界历史投影，保留 metadata、完整消息和读取错误，不改写或补造历史。
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..runtime_errors import runtime_error_report
from .authority import (
    CONVERSATION_AUDIT_PREPARE_ATTR,
    CONVERSATION_WORK_KIND_ATTR,
    CONVERSATION_WORK_NAME_ATTR,
)
from .channels import project_user_reply
from .models import is_audit_background_transcript_entry
from .tool_context_window import conversation_message_with_terminal_tool_fold

if TYPE_CHECKING:
    from ...core import SimpleAgent

# LLM: 每条投影对应原持久记录；正文可脱敏，metadata 必须完整保留给 native_history 回放。
# 类用途: 保存进入下一轮模型上下文的一条历史消息，正文可投影但结构化回合信息不丢失。
@dataclass(frozen=True)
class ConversationHistoryRow:
    message_id: str
    role: str
    content: str
    metadata: dict[str, object] = field(default_factory=dict)


# LLM: 准备轮隔离只由 typed 工作种类、名称和显式布尔标记决定；普通用户文本不参与判定。
# 函数用途: 判断本次历史与输入是否需采用精确命名工作的准备范围。
def is_scoped_audit_prepare(work_scope: object) -> bool:
    scope = work_scope if isinstance(work_scope, dict) else {}
    return (
        scope.get(CONVERSATION_AUDIT_PREPARE_ATTR) is True
        and str(scope.get(CONVERSATION_WORK_KIND_ATTR) or "").strip().lower() == "audit"
        and bool(str(scope.get(CONVERSATION_WORK_NAME_ATTR) or "").strip())
    )


# LLM: 有确切 task_id 时按 ID 比较，缺失时只沿原结构化名称归属；不可把模型文字解释成范围授权。
# 函数用途: 为准备轮排除兄弟命名工作的历史，普通对话和本项工作仍保留。
def _history_row_visible_in_work_scope(
    metadata: object,
    work_scope: object,
) -> bool:
    """Hide only typed sibling Audit turns from an exact prepare projection."""

    if not is_scoped_audit_prepare(work_scope):
        return True
    row = metadata if isinstance(metadata, dict) else {}
    row_kind = str(row.get(CONVERSATION_WORK_KIND_ATTR) or "").strip().lower()
    if row_kind != "audit":
        return True
    current = work_scope if isinstance(work_scope, dict) else {}
    current_task_id = str(current.get("conversation_task_id") or "").strip()
    row_task_id = str(row.get("conversation_task_id") or "").strip()
    if current_task_id and row_task_id:
        return row_task_id == current_task_id
    current_name = str(current.get(CONVERSATION_WORK_NAME_ATTR) or "").strip()
    row_name = str(row.get(CONVERSATION_WORK_NAME_ATTR) or "").strip()
    return bool(current_name and row_name and row_name == current_name)


# LLM: 唯一的单行选择规则：只看结构化 role、请求身份、Audit 投递标记与工作范围，不读正文判断；
# Gateway/后台的具体投影与只读来源种子必须共用它，不能另抄一套选择算法。
# 函数用途: 判断一条原始会话记录是否进入本轮模型历史。
def history_row_selected(row: object, *, current_request_id: str, work_scope: dict[str, object] | None) -> bool:
    role = str(getattr(row, "role", "") or "").strip().lower()
    metadata = getattr(row, "metadata", None)
    metadata = metadata if isinstance(metadata, dict) else {}
    return not (
        role not in {"user", "assistant"}
        or metadata.get("gateway_request_id") == current_request_id
        or is_audit_background_transcript_entry(row)
        or not _history_row_visible_in_work_scope(metadata, work_scope)
    )


# LLM: 唯一的单行正文投影：assistant 先做用户回复投影再追加终态工具折叠，metadata 完整复制给原生回放；
# 调用方须先用 history_row_selected 过滤，本函数不再判断范围。current_epoch 决定折叠取热尾还是冷折叠：
# 具体投影传 None（按当前时刻），只读来源传冻结时刻，保证延后解析与准备时一致。
# 函数用途: 把一条已选中的原始记录变成模型历史行，供具体投影和来源种子重放共用。
def project_history_row(row: object, *, current_epoch: float | None = None) -> ConversationHistoryRow:
    role = str(getattr(row, "role", "") or "").strip().lower()
    metadata = getattr(row, "metadata", None)
    metadata = metadata if isinstance(metadata, dict) else {}
    content = str(getattr(row, "content", "") or "")
    if role == "assistant":
        content = project_user_reply(content).content
        content = conversation_message_with_terminal_tool_fold(content, metadata, current_epoch=current_epoch)
    return ConversationHistoryRow(
        message_id=str(getattr(row, "message_id", "") or ""),
        role=role,
        content=content,
        metadata=dict(metadata),
    )


# LLM: 前后台共用同一行选择；preserve_complete仅用于已裁决Compact来源，必须完整进入容量门，普通读取保留展示窗口。
# 函数用途: 接受显式只读Sequence并保持原选择顺序；已裁决来源不误回Store或再次窗口化，宿主物化仍沿原入口。
def conversation_history_rows(
    agent: SimpleAgent,
    thread_id: str,
    current_request_id: str,
    load_errors: list[dict],
    *,
    rows: object = None,
    token_budget: int = 0,
    work_scope: dict[str, object] | None = None,
    preserve_complete: bool = False,
) -> tuple[ConversationHistoryRow, ...]:
    store = getattr(agent, "conversation_store", None)
    config = getattr(agent, "config", None)
    max_turns = max(
        1,
        int(getattr(config, "conversation_history_max_turns", 20) or 20),
    )
    total_chars = max(
        1000,
        int(getattr(config, "conversation_history_max_chars", 48_000) or 48_000),
    )
    supplied_rows = isinstance(rows, Sequence) and not isinstance(rows, (str, bytes, bytearray))
    if preserve_complete and not supplied_rows:
        raise ValueError("complete history projection requires explicit source rows")
    if supplied_rows:
        message_rows = list(rows)
    else:
        try:
            message_rows, errors = store.messages.recent_report(
                thread_id,
                limit=max_turns * 2 + 8,
            )
        except Exception as exc:
            load_errors.append(runtime_error_report(exc, context="gateway.conversation.messages"))
            return ()
        load_errors.extend(error for error in errors if isinstance(error, dict))
    candidates = [
        project_history_row(row)
        for row in message_rows
        if history_row_selected(row, current_request_id=current_request_id, work_scope=work_scope)
    ]
    if preserve_complete:
        return tuple(candidates)
    history_chars = total_chars
    history_messages = max_turns * 2
    if supplied_rows:
        history_chars = max(history_chars, max(0, int(token_budget)) * 3)
        history_messages = max(history_messages, len(candidates))
    return _latest_conversation_rows(
        candidates,
        max_messages=history_messages,
        max_chars=history_chars,
    )


# LLM: 字符预算只能淘汰完整旧行，不能截断单行或删除 metadata；原生回放与正文必须使用同一尾部。
# 函数用途: 从尾部选择完整历史行，给正文和原生工具消息共用同一个窗口。
def _latest_conversation_rows(
    candidates: list[ConversationHistoryRow],
    *,
    max_messages: int,
    max_chars: int,
) -> tuple[ConversationHistoryRow, ...]:
    selected: list[ConversationHistoryRow] = []
    used = 0
    for row in reversed(candidates[-max_messages:]):
        if selected and used + len(row.content) > max_chars:
            break
        selected.append(row)
        used += len(row.content)
    selected.reverse()
    return tuple(selected)
