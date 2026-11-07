# LLM: Durable request SSE is a read-only projection. Hub sequence numbers are never replay authority; all frames follow the existing ownership and public-progress gates.
# 模块用途: 按磁盘字节游标恢复请求事件，复用认证、脱敏和有界流池，不创建或控制请求。
from __future__ import annotations

import json
import socket
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from .http_handlers import (
    _can_read_finished_request,
    _denial_body,
    _FinishedRequestAccess,
    _project_public_progress_rows,
    _request_channel,
    _request_identity,
    _ResultAccessContext,
    handle_result,
    host_paths_visible_to_viewer,
)
from .io import STREAM_CHUNK_READ_MAX_BYTES
from .paths import gateway_chunk_path, gateway_chunk_path_candidates
from .request_event_hub import hub_for_paths


@dataclass(frozen=True)
class DurableCursor:
    request_id: str
    attempt_id: str
    line: int = 0
    offset: int | None = 0

    # LLM: Cursor contains only durable identity and position; it survives ring overflow and process restart.
    # 函数用途: 编码当前完整行边界，旧版无字节偏移游标仍可由读取器恢复。
    def encode(self):
        suffix = "" if self.offset is None else f":{self.offset}"
        return f"v1:{self.request_id}:{self.attempt_id}:{self.line}{suffix}"

    # LLM: Parsing does not grant access or consult volatile hub state.
    # 函数用途: 严格解析非负游标；错误统一交重置分支。
    @classmethod
    def parse(cls, text):
        fields = text.split(":")
        if len(fields) not in (4, 5) or fields[0] != "v1" or not fields[1] or not fields[2]:
            raise ValueError("invalid_cursor")
        if any(not x.isascii() or not x.isdecimal() for x in fields[3:]):
            raise ValueError("invalid_cursor")
        return cls(fields[1], fields[2], int(fields[3]), int(fields[4]) if len(fields) == 5 else None)


# LLM: Reads use one bounded page; uncommitted UTF-8 tails never advance a durable cursor. No full-file replay cache is retained.
# 类用途: 跟踪一次连接的磁盘游标，并在无字节偏移时逐页回退到行号。
class RequestReplayReader:
    # LLM: Identity is obtained from the canonical queue/terminal record, not a hub counter.
    # 函数用途: 校验重连身份，先订阅的调用方随后用此对象重读磁盘。
    def __init__(self, paths, request_id, cursor=""):
        self.paths, self.request_id = paths, request_id
        self.attempt_id = self._attempt()
        self.line, self.offset, self._skip = 0, 0, 0
        self._oversize = 0
        self.reset_required = False
        if cursor:
            try:
                value = DurableCursor.parse(cursor)
                if (value.request_id, value.attempt_id) != (request_id, self.attempt_id):
                    raise ValueError("cursor_identity")
                path = self._path()
                if value.offset is None:
                    self._skip = value.line
                else:
                    if value.offset == 0 and value.line != 0:
                        raise ValueError("cursor_boundary")
                    if path is None and value.offset:
                        raise ValueError("cursor_missing")
                    if path is not None:
                        with path.open("rb") as handle:
                            handle.seek(0, 2)
                            if value.offset > handle.tell():
                                raise ValueError("cursor_past_end")
                            if value.offset:
                                handle.seek(value.offset - 1)
                                if handle.read(1) != b"\n":
                                    raise ValueError("cursor_mid_line")
                    self.line, self.offset = value.line, value.offset
            except (OSError, ValueError, TypeError, OverflowError):
                self.reset_required = True

    # LLM: Only trusted durable request records select the execution attempt; missing records have a stable pending identity.
    # 函数用途: 从原队列或终态记录读取尝试号，不从通知序号猜测。
    def _attempt(self):
        from .io import read_json_file_report
        for folder in (self.paths.processing, self.paths.done, self.paths.failed, self.paths.inbox, self.paths.terminal):
            record = read_json_file_report(folder / f"{self.request_id}.json", context="gateway.events.attempt").payload
            if record:
                return str(record.get("execution_attempt_id") or "pending")
        return "pending"

    # LLM: Archived chunk candidates follow the original queue root, never an owner-selected path.
    # 函数用途: 在原processing/done/failed位置定位同一请求的事件文件。
    def _path(self):
        return next((p for p in gateway_chunk_path_candidates(gateway_chunk_path(self.paths, self.request_id)) if p.is_file()), None)

    # LLM: A per-row cursor describes that row, not the end of the already-read page.
    # 函数用途: 为刚消费的完整原始行生成可恢复游标。
    def cursor(self):
        return DurableCursor(self.request_id, self.attempt_id, self.line, self.offset).encode()

    # LLM: A changed attempt or truncation resets byte/line identity; filtered rows still advance internal disk position.
    # 函数用途: 最多读一页原始字节，返回完整行及各自游标，半行留待后续写完。
    def read_page(self):
        from .progress_reader import _MAX_LINE_BYTES, _oversize_marker
        attempt = self._attempt()
        path = self._path()
        if attempt != self.attempt_id or (path is not None and path.stat().st_size < self.offset):
            self.attempt_id, self.line, self.offset, self._skip, self._oversize = attempt, 0, 0, 0, 0
            self.reset_required = True
        if path is None:
            return (), False
        with path.open("rb") as handle:
            handle.seek(self.offset + self._oversize)
            page = handle.read(STREAM_CHUNK_READ_MAX_BYTES)
        boundary = page.rfind(b"\n")
        if boundary < 0:
            if len(page) == STREAM_CHUNK_READ_MAX_BYTES:
                self._oversize += len(page)
                return (), True
            return (), False
        rows = []
        complete = page[:boundary + 1]
        for raw in complete.split(b"\n")[:-1]:
            self.offset += self._oversize + len(raw) + 1
            self.line += 1
            if self._skip:
                self._skip -= 1
                self._oversize = 0
                continue
            size = self._oversize + len(raw) + 1
            if self._oversize or size > _MAX_LINE_BYTES:
                text = _oversize_marker(self.line - 1, size)
            else:
                try:
                    text = raw.removesuffix(b"\r").decode("utf-8")
                except UnicodeDecodeError:
                    text = ""
            self._oversize = 0
            rows.append((text, self.cursor()))
        return tuple(rows), len(page) == STREAM_CHUNK_READ_MAX_BYTES


