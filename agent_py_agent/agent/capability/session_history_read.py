# LLM: session_search 的会话原文读取部分，只读 owner ConversationStore 的 canonical 消息文件（唯一权威），不读派生索引。
#   会话身份：thread_id 参数只在读取单条消息时可显式给出（同一 owner 的会话库内）；其余一律取宿主运行上下文里的可信会话，
#   并在结果里用 scope_resolution 说明来源。长消息按字符游标分段返回，任何一次调用的输出都有上限。
#   新增字段或模式须同步 session_search_tool 的 schema、说明与 test_session_history_read.py。
# 模块用途: 让模型在上下文被压缩后，按消息编号分段读回原话，或按页浏览当前会话的全部用户与助手消息。
from __future__ import annotations

from typing import Any

from ..conversation.channels import project_user_reply
from ..conversation.display_checkpoint import is_display_checkpoint
from ..conversation.models import MessageLogEntry, is_audit_background_transcript_entry

READ_DEFAULT_CHARS = 6_000
READ_MIN_CHARS = 200
READ_MAX_CHARS = 20_000
_BROWSE_PREVIEW_CHARS = 160


# LLM: 只携带结构化错误码和面向模型的提示，调用方把它转成失败的工具结果；不用异常文字做判定。
# 类用途: 读取会话原文时的可预期失败（没有可信会话、会话库不可用、读取出错、游标无效）。
class SessionHistoryRefusal(Exception):
    # LLM: 只保存结构化字段；message 同时作为异常文字，调用方不得解析它来分支。
    # 函数用途: 记录错误码、说明与下一步提示。
    def __init__(self, code: str, message: str, hint: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint


# LLM: 与审计工具同一个可信会话裁决函数，只读宿主运行上下文，不接受模型参数；没有可信会话时返回空编号。
# 函数用途: 取得当前运行所属会话的编号及其来源标签。
def trusted_thread(agent: object) -> tuple[str, str]:
    from ..tooling.user_config_tool import _decision_thread_scope

    return _decision_thread_scope(agent)


# LLM: 读取单条消息时允许显式 thread_id（来自检索结果，只能落在同一 owner 的会话库里）；否则只用可信当前会话。
# 函数用途: 决定本次读取落在哪个会话，并给出 scope_resolution 结构化说明。
def read_thread_scope(agent: object, params: dict[str, object]) -> tuple[str, dict[str, object]]:
    current, source = trusted_thread(agent)
    explicit = str(params.get("thread_id") or "").strip()
    if explicit:
        return explicit, {"thread_source": "explicit_parameter", "current_thread_id": current,
                          "same_as_current": explicit == current}
    return require_current_thread(current, source), {"thread_source": source, "current_thread_id": current,
                                                     "same_as_current": True}


# LLM: 空会话编号一律拒绝，不退化为全库或其它会话；source 只用于说明来源，不参与判定。
# 函数用途: 没有可信会话时明确拒绝，提示改用检索结果里的会话编号。
def require_current_thread(thread_id: str, source: str) -> str:
    if not thread_id:
        raise SessionHistoryRefusal("TOOL_INVALID_ARGUMENTS", f"当前运行没有可信会话（{source}）",
                                    "传入检索结果 scope 里的 thread_id，或先用 query 检索。")
    return thread_id


# LLM: 只读 canonical 消息文件；读取错误以结构化失败返回，不把坏读当成“没有这条消息”。
# 函数用途: 按消息编号取出一条原始会话消息。
def _message_entry(agent: object, thread_id: str, message_id: str) -> MessageLogEntry | None:
    store = _conversation_store(agent)
    entry, errors = store.messages.by_id_report(thread_id, message_id)
    if errors:
        raise SessionHistoryRefusal("TOOL_EXECUTION_FAILED", "会话记录读取失败", "可原样重试一次。")
    return entry


# LLM: 会话库挂在 agent 上且已按 owner 隔离；这里只做存在性检查，不创建目录或会话。
# 函数用途: 取当前 owner 的会话库；运行环境没有会话库时明确拒绝。
def _conversation_store(agent: object) -> Any:
    store = getattr(agent, "conversation_store", None)
    if store is None or getattr(store, "messages", None) is None:
        raise SessionHistoryRefusal("TOOL_UNAVAILABLE", "会话存储不可用", "当前运行环境没有会话记录。")
    return store


# LLM: 用户消息原样返回；助手消息与历史索引、压缩锚点一样取给用户的回复正文，不含内部过程标记。
# 函数用途: 得到一条消息面向模型的原文。
def message_text(entry: MessageLogEntry) -> str:
    if entry.role == "assistant":
        return project_user_reply(entry.content).content
    return str(entry.content or "")


# LLM: 与模型可见历史同一口径：只收用户消息和助手最终答复，排除显示记录、后台审计投递和助手过程片段。
# 函数用途: 判断一条记录是否属于可浏览的会话消息。
def visible_conversation_row(row: MessageLogEntry) -> bool:
    if row.role not in {"user", "assistant"} or is_display_checkpoint(row):
        return False
    metadata = row.metadata if isinstance(row.metadata, dict) else {}
    if row.role == "assistant" and str(metadata.get("assistant_part_id") or "").strip() not in {"", "final"}:
        return False
    return not is_audit_background_transcript_entry(row) and bool(message_text(row).strip())


# LLM: 纯函数；只接受能转成整数的值，布尔以外的非法输入按默认值处理，结果总在 [low, high] 内。
# 函数用途: 把一个可选整数参数夹到给定范围，缺失或非法时用默认值。
def _bounded_int(value: object, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(low, min(parsed, high))


# LLM: 只读；offset 超过全文长度时返回空片段并标记读完。next_offset 为空表示已到结尾，模型据此停止续读。
# 函数用途: 分段读取一条会话消息的原文（message_id 模式）。
def read_conversation_message(agent: object, message_id: str, params: dict[str, object]) -> dict[str, Any]:
    thread_id, resolution = read_thread_scope(agent, params)
    entry = _message_entry(agent, thread_id, message_id)
    if entry is None:
        return {"mode": "read", "found": False, "thread_id": thread_id, "message_id": message_id,
                "scope_resolution": resolution,
                "hint": "该会话里没有这条消息；可用 current_thread=true 浏览或检索拿到正确的 message_id。"}
    text = message_text(entry)
    offset = _bounded_int(params.get("offset"), 0, 0, len(text))
    size = _bounded_int(params.get("max_chars"), READ_DEFAULT_CHARS, READ_MIN_CHARS, READ_MAX_CHARS)
    chunk = text[offset:offset + size]
    end = offset + len(chunk)
    return {
        "mode": "read", "found": True, "thread_id": thread_id, "message_id": entry.message_id,
        "role": entry.role, "when": round(float(entry.created_at or 0.0), 3), "total_chars": len(text),
        "offset": offset, "content": chunk, "next_offset": end if end < len(text) else None,
        "complete": end >= len(text), "scope_resolution": resolution,
        "hint": "已读到结尾。" if end >= len(text) else f"未读完：用 offset={end} 继续读取下一段。",
    }


# LLM: 按 canonical 文件倒序分页；cursor 必须是上一页返回的 older_cursor（完整行边界），否则按无效游标拒绝。
#   只返回编号、角色、时间、长度和短预览，读全文走 message_id 模式；不写任何状态。
# 函数用途: 按页浏览当前会话的用户与助手消息（current_thread 且不带 query 的模式）。
def browse_current_thread(agent: object, params: dict[str, object], limit: int) -> dict[str, Any]:
    thread_id, source = trusted_thread(agent)
    require_current_thread(thread_id, source)
    cursor = params.get("cursor")
    if cursor is not None and (type(cursor) is not int or cursor < 0):
        raise SessionHistoryRefusal("TOOL_INVALID_ARGUMENTS", "cursor 必须是上一页返回的 older_cursor 整数",
                                    "不传 cursor 从最新一页开始浏览。")
    page = _conversation_store(agent).messages.history_page_report(thread_id, before=cursor, limit=limit)
    if page.errors:
        raise SessionHistoryRefusal("TOOL_INVALID_ARGUMENTS", "无法按这个游标读取会话记录",
                                    "不传 cursor 从最新一页开始，或使用上一页返回的 older_cursor。")
    rows = [row for row in page.rows if visible_conversation_row(row)]
    return {
        "mode": "thread_browse", "thread_id": thread_id, "messages": [_browse_item(row) for row in rows],
        "count": len(rows), "older_cursor": page.before or None,
        "scope_resolution": {"thread_source": source, "current_thread_id": thread_id},
        "hint": ("按时间正序列出本页消息；用 message_id 读取全文，用 older_cursor 翻到更早一页。"
                 if page.before else "已到会话开头；用 message_id 读取任意一条的全文。"),
    }


# LLM: 预览有固定上限，全文只能走 message_id 分段读取；chars 是面向模型的原文长度。
# 函数用途: 把一条会话消息整形成浏览条目（编号、角色、时间、长度、短预览）。
def _browse_item(row: MessageLogEntry) -> dict[str, Any]:
    text = message_text(row)
    return {"message_id": row.message_id, "role": row.role, "when": round(float(row.created_at or 0.0), 3),
            "chars": len(text), "preview": text[:_BROWSE_PREVIEW_CHARS]}


__all__ = [
    "READ_DEFAULT_CHARS",
    "READ_MAX_CHARS",
    "READ_MIN_CHARS",
    "SessionHistoryRefusal",
    "browse_current_thread",
    "message_text",
    "read_conversation_message",
    "read_thread_scope",
    "require_current_thread",
    "trusted_thread",
    "visible_conversation_row",
]
