# LLM: 流缓冲只消费已授权公开事件；MAC游标和入口授权由A提供，不授执行或取消权。
# 模块用途: 提供有界SSE传输、心跳和慢消费者关闭。
from __future__ import annotations

import hashlib
import json
import math
import threading
from collections import deque

from .errors import HostAPIError
from .schemas import canonical_json, validate_request_event

# SSE 流式心跳间隔 15 秒：无新事件时定期发送注释帧保持连接活跃。
_HEARTBEAT_SECONDS = 15.0
# 单条 SSE 事件帧最大字节数 64 KiB：防止超大事件占用流缓冲。
_MAX_FRAME_BYTES = 65536


# LLM: 这里只校验SSE编码安全，MAC真实性仍由A的游标服务核验。
# 函数用途: 拒绝游标换行注入及超长输入。
def _safe_cursor(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 2048 or any(ord(c) < 33 or ord(c) > 126 for c in value):
        raise HostAPIError("INVALID_SCHEMA")
    return value


# LLM: HTTP泵与缓冲消费同一schema，新增可选计量/耗时不能在第二份合同中丢失；不放行任意原始工具字段。
# 函数用途: 使用宿主唯一公开事件校验器，返回独立JSON副本。
def _public_event(event):
    return validate_request_event(event)


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


# LLM: 若当前线程绑定了流订阅停止事件则可中断等待，否则普通睡眠0.25秒。
# 函数用途: 空闲轮询等待并在订阅关闭时立即唤醒。
def _wait_stream_idle() -> None:
    import time
    stop = getattr(threading.current_thread(), "_plugin_stream_stop", None)
    if stop is not None:
        stop.wait(0.25)
    else:
        time.sleep(0.25)


# LLM: 检查当前流生产线程是否已收到订阅关闭信号。
# 函数用途: 判断SSE流订阅是否已停止。
def _stream_stopped() -> bool:
    stop = getattr(threading.current_thread(), "_plugin_stream_stop", None)
    return bool(stop is not None and stop.is_set())


# LLM: 先消费同请求原chunk再读最终消息，MAC保存字节前缀/分片位置；逐次核A/B1，断连不取消请求。
# 函数用途: 将真实过程、最终消息和规范终态有序交给原SSE泵。
def request_events(conversation, authorized):
    from .conversations import read_context
    from .requests import RequestDomain
    from .schemas import thaw, validate_request_event

    domain = RequestDomain(conversation)
    context = read_context(authorized)
    if domain.ports is None:
        raise HostAPIError("HOST_NOT_READY", 503)
    paths, store = domain.ports.paths_for(context), conversation.store_resolver(context)
    token = authorized.call.payload.get("event_cursor")
    filters = {"channel": "stream"}
    position = thaw(domain.codec.decode(authorized, token, filters).position) if token else {}
    while not _stream_stopped():
        context = read_context(authorized)
        facts = domain.facts(authorized, context, paths, store)
        if position.get("terminal"):
            return
        event = _next_request_event(paths, store, facts, position)
        if event is None:
            _wait_stream_idle()
            continue
        kind, data, stamp = event
        sequence = position.get("sequence", 0) + 1
        position["sequence"] = sequence
        event_id = domain.codec.encode(authorized, facts["request_id"], position, filters)
        read_context(authorized)
        yield validate_request_event({"event_id": event_id, "sequence": sequence,
                                      "kind": kind, "created_at": stamp, "data": data})


# LLM: 顺序固定为原过程→最终消息→规范终态；空闲不推进sequence，续读位置只在原MAC内保存。
# 函数用途: 选择下一条公开事件并推进其准确字节位置，不用多层分支拼状态。
def _next_request_event(paths, store, facts, position):
    from datetime import datetime, timezone

    from .requests import request_replies

    progress, saved, pending = request_progress(paths, facts, position.get("progress", {}))
    position["progress"] = saved
    if progress or pending:
        return progress
    stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    rows, reply, more = request_replies(store, facts, position.get("reply", {}))
    if rows:
        position["reply"] = reply
        row = rows[0]
        return "assistant_delta", {"message_id": row["message_id"], "part": "final",
            "offset_bytes": row["offset_bytes"], "text": row["content"]}, stamp
    if facts["terminal"] is not None and not more:
        position["terminal"] = True
        return "terminal", facts["terminal"], facts["terminal"]["completed_at"]
    if position.get("state") != facts["state"]:
        position["state"] = facts["state"]
        return "status", {"state": facts["state"]}, stamp
    return None


# LLM: 原producer无模型call_id时，仅按本请求round/index生成稳定展示ID；不赋予审批或执行能力。
# 函数用途: 给原生工具卡配对展示身份，不依赖文案或日志字节位置。
def _progress_tool_id(progress, request_id):
    explicit = progress.get("call_id", progress.get("tool_call_id"))
    if explicit is not None:
        return explicit
    parts = (progress.get("round"), progress.get("call_index"))
    if not all(type(value) is int and 0 <= value < 2 ** 63 for value in parts):
        raise HostAPIError("STORE_UNAVAILABLE", 503)
    return "tool-" + hashlib.sha256(f"{request_id}:{parts[0]}:{parts[1]}".encode()).hexdigest()[:32]


# LLM: 只消费原writer已净化的typed公开事件；原phase优先于中文status，不暴露raw参数/输出/旧文本。
# 函数用途: 将原chunk的公开说明或工具状态裁为Host事件。
def _progress_event(row, request_id, offset):
    from datetime import datetime, timezone

    from ..gateway_parts.stream_events import public_model_text

    kind = row.get("kind")
    stamp = row.get("t")
    stamp = stamp if type(stamp) in (int, float) and math.isfinite(stamp) else None
    created = datetime.fromtimestamp(stamp, timezone.utc) if stamp is not None else datetime.now(timezone.utc)
    created = created.isoformat().replace("+00:00", "Z")
    if kind in {"assistant_commentary", "thinking_delta"}:
        text = public_model_text(row.get("text"), max_chars=0, delivery_channel="plugin", identifiers=())
        if text:
            return "assistant_delta", {"message_id": request_id + ":p" + str(offset), "part": "commentary",
                                       "offset_bytes": 0, "text": text}, created
    if kind == "tool_progress":
        progress = row.get("progress", row.get("data", {}))
        if type(progress) is not dict:
            raise HostAPIError("STORE_UNAVAILABLE", 503)
        states = {"started": "started", "progress": "progress", "ended": "ended",
                  "finished": "ended", "completed": "ended", "deferred": "ended", "interrupted": "ended"}
        status = states.get(progress.get("phase")) or states.get(progress.get("status"))
        if status is None:
            return None
        summary = public_model_text(progress.get("summary", progress.get("status", "")), max_chars=1024,
                                    delivery_channel="plugin", identifiers=())
        elapsed = progress.get("elapsed_ms")
        if elapsed is None and type(progress.get("elapsed_seconds")) in (int, float):
            seconds = progress["elapsed_seconds"]
            elapsed = round(seconds * 1000) if math.isfinite(seconds) and seconds >= 0 else None
        return "tool_progress", {"call_id": _progress_tool_id(progress, request_id),
            "tool_name": progress.get("tool_name", progress.get("tool")),
            "status": status, "elapsed_ms": elapsed, "summary": summary}, created
    return None


# LLM: chunk只是展示原账；完整旧前缀常量内存校hash，每次读1MiB窗口，分片还绑定当前完整行；终态坏尾显式失败。
# 函数用途: 从原流取下一条有界事件，支持超过4MiB的长请求，不建立第二事件账。
def request_progress(paths, facts, position):
    base, content_offset = position.get("offset", 0), position.get("content_offset", 0)
    if type(content_offset) is not int or content_offset < 0:
        raise HostAPIError("CURSOR_EXPIRED", 410)
    raw, prefix, eof = _progress_window(paths, facts["request_id"], position)
    if raw is None:
        if (base or content_offset) and facts.get("terminal") is not None:
            raise HostAPIError("CURSOR_EXPIRED", 410)
        return None, position, bool(base or content_offset)
    offset, line_hash = 0, None
    for _ in range(64):
        end = raw.find(b"\n", offset)
        if end < 0:
            break
        line_hash = hashlib.sha256(raw[offset:end]).hexdigest()
        if content_offset and position.get("line_sha256") != line_hash:
            raise HostAPIError("CURSOR_EXPIRED", 410)
        event = _parse_progress(raw[offset:end], facts["request_id"], base + offset)
        next_offset = end + 1
        if event and event[0] == "assistant_delta":
            encoded = event[1]["text"].encode("utf-8")
            if content_offset >= len(encoded):
                raise HostAPIError("CURSOR_EXPIRED", 410)
            piece = encoded[content_offset:content_offset + 8192].decode("utf-8", errors="ignore")
            event[1].update(text=piece, offset_bytes=content_offset)
            content_offset += len(piece.encode("utf-8"))
            if content_offset < len(encoded):
                next_offset = offset
            else:
                content_offset = 0
        offset = next_offset
        saved = _progress_position(prefix, raw, base, offset, content_offset, line_hash)
        if event:
            return event, saved, not eof or offset < len(raw)
    if content_offset:
        raise HostAPIError("CURSOR_EXPIRED", 410)
    if offset == 0 and not eof:
        raise HostAPIError("PAYLOAD_TOO_LARGE", 413)
    if eof and offset < len(raw) and facts.get("terminal") is not None:
        raise HostAPIError("STORE_UNAVAILABLE", 503)
    saved = _progress_position(prefix, raw, base, offset, 0, None)
    return None, saved, not eof or offset < len(raw)


# LLM: 只从已核请求原队列取FD，不跟链接；前缀用64KiB块校验，窗口固定1MiB，不把整个日志载入内存。
# 函数用途: 按准确旧字节位置读取长日志窗口，并拒绝已消费前缀被改写或截断。
def _progress_window(paths, request_id, position):
    import os

    from ..common.nofollow_fs import open_readonly_file_beneath

    offset = position.get("offset", 0)
    if type(offset) is not int or offset < 0:
        raise HostAPIError("CURSOR_EXPIRED", 410)
    for folder in ("processing", "done", "failed"):
        try:
            descriptor = open_readonly_file_beneath(paths.root, ("requests", folder, request_id + ".chunks.jsonl"))
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            raise HostAPIError("STORE_UNAVAILABLE", 503) from None
        with os.fdopen(descriptor, "rb") as reader:
            before = os.fstat(reader.fileno())
            if offset > before.st_size:
                raise HostAPIError("CURSOR_EXPIRED", 410)
            prefix, remaining = hashlib.sha256(), offset
            while remaining:
                block = reader.read(min(65536, remaining))
                if not block:
                    raise HostAPIError("CURSOR_EXPIRED", 410)
                prefix.update(block)
                remaining -= len(block)
            if position.get("sha256", hashlib.sha256(b"").hexdigest()) != prefix.hexdigest():
                raise HostAPIError("CURSOR_EXPIRED", 410)
            raw = reader.read(min(1024 * 1024, before.st_size - offset))
            after = os.fstat(reader.fileno())
            if after.st_nlink != 1 or after.st_size < before.st_size:
                raise HostAPIError("CURSOR_EXPIRED", 410)
            return raw, prefix, offset + len(raw) == before.st_size
    return None, None, True


# LLM: 任意坏JSON或深层递归都映射成安全存储错误；原始字段不能进入异常消息。
# 函数用途: 解码一条原日志并投影公开事件。
def _parse_progress(raw, request_id, offset):
    try:
        row = json.loads(raw, object_pairs_hook=_unique_progress_fields)
        if type(row) is not dict:
            raise ValueError("invalid event")
        return _progress_event(row, request_id, offset)
    except (ValueError, TypeError, OverflowError, OSError, RecursionError):
        raise HostAPIError("STORE_UNAVAILABLE", 503) from None


# LLM: 旧前缀与分片中整行分别签入原MAC；digest对象仅是本次局部计算，不另建运行缓存或持久状态。
# 函数用途: 生成完整消费或当前分片的准确续读位置。
def _progress_position(prefix, raw, base, offset, content_offset, line_hash):
    current = prefix.copy()
    current.update(raw[:offset])
    return {"offset": base + offset, "sha256": current.hexdigest(), "content_offset": content_offset,
            **({"line_sha256": line_hash} if content_offset else {})}


# LLM: 展示原账也不能接受重复键隐藏原始kind或身份；不从JSON值获取执行权。
# 函数用途: 拒绝进度JSON行里的歧义字段。
def _unique_progress_fields(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate progress key")
        value[key] = item
    return value
