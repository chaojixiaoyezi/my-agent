# LLM: Process-local Responses-only cache; keys bind host owner/thread, model, credential fingerprint, provider and exact endpoint.
#   Idle entries NEVER own provider leases. State eviction/reset revokes in-flight returns by object identity.
# 模块用途: 保存有界会话亲和状态与空闲WebSocket，主动TTL回收，不持久化秘密或历史。
from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from threading import Lock, Timer
from urllib.parse import urlsplit, urlunsplit

from .gateway_helpers import gateway_request_body
from .provider_headers import current_provider_session


# LLM: 宿主ContextVar决定owner/thread隔离，所有认证/自定义头参与指纹；规范化模型名隔离socket与sticky状态。
#   session保持为末项，原close(session)会一次清理同会话所有模型，不借prompt_cache_key推断宿主身份。
# 函数用途: 生成不含明文凭据、含模型维度的连接池键，无宿主身份时禁用复用。
def session_pool_key(request, provider_id: str = "") -> tuple | None:
    session = current_provider_session()
    if not session:
        return None
    url = urlsplit(request.url)
    port = url.port
    host = (url.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"
    authority = host if not port or port == {"https": 443, "http": 80}.get(url.scheme) else f"{host}:{port}"
    endpoint = urlunsplit((url.scheme.lower(), authority, url.path.rstrip("/"), url.query, ""))
    headers = sorted((name.lower(), value) for name, value in request.headers.items() if name.lower() != "x-codex-turn-state")
    fingerprint = hashlib.sha256(json.dumps([request.api_key, headers], ensure_ascii=True).encode()).hexdigest()
    model = str((request.payload or {}).get("model") or "").strip()
    return provider_id, endpoint, fingerprint, model, session


# LLM: Values are opaque printable ASCII from provider response headers, not custom configuration or logs.
# 类用途: 记录当前会话的短期路由状态及过期时间。
@dataclass(eq=False)
class SessionState:
    expires: float
    turn_state_header: str = field(default="", repr=False)


# LLM: A borrowed entry is request-exclusive; byte snapshots cannot be changed by caller mutations. No lease field exists here.
# 类用途: 保存一条连接及只适用于这条连接的增量历史锚点。
@dataclass(eq=False)
class ConnectionEntry:
    connection: object
    state: SessionState
    expires: float = 0
    _ws_last_response_id: str = ""
    _ws_last_items: bytes = field(default=b"", repr=False)
    options: bytes = field(default=b"", repr=False)

    # LLM: Any failure/compaction clears the entire anchor, not just its ID; closing is best-effort and cannot mask cancellation.
    # 函数用途: 丢弃连接和旧增量锚点。
    def discard(self) -> None:
        self._ws_last_response_id, self._ws_last_items, self.options = "", b"", b""
        try:
            self.connection.close_socket()
        except Exception:
            pass


# LLM: Only idle sockets live in this lock-protected LRU; a single daemon timer closes expired descriptors without further requests.
# 类用途: 为Responses维护有容量和空闲寿命上限的进程内连接池。
class ResponsesSessionPool:
    # LLM: No worker thread starts until a scoped state exists; no filesystem, credentials, or provider calls during construction.
    # 函数用途: 初始化空连接池。
    def __init__(self):
        self._lock = Lock()
        self._idle: OrderedDict[tuple, ConnectionEntry] = OrderedDict()
        self._states: OrderedDict[tuple, SessionState] = OrderedDict()
        self._timer: Timer | None = None

    # LLM: TTL and LRU also bound HTTP sticky metadata; evicting a state revokes any concurrently borrowed entry's return.
    # 函数用途: 取得或新建同一身份的亲和状态，淘汰过期及超额会话。
    def state(self, key, ttl: float, maximum: int) -> SessionState | None:
        if key is None or ttl <= 0 or maximum <= 0:
            return None
        victims = []
        with self._lock:
            victims.extend(self._prune_locked())
            state = self._states.pop(key, None) or SessionState(0)
            state.expires = time.monotonic() + ttl
            self._states[key] = state
            while len(self._states) > min(256, maximum):
                old_key, retired = self._states.popitem(last=False)
                retired.turn_state_header = ""
                entry = self._idle.pop(old_key, None)
                if entry is not None:
                    victims.append(entry)
            self._schedule_locked()
        for entry in victims:
            entry.discard()
        return state

    # LLM: Pop only after locked expiry/generation validation, so a paused pre-reset caller cannot borrow a new generation's socket.
    # 函数用途: 独占借出一条空闲连接；没有则交调用方建新连接。
    def take(self, key, state: SessionState | None = None) -> ConnectionEntry | None:
        with self._lock:
            victims = self._prune_locked()
            entry = self._idle.get(key)
            if entry is not None and (state is None or entry.state is state):
                self._idle.pop(key)
            else:
                entry = None
        for victim in victims:
            victim.discard()
        return entry

    # LLM: Caller already released its lease and checked for unread frames; rejected/evicted entries are closed outside the lock.
    # 函数用途: 仅将仍属有效会话的干净连接归还池，超额时按LRU关闭。
    def put(self, key, entry: ConnectionEntry, ttl: float, maximum: int) -> None:
        victims = []
        with self._lock:
            if self._states.get(key) is not entry.state:
                victims.append(entry)
            else:
                entry.expires = entry.state.expires = time.monotonic() + ttl
                previous = self._idle.pop(key, None)
                if previous is not None:
                    victims.append(previous)
                self._idle[key] = entry
                while len(self._idle) > min(256, maximum):
                    victims.append(self._idle.popitem(last=False)[1])
                self._schedule_locked()
        for victim in victims:
            victim.discard()

    # LLM: Header writes are identity-checked; a late HTTP response cannot resurrect a reset or evicted session.
    # 函数用途: 只记住合法的服务端路由状态，不保存完整响应头。
    def capture(self, key, state: SessionState | None, headers) -> None:
        if state is None or headers is None:
            return
        values = [value for name, value in headers.items() if name.lower() == "x-codex-turn-state"]
        if len(values) != 1 or not isinstance(values[0], str):
            return
        value = values[0]
        if not value or len(value) > 4096 or any(ord(char) < 32 or ord(char) > 126 for char in value):
            return
        with self._lock:
            if self._states.get(key) is state:
                state.turn_state_header = value

    # LLM: Removing state identity revokes pending returns; session=None is explicit whole-pool shutdown (tests/process teardown).
    # 函数用途: 清除reset会话的路由头、连接与增量链。
    def close(self, session: str | None = None) -> None:
        with self._lock:
            keys = [key for key in self._states if session is None or key[-1] == session]
            victims = [self._idle.pop(key) for key in keys if key in self._idle]
            for key in keys:
                self._states.pop(key).turn_state_header = ""
            self._schedule_locked()
        for entry in victims:
            entry.discard()

    # LLM: Called only with lock held; returns victims so closing a network resource can never block the pool lock.
    # 函数用途: 从池内摘掉过期会话和连接。
    def _prune_locked(self) -> list[ConnectionEntry]:
        now = time.monotonic()
        keys = [key for key, state in self._states.items() if state.expires <= now]
        for key in keys:
            self._states.pop(key).turn_state_header = ""
        expired = [key for key, entry in self._idle.items() if key not in self._states or entry.expires <= now]
        return [self._idle.pop(key) for key in expired]

    # LLM: At most one pending timer per pool; timer callbacks recalculate under the same lock, including stale cancelled callbacks.
    # 函数用途: 将下一次主动清理安排在最近的过期时刻。
    def _schedule_locked(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        deadlines = [state.expires for state in self._states.values()] + [entry.expires for entry in self._idle.values()]
        self._timer = None
        if deadlines:
            self._timer = Timer(max(.001, min(deadlines) - time.monotonic()), self._expire)
            self._timer.daemon = True
            self._timer.start()

    # LLM: Timer never borrows leases or performs network reads; it only closes expired idle descriptors.
    # 函数用途: 到期主动回收空闲连接，并安排下一次清理。
    def _expire(self) -> None:
        with self._lock:
            victims = self._prune_locked()
            self._schedule_locked()
        for entry in victims:
            entry.discard()


# LLM: Exact wire bytes (including key order/types) must match both request options and input prefix; output uses the existing IR replay.
# 函数用途: 仅在可证明完整前缀一致时改发增量，不修改原始请求。
def incremental_payload(entry: ConnectionEntry, payload: dict) -> dict:
    items = payload.get("input")
    options = gateway_request_body({key: value for key, value in payload.items() if key not in {"input", "previous_response_id"}})
    if entry._ws_last_response_id and isinstance(items, list) and entry.options == options:
        previous = json.loads(entry._ws_last_items)
        if len(items) > len(previous) and gateway_request_body(items[:len(previous)]) == entry._ws_last_items:
            return {**payload, "input": items[len(previous):], "previous_response_id": entry._ws_last_response_id}
    entry._ws_last_response_id, entry._ws_last_items, entry.options = "", b"", b""
    return payload


# LLM: 空终态output的完整done事件先复用既有collector排序/去重，保留_CollectedResponse的真实索引与冲突属性；
#   非空终态output仍权威。随后用同一个native重放codec生成下轮前缀，压缩或结构冲突不建立旧链。
# 函数用途: 按正常响应收集器同一顺序保存连接历史锚点，避免乱序done打断增量续接。
def remember_completed(entry: ConnectionEntry, payload: dict, response: dict, done: list) -> None:
    from .responses_wire import collect_response, message_items, response_fields

    entry._ws_last_response_id, entry._ws_last_items, entry.options = "", b"", b""
    if not response.get("output") and done:
        events = [*done, {"type": "response.completed", "response": response}]
        response = collect_response((json.dumps(event, ensure_ascii=False) for event in events), None, None)
    output = response.get("output") or []
    if any(item.get("type") in {"compaction", "compaction_trigger"} for item in [*payload.get("input", []), *output]):
        return
    fields = response_fields(response, str(payload.get("model") or ""))
    if fields.get("truncated"):
        return
    replay = message_items({"role": "assistant", "content": fields["assistant_content_blocks"]}, str(payload.get("model") or ""))
    entry._ws_last_response_id = response["id"]
    entry._ws_last_items = gateway_request_body([*payload.get("input", []), *replay])
    entry.options = gateway_request_body({key: value for key, value in payload.items() if key not in {"input", "previous_response_id"}})


# LLM: Provider tokens are observations: absent/malformed cache counters remain unknown, never fabricated as a zero hit rate.
# 函数用途: 在响应usage内附加实际缓存计数及本次连接/增量复用事实。
def cache_usage(usage, *, reused: bool = False, incremental: bool = False) -> dict:
    result = dict(usage) if isinstance(usage, dict) else {}
    total = result.get("input_tokens")
    details = result.get("input_tokens_details")
    cached = details.get("cached_tokens") if isinstance(details, dict) else result.get("cached_tokens")
    valid = type(total) is int and type(cached) is int and 0 <= cached <= total
    result.update(cached_tokens=cached if valid else None, uncached_input_tokens=total - cached if valid else None,
                  cache_hit_rate=cached / total if valid and total else None,
                  ws_connection_reused=reused, incremental_chain_hit=incremental)
    return result