# LLM: Recheck the same existing access gate before every public frame; credentials never become an owner selector.
# 函数用途: 校验原请求归属，认证撤销或跨用户读取立即停止。
def _require_access(handler, server, request_id):
    user_id, permission = _request_identity(handler)
    state = _can_read_finished_request(server.paths, _ResultAccessContext(request_id, user_id, permission))
    if state is not _FinishedRequestAccess.ALLOWED:
        raise PermissionError("request_event_access_denied")
    return permission


# LLM: Proxy only captures the existing result HTTP projection; it cannot send another response or bypass ownership.
# 类用途: 复用/result的完整公开终态投影，不另造终态过滤规则。
class _ResultView:
    # LLM: Retain request authentication/headers by delegation and override only the read-only result path.
    # 函数用途: 绑定原请求身份，捕获结果处理器返回值。
    def __init__(self, handler, request_id):
        self._handler = handler
        self.path = f"/result/{request_id}"
        self.result = None

    # LLM: Preserve original handler identity and peer facts without copying secrets.
    # 函数用途: 透传结果读取需要的原请求属性。
    def __getattr__(self, name):
        return getattr(self._handler, name)

    # LLM: Only a canonical terminal 200 ends the stream; pending/error projections never count as completion.
    # 函数用途: 只收取原结果端点确认的终态。
    def _send_json(self, status, payload):
        if status == 200:
            self.result = payload


# LLM: One encoder serves handler-thread and detached transports; ids are durable positions, never volatile ring sequences.
# 函数用途: 按SSE协议编码完整帧，数据中的换行只由JSON转义。
def encode_sse(frame):
    event, data, cursor = frame
    prefix = f"id: {cursor}\n" if cursor else ""
    return (prefix + f"event: {event}\ndata: " + json.dumps(data, ensure_ascii=False, allow_nan=False) + "\n\n").encode("utf-8")


# LLM: Subscribe precedes the first durable read. Wakes merely trigger rereads; missing/overflowed hints do not decide state.
# 函数用途: 按原公开白名单顺序重放事件，尾部读完才发送原终态，否则等待提示或有界兜底。
def request_event_frames(handler, server, request_id, *, cursor="", include=frozenset(), ticket=None):
    from .request_worker import _WAIT_POLL_STEPS_SECONDS
    hub = hub_for_paths(server.paths)
    signal = hub.signal(("request", request_id)) if hub is not None else None
    reader = RequestReplayReader(server.paths, request_id, cursor)
    heartbeat = _WAIT_POLL_STEPS_SECONDS[-1] if hub is None or hub.fallback_poll_seconds <= 0 else hub.fallback_poll_seconds
    try:
        yield "heartbeat", {"request_id": request_id}, ""
        while True:
            if ticket is not None:
                ticket.wait(0)
            permission = _require_access(handler, server, request_id)
            rows, more = reader.read_page()
            if reader.reset_required:
                yield "reset", {"request_id": request_id, "reset_required": True}, ""
                reader.reset_required = False
            channel = _request_channel(handler)[1]
            preserve = host_paths_visible_to_viewer(channel, can_access_all_users=permission is None or permission.can_access_all_users,
                chat_type=(getattr(handler, "headers", {}) or {}).get("X-Channel-Chat-Type", ""))
            for text, row_cursor in rows:
                events, _ = _project_public_progress_rows((text,), channel=channel, include=include, preserve_host_paths=preserve)
                for event in events:
                    _require_access(handler, server, request_id)
                    yield "progress", {"request_id": request_id, "event": event}, row_cursor
            if more:
                continue
            view = _ResultView(handler, request_id)
            handle_result(view, server)
            if view.result is not None:
                # 终态可能在本页读取之后提交：最后再读一遍尾部，不能先发终态而漏掉最后一批。
                tail, more_tail = reader.read_page()
                if tail or more_tail:
                    for text, row_cursor in tail:
                        events, _ = _project_public_progress_rows((text,), channel=channel, include=include, preserve_host_paths=preserve)
                        for event in events:
                            _require_access(handler, server, request_id)
                            yield "progress", {"request_id": request_id, "event": event}, row_cursor
                    if more_tail:
                        continue
                _require_access(handler, server, request_id)
                yield "terminal", {"request_id": request_id, "result": view.result}, reader.cursor()
                return
            if signal is not None:
                signal.wait(heartbeat, None if ticket is None else ticket.stop_event)
            elif ticket is not None:
                ticket.wait(heartbeat)
            else:
                time.sleep(heartbeat)
            yield "heartbeat", {"request_id": request_id}, ""
    finally:
        if signal is not None:
            signal.close()


