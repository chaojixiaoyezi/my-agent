# LLM: session_search 的会话原文读取部分，只读 owner ConversationStore 的 canonical 消息文件（唯一权威），不读派生索引。
#   会话身份：thread_id 参数只在读取单条消息时可显式给出，它是不可信的模型输入：先按 opaque ID 拒绝式校验（不含路径
#   分隔、点段或绝对路径），再必须是本 owner 会话库里已存在的会话，否则拒绝，绝不拼出指向其它 owner 的路径；
#   其余一律取宿主运行上下文里的可信会话，并在结果里用 scope_resolution 说明来源。长消息按字符游标分段返回，任何一次调用的输出都有上限。
#   返回提示提供显式mode的规范JSON并保留读取线程和游标；新增字段须同步session_search_tool及相关历史回归。
# 模块用途: 让模型在上下文被压缩后，按消息编号分段读回原话，或按页浏览当前会话的全部用户与助手消息。
from __future__ import annotations

import json
from typing import Any

from ..common.opaque_id import OpaqueIdError, validate_path_segment
from ..conversation.channels import project_user_reply
from ..conversation.display_checkpoint import is_display_checkpoint
from ..conversation.models import MessageLogEntry, is_audit_background_transcript_entry

# 读取历史消息时默认返回的字符数。
READ_DEFAULT_CHARS = 6_000
# 读取历史消息的最小字符数，避免一次返回过少。
READ_MIN_CHARS = 200
# 读取历史消息的最大字符数，防止一次拉爆上下文。
READ_MAX_CHARS = 20_000
# 浏览历史时单条记录的预览字符数。
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


# LLM: 读取单条消息时允许显式 thread_id（来自检索结果），但必须通过 _owned_thread 校验落在本 owner 会话库里；
#   否则只用可信当前会话。
# 函数用途: 决定本次读取落在哪个会话，并给出 scope_resolution 结构化说明。
def read_thread_scope(agent: object, params: dict[str, object]) -> tuple[str, dict[str, object]]:
    current, source = trusted_thread(agent)
    explicit = str(params.get("thread_id") or "").strip()
    if explicit:
        return _owned_thread(agent, explicit), {"thread_source": "explicit_parameter", "current_thread_id": current,
                                                "same_as_current": explicit == current}
    return require_current_thread(current, source), {"thread_source": source, "current_thread_id": current,
                                                     "same_as_current": True}


# LLM: 模型给的会话编号先做拒绝式路径段校验（绝对路径、../ 跳转、斜杠一律拒绝），再按结构化身份在本 owner 的
#   线程登记里查存在性；两步都过才返回，失败不回退到当前会话或全库。只读，不创建线程。
# 函数用途: 确认模型给出的会话编号只指向本人会话库里的一个真实会话。
def _owned_thread(agent: object, thread_id: str) -> str:
    try:
        validate_path_segment(thread_id, kind="thread_id")
    except OpaqueIdError:
        raise SessionHistoryRefusal("TOOL_INVALID_ARGUMENTS", "thread_id 不是有效的会话编号",
                                    "使用检索结果 scope 里的 thread_id，或不传 thread_id 读当前会话。") from None
    thread, load_error = _conversation_store(agent).threads.load_report(thread_id)
    if load_error:
        raise SessionHistoryRefusal("TOOL_EXECUTION_FAILED", "会话记录读取失败", "可原样重试一次。")
    if thread is None:
        raise SessionHistoryRefusal("TOOL_INVALID_ARGUMENTS", "本人会话库里没有这个会话",
                                    "使用检索结果 scope 里的 thread_id，或不传 thread_id 读当前会话。")
    return thread_id


# LLM: 空会话编号一律拒绝，不退化为全库或其它会话；source 只用于说明来源，不参与判定。
# 函数用途: 没有可信会话时明确拒绝，提示改用检索结果里的会话编号。
def require_current_thread(thread_id: str, source: str) -> str:
    if not thread_id:
        raise SessionHistoryRefusal("TOOL_INVALID_ARGUMENTS", f"当前运行没有可信会话（{source}）",
                                    "传入检索结果 scope 里的 thread_id，或先用 query 检索。")
    return thread_id


# LLM: owner/thread裁决先于本入口；只经原MessageStore字节索引读canonical，错误或跨thread记录一律拒绝。
# 函数用途: 按消息首地址读取原话，防止坏读或错误线程记录变成合法消息。
def _message_entry(agent: object, thread_id: str, message_id: str) -> MessageLogEntry | None:
    store = _conversation_store(agent)
    entry, errors = store.messages.by_id_report(thread_id, message_id)
    if errors or (entry is not None and entry.thread_id != thread_id):
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


# LLM: 先裁决owner/thread再读canonical；字符分页提示带mode、message_id和原显式thread_id，缺消息只给合法本会话浏览入口。
# 函数用途: 从已授权会话的单条原话按字符分页，不因底层seek优化改变分页口径。
def read_conversation_message(agent: object, message_id: str, params: dict[str, object]) -> dict[str, Any]:
    thread_id, resolution = read_thread_scope(agent, params)
    entry = _message_entry(agent, thread_id, message_id)
    if entry is None:
        return {"mode": "read", "found": False, "thread_id": thread_id, "message_id": message_id,
                "scope_resolution": resolution,
                "hint": '该会话里没有这条消息；可用 session_search {"mode":"thread_browse"} 浏览当前会话拿到正确的 message_id。'}
    text = message_text(entry)
    offset = _bounded_int(params.get("offset"), 0, 0, len(text))
    size = _bounded_int(params.get("max_chars"), READ_DEFAULT_CHARS, READ_MIN_CHARS, READ_MAX_CHARS)
    chunk = text[offset:offset + size]
    end = offset + len(chunk)
    next_params = {"mode": "message", "message_id": entry.message_id, "offset": end}
    if params.get("thread_id"):
        next_params["thread_id"] = thread_id
    return {
        "mode": "read", "found": True, "thread_id": thread_id, "message_id": entry.message_id,
        "role": entry.role, "when": round(float(entry.created_at or 0.0), 3), "total_chars": len(text),
        "offset": offset, "content": chunk, "next_offset": end if end < len(text) else None,
        "complete": end >= len(text), "scope_resolution": resolution,
        "hint": "已读到结尾。" if end >= len(text) else "未读完：用 session_search " + json.dumps(next_params, ensure_ascii=False) + " 继续读取下一段。",
    }


# LLM: 按 canonical 文件倒序分页；cursor 必须是上一页返回的 older_cursor（完整行边界），否则按无效游标拒绝。
#   只返回编号和有界预览；下一跳JSON明确message/thread_browse模式及真实游标，不写任何状态。
# 函数用途: 按规范thread_browse模式浏览当前会话，给出可直接执行的原文及翻页调用。
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
    hint = "按时间正序列出本页消息。" if page.before else "已到会话开头。"
    if rows:
        hint += "读取原文用 session_search " + json.dumps({"mode": "message", "message_id": rows[0].message_id}, ensure_ascii=False) + "。"
    if page.before:
        hint += "翻到更早一页用 session_search " + json.dumps({"mode": "thread_browse", "cursor": page.before}, ensure_ascii=False) + "。"
    elif not rows:
        hint += '当前无可见消息；可用 session_search {"mode":"thread_browse"} 重新浏览。'
    return {
        "mode": "thread_browse", "thread_id": thread_id, "messages": [_browse_item(row) for row in rows],
        "count": len(rows), "older_cursor": page.before or None,
        "scope_resolution": {"thread_source": source, "current_thread_id": thread_id},
        "hint": hint,
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
