# LLM: 本模块在已运行 Gateway 内执行薄客户端的 owner-scoped 记忆与历史操作；HTTP/TUI/IM 只能消费结构化结果，不能直接打开 Agent 存储。
# 模块用途: 为终端、飞书和未来 Web 提供统一的记忆查询、显式保存与会话历史读取服务。

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..conversation.models import is_audit_background_transcript_entry
from .control_service import GatewayControlScope, resolve_gateway_scope_agent


# LLM: Memory result 只投影用户可见记录字段和稳定错误码；底层路径、异常正文、候选审核内部状态不得出 Gateway。
# 类用途: 表示一次记忆查询或显式保存的结构化结果。
@dataclass(frozen=True)
class GatewayClientMemoryResult:
    operation: str
    ok: bool
    records: tuple[dict[str, object], ...] = ()
    error_code: str = ""

    # LLM: 序列化字段是 HTTP 与各前端共用合同，新增字段必须保持向后兼容。
    # 函数用途: 转换成可发送的 JSON 字典。
    def to_dict(self) -> dict[str, object]:
        return {
            "operation": self.operation,
            "ok": self.ok,
            "records": [dict(item) for item in self.records],
            "error_code": self.error_code,
        }


# LLM: History result 只包含完整 user/assistant 对和小型读取错误；chunk、内部注入、后台消息不能冒充恢复历史。
# 类用途: 表示一个 owner/channel/conversation 的可恢复聊天回合。
@dataclass(frozen=True)
class GatewayClientHistoryResult:
    ok: bool
    thread_id: str = ""
    turns: tuple[dict[str, str], ...] = ()
    load_errors: tuple[dict[str, object], ...] = ()

    # LLM: HTTP 投影保留 typed turns 和错误码，不输出服务端路径或异常原文。
    # 函数用途: 转换成可发送的 JSON 字典。
    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "thread_id": self.thread_id,
            "turns": [dict(item) for item in self.turns],
            "load_errors": [dict(item) for item in self.load_errors],
        }


# LLM: 操作选择只读显式 operation 枚举；自然语言 query/content 不得改变路由、权限或写入类型。
# 函数用途: 在已解析 owner 的长期记忆服务中查询最近记录、搜索或保存一条明确事实。
def execute_gateway_client_memory(
    base_agent: object,
    *,
    scope: GatewayControlScope,
    operation: str,
    query: str = "",
    content: str = "",
    kind: str = "fact",
    limit: int = 5,
) -> GatewayClientMemoryResult:
    normalized = str(operation or "").strip().lower()
    if normalized not in {"recent", "search", "remember"}:
        return GatewayClientMemoryResult(normalized, False, error_code="UNSUPPORTED_OPERATION")
    try:
        owner_agent = resolve_gateway_scope_agent(base_agent, scope)
        bounded_limit = _bounded_limit(limit)
        if normalized == "remember":
            record = owner_agent.remember(str(content or "").strip(), kind=str(kind or "fact"))
            records = (memory_record_payload(record),)
        elif normalized == "search":
            records = tuple(
                memory_record_payload(record)
                for record in owner_agent.recall(str(query or "").strip(), bounded_limit)
            )
        else:
            records = tuple(
                memory_record_payload(record)
                for record in list(owner_agent.memory.all())[-bounded_limit:]
            )
    except Exception as exc:  # noqa: BLE001 HTTP 合同只暴露稳定错误类别
        return GatewayClientMemoryResult(
            normalized,
            False,
            error_code=type(exc).__name__,
        )
    return GatewayClientMemoryResult(normalized, True, records=records)