# LLM: Reserve before writing headers; transfer socket ownership only after a valid first frame, identical to the native business-stream lifecycle.
# 函数用途: 接入两个兼容事件路径，按原归属门返回SSE，容量满明确503，关闭流不取消任务。
def handle_request_events(handler, server):
    from .business_stream_pool import NativeStreamStopped
    if server is None:
        handler._send_json(503, {"error": {"code": "HOST_NOT_READY"}})
        return
    parsed = urlsplit(handler.path)
    prefix = "/client/requests/" if parsed.path.startswith("/client/requests/") else "/v1/requests/"
    request_id = parsed.path[len(prefix):-len("/events")]
    if not request_id or any(x in request_id for x in ("/", "\\", ":")):
        handler._send_json(400, {"error": "invalid request id"})
        return
    try:
        _require_access(handler, server, request_id)
    except PermissionError:
        state = _can_read_finished_request(server.paths, _ResultAccessContext(request_id, *_request_identity(handler)))
        handler._send_json(404 if state is _FinishedRequestAccess.NOT_FOUND else 403,
            _denial_body(handler, {"error": "request event access denied", "request_id": request_id}))
        return
    query = parse_qs(parsed.query, keep_blank_values=True)
    include = frozenset(x.strip() for item in query.get("include", []) for x in item.split(",") if x.strip())
    if include - {"assistant_text_delta", "assistant_text_reset", "assistant_text_done", "workflow_step_progress"}:
        handler._send_json(400, {"error_code": "INVALID_PROGRESS_INCLUDE"})
        return
    cursor = (getattr(handler, "headers", {}) or {}).get("Last-Event-ID", "") or query.get("cursor", query.get("after", [""]))[0]
    pool = getattr(server, "business_stream_pool", None)
    ticket = sock = iterator = None
    detached_fd = None
    handed_off = headers_sent = False
    try:
        if pool is not None:
            if getattr(server, "agent", None) is not None or getattr(server, "control_plane", None) is not None:
                from .business_api import native_business_context
                _, services = native_business_context(handler, server)
                home = services.host.home_paths
                owner_key = (home.owner_id, str(home.owner_home_dir))
            else:
                owner_key = (_request_identity(handler)[0], "standalone")
            ticket = pool.reserve(owner_key)
            if ticket is None:
                body = json.dumps({"error": {"code": "GATEWAY_STREAM_BUSY"}}).encode()
                handler.send_response(503)
                handler.send_header("Content-Type", "application/json; charset=utf-8")
                handler.send_header("Content-Length", str(len(body)))
                handler.send_header("Retry-After", "1")
                handler.end_headers()
                handler.wfile.write(body)
                return
        iterator = request_event_frames(handler, server, request_id, cursor=cursor, include=include, ticket=ticket)
        first = next(iterator)
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Connection", "close")
        handler.end_headers()
        headers_sent = True
        handler.wfile.write(encode_sse(first))
        handler.wfile.flush()
        handler.close_connection = True
        if ticket is not None:
            detached_fd = handler.connection.detach()
            sock = socket.socket(fileno=detached_fd)
            detached_fd = None
            sock.settimeout(pool.write_timeout_seconds)
            ticket.attach(sock)
            pool.submit(ticket, sock, iterator, encode_sse, lambda _: _require_access(handler, server, request_id))
            handed_off = True
        else:
            for frame in iterator:
                _require_access(handler, server, request_id)
                handler.wfile.write(encode_sse(frame))
                handler.wfile.flush()
    except (NativeStreamStopped, OSError, ValueError, RuntimeError, TypeError):
        if not headers_sent:
            handler._send_json(503, {"error": {"code": "HOST_NOT_READY"}})
    finally:
        if not handed_off:
            try:
                if iterator is not None:
                    iterator.close()
            finally:
                if detached_fd is not None:
                    socket.close(detached_fd)
                if sock is not None:
                    sock.close()
                if ticket is not None:
                    ticket.release()
