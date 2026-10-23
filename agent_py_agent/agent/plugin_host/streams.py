# LLM: 流缓冲只消费已授权公开事件；MAC游标和入口授权由A提供，不授执行或取消权。
# 模块用途: 提供有界SSE传输、心跳和慢消费者关闭。
from __future__ import annotations

import re
import threading
from collections import deque
from datetime import datetime

from .errors import HostAPIError
from .schemas import canonical_json, closed, integer, text, validate_object

_STATES = ("queued", "admission_wait", "running", "waiting_approval", "waiting_input",
           "cancelling", "succeeded", "failed", "cancelled", "unknown")
_HEARTBEAT_SECONDS = 15.0
_MAX_FRAME_BYTES = 65536


# LLM: 这里只校验SSE编码安全，MAC真实性仍由A的游标服务核验。
# 函数用途: 拒绝游标换行注入及超长输入。
def _safe_cursor(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 2048 or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise HostAPIError("INVALID_SCHEMA")
    return value


# LLM: 只输出封闭公开形状；未知字段不能靠调用者声称已脱敏而穿透。
# 函数用途: 校验事件和时间戳并复制为独立字典。
def _public_event(event):
    schemas = _event_schemas()
    row = validate_object(event, closed({
        "event_id": text(128, 1), "sequence": integer(2**63 - 1, 1),
        "kind": {"enum": list(schemas)}, "created_at": text(64, 1),
        "data": {"type": "object"},
    }))
    row["data"] = validate_object(row["data"], schemas[row["kind"]])
    _utc_timestamp(row["created_at"])
    if row["kind"] == "terminal":
        _validate_terminal_data(row["data"])
    return row


# LLM: 每种事件只允许公开结构，禁止任意raw参数、tool结果和私卡内容。
# 函数用途: 构造各事件数据的封闭schema。
def _event_schemas():
    interaction = closed({"interaction_id": text(128, 1), "summary": text(1024)})
    return {
        "status": closed({"state": {"enum": list(_STATES)}}),
        "assistant_delta": closed({"message_id": text(128, 1), "part": {"enum": ["commentary", "final"]},
            "offset_bytes": integer(2**63 - 1), "text": text(65536)}),
        "tool_progress": closed({"call_id": text(128, 1), "tool_name": text(96, 1),
            "status": {"enum": ["started", "progress", "ended"]}, "summary": text(1024)}),
        "approval_required": interaction, "input_required": interaction,
        "terminal": closed({"terminal_id": text(128, 1), "status": {"enum": ["succeeded", "failed", "cancelled"]},
            "completed_at": text(64, 1), "result_message_ids": {"type": "array", "items": text(128, 1), "maxItems": 100},
            "error_code": {}, "usage": closed({"input_tokens": {}, "output_tokens": {}}), "resources_stopped": {}}),
    }


# LLM: nullable字段做严格类型检查，bool不能冒充token整数。
# 函数用途: 检查终态里可为空的安全字段。
def _validate_terminal_data(data):
    _utc_timestamp(data["completed_at"])
    code = data["error_code"]
    if code is not None and (type(code) is not str or re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", code) is None):
        raise HostAPIError("INVALID_SCHEMA")
    if data["resources_stopped"] is not None and type(data["resources_stopped"]) is not bool:
        raise HostAPIError("INVALID_SCHEMA")
    for value in data["usage"].values():
        if value is not None and (type(value) is not int or not 0 <= value <= 2**63 - 1):
            raise HostAPIError("INVALID_SCHEMA")


# LLM: 日期只能是可解析UTC，不按字符串看起来像时间就授信。
# 函数用途: 校验事件及终态的UTC时间戳。
def _utc_timestamp(value):
    try:
        stamp = datetime.fromisoformat(value)
        if stamp.utcoffset() is None or stamp.utcoffset().total_seconds() != 0:
            raise ValueError("UTC required")
    except ValueError as error:
        raise HostAPIError("INVALID_SCHEMA") from error


# LLM: 每条帧有明确字节上限，超限拒绝而不截断业务事件。
# 函数用途: 编码一条已白名单校验的SSE事件。
def _event_frame(event, cursor):
    frame = f"id: {cursor}\nevent: {event['kind']}\ndata: {canonical_json(event)}\n\n".encode()
    if len(frame) > _MAX_FRAME_BYTES:
        raise HostAPIError("PAYLOAD_TOO_LARGE", 413)
    return frame


# LLM: 只回安全码和最后已交付位置；慢消费者错误不指令取消任务。
# 函数用途: 生成关闭前的安全错误帧。
def _error_frame(code, cursor):
    data = {"code": code}
    if code == "BACKPRESSURE":
        data["event_cursor"] = cursor
        return ("retry: 1000\nevent: error\ndata: " + canonical_json(data) + "\n\n").encode()
    return ("event: error\ndata: " + canonical_json(data) + "\n\n").encode()


# LLM: 此传输叶子不读取业务账本，不运行模型；调用方独立泵轮询take，禁止在普通HTTP池阻塞。
# 类用途: 保存单条连接尚未交付的事件，容量和单帧大小同时受限。
class RequestEventBuffer:
    # LLM: 参数来自宿主解码游标，不接受公开after_seq；时间采用宿主单调时钟。
    # 函数用途: 建立有界缓冲和已交付位置。
    def __init__(self, *, capacity=32, last_sequence=0, last_cursor=None, now=0.0):
        if type(capacity) is not int or not 1 <= capacity <= 64 or type(last_sequence) is not int or last_sequence < 0:
            raise ValueError("invalid stream buffer bounds")
        self._capacity = capacity
        self._last_sequence = last_sequence
        self._last_cursor = _safe_cursor(last_cursor) if last_cursor is not None else None
        self._seen_sequence = last_sequence
        self._last_sent = now
        self._pending = deque()
        self._error = None
        self._closed = False
        self._lock = threading.RLock()

    # LLM: 编码前核公开字段；序列从宿主原日志产生，入缓冲不推进最后交付游标。
    # 函数用途: 去重并放入事件，超载时清正文且准备重连提示。
    def push(self, event, cursor):
        row = _public_event(event)
        cursor = _safe_cursor(cursor)
        frame = _event_frame(row, cursor)
        with self._lock:
            if self._closed or self._error or row["sequence"] <= self._seen_sequence:
                return False
            if len(self._pending) >= self._capacity:
                self._pending.clear()
                self._error = "BACKPRESSURE"
                return False
            self._pending.append((row["sequence"], cursor, frame))
            self._seen_sequence = row["sequence"]
            return True

    # LLM: 每帧含心跳均先复验；拒绝只输出安全错误，无traceback、业务正文或令牌。
    # 函数用途: 返回下一帧或15秒心跳，撤销后关闭连接。
    def take(self, *, now, check_current):
        with self._lock:
            if self._closed:
                return None
            try:
                check_current()
            except HostAPIError as error:
                self._pending.clear()
                self._closed = True
                return _error_frame(error.code, None)
            except Exception:
                self.close()
                raise
            if self._error:
                frame = _error_frame(self._error, self._last_cursor)
                self.close()
                return frame
            if self._pending:
                self._last_sequence, self._last_cursor, frame = self._pending.popleft()
                self._last_sent = now
                return frame
            if now - self._last_sent >= _HEARTBEAT_SECONDS:
                self._last_sent = now
                return b": heartbeat\n\n"
            return None

    # LLM: 清除待发正文，不向底层执行发出任何控制操作。
    # 函数用途: 关闭传输。
    def close(self):
        with self._lock:
            self._closed = True
            self._pending.clear()


# LLM: 生产只拉一条有界原消息页；MAC保存已交付位置，重新连接仍核A和B1范围，断连不取消请求。
# 函数用途: 从原请求/最终消息投影持续RequestEvent，交给A原SSE泵运输。
def request_events(conversation, authorized):
    import time
    from datetime import datetime, timezone

    from .conversations import read_context
    from .requests import RequestDomain, request_replies
    from .schemas import thaw, validate_request_event

    domain = RequestDomain(conversation)
    context = read_context(authorized)
    if domain.ports is None:
        raise HostAPIError("HOST_NOT_READY", 503)
    paths, store = domain.ports.paths_for(context), conversation.store_resolver(context)
    token = authorized.call.payload.get("event_cursor")
    filters = {"channel": "stream"}
    position = thaw(domain.codec.decode(authorized, token, filters).position) if token else {}
    while True:
        context = read_context(authorized)
        facts = domain.facts(authorized, context, paths, store)
        if position.get("terminal"):
            return
        rows, reply, more = request_replies(store, facts, position.get("reply", {}))
        sequence = position.get("sequence", 0) + 1
        stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        position = {**position, "reply": reply}
        if rows:
            row = rows[0]
            kind = "assistant_delta"
            data = {"message_id": row["message_id"], "part": "final", "offset_bytes": row["offset_bytes"], "text": row["content"]}
        elif facts["terminal"] is not None and not more:
            kind, data = "terminal", facts["terminal"]
            position["terminal"] = True
            stamp = data["completed_at"]
        elif position.get("state") != facts["state"]:
            kind, data = "status", {"state": facts["state"]}
            position["state"] = facts["state"]
        else:
            time.sleep(0.25)
            continue
        position["sequence"] = sequence
        event_id = domain.codec.encode(authorized, facts["request_id"], position, filters)
        read_context(authorized)
        yield validate_request_event({"event_id": event_id, "sequence": sequence,
                                      "kind": kind, "created_at": stamp, "data": data})