# LLM: 历史读取只使用 authenticated scope 解析 owner/thread，并按稳定 request id 配对；不按正文或相邻行猜问答关系。
# 函数用途: 读取当前会话最近若干个完整问答回合供薄客户端恢复显示。
def read_gateway_client_history(
    base_agent: object,
    *,
    scope: GatewayControlScope,
    max_turns: int,
) -> GatewayClientHistoryResult:
    try:
        owner_agent = resolve_gateway_scope_agent(base_agent, scope)
        store = owner_agent.conversation_store
        thread, thread_error = store.resolve_thread_report(
            channel=scope.channel,
            channel_conversation_id=scope.conversation_id,
            channel_user_id=scope.user_id,
        )
    except Exception as exc:  # noqa: BLE001 读取失败必须成为显式 typed error
        return GatewayClientHistoryResult(False, load_errors=(_safe_load_error(exc),))
    if thread_error is not None:
        return GatewayClientHistoryResult(
            False,
            load_errors=(_safe_load_error(thread_error),),
        )
    if thread is None:
        return GatewayClientHistoryResult(True)
    thread_id = str(getattr(thread, "thread_id", "") or "")
    limit = _bounded_limit(max_turns, maximum=200)
    try:
        rows, row_errors = store.recent_messages_report(
            thread_id,
            limit=max(16, limit * 4),
        )
    except Exception as exc:  # noqa: BLE001 同上
        return GatewayClientHistoryResult(
            False,
            thread_id=thread_id,
            load_errors=(_safe_load_error(exc),),
        )
    safe_errors = tuple(_safe_load_error(item) for item in row_errors)
    return GatewayClientHistoryResult(
        not safe_errors,
        thread_id=thread_id,
        turns=_paired_history_turns(rows, max_turns=limit),
        load_errors=safe_errors,
    )


# LLM: 记忆投影只开放稳定 ID、角色、类型和正文；attributes/source 等内部审核字段保留在服务端。
# 函数用途: 把一条正式记忆转换成前端通用记录。
def memory_record_payload(record: Any) -> dict[str, object]:
    return {
        "entry_id": str(getattr(record, "entry_id", "") or ""),
        "kind": str(getattr(record, "kind", "") or ""),
        "role": str(getattr(record, "role", "") or ""),
        "content": str(getattr(record, "content", "") or ""),
    }


# LLM: 配对必须要求同一 request id 的 user 先于 assistant，后台审计投递和残缺轮一律排除。
# 函数用途: 将会话消息账本整理成按时间排列的完整问答回合。
def _paired_history_turns(
    rows: list[object],
    *,
    max_turns: int,
) -> tuple[dict[str, str], ...]:
    user_by_request: dict[str, str] = {}
    assistant_by_request: dict[str, str] = {}
    request_order: list[str] = []
    for row in rows:
        if is_audit_background_transcript_entry(row):
            continue
        metadata = getattr(row, "metadata", {})
        request_id = str(
            metadata.get("gateway_request_id") if isinstance(metadata, dict) else ""
        ).strip()
        role = str(getattr(row, "role", "") or "").strip().lower()
        content = str(getattr(row, "content", "") or "")
        if not request_id or not content:
            continue
        if role == "user":
            if request_id not in user_by_request:
                request_order.append(request_id)
            user_by_request[request_id] = content
        elif role == "assistant" and request_id in user_by_request:
            assistant_by_request[request_id] = content
    complete = tuple(
        {
            "request_id": request_id,
            "user_message": user_by_request[request_id],
            "assistant_message": assistant_by_request[request_id],
        }
        for request_id in request_order
        if request_id in assistant_by_request
    )
    return complete[-max(1, int(max_turns)):]


# LLM: 数量限制是资源边界而非内容判断；无效值回落默认且 0 不表示无限。
# 函数用途: 将前端请求的记录数限制在安全范围。
def _bounded_limit(value: object, *, maximum: int = 100) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 5
    return max(1, min(maximum, parsed))


# LLM: 客户端错误不得携带服务端路径或任意异常正文，只保留稳定错误类别。
# 函数用途: 把异常或存储错误报告压缩成安全的小型字典。
def _safe_load_error(value: object) -> dict[str, object]:
    if isinstance(value, BaseException):
        return {"error_code": "CLIENT_HISTORY_LOAD_FAILED", "error_type": type(value).__name__}
    if isinstance(value, dict):
        return {
            "error_code": str(value.get("error_code") or value.get("category") or "LOAD_FAILED"),
            "error_type": str(value.get("error_type") or ""),
        }
    return {"error_code": "CLIENT_HISTORY_LOAD_FAILED", "error_type": type(value).__name__}


__all__ = [
    "GatewayClientHistoryResult",
    "GatewayClientMemoryResult",
    "execute_gateway_client_memory",
    "read_gateway_client_history",
]
